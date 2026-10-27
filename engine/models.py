"""Input definition for one bond holding.

Every input field is declared once here. Validation, the API, storage and the
forms all read from this model, so adding a field means adding it here.
"""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, Optional

from .safety import MAX_BOND_YEARS, MAX_COUPON_DATES, MAX_TEXT, MIN_YEAR, check_extra, is_safe_isin
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DayBasis(str, Enum):
    """How a coupon period is split at a cut-off date."""

    EXCEL_182 = "excel_182"   # days elapsed / fixed 364/frequency (182 for semi-annual)
    ACTUAL = "actual"         # days elapsed / actual days in the coupon period


class Sale(BaseModel):
    """Full sale of the holding before maturity."""

    model_config = ConfigDict(extra="forbid")

    sale_date: date
    clean_proceeds: float = Field(gt=0, description="Sale price excluding accrued interest")
    accrued_interest_received: float = Field(ge=0, description="Accrued interest received from the buyer")


class BondInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    isin: str = Field(min_length=1, max_length=20)
    name: str = Field(default="", max_length=MAX_TEXT)
    face_value: float = Field(gt=0)
    clean_price: float = Field(gt=0, description="Purchase price excluding accrued interest")
    accrued_interest_paid: float = Field(ge=0, description="Accrued interest paid at purchase (from deal slip)")
    coupon_rate: float = Field(ge=0, lt=1, description="Annual coupon rate as a decimal, e.g. 0.0895")
    frequency: int = Field(default=2, description="Coupons per year")
    issue_date: date
    purchase_date: date
    maturity_date: date
    day_basis: DayBasis = DayBasis.EXCEL_182
    coupon_dates: Optional[list[date]] = Field(
        default=None, max_length=MAX_COUPON_DATES,
        description="Override of generated coupon dates (all dates after issue, ending at maturity)"
    )
    sale: Optional[Sale] = None
    extra: dict[str, Any] = Field(default_factory=dict, description="Additional custom fields")

    @field_validator("isin")
    @classmethod
    def _isin_clean(cls, v: str) -> str:
        v = v.strip().upper()
        if not is_safe_isin(v):
            raise ValueError("ISIN must contain only the letters A-Z and digits 0-9")
        return v

    @field_validator("extra")
    @classmethod
    def _extra_ok(cls, v: dict) -> dict:
        return check_extra(v)

    @field_validator("frequency")
    @classmethod
    def _freq_ok(cls, v: int) -> int:
        # Agreed scope: semi-annual coupons only until another frequency is needed.
        if v != 2:
            raise ValueError("only semi-annual coupons (frequency = 2) are supported")
        return v

    @model_validator(mode="after")
    def _dates_ok(self) -> "BondInput":
        if not self.issue_date < self.maturity_date:
            raise ValueError("The issue date must be before the maturity date")
        if self.issue_date.year < MIN_YEAR:
            raise ValueError(f"The issue date must be in {MIN_YEAR} or later")
        if (self.maturity_date - self.issue_date).days > MAX_BOND_YEARS * 366:
            raise ValueError(f"A bond can run for at most {MAX_BOND_YEARS} years from issue to maturity")
        if not self.issue_date <= self.purchase_date < self.maturity_date:
            raise ValueError("The purchase date must be on or after the issue date and before the maturity date")
        if self.coupon_dates is not None:
            cds = self.coupon_dates
            if not cds:
                raise ValueError("Your own coupon dates list is empty")
            if any(b <= a for a, b in zip(cds, cds[1:])):
                raise ValueError("Coupon dates must be in order, earliest first, without repeats")
            if cds[0] <= self.issue_date:
                raise ValueError("All coupon dates must be after the issue date")
            if cds[-1] != self.maturity_date:
                raise ValueError("The last coupon date must be the maturity date")
        if self.sale is not None:
            if not self.purchase_date < self.sale.sale_date < self.maturity_date:
                raise ValueError("The sale date must be after the purchase date and before the maturity date")
        return self

    @property
    def coupon_amount(self) -> float:
        return self.face_value * self.coupon_rate / self.frequency

    @property
    def discount_premium(self) -> float:
        """Positive = discount, negative = premium."""
        return self.face_value - self.clean_price

    @property
    def settlement_amount(self) -> float:
        return self.clean_price + self.accrued_interest_paid
