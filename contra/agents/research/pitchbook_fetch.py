"""
Authenticated PitchBook profile fetcher.

When PitchBook session cookies are present (saved by pitchbook_scraper.py after
any successful login), this module fetches LP profile pages from PitchBook using
a shared headless Selenium session.  The extracted text is richer than what Tavily
can retrieve from public pages — it includes AUM, LP type, geography, recent fund
commitments, and portfolio descriptions that sit behind the login wall.

The module uses a process-level singleton driver so the startup cost is paid only
once per Python process, not on every gate call.

Usage
-----
    from agents.research.pitchbook_fetch import pb_inject_result

    result = pb_inject_result(lp_name="HarbourVest Partners", pb_url=None)
    if result:
        # inject into SearchResult list before compiling context
        ...
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent.parent
_COOKIE_PATH = _ROOT / "processed_data" / "pb_session_cookies.json"
_CACHE_DIR = _ROOT / "processed_data" / "pb_profile_cache"

_PB_BASE = "https://my.pitchbook.com"
_PB_SEARCH_TEMPLATE = "https://my.pitchbook.com/search?q={q}&type=limited-partner"

# ---------------------------------------------------------------------------
# Cookie availability check (fast — no browser)
# ---------------------------------------------------------------------------

def cookies_available() -> bool:
    """Return True if saved PitchBook session cookies exist on disk."""
    return _COOKIE_PATH.exists() and _COOKIE_PATH.stat().st_size > 10


# ---------------------------------------------------------------------------
# Per-process singleton driver
# ---------------------------------------------------------------------------

_driver = None  # module-level singleton


def _get_driver():
    global _driver
    if _driver is not None:
        try:
            _ = _driver.current_url  # ping — raises if browser crashed
            return _driver
        except Exception:
            _driver = None

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)

    # Load cookies into the browser session
    driver.get(_PB_BASE)
    time.sleep(2)

    cookies = json.loads(_COOKIE_PATH.read_text(encoding="utf-8"))
    for cookie in cookies:
        if "expiry" in cookie:
            cookie["expiry"] = int(cookie["expiry"])
        for key in ("sameSite", "stalenessTtl"):
            cookie.pop(key, None)
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass

    _driver = driver
    return _driver


def shutdown_driver() -> None:
    """Call at process exit to cleanly quit the singleton browser."""
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _read_cache(key: str) -> Optional[str]:
    path = _CACHE_DIR / f"{key}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return None


def _write_cache(key: str, text: str) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{key}.txt").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Core fetch logic
# ---------------------------------------------------------------------------

def _fetch_url_text(url: str) -> Optional[str]:
    """Navigate to a PitchBook URL with the authenticated driver; return page text."""
    driver = _get_driver()
    try:
        driver.get(url)
        time.sleep(3.5)  # allow SPA to render

        if "/login" in driver.current_url:
            logger.debug("PitchBook cookies expired — profile fetch unavailable")
            return None

        # Extract meaningful sections: overview panel + recent activity
        text_parts = []

        # Try targeted selectors first for cleaner output
        for sel in [
            "[class*='entity-overview']",
            "[class*='profile-overview']",
            "[class*='EntityOverview']",
            "[class*='overview-panel']",
            "main",
            "article",
            "[role='main']",
        ]:
            elems = driver.find_elements("css selector", sel)
            if elems:
                t = elems[0].text.strip()
                if len(t) > 100:
                    text_parts.append(t)
                    break

        # Always append page title + first 1500 chars of body as fallback
        try:
            body_text = driver.find_element("css selector", "body").text
            text_parts.append(body_text[:2500])
        except Exception:
            pass

        full = "\n\n".join(dict.fromkeys(text_parts))  # deduplicate, preserve order
        return full[:3000].strip() if full.strip() else None

    except Exception as exc:
        logger.debug("PitchBook fetch error for %s: %s", url, exc)
        return None


def _search_pb(lp_name: str) -> Optional[str]:
    """Search PitchBook for an LP by name; return the profile URL of the top result."""
    driver = _get_driver()
    try:
        search_url = _PB_SEARCH_TEMPLATE.format(q=lp_name.replace(" ", "+"))
        driver.get(search_url)
        time.sleep(3)

        if "/login" in driver.current_url:
            return None

        # Find the first profile link in results
        for sel in [
            "a[href*='/profiles/limited-partner/']",
            "a[href*='/profiles/investor/']",
            "[class*='result'] a[href*='/profiles/']",
            "[class*='SearchResult'] a",
            "table a[href*='/profiles/']",
        ]:
            elems = driver.find_elements("css selector", sel)
            if elems:
                href = elems[0].get_attribute("href") or ""
                if href and "pitchbook.com" in href:
                    return href

        return None
    except Exception as exc:
        logger.debug("PitchBook search error for '%s': %s", lp_name, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class PBResult:
    lp_name: str
    url: str
    text: str  # extracted page text, max ~3000 chars


def fetch_pb_profile(
    lp_name: str,
    pb_url: Optional[str] = None,
) -> Optional[PBResult]:
    """
    Fetch an LP's PitchBook profile and return the extracted text.

    Parameters
    ----------
    lp_name : display name of the LP (used for searching if pb_url is None)
    pb_url  : direct PitchBook profile URL; if omitted, searches by name

    Returns None if cookies aren't available, the session is expired, or the
    fetch fails for any reason — callers should treat this as a graceful miss.
    """
    if not cookies_available():
        return None

    # Cache lookup
    cache_key = _cache_key(pb_url or lp_name.lower())
    cached = _read_cache(cache_key)
    if cached:
        logger.debug("PitchBook cache hit for '%s'", lp_name)
        return PBResult(lp_name=lp_name, url=pb_url or "", text=cached)

    try:
        url = pb_url
        if not url:
            url = _search_pb(lp_name)
            if not url:
                logger.debug("PitchBook: no search result for '%s'", lp_name)
                return None

        text = _fetch_url_text(url)
        if not text:
            return None

        _write_cache(cache_key, text)
        logger.debug("PitchBook profile fetched for '%s' (%s)", lp_name, url)
        return PBResult(lp_name=lp_name, url=url, text=text)

    except Exception as exc:
        logger.debug("PitchBook fetch failed for '%s': %s", lp_name, exc)
        return None


def pb_inject_result(
    lp_name: str,
    pb_url: Optional[str] = None,
) -> Optional[object]:
    """
    Convenience wrapper that returns a ``SearchResult``-compatible object
    ready to inject into the gate / enrichment search result list.

    The result is given score=2.0 so it ranks above all Tavily results
    and the LLM sees PitchBook's structured data first.

    Returns None if no profile could be fetched.
    """
    # Import here to avoid circular imports
    from agents.research.web_search import SearchResult

    profile = fetch_pb_profile(lp_name, pb_url)
    if not profile:
        return None

    # Build a clean snippet from the first 400 chars of the profile text
    snippet = " ".join(profile.text.split())[:400]

    return SearchResult(
        title=f"PitchBook profile: {lp_name}",
        url=profile.url or f"https://my.pitchbook.com/search?q={lp_name.replace(' ', '+')}",
        snippet=snippet,
        score=2.0,   # highest priority — above Tavily (0–1) and NFX (1.5)
        raw_content=profile.text,
    )
