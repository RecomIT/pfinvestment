"""FastAPI application: JSON API (Phase 3). HTML screens are added in Phase 4.

Note: no `from __future__ import annotations` here. FastAPI must see the real type objects
for dependencies defined inside create_app (Viewer, Editor, Admin).
"""
import os
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine import BondInput, build_schedule, compute_periods
from engine.irr import IRRError
from engine.clock import today_local
from engine.periods import fiscal_cutoffs
from reports import build_annexure, default_period
from storage import (AlreadyExists, BondRecord, Busy, Company, CouponActual, FileRepository, Fund, NotFound,
                     VersionConflict)

from engine.safety import MAX_BODY_BYTES, MAX_CUTOFF_DATES

from .auth import RANK, AuthError, LoginThrottle, Role, SessionManager, User, UserStore

COOKIE = "bond_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "bondapp"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ====================================================================== request bodies
class LoginBody(BaseModel):
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


class NewUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(max_length=64)
    full_name: str = Field(default="", max_length=100)
    role: Role
    password: str


class UpdateUserBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: Optional[str] = Field(default=None, max_length=100)
    role: Optional[Role] = None
    active: Optional[bool] = None
    unlock: bool = False


class ResetPasswordBody(BaseModel):
    new_password: str


class BondSaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bond: BondInput
    coupon_actuals: list[CouponActual] = Field(default_factory=list)


class BondUpdateBody(BondSaveBody):
    version: int


# ====================================================================== serialisers
def schedule_json(sched) -> dict:
    b = sched.bond
    return {
        "irr": sched.irr,
        "irr_source": sched.irr_source,
        "coupon_amount": b.coupon_amount,
        "discount_premium": b.discount_premium,
        "settlement_amount": b.settlement_amount,
        "suggested_accrued_interest": sched.suggested_accrued_interest,
        "final_residual": sched.final_residual,
        "total_amortization": sched.total_amortization,
        "coupon_dates": [d.isoformat() for d in sched.coupon_dates],
        "cash_flows": [{"date": d.isoformat(), "amount": a} for d, a in sched.cash_flows],
        "rows": [asdict(r) for r in sched.rows],
        "warnings": sched.warnings,
    }


def record_json(rec: BondRecord) -> dict:
    return rec.model_dump(mode="json")


def summary_json(rec: BondRecord, today: date) -> dict:
    b = rec.bond
    out = {"isin": b.isin, "name": b.name, "face_value": b.face_value, "coupon_rate": b.coupon_rate,
           "purchase_date": b.purchase_date.isoformat(), "maturity_date": b.maturity_date.isoformat(),
           "day_basis": b.day_basis.value, "sold": b.sale is not None, "version": rec.version,
           "updated_by": rec.updated_by, "updated_at": rec.updated_at}
    try:
        s = build_schedule(b)
        from engine.periods import state_at
        st = state_at(s, today)
        out.update(irr=s.irr, carrying_value_today=st.carrying_value,
                   status="sold" if b.sale and today >= b.sale.sale_date
                   else "matured" if today >= b.maturity_date else "held",
                   warnings=s.warnings)
    except (ValueError, IRRError) as e:
        out.update(irr=None, status="error", warnings=[str(e)])
    return out


# ====================================================================== app factory
def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


def create_app(data_dir: Optional[str | Path] = None, clock: Optional[Callable[[], float]] = None,
               today: Optional[Callable[[], date]] = None, docs: Optional[bool] = None,
               backends: Optional[tuple] = None) -> FastAPI:
    """Build the app.

    backends = (repository, user store, session manager, login throttle) lets the cloud version plug in
    database-backed parts; by default everything is stored in files under data_dir (office PC).
    """
    if backends is not None:
        repo, users, sessions, throttle = backends
    else:
        data_dir = Path(data_dir or os.environ.get("BOND_APP_DATA") or PROJECT_ROOT / "data")
        repo = FileRepository(data_dir)
        users = UserStore(data_dir, **({"clock": clock} if clock else {}))
        sessions = SessionManager(**({"clock": clock} if clock else {}))
        throttle = LoginThrottle(**({"clock": clock} if clock else {}))
    today_fn = today or today_local
    # Settings (environment variables):
    #   BOND_APP_DOCS=0           turn off the /docs API test page (default on for the office PC)
    #   BOND_APP_TRUST_PROXY=1    take the client address from X-Forwarded-For (only behind a trusted proxy)
    show_docs = _env_flag("BOND_APP_DOCS", True) if docs is None else docs
    trust_proxy = _env_flag("BOND_APP_TRUST_PROXY", False)

    app = FastAPI(title="Bond Amortization", version="1.1.0",
                  docs_url="/docs" if show_docs else None, redoc_url=None,
                  openapi_url="/openapi.json" if show_docs else None)
    app.state.repo, app.state.users, app.state.sessions, app.state.throttle = repo, users, sessions, throttle

    def client_ip(request: Request) -> str:
        if trust_proxy:
            # Vercel overwrites these with the real client address (they cannot be spoofed there)
            fwd = request.headers.get("x-vercel-forwarded-for", "") or request.headers.get("x-forwarded-for", "")
            if fwd:
                return fwd.split(",")[0].strip()[:64]
            if request.headers.get("x-real-ip"):
                return request.headers["x-real-ip"].strip()[:64]
        return request.client.host if request.client else "unknown"

    def is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        return trust_proxy and request.headers.get("x-forwarded-proto", "").lower() == "https"

    def set_session_cookie(response: Response, request: Request, token: str) -> None:
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/", secure=is_https(request))

    # ------------------------------------------------------------------ errors
    def _err(status: int, msg: str, extra: Any = None) -> JSONResponse:
        body = {"detail": msg}
        if extra is not None:
            body["errors"] = extra
        return JSONResponse(status_code=status, content=body)

    @app.exception_handler(NotFound)
    async def _nf(_, e):
        return _err(404, str(e))

    @app.exception_handler(AlreadyExists)
    async def _ae(_, e):
        return _err(409, str(e))

    @app.exception_handler(VersionConflict)
    async def _vc(_, e):
        return _err(409, str(e))

    @app.exception_handler(Busy)
    async def _busy(_, e):
        return _err(503, str(e))

    @app.exception_handler(AuthError)
    async def _auth(_, e):
        return _err(400, str(e))

    @app.exception_handler(IRRError)
    async def _irr(_, e):
        return _err(422, f"IRR could not be calculated: {e}")

    @app.exception_handler(ValidationError)
    async def _val(_, e: ValidationError):
        return _err(422, "Please correct the following:", [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()])

    @app.exception_handler(RequestValidationError)
    async def _rval(_, e: RequestValidationError):
        return _err(422, "Please correct the following:", [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()])

    # ------------------------------------------------------------------ CSRF guard
    def _same_origin(request: Request) -> bool:
        """True if the browser says the request came from a page of this same app.

        Browsers always send Origin (or at least Referer) on POST/PUT/PATCH/DELETE and a web page
        cannot forge it, so a match proves the request was not made by another website.
        """
        from urllib.parse import urlsplit

        src = request.headers.get("origin") or request.headers.get("referer")
        if not src or src == "null":
            return False
        return urlsplit(src).netloc.lower() == request.headers.get("host", "").lower()

    # ------------------------------------------------------------------ request size + security headers
    CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
           "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return _err(413, "request is too large")
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if not request.url.path.startswith("/docs"):          # /docs loads its own scripts from a CDN
            h.setdefault("Content-Security-Policy", CSP)
        if is_https(request):
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            h.setdefault("Cache-Control", "no-store")         # never cache account or bond data
        return resp

    @app.middleware("http")
    async def csrf_guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin != "null" and not _same_origin(request):
                return _err(403, "request from another website was blocked")
            if request.headers.get(CSRF_HEADER, "").lower() != CSRF_VALUE and not _same_origin(request):
                return _err(403, "request blocked: it must come from this app's own pages "
                                 "(or send the header X-Requested-With: bondapp)")
        return await call_next(request)

    # ------------------------------------------------------------------ auth dependencies
    def current_user(request: Request) -> User:
        uname = sessions.touch(request.cookies.get(COOKIE))
        if uname is None:
            raise HTTPException(401, "not logged in or session expired")
        u = users.get(uname)
        if u is None or not u.active:
            sessions.end(request.cookies.get(COOKIE))
            raise HTTPException(401, "account not available")
        return u

    def user_ready(u: Annotated[User, Depends(current_user)]) -> User:
        if u.must_change_password:
            raise HTTPException(403, "you must change your password first")
        return u

    def need(role: Role):
        def dep(u: Annotated[User, Depends(user_ready)]) -> User:
            if RANK[u.role] < RANK[role]:
                raise HTTPException(403, f"requires {role.value} role")
            return u
        return dep

    Viewer = Annotated[User, Depends(need(Role.VIEWER))]
    Editor = Annotated[User, Depends(need(Role.EDITOR))]
    Admin = Annotated[User, Depends(need(Role.ADMIN))]

    # ------------------------------------------------------------------ health & auth
    @app.get("/api/health")
    def health():
        return {"status": "ok", "admin_exists": users.count_admins() > 0}

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response):
        if not throttle.allow(client_ip(request)):
            raise HTTPException(429, "Too many login attempts from this network. Please wait 15 minutes.")
        u = users.authenticate(body.username, body.password)
        token = sessions.create(u.username)
        set_session_cookie(response, request, token)
        return u.public()

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        sessions.end(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(u: Annotated[User, Depends(current_user)]):
        return u.public()

    @app.post("/api/auth/change-password")
    def change_password(body: ChangePasswordBody, request: Request, u: Annotated[User, Depends(current_user)]):
        from .auth import verify_password
        if not throttle.allow(client_ip(request)):              # same brute-force limit as login
            raise HTTPException(429, "Too many attempts from this network. Please wait 15 minutes.")
        if not verify_password(body.current_password, u.password_hash):
            raise HTTPException(400, "current password is wrong")
        if body.new_password == body.current_password:
            raise HTTPException(400, "new password must be different")
        users.set_password(u.username, body.new_password, actor=u.username, must_change=False)
        sessions.end_user(u.username, keep=request.cookies.get(COOKIE))   # log out other devices
        return {"ok": True}

    # ------------------------------------------------------------------ users (admin)
    @app.get("/api/users")
    def list_users(_: Admin):
        return [u.public() for u in users.list()]

    @app.post("/api/users", status_code=201)
    def create_user(body: NewUserBody, a: Admin):
        return users.create(body.username, body.password, body.role, body.full_name, actor=a.username).public()

    @app.patch("/api/users/{username}")
    def update_user(username: str, body: UpdateUserBody, a: Admin):
        before = users.get(username)
        u = users.update(username, a.username, role=body.role, active=body.active, full_name=body.full_name,
                         unlock=body.unlock)
        # log the user out only when access really changed (not for a name edit)
        if body.active is False or (before is not None and u.role != before.role):
            sessions.end_user(username)
        return u.public()

    @app.post("/api/users/{username}/reset-password")
    def reset_password(username: str, body: ResetPasswordBody, a: Admin):
        users.set_password(username, body.new_password, actor=a.username, must_change=True)
        sessions.end_user(username)
        return {"ok": True}

    # ------------------------------------------------------------------ companies
    def company_name(fund: Fund) -> str:
        if not fund.company:
            return ""
        try:
            return repo.get_company(fund.company).name
        except NotFound:
            return fund.company

    def fund_json(fund: Fund) -> dict:
        return {**fund.model_dump(mode="json"), "company_name": company_name(fund)}

    def _require_company(fund: Fund) -> None:
        if not fund.company:
            raise HTTPException(422, "Choose the company this fund belongs to")
        repo.get_company(fund.company)

    @app.get("/api/companies")
    def list_companies(_: Viewer, include_archived: bool = False):
        funds = repo.list_funds(include_archived=True)
        out = []
        for c in repo.list_companies(include_archived):
            mine = [f for f in funds if f.company == c.code]
            out.append({**c.model_dump(mode="json"), "funds": len([f for f in mine if not f.archived]),
                        "archived_funds": len([f for f in mine if f.archived])})
        return out

    @app.post("/api/companies", status_code=201)
    def create_company(company: Company, a: Admin):
        return repo.save_company(company, user=a.username, create=True).model_dump(mode="json")

    @app.get("/api/companies/{code}")
    def get_company(code: str, _: Viewer):
        return repo.get_company(code).model_dump(mode="json")

    @app.put("/api/companies/{code}")
    def update_company(code: str, company: Company, a: Admin):
        if company.code != code.strip().upper():
            raise HTTPException(400, "company code in the body must match the URL (codes cannot be renamed)")
        repo.get_company(code)
        return repo.save_company(company, user=a.username).model_dump(mode="json")

    # ------------------------------------------------------------------ funds
    @app.get("/api/funds")
    def list_funds(_: Viewer, include_archived: bool = False,
                   company: Optional[str] = Query(None, description="company code; empty = funds without a company")):
        return [fund_json(f) for f in repo.list_funds(include_archived, company=company)]

    @app.post("/api/funds", status_code=201)
    def create_fund(fund: Fund, a: Admin):
        _require_company(fund)
        return fund_json(repo.save_fund(fund, user=a.username, create=True))

    @app.get("/api/funds/{code}")
    def get_fund(code: str, _: Viewer):
        return fund_json(repo.get_fund(code))

    @app.put("/api/funds/{code}")
    def update_fund(code: str, fund: Fund, a: Admin):
        if fund.code != code.strip().upper():
            raise HTTPException(400, "fund code in the body must match the URL (codes cannot be renamed)")
        repo.get_fund(code)
        _require_company(fund)
        return fund_json(repo.save_fund(fund, user=a.username))

    # ------------------------------------------------------------------ bonds
    @app.get("/api/funds/{code}/bonds")
    def list_bonds(code: str, _: Viewer):
        t = today_fn()
        return [summary_json(r, t) for r in repo.list_bonds(code)]

    @app.post("/api/funds/{code}/bonds", status_code=201)
    def create_bond(code: str, body: BondSaveBody, e: Editor):
        rec = BondRecord(bond=body.bond, coupon_actuals=body.coupon_actuals)
        build_schedule(rec.bond)                       # refuse to save a bond that cannot be calculated
        return record_json(repo.save_bond(code, rec, user=e.username))

    @app.get("/api/funds/{code}/bonds/{isin}")
    def get_bond(code: str, isin: str, _: Viewer):
        return record_json(repo.get_bond(code, isin))

    @app.put("/api/funds/{code}/bonds/{isin}")
    def update_bond(code: str, isin: str, body: BondUpdateBody, e: Editor):
        if body.bond.isin != isin.strip().upper():
            raise HTTPException(400, "ISIN in the body must match the URL (ISINs cannot be changed)")
        rec = BondRecord(bond=body.bond, coupon_actuals=body.coupon_actuals)
        build_schedule(rec.bond)
        return record_json(repo.save_bond(code, rec, user=e.username, expected_version=body.version))

    @app.delete("/api/funds/{code}/bonds/{isin}")
    def delete_bond(code: str, isin: str, version: int, e: Editor):
        repo.delete_bond(code, isin, user=e.username, expected_version=version)
        return {"ok": True}

    @app.get("/api/funds/{code}/bonds/{isin}/history")
    def bond_history(code: str, isin: str, _: Viewer):
        return repo.bond_history(code, isin)

    @app.get("/api/funds/{code}/bonds/{isin}/history/{file_name}")
    def bond_history_version(code: str, isin: str, file_name: str, _: Viewer):
        return record_json(repo.read_history_version(code, isin, file_name))

    # ------------------------------------------------------------------ calculations
    def _parse_dates(text: Optional[str]) -> list[date]:
        parts = [x.strip() for x in (text or "").split(",") if x.strip()]
        if len(parts) > MAX_CUTOFF_DATES:
            raise HTTPException(422, f"at most {MAX_CUTOFF_DATES} cut-off dates at a time")
        try:
            return sorted({date.fromisoformat(x) for x in parts})
        except ValueError:
            raise HTTPException(422, "dates must be YYYY-MM-DD, comma-separated")

    @app.post("/api/preview/schedule")
    def preview_schedule(bond: BondInput, _: Viewer):
        """Calculate an unsaved bond (used by the entry form)."""
        return schedule_json(build_schedule(bond))

    @app.get("/api/funds/{code}/bonds/{isin}/schedule")
    def bond_schedule(code: str, isin: str, _: Viewer):
        return schedule_json(build_schedule(repo.get_bond(code, isin).bond))

    @app.get("/api/funds/{code}/bonds/{isin}/periods")
    def bond_periods(
        code: str, isin: str, _: Viewer,
        mode: str = Query("years", pattern="^(years|quarters|custom)$"),
        dates: Optional[str] = Query(None, description="custom: comma-separated cut-off dates YYYY-MM-DD"),
        start: Optional[date] = Query(None, description="end of the period before the first cut-off"),
    ):
        fund = repo.get_fund(code)
        rec = repo.get_bond(code, isin)
        b = rec.bond
        sched = build_schedule(b)
        if mode == "custom":
            if not dates:
                raise HTTPException(422, "dates are required for custom mode")
            cutoffs = _parse_dates(dates)
        else:
            close = b.sale.sale_date if b.sale else b.maturity_date
            cutoffs = fiscal_cutoffs(b.purchase_date, close + timedelta(days=400),
                                     fund.year_end_month, fund.year_end_day, quarterly=(mode == "quarters"))
            # stop after the first cut-off on/after the bond closes
            cutoffs = [c for i, c in enumerate(cutoffs) if i == 0 or cutoffs[i - 1] < close]
        if start is not None and cutoffs and start >= cutoffs[0]:
            raise HTTPException(422, "start must be before the first cut-off date")
        rows = compute_periods(sched, cutoffs, start=start)
        return {"isin": b.isin, "day_basis": b.day_basis.value, "irr": sched.irr,
                "rows": [asdict(r) for r in rows], "warnings": sched.warnings}

    @app.get("/api/funds/{code}/annexure")
    def annexure(code: str, _: Viewer, start: Optional[date] = None, end: Optional[date] = None):
        fund = repo.get_fund(code)
        if start is None or end is None:
            ds, de = default_period(fund, today_fn())
            start, end = start or ds, end or de
        if start >= end:
            raise HTTPException(422, "start must be before end")
        a = build_annexure(fund, repo.list_bonds(code), start, end, company_name(fund))
        return {"fund": fund_json(fund), "period_start": a.period_start.isoformat(),
                "period_end": a.period_end.isoformat(), "rows": [asdict(r) for r in a.rows],
                "totals": a.totals, "warnings": a.warnings}

    # ------------------------------------------------------------------ Excel downloads (Phase 5)
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _download(data: bytes, filename: str) -> Response:
        return Response(content=data, media_type=XLSX,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})

    @app.get("/api/funds/{code}/bonds/{isin}/export.xlsx")
    def bond_excel(code: str, isin: str, _: Viewer, dates: Optional[str] = None, start: Optional[date] = None):
        from exports.excel import bond_workbook
        fund = repo.get_fund(code)
        rec = repo.get_bond(code, isin)
        cuts = _parse_dates(dates)
        if start is not None and cuts and start >= cuts[0]:
            raise HTTPException(422, "start must be before the first cut-off date")
        data = bond_workbook(rec, fund, (start, cuts) if cuts else None, company_name(fund))
        return _download(data, f"{rec.isin}_amortization_{today_fn().isoformat()}.xlsx")

    @app.get("/api/funds/{code}/annexure.xlsx")
    def annexure_excel(code: str, _: Viewer, start: Optional[date] = None, end: Optional[date] = None):
        from exports.excel import annexure_workbook
        fund = repo.get_fund(code)
        if start is None or end is None:
            ds, de = default_period(fund, today_fn())
            start, end = start or ds, end or de
        if start >= end:
            raise HTTPException(422, "start must be before end")
        a = build_annexure(fund, repo.list_bonds(code), start, end, company_name(fund))
        return _download(annexure_workbook(a), f"{fund.code}_Annexure-D_{end.isoformat()}.xlsx")

    # ------------------------------------------------------------------ PDF downloads (Phase 6)
    def _pdf(data: bytes, filename: str) -> Response:
        return Response(content=data, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                 "Cache-Control": "no-store"})

    @app.get("/api/funds/{code}/bonds/{isin}/report.pdf")
    def bond_report_pdf(code: str, isin: str, u: Viewer, dates: Optional[str] = None, start: Optional[date] = None):
        from exports.pdf import bond_pdf
        fund = repo.get_fund(code)
        rec = repo.get_bond(code, isin)
        cuts = _parse_dates(dates)
        if start is not None and cuts and start >= cuts[0]:
            raise HTTPException(422, "start must be before the first cut-off date")
        data = bond_pdf(rec, fund, u.full_name or u.username, (start, cuts) if cuts else None,
                        company_name(fund))
        return _pdf(data, f"{rec.isin}_amortization_{today_fn().isoformat()}.pdf")

    @app.get("/api/funds/{code}/annexure.pdf")
    def annexure_report_pdf(code: str, u: Viewer, start: Optional[date] = None, end: Optional[date] = None):
        from exports.pdf import annexure_pdf
        fund = repo.get_fund(code)
        if start is None or end is None:
            ds, de = default_period(fund, today_fn())
            start, end = start or ds, end or de
        if start >= end:
            raise HTTPException(422, "start must be before end")
        a = build_annexure(fund, repo.list_bonds(code), start, end, company_name(fund))
        return _pdf(annexure_pdf(a, u.full_name or u.username), f"{fund.code}_Annexure-D_{end.isoformat()}.pdf")

    @app.get("/api/funds/{code}/bonds/{isin}/summary")
    def bond_summary(code: str, isin: str, _: Viewer):
        return summary_json(repo.get_bond(code, isin), today_fn())

    @app.get("/api/funds/{code}/default-period")
    def fund_default_period(code: str, _: Viewer, quarterly: bool = True):
        s, e = default_period(repo.get_fund(code), today_fn(), quarterly=quarterly)
        return {"start": s.isoformat(), "end": e.isoformat()}

    # ------------------------------------------------------------------ HTML screens (Phase 4)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def no_cache_pages(request: Request, call_next):
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-cache"      # always pick up new versions after an update
        return resp

    PAGES = {"/": "index.html", "/login": "login.html", "/company": "company.html", "/fund": "fund.html", "/bond": "bond.html",
             "/bond/edit": "bond_edit.html", "/annexure": "annexure.html", "/users": "users.html",
             "/account": "account.html"}

    def _page(file_name: str):
        def handler():
            return FileResponse(static_dir / "pages" / file_name, media_type="text/html")
        return handler

    for route, file_name in PAGES.items():
        app.add_api_route(route, _page(file_name), methods=["GET"], include_in_schema=False)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    return app
