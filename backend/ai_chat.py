"""AI Chat per deal — conversational interface with Claude.

The deal's full data + any prior AI insights are packed into the system
prompt so Claude can answer specific questions intelligently.
"""

import json
import logging
from typing import Optional

from .ai_research import get_api_key, get_model, is_configured

log = logging.getLogger("flip-board.ai_chat")

CHAT_SYSTEM = """You are a senior fix-and-flip real estate analyst embedded in the user's deal-evaluation app. You are talking ONE-ON-ONE about a specific property they have on their board.

Be:
- Concise but specific (3-6 sentences typically; use bullets for lists)
- Numbers-driven (always cite the figures from the deal data when relevant)
- Honest about uncertainty — flag when something is a guess vs. a known fact
- Practical (actionable advice, next steps)
- Domain-fluent (use terms like ARV, 70% rule, BRRRR, DSCR, MAO, cap rate naturally)

When asked subjective questions ("should I buy?", "is this a good deal?"), give your honest assessment grounded in the data. Do not hedge endlessly.

Format guidance:
- Use Markdown for structure (bold, bullets, short tables when helpful)
- Lead with the answer, then the reasoning
- Don't repeat data the user can already see — synthesize and infer

You have access to the deal's full snapshot and any prior AI analyses (ARV research, rehab estimate, etc.) — use them as ground truth."""


def _build_deal_context(deal: dict) -> str:
    """Pack the entire deal into a compact context block for the system prompt."""
    parts = ["# Deal snapshot\n"]

    # Property basics
    parts.append("## Property")
    parts.append(f"- Address: {deal.get('address', '?')}")
    parts.append(f"- City/State/Zip: {deal.get('city', '?')}, {deal.get('state', '?')} {deal.get('zip', '')}")
    parts.append(f"- Neighborhood: {deal.get('neighborhood', '?')}")
    parts.append(f"- Type: {deal.get('property_type', '?')}")
    parts.append(f"- Beds/Baths: {deal.get('beds', '?')} / {deal.get('baths', '?')}")
    parts.append(f"- Sqft: {deal.get('sqft', '?')}")
    parts.append(f"- Year built: {deal.get('year_built', '?')}")
    parts.append(f"- Lot: {deal.get('lot_size', '?')}")

    # Financials
    parts.append("\n## Financials")
    parts.append(f"- Purchase price: ${deal.get('purchase_price', 0):,}")
    parts.append(f"- ARV (base): ${deal.get('arv_base', 0):,} "
                  f"(low ${deal.get('arv_low', 0):,} / high ${deal.get('arv_high', 0):,})")
    parts.append(f"- Rehab budget: ${deal.get('rehab_base', 0):,} ({deal.get('rehab_scope', 'mid')})")
    parts.append(f"- Holding: {deal.get('holding_months', '?')} months × ${deal.get('holding_cost_monthly', 0)}/mo")
    parts.append(f"- Selling cost: {deal.get('selling_cost_pct', 8)}%")

    # External anchors
    anchors = []
    if deal.get("zillow_estimate"): anchors.append(f"Zillow ${deal['zillow_estimate']:,}")
    if deal.get("realtor_estimate"): anchors.append(f"Realtor ${deal['realtor_estimate']:,}")
    if deal.get("comp_value_estimate"): anchors.append(f"RentCast comp ${deal['comp_value_estimate']:,}")
    if anchors:
        parts.append(f"- External ARV anchors: {' • '.join(anchors)}")

    # Rental
    if deal.get("estimated_rent"):
        parts.append("\n## Rental")
        parts.append(f"- Est. monthly rent: ${deal['estimated_rent']}")
        parts.append(f"- Monthly expenses: taxes ${deal.get('monthly_taxes', 0)}, "
                      f"ins ${deal.get('monthly_insurance', 0)}, hoa ${deal.get('monthly_hoa', 0)}, "
                      f"maint ${deal.get('monthly_maintenance', 0)}, mgmt ${deal.get('monthly_mgmt', 0)}")
        parts.append(f"- Vacancy: {deal.get('vacancy_pct', 8)}%")

    # Market / neighborhood
    parts.append("\n## Market")
    parts.append(f"- YoY trend: {deal.get('market_trend_yoy_pct', '?')}%")
    parts.append(f"- Median DOM: {deal.get('median_dom', '?')} days")
    parts.append(f"- Crime rating: {deal.get('crime_rating', '?')}")
    parts.append(f"- School rating: {deal.get('school_rating', '?')}")

    # Computed score
    if deal.get("score"):
        parts.append(f"\n## Current Flip Score: {deal.get('score')}/100 (Grade {deal.get('grade')}) — Signal: {deal.get('signal')}")

    # Comparable sales (compact)
    sale_comps = deal.get("sale_comparables") or []
    if sale_comps:
        parts.append(f"\n## Sale comps ({len(sale_comps)}):")
        for c in sale_comps[:6]:
            parts.append(f"- {c.get('address', '?')} | {c.get('beds')}bd/{c.get('baths')}ba/{c.get('sqft')}sf "
                          f"| ${c.get('price', 0):,} | {c.get('date', '?')}")

    # Rental comps
    rent_comps = deal.get("rent_comparables") or []
    if rent_comps:
        parts.append(f"\n## Rent comps ({len(rent_comps)}):")
        for c in rent_comps[:6]:
            parts.append(f"- {c.get('address', '?')} | {c.get('beds')}bd/{c.get('baths')}ba | ${c.get('rent', 0)}/mo")

    # AI insights (compact)
    insights = deal.get("ai_insights") or {}
    if insights:
        parts.append("\n## Prior AI insights available:")
        for name, ins in insights.items():
            r = ins.get("result", {})
            parts.append(f"\n### {name}")
            # Keep each summary compact
            parts.append(json.dumps(r, indent=2)[:1200])

    # Description (truncated)
    desc = deal.get("description") or deal.get("notes")
    if desc:
        parts.append("\n## Description / notes")
        parts.append(desc[:1500])

    return "\n".join(parts)


def chat(deal: dict, message: str, history: Optional[list] = None) -> dict:
    """Send a chat message with full deal context. Returns {ok, reply, model, usage}."""
    if not is_configured():
        return {"ok": False, "error": "No Anthropic API key (Settings → AI)."}
    if not message or not message.strip():
        return {"ok": False, "error": "Empty message"}

    import anthropic
    client = anthropic.Anthropic(api_key=get_api_key())
    model = get_model()

    system = f"{CHAT_SYSTEM}\n\n{_build_deal_context(deal)}"

    messages = []
    for h in (history or [])[-12:]:  # last 12 messages
        role = h.get("role")
        content = h.get("content", "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message.strip()})

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1800,
            system=system,
            messages=messages,
        )
    except Exception as e:
        err_str = str(e)
        log.exception("Chat failed")
        if "credit balance" in err_str.lower() or "insufficient" in err_str.lower():
            return {"ok": False, "error": (
                "Your Anthropic account is out of credits. Top up at "
                "console.anthropic.com/settings/billing."
            ), "error_type": "billing"}
        if "authentication" in err_str.lower():
            return {"ok": False, "error": "Invalid API key.", "error_type": "auth"}
        if "rate_limit" in err_str.lower():
            return {"ok": False, "error": "Rate limited. Retry in 60s.", "error_type": "rate_limit"}
        return {"ok": False, "error": f"Chat failed: {e}", "error_type": "other"}

    reply = "\n".join(b.text for b in msg.content if hasattr(b, "text"))
    from . import ai_usage
    ai_usage.record_msg("chat", model, msg)
    return {
        "ok": True,
        "reply": reply,
        "model": model,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
    }
