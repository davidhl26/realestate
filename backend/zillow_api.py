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
