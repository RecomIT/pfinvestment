"""Local date and time for 'today', file names and 'printed on' stamps.

On the office PC this is the PC's own clock. A cloud server runs on UTC, so set
BOND_APP_UTC_OFFSET_HOURS=6 there to get Bangladesh time (no daylight saving).
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone


def _offset() -> timezone | None:
    v = os.environ.get("BOND_APP_UTC_OFFSET_HOURS")
    if not v:
        return None
    return timezone(timedelta(hours=float(v)))


def now_local() -> datetime:
    """Naive local datetime (the configured offset, else the machine's local time)."""
    tz = _offset()
    return datetime.now(tz).replace(tzinfo=None) if tz else datetime.now()


def today_local() -> date:
    return now_local().date()
