"""The cloud (Vercel) app: same screens and API, data in Postgres (Neon).

Environment variables (set in the Vercel project):
    DATABASE_URL                 added automatically when the Neon database is connected to the project
                                 (POSTGRES_URL is used if DATABASE_URL is missing)
Defaults applied by the cloud app (change in the Vercel dashboard only if needed):
    BOND_APP_UTC_OFFSET_HOURS    6  -> 'today' and 'printed on' in Bangladesh time
    BOND_APP_TRUST_PROXY         1  -> client address from Vercel's X-Forwarded-For
    BOND_APP_DOCS                0  -> no /docs API test page in the cloud
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import FastAPI

from storage.pg import Database, PgRepository

from .app import create_app
from .pg_auth import PgLoginThrottle, PgSessionManager, PgUserStore


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""


CLOUD_DEFAULTS = {
    "BOND_APP_UTC_OFFSET_HOURS": "6",      # Vercel servers run on UTC; show Bangladesh dates and times
    "BOND_APP_TRUST_PROXY": "1",           # Vercel's edge sets X-Forwarded-For / X-Forwarded-Proto
    "BOND_APP_DOCS": "0",                  # no public /docs API page in the cloud
}


def create_cloud_app(url: Optional[str] = None, clock=None, today=None, docs: Optional[bool] = None) -> FastAPI:
    for k, v in CLOUD_DEFAULTS.items():    # a value set in the Vercel dashboard still wins
        os.environ.setdefault(k, v)
    db = Database(url or database_url())
    db.create_schema()                       # CREATE TABLE IF NOT EXISTS: safe on every cold start
    extra = {"clock": clock} if clock else {}
    app = create_app(backends=(PgRepository(db), PgUserStore(db, **extra), PgSessionManager(db, **extra),
                               PgLoginThrottle(db, **extra)), today=today, docs=docs)
    app.state.db = db
    return app
