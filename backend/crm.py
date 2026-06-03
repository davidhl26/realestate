"""CRM — contacts + interactions storage.

Stores in data/crm.json with structure:
{
  "contacts": [
    {"id": "...", "name": "...", "role": "seller|agent|contractor|lender|title|other",
     "phone": "...", "email": "...", "company": "...", "notes": "...",
     "deal_ids": ["..."], "created_at": "...", "updated_at": "..."}
  ],
  "interactions": [
    {"id": "...", "deal_id": "...", "contact_id": "...",
     "type": "call|email|meeting|sms|note|offer|other",
     "subject": "...", "body": "...", "date": "...", "follow_up": "..."}
  ]
}
"""

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

_LOCK = threading.Lock()


def _now():
    return datetime.utcnow().isoformat() + "Z"


class CrmDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"contacts": [], "interactions": [],
                          "created": _now(), "updated": _now()})

    def _read(self) -> dict:
        with _LOCK:
            with open(self.path, "r") as f:
                return json.load(f)

    def _write(self, data: dict):
        with _LOCK:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self.path)

    # ---------- Contacts ----------
    def list_contacts(self, deal_id: Optional[str] = None) -> list:
        data = self._read()
        cs = data.get("contacts", [])
        if deal_id:
            cs = [c for c in cs if deal_id in (c.get("deal_ids") or [])]
        return cs

    def get_contact(self, contact_id: str) -> Optional[dict]:
        return next((c for c in self._read().get("contacts", [])
                       if c["id"] == contact_id), None)

    def upsert_contact(self, contact: dict) -> dict:
        data = self._read()
        if not contact.get("id"):
            contact["id"] = str(uuid.uuid4())[:8]
            contact["created_at"] = _now()
        contact["updated_at"] = _now()
        idx = next((i for i, c in enumerate(data["contacts"])
                     if c["id"] == contact["id"]), None)
        if idx is None:
            data["contacts"].append(contact)
        else:
            contact["created_at"] = data["contacts"][idx].get("created_at",
                                                                contact["updated_at"])
            data["contacts"][idx] = contact
        data["updated"] = _now()
        self._write(data)
        return contact

    def delete_contact(self, contact_id: str) -> bool:
        data = self._read()
        before = len(data["contacts"])
        data["contacts"] = [c for c in data["contacts"] if c["id"] != contact_id]
        # Also remove related interactions
        data["interactions"] = [i for i in data.get("interactions", [])
                                  if i.get("contact_id") != contact_id]
        if len(data["contacts"]) < before:
            data["updated"] = _now()
            self._write(data)
            return True
        return False

    # ---------- Interactions ----------
    def list_interactions(self, deal_id: Optional[str] = None,
                           contact_id: Optional[str] = None) -> list:
        data = self._read()
        items = data.get("interactions", [])
        if deal_id:
            items = [i for i in items if i.get("deal_id") == deal_id]
        if contact_id:
            items = [i for i in items if i.get("contact_id") == contact_id]
        # Sort newest first
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        return items

    def get_interaction(self, interaction_id: str) -> Optional[dict]:
        return next((i for i in self._read().get("interactions", [])
                       if i["id"] == interaction_id), None)

    def upsert_interaction(self, interaction: dict) -> dict:
        data = self._read()
        if not interaction.get("id"):
            interaction["id"] = str(uuid.uuid4())[:8]
        if not interaction.get("date"):
            interaction["date"] = _now()
        interaction["updated_at"] = _now()
        idx = next((i for i, x in enumerate(data["interactions"])
                     if x["id"] == interaction["id"]), None)
        if idx is None:
            data["interactions"].append(interaction)
        else:
            data["interactions"][idx] = interaction
        data["updated"] = _now()
        self._write(data)
        return interaction

    def delete_interaction(self, interaction_id: str) -> bool:
        data = self._read()
        before = len(data["interactions"])
        data["interactions"] = [i for i in data["interactions"]
                                  if i["id"] != interaction_id]
        if len(data["interactions"]) < before:
            data["updated"] = _now()
            self._write(data)
            return True
        return False

    def aggregates(self) -> dict:
        data = self._read()
        by_role = {}
        for c in data.get("contacts", []):
            r = c.get("role", "other")
            by_role[r] = by_role.get(r, 0) + 1
        by_type = {}
        for i in data.get("interactions", []):
            t = i.get("type", "other")
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "contacts_count": len(data.get("contacts", [])),
            "interactions_count": len(data.get("interactions", [])),
            "by_role": by_role,
            "by_type": by_type,
        }
