"""Local photo archive for deals.

Remote listing photos (photos.zillowstatic.com, lh3.googleusercontent.com…)
expire after weeks. This module downloads a deal's gallery to the persistent
data disk and rewrites the deal's image fields to local URLs served by the
/deal-photos static mount — so photos survive forever (deal PDFs,
presentations, and photo-based AI all keep working).
"""
from __future__ import annotations

import io
import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger("flip-board.photo_store")

MAX_PHOTOS = 20
MAX_WIDTH = 1280
JPEG_QUALITY = 80
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
       "Referer": "https://www.zillow.com/"}


def _shrink(data: bytes) -> bytes:
    """Re-encode to a bounded JPEG; return original bytes if PIL chokes."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        if img.width > MAX_WIDTH:
            img = img.resize((MAX_WIDTH, int(img.height * MAX_WIDTH / img.width)))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
        return out.getvalue()
    except Exception:
        return data


def archive_deal_photos(deal: dict, photos_dir: Path) -> bool:
    """Download the deal's remote gallery into photos_dir/<deal_id>/ and point
    image/image_gallery at the local copies. Mutates `deal`; returns True if
    anything changed. Originals are kept in image_gallery_remote."""
    deal_id = deal.get("id")
    gallery = deal.get("image_gallery") or []
    if not deal_id:
        return False
    remote = [u for u in gallery if isinstance(u, str) and u.startswith("http")]
    if not remote:
        return False

    out_dir = Path(photos_dir) / re.sub(r"[^a-zA-Z0-9_-]", "", deal_id)[:80]
    out_dir.mkdir(parents=True, exist_ok=True)
    local_urls, saved = [], 0
    with httpx.Client(timeout=25, follow_redirects=True, headers=_UA) as client:
        for i, url in enumerate(remote[:MAX_PHOTOS]):
            fname = f"{i:02d}.jpg"
            fpath = out_dir / fname
            try:
                if not fpath.exists():
                    r = client.get(url)
                    if r.status_code != 200 or not r.content:
                        continue
                    ctype = r.headers.get("content-type", "")
                    if "image" not in ctype and not url.lower().endswith(
                            (".jpg", ".jpeg", ".png", ".webp")):
                        continue
                    fpath.write_bytes(_shrink(r.content))
                local_urls.append(f"/deal-photos/{out_dir.name}/{fname}")
                saved += 1
            except Exception as e:
                log.warning("photo %s failed for %s: %s", i, deal_id, str(e)[:80])
    if not local_urls:
        return False

    deal["image_gallery_remote"] = remote          # keep originals as reference
    deal["image_gallery"] = local_urls
    deal["image"] = local_urls[0]
    log.info("archived %d/%d photos for %s", saved, len(remote), deal_id)
    return True
