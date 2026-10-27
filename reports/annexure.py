"""Annexure-D (schedule of bond investment) for a fund, for one reporting period.

Columns follow the client's Annexure-D, plus Gain/(Loss) on sale:
  K opening investment  L investment during period (clean price)  M amortization
  N encashed (carrying value at sale / face at maturity)  O closing balance
  P opening interest receivable  Q interest income  R holding period interest
  (part of the first coupon that repays accrued interest bought)  S gross interest receipt
  T interest receipt (net of TDS)  U TDS  V encashed interest (accrued interest received on sale)
  W closing interest receivable = P + Q - M - S + R - V   X gain/(loss) on sale
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from engine import build_schedule, compute_periods
from engine.fmt import dmy
from engine.schedule import Schedule
from storage.records import BondRecord, Fund

TOL = 0.01  # taka; tolerance for the internal arithmetic checks


@dataclass
class AnnexureRow:
    serial: int
    isin: str
    name: str
    issue_date: date
    purchase_date: date
    maturity_date: date
    nature: str
    coupon_rate: float
    irr: float
    day_basis: str
    face_value: float
    settlement_amount: float
    opening_investment: float           # K
    investment: float                   # L
    amortization: float                 # M
    encashed: float                     # N
    closing_balance: float              # O
    opening_interest: float             # P
    interest_income: float              # Q
    holding_period_interest: float      # R
    gross_interest_receipt: float       # S
    interest_receipt: float             # T
    tds: float                          # U
    encashed_interest: float            # V
    closing_interest_receivable: float  # W
    gain_loss: float                    # X
    warnings: list[str] = field(default_factory=list)


NUMERIC = ["face_value", "settlement_amount", "opening_investment", "investment", "amortization", "encashed",
           "closing_balance", "opening_interest", "interest_income", "holding_period_interest",
           "gross_interest_receipt", "interest_receipt", "tds", "encashed_interest",
           "closing_interest_receivable", "gain_loss"]


@dataclass
class Annexure:
    fund: Fund
    period_start: date      # exclusive (previous period end)
    period_end: date        # inclusive
    rows: list[AnnexureRow]
    totals: dict[str, float]
    warnings: list[str]
    company_name: str = ""   # shown above the fund name on every report heading


def nature(clean_price: float, face_value: float) -> str:
    if clean_price > face_value:
        return "Premium"
    if clean_price == face_value:
        return "At Par"
    return "Discount"


def _close_date(rec: BondRecord) -> date:
    return rec.bond.sale.sale_date if rec.bond.sale else rec.bond.maturity_date


def coupon_tds(rec: BondRecord, sched: Schedule, fund: Fund, start: date, end: date) -> tuple[float, list[str]]:
    """TDS on coupons received in (start, end]: actual entered figure, else gross x fund rate."""
    actual = {c.coupon_date: c.tds for c in rec.coupon_actuals}
    last_day = min(end, _close_date(rec))
    total = 0.0
    for row in sched.rows:
        if start < row.end <= last_day:
            total += actual.get(row.end, row.coupon_gross * fund.tds_rate)
    warnings = []
    coupon_days = {r.end for r in sched.rows}
    for d in actual:
        if d not in coupon_days:
            warnings.append(f"TDS entered for {dmy(d)}, which is not a coupon date of this bond")
        elif rec.bond.sale and d > rec.bond.sale.sale_date:
            warnings.append(f"TDS entered for {dmy(d)}, after the bond was sold")
    return total, warnings


def build_annexure(fund: Fund, records: list[BondRecord], period_start: date, period_end: date,
                   company_name: str = "") -> Annexure:
    if period_start >= period_end:
        raise ValueError("period start must be before period end")

    rows: list[AnnexureRow] = []
    all_warnings: list[str] = []
    held = [r for r in records if r.bond.purchase_date <= period_end and _close_date(r) > period_start]
    held.sort(key=lambda r: (r.bond.purchase_date, r.isin))

    for n, rec in enumerate(held, start=1):
        b = rec.bond
        sched = build_schedule(b)
        (p,) = compute_periods(sched, [period_end], start=period_start)
        tds, tds_warn = coupon_tds(rec, sched, fund, period_start, period_end)
        row = AnnexureRow(
            serial=n, isin=b.isin, name=b.name, issue_date=b.issue_date, purchase_date=b.purchase_date,
            maturity_date=b.maturity_date, nature=nature(b.clean_price, b.face_value), coupon_rate=b.coupon_rate,
            irr=sched.irr, day_basis=b.day_basis.value, face_value=b.face_value,
            settlement_amount=b.settlement_amount,
            opening_investment=p.opening_carrying, investment=p.investment, amortization=p.amortization,
            encashed=p.encashed_principal, closing_balance=p.closing_carrying,
            opening_interest=p.opening_receivable, interest_income=p.interest_income,
            holding_period_interest=p.purchased_accrued_recovered, gross_interest_receipt=p.coupon_gross,
            interest_receipt=p.coupon_gross - tds, tds=tds, encashed_interest=p.sale_accrued_received,
            closing_interest_receivable=p.closing_receivable, gain_loss=p.gain_loss,
            warnings=list(sched.warnings) + tds_warn,
        )
        # internal arithmetic checks, same as the sheet's own formulas
        k = row
        if abs(k.opening_investment + k.investment + k.amortization - k.encashed - k.closing_balance) > TOL:
            row.warnings.append("Principal columns do not add up (K + L + M - N != O)")
        w = (k.opening_interest + k.interest_income - k.amortization - k.gross_interest_receipt
             + k.holding_period_interest - k.encashed_interest)
        if abs(w - k.closing_interest_receivable) > TOL:
            row.warnings.append("Interest columns do not add up (P + Q - M - S + R - V != W)")
        if abs(k.interest_receipt + k.tds - k.gross_interest_receipt) > TOL:
            row.warnings.append("T + U != S")
        rows.append(row)
        all_warnings += [f"{b.isin}: {w}" for w in row.warnings]

    totals = {c: sum(getattr(r, c) for r in rows) for c in NUMERIC}
    return Annexure(fund, period_start, period_end, rows, totals, all_warnings, company_name)


def default_period(fund: Fund, as_of: date, quarterly: bool = True) -> tuple[date, date]:
    """The reporting period ending on the latest fund quarter-end (or year-end) on/before as_of."""
    from engine.periods import fiscal_cutoffs
    from engine.dates import add_months

    ends = fiscal_cutoffs(date(as_of.year - 2, 1, 1), as_of, fund.year_end_month, fund.year_end_day, quarterly)
    end = ends[-1]
    prev: Optional[date] = ends[-2] if len(ends) > 1 else add_months(end, -3 if quarterly else -12)
    return prev, end
