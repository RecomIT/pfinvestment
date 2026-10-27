"""JSON file storage with history copies, locking and version checks.

Layout under the data folder:
    companies/<COMPANY>.json
    funds/<FUND>/fund.json            (fund.company = the company code)
    funds/<FUND>/bonds/<ISIN>.json
    history/<FUND>/<ISIN or _fund>/<timestamp>__<user>__v<version>.json
    deleted/<FUND>/<ISIN>__<timestamp>.json

Every write takes a lock on the fund, writes to a temporary file and renames it into
place, so a crash or two people saving at once can never leave a half-written file.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from filelock import FileLock, Timeout

from engine.clock import now_local

from engine.safety import is_safe_code, is_safe_isin

from .records import BondRecord, Company, Fund

LOCK_TIMEOUT_SECONDS = 10
_HISTORY_FILE_RE = re.compile(r"^\d{8}T\d{12}__[A-Za-z0-9._-]{1,40}__v\d{1,9}\.json$")


class StorageError(Exception):
    pass


class NotFound(StorageError):
    pass


class AlreadyExists(StorageError):
    pass


class VersionConflict(StorageError):
    """Someone else saved this record after you opened it."""


class Busy(StorageError):
    """Could not get the lock in time (another save is in progress)."""


def _now() -> datetime:
    return now_local()


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S%f")


def _safe_user(user: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in user)[:40] or "unknown"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # On Windows the rename fails while another process is reading the target file;
        # reads are brief, so retry for up to ~2 seconds.
        for attempt in range(40):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


class FileRepository:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        (self.root / "funds").mkdir(parents=True, exist_ok=True)
        (self.root / "companies").mkdir(parents=True, exist_ok=True)
        (self.root / "history").mkdir(parents=True, exist_ok=True)
        (self.root / "deleted").mkdir(parents=True, exist_ok=True)
        (self.root / "locks").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ paths & locks
    # Every code / ISIN that reaches a file path is checked first, so a value like "..\\x" taken
    # from a web address can never point outside the data folder.
    @staticmethod
    def _code(code: str, what: str = "fund") -> str:
        c = (code or "").strip().upper()
        if not is_safe_code(c):
            raise NotFound(f"{what} {code!r} not found")
        return c

    @staticmethod
    def _isin(isin: str) -> str:
        i = (isin or "").strip().upper()
        if not is_safe_isin(i):
            raise NotFound(f"bond {isin!r} not found")
        return i

    def _fund_dir(self, code: str) -> Path:
        return self.root / "funds" / self._code(code)

    def _bond_path(self, code: str, isin: str) -> Path:
        return self._fund_dir(code) / "bonds" / f"{self._isin(isin)}.json"

    def _lock(self, code: str) -> FileLock:
        return FileLock(str(self.root / "locks" / f"{code}.lock"), timeout=LOCK_TIMEOUT_SECONDS)

    def _history(self, code: str, key: str, text: str, user: str, version: int, when: datetime) -> None:
        p = self.root / "history" / code / key / f"{_stamp(when)}__{_safe_user(user)}__v{version}.json"
        _atomic_write(p, text)

    # ------------------------------------------------------------ companies
    def list_companies(self, include_archived: bool = False) -> list[Company]:
        out = []
        for f in sorted((self.root / "companies").glob("*.json")):
            c = Company.model_validate_json(f.read_text(encoding="utf-8"))
            if include_archived or not c.archived:
                out.append(c)
        return out

    def get_company(self, code: str) -> Company:
        code = self._code(code, "company")
        f = self.root / "companies" / f"{code}.json"
        if not f.is_file():
            raise NotFound(f"company {code} not found")
        return Company.model_validate_json(f.read_text(encoding="utf-8"))

    def save_company(self, company: Company, user: str, create: bool = False) -> Company:
        try:
            with FileLock(str(self.root / "locks" / "_companies.lock"), timeout=LOCK_TIMEOUT_SECONDS):
                path = self.root / "companies" / f"{company.code}.json"
                if create and path.exists():
                    raise AlreadyExists(f"company {company.code} already exists")
                if not create and not path.exists():
                    raise NotFound(f"company {company.code} not found")
                text = company.model_dump_json(indent=2)
                _atomic_write(path, text)
                self._history("_companies", company.code, text, user, 0, _now())
                return company
        except Timeout as e:
            raise Busy("another save is in progress, please try again") from e

    # ------------------------------------------------------------ funds
    def list_funds(self, include_archived: bool = False, company: Optional[str] = None) -> list[Fund]:
        """All funds, or only those of one company (company="" for funds without a company)."""
        out = []
        for d in sorted((self.root / "funds").iterdir()):
            f = d / "fund.json"
            if f.is_file():
                fund = Fund.model_validate_json(f.read_text(encoding="utf-8"))
                if company is not None and (fund.company or "") != company.strip().upper():
                    continue
                if include_archived or not fund.archived:
                    out.append(fund)
        return out

    def get_fund(self, code: str) -> Fund:
        code = self._code(code)
        f = self._fund_dir(code) / "fund.json"
        if not f.is_file():
            raise NotFound(f"fund {code} not found")
        return Fund.model_validate_json(f.read_text(encoding="utf-8"))

    def save_fund(self, fund: Fund, user: str, create: bool = False) -> Fund:
        if fund.company is not None:
            self.get_company(fund.company)            # the company must exist
        try:
            with self._lock(fund.code):
                path = self._fund_dir(fund.code) / "fund.json"
                if create and path.exists():
                    raise AlreadyExists(f"fund {fund.code} already exists")
                if not create and not path.exists():
                    raise NotFound(f"fund {fund.code} not found")
                text = fund.model_dump_json(indent=2)
                _atomic_write(path, text)
                (self._fund_dir(fund.code) / "bonds").mkdir(exist_ok=True)
                self._history(fund.code, "_fund", text, user, 0, _now())
                return fund
        except Timeout as e:
            raise Busy("another save is in progress, please try again") from e

    # ------------------------------------------------------------ bonds
    def list_bonds(self, code: str) -> list[BondRecord]:
        self.get_fund(code)
        d = self._fund_dir(code) / "bonds"
        return [
            BondRecord.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(d.glob("*.json"))
        ]

    def get_bond(self, code: str, isin: str) -> BondRecord:
        p = self._bond_path(code, isin)
        if not p.is_file():
            raise NotFound(f"bond {isin} not found in fund {code}")
        return BondRecord.model_validate_json(p.read_text(encoding="utf-8"))

    def save_bond(self, code: str, record: BondRecord, user: str,
                  expected_version: Optional[int] = None) -> BondRecord:
        """Create (expected_version=None, must not exist) or update (expected_version = version you loaded)."""
        code = self._code(code)
        self.get_fund(code)
        try:
            with self._lock(code):
                path = self._bond_path(code, record.isin)
                if expected_version is None:
                    if path.exists():
                        raise AlreadyExists(f"bond {record.isin} already exists in fund {code}")
                    new_version = 1
                else:
                    if not path.exists():
                        raise NotFound(f"bond {record.isin} not found in fund {code}")
                    current = BondRecord.model_validate_json(path.read_text(encoding="utf-8")).version
                    if current != expected_version:
                        raise VersionConflict(
                            f"bond {record.isin} was changed by someone else (version {current}, "
                            f"you had {expected_version}); reload and try again"
                        )
                    new_version = current + 1
                when = _now()
                saved = record.model_copy(update={
                    "version": new_version, "updated_by": user, "updated_at": when.isoformat(timespec="seconds"),
                })
                text = saved.model_dump_json(indent=2)
                _atomic_write(path, text)
                self._history(code, record.isin, text, user, new_version, when)
                return saved
        except Timeout as e:
            raise Busy("another save is in progress, please try again") from e

    def delete_bond(self, code: str, isin: str, user: str, expected_version: int) -> None:
        """Moves the bond file to deleted/ (recoverable); history is kept."""
        code, isin = self._code(code), self._isin(isin)
        try:
            with self._lock(code):
                path = self._bond_path(code, isin)
                if not path.exists():
                    raise NotFound(f"bond {isin} not found in fund {code}")
                current = BondRecord.model_validate_json(path.read_text(encoding="utf-8")).version
                if current != expected_version:
                    raise VersionConflict(f"bond {isin} was changed by someone else; reload and try again")
                when = _now()
                dest = self.root / "deleted" / code / f"{isin}__{_stamp(when)}__{_safe_user(user)}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(dest))
        except Timeout as e:
            raise Busy("another save is in progress, please try again") from e

    def bond_history(self, code: str, isin: str) -> list[dict]:
        d = self.root / "history" / self._code(code) / self._isin(isin)
        out = []
        for p in sorted(d.glob("*.json")):
            stamp, user, ver = p.stem.split("__")
            out.append({"file": p.name, "timestamp": datetime.strptime(stamp, "%Y%m%dT%H%M%S%f").isoformat(),
                        "user": user, "version": int(ver[1:])})
        return out

    def read_history_version(self, code: str, isin: str, file_name: str) -> BondRecord:
        if not _HISTORY_FILE_RE.match(file_name or ""):
            raise NotFound("history entry not found")
        p = self.root / "history" / self._code(code) / self._isin(isin) / file_name
        if not p.is_file():
            raise NotFound("history entry not found")
        return BondRecord.model_validate_json(p.read_text(encoding="utf-8"))
