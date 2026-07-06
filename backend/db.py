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


# Suffix/direction abbreviations so "3457 E 149th Street" == "3457 E 149th St".
_ADDR_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd",
    "drive": "dr", "road": "rd", "lane": "ln", "court": "ct", "place": "pl",
    "terrace": "ter", "circle": "cir", "parkway": "pkwy", "highway": "hwy",
    "square": "sq", "trail": "trl", "north": "n", "south": "s", "east": "e",
    "west": "w", "northeast": "ne", "northwest": "nw", "southeast": "se",
    "southwest": "sw",
}
_STREET_SUFFIXES = {"st", "ave", "blvd", "dr", "rd", "ln", "ct", "pl", "ter",
                    "cir", "pkwy", "hwy", "sq", "trl", "way", "loop", "run"}


def normalize_address(addr: str) -> str:
    """Canonical form of a full address for duplicate detection."""
    words = re.sub(r"[^a-z0-9 ]+", " ", (addr or "").lower()).split()
    return " ".join(_ADDR_ABBREV.get(w, w) for w in words)


def address_dup_key(addr: str) -> str:
    """Loose duplicate key: house number + street-name words (suffix dropped)
    + zip. Catches '1896 Belvoir Blvd' vs '1896 Belvoir Rd' (same number,
    same street name, different suffix) and Street/St variants."""
    words = normalize_address(addr).split()
    if not words or not words[0].isdigit():
        return normalize_address(addr)  # no house number → strict form only
    number = words[0]
    zips = [w for w in words[1:] if re.fullmatch(r"\d{5}", w)]
    zipc = zips[-1] if zips else ""
    street = []
    for w in words[1:]:
        if w in _STREET_SUFFIXES:
            break
        if re.fullmatch(r"\d{5}", w):
            break
        street.append(w)
        if len(street) >= 3:
            break
    return f"{number} {' '.join(street)} {zipc}".strip()


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
        from . import backup
        backup.snapshot(self.db_path)

    def list_deals(self) -> list:
        return self._read().get("deals", [])

    def get_deal(self, deal_id: str) -> Optional[dict]:
        return next((d for d in self.list_deals() if d["id"] == deal_id), None)

    def find_duplicate(self, address: str, exclude_id: str = None) -> Optional[dict]:
        """Return an existing deal whose address matches (loose key)."""
        key = address_dup_key(address)
        if not key:
            return None
        for d in self.list_deals():
            if exclude_id and d.get("id") == exclude_id:
                continue
            if address_dup_key(d.get("address", "")) == key:
                return d
        return None

    def duplicate_groups(self) -> list:
        """Group existing deals by loose address key; return groups of 2+."""
        groups = {}
        for d in self.list_deals():
            key = address_dup_key(d.get("address", ""))
            if key:
                groups.setdefault(key, []).append(
                    {"id": d["id"], "address": d.get("address", "")})
        return [v for v in groups.values() if len(v) > 1]

    def upsert_deal(self, deal: dict) -> dict:
        data = self._read()
        if "id" not in deal or not deal["id"]:
            base = deal.get("address") or "deal"
            # _slugify can return "" for a symbol-only address — never allow a
            # blank id (it would collide and overwrite other blank-id deals).
            deal["id"] = _slugify(base) or "deal"
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
