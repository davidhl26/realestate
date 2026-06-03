"""Browser-based scraper for JS-rendered authenticated sites.

Strategy:
1. Use a Playwright PERSISTENT context (cookies saved to disk across runs)
2. Try cookies from the user's installed Chrome too (browser_cookie3)
3. If no session, launch a VISIBLE Chromium window so the user can log in
   once. After login, the persistent context remembers it forever.
4. On subsequent scrapes: headless, transparent.

This handles ispeedtolead and other auth-protected SPAs without the user
having to copy cookies manually.
"""

import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


def _run_in_clean_thread(fn, *args, **kwargs):
    """Run a function in a fresh thread.

    Why: FastAPI runs sync endpoints in a thread pool managed by anyio,
    which can leave an asyncio loop reference attached. Playwright's
    sync_playwright() detects this and refuses to start. A brand-new
    thread has no asyncio context, so Playwright works correctly.
    """
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

log = logging.getLogger("flip-board.scraper_browser")

# Persistent Chromium profile lives here. Set by server on startup.
_PROFILE_DIR: Optional[Path] = None


def set_profile_dir(path: Path):
    global _PROFILE_DIR
    _PROFILE_DIR = Path(path)
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def _profile_dir() -> Path:
    if _PROFILE_DIR is None:
        set_profile_dir(Path.home() / ".flip-board-playwright-profile")
    return _PROFILE_DIR


def open_authenticated_session(login_url=None, domain_marker="ispeedtolead",
                                  success_url_contains="/my-leads",
                                  timeout_ms=300000) -> dict:
    return _run_in_clean_thread(_open_authenticated_session_impl,
        login_url or "https://app.ispeedtolead.com/auth/login",
        domain_marker, success_url_contains, timeout_ms)


def _open_authenticated_session_impl(
    login_url: str = "https://app.ispeedtolead.com/auth/login",
    domain_marker: str = "ispeedtolead",
    success_url_contains: str = "/my-leads",
    timeout_ms: int = 300000,  # 5 min for user to log in
) -> dict:
    """Open a visible Chromium so the user can sign in.

    After the user logs in (detected by navigation to a post-login URL),
    we close the window. The persistent profile keeps the session for
    future headless scrapes.

    Returns {ok: bool, message: str}.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "Playwright not installed"}

    profile = _profile_dir()
    session_marker = profile / ".session-established"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 820},
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url, timeout=30000)
            # Wait until URL changes to a post-login state OR window closes
            try:
                page.wait_for_url(
                    lambda u: success_url_contains in u and "/auth/" not in u.lower(),
                    timeout=timeout_ms,
                )
                session_marker.touch()
                return {"ok": True, "message": "Signed in successfully — session saved."}
            except Exception as e:
                # User may have closed window without logging in
                return {"ok": False, "error": f"Login timed out or window closed: {e}"}
        finally:
            try:
                context.close()
            except Exception:
                pass


def session_status(domain: str = "ispeedtolead") -> dict:
    """Quick check of the persistent profile state."""
    profile = _profile_dir()
    marker = profile / ".session-established"
    size_mb = 0
    if profile.exists():
        try:
            size_mb = sum(p.stat().st_size for p in profile.rglob("*")
                           if p.is_file()) / 1024 / 1024
        except Exception:
            pass
    return {
        "profile_exists": profile.exists(),
        "session_established": marker.exists(),
        "profile_size_mb": round(size_mb, 1),
    }


# Browsers to try, in priority order. browser_cookie3 handles keychain
# decryption transparently on macOS.
_BROWSER_LOADERS = ["chrome", "edge", "brave", "firefox", "safari"]


def load_cookies_for_domain(domain: str) -> list:
    """Auto-extract cookies from installed browsers for a given domain.

    Returns a list of cookie dicts in Playwright format. Empty list if
    nothing found.
    """
    try:
        import browser_cookie3
    except ImportError:
        log.warning("browser_cookie3 not installed")
        return []

    base_domain = domain.lstrip("www.")
    # Try multiple variants (root domain + subdomains)
    domain_variants = [base_domain]
    parts = base_domain.split(".")
    if len(parts) >= 2:
        domain_variants.append("." + ".".join(parts[-2:]))

    all_cookies = []
    seen = set()

    for browser_name in _BROWSER_LOADERS:
        loader = getattr(browser_cookie3, browser_name, None)
        if loader is None:
            continue
        for d in domain_variants:
            try:
                cj = loader(domain_name=d)
                for c in cj:
                    key = (c.name, c.domain, c.path)
                    if key in seen:
                        continue
                    seen.add(key)
                    all_cookies.append({
                        "name": c.name,
                        "value": c.value,
                        "domain": c.domain,
                        "path": c.path or "/",
                        "expires": int(c.expires) if c.expires else -1,
                        "httpOnly": bool(getattr(c, "_rest", {}).get("HttpOnly", False)),
                        "secure": bool(c.secure),
                        "sameSite": "Lax",
                    })
                if all_cookies:
                    log.info("Loaded %d cookies from %s for %s",
                              len(all_cookies), browser_name, d)
                    break
            except Exception as e:
                log.debug("Could not load %s cookies for %s: %s",
                           browser_name, d, e)
        if all_cookies:
            break
    return all_cookies


def scrape_ispeedtolead_browser(url, timeout_ms=30000, force_headful=False):
    return _run_in_clean_thread(_scrape_ispeedtolead_browser_impl,
        url, timeout_ms, force_headful)


def _scrape_ispeedtolead_browser_impl(url: str, timeout_ms: int = 30000,
                                  force_headful: bool = False) -> dict:
    """Scrape ispeedtolead using persistent Playwright profile.

    First-run UX: if no session is stored in the profile AND no cookies are
    found in Chrome, opens a VISIBLE Chromium window so the user can log in
    once. After login, the session is saved to disk and all future scrapes
    are headless and silent.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"error": "Playwright not installed", "url": url,
                "requires_manual_entry": True}

    # Extract property ID from URL
    m = re.search(r"/property/(\d+)", url)
    prop_id = m.group(1) if m else None

    profile = _profile_dir()
    session_marker = profile / ".session-established"

    # Detect whether we already have a session for this site
    has_session = session_marker.exists()

    # Also try to seed cookies from the user's Chrome (one-shot import)
    if not has_session:
        chrome_cookies = (load_cookies_for_domain("ispeedtolead.com") or
                            load_cookies_for_domain("app.ispeedtolead.com") or
                            load_cookies_for_domain("be.ispeedtolead.com"))
    else:
        chrome_cookies = []

    headless = has_session and not force_headful

    api_payloads = []
    captured_data = {}
    login_required = False

    with sync_playwright() as p:
        # Persistent context = saves cookies + localStorage to disk
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            if chrome_cookies:
                try:
                    context.add_cookies(chrome_cookies)
                    log.info("Seeded %d cookies from system Chrome",
                              len(chrome_cookies))
                except Exception as e:
                    log.warning("Could not seed Chrome cookies: %s", e)

            page = context.pages[0] if context.pages else context.new_page()

            # Intercept all API responses to find the property data
            def on_response(resp):
                try:
                    u = resp.url
                    if "/api/" not in u or resp.status >= 400:
                        return
                    if prop_id and prop_id not in u:
                        return
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ct:
                        return
                    body = resp.json()
                    api_payloads.append({"url": u, "data": body})
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e:
                log.warning("Initial goto error: %s", e)

            # Wait for Angular to render and API calls to complete
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)

            # Check if we landed on a login page (redirect or login form)
            current_url = page.url
            if any(t in current_url.lower() for t in ["login", "signin", "auth"]):
                login_required = True
                log.warning("Redirected to login: %s", current_url)
            else:
                # Try to detect login form on the current page
                try:
                    has_login = page.evaluate("""() => {
                        return !!(document.querySelector('input[type=password]') ||
                                  document.querySelector('input[type=email]'));
                    }""")
                    if has_login and not api_payloads:
                        login_required = True
                except Exception:
                    pass

            # If session is valid (we got data or stayed on the property page),
            # mark session as established
            if not login_required and (api_payloads or "/property/" in current_url):
                session_marker.touch()

            # Best-effort: extract text from the rendered DOM as a fallback
            try:
                captured_data["page_text"] = page.evaluate(
                    "() => document.body && document.body.innerText "
                    "    ? document.body.innerText.slice(0, 8000) : ''")
                captured_data["page_title"] = page.title()
                captured_data["final_url"] = current_url
            except Exception:
                pass

            try:
                captured_data["rendered_html_length"] = len(page.content())
            except Exception:
                pass

            # If login was required AND we ran headless, retry headful so user
            # can log in. (Only do this once per request to avoid loops.)
            if login_required and headless and not force_headful:
                log.info("Login required — will retry in headful mode")
                context.close()
                return _run_in_clean_thread(_scrape_ispeedtolead_browser_impl,
                    url, timeout_ms * 2, True)

            # If headful mode and login required, wait for user to authenticate
            # (max 3 min, or until they navigate back to the property page)
            if login_required and force_headful:
                log.info("Waiting for user to log in (up to 3 min)...")
                try:
                    page.wait_for_url(
                        lambda u: "/property/" in u and "login" not in u.lower(),
                        timeout=180000)
                    log.info("User logged in, proceeding")
                    # Page might still be loading — wait a bit
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)
                    session_marker.touch()
                    # Re-collect data
                    captured_data["page_text"] = page.evaluate(
                        "() => document.body && document.body.innerText "
                        "    ? document.body.innerText.slice(0, 8000) : ''")
                    captured_data["final_url"] = page.url
                except Exception as e:
                    log.warning("User did not complete login: %s", e)

        finally:
            context.close()

    # Pick the most relevant API payload
    best_payload = _pick_best_payload(api_payloads, prop_id)
    deal = {"source": "ispeedtolead", "url": url, "external_id": prop_id,
             "external_link": url, "_api_responses_captured": len(api_payloads)}

    if best_payload:
        deal.update(_parse_ispeedtolead_payload(best_payload["data"]))
        deal["_api_endpoint"] = best_payload["url"]
        log.info("Got data from API: %s", best_payload["url"])
    elif captured_data.get("page_text"):
        parsed = _parse_rendered_text(captured_data["page_text"])
        deal.update(parsed)
        deal["_parsed_from"] = "rendered_dom"
        log.info("Got data from rendered DOM (fallback)")
        if not parsed.get("street"):
            deal["scrape_warning"] = (
                "Page loaded but no structured data extracted. "
                "Verify the form below — values may need correction."
            )
    elif login_required:
        deal["error"] = (
            "Login required. A Chromium window was opened — please log in "
            "there, then click Fetch again."
        )
        deal["requires_manual_entry"] = True
    else:
        deal["error"] = (
            "Page loaded but no property data captured. "
            "Property may not exist or not be accessible in your account."
        )
        deal["requires_manual_entry"] = True

    return deal


def scrape_ispeedtolead_lead(url, timeout_ms=30000, force_headful=False):
    return _run_in_clean_thread(_scrape_ispeedtolead_lead_impl,
        url, timeout_ms, force_headful)


def _try_lead_api_directly(lead_id: str, cookies: list) -> Optional[dict]:
    """Try the backend API directly with the user's cookies.

    With browser_cookie3 we get 21 cookies from Chrome including the
    session JWT. Calling the API directly bypasses Playwright entirely.

    Returns the JSON payload if found, None otherwise.
    """
    if not lead_id or not cookies:
        return None
    import httpx
    # Build cookie header from Playwright cookie dicts
    cookie_header = "; ".join(
        f"{c['name']}={c['value']}" for c in cookies
        if c.get("name") and c.get("value"))
    if not cookie_header:
        return None

    headers = {
        "Cookie": cookie_header,
        "Origin": "https://app.ispeedtolead.com",
        "Referer": "https://app.ispeedtolead.com/my-leads",
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"),
    }
    # Also try with Bearer token if any cookie name suggests one
    bearer_token = None
    for c in cookies:
        name_lower = (c.get("name") or "").lower()
        if any(k in name_lower for k in ("token", "jwt", "auth", "access")):
            bearer_token = c.get("value")
            break

    candidates = [
        # DISCOVERED endpoint (note /id/ segment in middle)
        f"https://be.ispeedtolead.com/api/orders/id/{lead_id}",
        # Other likely patterns
        f"https://be.ispeedtolead.com/api/orders/{lead_id}",
        f"https://be.ispeedtolead.com/api/order/{lead_id}",
        f"https://be.ispeedtolead.com/api/v2/lead/ai-deal-command/{lead_id}",
        f"https://be.ispeedtolead.com/api/leads/{lead_id}",
        f"https://be.ispeedtolead.com/api/lead/{lead_id}",
    ]
    log.info("Trying %d direct API endpoints with %d cookies",
              len(candidates), len(cookies))
    auth_variants = [{}, {"Authorization": f"Bearer {bearer_token}"}] if bearer_token else [{}]

    try:
        with httpx.Client(follow_redirects=True, timeout=15) as c:
            primary = None
            for extra in auth_variants:
                h = {**headers, **extra}
                for cand in candidates:
                    try:
                        r = c.get(cand, headers=h)
                        if r.status_code == 200 and len(r.content) > 100:
                            try:
                                data = r.json()
                                if isinstance(data, (dict, list)) and not (
                                        isinstance(data, dict) and data.get("status") in (401, 403)):
                                    log.info("✓ Direct API hit: %s", cand)
                                    primary = {"url": cand, "data": data}
                                    break
                            except Exception:
                                pass
                        elif r.status_code in (401, 403):
                            log.debug("Auth-blocked %s: %d", cand, r.status_code)
                    except Exception as e:
                        log.debug("Request %s failed: %s", cand, e)
                if primary:
                    break

            if not primary:
                return None

            # Try to enrich with the lead-detail endpoint if order references
            # an inner lead/property ID
            data = primary["data"]
            inner_id = _find_inner_lead_id(data)
            if inner_id and inner_id != lead_id:
                log.info("Order references inner lead/property id: %s", inner_id)
                enriched_urls = [
                    f"https://be.ispeedtolead.com/api/v2/lead/ai-deal-command/{inner_id}",
                    f"https://be.ispeedtolead.com/api/leads/id/{inner_id}",
                    f"https://be.ispeedtolead.com/api/lead/id/{inner_id}",
                    f"https://be.ispeedtolead.com/api/properties/id/{inner_id}",
                    f"https://be.ispeedtolead.com/api/properties/slug/guest/{inner_id}",
                ]
                for url2 in enriched_urls:
                    try:
                        r = c.get(url2, headers=headers)
                        if r.status_code == 200 and len(r.content) > 200:
                            try:
                                enriched = r.json()
                                if isinstance(enriched, dict) and enriched.get("status") not in (401, 403):
                                    log.info("✓ Enriched with: %s", url2)
                                    # Merge: store the order data as 'order' and the lead detail at root
                                    merged = dict(enriched)
                                    merged["__order"] = data
                                    return {"url": primary["url"] + " + " + url2, "data": merged}
                            except Exception:
                                pass
                    except Exception:
                        pass
            return primary
    except Exception as e:
        log.warning("Direct API attempt failed: %s", e)
    return None


def _find_inner_lead_id(data, _depth=0):
    """Find a nested lead/property ID reference (24-char hex)."""
    if _depth > 4:
        return None
    if isinstance(data, dict):
        # Direct ID fields
        for k in ("lead", "lead_id", "property", "property_id", "lead_obj", "property_obj"):
            v = data.get(k)
            if isinstance(v, str) and len(v) == 24 and all(c in "0123456789abcdef" for c in v):
                return v
            if isinstance(v, dict):
                inner_id = v.get("_id") or v.get("id")
                if isinstance(inner_id, str) and len(inner_id) == 24:
                    return inner_id
        # Recurse into nested
        for v in data.values():
            found = _find_inner_lead_id(v, _depth + 1)
            if found:
                return found
    elif isinstance(data, list):
        for item in data[:5]:
            found = _find_inner_lead_id(item, _depth + 1)
            if found:
                return found
    return None


def _scrape_ispeedtolead_lead_impl(url: str, timeout_ms: int = 30000,
                                force_headful: bool = False) -> dict:
    """Scrape an ispeedtolead LEAD page.

    Supports both URL patterns:
      - /ld/{id}?shared={token}    — public shared link
      - /my-leads?...&open_order={id}  — private my-leads view (needs auth)

    Uses persistent Playwright profile + Chrome cookie import to render the
    page and intercept the lead API call.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "Playwright not installed"}

    # Detect URL pattern + extract identifiers
    lead_id = None
    share_token = None
    private_mode = False

    m = re.search(r"/ld/([a-f0-9]+)", url)
    if m:
        lead_id = m.group(1)
    else:
        # my-leads pattern: ?open_order={id}
        m2 = re.search(r"[?&]open_order=([a-f0-9]+)", url)
        if m2:
            lead_id = m2.group(1)
            private_mode = True

    share_m = re.search(r"shared=([a-f0-9]+)", url)
    if share_m:
        share_token = share_m.group(1)

    log.info("scraping lead: id=%s share=%s private_mode=%s",
              lead_id, share_token, private_mode)

    # === Direct HTTP attempt (fast path — bypasses Playwright entirely) ===
    if lead_id:
        chrome_cookies_direct = (load_cookies_for_domain("ispeedtolead.com") or
                                   load_cookies_for_domain("app.ispeedtolead.com") or
                                   load_cookies_for_domain("be.ispeedtolead.com"))
        if chrome_cookies_direct:
            direct = _try_lead_api_directly(lead_id, chrome_cookies_direct)
            if direct:
                log.info("Direct API success → bypass Playwright")
                marker = _profile_dir() / ".session-established"
                marker.touch()
                result = {
                    "source": "ispeedtolead_lead",
                    "source_url": url,
                    "external_id": lead_id,
                    "share_token": share_token,
                    "_api_endpoint": direct["url"],
                }
                d = direct["data"]
                if isinstance(d, dict):
                    inner = d.get("data") or d.get("lead") or d.get("order") or d.get("property") or d
                    if isinstance(inner, list) and inner:
                        inner = inner[0]
                    if isinstance(inner, dict):
                        result.update(_parse_ispeed_lead(inner))
                return result
            else:
                log.info("Direct API didn't work; falling back to Playwright")

    profile = _profile_dir()
    session_marker = profile / ".session-established"
    has_session = session_marker.exists()

    # Clean stale SingletonLock that may persist after crash
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = profile / lock_name
        if lock_path.exists() or lock_path.is_symlink():
            try:
                lock_path.unlink()
                log.info("Removed stale lock: %s", lock_path)
            except Exception as e:
                log.warning("Couldn't remove lock %s: %s", lock_path, e)

    chrome_cookies = (load_cookies_for_domain("ispeedtolead.com") or
                       load_cookies_for_domain("app.ispeedtolead.com")) if not has_session else []

    headless = has_session and not force_headful
    api_payloads = []
    captured_text = ""

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=headless,
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/131.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            if chrome_cookies:
                try: context.add_cookies(chrome_cookies)
                except Exception as e: log.warning("Cookie inject: %s", e)
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(resp):
                try:
                    u = resp.url
                    if "/api/" not in u or resp.status >= 400:
                        return
                    ct = (resp.headers.get("content-type") or "").lower()
                    if "json" not in ct: return
                    body = resp.json()
                    api_payloads.append({"url": u, "data": body})
                except Exception:
                    pass

            page.on("response", on_response)
            # In private mode, go to the my-leads LIST first (without open_order)
            # to capture the bulk API call that returns ALL the user's leads.
            # Then we'll filter for our specific lead_id in that payload.
            if private_mode:
                list_url = "https://app.ispeedtolead.com/my-leads"
                log.info("Private mode: navigating to %s first to grab list API", list_url)
                try: page.goto(list_url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception as e: log.warning("list goto: %s", e)
                try: page.wait_for_load_state("networkidle", timeout=20000)
                except Exception: pass
                page.wait_for_timeout(4000)
                log.info("List page loaded — captured %d API calls so far", len(api_payloads))

            try: page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e: log.warning("goto: %s", e)
            try: page.wait_for_load_state("networkidle", timeout=15000)
            except Exception: pass
            # For private my-leads mode, the open_order modal may take longer
            page.wait_for_timeout(4500 if private_mode else 2500)

            # Detect login redirect EARLY
            cur = page.url
            if any(t in cur.lower() for t in ["/auth/", "/login", "/signin"]):
                if not force_headful:
                    log.info("Login required — retrying headful")
                    context.close()
                    return _run_in_clean_thread(_scrape_ispeedtolead_lead_impl,
                        url, timeout_ms*2, True)
                else:
                    # Wait for user to log in (max 3 min) and reach the lead page
                    try:
                        page.wait_for_url(
                            lambda u: ("/my-leads" in u or "/ld/" in u) and "/auth/" not in u.lower(),
                            timeout=180000,
                        )
                        session_marker.touch()
                        # Reload the original target URL to ensure the right state
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        page.wait_for_timeout(3000)
                    except Exception as e:
                        log.warning("User did not complete login: %s", e)

            # In private my-leads mode, the modal may not open automatically.
            # Try clicking the row matching the lead_id.
            if private_mode and lead_id and not any(
                lead_id in (p.get("url") or "") for p in api_payloads
            ):
                log.info("Modal didn't auto-open; trying to click the lead row")
                try:
                    # Try several selectors that might match a row containing the lead_id
                    clicked = page.evaluate(f"""() => {{
                        const rows = document.querySelectorAll('[data-id], [data-order-id], [data-lead-id], tr, .lead-row, .order-row, .card');
                        for (const r of rows) {{
                            const idAttr = r.getAttribute('data-id') || r.getAttribute('data-order-id') || r.getAttribute('data-lead-id') || '';
                            if (idAttr.includes('{lead_id}')) {{ r.click(); return true; }}
                        }}
                        // Fallback: find any element whose text/innerHTML mentions the lead_id
                        const all = document.querySelectorAll('a, button, [class*="row"], [class*="card"]');
                        for (const el of all) {{
                            if ((el.outerHTML || '').includes('{lead_id}')) {{ el.click(); return true; }}
                        }}
                        return false;
                    }}""")
                    if clicked:
                        log.info("Clicked a row; waiting for modal data")
                        page.wait_for_timeout(3000)
                except Exception as e:
                    log.warning("Row click failed: %s", e)

            # Wait up to 12 more seconds for the lead-specific API call to land
            if lead_id:
                deadline = time.time() + 12
                while time.time() < deadline:
                    if any(lead_id in (p.get("url") or "") for p in api_payloads):
                        break
                    page.wait_for_timeout(500)

            try:
                captured_text = page.evaluate(
                    "() => document.body && document.body.innerText "
                    "    ? document.body.innerText.slice(0, 8000) : ''")
            except Exception: pass

            # Only mark session as established if we actually got the lead's
            # API data (proves auth worked). URL alone isn't enough — the user
            # might have been redirected to signup.
            got_lead_data = lead_id and any(lead_id in (p.get("url") or "") for p in api_payloads)
            on_target_page = ("/my-leads" in (page.url or "") or "/ld/" in (page.url or "")) \
                and "/auth/" not in (page.url or "").lower()
            if got_lead_data or (on_target_page and len(api_payloads) >= 3):
                session_marker.touch()
        finally:
            context.close()

    # Pick the most relevant payload — usually the lead one
    # Priority order:
    #  1. Exact match: /api/orders/id/{lead_id}  (the discovered real endpoint)
    #  2. URLs containing the lead_id and "orders" (not "notes" or "counts")
    #  3. URLs with lead_id (any)
    #  4. List endpoints with lead_id in body
    best = None
    found_in_list = None
    # Priority 1: exact `/orders/id/` endpoint
    for p in api_payloads:
        if lead_id and f"/orders/id/{lead_id}" in p["url"]:
            best = p
            log.info("✓ Found primary endpoint: %s", p["url"])
            break
    # Priority 2: /orders/ or /lead/ with lead_id (but not notes/counts/ratings)
    if not best:
        for p in api_payloads:
            if (lead_id and lead_id in p["url"] and
                    ("/orders/" in p["url"] or "/lead/" in p["url"]) and
                    not any(skip in p["url"] for skip in ("notes", "counts", "ratings", "phones", "chats"))):
                best = p
                break
    # Priority 3: any URL containing the lead_id
    if not best:
        for p in api_payloads:
            if lead_id and lead_id in p["url"]:
                best = p
                break
    # Also scan inside payload BODIES for our lead_id (list endpoints often
    # return ALL the user's leads in a single array)
    if not best and lead_id:
        for p in api_payloads:
            body_str = str(p.get("data", ""))
            if lead_id in body_str:
                # Extract just our lead from the array
                data = p.get("data")
                found = _find_lead_in_payload(data, lead_id)
                if found:
                    log.info("Found lead inside list payload: %s", p["url"])
                    found_in_list = {"url": p["url"], "data": found}
                    break
    if not best and found_in_list:
        best = found_in_list
    if not best:
        for keyword in ["order", "lead", "owned", "property"]:
            for p in api_payloads:
                if keyword in p["url"].lower() and isinstance(p.get("data"), dict):
                    best = p
                    break
            if best:
                break
    if not best and api_payloads:
        # Filter out tiny / unrelated payloads (size > 500 bytes)
        big_ones = [p for p in api_payloads if len(str(p.get("data", ""))) > 500]
        candidates = big_ones if big_ones else api_payloads
        best = max(candidates, key=lambda x: len(str(x.get("data", ""))))
    log.info("captured %d API payloads; best=%s",
              len(api_payloads),
              (best["url"] if best else None))
    # Log ALL captured API URLs for debugging endpoint discovery
    for p in api_payloads:
        log.info("  API: %s", p.get("url", ""))

    result = {
        "source": "ispeedtolead_lead",
        "source_url": url,
        "external_id": lead_id,
        "share_token": share_token,
    }
    if best:
        data = best["data"]
        # Pass the WHOLE order/data to the parser — it knows to drill into .lead
        if isinstance(data, dict):
            # If wrapped in {data: ...} envelope
            wrap = data.get("data")
            if isinstance(wrap, dict) and (wrap.get("lead") or wrap.get("_id")):
                data = wrap
            result.update(_parse_ispeed_lead(data))
            result["_api_endpoint"] = best["url"]

    # ===== ENRICH from /api/v2/lead/ai-deal-command/{inner_id} =====
    # The order payload has a `lead` reference. The actual property data is
    # in a separate Playwright-captured payload at /api/v2/lead/ai-deal-command/
    for p in api_payloads:
        u = p.get("url", "")
        if "/api/v2/lead/ai-deal-command/" in u or "/api/lead/ai-deal-command/" in u:
            log.info("=== AI-DEAL-COMMAND PAYLOAD ===")
            try:
                import json as _j
                log.info(_j.dumps(p["data"], indent=2, default=str)[:8000])
            except Exception: pass
            log.info("=== END AI-DEAL-COMMAND ===")
            enriched = p["data"]
            if isinstance(enriched, dict):
                inner = enriched.get("data") or enriched.get("lead") or enriched.get("property") or enriched
                if isinstance(inner, list) and inner: inner = inner[0]
                if isinstance(inner, dict):
                    parsed = _parse_ispeed_lead(inner)
                    # Merge — order data has lead_price, ai-deal-command has property details
                    for k, v in parsed.items():
                        if v not in (None, "") and (k not in result or k in ("street","beds","baths","sqft","year_built","image","image_gallery","description","motivation","estimated_arv","estimated_rehab","address","asking_price")):
                            result[k] = v
                    result["_enriched_from"] = u
            break

    # Also check for properties/slug/guest payloads
    for p in api_payloads:
        u = p.get("url", "")
        if "/properties/slug/" in u or "/api/properties/" in u:
            log.info("Found property payload: %s", u)
            try:
                import json as _j
                log.info("PROPERTY: " + _j.dumps(p["data"], indent=2, default=str)[:4000])
            except Exception: pass
            enriched = p["data"]
            if isinstance(enriched, dict):
                inner = enriched.get("data") or enriched.get("property") or enriched
                if isinstance(inner, dict):
                    parsed = _parse_ispeed_lead(inner)
                    for k, v in parsed.items():
                        if v not in (None, "") and (k not in result or k in ("street","beds","baths","sqft","year_built","image","image_gallery","description")):
                            result[k] = v
            break
    if captured_text and not result.get("address"):
        result["_page_text_excerpt"] = captured_text[:3000]
    return result


def _find_lead_in_payload(data, lead_id: str):
    """Recursively search a JSON structure for an object containing the lead_id.

    Returns the matching dict if found, else None.
    """
    if isinstance(data, dict):
        # If this dict's _id or id matches, that's our object
        for key in ("_id", "id", "order_id", "lead_id"):
            v = data.get(key)
            if v == lead_id or (isinstance(v, dict) and v.get("$oid") == lead_id):
                return data
        # Otherwise recurse into nested dicts/lists
        for v in data.values():
            found = _find_lead_in_payload(v, lead_id)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_lead_in_payload(item, lead_id)
            if found:
                return found
    return None


def _to_int(v):
    if v is None: return None
    if isinstance(v, (int, float)): return int(v)
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*", v)
        if m:
            try: return int(m.group(0).replace(",", ""))
            except: return None
    return None


def _to_float(v):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    if isinstance(v, str):
        m = re.search(r"\d+(?:\.\d+)?", v)
        if m:
            try: return float(m.group(0))
            except: return None
    return None


def _parse_ispeed_lead(d: dict) -> dict:
    """Extract structured fields from an ispeedtolead order payload.

    The order payload has this structure:
      { _id, number, displayed_name (seller name),
        user {...buyer...},
        lead {
          _id, displayed_name, source,
          state, city, city_details {name, county_name},
          goal (motivation),
          price (= LEAD PRICE — what you paid),
          lead_details: [{field, name, value}],   ← basic info
          premium_details: [{field, name, value}], ← extra info
          summary: { summary: "free text" },
          call_strategy: { call_strategy: "HTML" },
          images: [...],
        }
      }
    """
    out = {}

    # If we got passed the FULL order, drill down to .lead
    lead = d.get("lead") if isinstance(d.get("lead"), dict) else d

    # ---- Seller name ----
    seller = d.get("displayed_name") or lead.get("displayed_name")
    if seller:
        out["seller_name"] = seller

    # ---- Lead-price (what you pay to acquire) ----
    # Always look in the lead block's `price` field
    lp = lead.get("price")
    if lp is not None:
        out["lead_price"] = lp

    # ---- Source / motivation ----
    if lead.get("source"):
        out["lead_source_label"] = lead["source"]
    motivation = lead.get("goal") or lead.get("motivation")
    if motivation:
        out["motivation"] = motivation

    # ---- City / state / county ----
    city = lead.get("city")
    if isinstance(city, dict): city = city.get("name", "")
    out["city"] = city or ""

    state = lead.get("state")
    if isinstance(state, dict): state = state.get("code") or state.get("name", "")
    out["state"] = state or ""

    cd = lead.get("city_details") or {}
    if isinstance(cd, dict):
        if cd.get("name") and not out.get("city"):
            out["city"] = cd["name"]
        if cd.get("county_name"):
            out["county"] = cd["county_name"]
        if cd.get("lat") and not out.get("lat"):
            out["lat"] = cd["lat"]
        if cd.get("lng") and not out.get("lng"):
            out["lng"] = cd["lng"]

    # ---- Flatten lead_details + premium_details key-value arrays ----
    all_details = []
    for arr_key in ("lead_details", "premium_details", "details"):
        arr = lead.get(arr_key) or []
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict) and item.get("field"):
                    all_details.append((item["field"], item.get("value")))

    def get_field(*field_names):
        for fn in field_names:
            for k, v in all_details:
                if k == fn:
                    return v
        return None

    out["asking_price"] = _to_int(get_field("asking_price", "price"))
    out["zip"] = get_field("zip", "zipcode") or ""
    out["beds"] = _to_int(get_field("bedrooms", "beds"))
    out["baths"] = _to_float(get_field("bathrooms", "baths"))
    # sqft can be a range like "1000 - 2000" — take midpoint
    sqft_raw = get_field("square", "square_feet", "sqft")
    if sqft_raw:
        m = re.search(r"(\d[\d,]*)\s*-\s*(\d[\d,]*)", str(sqft_raw))
        if m:
            try:
                lo = int(m.group(1).replace(",", ""))
                hi = int(m.group(2).replace(",", ""))
                out["sqft"] = (lo + hi) // 2
                out["sqft_range"] = f"{lo:,} - {hi:,}"
            except: pass
        else:
            out["sqft"] = _to_int(sqft_raw)
    # year_built can be a range "1900-1950"
    year_raw = get_field("year", "year_built")
    if year_raw:
        m = re.search(r"(\d{4})\s*-\s*(\d{4})", str(year_raw))
        if m:
            out["year_built"] = (int(m.group(1)) + int(m.group(2))) // 2
            out["year_range"] = f"{m.group(1)}-{m.group(2)}"
        else:
            out["year_built"] = _to_int(year_raw)
    # Lot size in acres
    lot_raw = get_field("lot_size", "lot")
    if lot_raw:
        out["lot_size"] = str(lot_raw)
        if "acre" not in str(lot_raw).lower():
            out["lot_size"] = f"{lot_raw} acres"

    # Property type
    pt = get_field("multifamily", "property_type", "type")
    if pt:
        out["property_type"] = pt
    elif d.get("type"):
        out["property_type"] = d["type"]

    # Repairs hint → put into estimated_rehab description
    repairs = get_field("repairs", "condition")
    occupied = get_field("occupied")
    sell_fast = get_field("sell_fast")
    owned_years = get_field("owned_years")
    listed = get_field("listed")
    owner = get_field("owner")

    # ---- Build a rich description ----
    desc_parts = []
    if motivation:
        desc_parts.append(f"**Motivation:** {motivation}")
    if seller:
        desc_parts.append(f"**Seller:** {seller}")
    if owner:
        desc_parts.append(f"**Owner status:** {owner}")
    if occupied:
        desc_parts.append(f"**Occupancy:** {occupied}")
    if listed:
        desc_parts.append(f"**Currently listed:** {listed}")
    if sell_fast:
        desc_parts.append(f"**Timeline to sell:** {sell_fast}")
    if owned_years:
        desc_parts.append(f"**Years owned:** {owned_years}")
    if repairs:
        desc_parts.append(f"**Repairs needed:** {repairs}")

    summary_obj = lead.get("summary")
    if isinstance(summary_obj, dict) and summary_obj.get("summary"):
        desc_parts.append("\n**Lead summary**\n" + summary_obj["summary"])
    elif isinstance(lead.get("description"), str):
        desc_parts.append("\n" + lead["description"])

    call_strat = lead.get("call_strategy")
    if isinstance(call_strat, dict) and call_strat.get("call_strategy"):
        clean = re.sub(r"<[^>]+>", " ", call_strat["call_strategy"])
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            desc_parts.append("\n**Call strategy**\n" + clean[:2000])

    if desc_parts:
        out["description"] = "\n".join(desc_parts)

    # ---- Address (build from pieces; ispeedtolead doesn't expose street in base data) ----
    parts = [out.get("city", ""),
             f"{out.get('state', '')} {out.get('zip', '')}".strip()]
    addr = ", ".join(p for p in parts if p)
    if addr:
        out["address"] = addr.title() if addr.islower() else addr

    # ---- Images ----
    images = lead.get("images") or lead.get("photos") or []
    if isinstance(images, list):
        urls = []
        for img in images[:30]:
            if isinstance(img, dict):
                cropped = img.get("cropped") or []
                best = None
                for c in cropped:
                    if isinstance(c, dict) and c.get("crop_size") == "800w":
                        best = c.get("location")
                        break
                urls.append(best or img.get("location") or img.get("url") or img.get("src"))
            elif isinstance(img, str):
                urls.append(img)
        urls = [u for u in urls if u]
        if urls:
            out["image"] = urls[0]
            out["image_gallery"] = urls

    # Lat/lng overrides
    out["lat"] = out.get("lat") or lead.get("lat") or lead.get("latitude")
    out["lng"] = out.get("lng") or lead.get("lng") or lead.get("longitude")

    # Status
    if lead.get("status"):
        out["lead_marketplace_status"] = lead["status"]

    return {k: v for k, v in out.items() if v not in (None, "")}


def _pick_best_payload(payloads: list, prop_id: Optional[str]) -> Optional[dict]:
    """Pick the payload most likely to contain the property data."""
    if not payloads:
        return None
    # Prefer payloads whose URL contains the prop_id
    if prop_id:
        for p in payloads:
            if f"/{prop_id}" in p["url"] or f"={prop_id}" in p["url"]:
                return p
    # Otherwise pick the largest JSON (most likely to be the full record)
    return max(payloads, key=lambda p: len(str(p.get("data", ""))))


def _parse_ispeedtolead_payload(data) -> dict:
    """Extract deal fields from an ispeedtolead API JSON response."""
    # Drill down to the property object
    p = data
    if isinstance(p, dict):
        for key in ("data", "property", "deal", "result", "payload"):
            if key in p and isinstance(p[key], (dict, list)):
                p = p[key]
                break
    if isinstance(p, list) and p:
        p = p[0]
    if not isinstance(p, dict):
        return {}

    def pick(*keys):
        for k in keys:
            v = p.get(k) if isinstance(p, dict) else None
            if v not in (None, "", 0):
                return v
        return None

    return {
        "street": pick("address", "street_address", "streetAddress",
                        "property_address", "addressLine1"),
        "city": pick("city", "property_city"),
        "state": pick("state", "property_state", "state_code", "stateCode"),
        "zip": pick("zip", "zipcode", "postal_code", "zip_code", "postalCode"),
        "bedrooms": pick("bedrooms", "beds", "bedrooms_count"),
        "bathrooms": pick("bathrooms", "baths", "bathrooms_count"),
        "sqft": pick("sqft", "square_feet", "squareFeet", "living_area",
                      "livingArea", "buildingSize"),
        "year_built": pick("year_built", "yearBuilt", "build_year"),
        "price": pick("asking_price", "list_price", "price", "wholesale_price",
                       "askingPrice", "listPrice"),
        "arv": pick("arv", "after_repair_value", "estimated_arv", "afterRepairValue"),
        "rehab_estimate": pick("rehab_estimate", "estimated_repair_cost",
                                 "repair_estimate", "rehab", "repairCost"),
        "rent_estimate": pick("rent_estimate", "estimated_rent", "rent",
                                "monthly_rent"),
        "home_type": pick("property_type", "home_type", "propertyType"),
        "lot_size_sqft": pick("lot_size", "lot_sqft", "lotSize"),
        "description": pick("description", "notes", "details"),
        "image": pick("image", "image_url", "primary_image", "thumbnail"),
        "days_on_market": pick("days_on_market", "daysOnMarket", "dom"),
    }


def _parse_rendered_text(text: str) -> dict:
    """Best-effort extraction from rendered page text using regex."""
    out = {}
    if not text:
        return out

    # Address (US format)
    m = re.search(
        r"(\d{1,5}\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,4}\s+"
        r"(?:St|Ave|Rd|Blvd|Dr|Ln|Ct|Pl|Way|Pkwy|Cir|Ter)\.?)",
        text)
    if m:
        out["street"] = m.group(1)

    # City, State ZIP
    m = re.search(r"([A-Z][a-zA-Z\s]+),\s*([A-Z]{2})\s+(\d{5})", text)
    if m:
        out["city"] = m.group(1).strip()
        out["state"] = m.group(2)
        out["zip"] = m.group(3)

    # Prices (look for labeled prices)
    def first_money(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            s = m.group(1).replace(",", "").replace("$", "")
            try:
                return int(float(s))
            except (ValueError, TypeError):
                return None
        return None

    out["price"] = (first_money(r"(?:asking|list|wholesale|price)[:\s]*\$?\s*([\d,]+)") or
                     first_money(r"price[:\s]*\$\s*([\d,]+)"))
    out["arv"] = first_money(r"(?:arv|after\s*repair)[:\s]*\$?\s*([\d,]+)")
    out["rehab_estimate"] = first_money(
        r"(?:rehab|repair(?:s|\s*cost)?)[:\s]*\$?\s*([\d,]+)")
    out["rent_estimate"] = first_money(r"(?:rent|monthly\s*rent)[:\s]*\$?\s*([\d,]+)")

    # Beds / baths / sqft
    m = re.search(r"(\d+)\s*(?:bed|br|bedroom)", text, re.IGNORECASE)
    if m:
        out["bedrooms"] = int(m.group(1))
    m = re.search(r"(\d+(?:\.\d)?)\s*(?:bath|ba|bathroom)", text, re.IGNORECASE)
    if m:
        try:
            out["bathrooms"] = float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"([\d,]+)\s*(?:sq\s*ft|sqft|sf\b)", text, re.IGNORECASE)
    if m:
        try:
            out["sqft"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Year built
    m = re.search(r"(?:built|year\s*built)[:\s]*(\d{4})", text, re.IGNORECASE)
    if m:
        out["year_built"] = int(m.group(1))

    return {k: v for k, v in out.items() if v is not None}
