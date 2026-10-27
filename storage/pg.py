"""Postgres storage for the cloud (Vercel + Neon). Same methods and errors as FileRepository.

Tables (created automatically on first use):
    companies(code, data)          funds(code, company, data)
    bonds(fund_code, isin, version, data)
    history(id, kind, scope, key, version, username, created_at, data)    every save, like the history folder
    deleted_bonds(id, fund_code, isin, deleted_by, deleted_at, data)       recoverable deletes
    users(username, data)          sessions(token_hash, username, last_seen)    login_attempts(ip, at)

Connections come from a small pool. Statements are never prepared server-side, so Neon's pooled
connection string (PgBouncer in transaction mode) works. Locks are transaction-scoped advisory locks,
which also work through PgBouncer.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from engine.clock import now_local

from .records import BondRecord, Company, Fund
from .repository import AlreadyExists, FileRepository, NotFound, VersionConflict

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (code text PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS funds (code text PRIMARY KEY, company text, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS bonds (
    fund_code text NOT NULL REFERENCES funds(code), isin text NOT NULL, version integer NOT NULL,
    data jsonb NOT NULL, PRIMARY KEY (fund_code, isin));
CREATE TABLE IF NOT EXISTS history (
    id bigserial PRIMARY KEY, kind text NOT NULL, scope text NOT NULL, key text NOT NULL,
    version integer NOT NULL DEFAULT 0, username text NOT NULL, created_at timestamp NOT NULL, data jsonb NOT NULL);
CREATE INDEX IF NOT EXISTS history_lookup ON history (kind, scope, key, id);
CREATE TABLE IF NOT EXISTS deleted_bonds (
    id bigserial PRIMARY KEY, fund_code text NOT NULL, isin text NOT NULL, deleted_by text NOT NULL,
    deleted_at timestamp NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS users (username text PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash text PRIMARY KEY, username text NOT NULL, last_seen double precision NOT NULL);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (username);
CREATE TABLE IF NOT EXISTS login_attempts (ip text NOT NULL, at double precision NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempts_ip ON login_attempts (ip, at);
"""


class Database:
    """A connection pool plus helpers. One per process."""

    def __init__(self, url: str, max_size: int = 4):
        if not url:
            raise RuntimeError("DATABASE_URL is not set. Add the Neon database to the Vercel project "
                               "(Storage -> Neon) or set DATABASE_URL.")
        self.pool = ConnectionPool(url, min_size=0, max_size=max_size, open=True, timeout=15,
                                   kwargs={"prepare_threshold": None, "connect_timeout": 10},
                                   max_idle=60, check=ConnectionPool.check_connection)
        self._local = threading.local()

    def create_schema(self) -> None:
        with self.transaction(lock="schema") as c:
            c.execute(SCHEMA)

    @contextmanager
    def transaction(self, lock: Optional[str] = None) -> Iterator[psycopg.Connection]:
        """One transaction. Nested calls in the same thread reuse it. `lock` takes an advisory lock
        (released automatically at commit) so concurrent saves of the same thing queue up."""
        cur = getattr(self._local, "conn", None)
        if cur is not None:
            if lock:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
            yield cur
            return
        with self.pool.connection() as conn:
            with conn.transaction():
                self._local.conn = conn
                try:
                    if lock:
                        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock,))
                    yield conn
                finally:
                    self._local.conn = None

    def close(self) -> None:
        self.pool.close()


def _history(c, kind: str, scope: str, key: str, version: int, user: str, when: datetime, data: dict) -> None:
    c.execute("INSERT INTO history (kind, scope, key, version, username, created_at, data) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s)", (kind, scope, key, version, user[:40], when, Jsonb(data)))


class PgRepository:
    """Drop-in replacement for FileRepository backed by Postgres."""

    _code = staticmethod(FileRepository._code)
    _isin = staticmethod(FileRepository._isin)

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------ companies
    def list_companies(self, include_archived: bool = False) -> list[Company]:
        with self.db.transaction() as c:
            rows = c.execute("SELECT data FROM companies ORDER BY code").fetchall()
        out = [Company.model_validate(r[0]) for r in rows]
        return [x for x in out if include_archived or not x.archived]

    def get_company(self, code: str) -> Company:
        code = self._code(code, "company")
        with self.db.transaction() as c:
            r = c.execute("SELECT data FROM companies WHERE code = %s", (code,)).fetchone()
        if r is None:
            raise NotFound(f"company {code} not found")
        return Company.model_validate(r[0])

    def save_company(self, company: Company, user: str, create: bool = False) -> Company:
        data = company.model_dump(mode="json")
        with self.db.transaction(lock="companies") as c:
            exists = c.execute("SELECT 1 FROM companies WHERE code = %s", (company.code,)).fetchone()
            if create and exists:
                raise AlreadyExists(f"company {company.code} already exists")
            if not create and not exists:
                raise NotFound(f"company {company.code} not found")
            c.execute("INSERT INTO companies (code, data) VALUES (%s, %s) "
                      "ON CONFLICT (code) DO UPDATE SET data = EXCLUDED.data", (company.code, Jsonb(data)))
            _history(c, "company", "_companies", company.code, 0, user, now_local(), data)
        return company

    # ------------------------------------------------------------ funds
    def list_funds(self, include_archived: bool = False, company: Optional[str] = None) -> list[Fund]:
        with self.db.transaction() as c:
            rows = c.execute("SELECT data FROM funds ORDER BY code").fetchall()
        out = []
        for (d,) in rows:
            f = Fund.model_validate(d)
            if company is not None and (f.company or "") != company.strip().upper():
                continue
            if include_archived or not f.archived:
                out.append(f)
        return out

    def get_fund(self, code: str) -> Fund:
        code = self._code(code)
        with self.db.transaction() as c:
            r = c.execute("SELECT data FROM funds WHERE code = %s", (code,)).fetchone()
        if r is None:
            raise NotFound(f"fund {code} not found")
        return Fund.model_validate(r[0])

    def save_fund(self, fund: Fund, user: str, create: bool = False) -> Fund:
        if fund.company is not None:
            self.get_company(fund.company)
        data = fund.model_dump(mode="json")
        with self.db.transaction(lock=f"fund:{fund.code}") as c:
            exists = c.execute("SELECT 1 FROM funds WHERE code = %s", (fund.code,)).fetchone()
            if create and exists:
                raise AlreadyExists(f"fund {fund.code} already exists")
            if not create and not exists:
                raise NotFound(f"fund {fund.code} not found")
            c.execute("INSERT INTO funds (code, company, data) VALUES (%s, %s, %s) ON CONFLICT (code) "
                      "DO UPDATE SET company = EXCLUDED.company, data = EXCLUDED.data",
                      (fund.code, fund.company, Jsonb(data)))
            _history(c, "fund", fund.code, "_fund", 0, user, now_local(), data)
        return fund

    # ------------------------------------------------------------ bonds
    def list_bonds(self, code: str) -> list[BondRecord]:
        code = self._code(code)
        self.get_fund(code)
        with self.db.transaction() as c:
            rows = c.execute("SELECT data FROM bonds WHERE fund_code = %s ORDER BY isin", (code,)).fetchall()
        return [BondRecord.model_validate(r[0]) for r in rows]

    def get_bond(self, code: str, isin: str) -> BondRecord:
        code, isin = self._code(code), self._isin(isin)
        with self.db.transaction() as c:
            r = c.execute("SELECT data FROM bonds WHERE fund_code = %s AND isin = %s", (code, isin)).fetchone()
        if r is None:
            raise NotFound(f"bond {isin} not found in fund {code}")
        return BondRecord.model_validate(r[0])

    def save_bond(self, code: str, record: BondRecord, user: str,
                  expected_version: Optional[int] = None) -> BondRecord:
        code = self._code(code)
        self.get_fund(code)
        isin = record.isin
        with self.db.transaction(lock=f"fund:{code}") as c:
            row = c.execute("SELECT version FROM bonds WHERE fund_code = %s AND isin = %s FOR UPDATE",
                            (code, isin)).fetchone()
            if expected_version is None:
                if row is not None:
                    raise AlreadyExists(f"bond {isin} already exists in fund {code}")
                new_version = 1
            else:
                if row is None:
                    raise NotFound(f"bond {isin} not found in fund {code}")
                if row[0] != expected_version:
                    raise VersionConflict(f"bond {isin} was changed by someone else (version {row[0]}, "
                                          f"you had {expected_version}); reload and try again")
                new_version = row[0] + 1
            when = now_local()
            saved = record.model_copy(update={"version": new_version, "updated_by": user,
                                              "updated_at": when.isoformat(timespec="seconds")})
            data = saved.model_dump(mode="json")
            c.execute("INSERT INTO bonds (fund_code, isin, version, data) VALUES (%s, %s, %s, %s) "
                      "ON CONFLICT (fund_code, isin) DO UPDATE SET version = EXCLUDED.version, data = EXCLUDED.data",
                      (code, isin, new_version, Jsonb(data)))
            _history(c, "bond", code, isin, new_version, user, when, data)
        return saved

    def delete_bond(self, code: str, isin: str, user: str, expected_version: int) -> None:
        code, isin = self._code(code), self._isin(isin)
        with self.db.transaction(lock=f"fund:{code}") as c:
            row = c.execute("SELECT version, data FROM bonds WHERE fund_code = %s AND isin = %s FOR UPDATE",
                            (code, isin)).fetchone()
            if row is None:
                raise NotFound(f"bond {isin} not found in fund {code}")
            if row[0] != expected_version:
                raise VersionConflict(f"bond {isin} was changed by someone else; reload and try again")
            c.execute("INSERT INTO deleted_bonds (fund_code, isin, deleted_by, deleted_at, data) "
                      "VALUES (%s, %s, %s, %s, %s)", (code, isin, user[:40], now_local(), Jsonb(row[1])))
            c.execute("DELETE FROM bonds WHERE fund_code = %s AND isin = %s", (code, isin))

    def bond_history(self, code: str, isin: str) -> list[dict]:
        code, isin = self._code(code), self._isin(isin)
        with self.db.transaction() as c:
            rows = c.execute("SELECT id, created_at, username, version FROM history "
                             "WHERE kind = 'bond' AND scope = %s AND key = %s ORDER BY id", (code, isin)).fetchall()
        return [{"file": f"h{i}", "timestamp": ts.isoformat(), "user": u, "version": v} for i, ts, u, v in rows]

    def read_history_version(self, code: str, isin: str, file_name: str) -> BondRecord:
        code, isin = self._code(code), self._isin(isin)
        if not (file_name or "").startswith("h") or not file_name[1:].isdigit() or len(file_name) > 20:
            raise NotFound("history entry not found")
        with self.db.transaction() as c:
            r = c.execute("SELECT data FROM history WHERE id = %s AND kind = 'bond' AND scope = %s AND key = %s",
                          (int(file_name[1:]), code, isin)).fetchone()
        if r is None:
            raise NotFound("history entry not found")
        return BondRecord.model_validate(r[0])

    # ------------------------------------------------------------ import (one-time copy from the office PC)
    def import_bond(self, code: str, rec: BondRecord) -> None:
        """Insert a bond exactly as saved on the PC (keeps its version number), without new history."""
        with self.db.transaction(lock=f"fund:{code}") as c:
            c.execute("INSERT INTO bonds (fund_code, isin, version, data) VALUES (%s, %s, %s, %s) "
                      "ON CONFLICT (fund_code, isin) DO UPDATE SET version = EXCLUDED.version, data = EXCLUDED.data",
                      (code, rec.isin, rec.version, Jsonb(rec.model_dump(mode="json"))))

    def import_history(self, kind: str, scope: str, key: str, version: int, user: str, when: datetime,
                       data) -> None:
        with self.db.transaction() as c:
            _history(c, kind, scope, key, version, user, when, data)

    def import_deleted(self, code: str, isin: str, user: str, when: datetime, data: dict) -> None:
        with self.db.transaction() as c:
            c.execute("INSERT INTO deleted_bonds (fund_code, isin, deleted_by, deleted_at, data) "
                      "VALUES (%s, %s, %s, %s, %s)", (code, isin, user[:40], when, Jsonb(data)))
