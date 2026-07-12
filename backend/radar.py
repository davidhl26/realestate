"""Deal Radar — stores the "interesting" finds surfaced by the Zillow watches.

A find is a fresh listing that passed the auto-filter (target margin, min
profit, 70% rule, low risk). Finds power the in-app Radar feed + unseen badge.
Lightweight JSON store, same pattern as the other *DB classes.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _key(address: str) -> str:
    return (address or "").strip().lower()


class RadarDB:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = RLock()
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write({"finds": []})

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {"finds": []}

    def _write(self, data: dict):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def list_finds(self, limit: int = 200) -> list:
        with self._lock:
            finds = list(self._read().get("finds", []))
        finds.sort(key=lambda f: f.get("created_at", ""), reverse=True)
        return finds[:limit]

    def unseen_count(self) -> int:
        with self._lock:
            return sum(1 for f in self._read().get("finds", []) if not f.get("seen"))

    def has_address(self, address: str) -> bool:
        k = _key(address)
        with self._lock:
            return any(_key(f.get("address")) == k for f in self._read().get("finds", []))

    def has_zpid(self, zpid) -> bool:
        """True if a find with this Zillow property id is already on the radar.
        zpid dedup is exact — immune to address-format drift ("123 Main St" vs
        "123 Main Street, Cleveland, OH")."""
        z = str(zpid or "").strip()
        if not z:
            return False
        with self._lock:
            return any(str(f.get("zpid") or "").strip() == z
                       for f in self._read().get("finds", []))

    def add_find(self, find: dict):
        """Insert a find (deduped by zpid when present, else address). Returns
        the stored find, or None if that home is already on the radar."""
        with self._lock:
            data = self._read()
            z = str(find.get("zpid") or "").strip()
            k = _key(find.get("address"))
            for f in data["finds"]:
                if z and str(f.get("zpid") or "").strip() == z:
                    return None
                if k and _key(f.get("address")) == k:
                    return None
            find = dict(find)
            find.setdefault("id", "r" + _now().replace(":", "").replace("-", "").replace(".", "")[:20]
                             + "-" + str(len(data["finds"])))
            find.setdefault("created_at", _now())
            find.setdefault("seen", False)
            data["finds"].insert(0, find)
            data["finds"] = data["finds"][:500]
            self._write(data)
            return find

    def mark_all_seen(self):
        with self._lock:
            data = self._read()
            for f in data["finds"]:
                f["seen"] = True
            self._write(data)
        return True

    def delete(self, find_id: str) -> bool:
        with self._lock:
            data = self._read()
            before = len(data["finds"])
            data["finds"] = [f for f in data["finds"] if f.get("id") != find_id]
            if len(data["finds"]) < before:
                self._write(data)
                return True
        return False
