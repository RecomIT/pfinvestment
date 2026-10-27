"""Text formatting for messages: lakh/crore grouping and 27-Mar-24 dates (agreed house style)."""
from __future__ import annotations

from datetime import date


def lakh(x: float, dp: int = 2) -> str:
    """230901871.92 -> '23,09,01,871.92'; negatives in brackets like the sheets: (36,276.26)."""
    neg = x < 0
    s = f"{abs(x):.{dp}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    out = whole + ("." + frac if frac else "")
    return f"({out})" if neg else out


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def dmy(d: date) -> str:
    """date(2024, 3, 27) -> '27-Mar-24' (fixed English month names, whatever the PC's language)."""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year % 100:02d}"
