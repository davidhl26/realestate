"""Rehab projects (chantiers) — manage every renovation in progress.

One project per property being renovated, usually linked to a deal on the
board. Tracks the work breakdown (budget line items with status), the real
spend (expense log — the single source of truth for "actual"), contractors,
dates, and the holding-cost clock. Same JSON-store pattern as the other DBs.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

# RLock so CRUD methods can hold it across their whole read-modify-write
# (the endpoints run concurrently in FastAPI's threadpool — two PATCHes
# interleaving _read/_write would silently drop one of the writes).
_LOCK = threading.RLock()

STATUSES = ("planning", "active", "paused", "done")
ITEM_STATUSES = ("todo", "doing", "done")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _parse_date(s) -> Optional[date]:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


class RehabsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"projects": []})

    def _read(self) -> dict:
        with _LOCK:
            try:
                with open(self.path) as f:
                    return json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                return {"projects": []}

    def _write(self, data: dict):
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

    # ---- sanitizers ----------------------------------------------------
    @staticmethod
    def _clean_items(items) -> list:
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            label = str(it.get("label") or "").strip()
            if not label:
                continue
            status = it.get("status") if it.get("status") in ITEM_STATUSES else "todo"
            out.append({
                "id": str(it.get("id") or uuid.uuid4().hex[:8]),
                "label": label[:160],
                "budget": round(_num(it.get("budget")), 2),
                "status": status,
                "contractor": str(it.get("contractor") or "").strip()[:80],
            })
        return out[:80]

    @staticmethod
    def _clean_expenses(expenses) -> list:
        out = []
        for e in expenses or []:
            if not isinstance(e, dict):
                continue
            amount = round(_num(e.get("amount")), 2)
            vendor = str(e.get("vendor") or "").strip()
            if not amount and not vendor:
                continue
            out.append({
                "id": str(e.get("id") or uuid.uuid4().hex[:8]),
                "date": str(e.get("date") or "")[:10],
                "vendor": vendor[:80],
                "amount": amount,
                "item_id": str(e.get("item_id") or "") or None,
                "note": str(e.get("note") or "").strip()[:200],
            })
        return out[:500]

    @staticmethod
    def _clean_contractors(contractors) -> list:
        out = []
        for c in contractors or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            out.append({
                "id": str(c.get("id") or uuid.uuid4().hex[:8]),
                "name": name[:80],
                "trade": str(c.get("trade") or "").strip()[:60],
                "phone": str(c.get("phone") or "").strip()[:40],
                "notes": str(c.get("notes") or "").strip()[:300],
            })
        return out[:40]

    # ---- CRUD ----------------------------------------------------------
    EDITABLE = ("address", "city", "state", "status", "start_date", "target_date",
                "completed_date", "holding_cost_monthly", "notes",
                "budget_items", "expenses", "contractors")

    def create(self, payload: dict) -> dict:
      with _LOCK:
        data = self._read()
        p = {
            "id": "rh" + uuid.uuid4().hex[:8],
            "deal_id": payload.get("deal_id") or None,
            "address": str(payload.get("address") or "").strip() or "Untitled project",
            "city": str(payload.get("city") or "").strip(),
            "state": str(payload.get("state") or "").strip(),
            "status": payload.get("status") if payload.get("status") in STATUSES else "planning",
            "start_date": str(payload.get("start_date") or "")[:10] or None,
            "target_date": str(payload.get("target_date") or "")[:10] or None,
            "completed_date": (_now()[:10] if payload.get("status") == "done" else None),
            "holding_cost_monthly": round(_num(payload.get("holding_cost_monthly"), 500)),
            "budget_items": self._clean_items(payload.get("budget_items")),
            "expenses": self._clean_expenses(payload.get("expenses")),
            "contractors": self._clean_contractors(payload.get("contractors")),
            "notes": str(payload.get("notes") or "")[:5000],
            "created_at": _now(), "updated_at": _now(),
        }
        data["projects"].append(p)
        self._write(data)
        return p

    def list_projects(self) -> list:
        return self._read().get("projects", [])

    def get(self, project_id: str) -> Optional[dict]:
        return next((p for p in self.list_projects() if p.get("id") == project_id), None)

    def update(self, project_id: str, updates: dict) -> Optional[dict]:
      with _LOCK:
        data = self._read()
        p = next((x for x in data["projects"] if x.get("id") == project_id), None)
        if not p:
            return None
        for k in self.EDITABLE:
            if k not in updates:
                continue
            v = updates[k]
            if k == "status":
                if v not in STATUSES:
                    continue
                # stamp / clear the completion date on transitions
                if v == "done" and p.get("status") != "done":
                    p["completed_date"] = _now()[:10]
                elif v != "done":
                    p["completed_date"] = None
            elif k == "budget_items":
                v = self._clean_items(v)
            elif k == "expenses":
                v = self._clean_expenses(v)
            elif k == "contractors":
                v = self._clean_contractors(v)
            elif k == "holding_cost_monthly":
                v = round(_num(v, 500))
            elif k in ("start_date", "target_date", "completed_date"):
                v = str(v or "")[:10] or None
            elif k == "notes":
                v = str(v or "")[:5000]
            else:
                v = str(v or "").strip()
            p[k] = v
        p["updated_at"] = _now()
        self._write(data)
        return p

    def delete(self, project_id: str) -> bool:
      with _LOCK:
        data = self._read()
        before = len(data["projects"])
        data["projects"] = [p for p in data["projects"] if p.get("id") != project_id]
        if len(data["projects"]) < before:
            self._write(data)
            return True
        return False

    # ---- derived numbers ----------------------------------------------
    @staticmethod
    def summary(p: dict) -> dict:
        """Computed view for the UI: budget vs actual, weighted progress,
        schedule and holding-cost clock."""
        items = p.get("budget_items") or []
        expenses = p.get("expenses") or []
        budget_total = round(sum(_num(i.get("budget")) for i in items), 2)
        actual_total = round(sum(_num(e.get("amount")) for e in expenses), 2)
        # per-item actuals from assigned expenses
        by_item = {}
        for e in expenses:
            iid = e.get("item_id")
            if iid:
                by_item[iid] = round(by_item.get(iid, 0) + _num(e.get("amount")), 2)
        # progress weighted by budget (fallback: plain item count)
        done_w = sum(_num(i.get("budget")) for i in items if i.get("status") == "done")
        if budget_total > 0:
            progress = round(100 * done_w / budget_total)
        elif items:
            progress = round(100 * sum(1 for i in items if i.get("status") == "done") / len(items))
        else:
            progress = 0

        today = date.today()
        start = _parse_date(p.get("start_date"))
        target = _parse_date(p.get("target_date"))
        end = _parse_date(p.get("completed_date")) or today
        days_elapsed = max(0, (end - start).days) if start else None
        days_remaining = (target - today).days if (target and p.get("status") != "done") else None
        holding = round(_num(p.get("holding_cost_monthly"), 0) * (days_elapsed or 0) / 30.4) \
            if days_elapsed is not None else 0

        return {
            **p,
            "budget_total": budget_total,
            "actual_total": actual_total,
            "variance": round(actual_total - budget_total, 2),
            "actual_by_item": by_item,
            "progress_pct": progress,
            "items_done": sum(1 for i in items if i.get("status") == "done"),
            "items_total": len(items),
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "holding_accrued": holding,
        }
