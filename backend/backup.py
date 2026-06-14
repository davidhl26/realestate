"""Lightweight point-in-time backups for the JSON data files.

Each time a data file is written, we drop a timestamped copy into a
`backups/` folder next to it and keep the newest N. This gives cheap
recovery points (deals/leads) without a database migration. Backups live
on the same persistent disk as the data, so they survive deploys.
"""

import logging
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("flip-board.backup")

KEEP = 40  # snapshots retained per file


def snapshot(path, keep: int = KEEP):
    """Copy `path` into ./backups/<stem>.<UTC-timestamp><suffix>, prune to N.
    Never raises — a backup failure must not break the save."""
    try:
        path = Path(path)
        if not path.exists():
            return
        bdir = path.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S_%f")
        dest = bdir / f"{path.stem}.{ts}{path.suffix}"
        shutil.copy2(path, dest)
        # Prune oldest, keeping the newest `keep` for this file's stem.
        snaps = sorted(bdir.glob(f"{path.stem}.*{path.suffix}"))
        for old in snaps[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception as e:  # pragma: no cover - best effort
        log.warning("snapshot failed for %s: %s", path, e)


def list_snapshots(data_dir) -> list:
    """Return metadata for available snapshots (newest first)."""
    bdir = Path(data_dir) / "backups"
    if not bdir.exists():
        return []
    out = []
    for f in sorted(bdir.glob("*.json"), reverse=True):
        try:
            st = f.stat()
            out.append({"name": f.name, "size": st.st_size,
                        "modified": datetime.utcfromtimestamp(st.st_mtime).isoformat() + "Z"})
        except OSError:
            pass
    return out
