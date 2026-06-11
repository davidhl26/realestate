"""Leads module — separate from "deals".

A LEAD is a motivated-seller contact you pay to acquire (typically via
iSpeedToLead, BatchLeads, etc.). The question is: is the lead price
WORTH paying given the property profile and your conversion economics?

This differs from a Deal where the price IS the property purchase price.

Storage: data/leads.json
Schema per lead:
  {
    "id": "...",
    "source": "ispeedtolead" | "batchleads" | "manual" | ...,
    "source_url": "...",
    "lead_price": <int>,                # what you'd pay to acquire the contact
    "address": "...", "city": "...", "state": "...", "zip": "...",
    "property_type": "...", "beds": <int>, "baths": <float>, "sqft": <int>,
    "year_built": <int>,
    "asking_price": <int>,              # what the seller is asking (if known)
    "estimated_arv": <int>,             # your or scraped estimate
    "estimated_rehab": <int>,
    "motivation": "...",                # 1-line summary
    "description": "...",               # full lead description
    "images": ["..."],
    "lat": <float>, "lng": <float>,
    "status": "new" | "contacted" | "appointment" | "offer" | "closed" | "passed",
    "notes": "...",
    "ai_analysis": {...},               # populated by analyze()
    "added_at": "ISO", "updated_at": "ISO",
  }
"""

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from .ai_research import get_api_key, get_model, is_configured

log = logging.getLogger("flip-board.leads")

_LOCK = threading.Lock()


def _now():
    return datetime.utcnow().isoformat() + "Z"


# Default kanban columns (used until the user customizes them).
DEFAULT_COLUMNS = [
    {"key": "new",         "label": "🆕 New",         "color": "#6b7280"},
    {"key": "contacted",   "label": "📞 Contacted",   "color": "#3b82f6"},
    {"key": "appointment", "label": "📅 Appointment", "color": "#f59e0b"},
    {"key": "offer",       "label": "💰 Offer",       "color": "#8b5cf6"},
    {"key": "closed",      "label": "✅ Closed",      "color": "#22c55e"},
    {"key": "passed",      "label": "⏭ Passed",      "color": "#ef4444"},
]
_COLOR_PALETTE = ["#6b7280", "#3b82f6", "#f59e0b", "#8b5cf6", "#22c55e",
                  "#ef4444", "#ec4899", "#14b8a6", "#eab308", "#0ea5e9"]


def _col_key(label: str, taken: set) -> str:
    """Make a unique slug key from a column label."""
    base = re.sub(r"[^a-z0-9]+", "-", (label or "col").lower()).strip("-")[:30] or "col"
    key, n = base, 1
    while key in taken:
        key = f"{base}-{n}"
        n += 1
    return key


class LeadsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"leads": [], "created": _now(), "updated": _now()})

    def _read(self) -> dict:
        with _LOCK:
            with open(self.path, "r") as f:
                return json.load(f)

    def _write(self, data: dict):
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

    def list_leads(self, status: Optional[str] = None) -> list:
        leads = self._read().get("leads", [])
        if status:
            leads = [l for l in leads if l.get("status") == status]
        # Sort newest first
        leads.sort(key=lambda l: l.get("added_at", ""), reverse=True)
        return leads

    def get_lead(self, lead_id: str) -> Optional[dict]:
        return next((l for l in self._read().get("leads", [])
                       if l["id"] == lead_id), None)

    def upsert_lead(self, lead: dict) -> dict:
        data = self._read()
        if not lead.get("id"):
            lead["id"] = str(uuid.uuid4())[:8]
            lead["added_at"] = _now()
        lead["updated_at"] = _now()
        idx = next((i for i, l in enumerate(data["leads"])
                     if l["id"] == lead["id"]), None)
        if idx is None:
            data["leads"].append(lead)
        else:
            # preserve original add date
            lead["added_at"] = data["leads"][idx].get("added_at", lead["updated_at"])
            data["leads"][idx] = lead
        data["updated"] = _now()
        self._write(data)
        return lead

    def delete_lead(self, lead_id: str) -> bool:
        data = self._read()
        before = len(data["leads"])
        data["leads"] = [l for l in data["leads"] if l["id"] != lead_id]
        if len(data["leads"]) < before:
            data["updated"] = _now()
            self._write(data)
            return True
        return False

    # ---- Kanban columns (customizable) ----
    def get_columns(self) -> list:
        cols = self._read().get("columns")
        return cols if cols else [dict(c) for c in DEFAULT_COLUMNS]

    def set_columns(self, columns: list) -> list:
        """Persist the column list. Sanitizes keys/colors and reassigns any
        lead whose status no longer matches a column to the first column."""
        clean, taken = [], set()
        for i, c in enumerate(columns or []):
            if not isinstance(c, dict):
                continue
            label = (c.get("label") or "").strip()
            if not label:
                continue
            key = (c.get("key") or "").strip() or _col_key(label, taken)
            if key in taken:
                key = _col_key(label, taken)
            taken.add(key)
            color = c.get("color") or _COLOR_PALETTE[i % len(_COLOR_PALETTE)]
            clean.append({"key": key, "label": label, "color": color})
        if not clean:
            clean = [dict(c) for c in DEFAULT_COLUMNS]
        data = self._read()
        data["columns"] = clean
        valid = {c["key"] for c in clean}
        fallback = clean[0]["key"]
        for l in data["leads"]:
            if l.get("status") not in valid:
                l["status"] = fallback
        data["updated"] = _now()
        self._write(data)
        return clean

    # ---- Comments on a lead ----
    def add_comment(self, lead_id: str, text: str) -> Optional[dict]:
        data = self._read()
        lead = next((l for l in data["leads"] if l["id"] == lead_id), None)
        if not lead:
            return None
        lead.setdefault("comments", []).append({
            "id": str(uuid.uuid4())[:8], "text": text, "created_at": _now(),
        })
        lead["updated_at"] = _now()
        data["updated"] = _now()
        self._write(data)
        return lead

    def delete_comment(self, lead_id: str, comment_id: str) -> Optional[dict]:
        data = self._read()
        lead = next((l for l in data["leads"] if l["id"] == lead_id), None)
        if not lead:
            return None
        lead["comments"] = [c for c in lead.get("comments", []) if c.get("id") != comment_id]
        lead["updated_at"] = _now()
        data["updated"] = _now()
        self._write(data)
        return lead

    def aggregates(self) -> dict:
        leads = self._read().get("leads", [])
        by_status = {}
        for l in leads:
            s = l.get("status", "new")
            by_status[s] = by_status.get(s, 0) + 1
        total_spent = sum(l.get("lead_price", 0) for l in leads if l.get("status") != "passed")
        worth_buys = sum(1 for l in leads if (l.get("ai_analysis") or {}).get("recommendation", "").upper().startswith("BUY"))
        return {
            "total": len(leads),
            "total_spent_on_leads": total_spent,
            "by_status": by_status,
            "worth_buying": worth_buys,
        }


# ============================================================================
# AI LEAD ANALYZER
# ============================================================================

LEAD_ANALYZER_SYSTEM = """You are a wholesale real estate analyst evaluating whether a LEAD is worth buying.

A LEAD is a motivated-seller contact (name, phone, address, brief property info). The user pays a lead-acquisition fee (LEAD PRICE) to get the contact details — they do NOT yet own or have contracted the property. Your job is to assess whether paying that fee is likely to be profitable.

Apply the realistic wholesale economics:
- Average wholesaler-to-buyer conversion rate on motivated-seller leads: 3-8%
- Average wholesale assignment fee on a successful close: $5,000–$15,000 (varies by market)
- Therefore: expected value per lead = (conversion rate × avg assignment fee) − lead cost
- Break-even rule of thumb: lead price ≤ 2-3% of expected ARV-rehab spread

Critical lead-quality signals:
- Asking price vs estimated ARV (more discount = better lead)
- Distress indicators (probate, divorce, tired landlord, vacant, foreclosure, code violations)
- Property condition (light reno → easier flip, heavy gut → harder)
- Specificity of details (vague = stale lead; specific = fresh)
- Photos quality and quantity
- Has the seller already named a price?

Use web_search if helpful to estimate the ARV / rehab in the area when not provided.

Output a single JSON object — NO other text after:
{
  "recommendation": "BUY LEAD"|"MAYBE — NEGOTIATE LEAD PRICE"|"PASS",
  "confidence": "Low"|"Medium"|"High",
  "estimated_arv": <int|null>,
  "estimated_rehab": <int|null>,
  "estimated_spread": <int|null>,
  "expected_assignment_fee": <int|null>,
  "fair_lead_price_max": <int>,        // maximum you should pay for this lead
  "ev_estimate": <int>,                // expected value = (conversion_pct/100) × assignment_fee − lead_price
  "conversion_likelihood_pct": <int>,  // 1-15 — your estimate of % chance you can close + assign
  "top_3_positive": ["...", "...", "..."],
  "top_3_concerns": ["...", "...", "..."],
  "motivation_signal": "Low"|"Medium"|"High"|"Very High",
  "next_steps_if_buy": ["...", "...", "..."],
  "verdict_summary": "<1-2 sentences: should they buy this lead, and why>"
}"""


def _build_lead_prompt(lead: dict) -> str:
    p = ["Evaluate whether this lead is worth buying.\n"]
    p.append(f"- Lead price (acquisition cost): ${lead.get('lead_price', 0):,}")
    if lead.get("source"):
        p.append(f"- Lead source: {lead['source']}")
    p.append("")
    p.append("## Property")
    p.append(f"- Address: {lead.get('address', '?')}")
    if lead.get("city"):
        p.append(f"- City/State: {lead['city']}, {lead.get('state', '')} {lead.get('zip', '')}")
    if lead.get("property_type"):
        p.append(f"- Type: {lead['property_type']}")
    if lead.get("beds") is not None:
        p.append(f"- Beds: {lead['beds']}")
    if lead.get("baths") is not None:
        p.append(f"- Baths: {lead['baths']}")
    if lead.get("sqft"):
        p.append(f"- Sq ft: {lead['sqft']:,}")
    if lead.get("year_built"):
        p.append(f"- Year built: {lead['year_built']}")
    if lead.get("asking_price"):
        p.append(f"- Seller's asking price: ${lead['asking_price']:,}")
    if lead.get("estimated_arv"):
        p.append(f"- Estimated ARV (user input): ${lead['estimated_arv']:,}")
    if lead.get("estimated_rehab"):
        p.append(f"- Estimated rehab (user input): ${lead['estimated_rehab']:,}")
    if lead.get("motivation"):
        p.append(f"\n## Motivation hint\n{lead['motivation']}")
    if lead.get("description"):
        p.append(f"\n## Description / notes\n{lead['description'][:2500]}")
    if lead.get("notes"):
        p.append(f"\n## My notes\n{lead['notes'][:1000]}")
    p.append("\n\nUse web_search if helpful to estimate local ARV and rehab costs. Then provide the JSON verdict.")
    return "\n".join(p)


def _extract_json(text: str) -> Optional[dict]:
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fallback: first { ... } block
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    return None


def analyze_lead(lead: dict) -> dict:
    if not is_configured():
        return {"ok": False, "error": "AI not configured. Add API key in Settings → AI."}
    if not lead.get("lead_price"):
        return {"ok": False, "error": "lead_price is required to evaluate worth."}

    import anthropic
    client = anthropic.Anthropic(api_key=get_api_key())
    model = get_model()

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=2500,
            system=LEAD_ANALYZER_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            messages=[{"role": "user", "content": _build_lead_prompt(lead)}],
        )
    except Exception as e:
        log.exception("Lead analysis failed")
        err_str = str(e)
        if "credit balance" in err_str.lower() or "insufficient" in err_str.lower():
            return {"ok": False, "error": "Out of Anthropic credits. Top up at console.anthropic.com/settings/billing.",
                    "error_type": "billing"}
        if "authentication" in err_str.lower():
            return {"ok": False, "error": "Invalid API key.", "error_type": "auth"}
        if "rate_limit" in err_str.lower():
            return {"ok": False, "error": "Rate limited. Retry in 60s.", "error_type": "rate_limit"}
        return {"ok": False, "error": f"Analysis failed: {e}", "error_type": "other"}

    text_parts, web = [], 0
    for block in msg.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        if getattr(block, "type", "") == "server_tool_use":
            web += 1
    text = "\n".join(text_parts)
    parsed = _extract_json(text)
    if not parsed:
        return {"ok": False, "error": "Could not parse AI response.", "raw": text[:2000]}

    return {
        "ok": True,
        "result": parsed,
        "model": model,
        "web_searches_used": web,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
        "ran_at": _now(),
    }
