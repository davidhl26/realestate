"""Zillow watches — saved searches that run repeatedly and diff the results.

A watch = criteria (location + filters). Each run compares the fresh listings
against what the watch has already seen and records EVENTS:
  - new         first time this address shows up
  - price_drop  same address, lower price than last seen (delta recorded)
  - gone        not seen in 3 consecutive runs (probably sold/delisted)
Listings keep a price_history so drops accumulate over time. Same JSON-store
pattern as the other DBs.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()

MAX_EVENTS = 120
GONE_AFTER_MISSES = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _listing_key(l: dict) -> str:
    """Stable identity for a listing across runs: zpid URL if present, else
    normalized address."""
    url = (l.get("url") or "")
    m = re.search(r"(\d+)_zpid", url)
    if m:
        return f"zpid:{m.group(1)}"
    addr = re.sub(r"[^a-z0-9]+", " ", (l.get("address") or "").lower()).strip()
    return f"addr:{addr}"


class WatchesDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"watches": [], "created": _now(), "updated": _now()})

    def _read(self) -> dict:
        with _LOCK:
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {"watches": [], "created": _now(), "updated": _now()}

    def _write(self, data: dict):
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

    # ---- CRUD ----
    def list_watches(self) -> list:
        return self._read().get("watches", [])

    def get(self, watch_id: str) -> Optional[dict]:
        return next((w for w in self.list_watches() if w.get("id") == watch_id), None)

    def create(self, criteria: dict) -> dict:
        data = self._read()
        w = {
            "id": uuid.uuid4().hex[:10],
            "label": criteria.get("label") or criteria.get("location", "Watch"),
            "location": (criteria.get("location") or "").strip(),
            "price_max": criteria.get("price_max"),
            "price_min": criteria.get("price_min"),
            "beds_min": criteria.get("beds_min"),
            "property_type": criteria.get("property_type"),
            "max_listings": min(int(criteria.get("max_listings") or 15), 30),
            # Auto-run cadence in minutes (60 = hourly). 0 = manual only.
            "interval_min": int(criteria.get("interval_min") if criteria.get("interval_min") is not None else 60),
            "created_at": _now(),
            "last_run": None,
            "run_count": 0,
            "listings": {},   # key -> tracked listing
            "events": [],     # newest first
        }
        data["watches"].append(w)
        data["updated"] = _now()
        self._write(data)
        return w

    def delete(self, watch_id: str) -> bool:
        data = self._read()
        before = len(data["watches"])
        data["watches"] = [w for w in data["watches"] if w.get("id") != watch_id]
        if len(data["watches"]) < before:
            data["updated"] = _now()
            self._write(data)
            return True
        return False

    def save(self, watch: dict):
        data = self._read()
        idx = next((i for i, w in enumerate(data["watches"])
                    if w.get("id") == watch.get("id")), None)
        if idx is None:
            return
        data["watches"][idx] = watch
        data["updated"] = _now()
        self._write(data)

    # ---- Diff engine ----
    def apply_run(self, watch_id: str, fresh_listings: list) -> dict:
        """Merge a fresh result set into the watch: detect new / price-drop /
        gone, update price histories, append events. Returns a summary."""
        w = self.get(watch_id)
        if not w:
            return {"ok": False, "error": "watch not found"}
        now = _now()
        tracked = w.get("listings") or {}
        events = w.get("events") or []
        seen_now = set()
        n_new, n_drops = 0, 0

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        for l in fresh_listings or []:
            key = _listing_key(l)
            if not key or key in ("addr:",):
                continue
            seen_now.add(key)
            price = _num(l.get("price"))
            snap = {k: l.get(k) for k in
                    ("url", "address", "city", "state", "zip", "price", "beds",
                     "baths", "sqft", "year_built", "last_renovated",
                     "arv_estimate", "rehab_estimate", "days_on_market")}
            if key not in tracked:
                tracked[key] = {**snap, "first_seen": now, "last_seen": now,
                                "misses": 0, "status": "active",
                                "price_history": ([{"ts": now, "price": price}]
                                                   if price is not None else [])}
                events.insert(0, {"ts": now, "type": "new", "key": key, **snap})
                n_new += 1
            else:
                t = tracked[key]
                t["last_seen"] = now
                t["misses"] = 0
                t["status"] = "active"
                old_price = _num(t.get("price"))
                if price is not None and old_price is not None and price < old_price:
                    delta = int(round(old_price - price))
                    events.insert(0, {"ts": now, "type": "price_drop", "key": key,
                                      **snap, "old_price": int(old_price),
                                      "drop": delta})
                    n_drops += 1
                if price is not None and price != old_price:
                    t.setdefault("price_history", []).append({"ts": now, "price": price})
                    t["price_history"] = t["price_history"][-20:]
                # refresh mutable fields
                t.update({k: v for k, v in snap.items() if v is not None})

        # Misses → gone (AI search can skip a listing once; 3 misses = signal)
        n_gone = 0
        for key, t in tracked.items():
            if key in seen_now or t.get("status") == "gone":
                continue
            t["misses"] = int(t.get("misses") or 0) + 1
            if t["misses"] >= GONE_AFTER_MISSES:
                t["status"] = "gone"
                events.insert(0, {"ts": now, "type": "gone", "key": key,
                                  "address": t.get("address"), "price": t.get("price"),
                                  "url": t.get("url")})
                n_gone += 1

        w["listings"] = tracked
        w["events"] = events[:MAX_EVENTS]
        w["last_run"] = now
        w["run_count"] = int(w.get("run_count") or 0) + 1
        self.save(w)
        return {"ok": True, "new": n_new, "price_drops": n_drops, "gone": n_gone,
                "tracked": len(tracked), "found": len(seen_now)}

    def summary(self, w: dict) -> dict:
        """Lightweight view for the list UI (no full listings payload)."""
        listings = w.get("listings") or {}
        active = [t for t in listings.values() if t.get("status") != "gone"]
        return {
            "id": w["id"], "label": w.get("label"), "location": w.get("location"),
            "price_max": w.get("price_max"), "price_min": w.get("price_min"),
            "beds_min": w.get("beds_min"), "property_type": w.get("property_type"),
            "max_listings": w.get("max_listings"),
            "interval_min": w.get("interval_min", 60),
            "created_at": w.get("created_at"), "last_run": w.get("last_run"),
            "run_count": w.get("run_count", 0),
            "tracked": len(active),
            "events": (w.get("events") or [])[:40],
        }

    def due_watches(self) -> list:
        """Watches whose auto-interval has elapsed since their last run."""
        out = []
        now = datetime.now(timezone.utc)
        for w in self.list_watches():
            try:
                interval = int(w.get("interval_min", 60) or 0)
            except (TypeError, ValueError):
                interval = 60
            if interval <= 0:
                continue   # manual-only
            lr = w.get("last_run")
            if not lr:
                out.append(w["id"])
                continue
            try:
                age_min = (now - datetime.fromisoformat(lr)).total_seconds() / 60
            except ValueError:
                age_min = interval + 1
            if age_min >= interval:
                out.append(w["id"])
        return out
