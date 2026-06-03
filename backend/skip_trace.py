"""Auto skip-trace for foreclosure auction items.

Given an auction item (address + case # + defendant name from court records),
ask Claude with web_search to find the property owner's:
  - full name (verified from public records)
  - phone number(s)
  - email if available
  - mailing address (often different from property address for absentee owners)

Sources Claude can hunt through:
  - county property appraiser sites (owner of record)
  - whitepages.com, truepeoplesearch.com, fastpeoplesearch.com
  - LinkedIn / Facebook for cross-reference
  - business records if the owner is an LLC

Output is structured JSON and gets merged onto the auction item, which moves
to status="traced".
"""
import json
import logging
import re
from typing import Optional

log = logging.getLogger("flip-board.skip_trace")


SYSTEM_PROMPT = """You are a real-estate skip-trace specialist. Given a property
under foreclosure or tax-deed auction, your job is to find the current legal
owner's contact information from PUBLIC records.

Use the web_search tool aggressively. Recommended sources (in priority order):
1. County Property Appraiser sites — they list the OWNER OF RECORD by full name
   and mailing address (which may differ from the property address for absentee owners)
2. Florida Sunbiz (sunbiz.org) — if the owner is an LLC, look up its registered agent
   and managers, which surfaces real human names
3. Public phone directories: truepeoplesearch.com, fastpeoplesearch.com, whitepages.com
4. LinkedIn for verification
5. Court case filings (referenced by the case #) — defendant names match the owner

CRITICAL RULES:
- ONLY use PUBLIC records. Do not fabricate phone numbers.
- If you can't find a verified phone, return phones: [] (empty array) — never invent one.
- Note your confidence per field: HIGH (verified from official source) / MEDIUM (likely match) / LOW (educated guess)
- For LLCs, surface the human(s) behind it: registered agent, manager(s)

Output a single JSON code block with this schema, NO other text after:
{
  "owner_name": "<verified full name or LLC name>",
  "owner_type": "individual" | "llc" | "trust" | "unknown",
  "owner_humans": [
    {"name": "<full name>", "role": "owner|manager|registered_agent|trustee", "source": "<URL or source>"}
  ],
  "phones": [
    {"number": "<+1-XXX-XXX-XXXX>", "type": "mobile|landline|business", "confidence": "HIGH|MEDIUM|LOW", "source": "<URL>"}
  ],
  "emails": [
    {"email": "<email>", "confidence": "HIGH|MEDIUM|LOW", "source": "<URL>"}
  ],
  "mailing_address": "<owner mailing address if different from property address>",
  "owner_age": <int or null>,
  "associated_addresses": ["<addr>", ...],
  "associated_people": ["<name>", ...],
  "notes": "<2-4 sentence summary of what you found and any caveats>",
  "confidence_overall": "HIGH|MEDIUM|LOW",
  "warnings": ["<flags — common name, conflicting records, etc>"]
}

If you find NOTHING after thorough search, still emit the JSON with empty arrays
and confidence_overall=LOW + a clear warning."""


def _build_user_prompt(item: dict) -> str:
    parts = [
        "Skip-trace the legal owner of this property under foreclosure auction:\n",
        f"- Property address: {item.get('address', 'unknown')}",
    ]
    if item.get("parcel_id"):
        parts.append(f"- Parcel/Folio ID: {item['parcel_id']}")
    if item.get("case_number"):
        parts.append(f"- Court case #: {item['case_number']} "
                      f"(use this to look up court filings)")
    if item.get("defendant"):
        parts.append(f"- Defendant name (from court records): {item['defendant']}")
    if item.get("plaintiff"):
        parts.append(f"- Plaintiff (lender): {item['plaintiff']}")
    if item.get("auction_type"):
        parts.append(f"- Auction type: {item['auction_type']}")
    if item.get("source_url"):
        parts.append(f"- Source listing: {item['source_url']}")

    parts.append("\nFind the CURRENT legal owner (which may be the defendant, "
                  "but verify against the county property appraiser). Return their "
                  "name + best phone number + mailing address. If owner is an LLC, "
                  "drill through to the human registered agent/manager.")
    parts.append("\nBe thorough — search multiple sources to cross-verify. "
                  "Aim for at least one HIGH-confidence phone number if any exists.")
    return "\n".join(parts)


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of an AI response."""
    if not text:
        return None
    # Try fenced ```json ... ``` block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    # Otherwise find the first balanced { } object
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                snippet = text[start:i + 1]
                try: return json.loads(snippet)
                except json.JSONDecodeError: start = None
    return None


def skip_trace_item(item: dict) -> dict:
    """Run Claude+web_search to find owner contact info for one auction item.

    Returns a dict that gets merged onto the auction item. Includes:
      ok: bool
      owner_name, phones, emails, mailing_address, …
      model, web_searches_used, usage
    """
    from . import ai_research

    api_key = ai_research.get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured."}
    if not (item.get("address") or item.get("case_number") or item.get("defendant")):
        return {"ok": False, "error": "Need at least an address/case#/defendant to trace."}

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = ai_research.get_model()

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=3500,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 12,
            }],
            messages=[{"role": "user", "content": _build_user_prompt(item)}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key", "error_type": "auth"}
    except anthropic.BadRequestError as e:
        return {"ok": False, "error": f"Bad request: {e}", "error_type": "bad_request"}
    except anthropic.RateLimitError as e:
        return {"ok": False, "error": f"Rate limited: {e}", "error_type": "rate_limit"}
    except anthropic.APIStatusError as e:
        if e.status_code == 402:
            return {"ok": False, "error": "Out of credits", "error_type": "billing"}
        if e.status_code == 503:
            return {"ok": False, "error": "Overloaded", "error_type": "overloaded"}
        return {"ok": False, "error": f"API error: {e}"}
    except Exception as e:
        log.exception("Skip-trace failed")
        return {"ok": False, "error": f"Skip-trace failed: {e}"}

    text_parts = []
    web_searches = 0
    for block in msg.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        if getattr(block, "type", "") == "server_tool_use":
            web_searches += 1
    text = "\n".join(text_parts)
    data = _extract_json(text)
    if not data:
        return {
            "ok": False,
            "error": "Could not parse skip-trace response from AI.",
            "raw_text": text[:2000],
        }

    # Pull the best phone/email for quick display
    phones = data.get("phones") or []
    emails = data.get("emails") or []
    best_phone = None
    for p in phones:
        if isinstance(p, dict) and p.get("number"):
            if not best_phone or p.get("confidence") == "HIGH":
                best_phone = p["number"]
    best_email = None
    for e in emails:
        if isinstance(e, dict) and e.get("email"):
            if not best_email or e.get("confidence") == "HIGH":
                best_email = e["email"]

    return {
        "ok": True,
        "owner_name": data.get("owner_name"),
        "owner_type": data.get("owner_type"),
        "owner_humans": data.get("owner_humans") or [],
        "phones": phones,
        "emails": emails,
        "owner_phone": best_phone,
        "owner_email": best_email,
        "mailing_address": data.get("mailing_address"),
        "owner_age": data.get("owner_age"),
        "associated_addresses": data.get("associated_addresses") or [],
        "associated_people": data.get("associated_people") or [],
        "notes": data.get("notes"),
        "confidence_overall": data.get("confidence_overall"),
        "warnings": data.get("warnings") or [],
        "model": model,
        "web_searches_used": web_searches,
        "usage": {
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
        },
    }
