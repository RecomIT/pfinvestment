"""Date helpers: month arithmetic and coupon date generation."""
from __future__ import annotations

import calendar
from datetime import date


def add_months(d: date, months: int, anchor_day: int | None = None) -> date:
    """Shift by whole months. Day = anchor_day (default d.day), clamped to month end."""
    day = anchor_day if anchor_day is not None else d.day
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def generate_coupon_dates(issue: date, maturity: date, frequency: int) -> list[date]:
    """Coupon dates after issue up to and including maturity.

    Worked back from maturity every 12/frequency months on maturity's day of month,
    clamped to month end (e.g. 31 Aug -> 28/29 Feb). Weekends/holidays are not adjusted.
    """
    step = 12 // frequency
    out: list[date] = []
    k = 0
    while True:
        d = add_months(maturity, -step * k, anchor_day=maturity.day)
        if d <= issue:
            break
        out.append(d)
        k += 1
    out.reverse()
    return out


def previous_coupon_date(issue: date, coupon_dates: list[date], on_or_before: date) -> date:
    """Last coupon date <= the given date, or the issue date if none."""
    prev = issue
    for cd in coupon_dates:
        if cd <= on_or_before:
            prev = cd
        else:
            break
    return prev
