"""AI spend meter — records every Claude call's tokens and estimated cost.

Tiny JSON store (DATA_DIR/ai-usage.json), aggregated per month and per task
label. Costs are ESTIMATES computed from list prices at record time; the
point is relative visibility (which feature burns what) and a monthly
budget guard for the automatic paths, not accounting-grade numbers.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("flip-board.ai_usage")

_LOCK = threading.Lock()
_PATH: Path = None   # set by init()

# $ per 1M tokens (input, output) — list prices, matched by substring.
_PRICES = [
    ("opus", (5.00, 25.00)),
    ("sonnet", (3.00, 15.00)),
    ("haiku", (1.00, 5.00)),
]
_DEFAULT_PRICE = (5.00, 25.00)
_WEB_SEARCH_PER_CALL = 0.01          # $10 / 1,000 searches


def init(data_dir):
    global _PATH
    _PATH = Path(data_dir) / "ai-usage.json"


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _read() -> dict:
    if not _PATH:
        return {"months": {}}
    try:
        return json.loads(_PATH.read_text())
    except Exception:
        return {"months": {}}


def _price_for(model: str):
    m = (model or "").lower()
    for key, price in _PRICES:
        if key in m:
            return price
    return _DEFAULT_PRICE


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  web_searches: int = 0) -> float:
    pin, pout = _price_for(model)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000 \
        + web_searches * _WEB_SEARCH_PER_CALL


def record(label: str, model: str, usage=None, web_searches: int = 0):
    """Add one call to the meter. `usage` is the {input_tokens, output_tokens}
    dict (or an SDK Usage object). Never raises — metering must not break
    the feature being metered."""
    try:
        if not _PATH:
            return
        ti = int(getattr(usage, "input_tokens", None)
                 or (usage or {}).get("input_tokens", 0) or 0)
        to = int(getattr(usage, "output_tokens", None)
                 or (usage or {}).get("output_tokens", 0) or 0)
        cost = estimate_cost(model, ti, to, web_searches)
        with _LOCK:
            data = _read()
            month = data.setdefault("months", {}).setdefault(_month(), {"by_task": {}})
            t = month["by_task"].setdefault(label, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "web_searches": 0, "cost": 0.0})
            t["calls"] += 1
            t["input_tokens"] += ti
            t["output_tokens"] += to
            t["web_searches"] += int(web_searches or 0)
            t["cost"] = round(t["cost"] + cost, 4)
            # keep only the last 12 months
            months = sorted(data["months"])
            for old in months[:-12]:
                del data["months"][old]
            tmp = _PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(_PATH)
    except Exception:
        log.exception("usage recording failed (ignored)")


def record_msg(label: str, model: str, msg):
    """Record straight from an SDK Message (counts server_tool_use blocks)."""
    try:
        web = sum(1 for b in (getattr(msg, "content", None) or [])
                  if getattr(b, "type", "") == "server_tool_use")
        record(label, model, getattr(msg, "usage", None), web_searches=web)
    except Exception:
        log.exception("usage recording failed (ignored)")


def month_summary() -> dict:
    """Current month's spend, for Settings: total + per-task breakdown."""
    data = _read()
    month = data.get("months", {}).get(_month(), {"by_task": {}})
    tasks = [{"task": k, **v} for k, v in month["by_task"].items()]
    tasks.sort(key=lambda t: -t["cost"])
    return {
        "month": _month(),
        "total_cost": round(sum(t["cost"] for t in tasks), 2),
        "total_calls": sum(t["calls"] for t in tasks),
        "total_web_searches": sum(t["web_searches"] for t in tasks),
        "by_task": tasks,
    }


def auto_budget_exceeded(cfg: dict) -> bool:
    """True when the optional monthly AI budget (Settings, ai_budget_monthly,
    0 = no limit) is used up — the AUTOMATIC paths (deal auto-enrich) stop
    spending; user-initiated actions keep working."""
    try:
        budget = float(cfg.get("ai_budget_monthly") or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        return False
    return month_summary()["total_cost"] >= budget
