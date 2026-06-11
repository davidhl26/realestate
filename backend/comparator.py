"""Multi-deal comparison engine.

Takes a list of deal IDs (or all deals), computes the Flip Score across
seven weighted dimensions, calculates P&L + flip vs rent vs BRRRR exits,
picks per-category winners, and optionally asks Claude for a final
"which deal is the best and why" verdict.

Methodology (mirrors the flip-board skill):
  Margin & ROI         30 pts
  ARV Confidence       15 pts
  70% Rule             15 pts
  Market Conditions    15 pts
  Rehab Complexity     10 pts
  Neighborhood Quality 10 pts
  Exit Optionality      5 pts
  ─────────────────── 100 pts
"""
import json
import logging
import re
from typing import Optional

log = logging.getLogger("flip-board.comparator")


# ============================================================================
# Per-deal metrics
# ============================================================================

def _safe(v, default=0):
    """Coerce None / '' to a numeric default."""
    try:
        if v in (None, ""): return default
        return float(v)
    except (TypeError, ValueError):
        return default


def compute_deal_metrics(deal: dict, brrrr_rate: float = 7.5,
                          brrrr_ltv: float = 0.70) -> dict:
    """Compute every metric for one deal — P&L, ROI, 70% rule,
    rental cap, BRRRR, recommended strategy.

    Returns a flat dict suitable for the comparison table.
    """
    pp = _safe(deal.get("purchase_price"))
    arv = _safe(deal.get("arv_base"))
    rehab = _safe(deal.get("rehab_base"))
    hold_mo = _safe(deal.get("holding_months"), 5) or 5
    hold_cost = _safe(deal.get("holding_cost_monthly"), 500)
    sell_pct = _safe(deal.get("selling_cost_pct"), 8)
    rent = _safe(deal.get("estimated_rent"))
    taxes_mo = _safe(deal.get("monthly_taxes"), 200)
    ins_mo = _safe(deal.get("monthly_insurance"), 70)
    hoa_mo = _safe(deal.get("monthly_hoa"), 0)
    maint_mo = _safe(deal.get("monthly_maintenance"), 100)
    mgmt_mo = _safe(deal.get("monthly_mgmt"), 120)
    vacancy = _safe(deal.get("vacancy_pct"), 8)

    # ===== FLIP P&L =====
    closing = pp * 0.02
    acquisition = pp + closing
    holding = hold_mo * hold_cost
    selling = arv * (sell_pct / 100)
    all_in = acquisition + rehab + holding + selling
    net_profit = arv - all_in
    cash_in = acquisition + rehab + holding
    roi = (net_profit / cash_in) if cash_in > 0 else 0
    annualized_roi = roi * (12 / hold_mo) if hold_mo > 0 else 0

    # ===== 70% RULE =====
    max_purchase_70 = (arv * 0.70) - rehab
    rule_overage = pp - max_purchase_70
    rule_status = "PASS" if pp <= max_purchase_70 else "FAIL"

    # ===== MAX PURCHASE BACK-SOLVER (target 15% margin) =====
    target_margin_pct = 0.15
    target_profit = arv * target_margin_pct
    max_acq = arv - target_profit - rehab - holding - selling
    max_purchase = max_acq / 1.02 if max_acq > 0 else 0

    # ===== RENT EXIT =====
    gross_rent_yr = rent * 12
    vacancy_loss = gross_rent_yr * (vacancy / 100)
    op_ex_yr = 12 * (taxes_mo + ins_mo + hoa_mo + maint_mo + mgmt_mo)
    noi_yr = gross_rent_yr - vacancy_loss - op_ex_yr
    rent_basis = pp + rehab
    cap_rate = (noi_yr / rent_basis) if rent_basis > 0 else 0
    coc_cash = (noi_yr / (acquisition + rehab)) if (acquisition + rehab) > 0 else 0
    grm = (rent_basis / gross_rent_yr) if gross_rent_yr > 0 else 0

    # ===== BRRRR EXIT =====
    refi_value = arv * brrrr_ltv
    capital_left_in = (acquisition + rehab) - refi_value  # negative = cashed out
    # 30-yr PMT @ brrrr_rate %
    monthly_rate = (brrrr_rate / 100) / 12
    n_periods = 360
    if monthly_rate > 0 and refi_value > 0:
        pmt = refi_value * (monthly_rate * (1 + monthly_rate)**n_periods) / \
              ((1 + monthly_rate)**n_periods - 1)
    else:
        pmt = 0
    monthly_PITI = pmt + taxes_mo + ins_mo + hoa_mo
    monthly_cf_brrrr = rent - (monthly_PITI + maint_mo + mgmt_mo +
                                 rent * (vacancy / 100))

    # ===== RECOMMENDED STRATEGY =====
    market_trend = _safe(deal.get("market_trend_yoy_pct"), 0)
    strategy = "PASS"
    if roi >= 0.20 and market_trend >= -3:
        strategy = "FLIP"
    elif cap_rate >= 0.08 and monthly_cf_brrrr >= 200:
        strategy = "BRRRR"
    elif cap_rate >= 0.09 and roi < 0.15:
        strategy = "RENT"
    elif rule_status == "FAIL" and rule_overage > 30000 and roi < 0.08:
        strategy = "WHOLESALE"
    elif roi >= 0.10:
        strategy = "FLIP"  # default to flip if profitable

    return {
        # Identity
        "id": deal.get("id"),
        "address": deal.get("address", ""),
        "city": deal.get("city", ""),
        "state": deal.get("state", ""),
        # Specs
        "beds": deal.get("beds"),
        "baths": deal.get("baths"),
        "sqft": deal.get("sqft"),
        "year_built": deal.get("year_built"),
        # Inputs
        "purchase_price": pp,
        "arv_base": arv,
        "rehab_base": rehab,
        "arv_confidence": deal.get("arv_confidence", "Medium"),
        "rehab_scope": deal.get("rehab_scope", "Mid-level"),
        "holding_months": hold_mo,
        # Flip P&L
        "acquisition": acquisition,
        "holding": holding,
        "selling": selling,
        "all_in": all_in,
        "net_profit": net_profit,
        "cash_in": cash_in,
        "roi": roi,
        "annualized_roi": annualized_roi,
        "profit_margin_pct": (net_profit / arv) if arv > 0 else 0,
        # 70% rule
        "max_purchase_70": max_purchase_70,
        "rule_overage": rule_overage,
        "rule_status": rule_status,
        "max_purchase_15pct_margin": max_purchase,
        "purchase_headroom": max_purchase - pp,
        # Rent exit
        "gross_rent_yr": gross_rent_yr,
        "noi_yr": noi_yr,
        "cap_rate": cap_rate,
        "cash_on_cash": coc_cash,
        "grm": grm,
        "monthly_rent": rent,
        # BRRRR
        "refi_value": refi_value,
        "capital_left_in": capital_left_in,
        "monthly_cf_brrrr": monthly_cf_brrrr,
        # Strategy + quality
        "recommended_strategy": strategy,
        "market_trend_yoy_pct": market_trend,
        "crime_rating": deal.get("crime_rating", ""),
        "school_rating": deal.get("school_rating", ""),
        "median_dom": deal.get("median_dom"),
        # Pre-existing score (if user already analyzed)
        "existing_score": deal.get("score"),
        "existing_grade": deal.get("grade"),
        "existing_signal": deal.get("signal"),
    }


# ============================================================================
# Flip Score (skill methodology — 7 weighted dimensions, 100 pts total)
# ============================================================================

def compute_flip_score(m: dict) -> dict:
    """Score a deal across the 7 dimensions. Returns the breakdown + total."""
    breakdown = {}

    # 1. Margin & ROI (30 pts)
    roi = m["roi"]
    if roi >= 0.25: pts = 30
    elif roi >= 0.15: pts = 22
    elif roi >= 0.05: pts = 14
    elif roi >= 0: pts = 6
    else: pts = 0
    breakdown["margin_roi"] = {"pts": pts, "max": 30,
                                 "label": f"ROI {roi*100:.1f}%"}

    # 2. ARV Confidence (15 pts)
    conf = (m.get("arv_confidence") or "Medium").lower()
    if "high" in conf: pts = 15
    elif "low" in conf: pts = 5
    else: pts = 10
    breakdown["arv_confidence"] = {"pts": pts, "max": 15,
                                     "label": m.get("arv_confidence", "Medium")}

    # 3. 70% Rule (15 pts)
    if m["rule_status"] == "PASS": pts = 15
    elif m["rule_overage"] < 10000: pts = 11
    elif m["rule_overage"] < 25000: pts = 7
    else: pts = 2
    breakdown["seventy_rule"] = {"pts": pts, "max": 15,
                                   "label": m["rule_status"] + (
                                       f" (-${m['rule_overage']:,.0f})"
                                       if m["rule_overage"] > 0 else "")}

    # 4. Market Conditions (15 pts)
    yoy = m.get("market_trend_yoy_pct", 0) or 0
    if yoy >= 5: pts = 15
    elif yoy >= 0: pts = 11
    elif yoy >= -5: pts = 7
    else: pts = 3
    breakdown["market"] = {"pts": pts, "max": 15,
                             "label": f"YoY {yoy:+.1f}%"}

    # 5. Rehab Complexity (10 pts)
    scope = (m.get("rehab_scope") or "Mid-level").lower()
    if "cosmetic" in scope: pts = 10
    elif "gut" in scope or "full" in scope or "heavy" in scope: pts = 4
    else: pts = 7
    breakdown["rehab_complexity"] = {"pts": pts, "max": 10,
                                       "label": m.get("rehab_scope", "Mid-level")}

    # 6. Neighborhood Quality (10 pts)
    crime = (m.get("crime_rating") or "C").upper()[:1]
    pts_map = {"A": 10, "B": 8, "C": 5, "D": 2, "F": 0}
    pts = pts_map.get(crime, 5)
    breakdown["neighborhood"] = {"pts": pts, "max": 10,
                                   "label": f"Crime {m.get('crime_rating', 'C')}"}

    # 7. Exit Optionality (5 pts) — uses the CANONICAL shared scorer so the
    # compare view grades exits exactly like the detail view.
    from . import analyzer
    pts = analyzer.score_exit_optionality(
        roi_pct=m["roi"] * 100,
        cap_rate_pct=m["cap_rate"] * 100,
        brrrr_cf=m["monthly_cf_brrrr"],
        rule_70_pass=(m["rule_status"] == "PASS"),
        rule_70_overage=m["rule_overage"],
    )
    # Recover the viable-exit count for the label
    _ev = sum([m["roi"] * 100 > 5, m["cap_rate"] * 100 >= 7,
               m["monthly_cf_brrrr"] >= 100,
               (m["rule_status"] == "PASS" or m["rule_overage"] < 15000)])
    breakdown["exit_optionality"] = {"pts": pts, "max": 5,
                                       "label": f"{_ev}/4 viable exits"}

    total = sum(b["pts"] for b in breakdown.values())
    grade, signal = analyzer.grade_and_signal(total)
    return {
        "score": total,
        "grade": grade,
        "signal": signal,
        "breakdown": breakdown,
    }


# ============================================================================
# Multi-deal comparison
# ============================================================================

def compare_deals(deals: list) -> dict:
    """Compute metrics + Flip Score for every deal, identify per-category
    winners, surface aggregates."""
    if not deals:
        return {"ok": False, "error": "No deals to compare"}

    scored = []
    for d in deals:
        m = compute_deal_metrics(d)
        s = compute_flip_score(m)
        scored.append({**m, **s})

    # Sort by Flip Score descending
    ranked = sorted(scored, key=lambda x: x["score"], reverse=True)

    # Per-category winners (max value wins; for some, min wins)
    def winner(key, higher_is_better=True):
        if not scored: return None
        if higher_is_better:
            w = max(scored, key=lambda x: x.get(key) or 0)
        else:
            w = min(scored, key=lambda x: x.get(key) or 9e9)
        return {"id": w["id"], "address": w["address"], "value": w.get(key)}

    winners = {
        "highest_score":     winner("score", True),
        "highest_roi":       winner("roi", True),
        "highest_profit":    winner("net_profit", True),
        "highest_cap_rate":  winner("cap_rate", True),
        "best_brrrr_cf":     winner("monthly_cf_brrrr", True),
        "biggest_headroom":  winner("purchase_headroom", True),
        "lowest_cash_in":    winner("cash_in", False),
        "best_market":       winner("market_trend_yoy_pct", True),
    }

    # Aggregates
    n = len(scored)
    avg_roi = sum(d["roi"] for d in scored) / n
    avg_score = sum(d["score"] for d in scored) / n
    total_profit_potential = sum(max(0, d["net_profit"]) for d in scored)
    total_cash_needed = sum(d["cash_in"] for d in scored)

    by_strategy = {}
    for d in scored:
        s = d["recommended_strategy"]
        by_strategy[s] = by_strategy.get(s, 0) + 1

    return {
        "ok": True,
        "deals": ranked,
        "winners": winners,
        "aggregates": {
            "count": n,
            "avg_score": round(avg_score, 1),
            "avg_roi": avg_roi,
            "total_profit_potential": total_profit_potential,
            "total_cash_needed": total_cash_needed,
            "by_strategy": by_strategy,
        },
        "best_deal": ranked[0] if ranked else None,
        "worst_deal": ranked[-1] if len(ranked) > 1 else None,
    }


# ============================================================================
# AI verdict — Claude writes a 2-paragraph "which deal wins and why"
# ============================================================================

VERDICT_SYSTEM = """You are a fix-and-flip portfolio analyst. You receive a
comparison of 2+ real-estate deals with every metric (Flip Score, ROI,
profit, cap rate, BRRRR cash-flow, recommended strategy, etc.).

Your job: identify the BEST deal and explain why in a way the user can
act on TODAY. Be opinionated, not academic. Acknowledge trade-offs.

CRITICAL: Output a single JSON code block with this exact schema, NO other text after:
{
  "winner_id": "<deal id of the best overall>",
  "verdict": "<2-3 sentence executive summary — name the winner and the single biggest reason>",
  "why_winner": ["<bullet 1>", "<bullet 2>", "<bullet 3>"],
  "honorable_mentions": [
    {"id": "<deal id>", "reason": "<one sentence on what this deal is best for>"}
  ],
  "avoid": [
    {"id": "<deal id>", "reason": "<why this one is weakest>"}
  ],
  "portfolio_play": "<1-2 sentences: if user can do MULTIPLE deals, which combo + why (e.g. 'flip A for cash, hold B as rental for cash-flow')>",
  "next_actions": ["<concrete action item>", "<another action item>"]
}"""


def ai_verdict(comparison: dict, focus: Optional[str] = None) -> dict:
    """Ask Claude to call the winner and explain why."""
    from . import ai_research
    api_key = ai_research.get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured."}

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = ai_research.get_model()

    # Build a compact prompt — don't waste tokens on the full deal dump
    rows = []
    for d in comparison["deals"]:
        rows.append({
            "id": d["id"],
            "address": d.get("address", ""),
            "score": d["score"],
            "grade": d["grade"],
            "signal": d["signal"],
            "purchase_price": d["purchase_price"],
            "arv": d["arv_base"],
            "rehab": d["rehab_base"],
            "cash_in": round(d["cash_in"]),
            "net_profit": round(d["net_profit"]),
            "roi_pct": round(d["roi"] * 100, 1),
            "annualized_roi_pct": round(d["annualized_roi"] * 100, 1),
            "cap_rate_pct": round(d["cap_rate"] * 100, 2),
            "brrrr_cashflow_mo": round(d["monthly_cf_brrrr"]),
            "70_rule": d["rule_status"],
            "headroom": round(d["purchase_headroom"]),
            "arv_confidence": d["arv_confidence"],
            "rehab_scope": d["rehab_scope"],
            "market_yoy_pct": d.get("market_trend_yoy_pct", 0),
            "crime": d.get("crime_rating", "?"),
            "recommended_strategy": d["recommended_strategy"],
            "breakdown": {k: v["pts"] for k, v in d["breakdown"].items()},
        })

    user_prompt = ("Compare these fix-and-flip deals and pick the best.\n\n"
                    "Deals:\n" + json.dumps(rows, indent=2) +
                    "\n\nAggregate stats:\n" +
                    json.dumps(comparison["aggregates"], indent=2))
    if focus:
        user_prompt += f"\n\nUser priority: {focus}"

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=2500,
            system=VERDICT_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key", "error_type": "auth"}
    except Exception as e:
        log.exception("AI verdict failed")
        return {"ok": False, "error": str(e)}

    text = "".join(b.text for b in msg.content if hasattr(b, "text"))
    # Extract JSON
    data = None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try: data = json.loads(m.group(1))
        except: pass
    if not data:
        try: data = json.loads(text)
        except:
            # Find first {} block
            depth, start = 0, None
            for i, ch in enumerate(text):
                if ch == "{":
                    if depth == 0: start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start is not None:
                        try:
                            data = json.loads(text[start:i+1]); break
                        except: start = None

    if not data:
        return {"ok": False, "error": "Could not parse verdict",
                "raw": text[:1500]}

    return {
        "ok": True,
        **data,
        "model": model,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
    }
