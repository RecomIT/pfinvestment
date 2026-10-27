"""Users, passwords and login sessions.

* Passwords: PBKDF2-SHA256 (Python standard library), 600,000 iterations, random salt.
* Users are stored in data/users.json (locked, atomic writes, history copies like bonds).
* Sessions live in memory: a random token in an HttpOnly cookie, expiring after
  SESSION_IDLE_SECONDS of inactivity. Restarting the app logs everyone out.
* After MAX_FAILED_LOGINS wrong passwords an account is locked for LOCKOUT_SECONDS.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, field_validator

from storage.repository import _atomic_write, _stamp
from engine.clock import now_local

SESSION_IDLE_SECONDS = 60 * 60          # agreed: 1 hour idle
MAX_FAILED_LOGINS = 5
LOCKOUT_SECONDS = 15 * 60
LOGIN_FAILED_MESSAGE = ("Wrong username or password. After 5 wrong attempts an account is locked for "
                        "15 minutes (an admin can unlock it sooner).")
IP_WINDOW_SECONDS = 15 * 60
IP_MAX_ATTEMPTS = 30            # login attempts per network address per window (checked before hashing)
MAX_PASSWORD_LENGTH = 200
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


class Role(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


RANK = {Role.VIEWER: 1, Role.EDITOR: 2, Role.ADMIN: 3}


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    full_name: str = Field(default="", max_length=100)
    role: Role
    active: bool = True
    must_change_password: bool = True
    password_hash: str
    failed_logins: int = 0
    locked_until: float = 0.0
    created_at: str = ""

    @field_validator("username")
    @classmethod
    def _uname(cls, v: str) -> str:
        v = v.strip().lower()
        if not _USERNAME_RE.match(v):
            raise ValueError("Username: 3-32 characters, lowercase letters, digits, '.', '_' or '-'")
        return v

    def public(self) -> dict:
        return {"username": self.username, "full_name": self.full_name, "role": self.role.value,
                "active": self.active, "must_change_password": self.must_change_password,
                "locked": self.locked_until > time.time(), "created_at": self.created_at}


class AuthError(Exception):
    pass


def check_password_strength(pw: str) -> None:
    if len(pw) > MAX_PASSWORD_LENGTH:
        raise AuthError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    if len(pw) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if pw.isdigit() or pw.isalpha():
        raise AuthError("password must contain both letters and digits")


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# A fixed hash used to spend the same time when the username does not exist.
_DUMMY_HASH = hash_password("dummy-password-1")


class UserStore:
    def __init__(self, data_dir: str | Path, clock: Callable[[], float] = time.time):
        self.root = Path(data_dir)
        self.path = self.root / "users.json"
        self.lock = FileLock(str(self.root / "locks" / "users.lock"), timeout=10)
        (self.root / "locks").mkdir(parents=True, exist_ok=True)
        self.clock = clock

    def _load(self) -> dict[str, User]:
        if not self.path.is_file():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {u["username"]: User.model_validate(u) for u in raw}

    def _save(self, users: dict[str, User], actor: str) -> None:
        text = json.dumps([u.model_dump(mode="json") for u in sorted(users.values(), key=lambda u: u.username)],
                          indent=2)
        _atomic_write(self.path, text)
        hist = self.root / "history" / "_users" / f"{_stamp(now_local())}__{actor}.json"
        _atomic_write(hist, text)

    # ------------------------------------------------------------ queries
    def list(self) -> list[User]:
        return sorted(self._load().values(), key=lambda u: u.username)

    def get(self, username: str) -> Optional[User]:
        return self._load().get(username.strip().lower())

    def count_admins(self, users: Optional[dict[str, User]] = None) -> int:
        users = users if users is not None else self._load()
        return sum(1 for u in users.values() if u.role == Role.ADMIN and u.active)

    # ------------------------------------------------------------ changes
    def create(self, username: str, password: str, role: Role, full_name: str, actor: str,
               must_change_password: bool = True) -> User:
        check_password_strength(password)
        with self.lock:
            users = self._load()
            u = User(username=username, full_name=full_name, role=role, password_hash=hash_password(password),
                     must_change_password=must_change_password,
                     created_at=now_local().isoformat(timespec="seconds"))
            if u.username in users:
                raise AuthError(f"user {u.username} already exists")
            users[u.username] = u
            self._save(users, actor)
            return u

    def update(self, username: str, actor: str, *, role: Optional[Role] = None, active: Optional[bool] = None,
               full_name: Optional[str] = None, unlock: bool = False) -> User:
        with self.lock:
            users = self._load()
            u = users.get(username)
            if u is None:
                raise AuthError("user not found")
            changes: dict = {}
            if role is not None:
                changes["role"] = role
            if active is not None:
                changes["active"] = active
            if full_name is not None:
                changes["full_name"] = full_name
            if unlock:
                changes.update(failed_logins=0, locked_until=0.0)
            new = u.model_copy(update=changes)
            trial = {**users, username: new}
            if u.role == Role.ADMIN and self.count_admins(trial) == 0:
                raise AuthError("cannot remove or deactivate the last active admin")
            users[username] = new
            self._save(users, actor)
            return new

    def set_password(self, username: str, new_password: str, actor: str, must_change: bool) -> None:
        check_password_strength(new_password)
        with self.lock:
            users = self._load()
            u = users.get(username)
            if u is None:
                raise AuthError("user not found")
            users[username] = u.model_copy(update={
                "password_hash": hash_password(new_password), "must_change_password": must_change,
                "failed_logins": 0, "locked_until": 0.0})
            self._save(users, actor)

    def authenticate(self, username: str, password: str) -> User:
        """Returns the user or raises AuthError.

        Unknown user, wrong password and locked account all give the same message, so the reply
        never reveals whether a username exists. The slow password check runs outside the lock,
        so one login never makes the others wait.
        """
        uname = (username or "").strip().lower()[:64]
        if len(password or "") > MAX_PASSWORD_LENGTH:
            raise AuthError(LOGIN_FAILED_MESSAGE)
        u = self._load().get(uname)
        if u is None:
            verify_password(password, _DUMMY_HASH)          # same time as a real check
            raise AuthError(LOGIN_FAILED_MESSAGE)
        if u.locked_until > self.clock():
            verify_password(password, _DUMMY_HASH)
            raise AuthError(LOGIN_FAILED_MESSAGE)
        ok = verify_password(password, u.password_hash)
        # Decide inside the lock, raise AFTER it: with database storage an error raised inside the
        # lock would also undo the saved failed-attempt count, and the lockout would never trigger.
        error: Optional[str] = None
        with self.lock:                                     # re-read: someone may have changed it
            users = self._load()
            cur = users.get(uname)
            if cur is None:
                error = LOGIN_FAILED_MESSAGE
            elif not ok:
                failed = cur.failed_logins + 1
                upd = {"failed_logins": failed}
                if failed >= MAX_FAILED_LOGINS:
                    upd.update(failed_logins=0, locked_until=self.clock() + LOCKOUT_SECONDS)
                users[uname] = cur.model_copy(update=upd)
                self._save(users, "system")
                error = LOGIN_FAILED_MESSAGE
            elif cur.password_hash != u.password_hash:     # password changed while we were checking
                error = LOGIN_FAILED_MESSAGE
            elif not cur.active:
                error = "this account is disabled"
            elif cur.failed_logins:
                cur = cur.model_copy(update={"failed_logins": 0, "locked_until": 0.0})
                users[uname] = cur
                self._save(users, "system")
        if error:
            raise AuthError(error)
        return cur


class SessionManager:
    def __init__(self, idle_seconds: int = SESSION_IDLE_SECONDS, clock: Callable[[], float] = time.time):
        self.idle = idle_seconds
        self.clock = clock
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            for t in [t for t, (_, last) in self._sessions.items() if now - last > self.idle]:
                del self._sessions[t]                          # drop expired sessions
            self._sessions[token] = (username, now)
        return token

    def touch(self, token: Optional[str]) -> Optional[str]:
        """Username for a live session (and refresh its idle timer), else None."""
        if not token:
            return None
        now = self.clock()
        with self._lock:
            s = self._sessions.get(token)
            if s is None:
                return None
            username, last = s
            if now - last > self.idle:
                del self._sessions[token]
                return None
            self._sessions[token] = (username, now)
            return username

    def end(self, token: Optional[str]) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def end_user(self, username: str, keep: Optional[str] = None) -> None:
        """End every session of a user (optionally keeping one, e.g. the one changing the password)."""
        with self._lock:
            for t in [t for t, (u, _) in self._sessions.items() if u == username and t != keep]:
                del self._sessions[t]


class LoginThrottle:
    """Limits login attempts per network address, checked BEFORE the slow password hash, so
    nobody can burn the server's CPU (or block logins) by sending many attempts."""

    def __init__(self, max_attempts: int = IP_MAX_ATTEMPTS, window: int = IP_WINDOW_SECONDS,
                 clock: Callable[[], float] = time.time):
        self.max, self.window, self.clock = max_attempts, window, clock
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = self.clock()
        with self._lock:
            hits = [h for h in self._hits.get(ip, []) if now - h < self.window]
            if len(hits) >= self.max:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            if len(self._hits) > 10_000:                       # keep memory bounded
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window}
            return True
