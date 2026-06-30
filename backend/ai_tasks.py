"""All Opus-4.7-powered analysis tasks.

Each task accepts a deal dict and returns:
    {"ok": True, "result": {...}, "model": str, "usage": {...}, "web_searches_used": int}
or
    {"ok": False, "error": str}

Tasks share infrastructure (Claude client, web_search, JSON extraction).
"""

import json
import logging
import re
from typing import Any, Optional

from .ai_research import (
    get_api_key, get_model, is_configured, _extract_json, _repair_json,
)

log = logging.getLogger("flip-board.ai_tasks")


def _parse_task_json(text: str) -> Optional[dict]:
    """Parse the task's JSON, salvaging truncated responses.

    Web-search tasks sometimes hit max_tokens mid-JSON, leaving unclosed
    braces. _extract_json fails on those; json-repair can still recover the
    fields emitted before the cut-off."""
    d = _extract_json(text)
    if d:
        return d
    start = text.find("{")
    if start >= 0:
        d = _repair_json(text[start:])
        if isinstance(d, dict) and d:
            return d
    return None

WEB_TOOL = {"type": "web_search_20250305", "name": "web_search", "max_uses": 8}


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=get_api_key())


def _summary(deal: dict, include_financials: bool = True,
              include_anchors: bool = True) -> str:
    """Compact text summary of deal data passed in every prompt."""
    p = []
    p.append(f"Address: {deal.get('address', '?')}")
    if deal.get("city") and deal.get("state"):
        p.append(f"City/State: {deal['city']}, {deal['state']} {deal.get('zip', '')}")
    p.append(f"Type: {deal.get('property_type', 'SFR')}")
    if deal.get("beds") is not None:
        p.append(f"Bedrooms: {deal['beds']}")
    if deal.get("baths") is not None:
        p.append(f"Bathrooms: {deal['baths']}")
    if deal.get("sqft"):
        p.append(f"Living area: {deal['sqft']:,} sqft")
    if deal.get("year_built"):
        p.append(f"Year built: {deal['year_built']}")
    if deal.get("lot_size"):
        p.append(f"Lot: {deal['lot_size']}")
    if include_financials:
        if deal.get("purchase_price"):
            p.append(f"Purchase price: ${deal['purchase_price']:,}")
        if deal.get("arv_base"):
            p.append(f"ARV (base): ${deal['arv_base']:,}")
        if deal.get("rehab_base"):
            p.append(f"Planned rehab: ${deal['rehab_base']:,} ({deal.get('rehab_scope', 'mid')})")
    if include_anchors:
        if deal.get("zillow_estimate"):
            p.append(f"Zillow estimate: ${deal['zillow_estimate']:,}")
        if deal.get("realtor_estimate"):
            p.append(f"Realtor estimate: ${deal['realtor_estimate']:,}")
        if deal.get("comp_value_estimate"):
            p.append(f"Rentcast comp avg: ${deal['comp_value_estimate']:,}")
        if deal.get("rent_zestimate") or deal.get("estimated_rent"):
            r = deal.get("rent_zestimate") or deal.get("estimated_rent")
            p.append(f"Rent estimate: ${r}/mo")
    if deal.get("description"):
        p.append(f"\nDescription excerpt: {deal['description'][:1500]}")
    return "\n".join(p)


def _run_claude(system: str, user: str, *, use_web: bool = True,
                  max_tokens: int = 3000, vision_images: Optional[list] = None,
                  pdf_bytes: Optional[bytes] = None) -> dict:
    """Common wrapper. Returns {ok, text, model, usage, web_searches_used} or {ok:False, error}.
    pdf_bytes: attach a PDF directly so Claude reads it natively (handles
    scanned / no-text-layer PDFs that pdfplumber can't extract)."""
    if not is_configured():
        return {"ok": False, "error": "No Anthropic API key (Settings → AI)."}
    try:
        client = _client()
        model = get_model()

        content = []
        if vision_images:
            for url in vision_images[:6]:  # limit
                content.append({"type": "image", "source": {"type": "url", "url": url}})
        if pdf_bytes:
            import base64
            content.append({"type": "document", "source": {
                "type": "base64", "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode("ascii")}})
        content.append({"type": "text", "text": user})

        kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        if use_web:
            kwargs["tools"] = [WEB_TOOL]

        import anthropic
        msg = client.messages.create(**kwargs)
    except Exception as e:
        log.exception("Claude call failed")
        err_str = str(e)
        # Friendly messages for known error patterns
        if "credit balance is too low" in err_str.lower() or "insufficient" in err_str.lower():
            return {"ok": False, "error": (
                "Your Anthropic account is out of credits. "
                "Top up at console.anthropic.com/settings/billing — "
                "$10-20 covers 30-100 AI runs."
            ), "error_type": "billing"}
        if "authentication" in err_str.lower() or "invalid_api_key" in err_str.lower():
            return {"ok": False, "error": (
                "Invalid API key — check Settings → AI."
            ), "error_type": "auth"}
        if "rate_limit" in err_str.lower() or "429" in err_str:
            return {"ok": False, "error": (
                "Anthropic rate limit hit. Wait 60 seconds and retry."
            ), "error_type": "rate_limit"}
        if "overloaded" in err_str.lower() or "529" in err_str:
            return {"ok": False, "error": (
                "Anthropic API is temporarily overloaded. Retry in a moment."
            ), "error_type": "overloaded"}
        return {"ok": False, "error": f"AI call failed: {e}", "error_type": "other"}

    text_parts, web = [], 0
    for block in msg.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        if getattr(block, "type", "") == "server_tool_use":
            web += 1
    return {
        "ok": True,
        "text": "\n".join(text_parts),
        "model": model,
        "web_searches_used": web,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
        "stop_reason": msg.stop_reason,
    }


def _wrap(text_result: dict, parsed: Any) -> dict:
    """Decorate a parsed JSON result with model/usage info."""
    if not text_result.get("ok"):
        return text_result
    return {
        "ok": True,
        "result": parsed,
        "model": text_result.get("model"),
        "usage": text_result.get("usage"),
        "web_searches_used": text_result.get("web_searches_used"),
    }


# ============================================================================
# TASK 1 — ARV RESEARCH (also in ai_research.py; re-implemented for uniformity)
# ============================================================================

def task_arv(deal: dict) -> dict:
    """ARV research. Single source of truth: delegates to
    ai_research.research_arv() (web_search + comps) so there is ONE ARV prompt
    to maintain. Adapts the flat research output into the task envelope shape
    {ok, result:{...}, model, usage, web_searches_used} the UI expects."""
    from . import ai_research
    r = ai_research.research_arv(deal)
    if not r.get("ok"):
        return r
    result = {
        "arv_low": r.get("arv_low"),
        "arv_base": r.get("arv_base"),
        "arv_high": r.get("arv_high"),
        "confidence": r.get("confidence", "Medium"),
        "reasoning": r.get("reasoning", ""),
        "comparables": r.get("comparables", []),
        "market_notes": r.get("market_notes", ""),
        "warnings": r.get("warnings", []),
    }
    return {
        "ok": True,
        "result": result,
        "model": r.get("model"),
        "usage": r.get("usage", {}),
        "web_searches_used": r.get("web_searches_used", 0),
    }


# ============================================================================
# TASK 2 — REHAB ESTIMATE (line-by-line)
# ============================================================================

REHAB_SYSTEM = """You are a fix-and-flip rehab cost estimator with deep knowledge of regional labor and material costs.

Use web_search for current local cost benchmarks if the city is unusual.

Apply a regional cost multiplier (Miami 1.30, NYC/SF 1.5, Cincinnati/Cleveland 0.85, Atlanta 0.95, Phoenix 1.0, Denver 1.15, etc.). State the multiplier used.

For each category, return low/base/high cost (base assumes "mid-grade" finish: shaker cabinets, quartz remnants, LVP, mid-tier appliances, basic tile).

Output JSON only (no other text):
{
  "regional_multiplier": <float>,
  "regional_basis": "<short rationale>",
  "scope_recommended": "Cosmetic"|"Mid-level"|"Full gut",
  "line_items": [
    {"category": "Kitchen", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Bathroom (each)", "count": <int>, "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Flooring", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Paint (interior)", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Paint (exterior)", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Electrical", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Plumbing", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "HVAC", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Roof", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Windows", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Landscaping/exterior", "low": <int>, "base": <int>, "high": <int>, "notes": "..."},
    {"category": "Permits + Cleanup", "low": <int>, "base": <int>, "high": <int>, "notes": "..."}
  ],
  "subtotal_low": <int>,
  "subtotal_base": <int>,
  "subtotal_high": <int>,
  "contingency_pct": <int>,
  "contingency_amount": <int>,
  "total_low": <int>,
  "total_base": <int>,
  "total_high": <int>,
  "timeline_weeks": {"low": <int>, "base": <int>, "high": <int>},
  "scope_notes": "<paragraph>",
  "risk_flags": ["..."]
}"""


def task_rehab(deal: dict) -> dict:
    user = (f"Estimate the rehab budget line-by-line for a flip on this property:\n\n"
             f"{_summary(deal)}\n\n"
             "Assume the goal is to bring the home to mid-grade renovated condition and resell at ARV.\n"
             "Use the property age, sqft, and city to estimate. Apply the regional multiplier. "
             "If photos or description suggest specific needs (roof, plumbing, etc.), include them.")
    r = _run_claude(REHAB_SYSTEM, user, max_tokens=3500)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 3 — RENT COMPS
# ============================================================================

RENT_SYSTEM = """You are a rental market analyst. Find recent rental comps for a property.

Use web_search. Find 5-8 actual rental listings (last 90 days, active or recently rented) within ~0.5 miles, matching beds/baths/sqft.

Output JSON only:
{
  "rent_low": <int>,
  "rent_base": <int>,
  "rent_high": <int>,
  "confidence": "Low"|"Medium"|"High",
  "occupancy_estimate_pct": <int>,
  "comparables": [
    {"address": "...", "beds": <int>, "baths": <float>, "sqft": <int>, "rent_per_mo": <int>, "date": "<YYYY-MM>", "notes": "..."},
    ...
  ],
  "market_notes": "<1-2 sentences on rental demand and trends>",
  "best_rent_strategy": "<long-term, mid-term, or short-term>",
  "warnings": ["..."]
}"""


def task_rent_comps(deal: dict) -> dict:
    user = (f"Find rental comparables and estimate market rent for this renovated property:\n\n"
             f"{_summary(deal)}")
    r = _run_claude(RENT_SYSTEM, user, max_tokens=2500)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 4 — NEIGHBORHOOD
# ============================================================================

NBHD_SYSTEM = """You are a neighborhood research analyst for real estate investors.

Use web_search to research the area around the address: schools (with GreatSchools ratings), crime (per-capita and trends), walkability, demographics, recent development (new construction, commercial), and any natural hazards.

Output JSON only:
{
  "school_ratings": {
    "elementary": {"name": "...", "rating_out_of_10": <int>},
    "middle": {"name": "...", "rating_out_of_10": <int>},
    "high": {"name": "...", "rating_out_of_10": <int>}
  },
  "crime": {
    "overall_grade": "A"|"B"|"C"|"D"|"F",
    "summary": "<sentence>",
    "vs_state_pct": <int>
  },
  "walk_score_estimate": <int>,
  "transit_score_estimate": <int>,
  "demographics": {
    "population": "<approx>",
    "median_income": "<$amount>",
    "owner_occupied_pct": <int>
  },
  "growth_outlook": {
    "rating": "Declining"|"Stable"|"Growing"|"Hot",
    "factors": ["..."]
  },
  "developments": ["<recent or planned major projects>"],
  "hazards": {
    "flood_zone": "...",
    "fire_risk": "Low"|"Moderate"|"High",
    "other": ["..."]
  },
  "investor_summary": "<paragraph: is this a good neighborhood for flipping/holding?>",
  "warnings": ["..."]
}"""


def task_neighborhood(deal: dict) -> dict:
    user = (f"Research the neighborhood quality and investment fundamentals around this address:\n\n"
             f"{_summary(deal, include_financials=False, include_anchors=False)}")
    r = _run_claude(NBHD_SYSTEM, user, max_tokens=4500)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 5 — TAXES & INSURANCE
# ============================================================================

TAX_SYSTEM = """You are a property tax and insurance researcher.

Use web_search to find:
- Actual property tax rate / bill for this address (or the county/city if address-level isn't available)
- Typical homeowners insurance cost for properties of this profile in this area
- Any HOA fees if multi-family, condo, or HOA community
- Hurricane / flood / wildfire insurance riders if applicable

Output JSON only:
{
  "property_tax": {
    "annual_estimate": <int>,
    "monthly_estimate": <int>,
    "effective_rate_pct": <float>,
    "source_note": "<county appraiser? millage rate? actual bill found?>"
  },
  "insurance": {
    "annual_estimate": <int>,
    "monthly_estimate": <int>,
    "type": "HO-3 + ...",
    "notes": "<flood/hurricane/etc. needed?>"
  },
  "hoa": {
    "monthly_estimate": <int|0>,
    "applies": <bool>,
    "notes": "..."
  },
  "warnings": ["..."]
}"""


def task_taxes_insurance(deal: dict) -> dict:
    user = (f"Research the actual property taxes and estimate insurance for this property:\n\n"
             f"{_summary(deal, include_anchors=False)}")
    r = _run_claude(TAX_SYSTEM, user, max_tokens=2000)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 6 — PROPERTY HISTORY
# ============================================================================

HISTORY_SYSTEM = """You are a property history researcher.

Use web_search to find: prior sales history (price + date), permit history (recent renovations), code violations, liens (HOA, tax, mechanic's), foreclosure status, and any other public records of significance.

Output JSON only:
{
  "sales_history": [
    {"date": "<YYYY-MM>", "price": <int>, "notes": "..."},
    ...
  ],
  "last_sale": {"date": "<YYYY-MM>", "price": <int>, "appreciation_since_pct": <int>},
  "permits": [{"date": "<YYYY>", "type": "...", "value": <int>}, ...],
  "violations_or_liens": [{"type": "...", "date": "...", "amount": <int>, "status": "..."}],
  "foreclosure_status": "None"|"Pre-foreclosure"|"REO"|"Auction",
  "title_concerns": ["..."],
  "summary": "<paragraph>",
  "warnings": ["..."]
}"""


def task_history(deal: dict) -> dict:
    user = (f"Research property records (sales, permits, liens, foreclosure status) for:\n\n"
             f"{_summary(deal, include_anchors=False)}")
    r = _run_claude(HISTORY_SYSTEM, user, max_tokens=2000)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 7 — RISK ASSESSMENT
# ============================================================================

RISK_SYSTEM = """You are a real estate risk analyst. Assess natural hazards and structural risks.

Use web_search to look up: FEMA flood zone for the address, wildfire risk maps, hurricane history, sinkhole/karst geology, termite zone, soil/foundation risks, environmental contamination (Superfund nearby).

Output JSON only:
{
  "flood": {"zone": "...", "annual_chance_pct": <float>, "insurance_required": <bool>, "notes": "..."},
  "fire_risk": {"level": "Low"|"Moderate"|"High"|"Very High", "notes": "..."},
  "hurricane": {"applies": <bool>, "level": "Low"|"Moderate"|"High", "notes": "..."},
  "earthquake": {"applies": <bool>, "level": "...", "notes": "..."},
  "termite_zone": "Low"|"Moderate"|"High",
  "environmental": {"superfund_within_1mi": <bool>, "notes": "..."},
  "structural_age_concerns": ["..."],
  "summary": "<paragraph>",
  "deal_breakers": ["any risks that would normally kill a flip"]
}"""


def task_risks(deal: dict) -> dict:
    user = (f"Assess natural hazards and structural risks for:\n\n"
             f"{_summary(deal, include_anchors=False)}")
    r = _run_claude(RISK_SYSTEM, user, max_tokens=2000)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 8 — PHOTO ANALYSIS (Vision)
# ============================================================================

PHOTO_SYSTEM = """You are a property condition assessor analyzing listing photos for a fix-and-flip investor.

Look at each photo and identify:
- Current condition of each visible space (kitchen, bath, flooring, exterior)
- Specific rehab items needed (cabinets, counters, appliances, paint, flooring, fixtures)
- Hidden concerns visible in photos (water stains, cracks, dated systems, structural issues)
- Estimated condition score 1-10 for each room
- Any positive features that could become selling points after rehab

Output JSON only:
{
  "overall_condition_score": <int 1-10>,
  "rooms_observed": [
    {"room": "Kitchen", "condition": <int>, "rehab_needed": ["..."], "selling_points": ["..."]},
    {"room": "Bath", "condition": <int>, ...},
    ...
  ],
  "exterior": {"condition": <int>, "rehab_needed": ["..."], "selling_points": ["..."]},
  "hidden_concerns": ["..."],
  "scope_summary": "<paragraph>",
  "rehab_complexity": "Cosmetic"|"Mid-level"|"Full gut",
  "estimated_rehab_range": {"low": <int>, "high": <int>}
}"""


def task_photos(deal: dict) -> dict:
    images = deal.get("image_gallery") or ([deal["image"]] if deal.get("image") else [])
    if not images:
        # Graceful skip — a deal without photos must NOT block the verdict.
        return {"ok": True, "result": {"skipped": True, "reason": "no_images",
                                        "note": "No photos available — visual rehab scope not assessed."},
                "model": None, "usage": {}, "web_searches_used": 0}
    user = (f"Analyze these {len(images[:6])} photos of the property and report scope:\n\n"
             f"{_summary(deal, include_anchors=False)}")
    r = _run_claude(PHOTO_SYSTEM, user, max_tokens=3000, use_web=False, vision_images=images)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 9 — GLOBAL VERDICT
# ============================================================================

VERDICT_SYSTEM = """You are a senior fix-and-flip dealmaker. Given all available data on a property, deliver a clear go/no-go verdict.

Consider: ARV vs purchase, rehab vs ARV (70% rule), market trend, neighborhood quality, risks, exit liquidity (DOM), and any prior AI insights provided.

Output JSON only:
{
  "verdict": "BUY"|"BUY (after negotiation)"|"NEGOTIATE"|"PASS"|"AVOID",
  "confidence": "Low"|"Medium"|"High",
  "target_offer_price": <int>,
  "max_offer_price": <int>,
  "expected_profit": <int>,
  "expected_roi_pct": <int>,
  "top_3_reasons_buy": ["...", "...", "..."],
  "top_3_reasons_pass": ["...", "...", "..."],
  "must_verify_before_offer": ["..."],
  "must_verify_before_closing": ["..."],
  "exit_strategy_priority": ["FLIP", "BRRRR", "RENT"],
  "deal_summary": "<paragraph: the synthesis you'd tell a partner over coffee>",
  "negotiation_levers": ["..."]
}"""


def task_verdict(deal: dict) -> dict:
    # Include prior AI insights if available
    ai = deal.get("ai_insights") or {}
    insights_summary = ""
    for k in ("arv", "rehab", "neighborhood", "risks", "history", "photos", "taxes_insurance"):
        if ai.get(k) and ai[k].get("result"):
            insights_summary += f"\n\n[Prior {k} insight]\n{json.dumps(ai[k]['result'], indent=2)[:1500]}"
    user = (f"Deliver a verdict on this deal:\n\n{_summary(deal)}{insights_summary}\n\n"
             "Combine everything and give a single clear recommendation.")
    r = _run_claude(VERDICT_SYSTEM, user, max_tokens=2500, use_web=False)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 10 — COUNTER OFFER
# ============================================================================

OFFER_SYSTEM = """You are an experienced real estate negotiator. Suggest a structured offer and negotiation strategy.

Output JSON only:
{
  "suggested_initial_offer": <int>,
  "max_walk_away_price": <int>,
  "negotiation_strategy": "<paragraph>",
  "terms_to_request": ["...", "...", "..."],
  "concessions_to_offer": ["..."],
  "estimated_seller_acceptance_pct": <int>,
  "rationale": "<paragraph>"
}"""


def task_offer(deal: dict) -> dict:
    ai = deal.get("ai_insights") or {}
    extras = ""
    if ai.get("verdict") and ai["verdict"].get("result"):
        extras = f"\n\n[Prior verdict]\n{json.dumps(ai['verdict']['result'], indent=2)[:1500]}"
    user = (f"Suggest an offer and negotiation strategy for this deal:\n\n{_summary(deal)}{extras}")
    r = _run_claude(OFFER_SYSTEM, user, max_tokens=1500, use_web=False)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 11 — RED FLAGS
# ============================================================================

REDFLAGS_SYSTEM = """You are a fraud and red-flag detector for fix-and-flip deals.

Scan the property data and description for warning signs:
- ARV anchors with > 15% spread (Zillow vs Realtor vs comping)
- Pricing oddities (way below market = title issues? auction-only? structural?)
- Description language that signals problems ("as-is", "investor only", "cash only", "needs work", "tlc")
- Anomalies in the property (size, year, lot)
- HOA/condo special-assessment indicators
- Geographic risk (flood, hurricane, fire)
- Lien hints, foreclosure indicators
- Multiple price drops, long DOM

Output JSON only:
{
  "red_flags": [
    {"flag": "<short>", "severity": "Critical"|"High"|"Medium"|"Low", "evidence": "<what triggered this>", "mitigation": "<what to verify>"}
  ],
  "deal_breakers": ["..."],
  "overall_risk_grade": "A"|"B"|"C"|"D"|"F",
  "summary": "<paragraph>"
}"""


def task_red_flags(deal: dict) -> dict:
    user = (f"Scan this deal for red flags:\n\n{_summary(deal)}")
    r = _run_claude(REDFLAGS_SYSTEM, user, max_tokens=2000, use_web=False)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 12 — MARKET TIMING
# ============================================================================

TIMING_SYSTEM = """You are a real estate market timer. Advise when to list the renovated property for resale.

Use web_search to research seasonal patterns in the local market.

Output JSON only:
{
  "best_listing_months": ["April", "May", ...],
  "worst_listing_months": ["..."],
  "current_market_phase": "Buyer's market"|"Balanced"|"Seller's market"|"Hot",
  "seasonal_premium_pct": <int>,
  "expected_dom_now": <int>,
  "expected_dom_best_season": <int>,
  "recommendation": "<paragraph: when should we list? hold-and-rent first?>",
  "warnings": ["..."]
}"""


def task_timing(deal: dict) -> dict:
    user = (f"Advise on market timing for resale of this renovated property:\n\n"
             f"{_summary(deal, include_anchors=False)}")
    r = _run_claude(TIMING_SYSTEM, user, max_tokens=1800)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 13 — MLS LISTING (5 styles)
# ============================================================================

MLS_SYSTEM = """You are a real estate copywriter. Write 5 MLS listing descriptions, each targeting a different buyer persona.

Personas:
- family: emphasizes schools, yard, safety, walkability
- first_time_buyer: emphasizes affordability, low-maintenance, starter-home features
- investor: emphasizes cap rate, rental demand, neighborhood appreciation
- luxury: emphasizes finishes, design, exclusivity (use sparingly)
- downsizer: emphasizes single-level, low-maintenance, community amenities

Each description: 120-180 words, present tense, vivid sensory language, end with a CTA.

Output JSON only:
{
  "descriptions": {
    "family": "<text>",
    "first_time_buyer": "<text>",
    "investor": "<text>",
    "luxury": "<text>",
    "downsizer": "<text>"
  },
  "headline_options": ["<5 short headlines, 60 char max each>"]
}"""


def task_mls_listing(deal: dict) -> dict:
    user = (f"Write 5 MLS listings for this property (post-rehab):\n\n{_summary(deal)}")
    r = _run_claude(MLS_SYSTEM, user, max_tokens=3500, use_web=False)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 14 — OFFER LETTER
# ============================================================================

LETTER_SYSTEM = """You are an experienced real estate buyer writing a personal offer letter to a seller.

The goal is to build rapport and explain your offer in a relatable way. Keep it warm, professional, and brief (200-300 words). Adapt to a likely owner-occupant seller; soften if the seller is institutional.

Output JSON only:
{
  "letter": "<full letter text>",
  "terms_summary": ["price", "earnest money", "due diligence period", "closing date", "contingencies"],
  "tone_used": "warm-rapport"|"investor-professional"|"firm",
  "personalization_used": ["..."]
}"""


def task_offer_letter(deal: dict) -> dict:
    user = (f"Write an offer letter for this property:\n\n{_summary(deal)}")
    r = _run_claude(LETTER_SYSTEM, user, max_tokens=1500, use_web=False)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK 15 — MARKETING COPY (social + flyer)
# ============================================================================

MARKETING_SYSTEM = """You are a real estate marketer. Generate social media + flyer copy for the renovated property listing.

Output JSON only:
{
  "instagram_post": "<text + 5 hashtags>",
  "facebook_post": "<text>",
  "twitter_post": "<short, under 280 chars>",
  "flyer": {
    "headline": "<8-12 words>",
    "tagline": "<sub-headline>",
    "bullets": ["...", "...", "...", "...", "..."],
    "cta": "<call to action>"
  },
  "email_subject_lines": ["...", "...", "..."]
}"""


def task_marketing(deal: dict) -> dict:
    user = (f"Generate marketing copy for the post-rehab listing of:\n\n{_summary(deal)}")
    r = _run_claude(MARKETING_SYSTEM, user, max_tokens=2000, use_web=False)
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


DOC_SYSTEM = """You analyze a real-estate DOCUMENT that a fix-and-flip investor uploaded for a specific property — most often a home INSPECTION report, but it may be an appraisal, a title/lien report, a seller disclosure, or a contractor estimate.

Read the document text and:
1. Identify the document type.
2. Extract the concrete findings. For an inspection: EVERY defect/issue, the system it affects (Roof, Foundation/Structural, Electrical, Plumbing, HVAC, Water heater, Windows, Exterior/Siding, Interior, Pests/Termites, Mold, Environmental, Other), a severity (minor | moderate | major | safety), and a rough repair cost in USD.
3. Pull key numbers present in the doc (appraised value, sqft, year built, any total repair figure stated).
4. Give an overall VERDICT for the investor: "good" (clean / only minor items), "caution" (notable issues — budget carefully), or "bad" (major/safety defects or deal-breakers). Explain in 1-3 sentences, comparing the implied total repair cost to the deal's current rehab budget and ARV when provided.
5. List deal_breakers (foundation failure, extensive mold, fire/structural damage, failed septic, unpermitted additions, active leaks/roof at end of life, etc.).
6. suggested_rehab = your best total repair budget implied by this document (integer USD; 0 if the doc isn't about repairs).

Be conservative — when a cost is unclear, estimate on the higher side. Output ONE JSON code block and NOTHING after it:
```json
{
  "doc_type": "Inspection report|Appraisal|Title/lien report|Seller disclosure|Contractor estimate|Other",
  "summary": "<2-3 sentence plain-language summary>",
  "findings": [{"system": "...", "issue": "...", "severity": "minor|moderate|major|safety", "est_cost": <int>}],
  "total_repair_estimate": <int>,
  "key_numbers": {"appraised_value": <int|null>, "sqft": <int|null>, "year_built": <int|null>},
  "deal_breakers": ["..."],
  "verdict": "good|caution|bad",
  "verdict_reason": "...",
  "suggested_rehab": <int>
}
```"""


def analyze_document(deal: dict, text: str = "", pdf_bytes: Optional[bytes] = None) -> dict:
    """Analyze an uploaded property document (inspection/appraisal/title/quote).
    Uses the extracted text when available; otherwise sends the PDF straight to
    Claude (handles scanned / no-text-layer PDFs). Returns {ok, result:{...}}."""
    ctx = _summary(deal, include_financials=True, include_anchors=True)
    if (text or "").strip():
        user = (f"DEAL CONTEXT:\n{ctx}\n\nDOCUMENT TEXT (may be truncated):\n"
                f"{text[:18000]}\n\nAnalyze the document and return the JSON.")
        r = _run_claude(DOC_SYSTEM, user, use_web=False, max_tokens=4000)
    elif pdf_bytes:
        user = (f"DEAL CONTEXT:\n{ctx}\n\nAnalyze the ATTACHED PDF document "
                "(no text layer — read it directly) and return the JSON.")
        r = _run_claude(DOC_SYSTEM, user, use_web=False, max_tokens=4000, pdf_bytes=pdf_bytes)
    else:
        return {"ok": False, "error": "No document content"}
    if not r.get("ok"):
        return r
    return _wrap(r, _parse_task_json(r["text"]) or {"error": "parse failed", "raw": r["text"][:1500]})


# ============================================================================
# TASK REGISTRY
# ============================================================================

TASKS = {
    "arv": {
        "fn": task_arv,
        "label": "ARV Research",
        "desc": "Find comps and estimate After Repair Value (low/base/high).",
        "icon": "🎯",
        "uses_web": True,
        "uses_vision": False,
        "group": "research",
        "priority": 1,
    },
    "rehab": {
        "fn": task_rehab,
        "label": "Rehab Estimate",
        "desc": "Line-by-line rehab budget with regional cost adjustments.",
        "icon": "🔧",
        "uses_web": True,
        "uses_vision": False,
        "group": "research",
        "priority": 2,
    },
    "rent_comps": {
        "fn": task_rent_comps,
        "label": "Rent Comps",
        "desc": "Find rental comparables and validate market rent.",
        "icon": "🏠",
        "uses_web": True,
        "uses_vision": False,
        "group": "research",
        "priority": 3,
    },
    "neighborhood": {
        "fn": task_neighborhood,
        "label": "Neighborhood",
        "desc": "Schools, crime, walkability, growth, hazards.",
        "icon": "🏘",
        "uses_web": True,
        "uses_vision": False,
        "group": "research",
        "priority": 4,
    },
    "taxes_insurance": {
        "fn": task_taxes_insurance,
        "label": "Taxes & Insurance",
        "desc": "Research actual property tax + estimate insurance.",
        "icon": "💰",
        "uses_web": True,
        "uses_vision": False,
        "group": "research",
        "priority": 5,
    },
    "history": {
        "fn": task_history,
        "label": "Property History",
        "desc": "Sales history, permits, liens, foreclosure status.",
        "icon": "📜",
        "uses_web": True,
        "uses_vision": False,
        "group": "research",
        "priority": 6,
    },
    "risks": {
        "fn": task_risks,
        "label": "Risk Assessment",
        "desc": "Flood, fire, hurricane, structural, environmental risks.",
        "icon": "⚠",
        "uses_web": True,
        "uses_vision": False,
        "group": "research",
        "priority": 7,
    },
    "photos": {
        "fn": task_photos,
        "label": "Photo Analysis",
        "desc": "Vision: detect scope from listing photos. Needs photos.",
        "icon": "📷",
        "uses_web": False,
        "uses_vision": True,
        "group": "analysis",
        "priority": 8,
    },
    "verdict": {
        "fn": task_verdict,
        "label": "Global Verdict",
        "desc": "Synthesize all insights into BUY / NEGOTIATE / PASS.",
        "icon": "🧠",
        "uses_web": False,
        "uses_vision": False,
        "group": "analysis",
        "priority": 9,
    },
    "red_flags": {
        "fn": task_red_flags,
        "label": "Red Flags",
        "desc": "Detect anomalies, fraud signals, and dealbreakers.",
        "icon": "🚩",
        "uses_web": False,
        "uses_vision": False,
        "group": "analysis",
        "priority": 10,
    },
    "offer": {
        "fn": task_offer,
        "label": "Counter-Offer Strategy",
        "desc": "Suggested offer + negotiation strategy.",
        "icon": "💬",
        "uses_web": False,
        "uses_vision": False,
        "group": "action",
        "priority": 11,
    },
    "timing": {
        "fn": task_timing,
        "label": "Market Timing",
        "desc": "Best month to list the renovated property.",
        "icon": "📈",
        "uses_web": True,
        "uses_vision": False,
        "group": "action",
        "priority": 12,
    },
    "mls_listing": {
        "fn": task_mls_listing,
        "label": "MLS Listing (5 styles)",
        "desc": "Polished resale descriptions for 5 buyer personas.",
        "icon": "📝",
        "uses_web": False,
        "uses_vision": False,
        "group": "content",
        "priority": 13,
    },
    "offer_letter": {
        "fn": task_offer_letter,
        "label": "Offer Letter",
        "desc": "Personal offer letter to send the seller.",
        "icon": "📄",
        "uses_web": False,
        "uses_vision": False,
        "group": "content",
        "priority": 14,
    },
    "marketing": {
        "fn": task_marketing,
        "label": "Marketing Copy",
        "desc": "Social posts + flyer + email subjects for resale.",
        "icon": "🎯",
        "uses_web": False,
        "uses_vision": False,
        "group": "content",
        "priority": 15,
    },
}


def run_task(task_name: str, deal: dict) -> dict:
    if task_name not in TASKS:
        return {"ok": False, "error": f"Unknown task: {task_name}"}
    try:
        return TASKS[task_name]["fn"](deal)
    except Exception as e:
        log.exception("Task %s failed", task_name)
        return {"ok": False, "error": f"{e}"}


def task_registry() -> list:
    """Public catalog for the frontend."""
    out = []
    for name, t in sorted(TASKS.items(), key=lambda kv: kv[1]["priority"]):
        out.append({
            "name": name,
            "label": t["label"],
            "desc": t["desc"],
            "icon": t["icon"],
            "uses_web": t["uses_web"],
            "uses_vision": t["uses_vision"],
            "group": t["group"],
        })
    return out
