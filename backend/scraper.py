"""Scraper for property listing pages.

Supports:
- Zillow (public listings) — parses embedded JSON-LD + __NEXT_DATA__
- Redfin (public listings) — parses JSON-LD + inline JSON
- ispeedtolead.com / DealSpeed (auth-required) — tries authenticated API
  if a session cookie is configured for the domain

Returns a normalized dict the analyzer / API can consume.

Known limitations:
- Zillow may serve a captcha challenge → flag `requires_manual_entry`
- ispeedtolead requires session auth → user must save a cookie via
  /api/auth-cookies (see server.py) for that domain
- Site HTML changes occasionally; selectors include multiple fallbacks.
"""

import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


# Per-domain auth cookie store (filesystem JSON).
# Lazy: the server module passes the file path in via set_cookie_store_path().
_COOKIE_STORE: Optional[Path] = None


def set_cookie_store_path(path: Path):
    global _COOKIE_STORE
    _COOKIE_STORE = Path(path)


def _load_cookies(domain: str) -> Optional[str]:
    """Return cookie string for a domain, or None."""
    if _COOKIE_STORE is None or not _COOKIE_STORE.exists():
        return None
    try:
        with open(_COOKIE_STORE, "r") as f:
            data = json.load(f)
        return data.get(domain) or data.get(domain.lstrip("www."))
    except Exception:
        return None


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def detect_site(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "zillow" in host:
        return "zillow"
    if "redfin" in host:
        return "redfin"
    if "ispeedtolead" in host or "dealspeed" in host:
        return "ispeedtolead"
    if "rapmls.com" in host:
        return "rapmls"
    if "auction.com" in host:
        return "auction_com"
    return "unknown"


def _fetch(url: str, extra_headers: Optional[dict] = None) -> str:
    """Fetch HTML with realistic headers. Raises on non-200."""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    with httpx.Client(follow_redirects=True, timeout=30) as c:
        r = c.get(url, headers=headers)
        r.raise_for_status()
        return r.text


def _fetch_json(url: str, extra_headers: Optional[dict] = None) -> dict:
    """Fetch JSON. Returns {} on non-200 or parse failure."""
    headers = dict(HEADERS)
    headers["Accept"] = "application/json, text/plain, */*"
    # Don't ask for brotli — httpx can't decompress it without optional deps
    headers["Accept-Encoding"] = "gzip, deflate"
    if extra_headers:
        headers.update(extra_headers)
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as c:
            r = c.get(url, headers=headers)
            if r.status_code == 200:
                return r.json()
            return {"_status": r.status_code}
    except Exception as e:
        return {"_error": str(e)}


def _extract_json_ld(soup: BeautifulSoup) -> list:
    """Extract all JSON-LD blocks."""
    blocks = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            txt = tag.get_text(strip=True)
            data = json.loads(txt)
            if isinstance(data, list):
                blocks.extend(data)
            else:
                blocks.append(data)
        except (json.JSONDecodeError, AttributeError):
            continue
    return blocks


def _extract_next_data(soup: BeautifulSoup) -> Optional[dict]:
    """Extract Next.js __NEXT_DATA__ block."""
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return None
    try:
        return json.loads(tag.get_text(strip=True))
    except json.JSONDecodeError:
        return None


def _safe_get(d, *path, default=None):
    """Walk a nested dict/list safely."""
    cur = d
    for p in path:
        if cur is None:
            return default
        if isinstance(p, int):
            if isinstance(cur, list) and 0 <= p < len(cur):
                cur = cur[p]
            else:
                return default
        else:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return default
    return cur if cur is not None else default


def parse_zillow(html: str) -> dict:
    """Parse a Zillow detail page."""
    soup = BeautifulSoup(html, "lxml")
    result = {"source": "zillow", "raw_html_length": len(html)}

    # Detect captcha / challenge
    if "Press & Hold" in html or "captcha" in html.lower()[:5000]:
        result["captcha_detected"] = True
        result["requires_manual_entry"] = True
        return result

    # 1) Try JSON-LD
    ld_blocks = _extract_json_ld(soup)
    for block in ld_blocks:
        if block.get("@type") in ("SingleFamilyResidence", "Apartment", "House",
                                    "Residence", "Product"):
            # Address
            addr = block.get("address", {})
            if isinstance(addr, dict):
                result["street"] = addr.get("streetAddress")
                result["city"] = addr.get("addressLocality")
                result["state"] = addr.get("addressRegion")
                result["zip"] = addr.get("postalCode")
            # Coords / lat-lng if present
            geo = block.get("geo", {})
            if isinstance(geo, dict):
                result["lat"] = geo.get("latitude")
                result["lng"] = geo.get("longitude")
            # Number of rooms
            rooms = block.get("numberOfRooms")
            if rooms:
                result["rooms"] = rooms
            # Image
            img = block.get("image")
            if img:
                result["image"] = img if isinstance(img, str) else img[0]
            # Floor size
            fsize = _safe_get(block, "floorSize", "value")
            if fsize:
                try:
                    result["sqft"] = int(fsize)
                except (ValueError, TypeError):
                    pass
            break

    # 2) Try __NEXT_DATA__
    next_data = _extract_next_data(soup)
    if next_data:
        # Zillow's structure varies; check common paths
        gdp_clipboard = _safe_get(next_data, "props", "pageProps",
                                   "componentProps", "gdpClientCache")
        if isinstance(gdp_clipboard, str):
            try:
                gdp = json.loads(gdp_clipboard)
                # First key is usually a hashed ID
                first = next(iter(gdp.values())) if gdp else {}
                prop = _safe_get(first, "property", default=first)
                _fill_from_zillow_prop(result, prop)
            except (json.JSONDecodeError, StopIteration):
                pass

        # Fallback paths
        prop = _safe_get(next_data, "props", "pageProps", "property")
        if prop:
            _fill_from_zillow_prop(result, prop)

    # 3) Regex fallback for price (sometimes only in inline text)
    if not result.get("price"):
        m = re.search(r'"price":\s*(\d{4,})', html)
        if m:
            try:
                result["price"] = int(m.group(1))
            except ValueError:
                pass

    # 4) Field extraction — Zillow embeds JSON both as plain `"key":val` AND
    # double-escaped `\"key\":val` (inside <script> string-literals). Match both.
    # The optional `\\?` consumes the leading backslash when present.
    def _zillow_grab(field, pattern_inner, cast):
        if result.get(field) is not None:
            return
        # Try escaped form first (Apollo cache uses it), then plain
        for pat in (rf'\\"{pattern_inner[0]}\\":\s*{pattern_inner[1]}',
                    rf'"{pattern_inner[0]}":\s*{pattern_inner[1]}'):
            m = re.search(pat, html)
            if m:
                try:
                    result[field] = cast(m.group(1))
                    return
                except (ValueError, TypeError):
                    continue

    _zillow_grab("year_built",    ("yearBuilt",          r"(\d{4})"),     int)
    _zillow_grab("bedrooms",      ("bedrooms",           r"(\d+)"),       int)
    _zillow_grab("bathrooms",     ("bathrooms",          r"([\d.]+)"),    float)
    _zillow_grab("sqft",          ("livingArea(?:Value)?", r"(\d+)"),     int)
    _zillow_grab("lot_size_sqft", ("lotSize(?:Value)?",  r"(\d+)"),       int)
    _zillow_grab("zestimate",     ("zestimate",          r"(\d+)"),       int)
    _zillow_grab("rent_zestimate",("rentZestimate",      r"(\d+)"),       int)
    _zillow_grab("home_type",     ("homeType",           r"\\?\"([A-Z_]+)\\?\""), str)
    _zillow_grab("home_status",   ("homeStatus",         r"\\?\"([A-Z_]+)\\?\""), str)
    _zillow_grab("days_on_market",("daysOnZillow",       r"(\d+)"),       int)
    _zillow_grab("price_per_sqft",("pricePerSquareFoot", r"(\d+)"),       int)
    _zillow_grab("mls_number",    ("mlsId",              r"\\?\"([\w-]+)\\?\""), str)
    _zillow_grab("monthly_hoa",   ("monthlyHoaFee",      r"(\d+)"),       int)
    _zillow_grab("favorite_count",("favoriteCount",      r"(\d+)"),       int)
    _zillow_grab("page_view_count",("pageViewCount",     r"(\d+)"),       int)

    # Property tax rate
    m = re.search(r'\\?"propertyTaxRate\\?":\s*([\d.]+)', html)
    if m:
        try:
            result["property_tax_rate_pct"] = float(m.group(1))
        except ValueError:
            pass

    # Annual tax amount (from tax history)
    m = re.search(r'\\?"taxPaid\\?":\s*([\d.]+)', html)
    if m:
        try:
            result["annual_taxes"] = int(float(m.group(1)))
            result["monthly_taxes"] = result["annual_taxes"] // 12
        except ValueError:
            pass

    # Garage parking
    m = re.search(r'\\?"hasGarage\\?":\s*(true|false)', html)
    if m:
        result["has_garage"] = (m.group(1) == "true")
    m = re.search(r'\\?"parkingCapacity\\?":\s*(\d+)', html)
    if m:
        try: result["parking_spaces"] = int(m.group(1))
        except: pass

    # Heating / cooling / construction (often as strings in escaped form)
    for field, key in [("heating", "heating"), ("cooling", "cooling"),
                         ("construction_materials", "constructionMaterials")]:
        m = re.search(rf'\\?"{key}\\?":\s*\[?\\?"([^"\\]+)\\?"', html)
        if m: result[field] = m.group(1)

    # Time on Zillow (string like "12 hours" / "3 days")
    m = re.search(r'\\?"timeOnZillow\\?":\s*\\?"([^"\\]+)\\?"', html)
    if m: result["time_on_zillow"] = m.group(1)

    return result


def _fill_from_zillow_prop(out: dict, prop: dict):
    """Fill output from a Zillow property dict."""
    if not isinstance(prop, dict):
        return
    if "streetAddress" in prop and not out.get("street"):
        out["street"] = prop["streetAddress"]
    if "city" in prop and not out.get("city"):
        out["city"] = prop["city"]
    if "state" in prop and not out.get("state"):
        out["state"] = prop["state"]
    if "zipcode" in prop and not out.get("zip"):
        out["zip"] = prop["zipcode"]
    for k_src, k_dst in [
        ("price", "price"),
        ("yearBuilt", "year_built"),
        ("bedrooms", "bedrooms"),
        ("bathrooms", "bathrooms"),
        ("livingArea", "sqft"),
        ("lotSize", "lot_size_sqft"),
        ("zestimate", "zestimate"),
        ("rentZestimate", "rent_zestimate"),
        ("homeType", "home_type"),
        ("description", "description"),
        ("daysOnZillow", "days_on_market"),
        ("latitude", "lat"),
        ("longitude", "lng"),
    ]:
        v = prop.get(k_src)
        if v is not None and not out.get(k_dst):
            out[k_dst] = v
    # ---- Photos (responsivePhotos preferred, originalPhotos fallback) ----
    if not out.get("image_gallery"):
        urls = _zillow_extract_photos(prop)
        if urls:
            out["image_gallery"] = urls
            out["image"] = urls[0]


def _zillow_extract_photos(prop: dict) -> list:
    """Extract the best-resolution image URL from a Zillow property dict.

    Zillow stores photos in `responsivePhotos[]` and `originalPhotos[]`.
    Each photo has `mixedSources.jpeg[]` (and sometimes `.webp[]`) with
    multiple width variants. We pick the largest reasonable size.
    """
    photos = prop.get("responsivePhotos") or prop.get("originalPhotos") or []
    if not isinstance(photos, list):
        return []
    out = []
    for p in photos:
        if not isinstance(p, dict):
            continue
        # Prefer webp if available (smaller, modern); fallback to jpeg
        ms = p.get("mixedSources") or {}
        best_url = None
        best_w = 0
        for fmt in ("webp", "jpeg"):
            for entry in (ms.get(fmt) or []):
                if not isinstance(entry, dict):
                    continue
                w = entry.get("width") or 0
                # Prefer 1024-1280 width (large but not huge)
                # Score: prefer widths in the 768-1500 range
                score = w if w <= 1500 else (1500 - (w - 1500))
                if score > best_w:
                    best_w = score
                    best_url = entry.get("url")
            if best_url:
                break
        if not best_url:
            best_url = p.get("url")
        if best_url and best_url not in out:
            out.append(best_url)
    return out[:30]


def parse_redfin(html: str) -> dict:
    """Parse a Redfin detail page."""
    soup = BeautifulSoup(html, "lxml")
    result = {"source": "redfin", "raw_html_length": len(html)}

    if "Pardon Our Interruption" in html or "captcha" in html.lower()[:5000]:
        result["captcha_detected"] = True
        result["requires_manual_entry"] = True
        return result

    # JSON-LD
    ld_blocks = _extract_json_ld(soup)
    for block in ld_blocks:
        types = block.get("@type")
        types_list = [types] if isinstance(types, str) else (types or [])
        if any(t in ("Product", "SingleFamilyResidence", "Apartment",
                       "House", "Residence") for t in types_list):
            addr = block.get("address", {})
            if isinstance(addr, dict):
                result["street"] = addr.get("streetAddress")
                result["city"] = addr.get("addressLocality")
                result["state"] = addr.get("addressRegion")
                result["zip"] = addr.get("postalCode")
            offers = block.get("offers", {})
            if isinstance(offers, dict):
                price = offers.get("price")
                if price:
                    try:
                        result["price"] = int(price)
                    except (ValueError, TypeError):
                        pass
            img = block.get("image")
            if img:
                result["image"] = img if isinstance(img, str) else img[0]

    # Regex fallbacks
    fields = [
        (r'"price":\s*(\d+)', "price", int),
        (r'"yearBuilt":\s*(\d+)', "year_built", int),
        (r'"beds":\s*(\d+)', "bedrooms", int),
        (r'"baths":\s*([\d.]+)', "bathrooms", float),
        (r'"sqFt":\s*(\d+)', "sqft", int),
        (r'"sqFtFinished":\s*(\d+)', "sqft", int),
        (r'"lotSize":\s*(\d+)', "lot_size_sqft", int),
        (r'"daysOnMarket":\s*(\d+)', "days_on_market", int),
        (r'"rentEstimate":\s*(\d+)', "rent_zestimate", int),
    ]
    for pat, key, cast in fields:
        if result.get(key):
            continue
        m = re.search(pat, html)
        if m:
            try:
                result[key] = cast(m.group(1))
            except (ValueError, TypeError):
                pass

    return result


def _clean_html_description(html: str) -> str:
    """Strip HTML while preserving paragraph and list structure."""
    if not html:
        return ""
    txt = html
    # Convert block elements to newlines
    txt = re.sub(r"<br\s*/?>", "\n", txt, flags=re.I)
    txt = re.sub(r"</(p|h[1-6]|li|div|tr|ul|ol)>", "\n", txt, flags=re.I)
    # Bullet markers
    txt = re.sub(r"<li[^>]*>", "• ", txt, flags=re.I)
    # Strip remaining tags
    txt = re.sub(r"<[^>]+>", "", txt)
    # HTML entities
    entities = {"&nbsp;": " ", "&amp;": "&", "&quot;": '"',
                "&#39;": "'", "&apos;": "'", "&lt;": "<", "&gt;": ">",
                "&ldquo;": '"', "&rdquo;": '"', "&rsquo;": "'", "&lsquo;": "'",
                "&mdash;": "—", "&ndash;": "–", "&hellip;": "..."}
    for ent, ch in entities.items():
        txt = txt.replace(ent, ch)
    # Collapse whitespace within lines + multiple newlines
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n[ \t]+", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def _extract_from_description(text: str) -> dict:
    """Pull structured fields out of a free-text property description."""
    if not text:
        return {}
    out = {}

    def _money(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        s = m.group(1).replace(",", "").replace("$", "").strip()
        try:
            return int(float(s))
        except (ValueError, TypeError):
            return None

    # Annual property taxes (most listings give the yearly amount)
    annual_tax = (_money(r"(?:property\s+tax(?:es)?|annual\s+tax(?:es)?)"
                          r"[^\d$]*\$?\s*([\d,]+)") or
                  _money(r"20\d{2}\s+property\s+tax(?:es)?[^\d$]*\$?\s*([\d,]+)"))
    if annual_tax and annual_tax > 100:  # sanity check
        out["annual_taxes"] = annual_tax
        out["monthly_taxes"] = round(annual_tax / 12)

    # External ARV anchors
    out["zillow_estimate"] = _money(r"zillow\s+(?:estimate|zestimate)[^\d$]*\$?\s*([\d,]+)")
    out["realtor_estimate"] = _money(r"realtor[^\d$]*estimate[^\d$]*\$?\s*([\d,]+)")
    out["redfin_estimate"] = _money(r"redfin\s+(?:estimate)[^\d$]*\$?\s*([\d,]+)")

    # Foundation
    m = re.search(r"foundation[:\s]+([A-Za-z][A-Za-z\s/-]{2,30}?)(?:\n|\.|,|;)",
                   text, re.IGNORECASE)
    if m:
        out["foundation"] = m.group(1).strip()

    # Basement
    m = re.search(r"basement[:\s]+([A-Za-z][A-Za-z\s/-]{2,40}?)(?:\n|\.|,|;)",
                   text, re.IGNORECASE)
    if m:
        out["basement"] = m.group(1).strip()

    # Roof (type + age)
    m = re.search(r"roof[:\s]+([^.\n]{3,80})", text, re.IGNORECASE)
    if m:
        out["roof_notes"] = m.group(1).strip()

    # HVAC notes
    m = re.search(r"hvac[:\s]+([^.\n]{3,80})", text, re.IGNORECASE)
    if m:
        out["hvac_notes"] = m.group(1).strip()

    # Water heater
    m = re.search(r"water\s+heater[:\s]+([^.\n]{3,60})", text, re.IGNORECASE)
    if m:
        out["water_heater_notes"] = m.group(1).strip()

    # School rating (e.g., "8/10 rating")
    m = re.search(r"(?:school[s]?[^.\n]{0,20}|rated|rating)\s*(\d{1,2})\s*/\s*10",
                   text, re.IGNORECASE)
    if m:
        out["school_rating"] = f"{m.group(1)}/10"

    # Flood risk
    m = re.search(r"(low|moderate|high|very\s+high)\s+flood\s+risk", text, re.IGNORECASE)
    if m:
        out["flood_risk"] = m.group(1).title()

    # Lot size in acres (overrides API's lot_size if larger/more precise)
    m = re.search(r"([\d.]+)\s*(?:ac|acres?)", text, re.IGNORECASE)
    if m:
        try:
            acres = float(m.group(1))
            if 0.05 < acres < 1000:
                out["lot_size_acres"] = acres
                out["lot_size"] = f"{acres:g} acres"
        except (ValueError, TypeError):
            pass

    # Walkthrough / open house dates
    m = re.search(
        r"(?:walkthrough|open\s+house|showing)[^.\n]{0,40}"
        r"((?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\s*,?\s*"
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*\d{1,2}"
        r"(?:\s*[-–at@]\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)",
        text, re.IGNORECASE)
    if m:
        out["showing_date"] = m.group(1).strip()

    # Strategy hints
    if re.search(r"buy\s*[&and]+\s*hold|buy[-\s]*and[-\s]*hold", text, re.I):
        out["strategy_hint"] = "Buy & Hold"
    elif re.search(r"\bflip\b|fix[-\s]*and[-\s]*flip", text, re.I):
        out["strategy_hint"] = "Flip"
    elif re.search(r"brrrr", text, re.I):
        out["strategy_hint"] = "BRRRR"

    return {k: v for k, v in out.items() if v is not None}


def parse_ispeedtolead(url: str) -> dict:
    """Fetch and parse a deal from ispeedtolead.com / DealSpeed.

    Uses the public guest endpoint:
        https://be.ispeedtolead.com/api/properties/slug/guest/{id}
    which returns the full property record (address, ARV, rehab, comps)
    WITHOUT authentication.
    """
    result = {"source": "ispeedtolead", "url": url}

    m = re.search(r"/property/(\d+)", url)
    if not m:
        result["error"] = "Could not extract property ID from URL"
        result["requires_manual_entry"] = True
        return result
    prop_id = m.group(1)
    result["property_id"] = prop_id

    headers = {
        "Origin": "https://app.ispeedtolead.com",
        "Referer": "https://app.ispeedtolead.com/",
    }
    api_url = (f"https://be.ispeedtolead.com/api/properties/slug/guest/"
               f"{prop_id}?new_view=null")
    data = _fetch_json(api_url, extra_headers=headers)

    if not data or data.get("_status") or data.get("_error"):
        result["error"] = (
            f"ispeedtolead API returned no data "
            f"(HTTP {data.get('_status', '?') if data else '?'})"
        )
        result["requires_manual_entry"] = True
        result["external_link"] = url
        return result

    result["_api_endpoint"] = api_url

    # Form fields: parse the form list for beds/baths/sqft/year/etc.
    form_map = {}
    for f in (data.get("form") or []):
        if isinstance(f, dict) and f.get("field"):
            form_map[f["field"]] = f.get("value")

    # Subject property (from rentcast data) — has the precise address
    sp = ((data.get("rent") or {}).get("rent") or {}).get("subjectProperty") or {}
    if not sp:
        sp = ((data.get("rent") or {}).get("comping") or {}).get("subjectProperty") or {}

    # Address fields
    result["street"] = sp.get("addressLine1") or ""
    result["city"] = sp.get("city") or data.get("city_name") or ""
    result["state"] = sp.get("state") or data.get("state") or ""
    result["zip"] = sp.get("zipCode") or ""

    # Property specs — prefer rentcast (exact) over form (range)
    def _to_int(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            mm = re.search(r"\d+", v)
            return int(mm.group(0)) if mm else None
        return None

    def _to_float(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            mm = re.search(r"\d+(?:\.\d+)?", v)
            return float(mm.group(0)) if mm else None
        return None

    result["bedrooms"] = sp.get("bedrooms") or _to_int(form_map.get("bedrooms"))
    result["bathrooms"] = sp.get("bathrooms") or _to_float(form_map.get("bathrooms"))
    result["sqft"] = sp.get("squareFootage") or _to_int(form_map.get("square_exact"))
    result["year_built"] = sp.get("yearBuilt") or _to_int(form_map.get("year_exact"))
    result["lot_size_sqft"] = sp.get("lotSize") or _to_int(data.get("lot_size"))
    result["home_type"] = sp.get("propertyType") or form_map.get("multifamily") or "Single Family Residence"

    # FINANCIALS — the gold
    result["price"] = data.get("asking_price")
    result["arv"] = data.get("arv")
    result["rehab_estimate"] = data.get("repairs_cost")

    # Rent estimate (rentcast)
    rent_obj = (data.get("rent") or {}).get("rent") or {}
    result["rent_estimate"] = rent_obj.get("rent")
    result["rent_low"] = rent_obj.get("rentRangeLow")
    result["rent_high"] = rent_obj.get("rentRangeHigh")

    # Sale comping (rentcast)
    comp_obj = (data.get("rent") or {}).get("comping") or {}
    result["comp_value_estimate"] = comp_obj.get("price")
    result["comp_value_low"] = comp_obj.get("priceRangeLow")
    result["comp_value_high"] = comp_obj.get("priceRangeHigh")

    # Comparable sales (for ARV verification)
    result["sale_comparables"] = [
        {
            "address": c.get("formattedAddress"),
            "beds": c.get("bedrooms"),
            "baths": c.get("bathrooms"),
            "sqft": c.get("squareFootage"),
            "price": c.get("price"),
            "date": (c.get("listedDate") or c.get("removedDate") or "")[:10],
            "distance_mi": c.get("distance"),
        }
        for c in (comp_obj.get("comparables") or [])[:10]
    ]
    # Rental comparables
    result["rent_comparables"] = [
        {
            "address": c.get("formattedAddress"),
            "beds": c.get("bedrooms"),
            "baths": c.get("bathrooms"),
            "sqft": c.get("squareFootage"),
            "rent": c.get("price"),
            "date": (c.get("listedDate") or "")[:10],
        }
        for c in (rent_obj.get("comparables") or [])[:10]
    ]

    # Description — clean HTML and parse useful fields from it
    desc_html = data.get("description") or ""
    if desc_html:
        clean = _clean_html_description(desc_html)
        result["description"] = clean
        # Extract structured info from the description text
        extracted = _extract_from_description(clean)
        for k, v in extracted.items():
            if v is not None and result.get(k) in (None, "", 0):
                result[k] = v

    # Images — store the full gallery (first image as hero + all URLs)
    images = data.get("images") or []
    image_urls = []
    for img in images:
        if isinstance(img, dict):
            # Prefer the medium "800w" crop if available for the gallery
            cropped = img.get("cropped") or []
            best = None
            for c in cropped:
                if c.get("crop_size") == "800w":
                    best = c.get("location")
                    break
            image_urls.append(best or img.get("location"))
    if image_urls:
        result["image"] = image_urls[0]
        result["image_gallery"] = [u for u in image_urls if u][:20]

    # Lat/lng
    result["lat"] = data.get("lat") or sp.get("latitude")
    result["lng"] = data.get("lng") or sp.get("longitude")

    # Listing name (deal title)
    result["listing_name"] = data.get("name")

    return result


def find_by_address(address: str) -> dict:
    """Find a property listing across Zillow / Redfin / AI search.

    Returns {found: bool, url: str|None, source: str, candidates: [{...}],
              data: <full scrape dict>|None, error: str|None}
    """
    import logging
    log = logging.getLogger("flip-board.scraper")
    if not address or len(address.strip()) < 5:
        return {"found": False, "error": "Address too short"}

    address = address.strip()
    candidates = []

    # ===== 1. Try Zillow's address-search URL pattern =====
    z_url = _zillow_search_url_for_address(address)
    log.info("Trying Zillow search: %s", z_url)
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as c:
            r = c.get(z_url, headers=HEADERS)
            final = str(r.url)
            # If Zillow redirected us straight to a homedetails page, we found it
            if "/homedetails/" in final:
                log.info("Zillow direct hit: %s", final)
                candidates.append({"source": "zillow", "url": final, "via": "direct-redirect"})
            else:
                # Parse search results for first homedetails link
                soup = BeautifulSoup(r.text, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/homedetails/" in href and "_zpid" in href:
                        if href.startswith("/"):
                            href = "https://www.zillow.com" + href
                        candidates.append({"source": "zillow", "url": href, "via": "search-result"})
                        break
                # Also try regex on raw HTML in case parser missed
                if not any(c.get("source") == "zillow" for c in candidates):
                    m = re.search(r'href="(/homedetails/[^"]+_zpid/)"', r.text)
                    if m:
                        candidates.append({"source": "zillow",
                                            "url": "https://www.zillow.com" + m.group(1),
                                            "via": "regex"})
    except Exception as e:
        log.warning("Zillow search failed: %s", e)

    # ===== 2. Try Redfin's address-search URL =====
    if not candidates:
        rf_url = _redfin_search_url_for_address(address)
        log.info("Trying Redfin search: %s", rf_url)
        try:
            with httpx.Client(follow_redirects=True, timeout=30) as c:
                r = c.get(rf_url, headers=HEADERS)
                final = str(r.url)
                if "/home/" in final or "/CA/" in final or "/OH/" in final:
                    candidates.append({"source": "redfin", "url": final, "via": "direct-redirect"})
                else:
                    # Look for property links
                    soup = BeautifulSoup(r.text, "lxml")
                    for a in soup.find_all("a", href=True):
                        href = a["href"]
                        if "/home/" in href and "/redfin.com" in (href if href.startswith("http") else "redfin.com"):
                            if href.startswith("/"):
                                href = "https://www.redfin.com" + href
                            candidates.append({"source": "redfin", "url": href, "via": "search-result"})
                            break
        except Exception as e:
            log.warning("Redfin search failed: %s", e)

    # ===== 3. AI fallback (Claude web search) =====
    if not candidates:
        log.info("No HTTP hits — trying AI find")
        try:
            from . import ai_research
            if ai_research.is_configured():
                ai_url = _ai_find_listing_url(address)
                if ai_url:
                    candidates.append({"source": "ai", "url": ai_url, "via": "claude-web-search"})
        except Exception as e:
            log.warning("AI find failed: %s", e)

    if not candidates:
        return {
            "found": False,
            "error": ("No listing found across Zillow, Redfin, or AI search. "
                       "Try a more specific address or add the property manually."),
            "candidates": [],
        }

    # ===== Scrape the first candidate to get full property data =====
    best = candidates[0]
    log.info("Scraping best candidate: %s", best["url"])
    data = scrape(best["url"])
    return {
        "found": True,
        "url": best["url"],
        "source": best["source"],
        "candidates": candidates,
        "data": data,
        "via": best.get("via"),
    }


def _zillow_search_url_for_address(address: str) -> str:
    # Zillow uses + instead of spaces in their _rb URL pattern
    clean = re.sub(r"\s+", " ", address.strip())
    encoded = clean.replace(", ", "+").replace(",", "+").replace(" ", "+")
    return f"https://www.zillow.com/homes/{encoded}_rb/"


def _redfin_search_url_for_address(address: str) -> str:
    from urllib.parse import quote
    return (f"https://www.redfin.com/stingray/api/gis-csv?al=1&"
             f"market=cleveland&num_homes=1&location={quote(address)}")


def _ai_find_listing_url(address: str) -> Optional[str]:
    """Ask Claude to find the Zillow/Redfin/Realtor listing URL for an address."""
    try:
        from . import ai_research
        import anthropic
        client = anthropic.Anthropic(api_key=ai_research.get_api_key())
        msg = client.messages.create(
            model="claude-haiku-4-5",  # cheap, fast for URL finding
            max_tokens=400,
            system=("You are a URL finder. Given a US property address, find the "
                     "canonical Zillow.com OR Redfin.com OR Realtor.com listing URL. "
                     "Return ONLY the URL, nothing else. If not found, return NONE."),
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            messages=[{"role": "user", "content":
                f"Find the canonical listing URL on Zillow, Redfin, or Realtor.com for:\n{address}"}],
        )
        text = "\n".join(b.text for b in msg.content if hasattr(b, "text")).strip()
        # Extract first URL
        m = re.search(
            r"https?://(?:www\.)?(?:zillow|redfin|realtor)\.com/[^\s\"'<>]+",
            text)
        return m.group(0) if m else None
    except Exception as e:
        import logging
        logging.getLogger("flip-board.scraper").warning("AI URL find failed: %s", e)
        return None


def _scrape_rapmls(url: str) -> dict:
    """Scrape a RAPMLS (Cincinnati/N. Kentucky MLS) client portal listing.

    These URLs come from agents sharing a listing via the client portal:
      https://cincy.rapmls.com/scripts/mgrqispi.dll?APPNAME=Cincynky&PRGNAME=MLSLogin&ARGUMENT=...

    The encrypted ARGUMENT auto-logs into a session that shows ONE property
    detail page. We use Playwright (sync) to render and extract structured
    fields from the HTML.
    """
    import queue
    import threading

    def _impl():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True,
                args=["--no-first-run", "--no-default-browser-check"])
            try:
                ctx = b.new_context(
                    user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/131.0.0.0 Safari/537.36"),
                    viewport={"width": 1366, "height": 900},
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try: page.wait_for_load_state("networkidle", timeout=15000)
                except Exception: pass
                page.wait_for_timeout(3500)

                text = page.evaluate("() => document.body.innerText.slice(0, 25000)")
                title = page.title() or ""

                # Pull all <img> URLs that look like listing photos
                imgs = page.evaluate("""() => {
                    // Only the real listing photo URLs — RAPMLS uses
                    // /cincy/listingpics/tmbphoto/... for thumbnails
                    // (we upscale by stripping 'tmb' below)
                    const out = [];
                    document.querySelectorAll('img').forEach(im => {
                        const src = im.src || im.getAttribute('data-src') || '';
                        if (src && /\\/listingpics\\//i.test(src)
                            && !/logo|status|map|attach/i.test(src)) {
                            out.push(src);
                        }
                    });
                    return Array.from(new Set(out)).slice(0, 30);
                }""") or []
                # Upscale: rapmls /tmbphoto/ → /photo/ for full-res
                imgs = [im.replace("/tmbphoto/", "/photo/") for im in imgs]
                return {"text": text, "title": title, "images": imgs, "html_len": len(page.content())}
            finally:
                b.close()

    # Run Playwright on a clean thread (FastAPI's loop conflicts with sync API)
    rq = queue.Queue()
    def runner():
        try: rq.put(("ok", _impl()))
        except Exception as e: rq.put(("err", e))
    t = threading.Thread(target=runner, daemon=True)
    t.start(); t.join()
    kind, payload = rq.get()
    if kind == "err":
        return {"error": f"RAPMLS scrape failed: {payload}", "url": url}

    text = payload["text"]
    images = payload["images"]

    # ---- Parse structured fields from the rendered text ----
    out: dict = {"source": "rapmls", "url": url, "site": "rapmls"}

    # MLS Number: "Listing Detail #1879854" or "Listing #1879854"
    m = re.search(r"(?:Listing\s*(?:Detail)?\s*#|MLS\s*#?\s*)(\d{6,8})", text)
    if m: out["mls_number"] = m.group(1)

    # Price: "$100,000 (LP)" or "$100,000"
    m = re.search(r"\$\s*([\d,]+)(?:\s*\(LP\))?", text)
    if m:
        try: out["price"] = int(m.group(1).replace(",", ""))
        except: pass

    # Address: pattern like "8791 Grenada Dr,Springfield Twp., OH  45231"
    m = re.search(
        r"(\d{1,6}\s+[A-Z][\w\s.'-]+?(?:Dr|Drive|St|Street|Ave|Avenue|Rd|Road|"
        r"Ln|Lane|Ct|Court|Pl|Place|Way|Blvd|Boulevard|Cir|Circle|Ter|Terrace|"
        r"Hwy|Pkwy|Trl|Trail|Pt|Point|Sq|Square)\.?)\s*,\s*"
        r"([\w\s.'-]+?)\s*,\s*([A-Z]{2})\s+(\d{5})",
        text, re.I)
    if m:
        # Normalize non-breaking spaces and runs of whitespace
        def _norm(s):
            return re.sub(r"\s+", " ", s.replace(" ", " ")).strip()
        out["street"] = _norm(m.group(1))
        out["city"] = _norm(m.group(2))
        out["state"] = m.group(3)
        out["zip"] = m.group(4)
        out["address"] = f"{out['street']}, {out['city']}, {out['state']} {out['zip']}"

    # Beds / Baths / Sqft / Lot / Year
    m = re.search(r"Bed\s*:?\s*(\d+)", text, re.I)
    if m: out["bedrooms"] = int(m.group(1))
    m = re.search(r"Baths?\s*:?\s*([\d.]+)(?:\s*\((\d+)\s+(\d+)\))?", text, re.I)
    if m:
        # "Baths: 2 (1 1)" → 1 full + 1 half = 1.5
        if m.group(2) and m.group(3):
            full, half = int(m.group(2)), int(m.group(3))
            out["bathrooms"] = full + (half * 0.5)
        else:
            try: out["bathrooms"] = float(m.group(1))
            except: pass
    m = re.search(r"Sq\s*Ft\s*:?\s*([\d,]+)", text, re.I)
    if m:
        try: out["sqft"] = int(m.group(1).replace(",", ""))
        except: pass
    m = re.search(r"Lot\s*Sz\s*:?\s*([\d.]+)", text, re.I)
    if m: out["lot_size_acres"] = float(m.group(1))
    m = re.search(r"Yr\s*:?\s*(\d{4})", text)
    if m: out["year_built"] = int(m.group(1))

    # Status (Active / Pending / Closed) + status date
    m = re.search(r"\b(Active|Pending|Sold|Closed|Contingent|Withdrawn)\b\s*\(([\d/]+)\)", text, re.I)
    if m:
        out["mls_status"] = m.group(1)
        out["mls_status_date"] = m.group(2)

    # Construction / Architecture / Levels / Basement
    for label, key in [("Architecture", "architecture"),
                        ("Construction", "construction"),
                        ("Levels", "levels"),
                        ("Basement", "basement"),
                        ("Foundation", "foundation"),
                        ("Roof", "roof"),
                        ("Heating", "heating"),
                        ("Cooling", "cooling"),
                        ("Primary Water Source", "water"),
                        ("Sewer", "sewer")]:
        m = re.search(rf"{label}\s+([A-Z][^\t\n]{{2,60}})", text)
        if m: out[key] = m.group(1).strip()

    # Single Family Description
    if "Single Family" in text:
        out["property_type"] = "Single Family Residence"
        out["home_type"] = "SFR"

    # Remarks (description) — between "Remarks" and "Pictures"
    m = re.search(r"Remarks\s*\n+(.+?)(?=\n\n|Pictures|Listing|Map)", text, re.DOTALL)
    if m:
        desc = re.sub(r"\s+", " ", m.group(1)).strip()
        out["description"] = desc[:1500]

    # Agent info
    m = re.search(r"Agent\s+([A-Z][\w\s.'-]+?)\s+Primary:\s*([\d-]+)", text)
    if m:
        out["listing_agent"] = m.group(1).strip()
        out["listing_agent_phone"] = m.group(2)
    m = re.search(r"Office\s+([A-Z][\w\s.&'-]+?)\s+Phone:\s*([\d-]+)", text)
    if m:
        out["listing_office"] = m.group(1).strip()
        out["listing_office_phone"] = m.group(2)

    # Images (with size upscale if RAPMLS uses thumb URLs)
    out["images"] = images
    if images:
        out["image"] = images[0]

    # MLS area code
    m = re.search(r"Area\s*:?\s*([A-Z]\d{2,3})", text)
    if m: out["mls_area_code"] = m.group(1)

    # Coerce a price (asking_price field on the deal seed schema)
    if out.get("price"):
        out["asking_price"] = out["price"]

    return out


def _scrape_auction_com(url: str) -> dict:
    """Scrape an auction.com property detail page.

    Pages are React-rendered with lazy image loading + extensive metadata
    in the body text. We use Playwright + regex extraction.
    """
    import queue
    import threading

    def _impl():
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True,
                args=["--no-first-run", "--no-default-browser-check"])
            try:
                ctx = b.new_context(
                    user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/131.0.0.0 Safari/537.36"),
                    viewport={"width": 1366, "height": 900},
                )
                page = ctx.new_page()
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(4000)
                # Scroll to trigger lazy loads (photos, market analysis)
                for _ in range(5):
                    page.evaluate("() => window.scrollBy(0, 700)")
                    page.wait_for_timeout(500)
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(1000)

                body = page.evaluate("() => document.body.innerText")
                title = page.title() or ""
                # Photos: filter to auction.com property images
                imgs = page.evaluate("""() => {
                    const out = new Set();
                    document.querySelectorAll('img').forEach(im => {
                        const s = im.src || im.getAttribute('data-src') || '';
                        if (s && /propertyImages|propertyphotos|listing/i.test(s)
                            && !/logo|icon|page-assets/i.test(s)) {
                            out.add(s.split('?')[0]);  // strip query (sizing)
                        }
                    });
                    return Array.from(out).slice(0, 50);
                }""") or []
                return {"text": body, "title": title, "images": imgs}
            finally:
                b.close()

    rq = queue.Queue()
    def runner():
        try: rq.put(("ok", _impl()))
        except Exception as e: rq.put(("err", e))
    t = threading.Thread(target=runner, daemon=True)
    t.start(); t.join()
    kind, payload = rq.get()
    if kind == "err":
        return {"error": f"auction.com scrape failed: {payload}", "url": url}

    text = payload["text"]
    imgs = payload["images"]
    out = {"source": "auction_com", "url": url, "site": "auction_com"}

    # Address — "1403 Plainfield Rd" + "South Euclid, OH 44121, Cuyahoga County"
    m = re.search(
        r"([\d]+\s+[\w\.\- ]+?(?:Rd|Road|St|Street|Ave|Avenue|Dr|Drive|Ln|Lane|"
        r"Ct|Court|Pl|Place|Way|Blvd|Boulevard|Cir|Circle|Ter|Terrace|Hwy|"
        r"Pkwy|Trl|Trail|Pt|Point|Sq|Square)\.?)\s*\n\s*([\w\s\.\-']+?),\s*"
        r"([A-Z]{2})\s+(\d{5})(?:,\s*([\w\s]+?\s+County))?",
        text)
    if m:
        out["street"] = m.group(1).strip()
        out["city"] = m.group(2).strip()
        out["state"] = m.group(3)
        out["zip"] = m.group(4)
        if m.group(5): out["county"] = m.group(5).strip()
        out["address"] = f"{out['street']}, {out['city']}, {out['state']} {out['zip']}"

    # Beds / Baths / Sqft (compact format: "4 Beds 1 Baths 1,594 Sq. Ft.")
    m = re.search(r"(\d+)\s*Beds?", text, re.I)
    if m: out["bedrooms"] = int(m.group(1))
    m = re.search(r"([\d.]+)\s*Baths?", text, re.I)
    if m: out["bathrooms"] = float(m.group(1))
    m = re.search(r"([\d,]+)\s*Sq\.?\s*F(?:eet|t)\.?", text, re.I)
    if m:
        try: out["sqft"] = int(m.group(1).replace(",", ""))
        except: pass

    # Opening bid
    m = re.search(r"Opening\s*Bid[\s\$]*([\d,]+)", text, re.I)
    if m:
        try:
            out["price"] = int(m.group(1).replace(",", ""))
            out["opening_bid"] = out["price"]
            out["asking_price"] = out["price"]
        except: pass

    # Estimated market value
    m = re.search(r"Est\.?\s*Market\s*Value[\s\$]*([\d,]+|Not Available)",
                   text, re.I)
    if m and m.group(1) != "Not Available":
        try: out["estimated_market_value"] = int(m.group(1).replace(",", ""))
        except: pass

    # Year built
    m = re.search(r"Year\s*Built[\s:]*(\d{4})", text, re.I)
    if m: out["year_built"] = int(m.group(1))

    # Property type
    m = re.search(r"Property\s*Type[\s:]*([A-Z][\w\s]{2,40}?)(?:\n|Lot)", text, re.I)
    if m: out["home_type"] = m.group(1).strip()

    # Parcel
    m = re.search(r"(?:APN|Parcel)[\s:#]*([\w\-]+)", text)
    if m: out["parcel_id"] = m.group(1)

    # Lot size
    m = re.search(r"Lot\s*Size[\s:]*([\d,]+\s*(?:sq\.?\s*ft|sqft|acres?))",
                   text, re.I)
    if m: out["lot_size"] = m.group(1)

    # Auction type (Foreclosure / Bank Owned / Private)
    if "Bank Owned" in text:    out["auction_type"] = "Bank Owned"
    elif "Foreclosure" in text: out["auction_type"] = "Foreclosure"
    elif "Private Seller" in text: out["auction_type"] = "Private Seller"

    # Auction date (format: "Jun 8, 2026 8:00 AM" or similar)
    m = re.search(r"([A-Z][a-z]{2}\s+\d{1,2},\s*\d{4}\s+\d{1,2}:\d{2}\s*[AP]M(?:\s*ET)?)",
                   text)
    if m: out["auction_date_text"] = m.group(1)

    # Special notes / warnings (very useful — flags mold, condition, etc.)
    m = re.search(r"NOTE:?\s*([^.]+(?:\.[^.]+){0,3}\.)\s*(?:More)?", text)
    if m:
        out["special_notes"] = m.group(1).strip()[:500]

    # Property type → schema
    out["property_type"] = out.get("home_type", "Single Family Residence")
    if "Single Family" in (out.get("home_type") or ""):
        out["property_type"] = "Single Family Residence"

    # Photos
    out["images"] = imgs
    if imgs:
        out["image"] = imgs[0]

    return out


def scrape(url: str) -> dict:
    """Main entrypoint: fetch + parse based on detected site."""
    site = detect_site(url)
    if site == "unknown":
        return {"error": "Unsupported URL — Zillow, Redfin, ispeedtolead, "
                          "RAPMLS, and auction.com are supported",
                "url": url}

    if site == "rapmls":
        return _scrape_rapmls(url)
    if site == "auction_com":
        return _scrape_auction_com(url)

    if site == "ispeedtolead":
        # Detect LEAD URLs vs PROPERTY URLs and route accordingly.
        is_lead_url = "/ld/" in url or "open_order=" in url
        if is_lead_url:
            # Route to the lead scraper (different endpoint structure)
            try:
                from . import scraper_browser
                lead_data = scraper_browser.scrape_ispeedtolead_lead(url)
                # Map lead-style data back to the deal-seed shape so the
                # generic Add Deal form gets pre-filled correctly.
                data = {
                    "source": "ispeedtolead_lead",
                    "url": url,
                    "address": lead_data.get("address", ""),
                    "street": lead_data.get("street", ""),
                    "city": lead_data.get("city", ""),
                    "state": lead_data.get("state", ""),
                    "zip": lead_data.get("zip", ""),
                    "home_type": lead_data.get("property_type"),
                    "bedrooms": lead_data.get("beds"),
                    "bathrooms": lead_data.get("baths"),
                    "sqft": lead_data.get("sqft"),
                    "year_built": lead_data.get("year_built"),
                    "lot_size_sqft": lead_data.get("lot_size"),
                    "price": lead_data.get("asking_price"),
                    "rent_zestimate": None,
                    "rehab_estimate": lead_data.get("estimated_rehab"),
                    "description": lead_data.get("description"),
                    "image": lead_data.get("image"),
                    "image_gallery": lead_data.get("image_gallery"),
                    "lat": lead_data.get("lat"),
                    "lng": lead_data.get("lng"),
                    "property_id": lead_data.get("external_id"),
                    "is_lead": True,
                    "lead_price": lead_data.get("lead_price"),
                    "seller_name": lead_data.get("seller_name"),
                    "motivation": lead_data.get("motivation"),
                    "lead_source_label": lead_data.get("lead_source_label"),
                    "_routed_to": "lead_scraper",
                }
            except Exception as e:
                import logging
                logging.getLogger("flip-board.scraper").exception("Lead scrape failed")
                data = {"error": f"Lead scrape failed: {e}",
                         "requires_manual_entry": True, "url": url,
                         "source": "ispeedtolead_lead"}
        else:
            # Public guest API works WITHOUT auth for dealspeed properties.
            data = parse_ispeedtolead(url)
            # If public API failed, fall back to browser scraper (auth path).
            if data.get("requires_manual_entry"):
                try:
                    from . import scraper_browser
                    browser_data = scraper_browser.scrape_ispeedtolead_browser(url)
                    if not browser_data.get("requires_manual_entry"):
                        data = browser_data
                except Exception:
                    import logging
                    logging.getLogger("flip-board.scraper").exception(
                        "Browser scraper fallback failed")
    else:
        try:
            html = _fetch(url)
        except httpx.HTTPStatusError as e:
            return {"error": f"HTTP {e.response.status_code} from {site}",
                    "url": url, "requires_manual_entry": True}
        except httpx.RequestError as e:
            return {"error": f"Request failed: {e}",
                    "url": url, "requires_manual_entry": True}
        if site == "zillow":
            data = parse_zillow(html)
        else:
            data = parse_redfin(html)

    data["url"] = url

    # Build a normalized address string — coerce dict/list values to strings
    def _as_str(v):
        if v is None:
            return ""
        if isinstance(v, dict):
            # Try common name fields if a dict slipped through
            return str(v.get("name") or v.get("addressLine1") or "")
        if isinstance(v, (list, tuple)):
            return ""
        return str(v)

    street = _as_str(data.get("street"))
    city = _as_str(data.get("city"))
    state = _as_str(data.get("state"))
    zipc = _as_str(data.get("zip"))
    parts = [street, city, f"{state} {zipc}".strip()]
    address = ", ".join(p for p in parts if p)
    if address:
        data["address"] = address

    if data.get("lot_size_sqft"):
        try:
            data["lot_size"] = f"{int(data['lot_size_sqft']):,} sq ft"
        except (ValueError, TypeError):
            pass

    # Map to deal schema friendly fields
    deal_seed = {
        "address": data.get("address") or "",
        "street": street,
        "city": city,
        "state": state,
        "zip": zipc,
        "property_type": data.get("home_type") or "Single Family Residence",
        "beds": data.get("bedrooms"),
        "baths": data.get("bathrooms"),
        "sqft": data.get("sqft"),
        "year_built": data.get("year_built"),
        "lot_size": data.get("lot_size", ""),
        "listing_price": data.get("price"),
        "zestimate": data.get("zestimate") or data.get("arv") or
                      data.get("comp_value_estimate"),
        "rent_zestimate": data.get("rent_zestimate") or data.get("rent_estimate"),
        "rehab_estimate": data.get("rehab_estimate"),
        "median_dom": data.get("days_on_market"),
        "image": data.get("image"),
        "source_url": url,
        "source": site,
    }
    # Pass through richer ispeedtolead fields if available
    for k in ("comp_value_estimate", "comp_value_low", "comp_value_high",
              "rent_low", "rent_high", "sale_comparables", "rent_comparables",
              "listing_name", "description", "lat", "lng", "image_gallery",
              "annual_taxes", "monthly_taxes", "zillow_estimate",
              "realtor_estimate", "redfin_estimate", "foundation",
              "basement", "roof_notes", "hvac_notes", "water_heater_notes",
              "school_rating", "flood_risk", "showing_date",
              "strategy_hint", "lot_size_acres",
              # Zillow-specific extra fields (now extracted)
              "mls_number", "home_status", "price_per_sqft",
              "property_tax_rate_pct", "monthly_hoa", "has_garage",
              "parking_spaces", "heating", "cooling",
              "construction_materials", "time_on_zillow",
              "favorite_count", "page_view_count"):
        if data.get(k) is not None:
            deal_seed[k] = data[k]
    if data.get("requires_manual_entry"):
        deal_seed["requires_manual_entry"] = True
        if not data.get("error"):
            deal_seed["scrape_error"] = "Captcha or anti-bot challenge detected"
    if data.get("error"):
        deal_seed["scrape_error"] = data["error"]
    if data.get("external_link"):
        deal_seed["external_link"] = data["external_link"]
    if data.get("property_id"):
        deal_seed["external_id"] = data["property_id"]
    return deal_seed
