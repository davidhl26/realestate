"""Computational engine for deal metrics + scoring.

Mirrors the logic from ~/.claude/skills/flip-board/scripts/generate_flip_board_pdf.py
so the local app and the skill produce identical numbers.
"""


def compute_metrics(deal: dict) -> dict:
    """Compute all derived metrics from raw deal data."""
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

    m.update({
        "closing": closing,
        "acquisition": acq,
        "holding": holding,
        "selling": selling,
        "all_in": all_in,
        "net_profit": net,
        "roi": roi,
        "annualized_roi": annualized,
        "margin": margin,
    })

    # Scenarios
    def _scenario(arv_v, rehab_v, hold_v):
        sell_v = round(arv_v * sell_pct / 100)
        all_v = acq + rehab_v + (hold_v * hold_mo_cost) + sell_v
        net_v = arv_v - all_v
        inv_v = acq + rehab_v + hold_v * hold_mo_cost
        roi_v = (net_v / inv_v * 100) if inv_v > 0 else 0
        return {"arv": arv_v, "rehab": rehab_v, "hold": hold_v,
                "net": net_v, "roi": roi_v}

    best = _scenario(arv_high, rehab_low, max(3, hold_mo - 1))
    base = _scenario(arv, rehab, hold_mo)
    worst = _scenario(arv_low, rehab_high, hold_mo + 2)
    m["scenarios"] = [
        {"name": "Best Case", **best},
        {"name": "Base Case", **base},
        {"name": "Worst Case", **worst},
    ]

    # 70% rule
    max_p_70 = (arv * 0.70) - rehab
    m["max_purchase_70"] = max_p_70
    m["rule_70_pct_of_arv"] = ((pp + rehab) / arv * 100) if arv > 0 else 0
    m["rule_70_pass"] = pp <= max_p_70
    m["rule_70_overage"] = pp - max_p_70

    # Back-solver
    backsolve = []
    for tgt in [10, 15, 20, 25]:
        target_profit = arv * (tgt / 100)
        max_acq = arv - target_profit - rehab - holding - selling
        max_pur = max_acq / 1.02 if max_acq > 0 else 0
        backsolve.append({"target_margin": tgt, "max_purchase": int(max_pur)})
    m["backsolve"] = backsolve

    # Rental
    rent = deal.get("estimated_rent", 0) or 0
    taxes_m = deal.get("monthly_taxes", 0) or 0
    ins_m = deal.get("monthly_insurance", 0) or 0
    hoa_m = deal.get("monthly_hoa", 0) or 0
    maint_m = deal.get("monthly_maintenance", 0) or 0
    mgmt_m = deal.get("monthly_mgmt", 0) or round(rent * 0.10)
    vac_pct = deal.get("vacancy_pct", 8) or 8

    gross_yr = rent * 12
    vac_loss = gross_yr * vac_pct / 100
    opex_yr = 12 * (taxes_m + ins_m + hoa_m + maint_m + mgmt_m)
    noi = gross_yr - vac_loss - opex_yr
    total_cap = acq + rehab
    cap_rate = (noi / total_cap * 100) if total_cap > 0 else 0
    grm = (total_cap / gross_yr) if gross_yr > 0 else 0
    monthly_net = (rent - (rent * vac_pct / 100) - taxes_m - ins_m -
                   hoa_m - maint_m - mgmt_m)

    m["rent"] = {
        "monthly_gross": rent,
        "monthly_net": monthly_net,
        "annual_noi": noi,
        "cap_rate": cap_rate,
        "coc": cap_rate,
        "grm": grm,
        "opex_breakdown": {
            "taxes": taxes_m, "insurance": ins_m, "hoa": hoa_m,
            "maintenance": maint_m, "management": mgmt_m,
            "vacancy_monthly": rent * vac_pct / 100,
        },
    }

    # BRRRR
    refi = arv * 0.70
    capital_left = total_cap - refi
    monthly_pi = refi * 0.00699  # 7.5% 30yr
    monthly_piti = monthly_pi + taxes_m + ins_m + hoa_m
    brrrr_cf = rent - (monthly_piti + maint_m + mgmt_m + rent * vac_pct / 100)
    m["brrrr"] = {
        "refi_value": refi,
        "capital_left_in": capital_left,
        "capital_recovered": refi,
        "monthly_PI": monthly_pi,
        "monthly_PITI": monthly_piti,
        "monthly_cash_flow": brrrr_cf,
        "annual_cash_flow": brrrr_cf * 12,
    }

    # ===== USER-SELECTED FINANCING SCENARIO =====
    # The deal can carry its own financing config (set via UI inline edits).
    # When set, we compute the real financing cost and ROI on cash invested.
    fin = deal.get("financing") or {}
    method = (fin.get("method") or "cash").lower()
    ltv_pct = float(fin.get("ltv_pct") or 0)
    rate_pct = float(fin.get("interest_rate_pct") or 0)
    orig_pct = float(fin.get("origination_pct") or 0)
    # Misc. lender fees (processing, admin, junk fees) as a % of the loan —
    # separate from the origination points above.
    lender_fees_pct = float(fin.get("lender_fees_pct") or 0)
    term_mo = int(fin.get("term_months") or hold_mo or 6)
    rehab_fin = bool(fin.get("rehab_financed", True))

    if method == "cash":
        loan_amount = 0
        interest_cost = 0
        points_paid = 0
        lender_fees_paid = 0
        cash_for_purchase = pp
        cash_for_rehab = rehab
    else:
        loan_principal_pp = pp * (ltv_pct / 100) if ltv_pct else 0
        loan_principal_rehab = rehab if rehab_fin else 0
        loan_amount = loan_principal_pp + loan_principal_rehab
        interest_cost = loan_amount * (rate_pct / 100) * (term_mo / 12)
        points_paid = loan_amount * (orig_pct / 100)
        lender_fees_paid = loan_amount * (lender_fees_pct / 100)
        cash_for_purchase = pp - loan_principal_pp
        cash_for_rehab = rehab - loan_principal_rehab

    fin_total_cost = interest_cost + points_paid + lender_fees_paid
    cash_needed_up_front = (max(0, cash_for_purchase) + max(0, cash_for_rehab)
                            + closing + points_paid + lender_fees_paid)
    # All-in WITH financing cost (already in holding via separate; but here keep separate)
    all_in_with_financing = all_in + fin_total_cost
    net_with_financing = arv - all_in_with_financing
    roi_on_cash = (net_with_financing / cash_needed_up_front * 100) if cash_needed_up_front > 0 else 0
    roi_on_cash_annualized = roi_on_cash * (12 / hold_mo) if hold_mo > 0 else 0

    m["selected_financing"] = {
        "method": method,
        "ltv_pct": ltv_pct,
        "interest_rate_pct": rate_pct,
        "origination_pct": orig_pct,
        "lender_fees_pct": lender_fees_pct,
        "term_months": term_mo,
        "rehab_financed": rehab_fin,
        "loan_amount": round(loan_amount),
        "interest_cost": round(interest_cost),
        "points_paid": round(points_paid),
        "lender_fees_paid": round(lender_fees_paid),
        "total_financing_cost": round(fin_total_cost),
        "cash_needed_up_front": round(cash_needed_up_front),
        "net_profit_after_financing": round(net_with_financing),
        "roi_on_cash": roi_on_cash,
        "roi_on_cash_annualized": roi_on_cash_annualized,
        "all_in_with_financing": round(all_in_with_financing),
    }

    # Financing options
    m["financing"] = [
        {"option": "Cash", "down": "100%", "rate": "N/A",
         "cost_6mo": 0,
         "total_capital_needed": acq + rehab + holding,
         "feasibility": "Best for sub-$50K purchases"},
        {"option": "Hard Money", "down": "10-20%",
         "rate": "11-13% + 2-3 pts",
         "cost_6mo": int(acq * 0.07 + acq * 0.025),
         "total_capital_needed": int(acq * 0.15 + rehab),
         "feasibility": "Standard for flips $100K+"},
        {"option": "Private Lender", "down": "15-25%",
         "rate": "8-10%",
         "cost_6mo": int(acq * 0.045),
         "total_capital_needed": int(acq * 0.20 + rehab),
         "feasibility": "Best if relationship exists"},
        {"option": "HELOC", "down": "N/A", "rate": "9-10%",
         "cost_6mo": int(acq * 0.05),
         "total_capital_needed": 0,
         "feasibility": "Good fit for smaller deals"},
    ]

    # Strategy recommendation
    yoy = deal.get("market_trend_yoy_pct", 0) or 0
    recs = []
    if roi >= 20 and yoy >= -3:
        recs.append("FLIP")
    if cap_rate >= 8 and brrrr_cf >= 200:
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

    # Alert
    m["flip_to_rent_alert"] = cap_rate >= 9 and roi <= 12

    return m


def grade_and_signal(score: int) -> tuple:
    """CANONICAL score→(grade, signal) mapping. Single source of truth used by
    BOTH the per-deal scorer (compute_score) and the comparison engine
    (comparator.compute_flip_score) so a deal NEVER shows a different signal in
    the detail view vs the compare view."""
    score = max(0, min(100, int(round(score))))
    if score >= 85:
        return "A+", "SLAM DUNK"
    if score >= 70:
        return "A", "GOOD FLIP"
    if score >= 55:
        return "B", "POSSIBLE"
    if score >= 40:
        return "C", "RISKY"
    if score >= 25:
        return "D", "MARGINAL"
    return "F", "NO DEAL"


def score_exit_optionality(roi_pct: float, cap_rate_pct: float,
                            brrrr_cf: float, rule_70_pass: bool,
                            rule_70_overage: float) -> int:
    """CANONICAL exit-optionality score (0-5 pts). Counts how many of the four
    exit strategies are genuinely viable. Shared by compute_score and the
    comparator so both grade exits identically."""
    exits = 0
    if roi_pct > 5:               exits += 1   # flip works
    if cap_rate_pct >= 7:         exits += 1   # rental cap acceptable
    if brrrr_cf >= 100:           exits += 1   # BRRRR cash-flows
    if rule_70_pass or rule_70_overage < 15000:  exits += 1  # acquisition headroom
    if exits >= 4:   return 5
    if exits >= 2:   return 3
    if exits >= 1:   return 1
    return 0


def compute_score(deal: dict, m: dict) -> tuple:
    """Returns (score 0-100, grade A+ to F, signal). ROI here is in PERCENT
    (m['roi']). Exit-optionality and the grade/signal mapping are shared with
    the comparator to guarantee identical results everywhere."""
    score = 0

    # Margin & ROI (30 pts)  — m["roi"] is a percent
    roi = m["roi"]
    if roi >= 25:
        score += 30
    elif roi >= 15:
        score += 22
    elif roi >= 5:
        score += 14
    elif roi >= 0:
        score += 6
    # ARV confidence (15 pts)
    conf = (deal.get("arv_confidence") or "Medium").lower()
    score += {"high": 15, "medium": 10, "low": 5}.get(conf, 10)
    # 70% rule (15 pts)
    if m["rule_70_pass"]:
        score += 15
    elif m["rule_70_overage"] < 10000:
        score += 11
    elif m["rule_70_overage"] < 25000:
        score += 7
    else:
        score += 2
    # Market conditions (15 pts)
    yoy = deal.get("market_trend_yoy_pct", 0) or 0
    if yoy >= 5:
        score += 15
    elif yoy >= 0:
        score += 11
    elif yoy >= -5:
        score += 7
    else:
        score += 3
    # Rehab complexity (10 pts)
    scope = (deal.get("rehab_scope") or "Mid-level").lower()
    if "cosmetic" in scope or "light" in scope:
        score += 10
    elif "gut" in scope or "full" in scope or "heavy" in scope:
        score += 4
    elif "mid" in scope:
        score += 7
    else:
        score += 4
    # Neighborhood quality (10 pts) - crude proxy via rating
    crime = (deal.get("crime_rating") or "C").upper()
    crime_score = {"A": 10, "B": 8, "C": 5, "D": 2, "F": 0}
    score += crime_score.get(crime[0] if crime else "C", 5)
    # Exit optionality (5 pts) — rigorous 4-strategy check (shared w/ comparator)
    score += score_exit_optionality(
        roi_pct=roi,
        cap_rate_pct=m["rent"]["cap_rate"],
        brrrr_cf=m["brrrr"]["monthly_cash_flow"],
        rule_70_pass=m["rule_70_pass"],
        rule_70_overage=m["rule_70_overage"],
    )

    score = max(0, min(100, int(round(score))))
    grade, signal = grade_and_signal(score)
    return score, grade, signal


def recommended_max_offer(deal: dict, m: dict, default_margin: float = 15.0) -> dict:
    """THE one price not to exceed for this deal — the headline number.

    Auction-sourced deals use the auction max-bid math (5% buyer's premium +
    fixed closing); everything else back-solves the purchase price at the
    target margin (same cost model as compute_metrics). Returns
    {max_offer, basis, target_margin_pct, gap_vs_price} — max_offer is None
    when there's no usable ARV yet."""
    arv = deal.get("arv_base", 0) or 0
    if arv <= 0:
        return {"max_offer": None, "basis": None,
                "target_margin_pct": None, "gap_vs_price": None}
    rehab = deal.get("rehab_base", 0) or 0
    try:
        margin = float(deal.get("target_margin_pct") or default_margin)
    except (TypeError, ValueError):
        margin = float(default_margin)
    target_profit = arv * margin / 100.0
    selling = m.get("selling") or round(arv * ((deal.get("selling_cost_pct", 8) or 8) / 100))
    holding = m.get("holding") or 0

    is_auction = (deal.get("source") == "auction"
                  or "auction" in (deal.get("source_url") or "").lower())
    if is_auction:
        # bid*(1+premium) + closing = ARV - rehab - selling - holding - profit
        premium, closing = 0.05, 2500
        net = arv - rehab - selling - holding - closing - target_profit
        max_offer = max(0, net / (1 + premium))
        basis = "auction"
    else:
        # Mirror of the back-solver: acquisition = price*1.02 (closing ≈ 2%)
        max_acq = arv - target_profit - rehab - holding - selling
        max_offer = max(0, max_acq / 1.02)
        basis = "flip"

    pp = deal.get("purchase_price", 0) or 0
    return {"max_offer": int(round(max_offer)), "basis": basis,
            "target_margin_pct": margin,
            "gap_vs_price": int(round(max_offer - pp)) if pp else None}


import re as _re

# Description/notes keyword scan → (compiled regex, severity, label).
# severity: "deal_breaker" | "high" | "medium" | "low"
_RISK_KEYWORDS = [
    (_re.compile(r"\bcondemn|uninhabitable|not? habitable\b", _re.I), "deal_breaker", "Condemned / uninhabitable"),
    (_re.compile(r"\btenant occupied|currently occupied|\boccupied\b|squatter", _re.I), "high", "Occupied — possible eviction (time + costs)"),
    (_re.compile(r"\bfoundation\b|structural", _re.I), "high", "Possible structural / foundation issue"),
    (_re.compile(r"\bfire[-\s]?damage|fire[-\s]?damaged\b", _re.I), "high", "Fire damage"),
    (_re.compile(r"\bflood|water damage\b", _re.I), "high", "Flood / water damage"),
    (_re.compile(r"\btax lien|\blien\b|back taxes|delinquent tax", _re.I), "high", "Lien / back taxes — title needs review"),
    (_re.compile(r"\bmold|mould\b", _re.I), "medium", "Mold"),
    (_re.compile(r"\bas[-\s]?is\b", _re.I), "medium", "Sold as-is — no warranty"),
    (_re.compile(r"\bcash[-\s]?only|cash offers?\b", _re.I), "medium", "Cash only"),
    (_re.compile(r"\bno (interior )?access|drive[-\s]?by only|exterior only", _re.I), "medium", "No interior access — condition unknown"),
    (_re.compile(r"\bestate sale|probate\b", _re.I), "medium", "Estate / probate — delays / multiple heirs"),
    (_re.compile(r"\bshort sale\b", _re.I), "medium", "Short sale — bank approval delay"),
    (_re.compile(r"\bauction\b|sheriff sale|foreclosure auction", _re.I), "medium", "Auction — non-refundable deposit, no inspection"),
]


def assess_risk(deal: dict, m: dict) -> dict:
    """Deterministic, free (no AI) safety screen. Returns risk_grade (A–F),
    deal_breakers (list of str), risk_flags (list of {severity,label}).
    This runs on EVERY deal at create/update so job-2 (avoid problems) is on by
    default, before any paid AI run."""
    breakers, flags = [], []

    def add(sev, label):
        if sev == "deal_breaker":
            if label not in breakers:
                breakers.append(label)
        else:
            if not any(f["label"] == label for f in flags):
                flags.append({"severity": sev, "label": label})

    pp = deal.get("purchase_price", 0) or 0
    arv = deal.get("arv_base", 0) or 0
    rehab = deal.get("rehab_base", 0) or 0
    arv_low = deal.get("arv_low") or 0
    arv_high = deal.get("arv_high") or 0

    # --- Financial guardrails ---
    if arv <= 0:
        add("high", "ARV unknown — establish before any offer (AI ARV Research)")
    else:
        if pp > arv:
            add("deal_breaker", "Purchase price above ARV (you're paying more than the after-repair value)")
        if rehab > 0.70 * arv:
            add("deal_breaker", "Rehab > 70% of ARV — excessive capital at risk")
        elif rehab > 0.50 * arv:
            add("high", "Heavy rehab (>50% of ARV) — verify the budget")
        if m.get("net_profit", 0) < 0:
            add("deal_breaker", "Negative profit at the current price")
        elif m.get("margin", 0) < 10:
            add("high", "Thin margin (<10% of ARV) — little cushion")
        if not m.get("rule_70_pass", True) and m.get("rule_70_overage", 0) > 15000:
            add("medium", "Above the 70% rule (purchase price too high)")
        # ARV reliability
        if arv_low and arv_high and (arv_high - arv_low) / arv > 0.30:
            add("medium", "Uncertain ARV — wide spread between comps")
        if str(deal.get("arv_confidence", "")).lower() == "low":
            add("medium", "Low ARV confidence — confirm the comparables")
    # worst-case scenario goes negative
    try:
        worst = next((s for s in m.get("scenarios", []) if s.get("name") == "Worst Case"), None)
        if worst and worst.get("net", 0) < 0 and m.get("net_profit", 0) >= 0:
            add("medium", "Worst case goes negative — fragile if ARV drops / rehab overruns")
    except Exception:
        pass

    # --- Property attributes ---
    yb = deal.get("year_built")
    try:
        if yb and int(yb) < 1950:
            add("medium", f"Built in {int(yb)} — likely lead / asbestos / old wiring")
    except (TypeError, ValueError):
        pass
    if (deal.get("source") or "") == "auction" or "auction" in (deal.get("source_url") or "").lower():
        add("medium", "Auction source — verify title/liens, occupancy and condition (no inspection)")
    dom = deal.get("days_on_market")
    try:
        if dom is not None and int(dom) > 150:
            add("low", f"On the market for {int(dom)} days — why isn't it selling?")
    except (TypeError, ValueError):
        pass

    # --- Keyword scan of description + notes + uploaded-document summary ---
    text = " ".join(str(deal.get(k) or "") for k in
                    ("description", "notes", "listing_name", "strategy_hint", "document_summary"))
    if text.strip():
        for rx, sev, label in _RISK_KEYWORDS:
            if rx.search(text):
                add(sev, label)

    # --- Uploaded documents (inspection/appraisal/…) drive risk directly ---
    for doc in (deal.get("documents") or []):
        a = (doc.get("analysis") or {})
        for b in (a.get("deal_breakers") or []):
            add("deal_breaker", f"Inspection: {b}")
        for f in (a.get("findings") or []):
            if f.get("severity") in ("major", "safety") and f.get("issue"):
                add("high", f"Inspection: {str(f['issue'])[:90]}")
        if a.get("verdict") == "bad":
            add("high", "Uploaded document: unfavorable verdict")

    # --- Grade ---
    highs = sum(1 for f in flags if f["severity"] == "high")
    meds = sum(1 for f in flags if f["severity"] == "medium")
    if breakers:
        grade = "F"
    elif highs >= 2:
        grade = "D"
    elif highs == 1 or meds >= 3:
        grade = "C"
    elif meds >= 1:
        grade = "B"
    else:
        grade = "A"

    n = len(breakers) + len(flags)
    if breakers:
        summary = f"{len(breakers)} deal-breaker(s) — avoid unless verified"
    elif n:
        summary = f"{n} point(s) to watch"
    else:
        summary = "No obvious risk signals"

    return {"risk_grade": grade, "deal_breakers": breakers,
            "risk_flags": flags, "risk_summary": summary}


def board_aggregates(deals_with_metrics: list) -> dict:
    """Compute board-level aggregate statistics. Null-safe: deals may be missing
    rehab_base or other fields."""
    if not deals_with_metrics:
        return {"count": 0}

    def _n(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    total_cap = sum(_n(m.get("acquisition")) + _n(d.get("rehab_base"))
                    for d, m in deals_with_metrics)
    total_profit = sum(_n(m.get("net_profit")) for _, m in deals_with_metrics)
    avg_roi = sum(_n(m.get("roi")) for _, m in deals_with_metrics) / len(deals_with_metrics)
    avg_cap = sum(_n((m.get("rent") or {}).get("cap_rate")) for _, m in deals_with_metrics) / len(deals_with_metrics)
    passing = sum(1 for _, m in deals_with_metrics if m.get("rule_70_pass"))
    by_signal = {}
    for d, _ in deals_with_metrics:
        s = d.get("signal", "UNKNOWN")
        by_signal[s] = by_signal.get(s, 0) + 1
    return {
        "count": len(deals_with_metrics),
        "total_capital": total_cap,
        "total_profit": total_profit,
        "avg_roi": avg_roi,
        "avg_cap_rate": avg_cap,
        "passing_70_rule": passing,
        "by_signal": by_signal,
    }
