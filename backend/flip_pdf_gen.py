"""
Flip Board PDF Generator

Reads flip-board.json and produces either:
  - Individual deal PDF (--deal <id> --out file.pdf)
  - Multi-deal comparison PDF (--compare --out file.pdf)

Usage:
  python3 generate_flip_board_pdf.py --board flip-board.json --deal 2007-emerson-ave --out report.pdf
  python3 generate_flip_board_pdf.py --board flip-board.json --compare --out comparison.pdf
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime

from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)
from reportlab.platypus.flowables import Flowable

# ============== COLOR PALETTE ==============
NAVY = colors.HexColor("#1B2A4A")
DARK_GRAY = colors.HexColor("#333333")
LIGHT_GRAY = colors.HexColor("#F5F5F5")
GREEN = colors.HexColor("#2E7D32")
RED = colors.HexColor("#C62828")
YELLOW = colors.HexColor("#F9A825")
ORANGE = colors.HexColor("#EF6C00")
BLUE_LIGHT = colors.HexColor("#E3F2FD")
GREEN_LIGHT = colors.HexColor("#E8F5E9")
RED_LIGHT = colors.HexColor("#FFEBEE")
ORANGE_LIGHT = colors.HexColor("#FFF3E0")
WHITE = colors.white
BORDER_GRAY = colors.HexColor("#CCCCCC")


# ============== HELPERS ==============

def fmt_money(v, signed=False):
    if v is None:
        return "—"
    if signed:
        if v > 0:
            return f"+${abs(v):,.0f}"
        if v < 0:
            return f"-${abs(v):,.0f}"
        return "$0"
    return f"${v:,.0f}"


def fmt_pct(v, signed=False):
    if v is None:
        return "—"
    if signed and v > 0:
        return f"+{v:.1f}%"
    return f"{v:.1f}%"


def score_color(score):
    if score is None:
        return colors.HexColor("#999999")
    if score >= 70:
        return GREEN
    if score >= 55:
        return YELLOW
    if score >= 40:
        return ORANGE
    return RED


def signal_color(signal):
    if not signal:
        return DARK_GRAY
    s = signal.upper()
    if any(x in s for x in ["SLAM", "STRONG BUY", "GOOD FLIP"]):
        return GREEN
    if "POSSIBLE" in s or "WATCH" in s:
        return YELLOW
    if "RISKY" in s or "MARGINAL" in s or "CAUTION" in s:
        return ORANGE
    if "AVOID" in s or "NO DEAL" in s or "PASS" in s:
        return RED
    return DARK_GRAY


def severity_color(sev):
    if not sev:
        return colors.HexColor("#90A4AE")
    s = sev.upper()
    if "CRIT" in s:
        return RED
    if "HIGH" in s:
        return colors.HexColor("#E53935")
    if "MED-HIGH" in s or "MEDIUM-HIGH" in s:
        return ORANGE
    if "LOW-MED" in s or "LOW-MEDIUM" in s:
        return colors.HexColor("#9CCC65")
    if "MED" in s:
        return YELLOW
    if "LOW" in s:
        return GREEN
    return colors.HexColor("#90A4AE")


# ============== FLOWABLES ==============

class ScoreGauge(Flowable):
    """Circular score gauge with arc colored by score."""

    def __init__(self, score, grade, size=2.4 * inch):
        Flowable.__init__(self)
        self.score = score if score is not None else 0
        self.grade = grade or "?"
        self.size = size
        self.width = size
        self.height = size

    def draw(self):
        c = self.canv
        r = self.size / 2
        cx, cy = r, r
        c.setStrokeColor(colors.HexColor("#E0E0E0"))
        c.setLineWidth(14)
        c.circle(cx, cy, r - 12, stroke=1, fill=0)
        color = score_color(self.score)
        c.setStrokeColor(color)
        c.setLineWidth(14)
        sweep = -360.0 * (self.score / 100.0)
        steps = max(int(abs(sweep) / 4), 6)
        start_a = math.radians(90)
        path = c.beginPath()
        path.moveTo(cx + (r - 12) * math.cos(start_a),
                    cy + (r - 12) * math.sin(start_a))
        for i in range(1, steps + 1):
            a = math.radians(90 + sweep * i / steps)
            path.lineTo(cx + (r - 12) * math.cos(a),
                        cy + (r - 12) * math.sin(a))
        c.drawPath(path, stroke=1, fill=0)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 38)
        c.drawCentredString(cx, cy + 2, str(self.score))
        c.setFont("Helvetica", 9)
        c.setFillColor(DARK_GRAY)
        c.drawCentredString(cx, cy - 14, "FLIP SCORE")
        c.setFont("Helvetica-Bold", 14)
        c.setFillColor(color)
        c.drawCentredString(cx, cy - 32, f"Grade: {self.grade}")


class MiniGauge(Flowable):
    """Smaller score gauge for comparison pages."""

    def __init__(self, score, grade, size=1.3 * inch):
        Flowable.__init__(self)
        self.score = score if score is not None else 0
        self.grade = grade or "?"
        self.size = size
        self.width = size
        self.height = size

    def draw(self):
        c = self.canv
        r = self.size / 2
        cx, cy = r, r
        c.setStrokeColor(colors.HexColor("#E0E0E0"))
        c.setLineWidth(8)
        c.circle(cx, cy, r - 7, stroke=1, fill=0)
        color = score_color(self.score)
        c.setStrokeColor(color)
        c.setLineWidth(8)
        sweep = -360.0 * (self.score / 100.0)
        steps = max(int(abs(sweep) / 4), 6)
        start_a = math.radians(90)
        path = c.beginPath()
        path.moveTo(cx + (r - 7) * math.cos(start_a),
                    cy + (r - 7) * math.sin(start_a))
        for i in range(1, steps + 1):
            a = math.radians(90 + sweep * i / steps)
            path.lineTo(cx + (r - 7) * math.cos(a),
                        cy + (r - 7) * math.sin(a))
        c.drawPath(path, stroke=1, fill=0)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 22)
        c.drawCentredString(cx, cy - 2, str(self.score))
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(color)
        c.drawCentredString(cx, cy - 18, self.grade)


# ============== PAGE DECORATIONS ==============

def make_footer(label):
    def footer(canv, doc):
        canv.saveState()
        canv.setFont("Helvetica", 8)
        canv.setFillColor(colors.HexColor("#888888"))
        canv.drawString(0.5 * inch, 0.4 * inch, label)
        canv.drawRightString(letter[0] - 0.5 * inch, 0.4 * inch, f"Page {doc.page}")
        canv.setStrokeColor(BORDER_GRAY)
        canv.line(0.5 * inch, 0.55 * inch, letter[0] - 0.5 * inch, 0.55 * inch)
        canv.restoreState()
    return footer


def make_footer_landscape(label):
    def footer(canv, doc):
        canv.saveState()
        canv.setFont("Helvetica", 8)
        canv.setFillColor(colors.HexColor("#888888"))
        canv.drawString(0.5 * inch, 0.4 * inch, label)
        canv.drawRightString(letter[1] - 0.5 * inch, 0.4 * inch, f"Page {doc.page}")
        canv.setStrokeColor(BORDER_GRAY)
        canv.line(0.5 * inch, 0.55 * inch, letter[1] - 0.5 * inch, 0.55 * inch)
        canv.restoreState()
    return footer


# ============== STYLES ==============

def build_styles():
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("H1", parent=base["Heading1"], fontName="Helvetica-Bold",
                              fontSize=20, textColor=NAVY, spaceAfter=10, leading=24),
        "h2": ParagraphStyle("H2", parent=base["Heading2"], fontName="Helvetica-Bold",
                              fontSize=14, textColor=NAVY, spaceAfter=6, spaceBefore=12,
                              leading=17),
        "h3": ParagraphStyle("H3", parent=base["Heading3"], fontName="Helvetica-Bold",
                              fontSize=11, textColor=NAVY, spaceAfter=4, spaceBefore=8,
                              leading=14),
        "body": ParagraphStyle("Body", parent=base["BodyText"], fontName="Helvetica",
                                fontSize=9.5, textColor=DARK_GRAY, leading=13,
                                spaceAfter=6, alignment=TA_JUSTIFY),
        "body_l": ParagraphStyle("BodyL", parent=base["BodyText"], fontName="Helvetica",
                                  fontSize=9.5, textColor=DARK_GRAY, leading=13,
                                  spaceAfter=6, alignment=TA_LEFT),
        "body_c": ParagraphStyle("BodyC", parent=base["BodyText"], fontName="Helvetica",
                                  fontSize=9.5, textColor=DARK_GRAY, leading=13,
                                  spaceAfter=6, alignment=TA_CENTER),
        "bullet": ParagraphStyle("Bullet", parent=base["BodyText"], fontName="Helvetica",
                                  fontSize=9.5, textColor=DARK_GRAY, leading=13,
                                  spaceAfter=3, alignment=TA_LEFT, leftIndent=14,
                                  bulletIndent=4),
        "disclaimer": ParagraphStyle("Disc", parent=base["BodyText"], fontName="Helvetica",
                                      fontSize=7.5, textColor=colors.HexColor("#666666"),
                                      leading=10, alignment=TA_JUSTIFY),
        "cover_addr": ParagraphStyle("CA", fontName="Helvetica-Bold", fontSize=20,
                                      alignment=TA_CENTER, textColor=NAVY, leading=24),
        "alert_box": ParagraphStyle("Alert", fontName="Helvetica-Bold", fontSize=11,
                                     alignment=TA_LEFT, textColor=WHITE, leading=14),
    }


# ============== COMPUTATION ==============

def compute_metrics(deal):
    """Compute all derived metrics from raw deal data. Returns a dict."""
    m = {}
    pp = deal.get("purchase_price", 0) or 0
    arv = deal.get("arv_base", 0) or 0
    arv_low = deal.get("arv_low") or int(arv * 0.92)
    arv_high = deal.get("arv_high") or int(arv * 1.08)
    rehab = deal.get("rehab_base", 0) or 0
    rehab_low = deal.get("rehab_low") or int(rehab * 0.8)
    rehab_high = deal.get("rehab_high") or int(rehab * 1.25)
    hold_mo = deal.get("holding_months", 5) or 5
    hold_mo_cost = deal.get("holding_cost_monthly", 500) or 500
    sell_pct = deal.get("selling_cost_pct", 8) or 8

    closing = round(pp * 0.02)
    acq = pp + closing
    holding = hold_mo * hold_mo_cost
    selling = round(arv * sell_pct / 100)
    all_in = acq + rehab + holding + selling
    net = arv - all_in
    invested = acq + rehab + holding
    roi = (net / invested * 100) if invested > 0 else 0
    annualized = roi * (12 / hold_mo) if hold_mo > 0 else 0
    margin = (net / arv * 100) if arv > 0 else 0

    m["closing"] = closing
    m["acquisition"] = acq
    m["holding"] = holding
    m["selling"] = selling
    m["all_in"] = all_in
    m["net_profit"] = net
    m["roi"] = roi
    m["annualized_roi"] = annualized
    m["margin"] = margin

    # Scenarios
    sc_best_arv = arv_high
    sc_best_rehab = rehab_low
    sc_best_hold = max(3, hold_mo - 1)
    sc_best_selling = round(sc_best_arv * sell_pct / 100)
    sc_best_all = acq + sc_best_rehab + (sc_best_hold * hold_mo_cost) + sc_best_selling
    sc_best_net = sc_best_arv - sc_best_all
    sc_best_roi = (sc_best_net / (acq + sc_best_rehab + sc_best_hold * hold_mo_cost) * 100) if (acq + sc_best_rehab) > 0 else 0

    sc_worst_arv = arv_low
    sc_worst_rehab = rehab_high
    sc_worst_hold = hold_mo + 2
    sc_worst_selling = round(sc_worst_arv * sell_pct / 100)
    sc_worst_all = acq + sc_worst_rehab + (sc_worst_hold * hold_mo_cost) + sc_worst_selling
    sc_worst_net = sc_worst_arv - sc_worst_all
    sc_worst_roi = (sc_worst_net / (acq + sc_worst_rehab + sc_worst_hold * hold_mo_cost) * 100) if (acq + sc_worst_rehab) > 0 else 0

    m["scenarios"] = [
        {"name": "Best Case", "arv": sc_best_arv, "rehab": sc_best_rehab,
         "hold": sc_best_hold, "net": sc_best_net, "roi": sc_best_roi},
        {"name": "Base Case", "arv": arv, "rehab": rehab,
         "hold": hold_mo, "net": net, "roi": roi},
        {"name": "Worst Case", "arv": sc_worst_arv, "rehab": sc_worst_rehab,
         "hold": sc_worst_hold, "net": sc_worst_net, "roi": sc_worst_roi},
    ]

    # 70% rule
    max_p_70 = (arv * 0.70) - rehab
    m["max_purchase_70"] = max_p_70
    m["rule_70_pct_of_arv"] = ((pp + rehab) / arv * 100) if arv > 0 else 0
    m["rule_70_pass"] = pp <= max_p_70
    m["rule_70_overage"] = pp - max_p_70

    # Back-solver: max purchase for target profit margins
    backsolve = []
    for target_pct in [10, 15, 20, 25]:
        target_profit = arv * (target_pct / 100)
        max_acq_for_target = arv - target_profit - rehab - holding - selling
        max_purchase = max_acq_for_target / 1.02 if max_acq_for_target > 0 else 0
        backsolve.append({"target_margin": target_pct, "max_purchase": int(max_purchase)})
    m["backsolve"] = backsolve

    # Rental metrics
    rent = deal.get("estimated_rent", 0) or 0
    taxes_m = deal.get("monthly_taxes", 0) or 0
    ins_m = deal.get("monthly_insurance", 0) or 0
    hoa_m = deal.get("monthly_hoa", 0) or 0
    maint_m = deal.get("monthly_maintenance", 0) or 0
    mgmt_m = deal.get("monthly_mgmt", 0) or round(rent * 0.10)
    vacancy_pct = deal.get("vacancy_pct", 8) or 8

    gross_yr = rent * 12
    vacancy_loss = gross_yr * vacancy_pct / 100
    opex_yr = 12 * (taxes_m + ins_m + hoa_m + maint_m + mgmt_m)
    noi = gross_yr - vacancy_loss - opex_yr
    total_capital = acq + rehab
    cap_rate = (noi / total_capital * 100) if total_capital > 0 else 0
    coc = cap_rate
    grm = (total_capital / gross_yr) if gross_yr > 0 else 0
    monthly_net = (rent - (rent * vacancy_pct / 100) - taxes_m - ins_m - hoa_m - maint_m - mgmt_m)

    m["rent"] = {
        "monthly_gross": rent,
        "monthly_net": monthly_net,
        "annual_noi": noi,
        "cap_rate": cap_rate,
        "coc": coc,
        "grm": grm,
        "opex_breakdown": {
            "taxes": taxes_m, "insurance": ins_m, "hoa": hoa_m,
            "maintenance": maint_m, "management": mgmt_m,
            "vacancy_monthly": rent * vacancy_pct / 100,
        },
    }

    # BRRRR
    refi_value = arv * 0.70
    capital_left_in = total_capital - refi_value  # negative = cash out
    # P&I at 7.5%, 30yr: ~$7/$1000 monthly
    monthly_pi = refi_value * 0.00699
    monthly_piti = monthly_pi + taxes_m + ins_m + hoa_m
    brrrr_monthly_cf = rent - (monthly_piti + maint_m + mgmt_m + rent * vacancy_pct / 100)
    m["brrrr"] = {
        "refi_value": refi_value,
        "capital_left_in": capital_left_in,
        "capital_recovered": refi_value,
        "monthly_PI": monthly_pi,
        "monthly_PITI": monthly_piti,
        "monthly_cash_flow": brrrr_monthly_cf,
        "annual_cash_flow": brrrr_monthly_cf * 12,
    }

    # Financing options
    rehab_total = rehab
    m["financing"] = [
        {"option": "Cash", "down": "100%", "rate": "N/A",
         "cost_6mo": 0, "total_capital_needed": acq + rehab + holding,
         "feasibility": "Best for low-purchase deals"},
        {"option": "Hard Money", "down": "10-20%", "rate": "11-13% + 2-3 pts",
         "cost_6mo": int(acq * 0.07 + acq * 0.025),
         "total_capital_needed": int(acq * 0.15 + rehab),
         "feasibility": "Standard for flips $100K+"},
        {"option": "Private Lender", "down": "15-25%", "rate": "8-10%",
         "cost_6mo": int(acq * 0.045),
         "total_capital_needed": int(acq * 0.20 + rehab),
         "feasibility": "Best if relationship available"},
        {"option": "HELOC (other property)", "down": "N/A", "rate": "9-10%",
         "cost_6mo": int(acq * 0.05),
         "total_capital_needed": 0,
         "feasibility": "Good fit for smaller deals"},
    ]

    # Recommended strategy
    market_yoy = deal.get("market_trend_yoy_pct", 0) or 0
    recs = []
    if roi >= 20 and market_yoy >= -3:
        recs.append("FLIP")
    if cap_rate >= 8 and brrrr_monthly_cf >= 200:
        recs.append("BRRRR")
    if cap_rate >= 9 and roi < 15:
        recs.append("RENT (hold)")
    if not m["rule_70_pass"] and (pp - max_p_70) > 30000 and roi < 8:
        recs.append("WHOLESALE / PASS")
    if not recs:
        if roi >= 8:
            recs.append("FLIP (modest)")
        elif cap_rate >= 6:
            recs.append("BRRRR (modest)")
        else:
            recs.append("PASS / RENEGOTIATE")
    m["recommended_strategy"] = recs

    # FLIP vs RENT alert
    m["flip_to_rent_alert"] = cap_rate >= 9 and roi <= 12

    return m


# ============== TABLE BUILDERS ==============

def base_table_style(n_rows, header_navy=True, alt_rows=True):
    style = [
        ("FONT", (0, 1), (-1, -1), "Helvetica", 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if header_navy:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9.5),
        ]
    if alt_rows:
        for i in range(1, n_rows):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), LIGHT_GRAY))
    return style


def kv_table(rows, col_widths, hilite_idx=None, hilite_color=None):
    """Key/value vertical table with bold left col."""
    t = Table(rows, colWidths=col_widths)
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9.5),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), NAVY),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT_GRAY]),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    if hilite_idx is not None and hilite_color is not None:
        style.append(("BACKGROUND", (1, hilite_idx), (1, hilite_idx), hilite_color))
        style.append(("TEXTCOLOR", (1, hilite_idx), (1, hilite_idx), WHITE))
        style.append(("FONT", (1, hilite_idx), (1, hilite_idx), "Helvetica-Bold", 10))
    t.setStyle(TableStyle(style))
    return t


# ============== INDIVIDUAL DEAL PDF ==============

def build_deal_pdf(deal, m, output_path):
    """Generate individual deal PDF."""
    doc = SimpleDocTemplate(
        output_path, pagesize=letter,
        leftMargin=0.55 * inch, rightMargin=0.55 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title=f"Flip Board - {deal['address']}",
    )
    s = build_styles()
    story = []

    # ===== COVER =====
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("FLIP BOARD — DEAL ANALYSIS", ParagraphStyle(
        "T", fontName="Helvetica-Bold", fontSize=11, alignment=TA_CENTER,
        textColor=colors.HexColor("#888888"), spaceAfter=18, letterSpacing=2)))
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph(deal["address"], s["cover_addr"]))
    city_line = f"{deal.get('city','')}, {deal.get('state','')} {deal.get('zip','')}"
    if city_line.strip(", "):
        story.append(Paragraph(city_line, s["cover_addr"]))
    if deal.get("neighborhood"):
        story.append(Paragraph(deal["neighborhood"], ParagraphStyle(
            "N", fontSize=11, alignment=TA_CENTER,
            textColor=colors.HexColor("#888888"), spaceAfter=20)))

    story.append(Spacer(1, 0.4 * inch))
    gauge = ScoreGauge(deal.get("score", 0), deal.get("grade", "?"), size=2.4 * inch)
    gt = Table([[gauge]], colWidths=[doc.width])
    gt.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story.append(gt)
    story.append(Spacer(1, 0.12 * inch))

    sig_hex = signal_color(deal.get("signal", "")).hexval()[2:]
    story.append(Paragraph(
        f'<para align="center"><b><font color="#{sig_hex}" size="14">'
        f'SIGNAL: {deal.get("signal", "—")}</font></b></para>', s["body_c"]))

    # Flip-to-rent alert at top of cover if triggered
    if m.get("flip_to_rent_alert"):
        story.append(Spacer(1, 0.15 * inch))
        alert = Table([[Paragraph(
            "<b>ALERT — Convert to Rental?</b><br/>"
            f"Cap rate {m['rent']['cap_rate']:.1f}% vs flip ROI {m['roi']:.1f}%. "
            "Rental conversion may produce better long-term returns than flipping. See page 7.",
            s["alert_box"])]], colWidths=[doc.width - 0.4 * inch])
        alert.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ORANGE),
            ("BOX", (0, 0), (-1, -1), 0, ORANGE),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        story.append(alert)

    story.append(Spacer(1, 0.3 * inch))

    # Quick numbers
    net_pct = m["net_profit"]
    summary_rows = [
        ["Purchase Price", fmt_money(deal["purchase_price"])],
        ["Estimated ARV", fmt_money(deal["arv_base"])],
        ["Total Rehab Budget", fmt_money(deal["rehab_base"])],
        ["All-In Cost", fmt_money(m["all_in"])],
        ["Net Profit", fmt_money(net_pct, signed=True)],
        ["ROI", f"{m['roi']:.1f}% (annualized ~{m['annualized_roi']:.0f}%)"],
        ["70% Rule", f"{'PASS' if m['rule_70_pass'] else 'FAIL'} — {m['rule_70_pct_of_arv']:.0f}% of ARV"],
        ["Recommended Strategy", " / ".join(m["recommended_strategy"])],
    ]
    profit_hilite = GREEN if net_pct > 5000 else (RED if net_pct < 0 else YELLOW)
    t = kv_table(summary_rows, [2.8 * inch, 3.4 * inch], hilite_idx=4, hilite_color=profit_hilite)
    wrap = Table([[t]], colWidths=[doc.width])
    wrap.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    story.append(wrap)

    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph(
        f'<para align="center"><font size="9" color="#666666">'
        f'Report Date: {datetime.now().strftime("%B %d, %Y")}</font></para>',
        s["body_c"]))
    story.append(PageBreak())

    # ===== PAGE 2: PROPERTY OVERVIEW + NEIGHBORHOOD =====
    story.append(Paragraph("Property Overview", s["h1"]))
    specs = [
        ["Property Type", deal.get("property_type", "—")],
        ["Bedrooms / Baths", f"{deal.get('beds','?')} / {deal.get('baths','?')}"],
        ["Square Footage", f"{deal.get('sqft','?'):,} sq ft" if deal.get("sqft") else "—"],
        ["Year Built", str(deal.get("year_built", "—"))],
        ["Lot Size", deal.get("lot_size", "—")],
        ["Purchase Price", fmt_money(deal["purchase_price"])],
        ["Status", deal.get("status", "evaluating").upper()],
    ]
    t = Table(specs, colWidths=[1.7 * inch, 4.3 * inch])
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9.5),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT_GRAY]),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)

    story.append(Paragraph(
        f"Neighborhood Snapshot — {deal.get('neighborhood', '')}", s["h2"]))
    yoy = deal.get("market_trend_yoy_pct")
    yoy_color = (GREEN if yoy and yoy > 0 else RED if yoy and yoy < -3 else YELLOW)
    yoy_str = fmt_pct(yoy, signed=True) if yoy is not None else "—"
    nbhd = [
        ["YoY Price Trend", yoy_str],
        ["Median DOM (Days on Market)", f"{deal.get('median_dom','?')} days"],
        ["Crime Rating", deal.get("crime_rating", "—")],
        ["School Rating", deal.get("school_rating", "—")],
        ["ARV Confidence", deal.get("arv_confidence", "Medium")],
    ]
    t = Table(nbhd, colWidths=[2.0 * inch, 4.0 * inch])
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9.5),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold", 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, BLUE_LIGHT]),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (1, 0), (1, 0), yoy_color),
        ("TEXTCOLOR", (1, 0), (1, 0), WHITE),
        ("FONT", (1, 0), (1, 0), "Helvetica-Bold", 10),
    ]
    t.setStyle(TableStyle(style))
    story.append(t)

    if deal.get("key_findings"):
        story.append(Paragraph("Key Findings", s["h3"]))
        for f in deal["key_findings"]:
            story.append(Paragraph(f"&bull; {f}", s["bullet"]))

    story.append(PageBreak())

    # ===== PAGE 3: COMPS + ARV =====
    story.append(Paragraph("Comparable Sales & ARV", s["h1"]))
    if deal.get("comps"):
        comp_rows = [["Address", "Bd/Ba", "SqFt", "Sale Price", "$/SqFt", "Date"]]
        for c in deal["comps"]:
            ppsf = c["price"] / c["sqft"] if c.get("sqft") and c.get("price") else None
            comp_rows.append([
                c.get("address", "—"),
                f"{c.get('beds','?')}/{c.get('baths','?')}",
                f"{c.get('sqft',0):,}" if c.get("sqft") else "—",
                fmt_money(c.get("price")),
                f"${ppsf:.0f}" if ppsf else "—",
                c.get("date", "—"),
            ])
        t = Table(comp_rows, colWidths=[1.9 * inch, 0.7 * inch, 0.7 * inch,
                                          1.2 * inch, 0.8 * inch, 0.7 * inch])
        style = base_table_style(len(comp_rows))
        style.append(("ALIGN", (1, 0), (-1, -1), "CENTER"))
        style.append(("FONT", (0, 1), (-1, -1), "Helvetica", 8.5))
        t.setStyle(TableStyle(style))
        story.append(t)
    else:
        story.append(Paragraph("<i>No comparable sales recorded.</i>", s["body"]))

    story.append(Paragraph("ARV Estimate Range", s["h3"]))
    arv_rows = [
        ["Scenario", "ARV", "Notes"],
        ["Conservative", fmt_money(deal.get("arv_low") or int(deal["arv_base"] * 0.92)),
         "Soft market or below-mid finish"],
        ["Base Case", fmt_money(deal["arv_base"]), "Working estimate"],
        ["Aggressive", fmt_money(deal.get("arv_high") or int(deal["arv_base"] * 1.08)),
         "Peak market / premium finish"],
    ]
    t = Table(arv_rows, colWidths=[1.8 * inch, 1.4 * inch, 3.4 * inch])
    style = base_table_style(len(arv_rows))
    style += [
        ("FONT", (0, 2), (-1, 2), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 2), (-1, 2), BLUE_LIGHT),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
    ]
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(PageBreak())

    # ===== PAGE 4: REHAB =====
    story.append(Paragraph("Rehab Budget", s["h1"]))
    if deal.get("rehab_items"):
        items = [["Category", "Amount", "Notes"]]
        for item in deal["rehab_items"]:
            items.append([
                item.get("category", "—"),
                fmt_money(item.get("amount", 0)),
                item.get("notes", ""),
            ])
        items.append(["TOTAL (with contingency)", fmt_money(deal["rehab_base"]), ""])
        t = Table(items, colWidths=[2.0 * inch, 1.2 * inch, 3.4 * inch])
        style = base_table_style(len(items))
        style.append(("ALIGN", (1, 0), (1, -1), "RIGHT"))
        # Total row
        ti = len(items) - 1
        style += [
            ("BACKGROUND", (0, ti), (-1, ti), NAVY),
            ("TEXTCOLOR", (0, ti), (-1, ti), WHITE),
            ("FONT", (0, ti), (-1, ti), "Helvetica-Bold", 10),
        ]
        t.setStyle(TableStyle(style))
        story.append(t)
    else:
        story.append(Paragraph(
            f"Total rehab budget: <b>{fmt_money(deal['rehab_base'])}</b> "
            f"(scope: {deal.get('rehab_scope', 'Mid-level')})", s["body"]))

    story.append(Paragraph("Rehab Range", s["h3"]))
    rehab_range = [
        ["Scope", "Estimate"],
        ["Light cosmetic", fmt_money(deal.get("rehab_low") or int(deal["rehab_base"] * 0.6))],
        ["Mid-level (base)", fmt_money(deal["rehab_base"])],
        ["Full gut", fmt_money(deal.get("rehab_high") or int(deal["rehab_base"] * 1.6))],
    ]
    t = Table(rehab_range, colWidths=[2.5 * inch, 2.0 * inch])
    style = base_table_style(len(rehab_range))
    style.append(("ALIGN", (1, 0), (1, -1), "CENTER"))
    style += [
        ("FONT", (0, 2), (-1, 2), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 2), (-1, 2), BLUE_LIGHT),
    ]
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(PageBreak())

    # ===== PAGE 5: P&L + SCENARIOS =====
    story.append(Paragraph("Full P&L Breakdown", s["h1"]))
    pp = deal["purchase_price"]
    rehab = deal["rehab_base"]
    pnl = [
        ["Line Item", "Amount"],
        ["Purchase Price", fmt_money(pp)],
        ["Closing Costs (2%)", fmt_money(m["closing"])],
        ["Total Acquisition", fmt_money(m["acquisition"])],
        ["Rehab (incl. contingency)", fmt_money(rehab)],
        [f"Holding Costs ({deal.get('holding_months',5)} mo)", fmt_money(m["holding"])],
        [f"Selling Costs ({deal.get('selling_cost_pct',8)}% of ARV)", fmt_money(m["selling"])],
        ["TOTAL ALL-IN", fmt_money(m["all_in"])],
        ["Sale Price (ARV)", fmt_money(deal["arv_base"])],
        ["NET PROFIT", fmt_money(m["net_profit"], signed=True)],
        ["ROI", f"{m['roi']:.1f}%"],
        ["Annualized ROI", f"{m['annualized_roi']:.0f}%"],
        ["Profit Margin", f"{m['margin']:.1f}%"],
    ]
    t = Table(pnl, colWidths=[4.0 * inch, 2.0 * inch])
    style = base_table_style(len(pnl), alt_rows=False)
    style.append(("ALIGN", (1, 0), (1, -1), "RIGHT"))
    # Subtotals & highlights
    style.append(("LINEABOVE", (0, 3), (-1, 3), 0.5, NAVY))
    style += [
        ("FONT", (0, 3), (-1, 3), "Helvetica-Bold", 9.5),
        ("BACKGROUND", (0, 3), (-1, 3), LIGHT_GRAY),
        # All-in
        ("FONT", (0, 7), (-1, 7), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 7), (-1, 7), LIGHT_GRAY),
        ("LINEABOVE", (0, 7), (-1, 7), 1, NAVY),
        # Sale price
        ("FONT", (0, 8), (-1, 8), "Helvetica-Bold", 10),
        ("BACKGROUND", (0, 8), (-1, 8), GREEN_LIGHT),
        # Profit
        ("FONT", (0, 9), (-1, 9), "Helvetica-Bold", 12),
        ("BACKGROUND", (0, 9), (-1, 9),
         GREEN if m["net_profit"] > 5000 else (RED if m["net_profit"] < 0 else YELLOW)),
        ("TEXTCOLOR", (0, 9), (-1, 9), WHITE),
        # Returns
        ("FONT", (0, 10), (-1, 12), "Helvetica-Bold", 9.5),
        ("BACKGROUND", (0, 10), (-1, 12), ORANGE_LIGHT),
    ]
    t.setStyle(TableStyle(style))
    story.append(t)

    # Scenarios
    story.append(Paragraph("Scenario Analysis", s["h2"]))
    sc_rows = [["Scenario", "ARV", "Rehab", "Hold", "Net Profit", "ROI"]]
    for sc in m["scenarios"]:
        sc_rows.append([
            sc["name"],
            fmt_money(sc["arv"]),
            fmt_money(sc["rehab"]),
            f"{sc['hold']} mo",
            fmt_money(sc["net"], signed=True),
            f"{sc['roi']:.1f}%",
        ])
    t = Table(sc_rows, colWidths=[1.4 * inch, 1.0 * inch, 1.0 * inch,
                                    0.7 * inch, 1.2 * inch, 0.7 * inch])
    style = base_table_style(len(sc_rows))
    style.append(("ALIGN", (1, 0), (-1, -1), "CENTER"))
    # Color the profit column
    for i, sc in enumerate(m["scenarios"], 1):
        if sc["net"] > 5000:
            style.append(("BACKGROUND", (-2, i), (-1, i), GREEN))
            style.append(("TEXTCOLOR", (-2, i), (-1, i), WHITE))
            style.append(("FONT", (-2, i), (-1, i), "Helvetica-Bold", 9))
        elif sc["net"] < 0:
            style.append(("BACKGROUND", (-2, i), (-1, i), RED))
            style.append(("TEXTCOLOR", (-2, i), (-1, i), WHITE))
            style.append(("FONT", (-2, i), (-1, i), "Helvetica-Bold", 9))
        else:
            style.append(("BACKGROUND", (-2, i), (-1, i), YELLOW))
            style.append(("FONT", (-2, i), (-1, i), "Helvetica-Bold", 9))
    # Highlight base row
    style.append(("FONT", (0, 2), (-3, 2), "Helvetica-Bold", 9))
    style.append(("BACKGROUND", (0, 2), (-3, 2), BLUE_LIGHT))
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(PageBreak())

    # ===== PAGE 6: MAX PURCHASE BACK-SOLVER + 70% RULE =====
    story.append(Paragraph("Max Purchase Price Calculator", s["h1"]))
    story.append(Paragraph(
        "Given the current ARV estimate, rehab budget, and selling costs, "
        "this back-solver tells you the maximum purchase price you should pay to hit "
        "different profit margin targets. Use this for negotiating future similar deals.",
        s["body"]))

    bs_rows = [["Target Profit Margin", "Max Purchase Price"]]
    for b in m["backsolve"]:
        bs_rows.append([f"{b['target_margin']}% of ARV", fmt_money(b["max_purchase"])])
    t = Table(bs_rows, colWidths=[2.5 * inch, 2.5 * inch])
    style = base_table_style(len(bs_rows))
    style.append(("ALIGN", (1, 0), (1, -1), "RIGHT"))
    style.append(("FONT", (0, 1), (-1, -1), "Helvetica-Bold", 10))
    # Highlight 15% row
    style.append(("BACKGROUND", (0, 2), (-1, 2), BLUE_LIGHT))
    t.setStyle(TableStyle(style))
    story.append(t)

    actual_vs_15 = m["backsolve"][1]["max_purchase"] - deal["purchase_price"]
    actual_color = GREEN if actual_vs_15 >= 0 else RED
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"<b>You paid {fmt_money(deal['purchase_price'])}.</b> "
        f"At a 15% target margin, the max you should have paid was "
        f"<b>{fmt_money(m['backsolve'][1]['max_purchase'])}</b> — "
        f"a difference of "
        f'<font color="#{actual_color.hexval()[2:]}"><b>{fmt_money(actual_vs_15, signed=True)}</b></font>.',
        s["body"]))

    # 70% Rule
    story.append(Paragraph("70% Rule Analysis", s["h2"]))
    rule_color = GREEN if m["rule_70_pass"] else RED
    rule_status = "PASS" if m["rule_70_pass"] else "FAIL"
    rule_rows = [
        ["Status", rule_status],
        ["Purchase + Rehab", fmt_money(deal["purchase_price"] + deal["rehab_base"])],
        ["70% of ARV", fmt_money(deal["arv_base"] * 0.70)],
        ["Max Purchase (70% rule)", fmt_money(m["max_purchase_70"])],
        ["Your Position vs Max", fmt_money(-m["rule_70_overage"], signed=True) +
         (" under" if m["rule_70_pass"] else " over")],
        ["Purchase + Rehab as % of ARV", f"{m['rule_70_pct_of_arv']:.0f}%"],
    ]
    t = kv_table(rule_rows, [2.7 * inch, 3.0 * inch], hilite_idx=0, hilite_color=rule_color)
    story.append(t)

    story.append(PageBreak())

    # ===== PAGE 7: FLIP vs RENT vs BRRRR =====
    story.append(Paragraph("Strategy Comparison — Flip vs Rent vs BRRRR", s["h1"]))

    if m.get("flip_to_rent_alert"):
        alert = Table([[Paragraph(
            "<b>RECOMMENDATION: Consider Rental Conversion</b><br/>"
            f"Cap rate ({m['rent']['cap_rate']:.1f}%) materially exceeds flip ROI ({m['roi']:.1f}%). "
            "The long-term hold likely produces better risk-adjusted returns than the flip.",
            s["alert_box"])]], colWidths=[doc.width])
        alert.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ORANGE),
            ("TOPPADDING", (0, 0), (-1, -1), 10),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ]))
        story.append(alert)
        story.append(Spacer(1, 10))

    strat = [
        ["Metric", "FLIP", "RENT (hold)", "BRRRR (refi & hold)"],
        ["Up-front capital",
         fmt_money(m["acquisition"] + deal["rehab_base"]),
         fmt_money(m["acquisition"] + deal["rehab_base"]),
         fmt_money(m["acquisition"] + deal["rehab_base"])],
        ["One-time profit",
         fmt_money(m["net_profit"], signed=True), "—", "—"],
        ["Monthly cash flow", "—",
         fmt_money(m["rent"]["monthly_net"], signed=True),
         fmt_money(m["brrrr"]["monthly_cash_flow"], signed=True)],
        ["Annual NOI / cash flow",
         "—",
         fmt_money(m["rent"]["annual_noi"], signed=True),
         fmt_money(m["brrrr"]["annual_cash_flow"], signed=True)],
        ["Cap rate", "—", f"{m['rent']['cap_rate']:.1f}%", "—"],
        ["Capital recovered at refi", "—", "—", fmt_money(m["brrrr"]["refi_value"])],
        ["Capital left in deal",
         fmt_money(m["all_in"] - deal["arv_base"]) + " (loss/gain on sale)",
         fmt_money(m["acquisition"] + deal["rehab_base"]),
         fmt_money(m["brrrr"]["capital_left_in"])],
        ["Time horizon",
         f"{deal.get('holding_months',5)} months",
         "Indefinite (5+ years)",
         "Indefinite + cash out at refi"],
    ]
    t = Table(strat, colWidths=[1.7 * inch, 1.5 * inch, 1.5 * inch, 1.8 * inch])
    style = base_table_style(len(strat))
    style.append(("ALIGN", (1, 0), (-1, -1), "CENTER"))
    style.append(("FONT", (0, 1), (0, -1), "Helvetica-Bold", 9))
    style.append(("TEXTCOLOR", (0, 1), (0, -1), NAVY))
    # Headers per strategy
    style += [
        ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#1565C0")),
        ("BACKGROUND", (2, 0), (2, 0), colors.HexColor("#2E7D32")),
        ("BACKGROUND", (3, 0), (3, 0), colors.HexColor("#6A1B9A")),
        ("TEXTCOLOR", (1, 0), (-1, 0), WHITE),
    ]
    t.setStyle(TableStyle(style))
    story.append(t)

    # Rent expense breakdown
    story.append(Paragraph("Rental Expense Detail", s["h3"]))
    exp = m["rent"]["opex_breakdown"]
    rent_exp = [
        ["Monthly Gross Rent", fmt_money(m["rent"]["monthly_gross"])],
        ["  Vacancy Allowance", fmt_money(-exp["vacancy_monthly"], signed=True)],
        ["  Property Taxes", fmt_money(-exp["taxes"], signed=True)],
        ["  Insurance", fmt_money(-exp["insurance"], signed=True)],
        ["  HOA", fmt_money(-exp["hoa"], signed=True)],
        ["  Maintenance", fmt_money(-exp["maintenance"], signed=True)],
        ["  Property Management", fmt_money(-exp["management"], signed=True)],
        ["Monthly Net (before debt)", fmt_money(m["rent"]["monthly_net"], signed=True)],
    ]
    t = Table(rent_exp, colWidths=[3.4 * inch, 2.4 * inch])
    n = len(rent_exp)
    style = [
        ("FONT", (0, 0), (-1, -1), "Helvetica", 9.5),
        ("FONT", (0, 0), (0, 0), "Helvetica-Bold", 10),
        ("FONT", (0, -1), (-1, -1), "Helvetica-Bold", 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [WHITE, LIGHT_GRAY]),
        ("BACKGROUND", (0, 0), (-1, 0), BLUE_LIGHT),
        ("BACKGROUND", (0, -1), (-1, -1),
         GREEN if m["rent"]["monthly_net"] > 100 else (RED if m["rent"]["monthly_net"] < 0 else YELLOW)),
        ("TEXTCOLOR", (0, -1), (-1, -1), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, BORDER_GRAY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(PageBreak())

    # ===== PAGE 8: FINANCING =====
    story.append(Paragraph("Financing & Selected Scenario", s["h1"]))

    # If the user picked specific financing options, show them prominently
    scenario = deal.get("scenario") or {}
    if scenario:
        strategy_label = {
            "flip": "Fix & Flip",
            "brrrr": "BRRRR (refi & hold)",
            "hold": "Buy & Hold (rental)",
            "wholesale": "Wholesale (assign)",
        }.get(scenario.get("strategy"), scenario.get("strategy", "—"))
        fin_method_label = {
            "cash": "Cash (no financing)",
            "hard_money": "Hard Money Loan",
            "private": "Private Lender",
            "conventional": "Conventional",
            "heloc": "HELOC",
        }.get(scenario.get("financing_method"), scenario.get("financing_method", "—"))

        sc_rows = [
            ["Strategy", strategy_label],
            ["Financing Method", fin_method_label],
        ]
        if scenario.get("financing_method") != "cash":
            sc_rows += [
                ["Loan Amount",
                 fmt_money(scenario.get("loan_amount", 0))],
                ["Loan-to-value", f"{scenario.get('loan_ltv_pct', 0)}% of purchase"],
                ["Interest Rate", f"{scenario.get('interest_rate_pct', 0)}% annual"],
                ["Origination Fee",
                 f"{scenario.get('origination_pct', 0)}% "
                 f"({fmt_money(scenario.get('points_paid', 0))})"],
                ["Loan Term",
                 f"{scenario.get('loan_term_months', 0)} months"],
                ["Rehab Financed?",
                 "Yes" if scenario.get("rehab_financed") == "yes" else "No"],
                ["Total Financing Cost",
                 fmt_money(scenario.get("financing_cost", 0))],
                ["Cash Required Up-Front",
                 fmt_money(scenario.get("cash_needed", 0))],
            ]
        sc_rows += [
            ["Purchase Closing %",
             f"{scenario.get('purchase_closing_pct', 2)}%"],
            ["Due Diligence Fees",
             fmt_money(scenario.get("due_diligence_fees", 0))],
            ["Other Fees",
             fmt_money(scenario.get("other_fees", 0))],
        ]
        t = kv_table(sc_rows, [2.5 * inch, 3.2 * inch])
        story.append(t)
        story.append(Spacer(1, 12))

    story.append(Paragraph("Reference: financing options comparison", s["h2"]))
    story.append(Paragraph(
        "Generic comparison of typical financing options at this deal size:",
        s["body"]))

    fin_rows = [["Option", "Down/Equity", "Rate", "~6mo Cost", "Cash Needed", "Feasibility"]]
    for f in m["financing"]:
        fin_rows.append([
            f["option"],
            f["down"],
            f["rate"],
            fmt_money(f["cost_6mo"]),
            fmt_money(f["total_capital_needed"]),
            f["feasibility"],
        ])
    t = Table(fin_rows, colWidths=[1.4 * inch, 0.8 * inch, 1.0 * inch,
                                     0.8 * inch, 1.0 * inch, 1.7 * inch])
    style = base_table_style(len(fin_rows))
    style.append(("ALIGN", (1, 0), (-2, -1), "CENTER"))
    style.append(("FONT", (0, 1), (0, -1), "Helvetica-Bold", 9))
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(Paragraph(
        "<b>Recommendation:</b> For deals under $50K purchase price, cash is typically optimal "
        "(hard money minimums are usually $75K loan). For deals $100K+, hard money or private "
        "lender financing leverages capital and allows multiple concurrent projects. HELOC is "
        "ideal if borrower has equity in another property.", s["body"]))

    # Risk Factors
    if deal.get("risks"):
        story.append(Paragraph("Risk Factors", s["h2"]))
        rr = [["#", "Risk", "Severity"]]
        for i, r in enumerate(deal["risks"], 1):
            rr.append([str(i), r.get("text", "—"), r.get("severity", "MEDIUM")])
        t = Table(rr, colWidths=[0.35 * inch, 4.65 * inch, 1.2 * inch])
        style = base_table_style(len(rr))
        style.append(("ALIGN", (0, 0), (0, -1), "CENTER"))
        style.append(("ALIGN", (2, 0), (2, -1), "CENTER"))
        for i, r in enumerate(deal["risks"], 1):
            sc = severity_color(r.get("severity", ""))
            style.append(("BACKGROUND", (2, i), (2, i), sc))
            style.append(("FONT", (2, i), (2, i), "Helvetica-Bold", 8.5))
            if sc in (RED, ORANGE, colors.HexColor("#E53935")):
                style.append(("TEXTCOLOR", (2, i), (2, i), WHITE))
            else:
                style.append(("TEXTCOLOR", (2, i), (2, i), NAVY))
        t.setStyle(TableStyle(style))
        story.append(t)

    story.append(PageBreak())

    # ===== PAGE 9: BOTTOM LINE =====
    story.append(Paragraph("Bottom Line & Recommendation", s["h1"]))

    rec_color_hex = signal_color(deal.get("signal", "")).hexval()[2:]
    story.append(Paragraph(
        f'<font size="13"><b>Recommended Strategy: '
        f'<font color="#{rec_color_hex}">{" / ".join(m["recommended_strategy"])}</font></b></font>',
        s["body_l"]))
    story.append(Spacer(1, 8))

    if deal.get("notes"):
        story.append(Paragraph("Notes", s["h3"]))
        story.append(Paragraph(deal["notes"], s["body"]))

    # Summary table at bottom
    summary_final = [
        ["Final Verdict", deal.get("signal", "—")],
        ["Flip Score", f"{deal.get('score','?')}/100 ({deal.get('grade','?')})"],
        ["Best Strategy", " / ".join(m["recommended_strategy"])],
        ["Flip Net Profit", fmt_money(m["net_profit"], signed=True)],
        ["Rental Cap Rate", f"{m['rent']['cap_rate']:.1f}%"],
        ["BRRRR Monthly CF", fmt_money(m["brrrr"]["monthly_cash_flow"], signed=True)],
        ["Status", deal.get("status", "evaluating").upper()],
    ]
    t = kv_table(summary_final, [2.7 * inch, 3.2 * inch])
    story.append(t)

    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph(
        "<b>DISCLAIMER:</b> For educational/research purposes only. Not financial or "
        "investment advice. All estimates are AI-generated approximations based on publicly "
        "available data. Rehab costs, ARV estimates, and timelines are approximations. "
        "Actual results may vary significantly. Always verify with licensed real estate "
        "professionals.", s["disclaimer"]))

    doc.build(story, onFirstPage=make_footer("Flip Board — Deal Analysis"),
              onLaterPages=make_footer("Flip Board — Deal Analysis"))


# ============== COMPARISON PDF ==============

def build_comparison_pdf(board, all_metrics, output_path):
    """Generate multi-deal comparison PDF."""
    doc = SimpleDocTemplate(
        output_path, pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="Flip Board — Multi-Deal Comparison",
    )
    s = build_styles()
    story = []

    # ===== COVER =====
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("FLIP BOARD — COMPARISON REPORT", ParagraphStyle(
        "T", fontName="Helvetica-Bold", fontSize=11, alignment=TA_CENTER,
        textColor=colors.HexColor("#888888"), spaceAfter=10, letterSpacing=2)))
    story.append(Paragraph("Side-by-Side Deal Analysis", s["cover_addr"]))
    story.append(Paragraph(
        f'<para align="center"><font size="10" color="#666666">'
        f'{len(board["deals"])} deals on board &mdash; Report date: '
        f'{datetime.now().strftime("%B %d, %Y")}</font></para>', s["body_c"]))
    story.append(Spacer(1, 0.3 * inch))

    # Mini-gauges side by side for top deals
    deals_sorted = sorted(zip(board["deals"], all_metrics),
                          key=lambda x: x[0].get("score", 0), reverse=True)

    gauge_row = []
    label_row = []
    for d, _ in deals_sorted[:4]:
        gauge_row.append(MiniGauge(d.get("score", 0), d.get("grade", "?"), size=1.3 * inch))
        addr_short = d["address"].split(",")[0]
        label_row.append(Paragraph(
            f'<para align="center"><b><font size="9">{addr_short}</font></b><br/>'
            f'<font size="8" color="#666666">{d.get("signal", "—")}</font></para>',
            s["body_c"]))

    while len(gauge_row) < 4:
        gauge_row.append("")
        label_row.append("")

    t = Table([gauge_row, label_row], colWidths=[doc.width / 4] * 4)
    t.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, 1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, 1), 6),
    ]))
    story.append(t)

    story.append(PageBreak())

    # ===== RANKING TABLE =====
    story.append(Paragraph("Deal Ranking — Sorted by Flip Score", s["h1"]))
    rank_rows = [["Rank", "Address", "Score/Grade", "Signal", "Net Profit",
                  "ROI", "Cap Rate", "Best Strategy"]]
    for i, (d, m) in enumerate(deals_sorted, 1):
        addr = d["address"][:38] + "..." if len(d["address"]) > 40 else d["address"]
        rank_rows.append([
            str(i),
            addr,
            f"{d.get('score','?')} ({d.get('grade','?')})",
            d.get("signal", "—"),
            fmt_money(m["net_profit"], signed=True),
            f"{m['roi']:.1f}%",
            f"{m['rent']['cap_rate']:.1f}%",
            " / ".join(m["recommended_strategy"])[:25],
        ])
    t = Table(rank_rows, colWidths=[0.5 * inch, 3.0 * inch, 1.0 * inch,
                                      1.4 * inch, 1.2 * inch, 0.8 * inch,
                                      0.9 * inch, 1.7 * inch])
    style = base_table_style(len(rank_rows))
    style.append(("ALIGN", (0, 0), (0, -1), "CENTER"))
    style.append(("ALIGN", (2, 0), (-1, -1), "CENTER"))
    # Color profit column
    for i, (d, m) in enumerate(deals_sorted, 1):
        if m["net_profit"] > 5000:
            style.append(("BACKGROUND", (4, i), (4, i), GREEN_LIGHT))
        elif m["net_profit"] < 0:
            style.append(("BACKGROUND", (4, i), (4, i), RED_LIGHT))
        # Color score column by grade
        sc = score_color(d.get("score", 0))
        style.append(("BACKGROUND", (2, i), (2, i), sc))
        style.append(("TEXTCOLOR", (2, i), (2, i), WHITE))
        style.append(("FONT", (2, i), (2, i), "Helvetica-Bold", 9))
    t.setStyle(TableStyle(style))
    story.append(t)

    # ===== SIDE-BY-SIDE METRICS =====
    story.append(Paragraph("Side-by-Side Key Metrics", s["h2"]))
    metric_rows = [["Metric"] + [d["address"].split(",")[0][:20] for d, _ in deals_sorted[:5]]]
    metric_defs = [
        ("Purchase Price", lambda d, m: fmt_money(d["purchase_price"])),
        ("ARV (Base)", lambda d, m: fmt_money(d["arv_base"])),
        ("Rehab Budget", lambda d, m: fmt_money(d["rehab_base"])),
        ("All-In Cost", lambda d, m: fmt_money(m["all_in"])),
        ("Net Profit", lambda d, m: fmt_money(m["net_profit"], signed=True)),
        ("ROI", lambda d, m: f"{m['roi']:.1f}%"),
        ("Annualized ROI", lambda d, m: f"{m['annualized_roi']:.0f}%"),
        ("70% Rule", lambda d, m: "PASS" if m["rule_70_pass"] else "FAIL"),
        ("Beds/Baths/Sqft", lambda d, m:
            f"{d.get('beds','?')}/{d.get('baths','?')}/{d.get('sqft','?')}"),
        ("Year Built", lambda d, m: str(d.get("year_built", "—"))),
        ("YoY Market Trend", lambda d, m: fmt_pct(d.get("market_trend_yoy_pct"), signed=True)),
        ("Days on Market", lambda d, m: f"{d.get('median_dom','?')} days"),
        ("Rental Cap Rate", lambda d, m: f"{m['rent']['cap_rate']:.1f}%"),
        ("BRRRR Monthly CF", lambda d, m: fmt_money(m["brrrr"]["monthly_cash_flow"], signed=True)),
        ("Status", lambda d, m: d.get("status", "evaluating").upper()),
    ]
    for label, fn in metric_defs:
        row = [label]
        for d, m in deals_sorted[:5]:
            row.append(fn(d, m))
        metric_rows.append(row)

    n_cols = len(metric_rows[0])
    col_widths = [1.7 * inch] + [(doc.width - 1.7 * inch) / max(1, n_cols - 1)] * (n_cols - 1)
    t = Table(metric_rows, colWidths=col_widths)
    style = base_table_style(len(metric_rows))
    style.append(("ALIGN", (1, 0), (-1, -1), "CENTER"))
    style.append(("FONT", (0, 1), (0, -1), "Helvetica-Bold", 8.5))
    style.append(("FONT", (0, 0), (-1, -1), "Helvetica", 8.5))
    style.append(("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9))
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(PageBreak())

    # ===== BEST / WORST CALL-OUTS =====
    story.append(Paragraph("Best & Worst Deals", s["h1"]))

    best_d, best_m = deals_sorted[0]
    worst_d, worst_m = deals_sorted[-1]

    cells = []
    for label, color_bg, d, m in [
        ("BEST DEAL", GREEN, best_d, best_m),
        ("WEAKEST DEAL", RED, worst_d, worst_m),
    ]:
        rows = [
            [Paragraph(f'<font color="white"><b>{label}</b></font>',
                       ParagraphStyle("X", fontSize=13, fontName="Helvetica-Bold",
                                       textColor=WHITE, alignment=TA_CENTER))],
            [Paragraph(f"<b>{d['address']}</b>", s["body_c"])],
            [Paragraph(
                f"Score <b>{d.get('score','?')}/100</b> ({d.get('grade','?')})<br/>"
                f"Net Profit: <b>{fmt_money(m['net_profit'], signed=True)}</b><br/>"
                f"ROI: <b>{m['roi']:.1f}%</b><br/>"
                f"Cap Rate: <b>{m['rent']['cap_rate']:.1f}%</b><br/>"
                f"Strategy: <b>{' / '.join(m['recommended_strategy'])}</b>",
                s["body_c"])],
        ]
        t = Table(rows, colWidths=[doc.width / 2 - 0.3 * inch])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), color_bg),
            ("BACKGROUND", (0, 1), (-1, -1), WHITE),
            ("BOX", (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ("TOPPADDING", (0, 0), (-1, -1), 12),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ("LEFTPADDING", (0, 0), (-1, -1), 14),
            ("RIGHTPADDING", (0, 0), (-1, -1), 14),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        cells.append(t)

    bw = Table([cells], colWidths=[doc.width / 2, doc.width / 2])
    bw.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(bw)

    # Aggregate stats
    story.append(Paragraph("Board Aggregate Statistics", s["h2"]))
    total_capital = sum(m["acquisition"] + d["rehab_base"]
                         for d, m in zip(board["deals"], all_metrics))
    total_profit = sum(m["net_profit"] for m in all_metrics)
    avg_roi = sum(m["roi"] for m in all_metrics) / len(all_metrics) if all_metrics else 0
    avg_cap = sum(m["rent"]["cap_rate"] for m in all_metrics) / len(all_metrics) if all_metrics else 0
    passing_70 = sum(1 for m in all_metrics if m["rule_70_pass"])

    agg_rows = [
        ["Total Deals on Board", str(len(board["deals"]))],
        ["Total Capital Deployed (if all flipped)", fmt_money(total_capital)],
        ["Aggregate Net Profit (if all flipped)", fmt_money(total_profit, signed=True)],
        ["Average ROI", f"{avg_roi:.1f}%"],
        ["Average Cap Rate", f"{avg_cap:.1f}%"],
        ["Deals Passing 70% Rule", f"{passing_70} / {len(all_metrics)}"],
    ]
    t = kv_table(agg_rows, [3.5 * inch, 3.0 * inch])
    story.append(t)

    story.append(PageBreak())

    # ===== STRATEGY MATRIX =====
    story.append(Paragraph("Strategy Recommendation Matrix", s["h1"]))
    story.append(Paragraph(
        "Which deal is best held as which strategy? This matrix surfaces conversion opportunities "
        "(flip-to-rent) where rental returns dominate flip returns.", s["body"]))

    sm_rows = [["Deal", "Flip ROI", "Cap Rate", "BRRRR CF/mo", "Recommended"]]
    for d, m in deals_sorted:
        flip_color = GREEN_LIGHT if m["roi"] >= 15 else (RED_LIGHT if m["roi"] < 5 else WHITE)
        rent_color = GREEN_LIGHT if m["rent"]["cap_rate"] >= 8 else (RED_LIGHT if m["rent"]["cap_rate"] < 5 else WHITE)
        addr = d["address"].split(",")[0][:32]
        sm_rows.append([
            addr,
            f"{m['roi']:.1f}%",
            f"{m['rent']['cap_rate']:.1f}%",
            fmt_money(m["brrrr"]["monthly_cash_flow"], signed=True),
            " / ".join(m["recommended_strategy"]),
        ])

    t = Table(sm_rows, colWidths=[2.5 * inch, 1.2 * inch, 1.2 * inch,
                                    1.4 * inch, 3.2 * inch])
    style = base_table_style(len(sm_rows))
    style.append(("ALIGN", (1, 0), (-2, -1), "CENTER"))
    style.append(("FONT", (0, 1), (0, -1), "Helvetica-Bold", 9))
    # Color individual cells based on values
    for i, (d, m) in enumerate(deals_sorted, 1):
        if m["roi"] >= 15:
            style.append(("BACKGROUND", (1, i), (1, i), GREEN_LIGHT))
        elif m["roi"] < 5:
            style.append(("BACKGROUND", (1, i), (1, i), RED_LIGHT))
        if m["rent"]["cap_rate"] >= 8:
            style.append(("BACKGROUND", (2, i), (2, i), GREEN_LIGHT))
        elif m["rent"]["cap_rate"] < 5:
            style.append(("BACKGROUND", (2, i), (2, i), RED_LIGHT))
        if m["brrrr"]["monthly_cash_flow"] >= 200:
            style.append(("BACKGROUND", (3, i), (3, i), GREEN_LIGHT))
        elif m["brrrr"]["monthly_cash_flow"] < 0:
            style.append(("BACKGROUND", (3, i), (3, i), RED_LIGHT))
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(Paragraph("Legend", s["h3"]))
    leg_rows = [
        ["Cell Color", "Meaning"],
        ["Green", "Strong performance on this dimension"],
        ["Light/White", "Acceptable / neutral"],
        ["Red", "Weak performance on this dimension"],
    ]
    t = Table(leg_rows, colWidths=[1.5 * inch, 4.0 * inch])
    style = base_table_style(len(leg_rows))
    style += [
        ("BACKGROUND", (0, 1), (0, 1), GREEN_LIGHT),
        ("BACKGROUND", (0, 3), (0, 3), RED_LIGHT),
    ]
    t.setStyle(TableStyle(style))
    story.append(t)

    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph(
        "<b>DISCLAIMER:</b> For educational/research purposes only. Not financial or "
        "investment advice. All estimates are AI-generated approximations.",
        s["disclaimer"]))

    doc.build(story,
              onFirstPage=make_footer_landscape("Flip Board — Comparison"),
              onLaterPages=make_footer_landscape("Flip Board — Comparison"))


# ============== MAIN ==============

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--board", required=True, help="Path to flip-board.json")
    p.add_argument("--deal", help="Deal ID for individual PDF")
    p.add_argument("--compare", action="store_true", help="Generate comparison PDF")
    p.add_argument("--out", required=True, help="Output PDF path")
    args = p.parse_args()

    with open(args.board, "r") as f:
        board = json.load(f)

    if args.compare:
        if not board.get("deals"):
            print("No deals on board.", file=sys.stderr)
            sys.exit(1)
        all_metrics = [compute_metrics(d) for d in board["deals"]]
        build_comparison_pdf(board, all_metrics, args.out)
        print(f"Comparison PDF: {args.out}")
        return

    if args.deal:
        deal = next((d for d in board["deals"] if d["id"] == args.deal), None)
        if not deal:
            print(f"Deal not found: {args.deal}", file=sys.stderr)
            print("Available IDs:", file=sys.stderr)
            for d in board["deals"]:
                print(f"  - {d['id']}", file=sys.stderr)
            sys.exit(1)
        m = compute_metrics(deal)
        build_deal_pdf(deal, m, args.out)
        print(f"Deal PDF: {args.out}")
        return

    print("Specify --deal <id> or --compare", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
