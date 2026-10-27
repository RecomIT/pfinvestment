"""Admin tasks for the cloud (Neon) database, run from your own PC.

First set the database address (Neon dashboard -> your project -> Connect -> copy the connection string):
    Windows Command Prompt:   set DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require

Commands:
    python manage_cloud.py init-db                         create the tables (also done automatically by the app)
    python manage_cloud.py import-data "D:\\...\\bond_app\\data"   copy companies, funds, bonds, users and history
                                                           from the office app's data folder (one time)
    python manage_cloud.py create-admin                    create an admin user in the cloud
    python manage_cloud.py reset-password                  set a new password (user must change it at next login)
    python manage_cloud.py list-users
    python manage_cloud.py check                           counts of everything stored in the cloud
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from psycopg.types.json import Jsonb  # noqa: E402

from storage import BondRecord, Company, FileRepository, Fund  # noqa: E402
from storage.pg import Database, PgRepository  # noqa: E402
from web.auth import AuthError, Role, User  # noqa: E402
from web.pg_auth import PgUserStore  # noqa: E402


def _db() -> Database:
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url:
        print("DATABASE_URL is not set. Copy the connection string from the Neon dashboard, then run:\n"
              "    set DATABASE_URL=postgresql://...      (Windows Command Prompt)")
        sys.exit(2)
    db = Database(url)
    db.create_schema()
    return db


def _ask_password() -> str:
    while True:
        p1 = getpass.getpass("Password (min 8 chars, letters and digits): ")
        if p1 == getpass.getpass("Repeat password: "):
            return p1
        print("Passwords do not match, try again.")


def _counts(db: Database) -> dict:
    with db.transaction() as c:
        return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("companies", "funds", "bonds", "users", "history", "deleted_bonds")}


# ------------------------------------------------------------------ commands
def init_db() -> int:
    db = _db()
    print("Tables are ready:", _counts(db))
    return 0


def check() -> int:
    for k, v in _counts(_db()).items():
        print(f"{k:15} {v}")
    return 0


def create_admin() -> int:
    store = PgUserStore(_db())
    username = input("Admin username: ").strip()
    full_name = input("Full name: ").strip()
    try:
        store.create(username, _ask_password(), Role.ADMIN, full_name, actor="manage_cloud.py", must_change_password=False)
    except (AuthError, ValueError) as e:
        print(f"Error: {e}")
        return 1
    print(f"Admin '{username.lower()}' created in the cloud database.")
    return 0


def reset_password() -> int:
    store = PgUserStore(_db())
    username = input("Username: ").strip().lower()
    if store.get(username) is None:
        print("Error: user not found")
        return 1
    store.set_password(username, _ask_password(), actor="manage_cloud.py", must_change=True)
    store.update(username, "manage_cloud.py", unlock=True)
    print(f"Password reset for '{username}'. They must change it at next login.")
    return 0


def list_users() -> int:
    for u in PgUserStore(_db()).list():
        print(f"{u.username:20} {u.role.value:7} {'active' if u.active else 'disabled':9} {u.full_name}")
    return 0


def _stamp(s: str) -> datetime:
    return datetime.strptime(s, "%Y%m%dT%H%M%S%f")


def import_data(data_dir: str, force: bool = False) -> int:
    """Copy everything from the office app's data folder into the cloud database."""
    src = Path(data_dir)
    if not (src / "funds").is_dir():
        print(f"Error: {src} does not look like the app's data folder (no 'funds' folder inside).")
        return 1
    db = _db()
    before = _counts(db)
    if any(before[k] for k in ("companies", "funds", "bonds", "users")) and not force:
        print("The cloud database already has data:", before)
        print("Nothing was copied. To copy anyway (existing items with the same codes are overwritten), "
              "add --force at the end of the command.")
        return 1
    files = FileRepository(src)
    cloud = PgRepository(db)
    n = {"companies": 0, "funds": 0, "bonds": 0, "history": 0, "users": 0, "deleted": 0}

    # companies and funds (inserted directly so existing history is not duplicated)
    with db.transaction() as c:
        for co in files.list_companies(include_archived=True):
            c.execute("INSERT INTO companies (code, data) VALUES (%s, %s) ON CONFLICT (code) DO UPDATE SET data = EXCLUDED.data",
                      (co.code, Jsonb(co.model_dump(mode="json"))))
            n["companies"] += 1
        for f in files.list_funds(include_archived=True):
            c.execute("INSERT INTO funds (code, company, data) VALUES (%s, %s, %s) ON CONFLICT (code) "
                      "DO UPDATE SET company = EXCLUDED.company, data = EXCLUDED.data",
                      (f.code, f.company, Jsonb(f.model_dump(mode="json"))))
            n["funds"] += 1

    # bonds (keeping version numbers, so the next save continues the numbering)
    for f in files.list_funds(include_archived=True):
        for rec in files.list_bonds(f.code):
            cloud.import_bond(f.code, rec)
            n["bonds"] += 1

    # history copies: history/<FUND>/<ISIN or _fund>/<stamp>__<user>__v<version>.json, history/_companies/<CODE>/...
    hist_root = src / "history"
    if hist_root.is_dir():
        for scope_dir in sorted(p for p in hist_root.iterdir() if p.is_dir() and p.name != "_users"):
            for key_dir in sorted(p for p in scope_dir.iterdir() if p.is_dir()):
                for h in sorted(key_dir.glob("*.json")):
                    try:
                        stamp, user, ver = h.stem.split("__")
                        when, version = _stamp(stamp), int(ver[1:])
                        data = json.loads(h.read_text(encoding="utf-8"))
                    except (ValueError, json.JSONDecodeError):
                        print(f"  skipped unreadable history file {h}")
                        continue
                    if scope_dir.name == "_companies":
                        kind, scope, key = "company", "_companies", key_dir.name
                    elif key_dir.name == "_fund":
                        kind, scope, key = "fund", scope_dir.name, "_fund"
                    else:
                        kind, scope, key = "bond", scope_dir.name, key_dir.name
                    cloud.import_history(kind, scope, key, version, user, when, data)
                    n["history"] += 1

    # users (password hashes are copied as they are, so everyone keeps their password)
    users_file = src / "users.json"
    if users_file.is_file():
        users = {u["username"]: User.model_validate(u) for u in json.loads(users_file.read_text(encoding="utf-8"))}
        PgUserStore(db)._save(users, "import")
        n["users"] = len(users)

    # deleted bonds: deleted/<FUND>/<ISIN>__<stamp>__<user>.json
    deleted_root = src / "deleted"
    if deleted_root.is_dir():
        for fund_dir in sorted(p for p in deleted_root.iterdir() if p.is_dir()):
            for dfile in sorted(fund_dir.glob("*.json")):
                try:
                    isin, stamp, user = dfile.stem.split("__")
                    cloud.import_deleted(fund_dir.name, isin, user, _stamp(stamp),
                                         json.loads(dfile.read_text(encoding="utf-8")))
                    n["deleted"] += 1
                except (ValueError, json.JSONDecodeError):
                    print(f"  skipped unreadable deleted file {dfile}")

    # verify: every bond in the cloud equals the one on the PC
    problems = 0
    for f in files.list_funds(include_archived=True):
        for rec in files.list_bonds(f.code):
            if cloud.get_bond(f.code, rec.isin) != rec:
                print(f"  MISMATCH {f.code} {rec.isin}")
                problems += 1
    print("Copied:", ", ".join(f"{v} {k}" for k, v in n.items()))
    print("Check: every bond matches the PC copy." if not problems else f"Check: {problems} bond(s) differ!")
    return 0 if not problems else 1


COMMANDS = {"init-db": init_db, "create-admin": create_admin, "reset-password": reset_password,
            "list-users": list_users, "check": check}

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "import-data":
        sys.exit(import_data(args[1], force="--force" in args[2:]))
    if len(args) != 1 or args[0] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    sys.exit(COMMANDS[args[0]]())
