"""Users, sessions and the login limit kept in Postgres (cloud version).

The rules are inherited unchanged from web.auth (same lockout, same messages, last-admin protection,
password hashing outside the lock); only where the data lives changes. Session tokens are stored as
SHA-256 fingerprints, so a copy of the database cannot be used to log in.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Callable, Optional

from psycopg.types.json import Jsonb

from engine.clock import now_local
from storage.pg import Database, _history

from .auth import IP_MAX_ATTEMPTS, IP_WINDOW_SECONDS, SESSION_IDLE_SECONDS, User, UserStore


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PgUserStore(UserStore):
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self.db = db
        self.clock = clock

    @property
    def lock(self):
        """`with self.lock:` = one transaction holding the users lock (replaces the file lock)."""
        return self.db.transaction(lock="users")

    def _load(self) -> dict[str, User]:
        with self.db.transaction() as c:
            rows = c.execute("SELECT data FROM users").fetchall()
        return {r[0]["username"]: User.model_validate(r[0]) for r in rows}

    def _save(self, users: dict[str, User], actor: str) -> None:
        with self.db.transaction(lock="users") as c:
            for u in users.values():
                c.execute("INSERT INTO users (username, data) VALUES (%s, %s) "
                          "ON CONFLICT (username) DO UPDATE SET data = EXCLUDED.data",
                          (u.username, Jsonb(u.model_dump(mode="json"))))
            c.execute("DELETE FROM users WHERE NOT (username = ANY(%s))", (list(users),))
            snapshot = [u.model_dump(mode="json") for u in sorted(users.values(), key=lambda x: x.username)]
            _history(c, "users", "_users", "_users", 0, actor, now_local(), snapshot)


class PgSessionManager:
    def __init__(self, db: Database, idle_seconds: int = SESSION_IDLE_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.db, self.idle, self.clock = db, idle_seconds, clock

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.db.transaction() as c:
            c.execute("DELETE FROM sessions WHERE last_seen < %s", (now - self.idle,))
            c.execute("INSERT INTO sessions (token_hash, username, last_seen) VALUES (%s, %s, %s)",
                      (_fingerprint(token), username, now))
        return token

    def touch(self, token: Optional[str]) -> Optional[str]:
        if not token or len(token) > 200:
            return None
        now = self.clock()
        with self.db.transaction() as c:
            r = c.execute("UPDATE sessions SET last_seen = %s WHERE token_hash = %s AND last_seen >= %s "
                          "RETURNING username", (now, _fingerprint(token), now - self.idle)).fetchone()
        return r[0] if r else None

    def end(self, token: Optional[str]) -> None:
        if not token:
            return
        with self.db.transaction() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = %s", (_fingerprint(token),))

    def end_user(self, username: str, keep: Optional[str] = None) -> None:
        with self.db.transaction() as c:
            c.execute("DELETE FROM sessions WHERE username = %s AND token_hash <> %s",
                      (username, _fingerprint(keep) if keep else ""))


class PgLoginThrottle:
    def __init__(self, db: Database, max_attempts: int = IP_MAX_ATTEMPTS, window: int = IP_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.db, self.max, self.window, self.clock = db, max_attempts, window, clock

    def allow(self, ip: str) -> bool:
        now = self.clock()
        with self.db.transaction(lock=f"ip:{ip}") as c:
            c.execute("DELETE FROM login_attempts WHERE at < %s", (now - self.window,))
            n = c.execute("SELECT count(*) FROM login_attempts WHERE ip = %s", (ip,)).fetchone()[0]
            if n >= self.max:
                return False
            c.execute("INSERT INTO login_attempts (ip, at) VALUES (%s, %s)", (ip, now))
            return True
