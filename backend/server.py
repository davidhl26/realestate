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


def _enrich(deal: dict) -> dict:
    """Compute metrics + score + signal, return augmented dict."""
    m = analyzer.compute_metrics(deal)
    score, grade, signal = analyzer.compute_score(deal, m)
    return {"deal": deal, "metrics": m, "score": score,
            "grade": grade, "signal": signal}


@app.get("/api/healthz")
def healthz():
    return {"ok": True, "deals": len(db.list_deals())}


@app.get("/api/deals")
def list_deals():
    deals = db.list_deals()
    out = []
    for d in deals:
        try:
            m = analyzer.compute_metrics(d)
            score, grade, signal = analyzer.compute_score(d, m)
            out.append({
                "id": d["id"],
                "address": d.get("address", ""),
                "city": d.get("city", ""),
                "state": d.get("state", ""),
                "beds": d.get("beds"),
                "baths": d.get("baths"),
                "sqft": d.get("sqft"),
                "year_built": d.get("year_built"),
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
    # Ensure required minimal fields
    if not deal.get("address"):
        raise HTTPException(400, "address is required")
    if not deal.get("purchase_price"):
        raise HTTPException(400, "purchase_price is required")
    # Auto-compute score/grade/signal so they are stored
    m = analyzer.compute_metrics(deal)
    score, grade, signal = analyzer.compute_score(deal, m)
    deal["score"] = score
    deal["grade"] = grade
    deal["signal"] = signal
    saved = db.upsert_deal(deal)
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
    saved = db.upsert_deal(d)
    return {
        "ok": True,
        "fields_updated": len(updates),
        "photos": len(updates.get("image_gallery", [])),
        "sale_comps": len(updates.get("sale_comparables", [])),
        "rent_comps": len(updates.get("rent_comparables", [])),
    }


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
    cash_needed = purchase + rehab
    loan_amount = 0
    if fin_method and fin_method != "cash":
        ltv_pct = float(options.get("loan_ltv_pct") or 0)
        rate_pct = float(options.get("interest_rate_pct") or 0)
        orig_pct = float(options.get("origination_pct") or 0)
        term_months = int(options.get("loan_term_months") or holding_months)
        rehab_financed = options.get("rehab_financed", "yes") == "yes"
        loan_base = purchase * ltv_pct / 100
        loan_amount = loan_base + (rehab if rehab_financed else 0)
        interest = loan_amount * (rate_pct / 100) * (term_months / 12)
        points_paid = loan_amount * (orig_pct / 100)
        financing_cost = interest + points_paid
        cash_needed = (purchase - loan_base) + (0 if rehab_financed else rehab) + points_paid

    # Attach a "scenario" block to the deal for the PDF generator to use
    deal_copy["scenario"] = {
        "strategy": options.get("strategy", "flip"),
        "financing_method": fin_method,
        "loan_amount": loan_amount,
        "loan_ltv_pct": options.get("loan_ltv_pct"),
        "interest_rate_pct": options.get("interest_rate_pct"),
        "origination_pct": options.get("origination_pct"),
        "loan_term_months": options.get("loan_term_months"),
        "rehab_financed": options.get("rehab_financed"),
        "purchase_closing_pct": options.get("purchase_closing_pct", 2),
        "due_diligence_fees": options.get("due_diligence_fees", 0),
        "other_fees": options.get("other_fees", 0),
        "financing_cost": financing_cost,
        "points_paid": points_paid,
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
    return {
        "configured": bool(key),
        "key_preview": (key[:8] + "..." + key[-4:]) if key else "",
        "model": cfg.get("model", "claude-opus-4-7"),
        "source": source,
        "maps_configured": bool(maps_key),
        "maps_key_preview": (maps_key[:6] + "..." + maps_key[-4:]) if maps_key else "",
        "proxy_configured": bool(proxy_key),
        "proxy_key_preview": (proxy_key[:6] + "..." + proxy_key[-4:]) if proxy_key else "",
    }


@app.post("/api/ai/config")
def ai_config_set(payload: dict = Body(...)):
    cfg = ai_research.read_config()
    if "anthropic_api_key" in payload:
        cfg["anthropic_api_key"] = (payload["anthropic_api_key"] or "").strip()
    if "model" in payload:
        cfg["model"] = (payload["model"] or "claude-opus-4-7").strip()
    if "google_maps_key" in payload:
        cfg["google_maps_key"] = (payload["google_maps_key"] or "").strip()
    if "scraper_api_key" in payload:
        cfg["scraper_api_key"] = (payload["scraper_api_key"] or "").strip()
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
    INDEPENDENT = ["arv", "rehab", "rent_comps", "neighborhood",
                   "taxes_insurance", "history", "risks", "photos", "red_flags"]

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


# Mount static frontend last so /api routes take precedence.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True),
              name="frontend")


def run(host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
