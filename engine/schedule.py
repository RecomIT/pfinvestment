"""Calculation 1: the amortization schedule (effective interest, actual days)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .dates import generate_coupon_dates, previous_coupon_date
from .fmt import dmy, lakh
from .irr import xirr
from .models import BondInput, DayBasis


@dataclass(frozen=True)
class ScheduleRow:
    index: int                      # 0 = first (stub) period from purchase to first coupon
    start: date                     # purchase date or previous coupon date
    end: date                       # coupon date
    days: int                       # actual days start -> end
    opening: float                  # beginning carrying value
    interest: float                 # effective interest for the period
    coupon_gross: float             # full coupon paid on `end`
    purchased_accrued_recovered: float  # part of the first coupon that repays accrued interest bought
    coupon_net: float               # coupon_gross - purchased_accrued_recovered (cash flow used in IRR)
    amortization: float             # interest - coupon_net
    cumulative_amortization: float
    closing: float                  # ending carrying value


@dataclass
class Schedule:
    bond: BondInput
    irr: float
    irr_source: str                         # "calculated" or "override"
    coupon_dates: list[date]                # all coupon dates after issue
    rows: list[ScheduleRow]
    cash_flows: list[tuple[date, float]]    # the flows used for XIRR
    final_residual: float                   # amortization absorbed in the last row to land on face value
    suggested_accrued_interest: float
    warnings: list[str] = field(default_factory=list)

    @property
    def total_amortization(self) -> float:
        return self.rows[-1].cumulative_amortization if self.rows else 0.0


def resolve_coupon_dates(bond: BondInput) -> list[date]:
    if bond.coupon_dates:
        return list(bond.coupon_dates)
    return generate_coupon_dates(bond.issue_date, bond.maturity_date, bond.frequency)


EXCEL_HALF_YEAR_DAYS = 182.0


def excel_period_days(frequency: int) -> float:
    """Fixed coupon-period length used in Excel mode: 182 days (semi-annual only)."""
    if frequency != 2:
        raise ValueError("Excel mode is defined for semi-annual coupons only")
    return EXCEL_HALF_YEAR_DAYS


def suggest_accrued_interest(bond: BondInput, coupon_dates: Optional[list[date]] = None) -> float:
    """Accrued interest the seller should have charged at purchase, per the bond's day basis."""
    cds = coupon_dates if coupon_dates is not None else resolve_coupon_dates(bond)
    prev = previous_coupon_date(bond.issue_date, cds, bond.purchase_date)
    nxt = next(cd for cd in cds if cd > bond.purchase_date)
    elapsed = (bond.purchase_date - prev).days
    if bond.day_basis == DayBasis.EXCEL_182:
        denom = excel_period_days(bond.frequency)
    else:
        denom = (nxt - prev).days
    return bond.coupon_amount * elapsed / denom


def build_schedule(bond: BondInput, irr_override: Optional[float] = None) -> Schedule:
    cds = resolve_coupon_dates(bond)
    future = [cd for cd in cds if cd > bond.purchase_date]
    if not future:
        raise ValueError("There are no coupon dates after the purchase date")

    coupon = bond.coupon_amount
    flows: list[tuple[date, float]] = [(bond.purchase_date, -bond.clean_price)]
    for i, cd in enumerate(future):
        flows.append((cd, coupon - bond.accrued_interest_paid if i == 0 else coupon))
    flows.append((bond.maturity_date, bond.face_value))

    if irr_override is not None:
        irr, src = irr_override, "override"
    else:
        irr, src = xirr(flows), "calculated"

    warnings: list[str] = []
    suggested = suggest_accrued_interest(bond, cds)
    if abs(suggested - bond.accrued_interest_paid) > 1.0:
        warnings.append(
            f"Accrued interest entered ({lakh(bond.accrued_interest_paid)}) differs from the "
            f"calculated figure ({lakh(suggested)}) by {lakh(bond.accrued_interest_paid - suggested)}"
        )
    if bond.accrued_interest_paid > coupon:
        warnings.append("Accrued interest paid is larger than one full coupon")

    rows: list[ScheduleRow] = []
    opening = bond.clean_price
    cum = 0.0
    prev = bond.purchase_date
    residual = 0.0
    last = len(future) - 1
    for i, cd in enumerate(future):
        days = (cd - prev).days
        recovered = bond.accrued_interest_paid if i == 0 else 0.0
        net = coupon - recovered
        interest = opening * ((1.0 + irr) ** (days / 365.0) - 1.0)
        amort = interest - net
        if i == last:
            forced = bond.face_value - opening
            residual = amort - forced
            amort = forced
            interest = net + forced
        cum += amort
        closing = opening + amort
        rows.append(
            ScheduleRow(i, prev, cd, days, opening, interest, coupon, recovered, net, amort, cum, closing)
        )
        opening, prev = closing, cd

    if abs(residual) > 1.0:
        warnings.append(f"Final-period adjustment of {lakh(residual, 4)} was needed to reach face value")

    sched = Schedule(bond, irr, src, cds, rows, flows, residual, suggested, warnings)

    if bond.sale is not None:
        from .periods import receivable_before_sale   # imported here: periods imports this module
        expected = receivable_before_sale(sched)
        diff = bond.sale.accrued_interest_received - expected
        if abs(diff) > 1.0:
            warnings.append(
                f"Accrued interest received on sale ({lakh(bond.sale.accrued_interest_received)}) differs from the "
                f"calculated interest receivable on {dmy(bond.sale.sale_date)} ({lakh(expected)}) by {lakh(diff)}; "
                f"the difference goes to interest income"
            )
    return sched
