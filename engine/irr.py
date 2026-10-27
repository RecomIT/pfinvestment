"""XIRR with the same definition as Excel:  sum( CF_i / (1+r)^((d_i - d_0)/365) ) = 0."""
from __future__ import annotations

from datetime import date


class IRRError(ValueError):
    pass


def _npv(rate: float, flows: list[tuple[date, float]]) -> float:
    d0 = flows[0][0]
    return sum(cf / (1.0 + rate) ** ((d - d0).days / 365.0) for d, cf in flows)


def _dnpv(rate: float, flows: list[tuple[date, float]]) -> float:
    d0 = flows[0][0]
    s = 0.0
    for d, cf in flows:
        t = (d - d0).days / 365.0
        s -= t * cf / (1.0 + rate) ** (t + 1.0)
    return s


def xirr(flows: list[tuple[date, float]], guess: float = 0.1) -> float:
    """Solve for the annual rate. Newton's method, falling back to bisection."""
    if len(flows) < 2:
        raise IRRError("need at least two cash flows")
    flows = sorted(flows, key=lambda x: x[0])
    if not (any(cf > 0 for _, cf in flows) and any(cf < 0 for _, cf in flows)):
        raise IRRError("cash flows need at least one positive and one negative value")

    r = guess
    for _ in range(100):
        f = _npv(r, flows)
        df = _dnpv(r, flows)
        if df == 0:
            break
        nr = r - f / df
        if nr <= -0.999999:
            break
        if abs(nr - r) < 1e-14:
            return nr
        r = nr

    # Bisection fallback on (-0.99, 10)
    lo, hi = -0.99, 10.0
    flo, fhi = _npv(lo, flows), _npv(hi, flows)
    if flo * fhi > 0:
        raise IRRError("IRR could not be found")
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = _npv(mid, flows)
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
        if hi - lo < 1e-15:
            break
    return (lo + hi) / 2
