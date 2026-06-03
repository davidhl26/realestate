"""Foreclosure auction module.

Scrapes RealForeclose / RealTaxDeed county auction sites (Florida + others)
to import full lists of upcoming auctions. Each auction item is stored in
a SKIP-TRACE QUEUE pipeline:

  queued → tracing → traced → contacted → won | lost | passed

The user typically:
  1. Imports a daily auction list (one URL = ~100-300 properties)
  2. Filters/triages them ("passed" the obvious junk)
  3. Marks promising ones as "tracing" — runs them through a skip-trace
     service externally to get owner contact info
  4. Once traced, adds owner name/phone/email here → marks "traced"
  5. Calls them → "contacted"
  6. Eventually "won" (got under contract) or "lost"

Supported sites (RealAuction.com family — same vendor):
- *.realforeclose.com (mortgage foreclosure)
- *.realtaxdeed.com  (tax deed)
- *.realtdm.com      (tax deed master)

Storage: data/auctions.json
"""

import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("flip-board.auctions")
_LOCK = threading.Lock()

# Path to credentials JSON (set by server on startup)
_CREDS_PATH: Optional[Path] = None


def set_credentials_path(path: Path):
    global _CREDS_PATH
    _CREDS_PATH = Path(path)


def _read_credentials() -> dict:
    """Per-domain login credentials for RealAuction sites.

    Schema:
      {
        "miamidade.realforeclose.com": {"username": "...", "password": "..."},
        "broward.realforeclose.com": {...},
        ...
      }
    """
    if _CREDS_PATH is None or not _CREDS_PATH.exists():
        return {}
    try:
        with open(_CREDS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_credentials(data: dict):
    if _CREDS_PATH is None:
        raise RuntimeError("Credentials path not initialized")
    _CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_CREDS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_credentials_for_url(url: str) -> Optional[dict]:
    """Find {username, password} matching the URL's hostname (or any subdomain)."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if not host: return None
    creds = _read_credentials()
    # Exact host match
    if host in creds:
        return creds[host]
    # Wildcard / sub-string match (e.g. credentials for "realforeclose.com" cover
    # all county sub-sites that share the same SSO realm — rare but possible)
    for k, v in creds.items():
        if k.lower() in host:
            return v
    return None


def save_credentials(domain: str, username: str, password: str):
    creds = _read_credentials()
    creds[domain.lower().strip()] = {
        "username": username.strip(),
        "password": password,
    }
    _write_credentials(creds)


def list_credential_domains() -> list:
    """Return [{domain, username, has_password}] for the Settings UI."""
    creds = _read_credentials()
    out = []
    for d, v in creds.items():
        out.append({
            "domain": d,
            "username": v.get("username", ""),
            "has_password": bool(v.get("password")),
        })
    return out


def delete_credentials(domain: str) -> bool:
    creds = _read_credentials()
    domain = domain.lower().strip()
    if domain in creds:
        del creds[domain]
        _write_credentials(creds)
        return True
    return False


def _now():
    return datetime.utcnow().isoformat() + "Z"


PIPELINE_STAGES = [
    "queued",     # just imported, not reviewed
    "tracing",    # being skip-traced
    "traced",     # owner info added
    "contacted",  # called/emailed
    "won",        # under contract
    "lost",       # outbid or sale fell through
    "passed",     # decided to skip
]


class AuctionsDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"items": [], "created": _now(), "updated": _now()})

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

    def list_items(self, status: Optional[str] = None,
                    auction_source: Optional[str] = None) -> list:
        items = self._read().get("items", [])
        if status:
            items = [x for x in items if x.get("status") == status]
        if auction_source:
            items = [x for x in items
                      if auction_source in (x.get("source_url") or "")]
        items.sort(key=lambda x: x.get("added_at", ""), reverse=True)
        return items

    def get_item(self, item_id: str) -> Optional[dict]:
        return next((x for x in self._read().get("items", [])
                       if x["id"] == item_id), None)

    def upsert_item(self, item: dict) -> dict:
        data = self._read()
        if not item.get("id"):
            item["id"] = str(uuid.uuid4())[:8]
            item["added_at"] = _now()
        item["updated_at"] = _now()
        idx = next((i for i, x in enumerate(data["items"])
                     if x["id"] == item["id"]), None)
        if idx is None:
            data["items"].append(item)
        else:
            item["added_at"] = data["items"][idx].get("added_at", item["updated_at"])
            data["items"][idx] = item
        data["updated"] = _now()
        self._write(data)
        return item

    def bulk_insert(self, items: list, source_url: str) -> dict:
        """Insert many auction items. Dedupes by case_number or address."""
        data = self._read()
        existing_keys = set()
        for x in data["items"]:
            for k in ("case_number", "parcel_id"):
                if x.get(k): existing_keys.add(f"{k}:{x[k]}")
            if x.get("address"):
                existing_keys.add(f"addr:{x['address'].lower().strip()}")

        added = 0
        skipped = 0
        for it in items:
            keys_for_item = []
            for k in ("case_number", "parcel_id"):
                if it.get(k): keys_for_item.append(f"{k}:{it[k]}")
            if it.get("address"):
                keys_for_item.append(f"addr:{it['address'].lower().strip()}")

            if any(k in existing_keys for k in keys_for_item):
                skipped += 1
                continue

            it["id"] = str(uuid.uuid4())[:8]
            it["added_at"] = _now()
            it["updated_at"] = it["added_at"]
            it["status"] = it.get("status", "queued")
            it["source_url"] = source_url
            data["items"].append(it)
            for k in keys_for_item:
                existing_keys.add(k)
            added += 1

        data["updated"] = _now()
        self._write(data)
        return {"added": added, "skipped": skipped, "total_now": len(data["items"])}

    def delete_item(self, item_id: str) -> bool:
        data = self._read()
        before = len(data["items"])
        data["items"] = [x for x in data["items"] if x["id"] != item_id]
        if len(data["items"]) < before:
            data["updated"] = _now()
            self._write(data)
            return True
        return False

    def bulk_delete(self, status: Optional[str] = None) -> int:
        """Delete all items matching status (or everything if no status)."""
        data = self._read()
        before = len(data["items"])
        if status:
            data["items"] = [x for x in data["items"] if x.get("status") != status]
        else:
            data["items"] = []
        deleted = before - len(data["items"])
        if deleted:
            data["updated"] = _now()
            self._write(data)
        return deleted

    def aggregates(self) -> dict:
        items = self._read().get("items", [])
        by_status = {s: 0 for s in PIPELINE_STAGES}
        for it in items:
            s = it.get("status", "queued")
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "total": len(items),
            "by_status": by_status,
            "pipeline": PIPELINE_STAGES,
        }


# ============================================================================
# SCRAPER for RealAuction-family sites (realforeclose / realtaxdeed / realtdm)
# ============================================================================

def _run_in_clean_thread(fn, *args, **kwargs):
    """Mirror scraper_browser's thread wrapper for Playwright safety in FastAPI."""
    result_q = queue.Queue()
    def runner():
        try:
            result_q.put(("ok", fn(*args, **kwargs)))
        except Exception as e:
            result_q.put(("err", e))
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    kind, payload = result_q.get()
    if kind == "err":
        raise payload
    return payload


def detect_auction_site(url: str) -> Optional[str]:
    u = url.lower()
    if "realforeclose.com" in u:
        return "realforeclose"
    if "realtaxdeed.com" in u or "realtdm.com" in u:
        return "realtaxdeed"
    return None


def scrape_auction_list(url: str) -> dict:
    """Public wrapper — runs Playwright in a clean thread."""
    return _run_in_clean_thread(_scrape_auction_list_impl, url)


def scrape_single_auction(url: str) -> dict:
    """Scrape ONE auction detail page (PREVIEW URL with AID)."""
    return _run_in_clean_thread(_scrape_single_auction_impl, url)


def _enter_realauction_session(page, original_url: str) -> bool:
    """RealAuction sites (miamidade.realforeclose.com, etc) gate their auction
    lists behind a splash + login page. We support two entry paths:

      A) AUTHENTICATED (preferred — when credentials are saved):
         1. Navigate to /index.cfm (splash) which has the login form
         2. Fill #LogName + #LogPass, click #LogButton
         3. Dismiss the #BNOTACC "Notice and alert" page
         4. Navigate to the target URL (DAYLIST works fully — 30+ items/page,
            real AID links you can drill into)

      B) UNAUTHENTICATED (public preview):
         1. Splash → click #splashMenuBottom ("AUCTION CALENDAR")
         2. JS-navigate (not page.goto) to URL with Zmethod rewritten DAYLIST→PREVIEW
         3. PREVIEW shows the same items but capped at 20/page and no AID hrefs

    Returns True if we landed on a page with auction content.
    """
    from urllib.parse import urlparse

    parsed = urlparse(original_url)
    root = f"{parsed.scheme}://{parsed.netloc}/index.cfm"

    creds = get_credentials_for_url(original_url)

    # ============== AUTHENTICATED PATH ==============
    if creds and creds.get("username") and creds.get("password"):
        log.info("Realauction: logging in as %s on %s",
                  creds["username"], parsed.netloc)
        try:
            page.goto(root, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
            # Fill login on splash form
            page.locator("#LogName").fill(creds["username"], timeout=8000)
            page.locator("#LogPass").fill(creds["password"], timeout=8000)
            page.locator("#LogButton").click(timeout=5000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(4000)

            # Check for bad-login indicator
            bad = page.evaluate(
                "() => document.getElementById('BadText')?.style?.display"
            )
            if bad and bad != "none":
                log.warning("Login rejected — falling back to public preview")
                creds = None  # force unauth path below
            else:
                # Dismiss "Notice and alert" page if shown
                try:
                    page.locator("#BNOTACC").click(timeout=4000)
                    page.wait_for_timeout(2500)
                    log.info("Dismissed notice page")
                except Exception:
                    pass  # No notice page this session

                # Now navigate to target — DAYLIST works fully when authenticated
                log.info("Realauction (auth): going to %s", original_url)
                try:
                    page.goto(original_url, wait_until="domcontentloaded",
                              timeout=25000)
                    page.wait_for_timeout(4500)
                except Exception as e:
                    log.warning("Target nav failed after login: %s", e)

                on_splash = "splash" in (page.title() or "").lower()
                log.info("Realauction (auth): at %s | title=%r | onSplash=%s",
                          page.url, page.title(), on_splash)
                return not on_splash
        except Exception as e:
            log.warning("Auth flow exception, falling back to public: %s", e)
            creds = None

    # ============== UNAUTHENTICATED PATH ==============
    # Rewrite DAYLIST → PREVIEW (DAYLIST is gated, PREVIEW isn't for public)
    target = re.sub(r"Zmethod=DAYLIST", "Zmethod=PREVIEW",
                     original_url, flags=re.IGNORECASE)

    log.info("Realauction (public): hitting splash %s", root)
    page.goto(root, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    # Click AUCTION CALENDAR to establish public session
    try:
        page.locator("#splashMenuBottom").first.click(timeout=5000)
        log.info("Clicked #splashMenuBottom (AUCTION CALENDAR)")
    except Exception:
        try:
            page.locator("text=AUCTION CALENDAR").first.click(timeout=5000)
        except Exception as e:
            log.warning("Could not click AUCTION CALENDAR: %s", e)
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2500)

    # JS-navigate to target (page.goto bounces back to splash)
    if target != page.url:
        log.info("Realauction (public): JS-navigating to %s", target)
        try:
            page.evaluate(f"() => {{ window.location.href = {json.dumps(target)}; }}")
            page.wait_for_load_state("domcontentloaded", timeout=20000)
        except Exception as e:
            log.warning("JS nav failed: %s", e)
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=20000)
            except Exception:
                return False

    page.wait_for_timeout(4000)
    on_splash = "splash" in (page.title() or "").lower()
    log.info("Realauction (public): at %s | title=%r | onSplash=%s",
              page.url, page.title(), on_splash)
    return not on_splash


def _scrape_single_auction_impl(url: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "Playwright not installed"}

    item = {}
    raw_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True,
            args=["--no-first-run", "--no-default-browser-check"])
        try:
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0.0.0 Safari/537.36"),
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            # For RealAuction-family sites, go through splash → calendar first
            if detect_auction_site(url):
                _enter_realauction_session(page, url)
            else:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try: page.wait_for_load_state("networkidle", timeout=15000)
            except Exception: pass
            page.wait_for_timeout(2500)

            try:
                raw_text = page.evaluate("() => document.body.innerText.slice(0, 30000)")
            except Exception: pass
            # Extract labelled key-value pairs from the page
            item = _normalize_auction_item({"_raw_text": raw_text})
            item["source_url"] = url
            # Try to also pull AID from URL
            m = re.search(r"AID=(\d+)", url, re.I)
            if m: item["aid"] = m.group(1)
        finally:
            browser.close()

    if not item.get("address") and not item.get("case_number"):
        return {
            "ok": False,
            "error": "Could not extract any auction info from this URL.",
            "raw_text_excerpt": raw_text[:1500] if raw_text else None,
        }
    return {"ok": True, "item": item}


def _scrape_auction_list_impl(url: str) -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "Playwright not installed"}

    site = detect_auction_site(url)
    if not site:
        return {"ok": False, "error": "Unsupported site"}

    items = []
    raw_payloads = []
    debug_text = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0.0.0 Safari/537.36"),
                viewport={"width": 1366, "height": 900},
            )
            page = context.new_page()

            # Capture all JSON-ish API responses
            def on_response(resp):
                try:
                    u = resp.url
                    if resp.status >= 400: return
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ct and "javascript" not in ct: return
                    raw_payloads.append({"url": u, "text": resp.text()[:30000]})
                except Exception:
                    pass

            page.on("response", on_response)

            log.info("Navigating to auction list: %s", url)
            # RealAuction sites need splash → calendar → JS-nav flow.
            # Direct goto bounces back to splash.
            if site in ("realforeclose", "realtaxdeed"):
                _enter_realauction_session(page, url)
            else:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)

            # Wait for auction items to render
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3500)

            # Walk through paginated auction items.
            # RealAuction PageFrame[area=C] is the "current day" rotation,
            # PageRight is the next-page click target. We loop until either
            # the page counter stops advancing or we hit the max page.
            seen_first_text = None
            page_no = 1
            max_pages = 50  # hard cap
            while page_no <= max_pages:
                # Extract items on this page
                page_items = _extract_realauction_dom(page)
                # Filter junk
                kept = [it for it in page_items
                         if it.get("case_number") or it.get("parcel_id")
                            or it.get("address")]
                log.info("Page %d: %d items extracted (%d after filter)",
                          page_no, len(page_items), len(kept))
                items.extend(kept)

                if site != "realforeclose" and site != "realtaxdeed":
                    break

                # Detect last page
                try:
                    max_p = page.evaluate(
                        "() => parseInt(document.getElementById('maxCA')?.textContent || '1', 10)"
                    )
                    cur_p = page.evaluate(
                        "() => parseInt(document.getElementById('curPCA')?.value || '1', 10)"
                    )
                except Exception:
                    max_p, cur_p = 1, 1

                log.info("Pagination: cur=%s max=%s", cur_p, max_p)
                if cur_p >= max_p:
                    break

                # Click PageRight in the C-area (current day)
                clicked = False
                try:
                    clicked = page.evaluate("""() => {
                        const rights = document.querySelectorAll('.PageRight');
                        for (const r of rights) {
                            if (r.closest('.PageFrame[area="C"]')) {
                                r.click(); return true;
                            }
                        }
                        return false;
                    }""")
                except Exception:
                    pass
                if not clicked:
                    log.info("Could not click next page")
                    break

                page.wait_for_timeout(2500)
                # Verify the first item changed (avoid infinite loop)
                try:
                    first_now = page.evaluate(
                        "() => document.querySelector('[class*=AUCTION_ITEM]')?.innerText?.slice(0,200) || ''"
                    )
                    if first_now == seen_first_text:
                        log.info("First item didn't change — assuming end")
                        break
                    seen_first_text = first_now
                except Exception:
                    pass

                page_no += 1

            try:
                debug_text = page.evaluate("() => document.body.innerText.slice(0, 40000)")
            except Exception:
                pass

            log.info("Total items scraped across pages: %d", len(items))

        finally:
            browser.close()

    # If DOM extraction failed, try regex over the rendered text
    if not items and debug_text:
        items = _extract_realauction_text(debug_text)

    if not items:
        return {
            "ok": False,
            "error": "Could not parse any auction items. Site may have changed format.",
            "raw_text_excerpt": debug_text[:2000] if debug_text else None,
            "api_payloads_count": len(raw_payloads),
        }

    return {
        "ok": True,
        "source_url": url,
        "site": site,
        "items": items,
        "count": len(items),
    }


def _extract_realauction_dom(page) -> list:
    """Extract auction items by scraping the rendered DOM.

    RealAuction sites use a consistent structure: each auction is a div with
    class containing 'AUCTION_ITEM' or rendered in a table with rows.
    """
    try:
        js = """
        () => {
            const items = [];
            // Strategy 1: AUCTION_ITEM divs (RealAuction standard)
            document.querySelectorAll('[class*="AUCTION_ITEM"]').forEach(el => {
                const text = el.innerText || '';
                const data = { _raw_text: text.slice(0, 2000) };
                // Try to find labelled fields inside
                el.querySelectorAll('.AUCTION_STATS, .AUCTION_DETAILS, [class*="STATUS"], dt, dd, span, strong, label').forEach(s => {
                    const t = (s.innerText || '').trim();
                    if (!t) return;
                    if (t.length < 80) data._meta = (data._meta || []).concat([t]);
                });
                items.push(data);
            });
            // Strategy 2: Tables with "case" or "auction" in headers
            if (!items.length) {
                document.querySelectorAll('table').forEach(table => {
                    const headers = Array.from(table.querySelectorAll('thead th, tr:first-child th, tr:first-child td')).map(h => (h.innerText || '').toLowerCase());
                    const hasAuctionCols = headers.some(h => /case|auction|bid|parcel|address/.test(h));
                    if (!hasAuctionCols) return;
                    const rows = table.querySelectorAll('tbody tr, tr');
                    rows.forEach((row, i) => {
                        if (i === 0 && row.querySelectorAll('th').length > 0) return;
                        const cells = Array.from(row.querySelectorAll('td')).map(c => (c.innerText || '').trim());
                        if (cells.length < 2) return;
                        const item = {};
                        cells.forEach((c, idx) => {
                            const hdr = headers[idx] || `col${idx}`;
                            if (c) item[hdr] = c;
                        });
                        items.push(item);
                    });
                });
            }
            return items;
        }
        """
        raw_items = page.evaluate(js) or []
        log.info("DOM extraction got %d raw items", len(raw_items))
        return [_normalize_auction_item(it) for it in raw_items if it]
    except Exception as e:
        log.warning("DOM extraction failed: %s", e)
        return []


def _normalize_auction_item(raw: dict) -> dict:
    """Normalize a raw extracted dict into our schema.

    RealForeclose AUCTION_ITEM cards have text like:
      "Auction StatusCanceled per Bankruptcy
       Auction Type:FORECLOSURE
       Case #: 2013-020140-CA-01
       Final Judgment Amount:$463,976.11
       Parcel ID: 04-2036-007-1410
       Property Address:779 W 64 DR
       HIALEAH, FL- 33012
       Assessed Value: …"

    Note: labels run together with values (no space after colon) and lines
    may or may not have line breaks. We parse labelled chunks robustly.
    """
    out = {}

    # Gather all text we have to extract from
    text_pool = []
    if raw.get("_raw_text"):
        text_pool.append(raw["_raw_text"])
    if raw.get("_meta"):
        text_pool.extend(raw["_meta"])
    for k, v in raw.items():
        if isinstance(v, str) and not k.startswith("_"):
            text_pool.append(f"{k}: {v}")

    full_text = " | ".join(text_pool)
    # Normalize whitespace
    norm = re.sub(r"\s+", " ", full_text)

    # ---- Case # ----
    # "Case #: 2013-020140-CA-01" or "Case#:..."
    m = re.search(r"Case\s*#?[:\s]*([0-9A-Z\-]{6,30})", norm, re.I)
    if m: out["case_number"] = m.group(1).strip()
    # Fallback to standard FL court case pattern
    if not out.get("case_number"):
        m = re.search(r"\b((?:19|20)\d{2}-?\s*\d{4,7}-?\s*[A-Z]{2,4}-?\s*\d{1,3})\b", norm)
        if m: out["case_number"] = m.group(1).replace(" ", "")

    # ---- Parcel / folio ----
    m = re.search(r"Parcel\s*(?:ID|#|number)?[:\s]*([0-9\-]{8,30})", norm, re.I)
    if m: out["parcel_id"] = m.group(1).strip()
    if not out.get("parcel_id"):
        m = re.search(r"(?:folio)[:\s]*([0-9\-]{8,30})", norm, re.I)
        if m: out["parcel_id"] = m.group(1)

    # ---- Property Address ----
    m = re.search(
        r"Property\s*Address[:\s]*([^|]+?)(?=\s*(?:Assessed|Plaintiff|Defendant|Comments|Final|Auction|Case\s*#|Parcel|$))",
        norm, re.I)
    if m:
        addr = m.group(1).strip().rstrip(",")
        # Often the format is "STREET CITY, ST- ZIP" — clean dash
        addr = re.sub(r"\s*FL\s*-\s*(\d{5})", r", FL \1", addr)
        addr = re.sub(r"\s*,\s*", ", ", addr)
        out["address"] = addr.strip()
    else:
        # Fallback regex
        m = re.search(
            r"(\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Z0-9][A-Z0-9\s\.]{2,40}?"
            r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|"
            r"Ct|Court|Pl|Place|Way|Pkwy|Cir|Circle|Ter|Terrace|Hwy|Path|Pt|Pky)\.?"
            r"(?:\s+(?:Apt|Unit|#)\s*[\dA-Z]+)?"
            r"(?:\s*,?\s*[A-Z][A-Z\s]+)?"
            r"(?:\s*,?\s*[A-Z]{2})?"
            r"(?:\s*-?\s*\d{5}(?:-\d{4})?)?)",
            norm, re.I)
        if m: out["address"] = re.sub(r"\s+", " ", m.group(1)).strip()

    # ---- Auction Type ----
    m = re.search(r"Auction\s*Type[:\s]*([A-Za-z\s]+?)(?=\s*(?:Case|Final|Parcel|$))",
                   norm, re.I)
    if m:
        t = m.group(1).strip().lower()
        if "foreclos" in t or "mortgage" in t:
            out["auction_type"] = "mortgage_foreclosure"
        elif "tax" in t:
            out["auction_type"] = "tax_deed"
        else:
            out["auction_type"] = t
    else:
        if re.search(r"\bforeclosure\b", norm, re.I):
            out["auction_type"] = "mortgage_foreclosure"
        elif re.search(r"\btax\s*deed\b", norm, re.I):
            out["auction_type"] = "tax_deed"

    # ---- Final Judgment Amount / Opening Bid ----
    m = re.search(r"Final\s*Judgment\s*Amount[:\s$]*([0-9,]+(?:\.\d{2})?)",
                   norm, re.I)
    if m:
        try: out["final_judgment"] = int(float(m.group(1).replace(",", "")))
        except: pass
    m = re.search(r"(?:Opening|Minimum|Starting)\s*Bid[:\s$]*([0-9,]+(?:\.\d{2})?)",
                   norm, re.I)
    if m:
        try: out["opening_bid"] = int(float(m.group(1).replace(",", "")))
        except: pass
    # If no explicit bid, use judgment amount as the opening bid proxy
    if not out.get("opening_bid") and out.get("final_judgment"):
        out["opening_bid"] = out["final_judgment"]

    # ---- Auction Status (Sold / Canceled / Active / Postponed) ----
    m = re.search(r"Auction\s*(?:Status|Sold)\s*[:\s]*([A-Za-z][\w\s/]+?)(?=\s*(?:Auction\s*Type|Amount|\$|Case|Final|Parcel|\d{2}/\d{2}|$))",
                   norm, re.I)
    if m:
        status_str = m.group(1).strip()
        out["auction_status"] = status_str[:50]
        sl = status_str.lower()
        if "cancel" in sl: out["status_short"] = "canceled"
        elif "sold" in sl: out["status_short"] = "sold"
        elif "postponed" in sl: out["status_short"] = "postponed"
        elif "withdrawn" in sl: out["status_short"] = "withdrawn"
        else: out["status_short"] = "active"
    else:
        for st in ("sold", "canceled", "cancelled", "postponed", "withdrawn"):
            if re.search(rf"\b{st}\b", norm, re.I):
                out["auction_status"] = st.title()
                out["status_short"] = st.replace("ll", "l").lower()
                break

    # ---- Amount sold (for completed auctions) ----
    m = re.search(r"Amount\s*\$\s*([0-9,]+(?:\.\d{2})?)", norm, re.I)
    if m:
        try: out["sold_amount"] = int(float(m.group(1).replace(",", "")))
        except: pass

    # ---- Sold To ----
    m = re.search(r"Sold\s*To\s*([A-Za-z0-9 &.,\-]{2,60})", norm, re.I)
    if m: out["sold_to"] = m.group(1).strip()

    # ---- Assessed Value ----
    m = re.search(r"Assessed\s*Value[:\s$]*([0-9,]+(?:\.\d{2})?)", norm, re.I)
    if m:
        try: out["assessed_value"] = int(float(m.group(1).replace(",", "")))
        except: pass

    # ---- Plaintiff / Defendant ----
    m = re.search(r"Plaintiff[:\s]*([^|]+?)(?=\s*(?:Defendant|Property|Case|$))",
                   norm, re.I)
    if m: out["plaintiff"] = m.group(1).strip()[:120]
    m = re.search(r"Defendant[:\s]*([^|]+?)(?=\s*(?:Plaintiff|Property|Case|Comments|$))",
                   norm, re.I)
    if m:
        # Defendant is often the owner — useful for skip-tracing
        defendant = m.group(1).strip()[:120]
        out["defendant"] = defendant
        if not out.get("owner_name"):
            out["owner_name"] = defendant

    # ---- Auction date / time ----
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{2,4})", norm)
    if m: out["auction_date"] = m.group(1)
    m = re.search(r"(\d{1,2}:\d{2}\s*[AP]M(?:\s*ET)?)", norm, re.I)
    if m: out["auction_time"] = m.group(1)

    out["raw_excerpt"] = norm[:800]
    return out


def _extract_realauction_text(text: str) -> list:
    """Fallback: extract items from raw page text using regex patterns."""
    # RealAuction text typically renders item blocks delimited by case numbers.
    # Each block has Case#, Address, Bid, Time, Status.
    items = []
    # Split on case-number-looking boundaries
    blocks = re.split(r"(?=Case\s*(?:Number|#))", text, flags=re.I)
    for block in blocks[1:]:  # skip preamble
        item = _normalize_auction_item({"_raw_text": block[:2000]})
        if item.get("case_number") or item.get("address"):
            items.append(item)
    return items
