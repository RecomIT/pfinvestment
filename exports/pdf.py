"""Phase 6: PDF reports (ReportLab, built-in Helvetica - nothing else to install).

  bond_pdf      - bond details, notes, amortization schedule, financial years, quarters,
                  dates chosen on screen, sign-off block
  annexure_pdf  - Annexure-D in two tables on A4 landscape (A-O principal, P-X interest + sale),
                  sign-off block

Amounts use lakh/crore grouping like the screens; negatives in brackets, zero as "-".
Every page: report title, fund, "Printed by <user> on <date time>", "Page x of y".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from engine import build_schedule, compute_periods
from engine.clock import now_local, today_local
from engine.fmt import dmy, lakh
from engine.models import DayBasis
from engine.periods import report_cutoffs
from reports.annexure import Annexure
from storage.records import BondRecord, Fund

PAGE = landscape(A4)
MARGIN = 12 * mm
BRAND = colors.HexColor("#1F4E79")
GROUP = colors.HexColor("#DDEBF7")
ZEBRA = colors.HexColor("#F5F7FA")
TOTAL = colors.HexColor("#EDEDED")
GRID = colors.HexColor("#BFBFBF")

ST_TITLE = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=14, leading=17, spaceAfter=2)
ST_SUB = ParagraphStyle("s", fontName="Helvetica", fontSize=9, leading=12, textColor=colors.HexColor("#444444"))
ST_H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11, leading=14, spaceBefore=8, spaceAfter=4,
                       textColor=BRAND)
ST_NOTE = ParagraphStyle("n", fontName="Helvetica-Oblique", fontSize=7.5, leading=10,
                         textColor=colors.HexColor("#555555"))
ST_WARN = ParagraphStyle("w", fontName="Helvetica", fontSize=8, leading=10.5, textColor=colors.HexColor("#8A5A00"))
ST_HEAD = ParagraphStyle("hd", fontName="Helvetica-Bold", fontSize=7, leading=8.5, alignment=TA_CENTER,
                         textColor=colors.white)
ST_CENTER = ParagraphStyle("c", fontName="Helvetica-Bold", fontSize=11, leading=14, alignment=TA_CENTER)
ST_CENTER_SUB = ParagraphStyle("cs", fontName="Helvetica", fontSize=9, leading=12, alignment=TA_CENTER)


# ------------------------------------------------------------------ formatting
def money(x: Optional[float]) -> str:
    if x is None:
        return ""
    r = round(x)
    if r == 0:
        return "-"
    return lakh(x, 0)


def money2(x: Optional[float]) -> str:
    return "" if x is None else lakh(x, 2)


def pct(x: float, dp: int = 4) -> str:
    return f"{x * 100:.{dp}f}%"


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ------------------------------------------------------------------ page furniture
class _NumberedCanvas(rl_canvas.Canvas):
    """Two-pass canvas so every page can say 'Page x of y'."""

    def __init__(self, *args, footer_left: str = "", header: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []
        self._footer_left = footer_left
        self._header = header

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self._furniture(total)
            super().showPage()
        super().save()

    def _furniture(self, total: int):
        w, h = self._pagesize
        self.setFont("Helvetica", 7.5)
        self.setFillColor(colors.HexColor("#666666"))
        self.drawString(MARGIN, h - 8 * mm, self._header)
        self.drawString(MARGIN, 7 * mm, self._footer_left)
        self.drawRightString(w - MARGIN, 7 * mm, f"Page {self._pageNumber} of {total}")
        self.setStrokeColor(GRID)
        self.line(MARGIN, h - 9.5 * mm, w - MARGIN, h - 9.5 * mm)
        self.line(MARGIN, 10 * mm, w - MARGIN, 10 * mm)


def _build(story, header: str, printed_by: str, title: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=PAGE, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=title, author=printed_by, creator="Bond Amortization app")
    now = now_local()
    footer = f"Printed by {printed_by} on {dmy(now.date())} {now.strftime('%H:%M')}"
    doc.build(story, canvasmaker=lambda *a, **k: _NumberedCanvas(*a, footer_left=footer, header=header, **k))
    return buf.getvalue()


def _table(header: list[str], rows: list[list], col_widths: list[float], num_cols: set[int],
           total_row: Optional[list] = None, group_row: Optional[list[tuple[str, int, int]]] = None,
           font_size: float = 7.5) -> Table:
    """A data table: wrapped header cells, right-aligned numbers, zebra rows, bold total row,
    header (and optional group row) repeated on every page."""
    data = []
    styles = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("LEADING", (0, 0), (-1, -1), font_size + 2),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.25, GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    head_rows = 0
    if group_row:
        g = [""] * len(header)
        for text, c0, c1 in group_row:
            g[c0] = text
            if c1 > c0:
                styles.append(("SPAN", (c0, 0), (c1, 0)))
        data.append(g)
        styles += [("BACKGROUND", (0, 0), (-1, 0), GROUP), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                   ("ALIGN", (0, 0), (-1, 0), "CENTER")]
        head_rows = 1
    data.append([Paragraph(_esc(h), ST_HEAD) for h in header])
    styles += [("BACKGROUND", (0, head_rows), (-1, head_rows), BRAND)]
    head_rows += 1
    data += rows
    for c in num_cols:
        styles.append(("ALIGN", (c, head_rows), (c, -1), "RIGHT"))
    for i in range(len(rows)):
        if i % 2 == 1:
            styles.append(("BACKGROUND", (0, head_rows + i), (-1, head_rows + i), ZEBRA))
    if total_row is not None:
        data.append(total_row)
        styles += [("BACKGROUND", (0, -1), (-1, -1), TOTAL), ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                   ("LINEABOVE", (0, -1), (-1, -1), 1, colors.black)]
    t = Table(data, colWidths=col_widths, repeatRows=head_rows)
    t.setStyle(TableStyle(styles))
    return t


def _kv_table(pairs: list[tuple[str, str]], columns: int = 2) -> Table:
    """Label/value pairs laid out in `columns` side-by-side blocks."""
    per = -(-len(pairs) // columns)
    rows = []
    for i in range(per):
        row = []
        for c in range(columns):
            k = c * per + i
            row += list(pairs[k]) if k < len(pairs) else ["", ""]
        rows.append(row)
    usable = PAGE[0] - 2 * MARGIN
    w = usable / columns
    t = Table(rows, colWidths=[w * 0.45, w * 0.55] * columns)
    st = [("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
          ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
          ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E3E3E3"))]
    for c in range(columns):
        st += [("FONTNAME", (2 * c, 0), (2 * c, -1), "Helvetica-Bold"),
               ("TEXTCOLOR", (2 * c, 0), (2 * c, -1), colors.HexColor("#444444")),
               ("ALIGN", (2 * c + 1, 0), (2 * c + 1, -1), "RIGHT")]
    t.setStyle(TableStyle(st))
    return t


def signoff(prepared_by: str) -> KeepTogether:
    usable = PAGE[0] - 2 * MARGIN
    cell = usable / 3
    name_row = [prepared_by, "", ""]
    t = Table(
        [["", "", ""], ["Prepared by", "Checked by", "Approved by"], name_row,
         [f"Date: {dmy(today_local())}", "Date:", "Date:"]],
        colWidths=[cell] * 3, rowHeights=[16 * mm, None, None, None])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (0, 0), 0.8, colors.black), ("LINEBELOW", (1, 0), (1, 0), 0.8, colors.black),
        ("LINEBELOW", (2, 0), (2, 0), 0.8, colors.black),
        ("LEFTPADDING", (0, 0), (-1, -1), 6 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 6 * mm),
    ]))
    return KeepTogether([Spacer(1, 8 * mm), t])


# ====================================================================== bond report
def _period_table(sched, cuts, start) -> Table:
    rows = compute_periods(sched, cuts, start=start)
    header = ["Period end", "Opening carrying value", "Investment", "Amortization", "Encashed",
              "Closing carrying value", "Opening interest receivable", "Interest income", "Coupon received (gross)",
              "Holding period interest", "Accrued interest on sale", "Gain / (loss) on sale",
              "Closing interest receivable", "Check"]
    body = []
    for p in rows:
        ok = abs(p.carrying_check) < 0.01 and abs(p.receivable_check) < 0.01
        body.append([dmy(p.period_end), money(p.opening_carrying), money(p.investment), money(p.amortization),
                     money(p.encashed_principal), money(p.closing_carrying), money(p.opening_receivable),
                     money(p.interest_income), money(p.coupon_gross), money(p.purchased_accrued_recovered),
                     money(p.sale_accrued_received), money(p.gain_loss), money(p.closing_receivable),
                     "OK" if ok else "CHECK"])
    s = lambda k: sum(getattr(p, k) for p in rows)          # noqa: E731
    total = ["Total", "", money(s("investment")), money(s("amortization")), money(s("encashed_principal")), "", "",
             money(s("interest_income")), money(s("coupon_gross")), money(s("purchased_accrued_recovered")),
             money(s("sale_accrued_received")), money(s("gain_loss")), "", ""] if rows else None
    usable = PAGE[0] - 2 * MARGIN
    widths = [16 * mm] + [(usable - 16 * mm - 12 * mm) / 12] * 12 + [12 * mm]
    return _table(header, body, widths, set(range(1, 13)), total)


def bond_pdf(rec: BondRecord, fund: Fund, printed_by: str,
             selected: Optional[tuple[Optional[date], list[date]]] = None, company_name: str = "") -> bytes:
    b = rec.bond
    sched = build_schedule(b)
    title = f"Bond amortization: {b.isin}"
    story = [Paragraph(_esc(f"{b.isin}{' - ' + b.name if b.name else ''}"), ST_TITLE),
             Paragraph(_esc(f"{company_name + ' · ' if company_name else ''}{fund.name} ({fund.code}) · "
                            f"Bond amortization schedule (effective interest method)"),
                       ST_SUB), Spacer(1, 4 * mm)]
    basis = "Excel (days / 182)" if b.day_basis == DayBasis.EXCEL_182 else "Actual days"
    pairs = [
        ("Face value", money(b.face_value)), ("Clean price paid", money2(b.clean_price)),
        ("Accrued interest paid", money2(b.accrued_interest_paid)), ("Settlement amount", money2(b.settlement_amount)),
        ("Discount / (premium)", money2(b.discount_premium)), ("Coupon rate", pct(b.coupon_rate, 2)),
        ("Coupon each half-year", money2(b.coupon_amount)), ("IRR (XIRR)", pct(sched.irr)),
        ("Issue date", dmy(b.issue_date)), ("Purchase date", dmy(b.purchase_date)),
        ("Maturity date", dmy(b.maturity_date)), ("Day basis at cut-off dates", basis),
        ("Coupon periods", str(len(sched.rows))), ("Fund TDS rate", pct(fund.tds_rate, 2)),
    ]
    if b.sale:
        pairs += [("Sale date", dmy(b.sale.sale_date)), ("Clean sale proceeds", money2(b.sale.clean_proceeds)),
                  ("Accrued interest received on sale", money2(b.sale.accrued_interest_received))]
    story.append(_kv_table(pairs, columns=3))
    if sched.warnings:
        story.append(Spacer(1, 3 * mm))
        story += [Paragraph(_esc("Note: " + w), ST_WARN) for w in sched.warnings]

    # ---- schedule
    story += [Paragraph("Amortization schedule", ST_H2),
              Paragraph("Interest = opening carrying value x ((1 + IRR) ^ (days / 365) - 1). Amortization = interest - "
                        "coupon received (net of accrued interest bought). The last period ends exactly at face value.",
                        ST_NOTE), Spacer(1, 2 * mm)]
    header = ["#", "From", "Coupon date", "Days", "Beginning carrying value", "Interest (IRR)", "Coupon (gross)",
              "Accrued interest bought, recovered", "Coupon received (net)", "Amortization", "Cumulative amortization",
              "Ending carrying value"]
    body = [[str(r.index + 1), dmy(r.start), dmy(r.end), str(r.days), money(r.opening), money(r.interest),
             money(r.coupon_gross), money(r.purchased_accrued_recovered), money(r.coupon_net), money(r.amortization),
             money(r.cumulative_amortization), money(r.closing)] for r in sched.rows]
    tot = lambda k: sum(getattr(r, k) for r in sched.rows)   # noqa: E731
    total = ["", "Total", "", "", "", money(tot("interest")), money(tot("coupon_gross")),
             money(tot("purchased_accrued_recovered")), money(tot("coupon_net")), money(tot("amortization")), "", ""]
    usable = PAGE[0] - 2 * MARGIN
    fixed = [8 * mm, 16 * mm, 16 * mm, 10 * mm]
    widths = fixed + [(usable - sum(fixed)) / 8] * 8
    story.append(_table(header, body, widths, {0, 3, 4, 5, 6, 7, 8, 9, 10, 11}, total))

    # ---- periods
    before = b.purchase_date - timedelta(days=1)
    for heading, cuts, start in (
        ("Financial years", report_cutoffs(b, fund.year_end_month, fund.year_end_day, False), before),
        ("Quarters", report_cutoffs(b, fund.year_end_month, fund.year_end_day, True), before),
    ) + ((("Dates chosen on screen", sorted(set(selected[1])), selected[0] or before),) if selected and selected[1]
         else ()):
        story += [PageBreak(), Paragraph(f"{heading}: interest income, amortization and carrying value", ST_H2),
                  Paragraph(_esc(f"Each period runs from the day after the previous date (first: {dmy(start)}) to the "
                                 f"cut-off date. Coupon periods are split at cut-off dates on the {basis} basis; the "
                                 f"first (stub) period uses actual days."), ST_NOTE), Spacer(1, 2 * mm),
                  _period_table(sched, cuts, start)]

    story.append(signoff(printed_by))
    who = f"{company_name} · {fund.name}" if company_name else fund.name
    return _build(story, header=f"{who} · {b.isin} · Bond amortization", printed_by=printed_by, title=title)


# ====================================================================== Annexure-D report
def annexure_pdf(a: Annexure, printed_by: str) -> bytes:
    period = f"For the period {dmy(a.period_start + timedelta(days=1))} to {dmy(a.period_end)}"
    from exports.excel import annexure_titles
    line1, line2 = annexure_titles(a.fund.name, a.company_name)
    story = [Paragraph(_esc(line1), ST_CENTER), Paragraph(_esc(line2 + " (Annexure-D)"), ST_CENTER_SUB),
             Paragraph(_esc(period), ST_CENTER_SUB), Spacer(1, 4 * mm)]
    usable = PAGE[0] - 2 * MARGIN
    rows, T = a.rows, a.totals

    # Table 1: A-O (particulars + principal)
    h1 = ["A\nSerial", "B\nISIN", "C\nIssue date", "D\nPurchase date", "E\nMaturity date", "F\nNature",
          "G\nCoupon rate", "H\nIRR", "I\nFace value", "J\nPurchase / settlement price", "K\nOpening investment",
          "L\nInvestment during the period", "M\nAmortization discount / (premium)", "N\nEncashed",
          "O\nClosing balance"]
    b1 = [[str(r.serial), r.isin, dmy(r.issue_date), dmy(r.purchase_date), dmy(r.maturity_date), r.nature,
           pct(r.coupon_rate, 2), pct(r.irr), money(r.face_value), money(r.settlement_amount),
           money(r.opening_investment), money(r.investment), money(r.amortization), money(r.encashed),
           money(r.closing_balance)] for r in rows]
    t1 = ["Total", "", "", "", "", "", "", "", money(T["face_value"]), money(T["settlement_amount"]),
          money(T["opening_investment"]), money(T["investment"]), money(T["amortization"]), money(T["encashed"]),
          money(T["closing_balance"])] if rows else None
    fixed = [11 * mm, 24 * mm, 15 * mm, 15 * mm, 15 * mm, 14 * mm, 13 * mm, 15 * mm]
    w1 = fixed + [(usable - sum(fixed)) / 7] * 7
    story += [Paragraph("Particulars and principal", ST_H2),
              _table([h.replace("\n", " ") for h in h1], b1, w1, {0, 6, 7, 8, 9, 10, 11, 12, 13, 14}, t1,
                     group_row=[("Particulars", 0, 9), ("Principal", 10, 14)])]

    # Table 2: P-X (interest + sale), keyed by serial and ISIN
    h2 = ["A\nSerial", "B\nISIN", "P\nOpening interest", "Q\nInterest income during the period",
          "R\nHolding period interest", "S\nGross interest receipt", "T\nInterest receipt", "U\nTDS",
          "V\nEncashed", "W\nClosing interest receivable", "X\nGain / (loss) on sale"]
    b2 = [[str(r.serial), r.isin, money(r.opening_interest), money(r.interest_income),
           money(r.holding_period_interest), money(r.gross_interest_receipt), money(r.interest_receipt),
           money(r.tds), money(r.encashed_interest), money(r.closing_interest_receivable), money(r.gain_loss)]
          for r in rows]
    t2 = ["Total", "", money(T["opening_interest"]), money(T["interest_income"]), money(T["holding_period_interest"]),
          money(T["gross_interest_receipt"]), money(T["interest_receipt"]), money(T["tds"]),
          money(T["encashed_interest"]), money(T["closing_interest_receivable"]), money(T["gain_loss"])] if rows else None
    fixed2 = [11 * mm, 24 * mm]
    w2 = fixed2 + [(usable - sum(fixed2)) / 9] * 9
    story += [Paragraph("Interest and sale", ST_H2),
              _table([h.replace("\n", " ") for h in h2], b2, w2, {0, 2, 3, 4, 5, 6, 7, 8, 9, 10}, t2,
                     group_row=[("", 0, 1), ("Interest", 2, 9), ("Sale", 10, 10)])]
    if not rows:
        story.append(Paragraph("No bonds were held in this period.", ST_SUB))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("O = K + L + M - N · W = P + Q - M - S + R - V · S = T + U. Amounts in Taka, rounded to "
                           "whole taka for display.", ST_NOTE))
    for w in a.warnings:
        story.append(Paragraph(_esc("Note: " + w), ST_WARN))
    story.append(signoff(printed_by))
    who = f"{a.company_name} · {a.fund.name}" if a.company_name else a.fund.name
    return _build(story, header=f"{who} · Annexure-D · {period}", printed_by=printed_by,
                  title=f"Annexure-D {a.fund.code} {dmy(a.period_end)}")
