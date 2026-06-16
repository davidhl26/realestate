"""Auction watchlist — auctions the user wants to track over time.

Stores the analysis (ARV, rehab, max bid, verdict) plus the form inputs so a
recheck can re-run the AI estimate. Lightweight JSON store, same pattern as the
other DBs in this app.
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(s: str) -> str:
    s = (s or "").lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or uuid.uuid4().hex[:8]


# Fields copied from the analyze payload/result onto a watch item.
_FIELDS = (
    "address", "url", "opening_bid", "auction_date", "beds", "baths", "sqft",
    "year_built", "comments", "target_margin_pct", "holding",
    "arv", "rehab", "max_bid", "mao70", "profit_at_max", "verdict",
    "verdict_note", "arv_confidence", "condition_summary", "summary", "risks",
)


class WatchlistDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"items": [], "created": _now(), "updated": _now()})

    def _read(self) -> dict:
        with _LOCK:
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {"items": [], "created": _now(), "updated": _now()}

    def _write(self, data: dict):
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

    def list_items(self) -> list:
        items = self._read().get("items", [])
        # Soonest auction date first; undated last; then newest tracked.
        items.sort(key=lambda x: (x.get("auction_date") or "9999",
                                  x.get("created_at") or ""))
        return items

    def get(self, item_id: str) -> Optional[dict]:
        return next((x for x in self._read().get("items", [])
                     if x.get("id") == item_id), None)

    def upsert(self, payload: dict) -> dict:
        """Add or update a watched auction. Keyed by id, or by address slug."""
        data = self._read()
        item_id = payload.get("id") or _slug(payload.get("address", ""))
        idx = next((i for i, x in enumerate(data["items"])
                    if x.get("id") == item_id), None)
        item = data["items"][idx] if idx is not None else {
            "id": item_id, "created_at": _now(), "history": []}
        for k in _FIELDS:
            if k in payload and payload[k] not in (None, ""):
                item[k] = payload[k]
        item["last_checked"] = _now()
        # Record a small history point so the user sees movement over time.
        item.setdefault("history", []).append({
            "ts": _now(),
            "max_bid": item.get("max_bid"),
            "opening_bid": item.get("opening_bid"),
            "verdict": item.get("verdict"),
        })
        item["history"] = item["history"][-30:]
        if idx is None:
            data["items"].append(item)
        else:
            data["items"][idx] = item
        data["updated"] = _now()
        self._write(data)
        return item

    def delete(self, item_id: str) -> bool:
        data = self._read()
        before = len(data["items"])
        data["items"] = [x for x in data["items"] if x.get("id") != item_id]
        if len(data["items"]) < before:
            data["updated"] = _now()
            self._write(data)
            return True
        return False
