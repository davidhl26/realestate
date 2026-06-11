"""AI-powered ARV research using Claude with web search.

When the user has a property but no ARV estimate, this module asks Claude
(Sonnet 4.5 with the web_search tool) to research comparable sales in the
neighborhood and produce a low/base/high ARV with reasoning + comp list.

The API key is read from data/ai-config.json (managed via the Settings UI).
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("flip-board.ai_research")

_CONFIG_PATH: Optional[Path] = None


def set_config_path(path: Path):
    global _CONFIG_PATH
    _CONFIG_PATH = Path(path)


def _config_path() -> Path:
    if _CONFIG_PATH is None:
        raise RuntimeError("AI config path not initialized")
    return _CONFIG_PATH


def read_config() -> dict:
    p = _config_path()
    if not p.exists():
        return {}
    try:
        with open(p, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def write_config(data: dict):
    p = _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


def get_api_key() -> Optional[str]:
    return (os.environ.get("ANTHROPIC_API_KEY")
            or read_config().get("anthropic_api_key"))


def get_model() -> str:
    return read_config().get("model") or "claude-opus-4-7"


def get_maps_key() -> Optional[str]:
    """Google Maps Static / Street View API key (optional). Used to fetch an
    exterior property photo when a listing site (e.g. Zillow) blocks scraping."""
    return (os.environ.get("GOOGLE_MAPS_KEY")
            or read_config().get("google_maps_key") or None)


def is_configured() -> bool:
    return bool(get_api_key())


SYSTEM_PROMPT = """You are a real-estate analyst specializing in residential property valuation for fix-and-flip investors.

When asked to estimate ARV (After Repair Value) for a property:
1. Use the web_search tool aggressively to find 3-6 recently-sold comparable properties (last 6-12 months) within ~0.5 miles
2. Prioritize comps that match the subject in: bedroom count (±1), bathroom count (±0.5), square footage (±20%), property type, and condition (look for renovated/updated/turnkey)
3. Adjust comp prices for size differences using the local $/sqft
4. Account for local market trend (rising/falling) and average days on market
5. Produce three figures: ARV_LOW (conservative, 8th-percentile), ARV_BASE (median expectation), ARV_HIGH (top-tier finish in hot market)

CRITICAL: Output a single JSON code block with this exact schema and NO other text after it:
{
  "arv_low": <integer>,
  "arv_base": <integer>,
  "arv_high": <integer>,
  "confidence": "Low"|"Medium"|"High",
  "reasoning": "<2-3 sentence summary of methodology and key findings>",
  "comparables": [
    {"address": "<address>", "beds": <int>, "baths": <float>, "sqft": <int>, "price": <int>, "date": "<YYYY-MM>", "notes": "<short>"},
    ...
  ],
  "market_notes": "<1-2 sentences on local market conditions>",
  "warnings": ["<any caveats — small sample size, stale comps, etc.>"]
}

If you cannot find enough comps, set confidence to "Low" and explain in warnings."""


def _build_user_prompt(deal: dict) -> str:
    parts = ["Estimate the After Repair Value (ARV) for this property:\n"]
    parts.append(f"- Address: {deal.get('address', '')}")
    if deal.get("city"):
        parts.append(f"- City/State: {deal.get('city')}, {deal.get('state', '')} {deal.get('zip', '')}")
    if deal.get("property_type"):
        parts.append(f"- Type: {deal['property_type']}")
    if deal.get("beds") is not None:
        parts.append(f"- Bedrooms: {deal['beds']}")
    if deal.get("baths") is not None:
        parts.append(f"- Bathrooms: {deal['baths']}")
    if deal.get("sqft"):
        parts.append(f"- Square footage: {deal['sqft']:,}")
    if deal.get("year_built"):
        parts.append(f"- Year built: {deal['year_built']}")
    if deal.get("lot_size"):
        parts.append(f"- Lot size: {deal['lot_size']}")
    if deal.get("purchase_price"):
        parts.append(f"- Asking/purchase price: ${deal['purchase_price']:,}")
    if deal.get("rehab_base"):
        parts.append(f"- Planned rehab: ${deal['rehab_base']:,} ({deal.get('rehab_scope', 'mid-level')})")
    if deal.get("zillow_estimate"):
        parts.append(f"- Existing anchor — Zillow estimate: ${deal['zillow_estimate']:,}")
    if deal.get("realtor_estimate"):
        parts.append(f"- Existing anchor — Realtor estimate: ${deal['realtor_estimate']:,}")
    if deal.get("comp_value_estimate"):
        parts.append(f"- Existing anchor — RentCast comp avg: ${deal['comp_value_estimate']:,}")
    parts.append("")
    parts.append("Assume the property will be brought to mid-grade renovated condition (new kitchen, "
                  "updated baths, fresh flooring/paint). Estimate the value AFTER repairs.")
    return "\n".join(parts)


def _repair_json(snippet: str) -> Optional[dict]:
    """Repair Claude's malformed JSON using the json-repair library.

    Common failure modes from Opus 4.7:
      - Strings split across lines
      - Unescaped newlines / stray prose between fields
      - Trailing commas
      - Single quotes
    The json-repair library handles all of these.
    """
    try:
        import json_repair
        result = json_repair.loads(snippet)
        if isinstance(result, dict):
            return result
    except Exception as e:
        log.warning("json-repair failed: %s", e)
    return None


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object from Claude's response, with repair fallback."""
    candidates = []
    # Try ```json ... ``` fenced block first
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    # Also find any balanced { ... } block
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
                start = None

    for snippet in candidates:
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            pass
        repaired = _repair_json(snippet)
        if repaired is not None:
            log.info("JSON repaired (was malformed)")
            return repaired
    return None


def research_arv(deal: dict) -> dict:
    """Call Claude with web_search to estimate ARV."""
    api_key = get_api_key()
    if not api_key:
        return {
            "ok": False,
            "error": "No Anthropic API key configured. Add one in Settings → AI.",
        }

    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    model = get_model()

    try:
        message = client.messages.create(
            model=model,
            max_tokens=2500,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": 8}],
            messages=[{"role": "user", "content": _build_user_prompt(deal)}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key — check Settings → AI."}
    except anthropic.APIError as e:
        return {"ok": False, "error": f"Anthropic API error: {e}"}
    except Exception as e:
        log.exception("Research failed")
        return {"ok": False, "error": f"Research failed: {e}"}

    # Concatenate text blocks from the final response
    text_parts = []
    web_searches = 0
    for block in message.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        if getattr(block, "type", "") == "server_tool_use":
            web_searches += 1
    text = "\n".join(text_parts)
    data = _extract_json(text)

    if not data:
        return {
            "ok": False,
            "error": "Could not parse ARV response from AI.",
            "raw_text": text[:2000],
        }

    return {
        "ok": True,
        "arv_low": data.get("arv_low"),
        "arv_base": data.get("arv_base"),
        "arv_high": data.get("arv_high"),
        "confidence": data.get("confidence", "Medium"),
        "reasoning": data.get("reasoning", ""),
        "comparables": data.get("comparables", []),
        "market_notes": data.get("market_notes", ""),
        "warnings": data.get("warnings", []),
        "model": model,
        "web_searches_used": web_searches,
        "stop_reason": message.stop_reason,
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        },
    }
