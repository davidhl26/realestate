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

log = logging.getLogger("flip-board.batch")

_JOBS = {}            # job_id -> BatchJob
_LOCK = threading.Lock()


def _now():
    return datetime.utcnow().isoformat() + "Z"


def _detect_type(s: str) -> str:
    s = s.strip()
    if s.startswith("http://") or s.startswith("https://"):
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
            if skip_duplicates and item["type"] == "url" and line_lower in existing_urls:
                item["status"] = "skipped"
                item["error"] = "Already on board"
                job.skipped += 1
            elif (skip_duplicates and item["type"] == "address" and
                  _slug_address(item["input"]) in existing_addresses):
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

        # Rate-limit (with frequent pause/cancel checks)
        if idx < job.total - 1:
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
        item["progress_message"] = "fetching URL…"
        data = scraper.scrape(raw)
        if not data or data.get("scrape_error"):
            item["error"] = data.get("scrape_error") if data else "scrape failed"
            return None
        item["progress_message"] = "parsing data…"
        item["result"] = {"source": data.get("source"),
                           "address": data.get("address"),
                           "city": data.get("city"),
                           "image_count": len(data.get("image_gallery") or [])}
        item["progress_message"] = "saving deal…"
        return _save_as_deal(data, raw, analyzer, db, source_url=raw)
    elif kind == "address":
        item["progress_message"] = "searching Zillow…"
        out = scraper.find_by_address(raw)
        if not out.get("found"):
            item["error"] = out.get("error") or "not found"
            return None
        item["progress_message"] = f"found via {out.get('source', '?')} — saving…"
        data = out.get("data") or {}
        item["result"] = {"source": out.get("source"),
                           "url": out.get("url"),
                           "address": data.get("address"),
                           "image_count": len(data.get("image_gallery") or [])}
        return _save_as_deal(data, raw, analyzer, db, source_url=out.get("url"))
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
