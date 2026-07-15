"""Autonomous batch scraping engine.

Lets the user paste a LIST of URLs or addresses (one per line) and processes
them sequentially in a background thread. Each item:
  1. Auto-detects URL vs address
  2. Scrapes (Zillow / Redfin / ispeedtolead / address search)
  3. Saves the result as a Deal on the board
  4. Reports status (pending / running / succeeded / failed / skipped)

Jobs are kept in-memory. Frontend polls /api/batch/{job_id} for updates.
"""

import logging
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

from . import zillow_api

log = logging.getLogger("flip-board.batch")

_JOBS = {}            # job_id -> BatchJob
_LOCK = threading.Lock()


def _now():
    return datetime.utcnow().isoformat() + "Z"


def _is_zillow_search(s: str) -> bool:
    """A Zillow *search results* URL (map/area search), not a single listing."""
    u = s.lower()
    if "zillow.com" not in u or "/homedetails/" in u:
        return False
    return ("searchquerystate=" in u or "/homes/for_sale" in u
            or "/homes/for_rent" in u or "searchqueryparams" in u)


def _detect_type(s: str) -> str:
    s = s.strip()
    if s.startswith("http://") or s.startswith("https://"):
        if _is_zillow_search(s):
            return "zillow_search"
        return "url"
    # If 5+ chars and contains a digit, treat as address
    if len(s) >= 5 and re.search(r"\d", s):
        return "address"
    return "unknown"


def _slug_address(addr: str) -> str:
    s = (addr or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:80]


def _full_address(L: dict) -> str:
    """Compose a full address string from an AI listing dict."""
    street = (L.get("address") or "").strip()
    city = (L.get("city") or "").strip()
    state = (L.get("state") or "").strip()
    zipc = str(L.get("zip") or "").strip()
    tail = ", ".join(p for p in [city, f"{state} {zipc}".strip()] if p)
    if tail and tail.lower() not in street.lower():
        return f"{street}, {tail}" if street else tail
    return street


def _shape_ai_listing(L: dict) -> dict:
    """Turn an AI area-search listing into a scrape-shaped dict that
    _save_as_deal understands. No network call — uses the AI-provided data."""
    full = _full_address(L)
    price = L.get("price")
    shaped = {
        "source": "ai_search",
        "address": full,
        "street": (L.get("address") or "").strip(),
        "city": (L.get("city") or "").strip(),
        "state": (L.get("state") or "").strip(),
        "zip": str(L.get("zip") or "").strip(),
        "home_type": "Single Family Residence",
        "beds": L.get("beds"),
        "baths": L.get("baths"),
        "sqft": L.get("sqft"),
        "listing_price": price,
        "price": price,
        "zestimate": L.get("zestimate"),
        "description": "Imported from Zillow area search. "
                       "Verify the live listing, price, and condition.",
    }
    # API search results carry the listing photo — use it directly.
    if L.get("image"):
        shaped["image"] = L["image"]
        shaped["image_gallery"] = [L["image"]]
    if L.get("home_type"):
        shaped["home_type"] = zillow_api.friendly_type(L["home_type"]) or shaped["home_type"]
    # Source link: the AI URL if it had one, else a Zillow address search so the
    # user can click through to the live listing.
    url = (L.get("url") or "").strip()
    if not url and full:
        from urllib.parse import quote_plus
        url = f"https://www.zillow.com/homes/{quote_plus(full)}_rb/"
    shaped["source_url"] = url
    # Optional Street View exterior photo (only if a Google Maps key is set
    # and the listing didn't already come with its own photo).
    if not shaped.get("image"):
        try:
            from . import ai_research, scraper as _sc
            mkey = ai_research.get_maps_key()
            if mkey and full:
                img = _sc.street_view_image_url(full, mkey)
                shaped["image"] = img
                shaped["image_gallery"] = [img]
        except Exception:
            pass
    return shaped


def _fallback_from_ai_listing(item, raw, analyzer, db):
    """When scraping a listing page fails, salvage the deal from the AI search
    summary attached to the item (address/price/beds/baths), so a real listing
    isn't dropped just because its page was blocked or slow."""
    L = item.get("ai_listing")
    if not L:
        return None
    data = _shape_ai_listing(L)
    if not data.get("address"):
        return None
    item["progress_message"] = "listing page unavailable — saving from search summary…"
    item["result"] = {"source": "ai_search_fallback",
                      "address": data.get("address"),
                      "city": data.get("city"),
                      "image_count": len(data.get("image_gallery") or [])}
    return _save_as_deal(data, raw, analyzer, db, source_url=data.get("source_url"))


def _expand_search_items(job: "BatchJob") -> list:
    """Pre-pass: replace each Zillow-search item with one info row plus one
    item per listing the AI found in that search area. Runs inside the worker
    thread (the AI call takes ~30-60s)."""
    from . import ai_research
    scraper = job.deps["scraper"]
    # How many newest listings to pull, and whether to scrape each listing page
    # (for photos + full data) vs. build the deal straight from the AI summary.
    try:
        max_listings = int(job.options.get("search_max") or 15)
    except (TypeError, ValueError):
        max_listings = 15
    max_listings = max(1, min(max_listings, 40))
    scrape_each = bool(job.options.get("scrape_each", True))
    out = []
    for it in job.items:
        if it["type"] != "zillow_search" or it["status"] != "pending":
            out.append(it)
            continue

        it["status"] = "running"
        it["progress_message"] = f"finding the {max_listings} newest listings…"
        it["started_at"] = _now()
        job.updated_at = _now()

        try:
            params = scraper.parse_zillow_search_url(it["input"])
            if not params:
                raise ValueError("Could not read the search filters from this URL")
            # Zillow's own search API first (real-time, 1 request, verified
            # data incl. photo + zestimate); AI web search as fallback.
            res = None
            loc_term = (params.get("search_term") or "").strip()
            if loc_term and zillow_api.is_configured():
                api_ls = zillow_api.search_newest(
                    location=loc_term, max_dom=params.get("max_dom") or 7,
                    price_min=params.get("price_min"), price_max=params.get("price_max"),
                    beds_min=params.get("beds_min"), baths_min=params.get("baths_min"),
                    sqft_min=params.get("sqft_min"),
                    property_type=params.get("property_type"), limit=max_listings)
                if api_ls is not None:
                    res = {"ok": True, "listings": api_ls, "area_label": loc_term,
                           "notes": f"{len(api_ls)} newest listings straight from Zillow"}
            if res is None:
                it["progress_message"] = f"finding the {max_listings} newest listings (AI web search)…"
                res = ai_research.find_listings_in_area(params, max_listings=max_listings)
        except Exception as e:
            log.exception("Search expansion failed")
            it["status"] = "failed"
            it["error"] = f"Search expansion failed: {str(e)[:200]}"
            it["finished_at"] = _now()
            job.failed += 1
            out.append(it)
            continue

        if not res.get("ok"):
            it["status"] = "failed"
            it["error"] = res.get("error") or "AI returned no result"
            it["finished_at"] = _now()
            job.failed += 1
            out.append(it)
            continue

        listings = (res.get("listings") or [])[:max_listings]
        area = res.get("area_label") or ""
        it["status"] = "skipped"  # informational row, not a deal
        it["progress_message"] = f"{len(listings)} listings — {area}"
        it["error"] = (f"Expanded into {len(listings)} newest listings · {area}"
                       if listings else (res.get("notes") or "No listings found in area"))
        it["result"] = {"area_label": area, "count": len(listings),
                        "notes": res.get("notes"), "web_searches": res.get("web_searches_used")}
        it["finished_at"] = _now()
        job.skipped += 1
        out.append(it)

        for L in listings:
            url = (L.get("url") or "").strip()
            if url:
                # Scrape the individual listing page → full data + photos.
                kind, item_input, prefetched = "url", url, None
            elif scrape_each:
                # No URL: resolve the address to a listing and scrape it (photos).
                kind, item_input, prefetched = "address", _full_address(L), None
            else:
                # Fast path: build the deal straight from the AI summary (no photos).
                kind, item_input, prefetched = "prefetched", _full_address(L), L
            out.append({
                "input": item_input,
                "type": kind,
                "prefetched": prefetched,
                # carry the AI summary so a failed scrape can still fall back to it
                "ai_listing": L,
                "status": "pending",
                "progress_message": "",
                "result": None,
                "error": None,
                "deal_id": None,
                "started_at": None,
                "finished_at": None,
            })
        log.info("Batch %s expanded search → %d listings (%s), scrape_each=%s",
                 job.id, len(listings), area, scrape_each)
    return out


class BatchJob:
    def __init__(self, inputs: list, options: dict):
        self.id = str(uuid.uuid4())[:8]
        self.created_at = _now()
        self.updated_at = self.created_at
        self.status = "queued"        # queued / running / paused / completed / cancelled
        self.cancel_requested = False
        self.pause_requested = False
        self.resume_event = threading.Event()
        self.resume_event.set()  # not paused initially
        self.options = options
        self.items = []
        for raw in inputs:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            self.items.append({
                "input": line,
                "type": _detect_type(line),
                "status": "pending",         # pending / running / succeeded / failed / skipped
                "progress_message": "",       # live sub-step message
                "result": None,
                "error": None,
                "deal_id": None,
                "started_at": None,
                "finished_at": None,
            })
        self.thread = None
        self.deps = None  # set later for resume/restart
        # Aggregates
        self.total = len(self.items)
        self.succeeded = 0
        self.failed = 0
        self.skipped = 0
        self.current_index = 0   # for "currently processing item N"

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "cancel_requested": self.cancel_requested,
            "pause_requested": self.pause_requested,
            "options": self.options,
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "remaining": sum(1 for x in self.items if x["status"] == "pending"),
            "current_index": self.current_index,
            "progress_pct": (
                round(((self.succeeded + self.failed + self.skipped) / self.total) * 100, 1)
                if self.total else 0),
            "items": self.items,
        }


def create_job(inputs: list, options: dict, deps: dict) -> BatchJob:
    """Create + start a batch job.

    deps: {"db": DealsDB, "scraper": scraper module, "analyzer": analyzer module}
    """
    job = BatchJob(inputs, options or {})
    if not job.items:
        job.status = "completed"
        return job
    job.deps = deps
    with _LOCK:
        _JOBS[job.id] = job
    job.thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    job.thread.start()
    return job


def pause_job(job_id: str) -> bool:
    j = get_job(job_id)
    if not j or j.status != "running":
        return False
    j.pause_requested = True
    j.resume_event.clear()
    return True


def resume_job(job_id: str) -> bool:
    j = get_job(job_id)
    if not j or j.status not in ("paused", "queued"):
        return False
    j.pause_requested = False
    j.resume_event.set()
    return True


def restart_job(job_id: str) -> Optional[BatchJob]:
    """Create a new job with the same inputs as an existing one (resets statuses)."""
    j = get_job(job_id)
    if not j:
        return None
    inputs = [item["input"] for item in j.items]
    return create_job(inputs, j.options or {}, j.deps)


def retry_failed_items(job_id: str) -> Optional[BatchJob]:
    """Create a new job from only the failed + skipped items of a previous job."""
    j = get_job(job_id)
    if not j:
        return None
    inputs = [item["input"] for item in j.items
              if item["status"] in ("failed", "skipped")]
    if not inputs:
        return None
    return create_job(inputs, j.options or {}, j.deps)


def get_job(job_id: str) -> Optional[BatchJob]:
    with _LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list:
    with _LOCK:
        return [j.snapshot() for j in
                 sorted(_JOBS.values(), key=lambda x: x.created_at, reverse=True)]


def cancel_job(job_id: str) -> bool:
    j = get_job(job_id)
    if not j:
        return False
    j.cancel_requested = True
    return True


def delete_job(job_id: str) -> bool:
    with _LOCK:
        if job_id in _JOBS:
            del _JOBS[job_id]
            return True
        return False


def _run_job(job: BatchJob):
    job.status = "running"
    job.updated_at = _now()
    log.info("Batch %s starting: %d items", job.id, job.total)
    rate_delay = float(job.options.get("delay_sec", 2.5))
    skip_duplicates = bool(job.options.get("skip_duplicates", True))
    deps = job.deps
    db = deps["db"]
    scraper = deps["scraper"]
    analyzer = deps["analyzer"]

    existing_urls = set()
    existing_addresses = set()
    if skip_duplicates:
        for d in db.list_deals():
            if d.get("source_url"):
                existing_urls.add(d["source_url"].strip().lower())
            if d.get("address"):
                existing_addresses.add(_slug_address(d["address"]))

    # Expand any Zillow search URLs into individual listings (AI web search).
    # This can grow job.items, so do it before computing the resume index.
    if any(it["type"] == "zillow_search" and it["status"] == "pending" for it in job.items):
        job.items = _expand_search_items(job)
        job.total = len(job.items)
        job.updated_at = _now()

    # Find resume index: skip already-processed items (when restarting after pause)
    start_idx = 0
    for i, it in enumerate(job.items):
        if it["status"] in ("succeeded", "failed", "skipped"):
            continue
        start_idx = i
        break
    else:
        start_idx = job.total  # nothing pending

    for idx in range(start_idx, job.total):
        item = job.items[idx]

        # ===== Cancel check =====
        if job.cancel_requested:
            log.info("Batch %s cancelled at %d/%d", job.id, idx, job.total)
            job.status = "cancelled"
            break

        # ===== Pause check (block until resume) =====
        if job.pause_requested:
            log.info("Batch %s pausing at %d/%d", job.id, idx, job.total)
            job.status = "paused"
            job.updated_at = _now()
            # Wait for resume signal (or cancel)
            while not job.resume_event.is_set():
                if job.cancel_requested:
                    job.status = "cancelled"
                    break
                time.sleep(0.3)
            if job.status == "cancelled":
                break
            log.info("Batch %s resumed at %d/%d", job.id, idx, job.total)
            job.status = "running"
            job.updated_at = _now()

        job.current_index = idx
        # Skip already-done (in case of resume)
        if item["status"] in ("succeeded", "failed", "skipped"):
            continue

        item["status"] = "running"
        item["progress_message"] = "starting…"
        item["started_at"] = _now()
        job.updated_at = _now()

        try:
            line_lower = item["input"].strip().lower()
            pf_addr = _slug_address((item.get("prefetched") or {}).get("address", "")) \
                if item["type"] == "prefetched" else ""
            if skip_duplicates and item["type"] == "url" and line_lower in existing_urls:
                item["status"] = "skipped"
                item["error"] = "Already on board"
                job.skipped += 1
            elif (skip_duplicates and item["type"] == "address" and
                  _slug_address(item["input"]) in existing_addresses):
                item["status"] = "skipped"
                item["error"] = "Already on board (by address)"
                job.skipped += 1
            elif skip_duplicates and item["type"] == "prefetched" and pf_addr and pf_addr in existing_addresses:
                item["status"] = "skipped"
                item["error"] = "Already on board (by address)"
                job.skipped += 1
            else:
                deal_id = _process_one(item, scraper, analyzer, db)
                if deal_id:
                    item["status"] = "succeeded"
                    item["progress_message"] = "saved"
                    item["deal_id"] = deal_id
                    job.succeeded += 1
                    existing_addresses.add(_slug_address(
                        item.get("result", {}).get("address", "")))
                    if pf_addr:
                        existing_addresses.add(pf_addr)
                    if item["type"] == "url":
                        existing_urls.add(line_lower)
                else:
                    item["status"] = "failed"
                    item["error"] = item.get("error") or "Scrape returned no data"
                    job.failed += 1
        except Exception as e:
            log.exception("Batch item failed: %s", item["input"])
            item["status"] = "failed"
            item["error"] = str(e)[:300]
            job.failed += 1
        finally:
            item["finished_at"] = _now()
            job.updated_at = _now()

        # Rate-limit (with frequent pause/cancel checks). Prefetched items did no
        # network fetch, so they don't need throttling.
        if idx < job.total - 1 and item["type"] != "prefetched":
            slept = 0.0
            while slept < rate_delay:
                if job.cancel_requested or job.pause_requested:
                    break
                time.sleep(0.2)
                slept += 0.2

    if job.status not in ("cancelled", "paused"):
        job.status = "completed"
    job.updated_at = _now()
    log.info("Batch %s ended (%s): %d ok, %d failed, %d skipped",
              job.id, job.status, job.succeeded, job.failed, job.skipped)


def _restart_worker_if_needed(job: BatchJob):
    """If a paused job is resumed and its worker thread died, spawn a new one."""
    if job.thread and job.thread.is_alive():
        return
    job.thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    job.thread.start()


def _process_one(item: dict, scraper, analyzer, db) -> Optional[str]:
    """Process a single batch item. Returns deal_id on success."""
    raw = item["input"]
    kind = item["type"]

    if kind == "url":
        # Zillow homedetails URL → the Data API first (structured JSON,
        # ~1s, no proxy credits, includes the photo gallery); page scrape
        # only as fallback or for non-Zillow URLs.
        data = None
        zm = re.search(r"/(\d+)_zpid", raw)
        if zm and zillow_api.is_configured():
            item["progress_message"] = "fetching from Zillow API…"
            try:
                data = zillow_api.scrape_shaped(zm.group(1))
            except Exception:
                data = None
        if not data:
            item["progress_message"] = "fetching listing page…"
            data = scraper.scrape(raw)
        if not data or data.get("scrape_error") or not data.get("address"):
            # Scrape blocked/failed — fall back to the AI summary if we have one
            # (so a real listing isn't lost just because the page didn't load).
            fb = _fallback_from_ai_listing(item, raw, analyzer, db)
            if fb:
                return fb
            item["error"] = (data.get("scrape_error") if data else None) or "scrape failed"
            return None
        item["progress_message"] = "parsing data…"
        item["result"] = {"source": data.get("source"),
                           "address": data.get("address"),
                           "city": data.get("city"),
                           "image_count": len(data.get("image_gallery") or [])}
        item["progress_message"] = "saving deal…"
        return _save_as_deal(data, raw, analyzer, db, source_url=raw)
    elif kind == "address":
        item["progress_message"] = "finding listing…"
        out = scraper.find_by_address(raw)
        data = out.get("data") or {}
        if not out.get("found") or not data.get("address"):
            fb = _fallback_from_ai_listing(item, raw, analyzer, db)
            if fb:
                return fb
            item["error"] = out.get("error") or "not found"
            return None
        item["progress_message"] = f"found via {out.get('source', '?')} — saving…"
        item["result"] = {"source": out.get("source"),
                           "url": out.get("url"),
                           "address": data.get("address"),
                           "image_count": len(data.get("image_gallery") or [])}
        return _save_as_deal(data, raw, analyzer, db, source_url=out.get("url"))
    elif kind == "prefetched":
        # Listing data already supplied by the AI area search — build the deal
        # directly (no scrape, no proxy credits). Photos via Street View if a
        # Google Maps key is set.
        item["progress_message"] = "saving deal…"
        data = _shape_ai_listing(item.get("prefetched") or {})
        item["result"] = {"source": "ai_search",
                           "address": data.get("address"),
                           "city": data.get("city"),
                           "image_count": len(data.get("image_gallery") or [])}
        return _save_as_deal(data, raw, analyzer, db, source_url=data.get("source_url"))
    else:
        item["error"] = "Unrecognized input (not a URL and too short to be an address)"
        return None


def _save_as_deal(data: dict, original_input: str, analyzer, db,
                   source_url: Optional[str] = None) -> Optional[str]:
    """Build a deal dict from scraped data and upsert into the board."""
    if not data.get("address") and not source_url:
        return None
    deal = {
        "address": data.get("address") or original_input,
        "street": data.get("street", ""),
        "city": data.get("city", ""),
        "state": data.get("state", ""),
        "zip": data.get("zip", ""),
        "property_type": data.get("home_type") or "Single Family Residence",
        "beds": data.get("beds") or data.get("bedrooms"),
        "baths": data.get("baths") or data.get("bathrooms"),
        "sqft": data.get("sqft"),
        "year_built": data.get("year_built"),
        "lot_size": data.get("lot_size", ""),
        # Best-guess financial fields (user will edit)
        "purchase_price": (data.get("listing_price") or 0),
        "arv_base": (data.get("zestimate") or data.get("listing_price") or 0),
        "arv_low": data.get("comp_value_low"),
        "arv_high": data.get("comp_value_high"),
        "arv_confidence": "Low" if not data.get("zestimate") else "Medium",
        "rehab_base": data.get("rehab_estimate") or 0,
        "rehab_scope": "Mid-level",
        "holding_months": 5,
        "holding_cost_monthly": 500,
        "selling_cost_pct": 8,
        "estimated_rent": data.get("rent_zestimate"),
        "monthly_taxes": data.get("monthly_taxes"),
        "vacancy_pct": 8,
        "median_dom": data.get("median_dom"),
        "image": data.get("image"),
        "image_gallery": data.get("image_gallery"),
        "lat": data.get("lat"),
        "lng": data.get("lng"),
        "sale_comparables": data.get("sale_comparables"),
        "rent_comparables": data.get("rent_comparables"),
        "zillow_estimate": data.get("zillow_estimate"),
        "realtor_estimate": data.get("realtor_estimate"),
        "comp_value_estimate": data.get("comp_value_estimate"),
        "comp_value_low": data.get("comp_value_low"),
        "comp_value_high": data.get("comp_value_high"),
        "description": data.get("description"),
        "notes": data.get("description") or "",
        "source": data.get("source"),
        "source_url": source_url,
        "status": "evaluating",
    }
    # Compute score
    try:
        m = analyzer.compute_metrics(deal)
        score, grade, signal = analyzer.compute_score(deal, m)
        deal["score"] = score; deal["grade"] = grade; deal["signal"] = signal
    except Exception:
        pass
    saved = db.upsert_deal(deal)
    return saved["id"]
