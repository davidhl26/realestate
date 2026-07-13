"""Zillow data via RapidAPI (Unofficial-Zillow-API) — structured JSON instead
of scraping. ~0.5s per lookup, no captcha, fields already clean.

Used as the PRIMARY source for listing verification when a key is configured
(Settings → Zillow Data API); the ScraperAPI/Scrapling scrape chain remains
the fallback. Free plan: 250 requests/month (hard limit — the API returns
429 and we fall back to scraping).
"""
import logging
from typing import Optional

import httpx

from . import ai_research

log = logging.getLogger("flip-board.zillow-api")

HOST = "unofficial-zillow-api2.p.rapidapi.com"
BASE = f"https://{HOST}"


def get_key() -> Optional[str]:
    try:
        return (ai_research.read_config().get("zillow_rapidapi_key") or "").strip() or None
    except Exception:
        return None


def is_configured() -> bool:
    return bool(get_key())


def _get(path: str, params: dict) -> Optional[dict]:
    key = get_key()
    if not key:
        return None
    try:
        r = httpx.get(BASE + path, params=params, timeout=25,
                      headers={"x-rapidapi-host": HOST, "x-rapidapi-key": key})
        if r.status_code == 429:
            log.warning("Zillow API monthly quota exhausted (429) — falling back to scraping")
            return None
        r.raise_for_status()
        d = r.json()
        if isinstance(d, dict) and d.get("success"):
            return d
        log.info("Zillow API no-success for %s %s: %s", path, params, str(d)[:200])
    except Exception as e:
        log.warning("Zillow API call failed (%s %s): %s", path, params, e)
    return None


def property_details(zpid) -> Optional[dict]:
    """One listing's live data, normalized to the same keys the verification
    merge expects (listing_price / beds / baths / …). None on any failure —
    the caller then falls back to scraping."""
    d = _get("/property/details", {"zpid": str(zpid)})
    if not d:
        return None
    addr = d.get("address") or {}
    return {
        "source": "zillow-api",
        "zpid": str(d.get("zpid") or zpid),
        "home_status": ((d.get("home_status") or "").strip().upper() or None),
        "days_on_market": d.get("days_on_zillow"),
        "listing_price": d.get("price"),
        "beds": d.get("beds"),
        "baths": d.get("baths"),
        "sqft": d.get("sqft"),
        "year_built": d.get("year_built"),
        "home_type": d.get("home_type"),
        "street": addr.get("streetAddress"),
        "city": addr.get("city"),
        "state": addr.get("state"),
        "zip": addr.get("zipcode"),
        "lat": d.get("latitude"),
        "lng": d.get("longitude"),
    }


def first_image(zpid) -> Optional[str]:
    """First listing photo URL (used on verified Radar finds only)."""
    d = _get("/property/images", {"zpid": str(zpid)})
    for im in (d or {}).get("images") or []:
        u = (im or {}).get("url")
        if u:
            return u
    return None


# ---- Discovery: newest listings straight from Zillow's search --------------
# Zillow's own days-on-Zillow buckets (the API rejects anything else).
_DOZ_BUCKETS = (1, 7, 14, 30, 90)


def _doz_bucket(max_dom) -> str:
    try:
        v = max(1, int(max_dom or 1))
    except (TypeError, ValueError):
        v = 1
    for b in _DOZ_BUCKETS:
        if v <= b:
            return str(b)
    return "90"


def _type_matches(property_type, home_type: str) -> bool:
    p = (property_type or "").lower()
    if not p:
        return True
    if "single" in p or p == "house":
        return home_type == "SINGLE_FAMILY"
    if "multi" in p or "duplex" in p:
        return "MULTI" in home_type or "DUPLEX" in home_type
    if "townhouse" in p:
        return home_type == "TOWNHOUSE"
    if "condo" in p:
        return home_type in ("CONDO", "APARTMENT")
    return True


def search_newest(location, max_dom=1, price_min=None, price_max=None,
                  beds_min=None, baths_min=None, sqft_min=None,
                  property_type=None, limit=40):
    """Newest FOR-SALE listings for an area, straight from Zillow's search —
    deterministic, real-time (no web-search index lag), ~0.7s, ONE request.

    Returns normalized listing dicts in the exact shape the Radar/Sourcing
    pipeline consumes (marked _preverified — the data IS Zillow's), or None
    on any API failure so the caller can fall back to the AI web search."""
    key = get_key()
    if not key or not (location or "").strip():
        return None
    body = {"page": 1, "status": "for_sale", "location": location.strip(),
            "days_on_zillow": _doz_bucket(max_dom)}
    if price_min:
        body["min_price"] = int(price_min)
    if price_max:
        body["max_price"] = int(price_max)
    if beds_min:
        body["min_beds"] = int(beds_min)
    if baths_min:
        body["min_baths"] = float(baths_min)
    if sqft_min:
        body["min_sqft"] = int(sqft_min)
    try:
        r = httpx.post(BASE + "/search/address", json=body, timeout=40,
                       headers={"x-rapidapi-host": HOST, "x-rapidapi-key": key,
                                "Content-Type": "application/json"})
        if r.status_code == 429:
            log.warning("Zillow API quota exhausted (429) — search falls back to AI")
            return None
        r.raise_for_status()
        d = r.json()
        if not (isinstance(d, dict) and d.get("success")):
            log.info("Zillow API search no-success: %s", str(d)[:200])
            return None
    except Exception as e:
        log.warning("Zillow API search failed (%s): %s", location, e)
        return None

    out = []
    for it in (d.get("listings") or []):
        if len(out) >= limit:
            break
        if not isinstance(it, dict):
            continue
        ht = (it.get("homeType") or "").upper()
        if not _type_matches(property_type, ht):
            continue
        status = (it.get("homeStatus") or "").upper()
        if status and status != "FOR_SALE":
            continue
        url = it.get("detailUrl") or ""
        if url.startswith("/"):
            url = "https://www.zillow.com" + url
        out.append({
            "_preverified": True,   # data comes from Zillow itself
            "zpid": str(it.get("zpid") or "") or None,
            "url": url,
            "address": it.get("address") or it.get("streetAddress"),
            "city": it.get("city"), "state": it.get("state"),
            "zip": it.get("zipcode"),
            "price": it.get("price"),
            "beds": it.get("bedrooms"), "baths": it.get("bathrooms"),
            "sqft": it.get("livingArea"),
            "year_built": None, "last_renovated": None,
            "days_on_market": it.get("daysOnZillow"),
            "listing_status": "for_sale",
            "home_type": ht,
            "image": it.get("imgSrc"),
            "zestimate": it.get("zestimate"),
            "arv_estimate": None, "rehab_estimate": None,
        })
    log.info("Zillow API search %s (doz=%s): %d listings",
             location, body["days_on_zillow"], len(out))
    return out
