"""What is stored on disk: companies, funds and bond holdings (inputs only; results are always recalculated)."""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.models import BondInput
from engine.safety import MAX_COUPON_DATES, MAX_TEXT, check_extra, is_safe_code

def _clean_code(v: str, what: str) -> str:
    v = v.strip().upper()
    if not is_safe_code(v):
        raise ValueError(f"{what} code: 1-40 letters A-Z, digits, '-' or '_', starting with a letter or digit "
                         f"(Windows device names such as CON or NUL are not allowed)")
    return v


class Company(BaseModel):
    """A client company. A company has one or more funds (e.g. Provident Fund, Gratuity Fund)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Short code, e.g. ABC")
    name: str = Field(min_length=1, max_length=MAX_TEXT)
    archived: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_ok(cls, v: dict) -> dict:
        return check_extra(v)

    @field_validator("code")
    @classmethod
    def _code_ok(cls, v: str) -> str:
        return _clean_code(v, "Company")


class Fund(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Short folder-safe code, unique across all companies, e.g. ABC-PF")
    company: Optional[str] = Field(default=None, description="Code of the company this fund belongs to")
    name: str = Field(min_length=1, max_length=MAX_TEXT)
    year_end_month: int = Field(default=6, ge=1, le=12)
    year_end_day: int = Field(default=30, ge=1, le=31)
    tds_rate: float = Field(default=0.0, ge=0, lt=1, description="TDS on coupons as a decimal, e.g. 0.05")
    archived: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("extra")
    @classmethod
    def _extra_ok(cls, v: dict) -> dict:
        return check_extra(v)

    @field_validator("code")
    @classmethod
    def _code_ok(cls, v: str) -> str:
        return _clean_code(v, "Fund")

    @field_validator("company")
    @classmethod
    def _company_ok(cls, v: Optional[str]) -> Optional[str]:
        return _clean_code(v, "Company") if v not in (None, "") else None

    @field_validator("year_end_day")
    @classmethod
    def _day_ok(cls, v: int, info) -> int:
        import calendar

        m = info.data.get("year_end_month", 6)
        if v > calendar.monthrange(2001, m)[1]:      # non-leap year: 29 Feb is not a valid year-end
            raise ValueError("The year-end day does not exist in that month")
        return v


class CouponActual(BaseModel):
    """Actual TDS deducted on one coupon, overriding the fund's TDS rate."""

    model_config = ConfigDict(extra="forbid")

    coupon_date: date
    tds: float = Field(ge=0)
    note: str = Field(default="", max_length=MAX_TEXT)


class BondRecord(BaseModel):
    """One holding of one ISIN within a fund."""

    model_config = ConfigDict(extra="forbid")

    bond: BondInput
    coupon_actuals: list[CouponActual] = Field(default_factory=list, max_length=MAX_COUPON_DATES)
    version: int = Field(default=0, ge=0, description="Incremented on every save; used to stop overwriting")
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("coupon_actuals")
    @classmethod
    def _unique_dates(cls, v: list[CouponActual]) -> list[CouponActual]:
        seen = set()
        for c in v:
            if c.coupon_date in seen:
                raise ValueError(f"TDS is entered twice for the coupon of {c.coupon_date}")
            seen.add(c.coupon_date)
        return sorted(v, key=lambda c: c.coupon_date)

    @property
    def isin(self) -> str:
        return self.bond.isin
