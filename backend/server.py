"""FastAPI server for the Flip Board desktop app.

Endpoints:
  GET    /api/deals                        list all
  GET    /api/deals/{id}                   single deal + metrics
  POST   /api/deals                        create / upsert
  PATCH  /api/deals/{id}                   partial update
  DELETE /api/deals/{id}                   delete
  GET    /api/deals/{id}/metrics           compute metrics on demand
  GET    /api/deals/{id}/pdf               returns the deal PDF (auto-builds)
  GET    /api/board/comparison-pdf         returns multi-deal comparison PDF
  GET    /api/board/aggregates             board statistics
  POST   /api/scrape                       {url} -> normalized seed
  GET    /api/healthz                      health check

Static frontend served at /
"""

import io
import logging
import os
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Body, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from . import analyzer, scraper, pdf_gen
from .db import DealsDB

log = logging.getLogger("flip-board.server")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Paths
ROOT = Path(__file__).resolve().parent.parent
# In production (Render/Docker), set FLIPBOARD_DATA_DIR=/var/data so JSON
# files + browser profile + AI config persist on the mounted disk.
DATA_DIR = Path(os.environ.get("FLIPBOARD_DATA_DIR") or (ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
FRONTEND_DIR = ROOT / "frontend"
PDF_DIR = DATA_DIR / "pdfs"
PDF_DIR.mkdir(parents=True, exist_ok=True)
DOCS_DIR = DATA_DIR / "deal-docs"   # uploaded per-deal documents (inspections…)
DOCS_DIR.mkdir(parents=True, exist_ok=True)
PHOTOS_DIR = DATA_DIR / "deal-photos"   # locally archived listing photos
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "flip-board.json"
COOKIE_STORE = DATA_DIR / "auth-cookies.json"

db = DealsDB(DB_PATH)
scraper.set_cookie_store_path(COOKIE_STORE)

# CRM database
from .crm import CrmDB
crm = CrmDB(DATA_DIR / "crm.json")

# AI chat module
from . import ai_chat

# Leads module
from .leads import LeadsDB, analyze_lead
leads_db = LeadsDB(DATA_DIR / "leads.json")

# Batch scraping
from . import batch_scraper

# Foreclosure auctions / skip-trace queue
from . import auctions as _auctions_mod
from .auctions import AuctionsDB, scrape_auction_list, PIPELINE_STAGES
auctions_db = AuctionsDB(DATA_DIR / "auctions.json")
_auctions_mod.set_credentials_path(DATA_DIR / "realauction-credentials.json")

# Auction watchlist (tracked auctions + recheck for daily alerts)
from .watchlist import WatchlistDB
watchlist_db = WatchlistDB(DATA_DIR / "auction-watchlist.json")

# Zillow watches (saved searches that run repeatedly and diff results)
from .watches import WatchesDB
watches_db = WatchesDB(DATA_DIR / "zillow-watches.json")

# Deal Radar — "interesting" finds auto-surfaced by the watches
from .radar import RadarDB
radar_db = RadarDB(DATA_DIR / "radar.json")

# Initialize browser scraper profile dir
try:
    from . import scraper_browser
    scraper_browser.set_profile_dir(DATA_DIR / ".browser-profile")
except ImportError:
    pass

# Initialize AI research config
try:
    from . import ai_research
    ai_research.set_config_path(DATA_DIR / "ai-config.json")
except ImportError:
    pass

app = FastAPI(title="Flip Board", version="0.1.0")
# CORS: in production set ALLOWED_ORIGINS=https://your-netlify-site.netlify.app
# (comma-separated for multiple). Defaults to "*" for local dev.
_allowed_origins = os.environ.get("ALLOWED_ORIGINS", "*")
_origins = [o.strip() for o in _allowed_origins.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# Authentication (optional). Enabled only when APP_PASSWORD is set (env or
# config). When unset, the app is open (local dev / desktop). Single shared
# password → signed cookie. Static assets stay public (no secrets in them);
# only the data API is gated.
# ============================================================================
import hashlib

_AUTH_OPEN_PATHS = {"/api/login", "/api/logout", "/api/auth-status", "/api/healthz"}


def _app_password():
    from . import ai_research
    return os.environ.get("APP_PASSWORD") or ai_research.read_config().get("app_password") or None


def _auth_token(pw: str) -> str:
    return hashlib.sha256(("flip-board::" + pw).encode()).hexdigest()


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    pw = _app_password()
    if pw:
        path = request.url.path
        if path.startswith("/api/") and path not in _AUTH_OPEN_PATHS:
            if request.cookies.get("fb_auth") != _auth_token(pw):
                return JSONResponse({"error": "Authentication required"}, status_code=401)
    return await call_next(request)


@app.get("/api/auth-status")
def auth_status(request: Request):
    pw = _app_password()
    if not pw:
        return {"required": False, "authed": True}
    return {"required": True, "authed": request.cookies.get("fb_auth") == _auth_token(pw)}


@app.post("/api/login")
def login(request: Request, payload: dict = Body(...)):
    pw = _app_password()
    if not pw:
        return {"ok": True, "required": False}
    if (payload.get("password") or "") != pw:
        raise HTTPException(401, "Incorrect password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie("fb_auth", _auth_token(pw), httponly=True, samesite="lax",
                    max_age=60 * 60 * 24 * 30,
                    secure=(request.url.scheme == "https"))
    return resp


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("fb_auth")
    return resp


_MAX_PLAUSIBLE_DOM = 1825  # ~5 years; above this it's almost surely a bad parse


def _set_dom_anchor(deal: dict, capture_date=None, force: bool = False) -> None:
    """Anchor the Zillow listing date so 'days on Zillow' self-updates daily
    without re-scraping: listed_date = capture_date - days_on_market.
    capture_date is TODAY for a fresh scrape (create/refresh); for backfilling
    an old stored value, pass the deal's added_date (when it was captured)."""
    dom = deal.get("days_on_market")
    if dom in (None, ""):
        return
    try:
        dom = int(dom)
    except (TypeError, ValueError):
        return
    if dom < 0 or dom > _MAX_PLAUSIBLE_DOM:
        return  # implausible — don't anchor garbage
    if not (force or not deal.get("zillow_listed_date")):
        return
    cap = capture_date or date.today()
    deal["zillow_listed_date"] = (cap - timedelta(days=dom)).isoformat()


def _current_dom(deal: dict):
    """Days-on-Zillow as of TODAY, computed from the anchored listing date so it
    increments every day on its own. Falls back to days_on_market offset by the
    capture date for deals saved before the anchor existed. Returns None for
    missing or implausible (>5y) values."""
    val = None
    base = deal.get("zillow_listed_date")
    if base:
        try:
            val = (date.today() - date.fromisoformat(str(base)[:10])).days
        except ValueError:
            val = None
    if val is None:
        dom = deal.get("days_on_market")
        if dom in (None, ""):
            return None
        try:
            dom = int(dom)
        except (TypeError, ValueError):
            return None
        cap = deal.get("added_date") or deal.get("last_analyzed")
        try:
            val = dom + (date.today() - date.fromisoformat(str(cap)[:10])).days
        except (TypeError, ValueError):
            val = dom
    if val is None or val < 0 or val > _MAX_PLAUSIBLE_DOM:
        return None
    return val


def _auto_enrich_deal(deal_id: str, do_ai: bool = True):
    """Background worker run after create: archive photos locally, then fill
    missing ARV (and rehab) via AI so the price/risk engines never sit on
    zeros. Progress is tracked on deal.ai_auto ('running'→'done'/'error')."""
    import threading  # noqa: F401  (documents intent; thread started by caller)
    d = db.get_deal(deal_id)
    if not d:
        return
    try:
        d["ai_auto"] = "running"
        db.upsert_deal(d)

        # 1) Photos: copy remote gallery to local storage (free, no AI).
        try:
            from . import photo_store
            if photo_store.archive_deal_photos(d, PHOTOS_DIR):
                db.upsert_deal(d)
        except Exception:
            log.exception("photo archive failed for %s", deal_id)

        # 2) ARV via AI when missing.
        if do_ai and not (d.get("arv_base") or 0):
            r = ai_research.research_arv(d)
            if r.get("ok") and r.get("arv_base"):
                d["arv_base"] = r["arv_base"]
                if r.get("arv_low"):  d["arv_low"] = r["arv_low"]
                if r.get("arv_high"): d["arv_high"] = r["arv_high"]
                d["arv_confidence"] = r.get("confidence", "Low")
                insights = d.get("ai_insights") or {}
                insights["arv"] = {"ok": True, "result": r, "auto": True}
                d["ai_insights"] = insights

        # 3) Rehab via AI when missing (only if we now have an ARV to price against).
        if do_ai and (d.get("arv_base") or 0) and not (d.get("rehab_base") or 0):
            rr = ai_research.estimate_rehab(d)
            if rr.get("ok") and rr.get("items"):
                total = sum(int(it.get("cost") or 0) for it in rr["items"])
                d["rehab_base"] = int(round(total * 1.15))
                d["rehab_items"] = rr["items"]
                d["rehab_contingency_pct"] = 15

        m = analyzer.compute_metrics(d)
        score, grade, signal = analyzer.compute_score(d, m)
        d["score"], d["grade"], d["signal"] = score, grade, signal
        _apply_risk(d, m)
        d["ai_auto"] = "done"
        db.upsert_deal(d)
    except Exception as e:
        log.exception("auto-enrich failed for %s", deal_id)
        try:
            d = db.get_deal(deal_id)
            if d:
                d["ai_auto"] = f"error: {str(e)[:120]}"
                db.upsert_deal(d)
        except Exception:
            pass


def _maybe_spawn_auto_enrich(deal: dict):
    """Fire the enrich worker for a fresh deal when useful: photos always (if a
    remote gallery exists), AI only when ARV is missing AND auto_research is on."""
    import threading
    needs_photos = any(str(u).startswith("http") for u in (deal.get("image_gallery") or []))
    cfg = ai_research.read_config()
    auto_ai = cfg.get("auto_research", True) and ai_research.is_configured()
    needs_ai = auto_ai and not (deal.get("arv_base") or 0)
    if not (needs_photos or needs_ai):
        return
    threading.Thread(target=_auto_enrich_deal, args=(deal["id"], needs_ai),
                     daemon=True).start()


def _global_margin() -> float:
    """The owner's target flip margin (Settings) — drives every max-offer."""
    try:
        return float(ai_research.read_config().get("target_margin_pct") or 15)
    except (TypeError, ValueError):
        return 15.0


def _apply_risk(deal: dict, m: dict) -> dict:
    """Run the deterministic safety screen and persist its fields on the deal."""
    risk = analyzer.assess_risk(deal, m)
    deal["risk_grade"] = risk["risk_grade"]
    deal["deal_breakers"] = risk["deal_breakers"]
    deal["risk_flags"] = risk["risk_flags"]
    deal["risk_summary"] = risk["risk_summary"]
    return risk


def _enrich(deal: dict) -> dict:
    """Compute metrics + score + signal + risk, return augmented dict."""
    m = analyzer.compute_metrics(deal)
    score, grade, signal = analyzer.compute_score(deal, m)
    risk = analyzer.assess_risk(deal, m)
    deal["days_on_market"] = _current_dom(deal)  # live value (daily); None hides garbage
    offer = analyzer.recommended_max_offer(deal, m, default_margin=_global_margin())
    offer["blocked"] = bool(risk.get("deal_breakers"))
    return {"deal": deal, "metrics": m, "score": score,
            "grade": grade, "signal": signal, "risk": risk, "max_offer": offer}


# Changes on every deploy/restart — the frontend polls it and self-reloads
# when it differs from the version it booted with (stale-SPA killer).
import uuid as _uuid_mod
APP_BOOT_ID = _uuid_mod.uuid4().hex[:12]


@app.get("/api/version")
def app_version():
    return {"version": APP_BOOT_ID}


@app.get("/api/healthz")
def healthz():
    return {"ok": True, "deals": len(db.list_deals())}


@app.get("/api/deals-duplicates")
def deals_duplicates():
    """Groups of deals that look like the same property (loose address key)."""
    return db.duplicate_groups()


@app.get("/api/backups")
def backups_list():
    """Available point-in-time snapshots of the data files (newest first)."""
    from . import backup
    return backup.list_snapshots(DATA_DIR)


def _fin_method(m):
    """Selected financing method for a computed-metrics dict ('cash' default)."""
    return ((m.get("selected_financing") or {}).get("method")) or "cash"


def _fin_net_profit(m):
    """Gain that matches the deal's chosen financing: cash → plain net profit;
    financed (hard money / private / etc.) → net profit after financing cost."""
    sel = m.get("selected_financing") or {}
    if _fin_method(m) != "cash" and sel.get("net_profit_after_financing") is not None:
        return sel["net_profit_after_financing"]
    return m["net_profit"]


def _fin_roi(m):
    """ROI matching the chosen financing (on cash actually invested)."""
    sel = m.get("selected_financing") or {}
    if _fin_method(m) != "cash" and sel.get("roi_on_cash") is not None:
        return sel["roi_on_cash"]
    return m["roi"]


@app.get("/api/deals")
def list_deals():
    deals = db.list_deals()
    gm = _global_margin()
    out = []
    for d in deals:
        try:
            m = analyzer.compute_metrics(d)
            score, grade, signal = analyzer.compute_score(d, m)
            risk = analyzer.assess_risk(d, m)
            offer = analyzer.recommended_max_offer(d, m, default_margin=gm)
            out.append({
                "id": d["id"],
                "address": d.get("address", ""),
                "risk_grade": risk["risk_grade"],
                "deal_breakers": risk["deal_breakers"],
                "risk_flags": risk["risk_flags"],
                "max_offer": offer["max_offer"],
                "max_offer_blocked": bool(risk["deal_breakers"]),
                "max_offer_gap": offer["gap_vs_price"],
                "ai_auto": d.get("ai_auto"),
                "city": d.get("city", ""),
                "state": d.get("state", ""),
                "beds": d.get("beds"),
                "baths": d.get("baths"),
                "sqft": d.get("sqft"),
                "year_built": d.get("year_built"),
                "days_on_market": _current_dom(d),
                "time_on_zillow": d.get("time_on_zillow"),
                "page_view_count": d.get("page_view_count"),
                "favorite_count": d.get("favorite_count"),
                "image": d.get("image"),
                "source_url": d.get("source_url"),
                "lat": d.get("lat"),
                "lng": d.get("lng"),
                "purchase_price": d.get("purchase_price"),
                "arv_base": d.get("arv_base"),
                "rehab_base": d.get("rehab_base"),
                "score": score,
                "grade": grade,
                "signal": signal,
                "status": d.get("status", "evaluating"),
                "net_profit": m["net_profit"],
                "roi": m["roi"],
                # Financing-aware gain: cash → net_profit; hard money/other →
                # net profit after financing (interest + points + lender fees).
                "financing_method": _fin_method(m),
                "net_profit_financed": _fin_net_profit(m),
                "roi_financed": _fin_roi(m),
                "cap_rate": m["rent"]["cap_rate"],
                "brrrr_cf": m["brrrr"]["monthly_cash_flow"],
                "rule_70_pass": m["rule_70_pass"],
                "recommended_strategy": m["recommended_strategy"],
                "added_date": d.get("added_date"),
            })
        except Exception as e:
            log.warning("Skipping deal %s: %s", d.get("id"), e)
    return out


@app.get("/api/deals/{deal_id}")
def get_deal(deal_id: str):
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    return _enrich(d)


@app.post("/api/deals")
def create_deal(deal: dict = Body(...)):
    # Only the address is strictly required; price/ARV/rehab can be filled later
    # (e.g. quick-insert from Search, then refine).
    if not deal.get("address"):
        raise HTTPException(400, "address is required")
    # Address dedup: same house number + street name (+zip) as an existing deal
    # → refuse unless the caller explicitly forces (Add-form dialog).
    force_dup = bool(deal.pop("force_duplicate", False))
    if not force_dup and not deal.get("id"):
        dup = db.find_duplicate(deal["address"])
        if dup:
            raise HTTPException(409, f"Possible duplicate: \"{dup.get('address')}\" "
                                     f"is already on the board (id: {dup['id']}).")
    if not deal.get("purchase_price"):
        deal["purchase_price"] = 0
    # Auto-compute score/grade/signal so they are stored
    m = analyzer.compute_metrics(deal)
    score, grade, signal = analyzer.compute_score(deal, m)
    deal["score"] = score
    deal["grade"] = grade
    deal["signal"] = signal
    _apply_risk(deal, m)
    _set_dom_anchor(deal)
    saved = db.upsert_deal(deal)
    _maybe_spawn_auto_enrich(saved)   # photos + missing-ARV research, in background
    return _enrich(saved)


@app.patch("/api/deals/{deal_id}")
def patch_deal(deal_id: str, updates: dict = Body(...)):
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    d.update(updates)
    # Recompute score
    m = analyzer.compute_metrics(d)
    score, grade, signal = analyzer.compute_score(d, m)
    d["score"] = score
    d["grade"] = grade
    d["signal"] = signal
    _apply_risk(d, m)
    # A fresh days_on_market re-anchors the listing date — unless the caller set
    # the anchor explicitly (then that wins).
    if "days_on_market" in updates and "zillow_listed_date" not in updates:
        _set_dom_anchor(d, force=True)
    saved = db.upsert_deal(d)
    return _enrich(saved)


@app.delete("/api/deals/{deal_id}")
def delete_deal(deal_id: str):
    ok = db.delete_deal(deal_id)
    if not ok:
        raise HTTPException(404, "Deal not found")
    # Cleanup PDF
    pdf = PDF_DIR / f"deal-{deal_id}.pdf"
    if pdf.exists():
        pdf.unlink()
    return {"ok": True}


@app.get("/api/deals/{deal_id}/metrics")
def deal_metrics(deal_id: str):
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    return analyzer.compute_metrics(d)


REFRESHABLE_FIELDS = [
    "image", "image_gallery", "sale_comparables", "rent_comparables",
    "lat", "lng", "zillow_estimate", "realtor_estimate", "redfin_estimate",
    "comp_value_estimate", "comp_value_low", "comp_value_high",
    "rent_low", "rent_high", "foundation", "basement", "roof_notes",
    "hvac_notes", "water_heater_notes", "school_rating", "flood_risk",
    "showing_date", "strategy_hint", "listing_name", "description",
    "source", "source_url", "external_id", "external_link",
    # Zillow market-activity stats (change over time → refreshable)
    "days_on_market", "time_on_zillow", "page_view_count", "favorite_count",
]


@app.post("/api/deals/{deal_id}/refresh")
def refresh_deal(deal_id: str, payload: dict = Body(default={})):
    """Re-scrape source_url (or override URL) and merge non-financial fields."""
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    url = (payload.get("url") or d.get("source_url") or "").strip()
    if not url:
        raise HTTPException(400, "No source URL on this deal — pass {url: ...} in body")
    try:
        seed = scraper.scrape(url)
    except Exception as e:
        raise HTTPException(500, f"Scrape failed: {e}")
    if seed.get("scrape_error") or seed.get("error"):
        return {"ok": False, "error": seed.get("scrape_error") or seed.get("error")}

    updates = {}
    for f in REFRESHABLE_FIELDS:
        if seed.get(f) is not None:
            updates[f] = seed[f]
    updates["source_url"] = url
    for k, v in updates.items():
        d[k] = v
    if "days_on_market" in updates:
        _set_dom_anchor(d, force=True)
    saved = db.upsert_deal(d)
    return {
        "ok": True,
        "fields_updated": len(updates),
        "photos": len(updates.get("image_gallery", [])),
        "sale_comps": len(updates.get("sale_comparables", [])),
        "rent_comps": len(updates.get("rent_comparables", [])),
    }


def _geocode(address: str) -> Optional[tuple]:
    """Address → (lat, lng). Google Geocoding when a Maps key is configured,
    else free Nominatim (OSM). Returns None on failure."""
    import httpx as _httpx
    if not address or len(address.strip()) < 8:
        return None
    key = ai_research.get_maps_key()
    try:
        if key:
            r = _httpx.get("https://maps.googleapis.com/maps/api/geocode/json",
                           params={"address": address, "key": key}, timeout=12)
            js = r.json()
            if js.get("results"):
                loc = js["results"][0]["geometry"]["location"]
                return (loc["lat"], loc["lng"])
        else:
            r = _httpx.get("https://nominatim.openstreetmap.org/search",
                           params={"q": address, "format": "json", "limit": 1,
                                   "countrycodes": "us"},
                           headers={"User-Agent": "flip-board/1.0"}, timeout=12)
            js = r.json()
            if js:
                return (float(js[0]["lat"]), float(js[0]["lon"]))
    except Exception as e:
        log.warning("geocode failed for %s: %s", address[:50], str(e)[:80])
    return None


@app.get("/api/deals/{deal_id}/comps-map")
def deal_comps_map(deal_id: str):
    """Subject + comparables with coordinates for the neighborhood map.

    Gathers comps from sale_comparables and the AI ARV research, geocodes the
    ones missing lat/lng (results cached on the deal in comps_geo), and
    returns pins ready to plot."""
    import time as _time
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    if not (d.get("lat") and d.get("lng")):
        # Try to geocode the subject itself once.
        loc = _geocode(d.get("address", ""))
        if loc:
            d["lat"], d["lng"] = loc
            db.upsert_deal(d)
        else:
            return {"ok": False, "error": "No coordinates for this property "
                    "(address could not be geocoded)."}

    # Collect comps: scraped first, then AI ARV comps.
    raw = []
    for c in (d.get("sale_comparables") or []):
        raw.append({"address": c.get("address"), "price": c.get("price"),
                    "beds": c.get("beds"), "baths": c.get("baths"),
                    "sqft": c.get("sqft"), "date": c.get("date"),
                    "distance_mi": c.get("distance_mi"),
                    "lat": c.get("lat"), "lng": c.get("lng"), "source": "scrape"})
    arv_ins = ((d.get("ai_insights") or {}).get("arv") or {}).get("result") or {}
    for c in (arv_ins.get("comparables") or []):
        if isinstance(c, dict) and c.get("address"):
            raw.append({"address": c.get("address"),
                        "price": c.get("sold_price") or c.get("price"),
                        "beds": c.get("beds"), "baths": c.get("baths"),
                        "sqft": c.get("sqft"),
                        "date": c.get("sold_date") or c.get("date"),
                        "distance_mi": c.get("distance_mi"),
                        "lat": c.get("lat"), "lng": c.get("lng"), "source": "ai"})
    # Neighborhood sold layer (find_area_sales) — the Zillow-style price tags.
    for s in (d.get("area_sales") or []):
        if isinstance(s, dict) and s.get("address"):
            raw.append({"address": s.get("address"), "price": s.get("price"),
                        "beds": s.get("beds"), "sqft": s.get("sqft"),
                        "date": s.get("date"), "lat": s.get("lat"),
                        "lng": s.get("lng"), "url": s.get("url"), "source": "sold"})

    # Dedupe by address, geocode missing coords (cached on the deal).
    cache = d.get("comps_geo") or {}
    seen, comps, geocoded, ungeocoded = set(), [], 0, 0
    nominatim = not ai_research.get_maps_key()
    for c in raw:
        addr = (c.get("address") or "").strip()
        akey = addr.lower()
        if not addr or akey in seen:
            continue
        seen.add(akey)
        if not (c.get("lat") and c.get("lng")):
            if akey in cache:
                cached = cache[akey]
                if cached:            # None = geocode already failed; skip
                    c["lat"], c["lng"] = cached
            elif geocoded < 12:   # bound per request
                loc = _geocode(addr)
                geocoded += 1
                if nominatim:
                    _time.sleep(1.0)   # Nominatim fair-use: 1 req/s
                cache[akey] = list(loc) if loc else None
                if loc:
                    c["lat"], c["lng"] = loc
            else:
                ungeocoded += 1   # cap hit — a follow-up request will finish
        if c.get("lat") and c.get("lng"):
            comps.append(c)
    if geocoded:
        d["comps_geo"] = cache
        db.upsert_deal(d)

    # Sanity filter: a true comp sits within ~2 miles of the subject. Anything
    # farther is almost surely a bad geocode (wrong street matched in another
    # part of town) and would zoom the map out to region level — drop it.
    import math
    def _km(lat1, lng1, lat2, lng2):
        rl1, rl2 = math.radians(lat1), math.radians(lat2)
        dlat, dlng = rl2 - rl1, math.radians(lng2 - lng1)
        a = math.sin(dlat / 2) ** 2 + math.cos(rl1) * math.cos(rl2) * math.sin(dlng / 2) ** 2
        return 6371 * 2 * math.asin(math.sqrt(a))
    # Neighborhood radius caps. Verified empirically: legit same-neighborhood
    # streets (Shady Oak/Oak Park vs Cranwood) sit at ~2.5 km, so sold pins get
    # 4 km; a farther hit is a wrong-street geocode. ARV comps may be broader.
    kept = [c for c in comps
            if _km(d["lat"], d["lng"], c["lat"], c["lng"])
               <= (4.0 if c.get("source") == "sold" else 8.0)]
    dropped = len(comps) - len(kept)
    comps = kept

    return {"ok": True,
            "subject": {"lat": d["lat"], "lng": d["lng"],
                        "address": d.get("address", ""),
                        "price": d.get("purchase_price"),
                        "arv": d.get("arv_base"), "image": d.get("image")},
            "comps": comps, "dropped_far": dropped, "ungeocoded": ungeocoded}


@app.post("/api/deals/{deal_id}/area-sales")
def deal_area_sales(deal_id: str):
    """Fetch recent neighborhood sales (AI web search) and store them on the
    deal — they show up as sold-price pins on the comps map."""
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add an Anthropic API key in Settings → AI.")
    res = ai_research.find_area_sales(d, max_sales=30, min_sales=20)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "search failed")}
    d["area_sales"] = res["sales"]
    d["area_sales_at"] = datetime.utcnow().isoformat() + "Z"
    db.upsert_deal(d)
    return {"ok": True, "count": len(res["sales"]), "notes": res.get("notes", ""),
            "sales": res["sales"]}


@app.post("/api/deals/archive-photos")
def archive_all_photos(payload: dict = Body(default={})):
    """Migrate remote photo galleries to local storage, `limit` deals per call
    (call repeatedly until remaining=0). Free, no AI."""
    from . import photo_store
    try:
        limit = max(1, min(int(payload.get("limit") or 5), 20))
    except (TypeError, ValueError):
        limit = 5
    deals = db.list_deals()
    pending = [d for d in deals
               if any(str(u).startswith("http") for u in (d.get("image_gallery") or []))]
    done = []
    for d in pending[:limit]:
        try:
            if photo_store.archive_deal_photos(d, PHOTOS_DIR):
                db.upsert_deal(d)
                done.append(d["id"])
        except Exception:
            log.exception("archive failed for %s", d.get("id"))
    return {"ok": True, "archived": done,
            "remaining": max(0, len(pending) - limit)}


@app.post("/api/deals/refresh-dom")
def refresh_all_dom():
    """Recompute 'days on Zillow' for every deal from its anchored listing date
    and persist it. No scraping, no cost — safe to run daily. (Display already
    computes this live, so this just keeps the stored value in sync.)"""
    data = db.raw()
    changed = 0
    for d in data.get("deals", []):
        if d.get("days_on_market") in (None, "") and not d.get("zillow_listed_date"):
            continue
        # Anchor from when the value was captured (added_date), not today, so an
        # old stored days_on_market isn't mistaken for today's count.
        cap = None
        try:
            cap = date.fromisoformat(str(d.get("added_date"))[:10])
        except (TypeError, ValueError):
            cap = None
        if not d.get("zillow_listed_date"):
            _set_dom_anchor(d, capture_date=cap)
        cur = _current_dom(d)
        if cur != d.get("days_on_market"):
            d["days_on_market"] = cur   # None clears implausible/garbage values
            changed += 1
    if changed:
        data["updated"] = datetime.utcnow().isoformat() + "Z"
        db._write(data)
    return {"ok": True, "updated": changed, "total": len(data.get("deals", []))}


def _refresh_document_risk(deal: dict):
    """Roll up uploaded-document findings so the risk engine + summary see them."""
    parts = []
    for doc in (deal.get("documents") or []):
        a = doc.get("analysis") or {}
        if a.get("summary"):
            parts.append(a["summary"])
        for b in (a.get("deal_breakers") or []):
            parts.append(b)
        for f in (a.get("findings") or []):
            if f.get("severity") in ("major", "safety"):
                parts.append(f.get("issue", ""))
    deal["document_summary"] = " ".join(p for p in parts if p)[:4000]


def _apply_document_to_deal(deal: dict, analysis: dict, apply_rehab: bool = True) -> dict:
    """Apply a document's extracted data to the deal (rehab, year), roll its
    findings into the risk engine, and recompute score + risk. Returns the
    fields applied. Shared by upload and the 'refresh from document' button."""
    applied = {}
    sugg = (analysis or {}).get("suggested_rehab")
    if apply_rehab and isinstance(sugg, (int, float)) and sugg > 0:
        deal["rehab_base"] = int(sugg)
        applied["rehab_base"] = int(sugg)
    kn = (analysis or {}).get("key_numbers") or {}
    if kn.get("year_built") and not deal.get("year_built"):
        deal["year_built"] = kn["year_built"]
        applied["year_built"] = kn["year_built"]
    _refresh_document_risk(deal)
    m = analyzer.compute_metrics(deal)
    score, grade, signal = analyzer.compute_score(deal, m)
    deal["score"], deal["grade"], deal["signal"] = score, grade, signal
    _apply_risk(deal, m)
    return applied


@app.post("/api/deals/{deal_id}/documents")
async def upload_deal_document(deal_id: str, file: UploadFile = File(...),
                               apply_rehab: str = Form("1")):
    """Upload a PDF document (inspection, appraisal, title…) to a deal: extract
    text, analyze with Claude, store the file + analysis, and roll findings into
    the deal's rehab + risk."""
    import uuid as _uuid
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add an Anthropic API key in Settings → AI.")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a .pdf")
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "Empty file")
    if len(pdf_bytes) > 25 * 1024 * 1024:
        raise HTTPException(413, "PDF too large (>25 MB)")

    extracted = pdf_importer.extract_text_from_pdf(pdf_bytes)
    text = (extracted.get("text") or "").strip() if extracted.get("ok") else ""
    # No text layer (scanned / vector PDF)? Send the PDF straight to Claude.
    res = ai_tasks.analyze_document(d, text=text, pdf_bytes=None if text else pdf_bytes)
    if not res.get("ok"):
        et = res.get("error_type")
        if et == "billing": raise HTTPException(402, res.get("error", ""))
        if et == "auth": raise HTTPException(401, res.get("error", ""))
        return {"ok": False, "error": res.get("error", "Analysis failed")}
    analysis = res.get("result") or {}

    doc_id = _uuid.uuid4().hex[:12]
    deal_dir = DOCS_DIR / deal_id
    deal_dir.mkdir(parents=True, exist_ok=True)
    try:
        (deal_dir / f"{doc_id}.pdf").write_bytes(pdf_bytes)
    except Exception as e:
        log.warning("Could not save document file: %s", e)

    rec = {
        "id": doc_id,
        "filename": file.filename,
        "size": len(pdf_bytes),
        "pages": extracted.get("page_count"),
        "uploaded_at": datetime.utcnow().isoformat() + "Z",
        "analysis": analysis,
    }
    d.setdefault("documents", []).append(rec)
    applied = _apply_document_to_deal(d, analysis, apply_rehab=apply_rehab not in ("0", "false", "no"))
    db.upsert_deal(d)
    return {"ok": True, "document": rec, "applied": applied,
            "risk_grade": d.get("risk_grade"), "deal_breakers": d.get("deal_breakers")}


@app.post("/api/deals/{deal_id}/documents/{doc_id}/reapply")
def reapply_deal_document(deal_id: str, doc_id: str):
    """Refresh the deal's data from an already-analyzed document (re-apply rehab,
    year, and risk rollup). No AI call — uses the stored analysis."""
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    doc = next((x for x in (d.get("documents") or []) if x.get("id") == doc_id), None)
    if not doc:
        raise HTTPException(404, "Document not found")
    applied = _apply_document_to_deal(d, doc.get("analysis") or {}, apply_rehab=True)
    db.upsert_deal(d)
    return {"ok": True, "applied": applied,
            "risk_grade": d.get("risk_grade"), "deal_breakers": d.get("deal_breakers")}


@app.delete("/api/deals/{deal_id}/documents/{doc_id}")
def delete_deal_document(deal_id: str, doc_id: str):
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    docs = d.get("documents") or []
    d["documents"] = [x for x in docs if x.get("id") != doc_id]
    if len(d["documents"]) == len(docs):
        raise HTTPException(404, "Document not found")
    f = DOCS_DIR / deal_id / f"{doc_id}.pdf"
    if f.exists():
        try: f.unlink()
        except OSError: pass
    _refresh_document_risk(d)
    m = analyzer.compute_metrics(d)
    _apply_risk(d, m)
    db.upsert_deal(d)
    return {"ok": True}


@app.get("/api/deals/{deal_id}/documents/{doc_id}/file")
def get_deal_document_file(deal_id: str, doc_id: str):
    f = DOCS_DIR / deal_id / f"{doc_id}.pdf"
    if not f.exists():
        raise HTTPException(404, "File not found")
    d = db.get_deal(deal_id) or {}
    doc = next((x for x in (d.get("documents") or []) if x.get("id") == doc_id), None)
    name = (doc or {}).get("filename") or f"{doc_id}.pdf"
    return FileResponse(str(f), media_type="application/pdf", filename=name)


# ---- Per-deal timestamped comments ----
@app.post("/api/deals/{deal_id}/comments")
def add_deal_comment(deal_id: str, payload: dict = Body(...)):
    import uuid as _uuid
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    d.setdefault("comments", []).append({
        "id": _uuid.uuid4().hex[:8], "text": text[:4000],
        "created_at": datetime.utcnow().isoformat() + "Z",
    })
    db.upsert_deal(d)
    return {"ok": True, "comments": d["comments"]}


@app.delete("/api/deals/{deal_id}/comments/{comment_id}")
def delete_deal_comment(deal_id: str, comment_id: str):
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    d["comments"] = [c for c in d.get("comments", []) if c.get("id") != comment_id]
    db.upsert_deal(d)
    return {"ok": True, "comments": d["comments"]}


@app.get("/api/deals/{deal_id}/pdf")
def deal_pdf(deal_id: str):
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    out = PDF_DIR / f"deal-{deal_id}.pdf"
    try:
        pdf_gen.build_deal_pdf(d, str(out))
    except Exception as e:
        log.exception("PDF generation failed")
        raise HTTPException(500, f"PDF generation failed: {e}")
    return FileResponse(out, media_type="application/pdf",
                        filename=out.name)


@app.get("/api/deals/{deal_id}/prequal-letter")
def deal_prequal_letter(deal_id: str):
    """Generate the fix-and-flip financing pre-qualification letter (PDF),
    dated today, with this deal's address as the subject property."""
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    from . import prequal_letter
    out = PDF_DIR / f"prequal-{deal_id}.pdf"
    try:
        prequal_letter.build_prequal_pdf(d, str(out))
    except Exception as e:
        log.exception("Pre-qual letter generation failed")
        raise HTTPException(500, f"Letter generation failed: {e}")
    slug = (d.get("address") or "property").split(",")[0].replace(" ", "-")
    return FileResponse(out, media_type="application/pdf",
                        filename=f"PreQualification-Letter-{slug}.pdf")


@app.post("/api/deals/{deal_id}/rehab-estimate")
def deal_rehab_estimate(deal_id: str):
    """AI itemized renovation budget for this deal."""
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    from . import ai_research
    return ai_research.estimate_rehab(d)


@app.post("/api/deals/{deal_id}/pdf-with-options")
def deal_pdf_with_options(deal_id: str, options: dict = Body(...)):
    """Generate PDF with user-specified strategy, financing, and fee overrides."""
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    # Clone the deal so we don't mutate the stored copy
    deal_copy = dict(d)

    # Map modal options onto the deal fields used by the analyzer
    if options.get("holding_months") is not None:
        deal_copy["holding_months"] = int(options["holding_months"])
    if options.get("holding_cost_monthly") is not None:
        deal_copy["holding_cost_monthly"] = int(options["holding_cost_monthly"])
    if options.get("selling_cost_pct") is not None:
        deal_copy["selling_cost_pct"] = float(options["selling_cost_pct"])

    # Compute financing-derived holding cost addition
    # The analyzer's holding_cost_monthly is a flat figure, so we fold
    # financing interest into that bucket for the chosen term.
    fin_method = options.get("financing_method", "cash")
    purchase = deal_copy.get("purchase_price", 0) or 0
    rehab = deal_copy.get("rehab_base", 0) or 0
    arv = deal_copy.get("arv_base", 0) or 0
    holding_months = deal_copy.get("holding_months", 5) or 5

    financing_cost = 0
    points_paid = 0
    lender_fees_paid = 0
    cash_needed = purchase + rehab
    loan_amount = 0
    if fin_method and fin_method != "cash":
        ltv_pct = float(options.get("loan_ltv_pct") or 0)
        rate_pct = float(options.get("interest_rate_pct") or 0)
        orig_pct = float(options.get("origination_pct") or 0)
        lender_fees_pct = float(options.get("lender_fees_pct") or 0)
        term_months = int(options.get("loan_term_months") or holding_months)
        rehab_financed = options.get("rehab_financed", "yes") == "yes"
        loan_base = purchase * ltv_pct / 100
        loan_amount = loan_base + (rehab if rehab_financed else 0)
        interest = loan_amount * (rate_pct / 100) * (term_months / 12)
        points_paid = loan_amount * (orig_pct / 100)
        lender_fees_paid = loan_amount * (lender_fees_pct / 100)
        financing_cost = interest + points_paid + lender_fees_paid
        cash_needed = ((purchase - loan_base) + (0 if rehab_financed else rehab)
                       + points_paid + lender_fees_paid)

    # Attach a "scenario" block to the deal for the PDF generator to use
    deal_copy["scenario"] = {
        "strategy": options.get("strategy", "flip"),
        "financing_method": fin_method,
        "loan_amount": loan_amount,
        "loan_ltv_pct": options.get("loan_ltv_pct"),
        "interest_rate_pct": options.get("interest_rate_pct"),
        "origination_pct": options.get("origination_pct"),
        "lender_fees_pct": options.get("lender_fees_pct"),
        "loan_term_months": options.get("loan_term_months"),
        "rehab_financed": options.get("rehab_financed"),
        "purchase_closing_pct": options.get("purchase_closing_pct", 2),
        "due_diligence_fees": options.get("due_diligence_fees", 0),
        "other_fees": options.get("other_fees", 0),
        "financing_cost": financing_cost,
        "points_paid": points_paid,
        "lender_fees_paid": lender_fees_paid,
        "cash_needed": cash_needed,
    }

    # Bake financing + extra fees into the base holding total so the analyzer
    # produces the correct net profit.
    extras = financing_cost + float(options.get("due_diligence_fees", 0) or 0) + \
             float(options.get("other_fees", 0) or 0)
    # Spread the extras across holding so the per-month average still works,
    # then the all-in calculation captures them.
    if holding_months > 0:
        existing_monthly = deal_copy.get("holding_cost_monthly", 500) or 500
        deal_copy["holding_cost_monthly"] = int(existing_monthly + extras / holding_months)

    out = PDF_DIR / f"deal-{deal_id}-scenario.pdf"
    try:
        pdf_gen.build_deal_pdf(deal_copy, str(out))
    except Exception as e:
        log.exception("Scenario PDF generation failed")
        raise HTTPException(500, f"PDF generation failed: {e}")
    return FileResponse(out, media_type="application/pdf",
                        filename=out.name)


@app.get("/api/board/comparison-pdf")
def comparison_pdf():
    deals = db.list_deals()
    if len(deals) < 2:
        raise HTTPException(400, "Need at least 2 deals on the board")
    out = PDF_DIR / "flip-board-comparison.pdf"
    try:
        pdf_gen.build_comparison_pdf(deals, str(out))
    except Exception as e:
        log.exception("Comparison PDF generation failed")
        raise HTTPException(500, f"PDF generation failed: {e}")
    return FileResponse(out, media_type="application/pdf",
                        filename=out.name)


@app.get("/api/board/aggregates")
def board_aggs():
    deals = db.list_deals()
    pairs = []
    for d in deals:
        try:
            m = analyzer.compute_metrics(d)
            pairs.append((d, m))
        except Exception:
            continue
    return analyzer.board_aggregates(pairs)


# ---- Multi-Deal Comparison (Flip Score + AI verdict) ----
from . import comparator


@app.post("/api/board/compare")
def board_compare(payload: dict = Body(default={})):
    """Compare 2+ deals — Flip Score across 7 dimensions, per-category
    winners, recommended strategy per deal.

    Body:
      {"deal_ids": ["id1", "id2", ...]}   # optional — defaults to ALL deals
      {"include_verdict": true}           # optional — also runs Claude verdict
      {"focus": "cash flow" | "fast flip" | "lowest risk"}   # optional bias for verdict
    """
    deal_ids = payload.get("deal_ids") or []
    if deal_ids:
        deals = [d for d in db.list_deals() if d.get("id") in deal_ids]
    else:
        deals = db.list_deals()

    if len(deals) < 1:
        raise HTTPException(400, "Need at least 1 deal to compare")

    result = comparator.compare_deals(deals)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Compare failed"))

    if payload.get("include_verdict"):
        if not ai_research.is_configured():
            result["verdict_error"] = "AI not configured — add Anthropic API key in Settings."
        else:
            verdict = comparator.ai_verdict(result, focus=payload.get("focus"))
            result["ai_verdict"] = verdict

    return result


@app.get("/api/board/compare/all")
def board_compare_all():
    """Quick GET shortcut: compare ALL deals without AI verdict."""
    deals = db.list_deals()
    if not deals:
        return {"ok": True, "deals": [], "aggregates": {"count": 0}}
    return comparator.compare_deals(deals)


# State-level investment potential data (median price, market trend, flip ROI
# baseline, etc). Used by the USA Map view to color states even when the user
# has no deals there yet.
# Sources: 2026 H1 Realtor.com / Zillow / NAR market data + flip benchmarks.
_STATE_BASELINE = {
    # state: (median_price, yoy_pct, flip_market_grade, region)
    "AL": (180000,  2.5, "B",  "South"),    "AK": (340000, -1.5, "C",  "West"),
    "AZ": (440000,  1.0, "C+", "West"),     "AR": (185000,  3.0, "B",  "South"),
    "CA": (770000, -2.0, "C-", "West"),     "CO": (560000, -0.5, "C",  "West"),
    "CT": (380000,  4.0, "B+", "Northeast"),"DE": (370000,  3.5, "B+", "Northeast"),
    "FL": (380000,  0.5, "B-", "South"),    "GA": (340000,  3.5, "B+", "South"),
    "HI": (820000, -3.0, "D",  "West"),     "ID": (450000, -2.5, "C-", "West"),
    "IL": (260000,  4.5, "A-", "Midwest"),  "IN": (235000,  6.0, "A",  "Midwest"),
    "IA": (220000,  4.0, "A-", "Midwest"),  "KS": (215000,  3.5, "B+", "Midwest"),
    "KY": (210000,  5.0, "A-", "South"),    "LA": (220000,  1.5, "C+", "South"),
    "ME": (390000,  5.5, "A-", "Northeast"),"MD": (430000,  3.0, "B+", "Northeast"),
    "MA": (610000,  3.5, "B+", "Northeast"),"MI": (250000,  4.5, "A-", "Midwest"),
    "MN": (340000,  3.0, "B+", "Midwest"),  "MS": (170000,  3.5, "B+", "South"),
    "MO": (245000,  4.0, "A-", "Midwest"),  "MT": (480000, -2.0, "C-", "West"),
    "NE": (260000,  3.5, "B+", "Midwest"),  "NV": (445000, -1.0, "C",  "West"),
    "NH": (475000,  4.5, "A-", "Northeast"),"NJ": (510000,  4.0, "B+", "Northeast"),
    "NM": (320000,  1.5, "C+", "West"),     "NY": (480000,  2.5, "B",  "Northeast"),
    "NC": (370000,  3.0, "B+", "South"),    "ND": (260000,  3.5, "B+", "Midwest"),
    "OH": (215000,  5.5, "A",  "Midwest"),  "OK": (200000,  3.0, "B+", "South"),
    "OR": (510000, -1.5, "C",  "West"),     "PA": (260000,  4.0, "A-", "Northeast"),
    "RI": (455000,  4.0, "B+", "Northeast"),"SC": (310000,  3.5, "B+", "South"),
    "SD": (310000,  3.0, "B+", "Midwest"),  "TN": (370000,  3.5, "B+", "South"),
    "TX": (315000,  0.5, "B-", "South"),    "UT": (510000, -1.0, "C",  "West"),
    "VT": (380000,  4.0, "A-", "Northeast"),"VA": (400000,  3.5, "B+", "South"),
    "WA": (610000, -1.0, "C",  "West"),     "WV": (160000,  4.0, "A-", "South"),
    "WI": (290000,  4.0, "A-", "Midwest"),  "WY": (340000,  0.5, "C+", "West"),
    "DC": (610000,  1.0, "C+", "Northeast"),
}

_STATE_NAMES = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi",
    "MO":"Missouri","MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire",
    "NJ":"New Jersey","NM":"New Mexico","NY":"New York","NC":"North Carolina",
    "ND":"North Dakota","OH":"Ohio","OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania",
    "RI":"Rhode Island","SC":"South Carolina","SD":"South Dakota","TN":"Tennessee",
    "TX":"Texas","UT":"Utah","VT":"Vermont","VA":"Virginia","WA":"Washington",
    "WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming","DC":"D.C.",
}


@app.get("/api/board/states-map")
def board_states_map():
    """Per-state aggregate data for the USA heatmap view.

    Combines:
    - Your own deals data (count, avg score, total potential profit, top deal)
    - National baseline (median price, market YoY, flip-market grade)

    Returns a list of states with all metrics + a national ranking.
    """
    deals = db.list_deals()

    # Aggregate deals by state
    by_state: dict = {}
    for d in deals:
        st = (d.get("state") or "").strip().upper()
        if not st or len(st) != 2: continue
        if st not in by_state:
            by_state[st] = {
                "code": st, "count": 0, "scores": [], "net_profits": [],
                "best_deal": None, "best_score": -1, "deals": [],
            }
        rec = by_state[st]
        rec["count"] += 1
        if d.get("score") is not None: rec["scores"].append(d["score"])
        if d.get("net_profit") is not None: rec["net_profits"].append(d["net_profit"])
        if (d.get("score") or 0) > rec["best_score"]:
            rec["best_score"] = d.get("score") or 0
            rec["best_deal"] = {
                "id": d.get("id"), "address": d.get("address"),
                "score": d.get("score"), "net_profit": d.get("net_profit"),
            }
        rec["deals"].append({
            "id": d.get("id"), "address": d.get("address"),
            "score": d.get("score"), "city": d.get("city"),
            "net_profit": d.get("net_profit"), "signal": d.get("signal"),
        })

    # Merge with baseline for all 50 + DC
    out = []
    for code, name in _STATE_NAMES.items():
        baseline = _STATE_BASELINE.get(code, (0, 0, "?", "?"))
        median_price, yoy, market_grade, region = baseline
        my = by_state.get(code, {})
        scores = my.get("scores", [])
        avg_score = sum(scores) / len(scores) if scores else None
        total_profit = sum(my.get("net_profits", []) or [])

        # Combined score: market potential + your data
        # Market score from grade (A+=95, A=85, A-=80, B+=75, B=70, etc.)
        grade_to_score = {
            "A+": 95, "A": 88, "A-": 82, "B+": 75, "B": 68, "B-": 62,
            "C+": 55, "C": 48, "C-": 42, "D+": 35, "D": 28, "D-": 22, "F": 15, "?": 50,
        }
        market_score = grade_to_score.get(market_grade, 50)

        out.append({
            "code": code,
            "name": name,
            "region": region,
            "median_price": median_price,
            "yoy_pct": yoy,
            "market_grade": market_grade,
            "market_score": market_score,
            "my_deals_count": my.get("count", 0),
            "my_avg_score": round(avg_score, 1) if avg_score else None,
            "my_total_profit": int(total_profit) if total_profit else 0,
            "my_best_deal": my.get("best_deal"),
            "my_deals": my.get("deals", []),
        })

    # Sort by combined potential
    out.sort(key=lambda x: (-x["market_score"], -(x["my_avg_score"] or 0)))
    for i, s in enumerate(out, 1):
        s["rank"] = i

    return {
        "states": out,
        "total_states_with_deals": sum(1 for s in out if s["my_deals_count"] > 0),
        "total_deals": sum(s["my_deals_count"] for s in out),
        "total_profit_potential": sum(s["my_total_profit"] for s in out),
    }


@app.post("/api/search/listings")
def search_listings(payload: dict = Body(...)):
    """Interactive Zillow-area search: returns matching listings (no deals
    created) so the user can insert them one click at a time.

    Body: { "url": "<zillow search url>" } OR
          { "location": "Cleveland, OH", "price_max": 125000, "beds_min": 3 }
          + optional "max_listings" (1-40)."""
    from . import ai_research
    url = (payload.get("url") or "").strip()
    try:
        max_listings = int(payload.get("max_listings") or 15)
    except (TypeError, ValueError):
        max_listings = 15
    max_listings = max(1, min(max_listings, 40))

    if url and scraper.is_zillow_search_url(url):
        params = scraper.parse_zillow_search_url(url)
        if not params:
            raise HTTPException(400, "Could not read the filters from that Zillow search URL")
    else:
        loc = (payload.get("location") or url or "").strip()
        if not loc:
            raise HTTPException(400, "Provide a Zillow search URL or a location")
        params = {"search_term": loc}
    # Extra filters apply to both URL-based and location-based searches.
    for k in ("price_max", "price_min", "beds_min", "baths_min", "sqft_min", "property_type"):
        if payload.get(k):
            params[k] = payload[k]
    return ai_research.find_listings_in_area(params, max_listings=max_listings)


_STREET_WORD_RE = re.compile(
    r"\b(st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|way|ct|"
    r"court|pl|place|ter|terrace|cir|circle|hwy|highway|pkwy|parkway|trl|trail|"
    r"loop|run|path|sq|square|pike|cove|cv|row|walk|xing|crossing|aly|alley|pt|"
    r"point|ridge|rdg|bend|bnd|hl|hill|hts|heights|plz|plaza)\b", re.I)
_LEADING_NUM_RE = re.compile(r"^\s*\d+\s+\S")
_UNIT_RE = re.compile(r"#\s*\w|\b(unit|apt|ste|suite|lot)\b", re.I)


def _looks_like_address(a: str) -> bool:
    """A real property address has a street-type word or a leading house number
    — not merely a digit (a bare 'Cleveland, OH 44109' would wrongly pass)."""
    a = (a or "").strip()
    if len(a) < 5:
        return False
    return bool(_STREET_WORD_RE.search(a) or _LEADING_NUM_RE.search(a) or _UNIT_RE.search(a))


def _compute_max_bid(arv, rehab, *, target_margin_pct=20.0, holding=3000.0,
                     selling_pct=8.0, premium_pct=5.0, closing=2500.0):
    """Disciplined max auction bid from ARV + rehab.

    All-in cost at purchase = bid*(1+premium) + closing. We solve for the bid
    that still leaves the target profit after rehab, selling and holding costs.
    Also returns the classic 70%-rule reference (ARV*0.70 - rehab).
    """
    arv = float(arv or 0)
    rehab = float(rehab or 0)
    selling = arv * (selling_pct / 100.0)
    target_profit = arv * (target_margin_pct / 100.0)
    premium = premium_pct / 100.0
    # bid*(1+premium) = ARV - rehab - selling - holding - closing - target_profit
    net = arv - rehab - selling - float(holding) - float(closing) - target_profit
    max_bid = max(0.0, net / (1.0 + premium))
    mao70 = max(0.0, arv * 0.70 - rehab)
    all_in = max_bid * (1.0 + premium) + float(closing)
    profit_at_max = arv - all_in - rehab - selling - float(holding)
    return {
        "max_bid": int(round(max_bid)),
        "mao70": int(round(mao70)),
        "all_in_at_max": int(round(all_in + rehab)),
        "target_profit": int(round(target_profit)),
        "profit_at_max": int(round(profit_at_max)),
        "selling_costs": int(round(selling)),
        "holding": int(round(float(holding))),
        "closing": int(round(float(closing))),
        "premium_pct": premium_pct,
        "target_margin_pct": target_margin_pct,
    }


def _run_auction_analysis(payload: dict) -> dict:
    """Core auction analysis (shared by the endpoint and watchlist recheck)."""
    from . import ai_research
    address = (payload.get("address") or "").strip()
    if not address:
        return {"ok": False, "error": "address required"}

    arv_override = payload.get("arv_override")
    rehab_override = payload.get("rehab_override")
    ai = {"ok": True, "arv": None, "rehab": None, "risks": [],
          "condition_summary": "", "summary": "", "arv_confidence": "Low"}
    # Skip the AI call only if BOTH numbers are supplied manually.
    if arv_override in (None, "") or rehab_override in (None, ""):
        ai = ai_research.analyze_auction({
            "address": address,
            "beds": payload.get("beds"), "baths": payload.get("baths"),
            "sqft": payload.get("sqft"), "year_built": payload.get("year_built"),
            "opening_bid": payload.get("opening_bid"),
            "comments": payload.get("comments"),
        })
        if not ai.get("ok"):
            return {"ok": False, "error": ai.get("error", "AI analysis failed"),
                    "raw_text": ai.get("raw_text")}

    def _num(v, fallback=None):
        try: return float(v)
        except (TypeError, ValueError): return fallback
    arv = _num(arv_override) if arv_override not in (None, "") else _num(ai.get("arv"))
    rehab = _num(rehab_override) if rehab_override not in (None, "") else _num(ai.get("rehab"))
    if not arv:
        return {"ok": False, "error": "Could not determine an ARV — add one manually.",
                "ai": ai}

    try:
        margin = float(payload.get("target_margin_pct") or 20)
    except (TypeError, ValueError):
        margin = 20.0
    holding = _num(payload.get("holding"), 3000.0)
    bid = _compute_max_bid(arv, rehab or 0, target_margin_pct=margin, holding=holding)

    opening = _num(payload.get("opening_bid"))
    verdict = "go"
    note = ""
    if opening is not None and opening > 0:
        if opening > bid["max_bid"]:
            verdict, note = "pass", "The opening bid already exceeds your max — pass."
        elif opening > bid["max_bid"] * 0.9:
            verdict, note = "tight", "Tight margin: little room left before your max."
        else:
            verdict, note = "go", "Room to spare under your max bid."
    if not arv or (rehab and rehab > arv * 0.6):
        verdict = "caution" if verdict == "go" else verdict

    return {
        "ok": True,
        "address": address,
        "arv": int(round(arv)),
        "rehab": int(round(rehab or 0)),
        "arv_confidence": ai.get("arv_confidence", "Low"),
        "condition_summary": ai.get("condition_summary", ""),
        "summary": ai.get("summary", ""),
        "risks": ai.get("risks", []),
        "opening_bid": int(round(opening)) if opening else None,
        "auction_date": payload.get("auction_date") or None,
        "verdict": verdict,
        "verdict_note": note,
        "ai_used": arv_override in (None, "") or rehab_override in (None, ""),
        "model": ai.get("model"),
        **bid,
    }


@app.post("/api/auction/analyze")
def auction_analyze(payload: dict = Body(...)):
    """Analyze an auction property and recommend a disciplined MAX BID.

    Body: { address (required), opening_bid, auction_date, beds, baths, sqft,
            year_built, comments, target_margin_pct, holding,
            arv_override, rehab_override }
    The user still places the bid themselves — this only computes the number.
    """
    address = (payload.get("address") or "").strip()
    if not address:
        raise HTTPException(400, "address required (copy it from the auction listing)")
    # Reject a bare city ("cleveland ohio") unless overridden — but ACCEPT real
    # addresses even without a house number (auction.com often hides it), so we
    # key off a street-type word / leading number, not merely "has a digit".
    has_override = payload.get("arv_override") not in (None, "") and payload.get("rehab_override") not in (None, "")
    if not payload.get("force") and not has_override and not _looks_like_address(address):
        return {"ok": False, "error": (
            f"\"{address}\" looks like a city, not a specific address. "
            "Enter a street address, e.g. \"3744 W 135th St, Cleveland, OH 44111\". "
            "To explore an entire city, use the Search module.")}
    return _run_auction_analysis(payload)


@app.post("/api/auction/find")
def auction_find(payload: dict = Body(...)):
    """Discover auction/foreclosure listings in a city or state and compute a
    max bid for each. Body: { location (required), max_listings, target_margin_pct,
    holding, price_max, beds_min, property_type }.
    Does NOT scrape auction.com (blocked) — uses Claude + web search."""
    from . import ai_research
    location = (payload.get("location") or payload.get("search_term") or "").strip()
    if not location:
        raise HTTPException(400, "location required (a city or state)")
    try:
        max_listings = int(payload.get("max_listings") or 15)
    except (TypeError, ValueError):
        max_listings = 15
    max_listings = max(1, min(max_listings, 30))
    params = {"search_term": location}
    for k in ("price_max", "beds_min", "property_type"):
        if payload.get(k):
            params[k] = payload[k]
    res = ai_research.find_auctions_in_area(params, max_listings=max_listings)
    if not res.get("ok"):
        return res
    try:
        margin = float(payload.get("target_margin_pct") or 20)
    except (TypeError, ValueError):
        margin = 20.0
    try:
        holding = float(payload.get("holding") or 3000)
    except (TypeError, ValueError):
        holding = 3000.0
    # Compute a disciplined max bid per listing (cheap, no extra AI calls).
    for l in res["listings"]:
        arv = l.get("arv_estimate")
        rehab = l.get("rehab_estimate") or 0
        if not arv:
            l["max_bid"] = None
            l["verdict"] = "unknown"
            continue
        bid = _compute_max_bid(arv, rehab, target_margin_pct=margin, holding=holding)
        l.update({k: bid[k] for k in ("max_bid", "mao70", "profit_at_max", "target_margin_pct")})
        opening = l.get("opening_bid")
        if opening and isinstance(opening, (int, float)) and opening > 0:
            if opening > bid["max_bid"]:
                l["verdict"] = "pass"
            elif opening > bid["max_bid"] * 0.9:
                l["verdict"] = "tight"
            else:
                l["verdict"] = "go"
        else:
            l["verdict"] = "go" if bid["max_bid"] > 0 else "pass"
    return res


# ---- Auction watchlist (track + recheck for daily alerts) ----
@app.get("/api/auction/watchlist")
def auction_watchlist():
    return watchlist_db.list_items()


@app.post("/api/auction/watch")
def auction_watch(payload: dict = Body(...)):
    """Add/update a tracked auction (saves the latest analysis)."""
    if not (payload.get("address") or "").strip():
        raise HTTPException(400, "address required")
    return watchlist_db.upsert(payload)


@app.delete("/api/auction/watch/{item_id}")
def auction_unwatch(item_id: str):
    if not watchlist_db.delete(item_id):
        raise HTTPException(404, "Not found")
    return {"ok": True}


@app.post("/api/auction/watch/{item_id}/recheck")
def auction_recheck(item_id: str):
    """Re-run the AI analysis for one tracked auction and store the result."""
    item = watchlist_db.get(item_id)
    if not item:
        raise HTTPException(404, "Not found")
    res = _run_auction_analysis(item)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    return watchlist_db.upsert({**item, **res, "id": item_id})


@app.post("/api/auction/watchlist/recheck-all")
def auction_recheck_all(payload: dict = Body(default={})):
    """Re-run every tracked auction. Returns a digest for the daily alert:
    opportunities (verdict go/tight) and auctions happening soon."""
    from datetime import date, timedelta
    soon_days = int(payload.get("soon_days") or 14)
    results, opportunities, upcoming = [], [], []
    for item in watchlist_db.list_items():
        res = _run_auction_analysis(item)
        if res.get("ok"):
            saved = watchlist_db.upsert({**item, **res, "id": item["id"]})
        else:
            saved = item
        results.append({"id": saved["id"], "address": saved.get("address"),
                        "max_bid": saved.get("max_bid"), "verdict": saved.get("verdict"),
                        "auction_date": saved.get("auction_date")})
        if saved.get("verdict") in ("go", "tight"):
            opportunities.append(saved)
        ad = saved.get("auction_date")
        if ad:
            try:
                d = date.fromisoformat(str(ad)[:10])
                if date.today() <= d <= date.today() + timedelta(days=soon_days):
                    upcoming.append(saved)
            except ValueError:
                pass
    return {"ok": True, "checked": len(results), "results": results,
            "opportunities": opportunities, "upcoming": upcoming}


# ---- Zillow watches ----
# Radar freshness: only surface homes listed in the last ~24h (Zillow
# days_on_market 0 or 1 = just listed today/yesterday).
_RADAR_FRESH_MAX_DOM = 1
_RADAR_VERIFY_MAX = 12    # live-verify at most N candidates per watch run


def _radar_verify(url: str):
    """Live-check a Radar candidate on Zillow before it reaches the user.

    The AI search is good at DISCOVERING listings but unreliable on status and
    age — so Zillow's own homedetails page is the source of truth. Returns
    (verdict, data):
      "ok"         — confirmed FOR_SALE, and daysOnZillow within the window
      "sold"       — page says it's pending/sold/off-market (not buyable)
      "stale"      — active but older than the freshness window
      "unverified" — no homedetails URL, blocked page, or scrape error (the AI
                     may have hallucinated the listing) → caller drops it
    On "ok", data carries Zillow's real price/beds/baths/sqft/photos so the
    find shows exact numbers instead of AI estimates.
    """
    if "/homedetails/" not in (url or ""):
        return "unverified", None
    try:
        data = scraper.scrape(url)
    except Exception:
        log.exception("radar verify: scrape failed for %s", url)
        return "unverified", None
    if not isinstance(data, dict) or data.get("error") or data.get("captcha_detected"):
        return "unverified", None
    status = (data.get("home_status") or "").strip().upper()
    if not status:
        return "unverified", None
    if status != "FOR_SALE":
        return "sold", data
    dom = data.get("days_on_market")
    try:
        dom_val = int(dom) if dom is not None else None
    except (TypeError, ValueError):
        dom_val = None
    if dom_val is not None and dom_val > _RADAR_FRESH_MAX_DOM:
        return "stale", data
    return "ok", data


def _radar_config() -> dict:
    """Deal Radar settings (with sensible defaults)."""
    cfg = ai_research.read_config()
    return {
        "enabled": cfg.get("radar_enabled", True),
        "auto_add": cfg.get("radar_auto_add", True),
        "min_profit": float(cfg.get("radar_min_profit") or 25000),
        "margin": _global_margin(),
    }


def _radar_evaluate(listing: dict, cfg: dict):
    """Judge a fresh listing against the 4 interest criteria (target margin,
    min profit, 70% rule, low risk). Returns (interesting: bool, info: dict|None).
    info is None when the listing lacks the data needed to judge it."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    price = num(listing.get("price"))
    arv = num(listing.get("arv_estimate"))
    if not price or not arv or arv <= 0:
        return False, None
    rehab = num(listing.get("rehab_estimate"))
    if rehab is None:
        rehab = round(arv * 0.15)   # conservative default when the AI didn't estimate
    deal = {
        "address": listing.get("address", ""),
        "purchase_price": price, "arv_base": arv, "rehab_base": rehab,
        "year_built": listing.get("year_built"), "source": "radar",
        "notes": listing.get("notes") or "",
    }
    m = analyzer.compute_metrics(deal)
    risk = analyzer.assess_risk(deal, m)
    profit = m["net_profit"]
    margin_pct = (profit / arv * 100) if arv else 0
    checks = {
        "margin": margin_pct >= cfg["margin"],
        "profit": profit >= cfg["min_profit"],
        "rule70": bool(m.get("rule_70_pass")),
        "low_risk": risk.get("risk_grade") in ("A", "B") and not risk.get("deal_breakers"),
    }
    reasons = [
        f"Margin {margin_pct:.0f}% (target {cfg['margin']:.0f}%)",
        f"Profit ${int(profit):,}",
        "Passes 70% rule" if checks["rule70"] else "Fails 70% rule",
        f"Risk {risk.get('risk_grade', '?')}",
    ]
    info = {"price": int(price), "arv": int(arv), "rehab": int(rehab),
            "profit": int(profit), "margin_pct": round(margin_pct, 1),
            "roi": round(m.get("roi", 0), 1), "risk_grade": risk.get("risk_grade"),
            "checks": checks, "reasons": reasons}
    return all(checks.values()), info


def _radar_process(watch: dict, new_listings: list) -> dict:
    """For each newly-seen listing, surface the ones listed in the last ~24h as
    Radar finds (deduped by zpid/address). Each candidate is LIVE-VERIFIED on
    its Zillow page (status FOR_SALE + daysOnZillow ≤ 1) before it reaches the
    user — the AI only discovers; Zillow is the source of truth. Ones that pass
    all criteria are flagged interesting; with auto_add on, every verified find
    is added to the board. Returns a count breakdown."""
    from urllib.parse import quote
    cfg = _radar_config()
    c = {"found": len(new_listings or []), "fresh": 0, "surfaced": 0,
         "interesting": 0, "added": 0, "stale": 0, "sold": 0, "unknown_age": 0,
         "verified": 0, "unverified": 0}
    if not cfg.get("enabled"):
        return c
    label = watch.get("label") or watch.get("location") or "Watch"

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    verify_budget = _RADAR_VERIFY_MAX
    for l in new_listings or []:
        try:
            # Active-only gate: drop anything not currently for sale (sold,
            # pending, contingent, under contract, coming soon, off-market,
            # foreclosure/auction).
            status = (l.get("listing_status") or "").strip().lower()
            if status and not any(k in status for k in ("for_sale", "for sale", "active", "new")):
                c["sold"] += 1
                continue
            # Freshness gate: only keep homes listed in the last ~24h — the real
            # Zillow days_on_market must be present and 0 or 1. Unknown age is
            # dropped (we can't confirm it's fresh, so it may be days old).
            dom = l.get("days_on_market")
            try:
                dom_val = int(dom) if dom is not None else None
            except (TypeError, ValueError):
                dom_val = None
            if dom_val is None:
                c["unknown_age"] += 1
                continue
            if dom_val > _RADAR_FRESH_MAX_DOM:
                c["stale"] += 1
                continue
            c["fresh"] += 1
            addr = (l.get("address") or "").strip()
            if not addr:
                continue
            url = (l.get("url") or "").strip()
            # zpid from the homedetails URL → exact dedup (immune to address
            # formatting differences between scans).
            m = re.search(r"/(\d+)_zpid", url)
            zpid = m.group(1) if m else None
            if radar_db.has_zpid(zpid) or radar_db.has_address(addr):
                continue   # duplicate — already on the radar

            # Live verification on Zillow — the page itself must say FOR_SALE
            # and daysOnZillow ≤ 1. Kills sold/pending leftovers, stale
            # listings and AI-hallucinated URLs before they reach the user.
            if verify_budget <= 0:
                c["unverified"] += 1
                continue
            verify_budget -= 1
            verdict, zdata = _radar_verify(url)
            if verdict == "sold":
                c["sold"] += 1
                continue
            if verdict == "stale":
                c["stale"] += 1
                continue
            if verdict == "unverified":
                c["unverified"] += 1
                log.info("radar %s: dropped unverifiable listing %s (%s)",
                         label, addr, url or "no url")
                continue
            c["verified"] += 1
            # Zillow's live data is the source of truth — overwrite AI guesses.
            if zdata:
                for src, dst in (("price", "price"), ("bedrooms", "beds"),
                                 ("bathrooms", "baths"), ("sqft", "sqft"),
                                 ("year_built", "year_built"),
                                 ("city", "city"), ("state", "state"),
                                 ("zip", "zip")):
                    v = zdata.get(src)
                    if v is not None and v != "":
                        l[dst] = v
                zdom = zdata.get("days_on_market")
                try:
                    if zdom is not None:
                        dom_val = int(zdom)
                except (TypeError, ValueError):
                    pass

            # Full address (no repeated parts) + a Zillow URL.
            parts = [addr]
            city = (l.get("city") or "").strip()
            tail = f"{l.get('state', '')} {l.get('zip', '')}".strip()
            if city and city.lower() not in addr.lower():
                parts.append(city)
            if tail and tail.lower() not in addr.lower():
                parts.append(tail)
            full = ", ".join(p for p in parts if p)
            url = url or ("https://www.zillow.com/homes/" + quote(full or addr) + "_rb/")

            # Score it if we have enough data (ARV present). Interesting ones are
            # highlighted; but with auto_add on, EVERY verified 24h listing is
            # added to the board (status evaluating) and analyzed in the background.
            interesting, info = _radar_evaluate(l, cfg)
            price = _num(l.get("price"))
            deal_id = None
            dup = db.find_duplicate(addr)
            if dup:
                deal_id = dup["id"]
            elif cfg.get("auto_add"):
                note = (" · ".join(info["reasons"]) if info
                        else "New listing (last 24h) — analyzing…")
                tag = "✓ interesting" if (interesting and info) else "fresh"
                deal = {
                    "address": full or addr, "city": l.get("city", ""),
                    "state": l.get("state", ""), "zip": str(l.get("zip") or ""),
                    "beds": l.get("beds"), "baths": l.get("baths"), "sqft": l.get("sqft"),
                    "year_built": l.get("year_built"),
                    "purchase_price": (info["price"] if info else (int(price) if price else 0)),
                    "arv_base": (info["arv"] if info else 0),
                    "rehab_base": (info["rehab"] if info else 0),
                    "arv_confidence": "Low", "source": "radar", "source_url": url,
                    "status": "evaluating",
                    "financing": dict(_HM_DEFAULTS, term_months=6, rehab_financed=True),
                    "notes": f"[Radar — {label} · {tag}]\n{note}\n{url}",
                }
                saved = db.upsert_deal(deal)
                deal_id = saved["id"]
                _maybe_spawn_auto_enrich(saved)
                c["added"] += 1

            find = {"address": full or addr, "city": l.get("city", ""),
                    "state": l.get("state", ""), "url": url,
                    "beds": l.get("beds"), "sqft": l.get("sqft"),
                    "days_on_market": dom_val, "watch_id": watch.get("id"),
                    "watch_label": label, "deal_id": deal_id,
                    "zpid": zpid, "image": (zdata or {}).get("image"),
                    "verified": True,
                    "interesting": bool(interesting and info)}
            if info:
                find.update(price=info["price"], arv=info["arv"], rehab=info["rehab"],
                            profit=info["profit"], margin_pct=info["margin_pct"],
                            roi=info["roi"], risk_grade=info["risk_grade"],
                            reasons=info["reasons"])
            else:
                find.update(price=int(price) if price else None, arv=None, rehab=None,
                            profit=None, margin_pct=None, roi=None, risk_grade=None,
                            reasons=["New listing — open it to analyze ARV & profit"])
            if radar_db.add_find(find):
                c["surfaced"] += 1
                if find["interesting"]:
                    c["interesting"] += 1
        except Exception:
            log.exception("radar: failed to process a listing")
    log.info("radar %s: found=%d fresh=%d verified=%d surfaced=%d interesting=%d "
             "added=%d stale=%d sold=%d unknown_age=%d unverified=%d",
             label, c["found"], c["fresh"], c["verified"], c["surfaced"],
             c["interesting"], c["added"], c["stale"], c["sold"],
             c["unknown_age"], c["unverified"])
    return c


def _run_watch(watch_id: str) -> dict:
    """Run one watch: AI area search with the watch's criteria, then diff, then
    let the Radar auto-add the interesting new listings."""
    from . import ai_research
    w = watches_db.get(watch_id)
    if not w:
        return {"ok": False, "error": "watch not found"}
    params = {"search_term": w.get("location", "")}
    for k in ("price_max", "price_min", "beds_min", "property_type"):
        if w.get(k):
            params[k] = w[k]
    res = ai_research.find_listings_in_area(params, max_listings=w.get("max_listings") or 15)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "search failed")}
    out = watches_db.apply_run(watch_id, res.get("listings") or [])
    out["area_label"] = res.get("area_label", "")
    out["listings_found"] = len(res.get("listings") or [])
    try:
        rc = _radar_process(w, out.get("new_listings") or [])
        out["radar"] = rc
        out["radar_added"] = rc.get("added", 0)
    except Exception:
        log.exception("radar processing failed for watch %s", watch_id)
        out["radar"] = {}
        out["radar_added"] = 0
    out.pop("new_listings", None)
    return out


@app.get("/api/watches")
def watches_list():
    return [watches_db.summary(w) for w in watches_db.list_watches()]


@app.post("/api/watches")
def watches_create(payload: dict = Body(...)):
    if not (payload.get("location") or "").strip():
        raise HTTPException(400, "location required (city, zip or state)")
    w = watches_db.create(payload)
    return watches_db.summary(w)


@app.delete("/api/watches/{watch_id}")
def watches_delete(watch_id: str):
    if not watches_db.delete(watch_id):
        raise HTTPException(404, "Watch not found")
    return {"ok": True}


@app.post("/api/watches/{watch_id}/run")
def watches_run(watch_id: str):
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add an Anthropic API key in Settings → AI.")
    return _run_watch(watch_id)


@app.patch("/api/watches/{watch_id}")
def watches_patch(watch_id: str, payload: dict = Body(...)):
    """Update a watch's settings (interval_min, filters, label)."""
    w = watches_db.get(watch_id)
    if not w:
        raise HTTPException(404, "Watch not found")
    for k in ("interval_min", "label", "price_max", "price_min", "beds_min",
              "property_type", "max_listings"):
        if k in payload:
            w[k] = payload[k]
    watches_db.save(w)
    return watches_db.summary(w)


# ---- Deal Radar (interesting finds auto-surfaced by the watches) ----
@app.get("/api/radar")
def radar_list():
    return {"finds": radar_db.list_finds(200), "unseen": radar_db.unseen_count()}


@app.post("/api/radar/seen")
def radar_seen():
    radar_db.mark_all_seen()
    return {"ok": True, "unseen": 0}


@app.delete("/api/radar/{find_id}")
def radar_delete(find_id: str):
    return {"ok": radar_db.delete(find_id)}


# Real-time import: run every watch right now (instead of waiting for the hourly
# scheduler). Runs in the background; the UI polls scan-status + refreshes.
_radar_scan = {"running": False, "added": 0, "done": 0, "total": 0,
               "found": 0, "fresh": 0, "surfaced": 0, "sold": 0, "old": 0,
               "unverified": 0, "error": ""}


@app.post("/api/radar/scan")
def radar_scan():
    if not _radar_config().get("enabled"):
        return {"ok": False, "error": "Radar is off. Turn it on in Settings → Deal Radar to scan."}
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add an Anthropic API key in Settings → AI.")
    watches = watches_db.list_watches()
    if not watches:
        return {"ok": False, "error": "No watches yet — create one first (Sourcing → Search & watch)."}
    if _radar_scan["running"]:
        return {"ok": True, "started": False, "already_running": True, "total": _radar_scan["total"]}
    import threading

    def _job():
        _radar_scan.update(running=True, added=0, done=0, total=len(watches),
                           found=0, fresh=0, surfaced=0, sold=0, old=0,
                           unverified=0, error="")
        try:
            for w in watches:
                try:
                    res = _run_watch(w["id"])
                    if not res.get("ok"):
                        _radar_scan["error"] = res.get("error", "") or _radar_scan["error"]
                    rc = res.get("radar") or {}
                    _radar_scan["added"] += int(res.get("radar_added") or 0)
                    _radar_scan["found"] += int(res.get("listings_found") or 0)
                    _radar_scan["fresh"] += int(rc.get("fresh") or 0)
                    _radar_scan["surfaced"] += int(rc.get("surfaced") or 0)
                    _radar_scan["sold"] += int(rc.get("sold") or 0)
                    _radar_scan["old"] += int(rc.get("stale") or 0) + int(rc.get("unknown_age") or 0)
                    _radar_scan["unverified"] += int(rc.get("unverified") or 0)
                except Exception as e:
                    log.exception("radar scan: watch %s failed", w.get("id"))
                    _radar_scan["error"] = str(e)
                _radar_scan["done"] += 1
        finally:
            _radar_scan["running"] = False

    threading.Thread(target=_job, daemon=True, name="radar-scan").start()
    return {"ok": True, "started": True, "total": len(watches)}


@app.get("/api/radar/scan-status")
def radar_scan_status():
    return dict(_radar_scan)


# ---- Hourly watch scheduler (server-side, plan standard = always on) ----
_WATCH_TICK_SEC = int(os.environ.get("FLIPBOARD_WATCH_TICK", "300"))  # 5 min


def _watch_scheduler():
    """Background loop: every tick, run watches whose interval has elapsed.
    Sequential (one AI call at a time) to stay gentle on rate limits/cost."""
    import time as _t
    log.info("watch scheduler started (tick=%ss)", _WATCH_TICK_SEC)
    while True:
        try:
            if ai_research.is_configured():
                for wid in watches_db.due_watches():
                    try:
                        res = _run_watch(wid)
                        log.info("watch %s: %s", wid,
                                 {k: res.get(k) for k in ("ok", "new", "price_drops", "gone", "error")})
                    except Exception:
                        log.exception("watch %s failed", wid)
        except Exception:
            log.exception("watch scheduler tick failed")
        _t.sleep(_WATCH_TICK_SEC)


# One-time backfill: apply the current Hard Money defaults (90% LTV, 11% rate,
# 2% origination, 3% other lender fees) to every existing deal. Requested
# 2026-07-09 ("applique les à tous"). Guarded by a marker so it runs once.
_HM_DEFAULTS = {"method": "hard_money", "ltv_pct": 90, "interest_rate_pct": 11.0,
                "origination_pct": 2.0, "lender_fees_pct": 3.0}


@app.on_event("startup")
def _apply_hard_money_defaults():
    marker = DATA_DIR / ".hm-defaults-applied"
    if marker.exists():
        return
    updated = 0
    try:
        for d in db.list_deals():
            fin = d.get("financing") or {}
            new_fin = {**_HM_DEFAULTS,
                       "term_months": int(fin.get("term_months") or 6),
                       "rehab_financed": bool(fin.get("rehab_financed", True))}
            if fin != new_fin:
                d["financing"] = new_fin
                db.upsert_deal(d)
                updated += 1
        marker.write_text(f"applied to {updated} deal(s)\n")
        log.info("Hard-money defaults applied to %d deal(s)", updated)
    except Exception:
        log.exception("Hard-money default backfill failed")


@app.on_event("startup")
def _upgrade_ai_model():
    """One-time: activate Opus 4.8 by bumping a saved legacy/default model.
    Guarded by a marker so a later deliberate choice in Settings is respected."""
    marker = DATA_DIR / ".model-4-8-applied"
    if marker.exists():
        return
    try:
        cfg = ai_research.read_config()
        if cfg.get("model") in (None, "", "claude-opus-4-7"):
            cfg["model"] = "claude-opus-4-8"
            ai_research.write_config(cfg)
        marker.write_text("done\n")
        log.info("AI model activated: claude-opus-4-8")
    except Exception:
        log.exception("AI model upgrade failed")


@app.on_event("startup")
def _pause_radar():
    """One-time kill switch (owner request 2026-07-10): turn the Radar OFF and
    pause every watch so it stops auto-adding deals and running hourly AI scans.
    Guarded by a marker so the owner can re-enable it in Settings afterward."""
    marker = DATA_DIR / ".radar-paused"
    if marker.exists():
        return
    try:
        cfg = ai_research.read_config()
        cfg["radar_enabled"] = False
        cfg["radar_auto_add"] = False
        ai_research.write_config(cfg)
        paused = 0
        for w in watches_db.list_watches():
            if int(w.get("interval_min") or 0) != 0:
                w["interval_min"] = 0   # manual-only → the scheduler skips it
                watches_db.save(w)
                paused += 1
        marker.write_text("done\n")
        log.info("Radar paused (disabled + %d watch(es) set to manual-only)", paused)
    except Exception:
        log.exception("Radar pause failed")


@app.on_event("startup")
def _resume_radar():
    """One-time (owner request 2026-07-12): turn the Radar back ON now that
    every find is live-verified on its Zillow page (FOR_SALE + ≤ 1 day on
    market) before reaching the user. Restores hourly scans on paused watches.
    Guarded by a marker so a later manual pause in Settings is respected.
    Registered after _pause_radar on purpose — on a fresh data dir the pause
    runs first and this immediately re-enables."""
    marker = DATA_DIR / ".radar-resumed"
    if marker.exists():
        return
    try:
        cfg = ai_research.read_config()
        cfg["radar_enabled"] = True
        cfg["radar_auto_add"] = True
        ai_research.write_config(cfg)
        resumed = 0
        for w in watches_db.list_watches():
            if int(w.get("interval_min") or 0) == 0:
                w["interval_min"] = 60   # back to hourly scans
                watches_db.save(w)
                resumed += 1
        marker.write_text("done\n")
        log.info("Radar resumed (enabled + %d watch(es) back to hourly)", resumed)
    except Exception:
        log.exception("Radar resume failed")


@app.on_event("startup")
def _purge_prefix_finds():
    """One-time (owner request 2026-07-12): clear the Radar feed of finds
    recorded BEFORE live Zillow verification existed — mostly sold/stale junk
    the old pipeline let through. Deals already on the board are untouched."""
    marker = DATA_DIR / ".radar-finds-purged"
    if marker.exists():
        return
    try:
        removed = radar_db.purge_unverified()
        marker.write_text(f"removed {removed} find(s)\n")
        log.info("Radar feed purged: %d pre-verification find(s) removed", removed)
    except Exception:
        log.exception("Radar finds purge failed")


@app.on_event("startup")
def _start_watch_scheduler():
    import threading
    if os.environ.get("FLIPBOARD_NO_SCHEDULER") == "1":
        return
    t = threading.Thread(target=_watch_scheduler, daemon=True, name="watch-scheduler")
    t.start()


@app.post("/api/watches/run-stale")
def watches_run_stale(payload: dict = Body(default={})):
    """Kick stale watches (last_run older than max_age_h) in a background
    thread — called on app open so the watch refreshes itself daily without a
    server-side cron (Render free tier sleeps). Bounded to `limit` watches."""
    import threading
    from datetime import datetime as dt, timedelta, timezone as tz
    if not ai_research.is_configured():
        return {"ok": True, "started": []}
    try:
        max_age_h = float(payload.get("max_age_h") or 20)
        limit = max(1, min(int(payload.get("limit") or 3), 5))
    except (TypeError, ValueError):
        max_age_h, limit = 20.0, 3
    cutoff = dt.now(tz.utc) - timedelta(hours=max_age_h)
    stale = []
    for w in watches_db.list_watches():
        lr = w.get("last_run")
        try:
            fresh = lr and dt.fromisoformat(lr) > cutoff
        except ValueError:
            fresh = False
        if not fresh:
            stale.append(w["id"])
    started = stale[:limit]
    for wid in started:
        threading.Thread(target=_run_watch, args=(wid,), daemon=True).start()
    return {"ok": True, "started": started, "stale_total": len(stale)}


@app.post("/api/batch/start")
def batch_start(payload: dict = Body(...)):
    """Start a batch scraping job.

    Body:
      {
        "inputs": ["url or address", ...],
        "options": {"delay_sec": 2.5, "skip_duplicates": true}
      }
    """
    inputs = payload.get("inputs") or []
    if isinstance(inputs, str):
        inputs = [l for l in inputs.splitlines() if l.strip()]
    if not inputs:
        raise HTTPException(400, "inputs required (list or newline-separated string)")
    job = batch_scraper.create_job(
        inputs=inputs,
        options=payload.get("options") or {},
        deps={"db": db, "scraper": scraper, "analyzer": analyzer},
    )
    return job.snapshot()


@app.get("/api/batch/jobs")
def batch_jobs():
    return batch_scraper.list_jobs()


@app.get("/api/batch/{job_id}")
def batch_get(job_id: str):
    j = batch_scraper.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return j.snapshot()


@app.post("/api/batch/{job_id}/cancel")
def batch_cancel(job_id: str):
    if not batch_scraper.cancel_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"ok": True}


@app.post("/api/batch/{job_id}/pause")
def batch_pause(job_id: str):
    if not batch_scraper.pause_job(job_id):
        raise HTTPException(400, "Job not pausable (not running)")
    return {"ok": True}


@app.post("/api/batch/{job_id}/resume")
def batch_resume(job_id: str):
    j = batch_scraper.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    if not batch_scraper.resume_job(job_id):
        raise HTTPException(400, "Job not resumable")
    batch_scraper._restart_worker_if_needed(j)
    return {"ok": True}


@app.post("/api/batch/{job_id}/restart")
def batch_restart(job_id: str):
    j = batch_scraper.restart_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    return j.snapshot()


@app.post("/api/batch/{job_id}/retry-failed")
def batch_retry_failed(job_id: str):
    j = batch_scraper.retry_failed_items(job_id)
    if not j:
        raise HTTPException(400, "No failed/skipped items to retry")
    return j.snapshot()


@app.delete("/api/batch/{job_id}")
def batch_delete(job_id: str):
    if not batch_scraper.delete_job(job_id):
        raise HTTPException(404, "Job not found")
    return {"ok": True}


# ---- Auctions / Skip-Trace Queue ----
@app.get("/api/auctions")
def auctions_list(status: Optional[str] = None):
    return auctions_db.list_items(status=status)


@app.get("/api/auctions/stages")
def auctions_stages():
    return {"stages": PIPELINE_STAGES,
             "aggregates": auctions_db.aggregates()}


# NOTE: The /{item_id} GET/PATCH/DELETE routes are registered FURTHER DOWN,
# after all the static-path routes (credentials, skip-trace, import, etc).
# FastAPI matches routes in registration order — if /{item_id} is registered
# first, it catches everything ("credentials", "skip-trace-bulk") as IDs.


@app.post("/api/auctions")
def auctions_create(item: dict = Body(...)):
    return auctions_db.upsert_item(item)


@app.post("/api/auctions/bulk-delete")
def auctions_bulk_delete(payload: dict = Body(default={})):
    n = auctions_db.bulk_delete(status=payload.get("status"))
    return {"deleted": n}


@app.post("/api/auctions/import-single")
def auctions_import_single(payload: dict = Body(...)):
    """Scrape ONE auction detail page (URL must contain AID)."""
    from .auctions import scrape_single_auction
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    try:
        result = scrape_single_auction(url)
    except Exception as e:
        log.exception("Single auction scrape failed")
        raise HTTPException(500, f"Scrape failed: {e}")
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "scrape failed"),
                "raw_text_excerpt": result.get("raw_text_excerpt")}
    insert_result = auctions_db.bulk_insert([result["item"]], source_url=url)
    return {
        "ok": True,
        "added": insert_result["added"],
        "skipped": insert_result["skipped"],
        "item": result["item"],
    }


@app.post("/api/auctions/import")
def auctions_import(payload: dict = Body(...)):
    """Import a foreclosure-auction URL: scrape, normalize, save to queue."""
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    try:
        result = scrape_auction_list(url)
    except Exception as e:
        log.exception("Auction scrape failed")
        raise HTTPException(500, f"Scrape failed: {e}")
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "scrape failed"),
                "items_found": 0,
                "raw_text_excerpt": result.get("raw_text_excerpt")}
    insert_result = auctions_db.bulk_insert(result["items"], source_url=url)
    return {
        "ok": True,
        "site": result.get("site"),
        "items_found": result.get("count"),
        "added": insert_result["added"],
        "skipped": insert_result["skipped"],
        "total_now": insert_result["total_now"],
    }


# ---- Auction site credentials (per-domain login for RealAuction sites) ----
@app.get("/api/auctions/credentials")
def auctions_credentials_list():
    """Return [{domain, username, has_password}] (passwords never returned)."""
    return _auctions_mod.list_credential_domains()


@app.post("/api/auctions/credentials")
def auctions_credentials_save(payload: dict = Body(...)):
    """Save login for a county auction site.
    Body: {"domain": "miamidade.realforeclose.com", "username": "...", "password": "..."}
    """
    domain = (payload.get("domain") or "").strip().lower()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not domain or not username or not password:
        raise HTTPException(400, "domain, username, password are required")
    # Strip protocol if user pasted a full URL
    if domain.startswith("http"):
        from urllib.parse import urlparse
        domain = (urlparse(domain).hostname or domain).lower()
    _auctions_mod.save_credentials(domain, username, password)
    return {"ok": True, "domain": domain}


@app.delete("/api/auctions/credentials/{domain}")
def auctions_credentials_delete(domain: str):
    if _auctions_mod.delete_credentials(domain):
        return {"ok": True}
    raise HTTPException(404, "Credentials not found")


# ---- Auction Skip-Trace (AI + web_search) ----
from . import skip_trace as _skip_trace
from . import ai_research
import threading as _threading
import uuid as _uuid
from datetime import datetime as _dt

# Job tracking for bulk skip-traces (so the frontend can poll progress)
_SKIP_JOBS: dict = {}
_SKIP_LOCK = _threading.Lock()


def _persist_trace(item_id: str, trace: dict):
    """Merge skip-trace result onto the auction item."""
    x = auctions_db.get_item(item_id)
    if not x: return None
    # Merge important fields onto the item itself
    if trace.get("owner_name"):    x["owner_name"]    = trace["owner_name"]
    if trace.get("owner_phone"):   x["owner_phone"]   = trace["owner_phone"]
    if trace.get("owner_email"):   x["owner_email"]   = trace["owner_email"]
    if trace.get("mailing_address"): x["mailing_address"] = trace["mailing_address"]
    # Store the full trace blob too (for the modal display)
    x["skip_trace"] = {
        "ran_at": _dt.utcnow().isoformat() + "Z",
        "model":  trace.get("model"),
        "web_searches_used": trace.get("web_searches_used"),
        "phones": trace.get("phones") or [],
        "emails": trace.get("emails") or [],
        "owner_humans": trace.get("owner_humans") or [],
        "associated_addresses": trace.get("associated_addresses") or [],
        "associated_people": trace.get("associated_people") or [],
        "owner_type": trace.get("owner_type"),
        "notes": trace.get("notes"),
        "confidence_overall": trace.get("confidence_overall"),
        "warnings": trace.get("warnings") or [],
    }
    # Promote stage: queued/tracing → traced if we found a real contact
    if (trace.get("owner_phone") or trace.get("owner_email")) and x.get("status") in (None, "queued", "tracing"):
        x["status"] = "traced"
    elif x.get("status") == "queued":
        x["status"] = "tracing"
    return auctions_db.upsert_item(x)


@app.post("/api/auctions/{item_id}/skip-trace")
def auctions_skip_trace(item_id: str):
    """Run AI skip-trace on a single auction item."""
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add Anthropic API key in Settings → AI.")
    x = auctions_db.get_item(item_id)
    if not x: raise HTTPException(404, "Not found")
    # Mark in-progress so the UI can show a spinner
    if x.get("status") in (None, "queued"):
        x["status"] = "tracing"
        auctions_db.upsert_item(x)
    out = _skip_trace.skip_trace_item(x)
    if not out.get("ok"):
        et = out.get("error_type")
        if et == "billing":  raise HTTPException(402, out.get("error", ""))
        if et == "auth":     raise HTTPException(401, out.get("error", ""))
        if et == "rate_limit": raise HTTPException(429, out.get("error", ""))
        raise HTTPException(500, out.get("error", "Skip-trace failed"))
    updated = _persist_trace(item_id, out)
    return {"ok": True, "item": updated, "trace": out}


@app.post("/api/auctions/skip-trace-bulk")
def auctions_skip_trace_bulk(payload: dict = Body(default={})):
    """Kick off a background bulk-skip-trace for all items matching a status.

    Body: {"status": "queued"}  (default: "queued")
    Returns a job_id you can poll via /api/auctions/skip-trace-bulk/{job_id}.
    """
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured.")
    status = (payload or {}).get("status") or "queued"
    force = bool((payload or {}).get("force"))
    items = auctions_db.list_items(status=status)
    if not items:
        return {"ok": True, "job_id": None, "message": "No items to trace", "total": 0}

    # PRE-FILTER (unless force): only skip-trace ACTIONABLE auctions.
    # Tracing canceled/sold properties (you can't buy them) or items with no
    # extracted address just burns tokens for ~0 result. Default cut is huge:
    # a typical 42-item list is ~27 canceled + 12 sold + 3 active → trace 3.
    skipped_unactionable = 0
    if not force:
        actionable = []
        for it in items:
            short = (it.get("status_short") or "").lower()
            has_addr = bool((it.get("address") or "").strip())
            # Active (or unknown status, to be safe) AND has an address.
            if has_addr and short not in ("canceled", "cancelled", "sold",
                                           "withdrawn", "postponed"):
                actionable.append(it)
            else:
                skipped_unactionable += 1
        items = actionable
        if not items:
            return {"ok": True, "job_id": None,
                    "message": f"No actionable items (filtered out {skipped_unactionable} "
                               f"canceled/sold/no-address). Pass force=true to trace anyway.",
                    "total": 0, "skipped_unactionable": skipped_unactionable}

    job_id = str(_uuid.uuid4())[:8]
    job = {
        "id": job_id, "status": "running", "total": len(items),
        "done": 0, "found": 0, "failed": 0,
        "current": None, "started_at": _dt.utcnow().isoformat() + "Z",
        "errors": [],
    }
    with _SKIP_LOCK:
        _SKIP_JOBS[job_id] = job

    def _worker():
        for it in items:
            with _SKIP_LOCK:
                j = _SKIP_JOBS.get(job_id)
                if not j or j.get("status") == "cancelled": return
                j["current"] = it.get("address") or it.get("case_number") or it["id"]
            try:
                out = _skip_trace.skip_trace_item(it)
                if out.get("ok"):
                    _persist_trace(it["id"], out)
                    if out.get("owner_phone") or out.get("owner_email"):
                        with _SKIP_LOCK: _SKIP_JOBS[job_id]["found"] += 1
                else:
                    with _SKIP_LOCK:
                        _SKIP_JOBS[job_id]["failed"] += 1
                        _SKIP_JOBS[job_id]["errors"].append(
                            {"item_id": it["id"], "error": out.get("error", "?")[:200]}
                        )
            except Exception as e:
                with _SKIP_LOCK:
                    _SKIP_JOBS[job_id]["failed"] += 1
                    _SKIP_JOBS[job_id]["errors"].append(
                        {"item_id": it["id"], "error": str(e)[:200]}
                    )
            finally:
                with _SKIP_LOCK:
                    _SKIP_JOBS[job_id]["done"] += 1
        with _SKIP_LOCK:
            _SKIP_JOBS[job_id]["status"] = "done"
            _SKIP_JOBS[job_id]["finished_at"] = _dt.utcnow().isoformat() + "Z"
            _SKIP_JOBS[job_id]["current"] = None

    _threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "job_id": job_id, "total": len(items),
            "skipped_unactionable": skipped_unactionable}


@app.get("/api/auctions/skip-trace-bulk/{job_id}")
def auctions_skip_trace_status(job_id: str):
    with _SKIP_LOCK:
        j = _SKIP_JOBS.get(job_id)
    if not j: raise HTTPException(404, "Job not found")
    return j


@app.post("/api/auctions/skip-trace-bulk/{job_id}/cancel")
def auctions_skip_trace_cancel(job_id: str):
    with _SKIP_LOCK:
        j = _SKIP_JOBS.get(job_id)
        if not j: raise HTTPException(404, "Job not found")
        j["status"] = "cancelled"
    return {"ok": True}


@app.post("/api/auctions/{item_id}/to-lead")
def auctions_to_lead(item_id: str):
    """Promote an auction item to a lead (after skip-tracing)."""
    x = auctions_db.get_item(item_id)
    if not x: raise HTTPException(404, "Not found")
    lead = {
        "source": "foreclosure_auction",
        "source_url": x.get("source_url"),
        "lead_price": 0,
        "address": x.get("address", ""),
        "asking_price": x.get("opening_bid"),
        "motivation": f"Foreclosure auction ({x.get('auction_type', 'foreclosure')})",
        "description": (f"Case: {x.get('case_number', '?')}\n"
                         f"Parcel: {x.get('parcel_id', '?')}\n"
                         f"Auction: {x.get('auction_date', '?')} {x.get('auction_time', '')}\n"
                         f"Opening bid: ${x.get('opening_bid', 0):,}\n\n"
                         f"Owner: {x.get('owner_name', 'unknown')}\n"
                         f"Phone: {x.get('owner_phone', '')}\n"
                         f"Email: {x.get('owner_email', '')}\n"),
        "status": "new",
        "external_id": x.get("case_number"),
    }
    saved = leads_db.upsert_lead(lead)
    # Mark auction item as contacted/promoted
    x["status"] = "contacted"
    x["promoted_lead_id"] = saved["id"]
    auctions_db.upsert_item(x)
    return {"ok": True, "lead_id": saved["id"]}


# ---- Auction item CRUD (registered LAST so static routes above win) ----
@app.get("/api/auctions/{item_id}")
def auctions_get(item_id: str):
    x = auctions_db.get_item(item_id)
    if not x: raise HTTPException(404, "Not found")
    return x


@app.patch("/api/auctions/{item_id}")
def auctions_patch(item_id: str, updates: dict = Body(...)):
    x = auctions_db.get_item(item_id)
    if not x: raise HTTPException(404, "Not found")
    x.update(updates)
    x["id"] = item_id
    return auctions_db.upsert_item(x)


@app.delete("/api/auctions/{item_id}")
def auctions_delete(item_id: str):
    if auctions_db.delete_item(item_id):
        return {"ok": True}
    raise HTTPException(404, "Not found")


# ---- PDF Import (upload → Claude extracts properties → bulk create) ----
from . import pdf_importer


@app.post("/api/import-pdf/analyze")
async def import_pdf_analyze(file: UploadFile = File(...)):
    """Upload a PDF — extract text, send to Claude, return structured property list.

    Doesn't create anything yet — user reviews the list in the UI, then calls
    /api/import-pdf/commit with the subset they want to keep.
    """
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add an Anthropic API key in Settings.")
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "File must be a .pdf")
    try:
        pdf_bytes = await file.read()
    except Exception as e:
        raise HTTPException(400, f"Could not read upload: {e}")
    if not pdf_bytes:
        raise HTTPException(400, "Empty file")
    if len(pdf_bytes) > 25 * 1024 * 1024:
        raise HTTPException(413, "PDF too large (>25 MB)")

    try:
        result = pdf_importer.analyze_pdf(pdf_bytes, filename=file.filename)
    except Exception as e:
        log.exception("PDF analyze failed")
        raise HTTPException(500, f"PDF analyze failed: {e}")

    if not result.get("ok"):
        et = result.get("error_type")
        if et == "billing": raise HTTPException(402, result.get("error", ""))
        if et == "auth":    raise HTTPException(401, result.get("error", ""))
        # Return partial info (e.g. raw text excerpt) so user can debug
        return result
    return result


@app.get("/api/import-pdf/recent")
def import_pdf_recent():
    """List recent PDFs from common folders (~/Downloads, ~/Desktop, ~/Documents)
    so the user can pick one with a single click — no file picker, no paths.
    """
    home = Path.home()
    folders = [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    ]
    pdfs = []
    for folder in folders:
        if not folder.exists() or not folder.is_dir():
            continue
        try:
            for p in folder.glob("*.pdf"):
                try:
                    stat = p.stat()
                    pdfs.append({
                        "path": str(p),
                        "name": p.name,
                        "folder": folder.name,
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
                except Exception:
                    pass
            # Also one level deep
            for p in folder.glob("*/*.pdf"):
                try:
                    stat = p.stat()
                    pdfs.append({
                        "path": str(p),
                        "name": p.name,
                        "folder": f"{folder.name}/{p.parent.name}",
                        "size": stat.st_size,
                        "mtime": stat.st_mtime,
                    })
                except Exception:
                    pass
        except Exception:
            pass
    # Sort by mtime descending, cap at 50
    pdfs.sort(key=lambda x: x["mtime"], reverse=True)
    pdfs = pdfs[:50]
    return {"pdfs": pdfs, "scanned_folders": [str(f) for f in folders if f.exists()]}


@app.post("/api/import-pdf/native-pick")
def import_pdf_native_pick():
    """Open a native macOS file picker via pywebview (server-side) and return
    the chosen path. Bypasses WKWebView's flaky <input type=file> change event.
    """
    try:
        import webview
        if not webview.windows:
            raise HTTPException(503,
                "Native picker only available inside the desktop app (pywebview).")
        win = webview.windows[0]
        paths = win.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("PDF Files (*.pdf)",),
        )
        if not paths:
            return {"ok": True, "path": None, "cancelled": True}
        p = paths[0] if isinstance(paths, (list, tuple)) else paths
        log.info("Native picker selected: %s", p)
        return {"ok": True, "path": p}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Native pick failed")
        raise HTTPException(500, f"Native pick failed: {e}")


@app.post("/api/import-pdf/analyze-from-path")
def import_pdf_analyze_from_path(payload: dict = Body(...)):
    """Read a PDF from a LOCAL FILE PATH and run the same analysis pipeline.

    Used by the pywebview native file dialog flow (more reliable than HTML
    `<input type="file">` in WKWebView).
    """
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add Anthropic API key in Settings.")
    path = (payload.get("path") or "").strip()
    if not path:
        raise HTTPException(400, "path is required")
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"File not found: {path}")
    if p.suffix.lower() != ".pdf":
        raise HTTPException(400, "File must be a .pdf")
    if p.stat().st_size > 25 * 1024 * 1024:
        raise HTTPException(413, "PDF too large (>25 MB)")
    try:
        pdf_bytes = p.read_bytes()
    except Exception as e:
        raise HTTPException(500, f"Could not read file: {e}")
    try:
        result = pdf_importer.analyze_pdf(pdf_bytes, filename=p.name)
    except Exception as e:
        log.exception("PDF analyze (from path) failed")
        raise HTTPException(500, f"PDF analyze failed: {e}")
    return result


@app.post("/api/import-pdf/commit")
def import_pdf_commit(payload: dict = Body(...)):
    """Bulk-create deals / leads / auctions from extracted properties.

    Body:
      {
        "properties": [ {address, purchase_price, beds, ...}, ... ],
        "target": "deal" | "lead" | "auction",  # default "deal"
        "doc_type": "<from analyze response, used for source labeling>",
        "filename": "<original filename, used for source labeling>"
      }
    """
    items = payload.get("properties") or []
    if not items:
        raise HTTPException(400, "No properties to import")
    target = (payload.get("target") or "deal").lower()
    doc_type = payload.get("doc_type") or "pdf"
    filename = payload.get("filename") or "uploaded.pdf"
    source_label = f"pdf:{filename}"

    created_deals, created_leads, created_auctions, skipped, errors = [], [], [], [], []

    for it in items:
        if not it.get("address"):
            skipped.append({"reason": "no address", "item": it}); continue
        try:
            if target == "lead":
                lead = {
                    "source": "pdf_import",
                    "source_url": source_label,
                    "lead_price": 0,
                    "address": it.get("address"),
                    "city": it.get("city"), "state": it.get("state"), "zip": it.get("zip"),
                    "property_type": it.get("property_type"),
                    "beds": it.get("beds"), "baths": it.get("baths"),
                    "sqft": it.get("sqft"), "year_built": it.get("year_built"),
                    "asking_price": it.get("purchase_price"),
                    "estimated_arv": it.get("arv_base"),
                    "estimated_rehab": it.get("rehab_base"),
                    "description": it.get("notes", ""),
                    "external_id": it.get("case_number"),
                    "status": "new",
                }
                saved = leads_db.upsert_lead(lead)
                created_leads.append(saved["id"])
            elif target == "auction":
                aitem = {
                    "address": it.get("address"),
                    "case_number": it.get("case_number"),
                    "parcel_id": it.get("parcel_id"),
                    "opening_bid": it.get("purchase_price"),
                    "auction_type": (it.get("property_type", "").lower()
                                       if "tax" in (it.get("price_type", "") or "")
                                       else "mortgage_foreclosure"),
                    "auction_date": it.get("auction_date"),
                    "auction_status": "Pending",
                    "status": "queued",
                }
                ar = auctions_db.bulk_insert([aitem], source_url=source_label)
                if ar.get("added"): created_auctions.append(it.get("address"))
                else: skipped.append({"reason": "duplicate auction", "item": it})
            else:  # deal
                deal = {
                    "address": it.get("address"),
                    "city": it.get("city", ""), "state": it.get("state", ""), "zip": it.get("zip", ""),
                    "property_type": it.get("property_type", "Single Family Residence"),
                    "beds": it.get("beds"), "baths": it.get("baths"),
                    "sqft": it.get("sqft"), "year_built": it.get("year_built"),
                    "lot_size": it.get("lot_size"),
                    "purchase_price": it.get("purchase_price") or 0,
                    "arv_base": it.get("arv_base") or 0,
                    "rehab_base": it.get("rehab_base") or 0,
                    "source_url": source_label,
                    "notes": f"[Imported from {filename}] {it.get('notes', '')}",
                    "status": "evaluating",
                }
                saved = db.upsert_deal(deal)
                created_deals.append(saved["id"])
        except Exception as e:
            log.exception("PDF import row failed")
            errors.append({"item": it.get("address"), "error": str(e)})

    return {
        "ok": True,
        "target": target,
        "total_input": len(items),
        "created_deals": created_deals,
        "created_leads": created_leads,
        "created_auctions": created_auctions,
        "skipped": skipped,
        "errors": errors,
        "summary": {
            "deals": len(created_deals),
            "leads": len(created_leads),
            "auctions": len(created_auctions),
            "skipped": len(skipped),
            "errors": len(errors),
        },
    }


@app.post("/api/find-by-address")
def find_by_address_endpoint(payload: dict = Body(...)):
    """Take an address, find a property listing across Zillow/Redfin/AI, scrape."""
    address = (payload.get("address") or "").strip()
    if not address:
        raise HTTPException(400, "address is required")
    try:
        out = scraper.find_by_address(address)
        return out
    except Exception as e:
        log.exception("find_by_address failed")
        raise HTTPException(500, f"Search failed: {e}")


@app.post("/api/scrape")
def scrape_endpoint(payload: dict = Body(...)):
    url = payload.get("url", "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    return scraper.scrape(url)


# ---- Auth cookie management (per-domain) ----
import json as _json


def _read_cookies() -> dict:
    if not COOKIE_STORE.exists():
        return {}
    try:
        with open(COOKIE_STORE, "r") as f:
            return _json.load(f)
    except Exception:
        return {}


def _write_cookies(data: dict):
    with open(COOKIE_STORE, "w") as f:
        _json.dump(data, f, indent=2)


@app.get("/api/auth-cookies")
def list_cookies():
    """Return list of configured domains (cookie values are masked)."""
    cookies = _read_cookies()
    return [{"domain": d, "preview": v[:30] + "..." if len(v) > 30 else v}
            for d, v in cookies.items()]


@app.post("/api/auth-cookies")
def set_cookie(payload: dict = Body(...)):
    domain = (payload.get("domain") or "").strip().lower()
    cookie = (payload.get("cookie") or "").strip()
    if not domain or not cookie:
        raise HTTPException(400, "domain and cookie are required")
    data = _read_cookies()
    data[domain] = cookie
    _write_cookies(data)
    return {"ok": True, "domain": domain}


@app.delete("/api/auth-cookies/{domain}")
def delete_cookie(domain: str):
    data = _read_cookies()
    if domain in data:
        del data[domain]
        _write_cookies(data)
        return {"ok": True}
    raise HTTPException(404, "domain not found")


@app.post("/api/browser-session/reset")
def reset_browser_session():
    """Wipe the persistent Chromium profile (logs you out of scraped sites)."""
    import shutil
    profile = DATA_DIR / ".browser-profile"
    if profile.exists():
        shutil.rmtree(profile)
        return {"ok": True, "message": "Browser session cleared"}
    return {"ok": True, "message": "No session to clear"}


# ---- AI configuration + ARV research ----
@app.get("/api/ai/config")
def ai_config_get():
    """Return AI config (API key masked). Honours both the file config and
    the ANTHROPIC_API_KEY env var (used in production / Render)."""
    cfg = ai_research.read_config()
    # Prefer env var if present (production), fall back to file (local dev)
    key = os.environ.get("ANTHROPIC_API_KEY") or cfg.get("anthropic_api_key", "")
    source = "env" if os.environ.get("ANTHROPIC_API_KEY") else "file"
    maps_key = ai_research.get_maps_key()
    proxy_key = ai_research.get_scraper_proxy_key()
    # Never leak key material: only report whether a key is set, masked.
    MASK = "••••••••••••"
    return {
        "configured": bool(key),
        "key_preview": MASK if key else "",
        "model": cfg.get("model", "claude-opus-4-8"),
        "auto_research": cfg.get("auto_research", True),
        "target_margin_pct": cfg.get("target_margin_pct", 15),
        "radar_enabled": cfg.get("radar_enabled", True),
        "radar_auto_add": cfg.get("radar_auto_add", True),
        "radar_min_profit": cfg.get("radar_min_profit", 25000),
        "source": source,
        "maps_configured": bool(maps_key),
        "maps_key_preview": MASK if maps_key else "",
        "proxy_configured": bool(proxy_key),
        "proxy_key_preview": MASK if proxy_key else "",
    }


@app.post("/api/ai/config")
def ai_config_set(payload: dict = Body(...)):
    cfg = ai_research.read_config()
    if "anthropic_api_key" in payload:
        cfg["anthropic_api_key"] = (payload["anthropic_api_key"] or "").strip()
    if "model" in payload:
        cfg["model"] = (payload["model"] or "claude-opus-4-8").strip()
    if "google_maps_key" in payload:
        cfg["google_maps_key"] = (payload["google_maps_key"] or "").strip()
    if "scraper_api_key" in payload:
        cfg["scraper_api_key"] = (payload["scraper_api_key"] or "").strip()
    if "auto_research" in payload:
        cfg["auto_research"] = bool(payload["auto_research"])
    if "target_margin_pct" in payload:
        try:
            cfg["target_margin_pct"] = max(5, min(40, float(payload["target_margin_pct"])))
        except (TypeError, ValueError):
            pass
    if "radar_enabled" in payload:
        cfg["radar_enabled"] = bool(payload["radar_enabled"])
    if "radar_auto_add" in payload:
        cfg["radar_auto_add"] = bool(payload["radar_auto_add"])
    if "radar_min_profit" in payload:
        try:
            cfg["radar_min_profit"] = max(0, float(payload["radar_min_profit"]))
        except (TypeError, ValueError):
            pass
    ai_research.write_config(cfg)
    return {"ok": True}


@app.post("/api/research-arv")
def research_arv(payload: dict = Body(...)):
    """Research ARV via Claude + web_search.

    Body can be either:
      {"deal_id": "<id>"}        — research a saved deal
      {"deal": {...}}            — research an unsaved deal seed
    Returns ARV low/base/high + comparables + reasoning.
    """
    if not ai_research.is_configured():
        raise HTTPException(400,
            "AI not configured. Add an Anthropic API key in Settings → AI.")
    if payload.get("deal_id"):
        deal = db.get_deal(payload["deal_id"])
        if not deal:
            raise HTTPException(404, "Deal not found")
    elif payload.get("deal"):
        deal = payload["deal"]
    else:
        raise HTTPException(400, "Pass either deal_id or deal in body")
    result = ai_research.research_arv(deal)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Research failed"))
    return result


# ---- AI Tasks (unified) ----
from . import ai_tasks
from datetime import datetime as _dt


@app.get("/api/ai/tasks")
def ai_tasks_list():
    """Return the registry of available AI tasks."""
    return ai_tasks.task_registry()


@app.post("/api/ai/run")
def ai_task_run(payload: dict = Body(...)):
    """Run a single AI task and persist the result on the deal.

    Body: {task: <name>, deal_id: <id>} OR {task: <name>, deal: {...}}
    Returns the task result + persists to deal.ai_insights[task_name].
    """
    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add an Anthropic API key in Settings → AI.")
    task_name = payload.get("task")
    if not task_name:
        raise HTTPException(400, "task is required")

    if payload.get("deal_id"):
        deal = db.get_deal(payload["deal_id"])
        if not deal:
            raise HTTPException(404, "Deal not found")
    elif payload.get("deal"):
        deal = payload["deal"]
    else:
        raise HTTPException(400, "Pass deal_id or deal")

    out = ai_tasks.run_task(task_name, deal)
    if not out.get("ok"):
        # Return structured error so frontend can show rich UI
        et = out.get("error_type")
        if et == "billing":
            raise HTTPException(402, out.get("error", "Out of credits"))
        if et == "auth":
            raise HTTPException(401, out.get("error", "Bad API key"))
        if et == "rate_limit":
            raise HTTPException(429, out.get("error", "Rate limited"))
        if et == "overloaded":
            raise HTTPException(503, out.get("error", "Overloaded"))
        raise HTTPException(500, out.get("error", "Task failed"))

    # Persist insight if running against a saved deal
    if payload.get("deal_id"):
        d = db.get_deal(payload["deal_id"])
        if d:
            insights = d.get("ai_insights") or {}
            insights[task_name] = {
                "result": out.get("result"),
                "model": out.get("model"),
                "usage": out.get("usage"),
                "web_searches_used": out.get("web_searches_used"),
                "ran_at": _dt.utcnow().isoformat() + "Z",
            }
            d["ai_insights"] = insights
            db.upsert_deal(d)
    return out


@app.post("/api/ai/run-all")
def ai_run_all(payload: dict = Body(...)):
    """Run the full research tier IN PARALLEL, then the verdict.

    Body: {deal_id: <id>}  (a saved deal is required to persist insights)

    This is the real "multi-agent" path: the 7 independent research tasks
    (arv, rehab, rent_comps, neighborhood, taxes_insurance, history, risks)
    plus photos + red_flags run concurrently instead of one-at-a-time, then
    the verdict runs once with all insights available. ~8 s instead of 30 s+.
    """
    import concurrent.futures as _cf

    if not ai_research.is_configured():
        raise HTTPException(400, "AI not configured. Add an Anthropic API key in Settings → AI.")
    deal_id = payload.get("deal_id")
    if not deal_id:
        raise HTTPException(400, "deal_id is required for run-all")
    deal = db.get_deal(deal_id)
    if not deal:
        raise HTTPException(404, "Deal not found")

    # Independent tasks that can run concurrently (verdict excluded — it depends).
    # Default = the buy-decision essentials. rent_comps / neighborhood /
    # taxes_insurance are opt-in (run them from their tab, or pass
    # {include: [...]}) — they rarely change a flip decision and each costs a
    # 30-60s web-search call. Cuts run-all cost/latency roughly in half.
    INDEPENDENT = ["arv", "rehab", "history", "risks", "photos", "red_flags"]
    extra = payload.get("include") or []
    for t in extra:
        if t in ("rent_comps", "neighborhood", "taxes_insurance") and t not in INDEPENDENT:
            INDEPENDENT.append(t)

    results = {}
    full_outputs = {}
    web_searches = 0

    def _run_one(name):
        return name, ai_tasks.run_task(name, deal)

    # Cap concurrency to stay within Anthropic rate limits.
    with _cf.ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(_run_one, n) for n in INDEPENDENT]
        for fut in _cf.as_completed(futures):
            try:
                name, out = fut.result()
            except Exception:
                continue
            full_outputs[name] = out
            results[name] = {"ok": out.get("ok"), "error": out.get("error")}
            if out.get("ok"):
                web_searches += out.get("web_searches_used") or 0

    # Persist every successful insight ONCE, single-threaded (no JSON race).
    d = db.get_deal(deal_id)
    insights = d.get("ai_insights") or {}
    for name, out in full_outputs.items():
        if out.get("ok"):
            insights[name] = {
                "result": out.get("result"),
                "model": out.get("model"),
                "usage": out.get("usage"),
                "web_searches_used": out.get("web_searches_used"),
                "ran_at": _dt.utcnow().isoformat() + "Z",
            }
    d["ai_insights"] = insights
    db.upsert_deal(d)

    # Verdict LAST, with the enriched deal context (it reads ai_insights).
    verdict_out = ai_tasks.run_task("verdict", db.get_deal(deal_id))
    if verdict_out.get("ok"):
        web_searches += verdict_out.get("web_searches_used") or 0
        d = db.get_deal(deal_id)
        insights = d.get("ai_insights") or {}
        insights["verdict"] = {
            "result": verdict_out.get("result"),
            "model": verdict_out.get("model"),
            "usage": verdict_out.get("usage"),
            "web_searches_used": verdict_out.get("web_searches_used"),
            "ran_at": _dt.utcnow().isoformat() + "Z",
        }
        d["ai_insights"] = insights
        db.upsert_deal(d)

    return {
        "ok": True,
        "ran": INDEPENDENT + ["verdict"],
        "results": results,
        "verdict": verdict_out,
        "total_web_searches": web_searches,
    }


# ---- LEADS endpoints ----
@app.get("/api/leads")
def leads_list(status: Optional[str] = None):
    return leads_db.list_leads(status=status)


@app.get("/api/leads/aggregates")
def leads_agg():
    return leads_db.aggregates()


@app.get("/api/leads/{lead_id}")
def leads_get(lead_id: str):
    l = leads_db.get_lead(lead_id)
    if not l: raise HTTPException(404, "Lead not found")
    return l


@app.post("/api/leads")
def leads_create(lead: dict = Body(...)):
    if not lead.get("address") and not lead.get("source_url"):
        raise HTTPException(400, "Provide at least address or source_url")
    return leads_db.upsert_lead(lead)


@app.patch("/api/leads/{lead_id}")
def leads_patch(lead_id: str, updates: dict = Body(...)):
    l = leads_db.get_lead(lead_id)
    if not l: raise HTTPException(404, "Lead not found")
    l.update(updates)
    l["id"] = lead_id
    return leads_db.upsert_lead(l)


@app.delete("/api/leads/{lead_id}")
def leads_delete(lead_id: str):
    if leads_db.delete_lead(lead_id):
        return {"ok": True}
    raise HTTPException(404, "Lead not found")


# ---- Kanban columns (customizable) ----
@app.get("/api/kanban/columns")
def kanban_columns_get():
    return leads_db.get_columns()


@app.put("/api/kanban/columns")
def kanban_columns_set(payload: dict = Body(...)):
    cols = payload.get("columns")
    if not isinstance(cols, list):
        raise HTTPException(400, "columns (list) required")
    return leads_db.set_columns(cols)


# ---- Comments on a lead ----
@app.post("/api/leads/{lead_id}/comments")
def lead_add_comment(lead_id: str, payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    l = leads_db.add_comment(lead_id, text[:2000])
    if not l:
        raise HTTPException(404, "Lead not found")
    return l


@app.delete("/api/leads/{lead_id}/comments/{comment_id}")
def lead_delete_comment(lead_id: str, comment_id: str):
    l = leads_db.delete_comment(lead_id, comment_id)
    if not l:
        raise HTTPException(404, "Lead not found")
    return l


@app.post("/api/leads/scrape")
def leads_scrape(payload: dict = Body(...)):
    """Attempt to scrape an ispeedtolead lead URL via Playwright + cookies.

    Accepts:
      - /ld/{id}?shared={token}
      - /my-leads?...&open_order={id}
    """
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url is required")
    if "/ld/" not in url and "open_order=" not in url:
        raise HTTPException(400,
            "URL must contain '/ld/...' (shared link) or '...&open_order=...' (my-leads view)")
    try:
        from . import scraper_browser
        result = scraper_browser.scrape_ispeedtolead_lead(url)
        return result
    except Exception as e:
        log.exception("Lead scrape failed")
        raise HTTPException(500, f"Scrape failed: {e}")


@app.post("/api/leads/{lead_id}/analyze")
def leads_analyze(lead_id: str):
    """Run AI worth-buying analysis on a lead."""
    l = leads_db.get_lead(lead_id)
    if not l: raise HTTPException(404, "Lead not found")
    out = analyze_lead(l)
    if not out.get("ok"):
        et = out.get("error_type")
        if et == "billing": raise HTTPException(402, out.get("error", ""))
        if et == "auth": raise HTTPException(401, out.get("error", ""))
        raise HTTPException(500, out.get("error", "Analysis failed"))
    # Persist analysis on the lead
    l["ai_analysis"] = {
        "result": out["result"], "model": out["model"],
        "web_searches_used": out["web_searches_used"],
        "usage": out["usage"], "ran_at": out["ran_at"],
    }
    leads_db.upsert_lead(l)
    return out


@app.post("/api/leads/{lead_id}/promote-to-deal")
def leads_promote(lead_id: str):
    """Convert a lead into a deal (when you decide to pursue)."""
    l = leads_db.get_lead(lead_id)
    if not l: raise HTTPException(404, "Lead not found")
    # Map lead → deal schema
    deal = {
        "address": l.get("address", ""),
        "city": l.get("city", ""), "state": l.get("state", ""), "zip": l.get("zip", ""),
        "property_type": l.get("property_type", "Single Family Residence"),
        "beds": l.get("beds"), "baths": l.get("baths"),
        "sqft": l.get("sqft"), "year_built": l.get("year_built"),
        "purchase_price": l.get("asking_price") or 0,
        "arv_base": l.get("estimated_arv") or 0,
        "rehab_base": l.get("estimated_rehab") or 0,
        "image": l.get("image"), "image_gallery": l.get("image_gallery"),
        "lat": l.get("lat"), "lng": l.get("lng"),
        "source": l.get("source"), "source_url": l.get("source_url"),
        "notes": f"Promoted from lead {lead_id}\n\n" + (l.get("description") or l.get("notes") or ""),
        "status": "evaluating",
    }
    # Recompute score
    m = analyzer.compute_metrics(deal)
    score, grade, signal = analyzer.compute_score(deal, m)
    deal["score"] = score; deal["grade"] = grade; deal["signal"] = signal
    saved_deal = db.upsert_deal(deal)
    # Mark lead as promoted
    l["status"] = "closed"
    l["promoted_deal_id"] = saved_deal["id"]
    leads_db.upsert_lead(l)
    return {"ok": True, "deal_id": saved_deal["id"]}


# ---- CRM endpoints ----
@app.get("/api/crm/contacts")
def crm_contacts(deal_id: Optional[str] = None):
    return crm.list_contacts(deal_id=deal_id)


@app.post("/api/crm/contacts")
def crm_create_contact(contact: dict = Body(...)):
    if not contact.get("name"):
        raise HTTPException(400, "name is required")
    return crm.upsert_contact(contact)


@app.patch("/api/crm/contacts/{contact_id}")
def crm_update_contact(contact_id: str, updates: dict = Body(...)):
    existing = crm.get_contact(contact_id)
    if not existing:
        raise HTTPException(404, "Contact not found")
    existing.update(updates)
    existing["id"] = contact_id
    return crm.upsert_contact(existing)


@app.delete("/api/crm/contacts/{contact_id}")
def crm_delete_contact(contact_id: str):
    if crm.delete_contact(contact_id):
        return {"ok": True}
    raise HTTPException(404, "Contact not found")


@app.get("/api/crm/interactions")
def crm_interactions(deal_id: Optional[str] = None, contact_id: Optional[str] = None):
    return crm.list_interactions(deal_id=deal_id, contact_id=contact_id)


@app.post("/api/crm/interactions")
def crm_create_interaction(interaction: dict = Body(...)):
    return crm.upsert_interaction(interaction)


@app.delete("/api/crm/interactions/{interaction_id}")
def crm_delete_interaction(interaction_id: str):
    if crm.delete_interaction(interaction_id):
        return {"ok": True}
    raise HTTPException(404, "Interaction not found")


@app.get("/api/crm/aggregates")
def crm_aggregates():
    return crm.aggregates()


# ---- AI chat endpoints ----
@app.post("/api/ai/chat")
def ai_chat_endpoint(payload: dict = Body(...)):
    deal_id = payload.get("deal_id")
    message = (payload.get("message") or "").strip()
    if not deal_id or not message:
        raise HTTPException(400, "deal_id and message required")
    deal = db.get_deal(deal_id)
    if not deal:
        raise HTTPException(404, "Deal not found")

    # Persisted history
    chat_history = deal.get("chat_history") or []
    out = ai_chat.chat(deal, message, history=chat_history)
    if not out.get("ok"):
        et = out.get("error_type")
        if et == "billing": raise HTTPException(402, out.get("error", ""))
        if et == "auth":    raise HTTPException(401, out.get("error", ""))
        if et == "rate_limit": raise HTTPException(429, out.get("error", ""))
        raise HTTPException(500, out.get("error", "Chat failed"))

    # Persist exchange
    from datetime import datetime as _dt
    ts = _dt.utcnow().isoformat() + "Z"
    chat_history.append({"role": "user", "content": message, "ts": ts})
    chat_history.append({"role": "assistant", "content": out["reply"], "ts": ts,
                          "model": out["model"], "usage": out.get("usage")})
    deal["chat_history"] = chat_history[-40:]  # keep last 40 messages
    db.upsert_deal(deal)

    return out


@app.get("/api/ai/chat/{deal_id}")
def ai_chat_history(deal_id: str):
    deal = db.get_deal(deal_id)
    if not deal:
        raise HTTPException(404, "Deal not found")
    return deal.get("chat_history") or []


@app.delete("/api/ai/chat/{deal_id}")
def ai_chat_clear(deal_id: str):
    deal = db.get_deal(deal_id)
    if not deal:
        raise HTTPException(404, "Deal not found")
    deal["chat_history"] = []
    db.upsert_deal(deal)
    return {"ok": True}


@app.delete("/api/ai/insight/{deal_id}/{task_name}")
def ai_clear_insight(deal_id: str, task_name: str):
    d = db.get_deal(deal_id)
    if not d:
        raise HTTPException(404, "Deal not found")
    if d.get("ai_insights") and task_name in d["ai_insights"]:
        del d["ai_insights"][task_name]
        db.upsert_deal(d)
        return {"ok": True}
    return {"ok": False, "error": "Not found"}


@app.get("/api/browser-session/status")
def browser_session_status():
    try:
        from . import scraper_browser
        return scraper_browser.session_status()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/browser-session/connect")
def browser_session_connect(payload: dict = Body(default={})):
    """Open a visible Chromium window so the user can log in once.

    Body (optional):
      {
        "login_url": "...",                 # default ispeedtolead login
        "success_url_contains": "/my-leads" # what tells us login succeeded
      }
    """
    try:
        from . import scraper_browser
        return scraper_browser.open_authenticated_session(
            login_url=payload.get("login_url") or
                "https://app.ispeedtolead.com/auth/login",
            success_url_contains=payload.get("success_url_contains") or "/my-leads",
        )
    except Exception as e:
        log.exception("Browser auth failed")
        raise HTTPException(500, f"Authentication failed: {e}")


# Add no-cache headers to all responses so PyWebView's WKWebView always
# serves the latest frontend code (otherwise users have to clear cache
# manually after every code change — frustrating in dev).
from starlette.middleware.base import BaseHTTPMiddleware


class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Only no-cache the frontend files — let API responses alone
        path = request.url.path
        if not path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


app.add_middleware(NoCacheMiddleware)


# Locally archived deal photos (mounted before "/" so it wins).
app.mount("/deal-photos", StaticFiles(directory=str(PHOTOS_DIR)), name="deal-photos")

# Mount static frontend last so /api routes take precedence.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True),
              name="frontend")


def run(host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
