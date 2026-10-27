"""Calculation 2: figures for reporting periods ending on chosen cut-off dates.

Everything is derived from one function, `state_at(d)`: the cumulative position of the
holding at the end of day d. A reporting period (a, b] is simply state_at(b) - state_at(a),
so periods always add up to the schedule totals and openings always equal prior closings.

Splitting a coupon period at a cut-off date (fraction f of the period earned):
  * first (stub) period, purchase -> first coupon: f = (d - purchase) / (first coupon - purchase), any basis
  * Excel mode:  f = min((d - last coupon) / 182, 1)
  * Actual mode: f = (d - last coupon) / (next coupon - last coupon)
The slice that ends on the coupon date takes the remainder, so each period sums to 100%.
A coupon (and maturity redemption) falling ON date d counts as received by the end of d.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, timedelta
from typing import Optional

from .models import DayBasis
from .schedule import Schedule, ScheduleRow, excel_period_days


@dataclass(frozen=True)
class State:
    """Cumulative position from purchase up to and including day d."""

    invested: float = 0.0               # clean price, once purchased
    interest_income: float = 0.0        # effective interest recognised (incl. sale adjustment)
    amortization: float = 0.0
    coupon_gross: float = 0.0           # coupons received (gross)
    purchased_accrued_recovered: float = 0.0
    encashed_principal: float = 0.0     # face at maturity, or carrying value at sale
    sale_accrued_received: float = 0.0
    sale_interest_adjustment: float = 0.0
    gain_loss: float = 0.0
    carrying_value: float = 0.0         # closing carrying value at d
    interest_receivable: float = 0.0    # closing interest receivable at d


def _fraction(sched: Schedule, row: ScheduleRow, d: date) -> float:
    if d <= row.start:
        return 0.0
    if d >= row.end:
        return 1.0
    elapsed = (d - row.start).days
    if row.index == 0 or sched.bond.day_basis == DayBasis.ACTUAL:
        return elapsed / row.days
    return min(elapsed / excel_period_days(sched.bond.frequency), 1.0)


def _accrual(sched: Schedule, d: date) -> tuple[float, float, float, float]:
    """(interest, amortization, coupon_gross, purchased_accrued_recovered) earned/received up to d."""
    i = a = c = r = 0.0
    for row in sched.rows:
        if d >= row.end:
            i += row.interest
            a += row.amortization
            c += row.coupon_gross
            r += row.purchased_accrued_recovered
        elif d > row.start:
            f = _fraction(sched, row, d)
            i += f * row.interest
            a += f * row.amortization
            break
        else:
            break
    return i, a, c, r


def receivable_before_sale(sched: Schedule) -> float:
    """Interest receivable the fund holds at the end of the sale date, before the sale is recorded."""
    sale = sched.bond.sale
    if sale is None:
        raise ValueError("bond has no sale")
    i, a, c, r = _accrual(sched, sale.sale_date)
    return i - a - (c - r)


def state_at(sched: Schedule, d: date) -> State:
    bond = sched.bond
    if d < bond.purchase_date:
        return State()

    sale = bond.sale
    if sale is not None and d >= sale.sale_date:
        i, a, c, r = _accrual(sched, sale.sale_date)
        carrying_at_sale = bond.clean_price + a
        receivable_at_sale = i - a - (c - r)
        adj = sale.accrued_interest_received - receivable_at_sale
        return State(
            invested=bond.clean_price,
            interest_income=i + adj,
            amortization=a,
            coupon_gross=c,
            purchased_accrued_recovered=r,
            encashed_principal=carrying_at_sale,
            sale_accrued_received=sale.accrued_interest_received,
            sale_interest_adjustment=adj,
            gain_loss=sale.clean_proceeds - carrying_at_sale,
            carrying_value=0.0,
            interest_receivable=0.0,
        )

    i, a, c, r = _accrual(sched, d)
    matured = d >= bond.maturity_date
    carrying = bond.clean_price + a
    return State(
        invested=bond.clean_price,
        interest_income=i,
        amortization=a,
        coupon_gross=c,
        purchased_accrued_recovered=r,
        encashed_principal=carrying if matured else 0.0,
        carrying_value=0.0 if matured else carrying,
        interest_receivable=i - a - (c - r),
    )


@dataclass(frozen=True)
class PeriodRow:
    period_start: date            # exclusive: figures cover the day after this ... period_end
    period_end: date              # inclusive cut-off date
    opening_carrying: float
    investment: float
    amortization: float
    encashed_principal: float
    closing_carrying: float
    opening_receivable: float
    interest_income: float
    coupon_gross: float
    purchased_accrued_recovered: float
    sale_accrued_received: float
    sale_interest_adjustment: float
    gain_loss: float
    closing_receivable: float
    carrying_check: float         # opening + investment + amortization - encashed - closing (should be 0)
    receivable_check: float       # opening + income - amortization - (coupon - recovered) - sale accrued - closing


def compute_periods(sched: Schedule, cutoffs: list[date], start: Optional[date] = None) -> list[PeriodRow]:
    """Figures for each period between consecutive dates in [start] + cutoffs.

    `start` is the end of the previous reporting period (exclusive). If omitted, the first
    period starts from the purchase date (inclusive).
    """
    if not cutoffs:
        return []
    ends = sorted(set(cutoffs))
    if start is None:
        start = sched.bond.purchase_date - timedelta(days=1)
    if start >= ends[0]:
        raise ValueError("start must be before the first cut-off date")

    out: list[PeriodRow] = []
    a_date, a = start, state_at(sched, start)
    for b_date in ends:
        b = state_at(sched, b_date)
        d = {f.name: getattr(b, f.name) - getattr(a, f.name) for f in fields(State)}
        coupon_net = d["coupon_gross"] - d["purchased_accrued_recovered"]
        out.append(
            PeriodRow(
                period_start=a_date,
                period_end=b_date,
                opening_carrying=a.carrying_value,
                investment=d["invested"],
                amortization=d["amortization"],
                encashed_principal=d["encashed_principal"],
                closing_carrying=b.carrying_value,
                opening_receivable=a.interest_receivable,
                interest_income=d["interest_income"],
                coupon_gross=d["coupon_gross"],
                purchased_accrued_recovered=d["purchased_accrued_recovered"],
                sale_accrued_received=d["sale_accrued_received"],
                sale_interest_adjustment=d["sale_interest_adjustment"],
                gain_loss=d["gain_loss"],
                closing_receivable=b.interest_receivable,
                carrying_check=a.carrying_value + d["invested"] + d["amortization"]
                - d["encashed_principal"] - b.carrying_value,
                receivable_check=a.interest_receivable + d["interest_income"] - d["amortization"]
                - coupon_net - d["sale_accrued_received"] - b.interest_receivable,
            )
        )
        a_date, a = b_date, b
    return out


def report_cutoffs(bond, year_end_month: int, year_end_day: int, quarterly: bool) -> list[date]:
    """Fund year-ends (or quarter-ends) from purchase up to and including the first one on/after
    the bond closes (sale date or maturity). Used by the screens and the exports."""
    close = bond.sale.sale_date if bond.sale else bond.maturity_date
    ds = fiscal_cutoffs(bond.purchase_date, close + timedelta(days=400), year_end_month, year_end_day, quarterly)
    return [c for i, c in enumerate(ds) if i == 0 or ds[i - 1] < close]


def fiscal_cutoffs(first: date, last: date, year_end_month: int = 6, year_end_day: int = 30,
                   quarterly: bool = False) -> list[date]:
    """Year-end (and optionally quarter-end) dates falling in [first, last]."""
    import calendar

    from .dates import add_months

    step = 3 if quarterly else 12
    anchor = date(first.year - 1, year_end_month, year_end_day)
    # A month-end year-end (30 Jun) gives month-end quarters (30 Sep, 31 Dec, 31 Mar).
    month_end = year_end_day == calendar.monthrange(anchor.year, year_end_month)[1]
    day = 31 if month_end else year_end_day
    out: list[date] = []
    k = 0
    while True:
        d = add_months(anchor, step * k, anchor_day=day)
        if d > last:
            break
        if d >= first:
            out.append(d)
        k += 1
    return out
