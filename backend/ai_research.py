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


def get_scraper_proxy_key() -> Optional[str]:
    """ScraperAPI key (optional). When set, Zillow/Redfin fetches route through
    a residential-IP proxy that solves PerimeterX, returning the full listing
    page WITH all photos. Free tier ~1000 requests/month."""
    return (os.environ.get("SCRAPER_API_KEY")
            or read_config().get("scraper_api_key") or None)


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


REHAB_SYSTEM = """You are a fix-and-flip renovation estimator. Given a property, produce a realistic, itemized renovation budget to bring it to clean mid-grade rent-ready / sellable condition.

Rules:
- Localize costs to the property's market (use web_search for local labor/material costs if useful).
- Base the scope on the beds/baths, square footage, age, and any stated condition or photo findings.
- Output 6-12 concrete line items (e.g., "Kitchen — cabinets, counters, appliances", "Bathroom x2 — vanity, tile, fixtures", "Flooring — LVP throughout", "Interior paint", "Roof", "HVAC", "Electrical panel & fixtures", "Plumbing updates", "Windows", "Exterior / curb appeal").
- Do NOT include a contingency line (the app adds it separately).

CRITICAL: End with a SINGLE JSON code block, no text after:
```json
{ "items": [ {"label": "<short work description>", "cost": <integer USD>} ], "summary": "<1-2 sentences on scope/level>" }
```"""


def _build_rehab_prompt(deal: dict) -> str:
    p = ["Estimate an itemized fix-and-flip renovation budget for this property:\n"]
    p.append(f"- Address: {deal.get('address','')}")
    if deal.get("city"): p.append(f"- City/State: {deal.get('city')}, {deal.get('state','')} {deal.get('zip','')}")
    if deal.get("property_type"): p.append(f"- Type: {deal['property_type']}")
    if deal.get("beds") is not None: p.append(f"- Beds: {deal['beds']}")
    if deal.get("baths") is not None: p.append(f"- Baths: {deal['baths']}")
    if deal.get("sqft"): p.append(f"- Square footage: {deal['sqft']}")
    if deal.get("year_built"): p.append(f"- Year built: {deal['year_built']}")
    if deal.get("rehab_scope"): p.append(f"- Intended scope: {deal['rehab_scope']}")
    if deal.get("notes"): p.append(f"- Notes / condition: {str(deal['notes'])[:1500]}")
    # Reuse any photo-analysis rehab findings if present
    ai = deal.get("ai_insights") or {}
    photos = (ai.get("photos") or {}) if isinstance(ai, dict) else {}
    if isinstance(photos, dict) and photos.get("rehab_complexity"):
        p.append(f"- Photo analysis rehab complexity: {photos.get('rehab_complexity')}")
    p.append("\nAssume mid-grade finishes (new kitchen, updated baths, fresh flooring/paint). "
             "Return the JSON itemized budget.")
    return "\n".join(p)


def estimate_rehab(deal: dict) -> dict:
    """Call Claude (web_search) to produce an itemized rehab budget."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured. Add one in Settings → AI."}
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = get_model()
    try:
        message = client.messages.create(
            model=model, max_tokens=2000, system=REHAB_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}],
            messages=[{"role": "user", "content": _build_rehab_prompt(deal)}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key — check Settings → AI."}
    except Exception as e:
        log.exception("Rehab estimate failed")
        return {"ok": False, "error": f"Rehab estimate failed: {e}"}
    text = "\n".join(b.text for b in message.content if hasattr(b, "text"))
    data = _extract_json(text)
    if not data or not isinstance(data.get("items"), list):
        return {"ok": False, "error": "Could not parse rehab estimate.", "raw_text": text[:1500]}
    items = []
    for it in data["items"]:
        if isinstance(it, dict) and it.get("label"):
            try:
                items.append({"label": str(it["label"])[:120], "cost": int(round(float(it.get("cost") or 0)))})
            except (TypeError, ValueError):
                items.append({"label": str(it["label"])[:120], "cost": 0})
    return {"ok": True, "items": items, "summary": data.get("summary", ""), "model": model}


AUCTION_SYSTEM = """You are evaluating a property listed at a foreclosure / real-estate AUCTION for a fix-and-flip investor. Given the address and any condition notes/comments from the listing, estimate two numbers and summarize.

Use web_search for recently-sold comparables and local rehab costs.

Return a SINGLE JSON code block, no text after:
```json
{
  "arv": <integer USD — After-Repair Value, mid-grade flip>,
  "arv_confidence": "Low"|"Medium"|"High",
  "rehab": <integer USD — renovation budget to reach that ARV, informed by the condition notes>,
  "condition_summary": "<1-2 sentences on likely condition / scope>",
  "risks": ["<auction-specific risk, e.g. occupied/eviction, liens, no inspection, cash-only>", "..."],
  "summary": "<1-2 sentence take>"
}
```
Be conservative: auctions are sold as-is, often occupied, no interior inspection — lean slightly higher on rehab and add the relevant risks."""


def analyze_auction(data: dict) -> dict:
    """Estimate ARV + rehab for an auction property (Claude + web search)."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured. Add one in Settings → AI."}
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = get_model()
    parts = [f"Auction property: {data.get('address','')}"]
    for k, lbl in [("beds", "Beds"), ("baths", "Baths"), ("sqft", "Sq ft"), ("year_built", "Year built")]:
        if data.get(k):
            parts.append(f"- {lbl}: {data[k]}")
    if data.get("opening_bid"):
        parts.append(f"- Opening/current bid: ${int(data['opening_bid']):,}")
    if data.get("comments"):
        parts.append(f"\nListing notes / comments:\n{str(data['comments'])[:2000]}")
    parts.append("\nEstimate ARV and rehab, then return the JSON.")
    try:
        msg = client.messages.create(
            model=model, max_tokens=1800, system=AUCTION_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
            messages=[{"role": "user", "content": "\n".join(parts)}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key — check Settings → AI."}
    except Exception as e:
        log.exception("Auction analysis failed")
        return {"ok": False, "error": f"Auction analysis failed: {e}"}
    text = "\n".join(b.text for b in msg.content if hasattr(b, "text"))
    d = _extract_json(text)
    if not d:
        return {"ok": False, "error": "Could not parse auction analysis.", "raw_text": text[:1500]}
    def _int(v):
        try: return int(round(float(v)))
        except (TypeError, ValueError): return None
    return {
        "ok": True,
        "arv": _int(d.get("arv")),
        "arv_confidence": d.get("arv_confidence", "Low"),
        "rehab": _int(d.get("rehab")),
        "condition_summary": d.get("condition_summary", ""),
        "risks": d.get("risks", []) if isinstance(d.get("risks"), list) else [],
        "summary": d.get("summary", ""),
        "model": model,
    }


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


# ============================================================================
# Area listing search — expand a Zillow search URL into individual listings
# using Claude + web search (free alternative to a premium scraping proxy).
# ============================================================================

LISTING_SEARCH_SYSTEM = """You are a real-estate research assistant for a fix-and-flip investor. Given a geographic area and filters, you find the NEWEST residential properties currently for sale and return their exact Zillow listing URLs.

Method:
1. Use the web_search tool aggressively (run many searches). Favor queries that surface the most recently listed homes: "newest homes for sale <city> under $<price>", "<city> new listings zillow", "<zip> houses just listed for sale", plus per-suburb/per-zip variations to widen coverage.
2. For EACH matching property, find its exact Zillow listing URL: https://www.zillow.com/homedetails/<street-city-state-zip-slug>/<zpid>_zpid/ . If a search snippet shows the address but not the URL, run a follow-up search like "<full address> zillow" to retrieve the exact listing URL. A Redfin listing URL (https://www.redfin.com/.../home/<id>) is also accepted.
3. Capture whatever price / beds / baths / sqft / days-on-market the result shows.

RULES:
- Return the MOST RECENTLY LISTED properties first (newest on market). The caller wants the freshest listings, then scrapes each individual listing page (for photos and full data).
- Strongly prefer providing a real listing URL for every property — that is what lets the system open and scrape the listing. Only set "url": null when you genuinely could not find the URL after searching for that address.
- Do NOT fabricate. Only return addresses and URLs that actually appeared in your search results. Never invent a zpid or guess a URL.
- Include candidates even if you cannot confirm they are still active — the system fetches each live page afterward and discards any that have already sold. Breadth + freshness matter more than certainty.
- Respect the price ceiling/floor and the property-type exclusions exactly (single-family houses only when condos/townhouses/land/etc. are excluded).

For EACH listing also estimate (best-effort, from the listing, comps, or public records):
- year_built (integer) and last_renovated (the year of the most recent major renovation/permit, integer, or null if unknown — check the description for "updated/renovated in YYYY")
- arv_estimate: a rough After-Repair Value for a mid-grade flip in that area (integer USD)
- rehab_estimate: a rough renovation budget to reach that ARV (integer USD)
These are quick estimates for triage, not appraisals.

CRITICAL: End your response with a SINGLE JSON code block in exactly this schema and NO text after it:
```json
{
  "area_label": "<human name of the area, e.g. 'Cleveland, OH — East Side & inner-ring suburbs'>",
  "listings": [
    {"url": "<full zillow/redfin listing url, or null>", "address": "<full street address>", "city": "<city>", "state": "<2-letter>", "zip": "<zip>", "price": <integer or null>, "beds": <integer or null>, "baths": <number or null>, "sqft": <integer or null>, "year_built": <integer or null>, "last_renovated": <integer year or null>, "arv_estimate": <integer or null>, "rehab_estimate": <integer or null>, "days_on_market": <integer or null>}
  ],
  "notes": "<1-2 sentences: how many found, coverage caveats>"
}
```"""


def _find_listings_payload(text: str) -> Optional[dict]:
    """Pull the JSON object that holds the `listings` array. Unlike _extract_json
    (which returns the FIRST parseable object), this prefers the block that
    actually contains "listings" — the model often emits other small JSON
    snippets earlier in its reasoning."""
    candidates = []
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidates.append(text[start:i + 1])
                start = None
    # Prefer candidates that mention "listings", longest first.
    candidates.sort(key=lambda c: ('"listings"' not in c, -len(c)))
    for c in candidates:
        if '"listings"' not in c:
            continue
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            rep = _repair_json(c)
            if rep is not None:
                return rep
    return None


def _salvage_listings(text: str) -> list:
    """Last-resort: pull individual listing objects even if the surrounding JSON
    array was truncated (response hit max_tokens). Listing objects are flat, so
    a non-nested {...} containing an "address" field is a safe match."""
    out = []
    for m in re.finditer(r'\{[^{}]*?"address"\s*:\s*"[^"]+?"[^{}]*?\}', text, re.DOTALL):
        snippet = m.group(0)
        obj = None
        try:
            obj = json.loads(snippet)
        except json.JSONDecodeError:
            obj = _repair_json(snippet)
        if isinstance(obj, dict) and obj.get("address"):
            out.append(obj)
    return out


def _build_listing_search_prompt(params: dict, max_listings: int = 60) -> str:
    p = params or {}
    lines = [f"Find the {max_listings} NEWEST residential properties currently FOR SALE in this area "
             "(most recently listed first).\n"]
    clat, clng = p.get("center_lat"), p.get("center_lng")
    mb = p.get("map_bounds") or {}
    if p.get("search_term"):
        lines.append(f"- User's search term: {p['search_term']}")
    if clat is not None and clng is not None:
        lines.append(f"- Map area center: latitude {clat:.4f}, longitude {clng:.4f}")
    if mb:
        lines.append(f"- Bounding box: lat {mb.get('south')}…{mb.get('north')}, "
                     f"lng {mb.get('west')}…{mb.get('east')}")
    if mb or (clat is not None and clng is not None):
        lines.append("  → First identify which city / neighborhoods / suburbs this box covers, "
                     "then search each of them.")
    elif p.get("search_term"):
        lines.append("  → Search this location and its neighborhoods/suburbs/zip codes.")
    if p.get("price_max"):
        lines.append(f"- Maximum price: ${int(p['price_max']):,}")
    if p.get("price_min"):
        lines.append(f"- Minimum price: ${int(p['price_min']):,}")
    if p.get("beds_min"):
        lines.append(f"- Minimum bedrooms: {p['beds_min']}")
    if p.get("baths_min"):
        lines.append(f"- Minimum bathrooms: {p['baths_min']}")
    if p.get("sqft_min"):
        lines.append(f"- Minimum square footage: {p['sqft_min']}")
    if p.get("property_type"):
        lines.append(f"- Property type: {p['property_type']} only")
    if p.get("excluded_type_labels"):
        lines.append(f"- EXCLUDE these property types: {', '.join(p['excluded_type_labels'])} "
                     "(i.e. single-family houses only).")
    lines.append("")
    lines.append(f"Return the {max_listings} most recently listed matching properties, newest first, "
                 "EACH with its exact Zillow listing URL (search '<address> zillow' to get the URL "
                 "when a result only shows the address), in the JSON schema specified.")
    return "\n".join(lines)


def find_listings_in_area(params: dict, max_listings: int = 60) -> dict:
    """Use Claude + web search to find for-sale listings in a geographic area.

    `params` comes from scraper.parse_zillow_search_url(). Returns
    {ok, area_label, listings: [{url, address, city, state, zip, price, beds,
    baths, sqft}], notes, web_searches_used, model} or {ok: False, error}."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured. Add one in Settings → AI."}

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = get_model()

    try:
        message = client.messages.create(
            model=model,
            max_tokens=16000,
            system=LISTING_SEARCH_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
            messages=[{"role": "user", "content": _build_listing_search_prompt(params, max_listings)}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key — check Settings → AI."}
    except anthropic.APIError as e:
        return {"ok": False, "error": f"Anthropic API error: {e}"}
    except Exception as e:
        log.exception("Listing search failed")
        return {"ok": False, "error": f"Listing search failed: {e}"}

    text_parts, web_searches = [], 0
    for block in message.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        if getattr(block, "type", "") == "server_tool_use":
            web_searches += 1
    text = "\n".join(text_parts)

    data = _find_listings_payload(text)
    raw_listings = (data or {}).get("listings") if data else None
    area_label = (data or {}).get("area_label", "") if data else ""
    notes = (data or {}).get("notes", "") if data else ""
    # Fallback: salvage individual objects if the array was truncated/unparseable.
    if not raw_listings:
        salvaged = _salvage_listings(text)
        if salvaged:
            raw_listings = salvaged
            if not notes:
                notes = "(recovered from a truncated response)"
    if not raw_listings:
        return {"ok": False,
                "error": "Could not parse listing results from AI.",
                "stop_reason": getattr(message, "stop_reason", None),
                "raw_text": text[-2000:]}

    # Clean + de-dupe listings
    seen, listings = set(), []
    for it in (raw_listings or []):
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or "").strip()
        addr = (it.get("address") or "").strip()
        key = (url.lower() or addr.lower())
        if not key or key in seen:
            continue
        if not (url or addr):
            continue
        seen.add(key)
        listings.append({
            "url": url,
            "address": addr,
            "city": (it.get("city") or "").strip(),
            "state": (it.get("state") or "").strip(),
            "zip": (str(it.get("zip") or "")).strip(),
            "price": it.get("price"),
            "beds": it.get("beds"),
            "baths": it.get("baths"),
            "sqft": it.get("sqft"),
            "year_built": it.get("year_built"),
            "last_renovated": it.get("last_renovated"),
            "arv_estimate": it.get("arv_estimate"),
            "rehab_estimate": it.get("rehab_estimate"),
            "days_on_market": it.get("days_on_market"),
        })
        if len(listings) >= max_listings:
            break

    return {
        "ok": True,
        "area_label": area_label,
        "listings": listings,
        "notes": notes,
        "model": model,
        "web_searches_used": web_searches,
        "usage": {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        },
    }


AUCTION_SEARCH_SYSTEM = """You help a fix-and-flip investor DISCOVER residential properties going to AUCTION / foreclosure sale in a given city or state, then triage them.

Use the web_search tool aggressively. Search sources like: auction.com, Hubzu, Xome, RealtyTrac, Foreclosure.com, county sheriff-sale / tax-foreclosure lists, and Zillow's "foreclosures / auctions" filter. Run many queries (per city, per county, per source).

For EACH property found, capture whatever the result shows and estimate the rest (best-effort, for triage — not appraisals):
- address (full street address if available; auction sites often hide the house number — include the street + city + zip you can see), city, state (2-letter), zip
- opening_bid OR current_bid (integer USD, or null)
- auction_date (ISO YYYY-MM-DD if shown, else null)
- beds, baths, sqft, year_built
- arv_estimate: rough After-Repair Value for a mid-grade flip in that area (integer USD)
- rehab_estimate: rough renovation budget to reach that ARV — auctions are as-is/occupied, lean higher (integer USD)
- property_type, source (e.g. "auction.com"), url (listing URL if available, else null)

RULES:
- Do NOT fabricate addresses. Only return properties that actually appeared in your searches. If you cannot find specific auction listings, return fewer (or zero) rather than inventing them.
- It's fine if opening_bid / auction_date are null — discovery + an ARV/rehab estimate is the priority.
- Prefer properties with an upcoming auction date.

CRITICAL: End with a SINGLE JSON code block in exactly this schema and NO text after it:
```json
{
  "area_label": "<human name of the area searched>",
  "listings": [
    {"address": "<street/area>", "city": "<city>", "state": "<2-letter>", "zip": "<zip or null>", "opening_bid": <integer or null>, "auction_date": "<YYYY-MM-DD or null>", "beds": <integer or null>, "baths": <number or null>, "sqft": <integer or null>, "year_built": <integer or null>, "arv_estimate": <integer or null>, "rehab_estimate": <integer or null>, "property_type": "<type or null>", "source": "<site>", "url": "<url or null>"}
  ],
  "notes": "<1-2 sentences: how many found, coverage caveats>"
}
```"""


def _build_auction_search_prompt(params: dict, max_listings: int) -> str:
    p = params or {}
    loc = p.get("search_term") or p.get("location") or ""
    lines = [f"Find up to {max_listings} residential properties going to AUCTION / foreclosure sale in: {loc}.",
             "Search auction.com, Hubzu, county sheriff/tax sales, and Zillow foreclosures for this location "
             "and its counties/cities/zip codes."]
    if p.get("price_max"):
        lines.append(f"- Target opening bid / value under: ${int(p['price_max']):,}")
    if p.get("beds_min"):
        lines.append(f"- Minimum bedrooms: {p['beds_min']}")
    if p.get("property_type"):
        lines.append(f"- Property type: {p['property_type']} only")
    lines.append("")
    lines.append(f"Return up to {max_listings} auction properties with ARV + rehab estimates, in the JSON schema specified.")
    return "\n".join(lines)


def find_auctions_in_area(params: dict, max_listings: int = 20) -> dict:
    """Use Claude + web search to DISCOVER auction/foreclosure listings in a
    city or state. Returns {ok, area_label, listings:[...], notes, model}."""
    api_key = get_api_key()
    if not api_key:
        return {"ok": False, "error": "No Anthropic API key configured. Add one in Settings → AI."}
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    model = get_model()
    try:
        message = client.messages.create(
            model=model, max_tokens=16000, system=AUCTION_SEARCH_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
            messages=[{"role": "user", "content": _build_auction_search_prompt(params, max_listings)}],
        )
    except anthropic.AuthenticationError:
        return {"ok": False, "error": "Invalid API key — check Settings → AI."}
    except anthropic.APIError as e:
        return {"ok": False, "error": f"Anthropic API error: {e}"}
    except Exception as e:
        log.exception("Auction search failed")
        return {"ok": False, "error": f"Auction search failed: {e}"}

    text_parts, web_searches = [], 0
    for block in message.content:
        if hasattr(block, "text"):
            text_parts.append(block.text)
        if getattr(block, "type", "") == "server_tool_use":
            web_searches += 1
    text = "\n".join(text_parts)

    data = _find_listings_payload(text)
    raw = (data or {}).get("listings") if data else None
    area_label = (data or {}).get("area_label", "") if data else ""
    notes = (data or {}).get("notes", "") if data else ""
    if not raw:
        salvaged = _salvage_listings(text)
        if salvaged:
            raw, notes = salvaged, (notes or "(recovered from a truncated response)")
    if not raw:
        return {"ok": False, "error": "Could not parse auction results from AI.",
                "stop_reason": getattr(message, "stop_reason", None),
                "raw_text": text[-2000:]}

    seen, listings = set(), []
    for it in raw:
        if not isinstance(it, dict):
            continue
        addr = (it.get("address") or "").strip()
        url = (it.get("url") or "").strip()
        key = (addr.lower() or url.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        listings.append({
            "address": addr, "url": url,
            "city": (it.get("city") or "").strip(),
            "state": (it.get("state") or "").strip(),
            "zip": (str(it.get("zip") or "")).strip(),
            "opening_bid": it.get("opening_bid") or it.get("current_bid"),
            "auction_date": it.get("auction_date"),
            "beds": it.get("beds"), "baths": it.get("baths"), "sqft": it.get("sqft"),
            "year_built": it.get("year_built"),
            "arv_estimate": it.get("arv_estimate"),
            "rehab_estimate": it.get("rehab_estimate"),
            "property_type": it.get("property_type"),
            "source": it.get("source"),
        })
        if len(listings) >= max_listings:
            break

    return {"ok": True, "area_label": area_label, "listings": listings,
            "notes": notes, "model": model, "web_searches_used": web_searches}
