"""JSON-backed persistence for deals."""

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:80]


class DealsDB:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.db_path.exists():
            self._write({
                "created": datetime.utcnow().isoformat() + "Z",
                "updated": datetime.utcnow().isoformat() + "Z",
                "deals": [],
            })

    def _read(self) -> dict:
        with _LOCK:
            with open(self.db_path, "r") as f:
                return json.load(f)

    def _write(self, data: dict):
        with _LOCK:
            tmp = self.db_path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.db_path)

    def list_deals(self) -> list:
        return self._read().get("deals", [])

    def get_deal(self, deal_id: str) -> Optional[dict]:
        return next((d for d in self.list_deals() if d["id"] == deal_id), None)

    def upsert_deal(self, deal: dict) -> dict:
        data = self._read()
        if "id" not in deal or not deal["id"]:
            base = deal.get("address", "deal")
            deal["id"] = _slugify(base)
        # Ensure unique id
        existing_ids = {d["id"] for d in data["deals"] if d["id"] != deal["id"]}
        original = deal["id"]
        n = 1
        while deal["id"] in existing_ids:
            deal["id"] = f"{original}-{n}"
            n += 1
        deal["last_analyzed"] = datetime.utcnow().isoformat() + "Z"
        if not deal.get("added_date"):
            deal["added_date"] = datetime.utcnow().isoformat() + "Z"

        idx = next((i for i, d in enumerate(data["deals"])
                    if d["id"] == deal["id"]), None)
        if idx is None:
            data["deals"].append(deal)
        else:
            # Preserve added_date from existing
            deal["added_date"] = data["deals"][idx].get(
                "added_date", deal["added_date"])
            data["deals"][idx] = deal
        data["updated"] = datetime.utcnow().isoformat() + "Z"
        self._write(data)
        return deal

    def delete_deal(self, deal_id: str) -> bool:
        data = self._read()
        before = len(data["deals"])
        data["deals"] = [d for d in data["deals"] if d["id"] != deal_id]
        if len(data["deals"]) < before:
            data["updated"] = datetime.utcnow().isoformat() + "Z"
            self._write(data)
            return True
        return False

    def update_field(self, deal_id: str, field: str, value) -> Optional[dict]:
        data = self._read()
        for d in data["deals"]:
            if d["id"] == deal_id:
                d[field] = value
                d["last_analyzed"] = datetime.utcnow().isoformat() + "Z"
                data["updated"] = datetime.utcnow().isoformat() + "Z"
                self._write(data)
                return d
        return None

    def raw(self) -> dict:
        return self._read()
