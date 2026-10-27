"""Input safety rules shared by the models, storage and API.

Codes and ISINs end up in file names (local version) and URLs, so they are restricted to plain
ASCII letters, digits, '-' and '_', and Windows device names are refused. Size limits stop a single
request from consuming unbounded CPU or memory.
"""
from __future__ import annotations

import json
import re

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,39}$")        # fund / company codes (upper-cased first)
ISIN_RE = re.compile(r"^[A-Z0-9]{1,20}$")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

MAX_BOND_YEARS = 50            # issue -> maturity; T-bonds run up to 20 years
MIN_YEAR = 1950
MAX_COUPON_DATES = 2 * MAX_BOND_YEARS + 2
MAX_CUTOFF_DATES = 400         # custom cut-off dates in one request
MAX_EXTRA_BYTES = 10_000       # free-form `extra` fields per record
MAX_TEXT = 200                 # names, notes
MAX_BODY_BYTES = 256_000       # whole request body


def is_safe_code(v: str) -> bool:
    return bool(CODE_RE.match(v)) and v not in WINDOWS_RESERVED


def is_safe_isin(v: str) -> bool:
    return bool(ISIN_RE.match(v)) and v not in WINDOWS_RESERVED


def check_extra(v: dict) -> dict:
    if len(json.dumps(v, default=str)) > MAX_EXTRA_BYTES:
        raise ValueError(f"Additional fields are too large (limit {MAX_EXTRA_BYTES:,} characters)")
    return v
