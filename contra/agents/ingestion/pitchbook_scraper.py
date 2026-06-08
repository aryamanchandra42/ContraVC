"""
PitchBook LP Discovery Scraper — https://my.pitchbook.com

Logs into PitchBook, navigates to the LP search with preset filters
(Family Office / Fund of Funds / Asset Manager, Asia + Middle East geography,
recent VC activity), paginates through results, deduplicates against the CRM
and known allocators, then runs every genuinely new LP through the LP Gate.
Investors that pass (YES / REVIEW) are persisted to contra.duckdb automatically.

Architecture mirrors nfx_selenium_scraper.py exactly.

Entry points
------------
- CLI:    contra pitchbook-scrape
- Direct: python -m agents.ingestion.pitchbook_scraper

Credentials
-----------
Set PITCHBOOK_EMAIL and PITCHBOOK_PASSWORD in .env before running.

Anti-bot note
-------------
PitchBook uses Cloudflare. This scraper uses undetected-chromedriver when
available (pip install undetected-chromedriver) and falls back to regular
Selenium Chrome. Use --no-headless if you encounter Cloudflare challenges
that require manual solving on the first run; subsequent runs typically work
headlessly once the session cookie is established.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from rapidfuzz import fuzz
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

logger = logging.getLogger(__name__)
console = Console()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PB_BASE_URL = "https://my.pitchbook.com"
PB_LOGIN_URL = f"{PB_BASE_URL}/login"

# LP Search with pre-applied filters: Family Office + FoF + Asset Manager,
# Asia Pacific + Middle East, Venture Capital fund activity.
# The `?` params may vary by PitchBook version — the scraper also tries to
# apply filters through the UI if URL params don't stick.
PB_LP_SEARCH_URL = (
    f"{PB_BASE_URL}/lp-search"
    "?investorTypes=familyOffice,fundOfFunds,assetManager"
    "&geographies=asiaPacific,middleEast"
    "&assetClasses=ventureCap"
)

CHECKPOINT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "processed_data"
    / "pb_scrape_checkpoint.json"
)

# Saved browser session cookies (written after successful login, reused on next run)
COOKIE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "processed_data"
    / "pb_session_cookies.json"
)

FUZZY_THRESHOLD = 85
PAGE_LOAD_PAUSE = 3.0
BETWEEN_PAGES_PAUSE = 2.0
MAX_STALE_PAGES = 4

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class PitchBookLPRecord:
    """One LP row extracted from PitchBook search results."""

    investor_name: str
    investor_type: Optional[str] = None
    hq_country: Optional[str] = None
    hq_city: Optional[str] = None
    aum_usd: Optional[str] = None
    pitchbook_url: Optional[str] = None
    geography_focus: Optional[str] = None

    def to_analyst_facts(self) -> List[str]:
        """
        Convert structured fields to analyst-fact strings that the gate
        evaluator understands — improves C4 (geography) and allocator type
        classification without needing extra web search.
        """
        from agents.normalization.pitchbook_normalizer import (
            normalize_pb_lp_type,
            normalize_pb_geography,
        )
        facts: List[str] = []
        if self.investor_type:
            canonical_type = normalize_pb_lp_type(self.investor_type)
            if canonical_type != "unknown":
                facts.append(f"Investor type: {canonical_type} (from PitchBook)")
        if self.hq_country:
            facts.append(f"HQ country: {self.hq_country} (from PitchBook)")
        if self.geography_focus:
            canon_geo = normalize_pb_geography(self.geography_focus)
            if canon_geo:
                facts.append(f"Geography focus: {canon_geo} (from PitchBook)")
        if self.aum_usd:
            facts.append(f"AUM: {self.aum_usd} (from PitchBook)")
        return facts


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> Set[str]:
    if CHECKPOINT_PATH.exists():
        try:
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
            return set(data.get("processed", []))
        except Exception:
            pass
    return set()


def _save_checkpoint(processed: Set[str]) -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH.write_text(
        json.dumps({"processed": sorted(processed)}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Deduplication helpers (mirrors NFX scraper)
# ---------------------------------------------------------------------------

def _load_known_names(con) -> List[str]:
    rows = con.execute("SELECT canonical_name FROM allocators").fetchall()
    names = [r[0] for r in rows if r[0]]
    crm_rows = con.execute("SELECT investor_name FROM crm_leads").fetchall()
    names.extend(r[0] for r in crm_rows if r[0])
    return names


def _is_crm_duplicate(con, name: str) -> bool:
    from contra.intelligence.brief import _crm_lookup
    in_crm, _ = _crm_lookup(con, name)
    return in_crm


def _is_fuzzy_duplicate(name: str, known_names: List[str]) -> Tuple[bool, Optional[str]]:
    if not known_names:
        return False, None
    best_match = None
    best_score = 0.0
    for known in known_names:
        score = fuzz.token_sort_ratio(name.lower(), known.lower())
        if score > best_score:
            best_score = score
            best_match = known
    if best_score >= FUZZY_THRESHOLD:
        return True, best_match
    return False, None


# ---------------------------------------------------------------------------
# Browser / WebDriver builders
# ---------------------------------------------------------------------------

# Brave executable locations to try on Windows (user-install first, then system-wide)
_BRAVE_EXE_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    os.path.join(os.environ.get("USERPROFILE", ""), r"AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
]

# Brave user-data directory (holds all saved sessions / cookies)
_BRAVE_PROFILE_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"BraveSoftware\Brave-Browser\User Data"),
    os.path.join(os.environ.get("USERPROFILE", ""), r"AppData\Local\BraveSoftware\Brave-Browser\User Data"),
]


def _build_brave_driver(connect_port: Optional[int] = None):
    """
    Launch Brave Browser reusing the existing user profile — which is already
    logged into PitchBook — so no login step is required at all.

    Two modes:
      connect_port=None  → start a fresh Brave window using the saved profile.
                           Brave must NOT already be running (profile is locked).
      connect_port=9222  → attach to an already-running Brave instance that was
                           started with --remote-debugging-port=9222.

    Brave is Chromium-based so we use Selenium's Chrome driver against it.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    opts = Options()

    if connect_port:
        # ── Attach to a running Brave via CDP ────────────────────────────
        opts.add_experimental_option("debuggerAddress", f"localhost:{connect_port}")
        console.print(f"[dim]Connecting to Brave via CDP on port {connect_port}[/dim]")
    else:
        # ── Launch Brave with its existing profile ────────────────────────
        brave_exe = next((p for p in _BRAVE_EXE_CANDIDATES if Path(p).exists()), None)
        if brave_exe:
            opts.binary_location = brave_exe
            console.print(f"[dim]Brave found: {brave_exe}[/dim]")
        else:
            console.print(
                "[yellow]Brave executable not found in default locations.\n"
                "Set BRAVE_EXE env var or pass --connect-port.[/yellow]"
            )
            brave_exe_env = os.environ.get("BRAVE_EXE", "")
            if brave_exe_env and Path(brave_exe_env).exists():
                opts.binary_location = brave_exe_env

        profile_dir = next((p for p in _BRAVE_PROFILE_CANDIDATES if Path(p).exists()), None)
        if profile_dir:
            opts.add_argument(f"--user-data-dir={profile_dir}")
            opts.add_argument("--profile-directory=Default")
            console.print(f"[dim]Using Brave profile: {profile_dir}[/dim]")
        else:
            console.print("[yellow]Brave profile directory not found — session may not be restored[/yellow]")

        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)

    # Use a compatible ChromeDriver — Brave is Chromium-based so the standard
    # ChromeDriver works; webdriver_manager will download the right version.
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        from webdriver_manager.core.os_manager import ChromeType  # type: ignore[import]
        service = Service(ChromeDriverManager(chrome_type=ChromeType.BRAVE).install())
        console.print("[dim]Using ChromeDriver matched to Brave version[/dim]")
    except Exception:
        # Older webdriver_manager or ChromeType.BRAVE not available — use default
        from webdriver_manager.chrome import ChromeDriverManager
        from selenium.webdriver.chrome.service import Service
        service = Service(ChromeDriverManager().install())

    driver = webdriver.Chrome(service=service, options=opts)
    return driver


def _build_driver(headless: bool = True):
    """
    Build a Chrome WebDriver. Tries undetected-chromedriver first (handles
    Cloudflare), falls back to regular Selenium Chrome.
    """
    # Try undetected-chromedriver first
    try:
        import undetected_chromedriver as uc  # type: ignore[import]

        opts = uc.ChromeOptions()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        driver = uc.Chrome(options=opts)
        console.print("[dim]Using undetected-chromedriver (Cloudflare bypass)[/dim]")
        return driver
    except ImportError:
        console.print(
            "[yellow]undetected-chromedriver not installed — falling back to regular Chrome. "
            "Run: pip install undetected-chromedriver[/yellow]"
        )

    # Fallback: regular Selenium Chrome
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def _wait_for(driver, css: str, timeout: float = 20.0):
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located(("css selector", css))
    )
    return driver.find_elements("css selector", css)


def _first_text(element, selectors: List[str]) -> Optional[str]:
    for sel in selectors:
        try:
            el = element.find_element("css selector", sel)
            text = el.text.strip()
            if text:
                return text
        except Exception:
            continue
    return None


def _first_attr(element, selectors: List[str], attr: str) -> Optional[str]:
    for sel in selectors:
        try:
            el = element.find_element("css selector", sel)
            val = el.get_attribute(attr)
            if val and val.strip():
                return val.strip()
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Cookie persistence — reuse sessions across runs
# ---------------------------------------------------------------------------

def _save_cookies(driver) -> None:
    """Persist browser cookies to disk so the next run skips login entirely."""
    try:
        COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cookies = driver.get_cookies()
        COOKIE_PATH.write_text(
            json.dumps(cookies, indent=2, default=str), encoding="utf-8"
        )
        console.print(f"[dim]Session cookies saved ({len(cookies)} cookies → {COOKIE_PATH.name})[/dim]")
    except Exception as exc:
        logger.warning("Could not save cookies: %s", exc)


def _load_cookies(driver) -> bool:
    """
    Load previously saved cookies into the browser.
    Returns True if cookies were loaded and the session appears valid
    (i.e. we land on a non-login page after restoring cookies).
    """
    if not COOKIE_PATH.exists():
        return False
    try:
        cookies = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
        if not cookies:
            return False

        # Must navigate to the domain first before adding cookies
        driver.get(PB_BASE_URL)
        time.sleep(2)

        for cookie in cookies:
            # Selenium requires 'expiry' to be an int, not float
            if "expiry" in cookie:
                cookie["expiry"] = int(cookie["expiry"])
            # Remove keys that Selenium doesn't accept
            for key in ("sameSite", "stalenessTtl"):
                cookie.pop(key, None)
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass

        # Navigate to the app and check we're not back at login
        driver.get(PB_BASE_URL)
        time.sleep(PAGE_LOAD_PAUSE)

        if "/login" not in driver.current_url and "pitchbook" in driver.current_url.lower():
            console.print("[green]Session restored from saved cookies — skipping login[/green]")
            return True

        console.print("[dim]Saved cookies expired — will log in fresh[/dim]")
        return False
    except Exception as exc:
        logger.warning("Cookie restore failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Login — standard email/password
# ---------------------------------------------------------------------------

_SEL_EMAIL = 'input[type="email"], input[name="email"], input[id*="email"], input[placeholder*="mail"]'
_SEL_PASSWORD = 'input[type="password"], input[name="password"]'
_SEL_SUBMIT = 'button[type="submit"], button[class*="submit"], button[class*="login"], input[type="submit"]'


def _login_password(driver, email: str, password: str) -> bool:
    """Standard email + password login."""
    from selenium.webdriver.support.ui import WebDriverWait

    console.print(f"[dim]Navigating to login: {PB_LOGIN_URL}[/dim]")
    driver.get(PB_LOGIN_URL)
    time.sleep(PAGE_LOAD_PAUSE)
    time.sleep(2)  # extra wait for Cloudflare

    try:
        elems = _wait_for(driver, _SEL_EMAIL, timeout=25)
        elems[0].clear()
        elems[0].send_keys(email)
    except Exception as exc:
        console.print(f"[red]Could not find email field: {exc}[/red]")
        console.print("[yellow]Try --no-headless to solve any Cloudflare challenge manually[/yellow]")
        return False

    try:
        pw_elems = driver.find_elements("css selector", _SEL_PASSWORD)
        if not pw_elems:
            console.print("[red]Could not find password field[/red]")
            return False
        pw_elems[0].clear()
        pw_elems[0].send_keys(password)
    except Exception as exc:
        console.print(f"[red]Could not fill password: {exc}[/red]")
        return False

    time.sleep(0.5)

    try:
        submit_elems = driver.find_elements("css selector", _SEL_SUBMIT)
        if not submit_elems:
            console.print("[red]Could not find submit button[/red]")
            return False
        submit_elems[0].click()
    except Exception as exc:
        console.print(f"[red]Submit failed: {exc}[/red]")
        return False

    try:
        WebDriverWait(driver, 30).until(lambda d: "/login" not in d.current_url)
        console.print("[green]Login successful[/green]")
        return True
    except Exception:
        page = driver.page_source.lower()
        if "invalid" in page or "incorrect" in page or "wrong" in page or "error" in page:
            console.print("[red]Login failed — check PITCHBOOK_EMAIL / PITCHBOOK_PASSWORD in .env[/red]")
        else:
            console.print("[yellow]Login redirect not detected; proceeding anyway[/yellow]")
        return True  # optimistic


# ---------------------------------------------------------------------------
# Login — SSO (Single Sign-On)
# ---------------------------------------------------------------------------

# PitchBook SSO button selectors (try in order)
_SEL_SSO_BUTTON = [
    'button[data-testid*="sso"]',
    'a[data-testid*="sso"]',
    'button[class*="sso"]',
    'a[class*="sso"]',
    'button:contains("Sign in with SSO")',   # not standard CSS, handled below
    '[class*="SsoButton"]',
    '[class*="sso-button"]',
    # Generic "Continue with" / "Sign in with" links that aren't email/password
    'a[href*="saml"]',
    'a[href*="sso"]',
    'button[aria-label*="SSO"]',
    'button[aria-label*="single sign"]',
]

# After clicking SSO, PitchBook asks for the work email / domain
_SEL_SSO_EMAIL = [
    'input[placeholder*="work email"]',
    'input[placeholder*="email"]',
    'input[type="email"]',
    'input[name="email"]',
]

_SEL_SSO_CONTINUE = [
    'button[type="submit"]',
    'button[class*="continue"]',
    'button[class*="submit"]',
    'input[type="submit"]',
]


def _click_sso_button(driver) -> bool:
    """Find and click the 'Sign in with SSO' button on the PitchBook login page."""
    # First try CSS selectors
    for sel in _SEL_SSO_BUTTON:
        try:
            elems = driver.find_elements("css selector", sel)
            for el in elems:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    return True
        except Exception:
            continue

    # Fallback: find any button/link whose text contains "SSO" or "single sign"
    try:
        all_btns = driver.find_elements("css selector", "button, a")
        for el in all_btns:
            text = (el.text or "").lower()
            if "sso" in text or "single sign" in text or "sign in with sso" in text:
                if el.is_displayed():
                    driver.execute_script("arguments[0].click();", el)
                    console.print(f"[dim]Clicked SSO button: '{el.text.strip()}'[/dim]")
                    return True
    except Exception:
        pass

    return False


def _fill_field(driver, selectors: List[str], value: str, label: str = "field") -> bool:
    """Try each CSS selector in order; fill the first visible input found. Returns True on success."""
    for sel in selectors:
        try:
            elems = driver.find_elements("css selector", sel)
            for el in elems:
                if el.is_displayed() and el.is_enabled():
                    el.clear()
                    el.send_keys(value)
                    console.print(f"[dim]  ✓ Filled {label} ({sel})[/dim]")
                    return True
        except Exception:
            continue
    return False


def _click_any(driver, selectors: List[str], label: str = "button") -> bool:
    """Try each CSS selector in order; click the first visible element found. Returns True on success."""
    for sel in selectors:
        try:
            elems = driver.find_elements("css selector", sel)
            for el in elems:
                if el.is_displayed() and el.is_enabled():
                    driver.execute_script("arguments[0].click();", el)
                    console.print(f"[dim]  ✓ Clicked {label} ({sel})[/dim]")
                    return True
        except Exception:
            continue
    return False


def _handle_account_picker(driver, email: str) -> bool:
    """
    Handle the Microsoft/Azure AD 'Pick an account' screen.
    Finds the tile whose text matches the given email and clicks it.
    Returns True if the picker was found and clicked, False if not present.
    """
    try:
        # Check if we're on an account picker page
        page_src = driver.page_source.lower()
        if "pick an account" not in page_src and "choose an account" not in page_src:
            return False

        console.print("[dim]  → Account picker detected — selecting account[/dim]")

        # Try data-email attribute first (most reliable)
        for attr in [f'[data-email="{email}"]', f'[data-email="{email.lower()}"]']:
            elems = driver.find_elements("css selector", attr)
            if elems:
                driver.execute_script("arguments[0].click();", elems[0])
                console.print(f"[dim]    Clicked account tile by data-email[/dim]")
                return True

        # Fallback: find any clickable element whose text contains the email
        candidates = driver.find_elements(
            "css selector",
            "div[role='button'], li[role='option'], .account-tile, [class*='account'], [class*='tile']"
        )
        email_lower = email.lower()
        for el in candidates:
            if email_lower in (el.text or "").lower():
                driver.execute_script("arguments[0].click();", el)
                console.print(f"[dim]    Clicked account tile by text match: {el.text.strip()}[/dim]")
                return True

        console.print("[yellow]    Account picker found but could not click the tile — check browser window[/yellow]")
        return False
    except Exception as exc:
        logger.debug("Account picker handling error: %s", exc)
        return False


def _login_sso(driver, email: str, password: str) -> bool:
    """
    Fully automated SSO login flow for PitchBook.

    Exact flow (IESE / institutional IdP):
      1. PitchBook login page  → click "Sign in with SSO"
      2. PitchBook SSO page    → enter work email → click Continue
      3. IESE / IdP redirect   → enter email again → click Next
      4. IESE / IdP password   → enter password → click Sign In
      5. Redirect back to PitchBook (detected as success)
    """
    from selenium.webdriver.support.ui import WebDriverWait

    console.print(f"[dim]→ Navigating to PitchBook login page[/dim]")
    driver.get(PB_LOGIN_URL)
    time.sleep(PAGE_LOAD_PAUSE + 1)

    # ── Step 1: Click "Sign in with SSO" ──────────────────────────────────
    console.print("[dim]→ Step 1: clicking 'Sign in with SSO' button[/dim]")
    found = _click_sso_button(driver)
    if not found:
        console.print("[yellow]  SSO button not found by selector — trying text scan[/yellow]")
    time.sleep(2)

    # ── Step 2: PitchBook asks for work email to identify IdP ─────────────
    console.print("[dim]→ Step 2: entering work email on PitchBook SSO page[/dim]")
    email_filled = _fill_field(
        driver,
        [
            'input[type="email"]',
            'input[name="email"]',
            'input[placeholder*="email" i]',
            'input[placeholder*="work" i]',
        ],
        email,
        label="SSO email",
    )
    if not email_filled:
        console.print("[yellow]  Could not find SSO email field — page may have changed[/yellow]")

    time.sleep(0.5)
    _click_any(
        driver,
        ['button[type="submit"]', 'button[class*="continue" i]',
         'button[class*="submit" i]', 'input[type="submit"]',
         'button[class*="next" i]', 'button:last-of-type'],
        label="Continue/Submit on PitchBook",
    )

    # ── Step 3: Wait for IdP redirect (IESE / institutional login page) ───
    console.print("[dim]→ Step 3: waiting for redirect to institutional IdP (IESE)…[/dim]")
    try:
        WebDriverWait(driver, 20).until(
            lambda d: "pitchbook.com" not in d.current_url
        )
        idp_url = driver.current_url
        console.print(f"[dim]  Redirected to IdP: {idp_url}[/dim]")
    except Exception:
        idp_url = driver.current_url
        console.print(f"[yellow]  No redirect detected yet, current URL: {idp_url}[/yellow]")

    time.sleep(2)

    # ── Step 3b: Handle Microsoft "Pick an account" screen ────────────────
    # IESE uses Azure AD / Microsoft which shows a "Pick an account" dialog
    # when the account is already known. We click the matching email.
    _handle_account_picker(driver, email)

    time.sleep(1.5)

    # ── Step 4a: Enter email on IdP page (if not pre-selected) ───────────
    console.print("[dim]→ Step 4a: entering email on IdP (IESE) page[/dim]")
    _fill_field(
        driver,
        [
            'input[type="email"]',
            'input[name="email"]',
            'input[name="username"]',
            'input[name="loginfmt"]',          # Azure AD / Microsoft
            'input[id*="email" i]',
            'input[id*="username" i]',
            'input[placeholder*="email" i]',
            'input[placeholder*="user" i]',
        ],
        email,
        label="IdP email",
    )

    time.sleep(0.5)

    # Click Next/Continue on IdP — Microsoft splits email and password into
    # two separate pages
    clicked_next = _click_any(
        driver,
        [
            '#idSIButton9',                    # Microsoft "Next" button
            'input[type="submit"]',
            'button[type="submit"]',
            'button[id*="next" i]',
            'button[id*="submit" i]',
            'button[class*="submit" i]',
            'button[class*="next" i]',
            'button[class*="continue" i]',
        ],
        label="Next on IdP",
    )

    if clicked_next:
        time.sleep(2.5)
        # Second account picker may appear after entering email
        _handle_account_picker(driver, email)
        time.sleep(1)

    # ── Step 4b: Enter password on IdP page ───────────────────────────────
    console.print("[dim]→ Step 4b: entering password on IdP (IESE) page[/dim]")
    pw_filled = _fill_field(
        driver,
        [
            'input[type="password"]',
            'input[name="password"]',
            'input[name="passwd"]',            # Microsoft
            'input[id*="password" i]',
        ],
        password,
        label="IdP password",
    )

    if not pw_filled:
        console.print("[yellow]  Password field not found — retrying after short wait[/yellow]")
        time.sleep(2.5)
        _fill_field(
            driver,
            ['input[type="password"]', 'input[name="password"]', 'input[name="passwd"]'],
            password,
            label="IdP password (retry)",
        )

    time.sleep(0.5)

    _click_any(
        driver,
        [
            '#idSIButton9',                    # Microsoft "Sign in"
            'input[type="submit"]',
            'button[type="submit"]',
            'button[id*="sign" i]',
            'button[class*="submit" i]',
            'button[class*="login" i]',
        ],
        label="Sign In on IdP",
    )

    time.sleep(1.5)
    # "Stay signed in?" prompt — click Yes to extend the session
    _click_any(
        driver,
        ['#idSIButton9', 'button[id*="yes" i]', 'input[value*="Yes" i]'],
        label="Stay signed in",
    )

    # ── Step 5: Wait for redirect back to PitchBook ───────────────────────
    console.print("[dim]→ Step 5: waiting for redirect back to PitchBook…[/dim]")
    try:
        WebDriverWait(driver, 60).until(
            lambda d: (
                "pitchbook.com" in d.current_url
                and "/login" not in d.current_url
                and "/sso" not in d.current_url.lower()
            )
        )
        console.print(f"[green]SSO login successful → {driver.current_url}[/green]")
        return True
    except Exception:
        current = driver.current_url
        console.print(f"[yellow]Timeout waiting for PitchBook. Current URL: {current}[/yellow]")
        if "pitchbook.com" in current and "/login" not in current:
            console.print("[green]Appears logged in — continuing[/green]")
            return True
        console.print(
            "[red]SSO failed. Possible causes:\n"
            "  • Wrong PITCHBOOK_EMAIL / PITCHBOOK_PASSWORD\n"
            "  • MFA required — run with --no-headless and complete manually\n"
            "  • IdP page structure changed — check browser window[/red]"
        )
        return False


def _login(driver, email: str, password: str, use_sso: bool = False) -> bool:
    """
    Unified login dispatcher.

    When use_sso=False  : try saved cookies → email/password login
    When use_sso=True   : try saved cookies → fully automated SSO flow
                          (cookies skip login on subsequent runs)
    """
    # Always try saved session first — works regardless of how it was originally obtained
    if _load_cookies(driver):
        return True

    if use_sso:
        return _login_sso(driver, email, password)
    return _login_password(driver, email, password)


# ---------------------------------------------------------------------------
# LP Search navigation + filter application
# ---------------------------------------------------------------------------

# PitchBook LP Search table row selectors (multiple fallbacks for DOM changes)
_SEL_TABLE_ROWS = [
    'tr[class*="ResultsRow"]',
    'tr[class*="results-row"]',
    'tr[data-testid*="row"]',
    'table tbody tr',
    '[class*="TableRow"]',
    '[class*="table-row"]',
    '[class*="lp-row"]',
]

# Within a row — field selectors
_SEL_ROW_NAME = [
    'td:first-child a',
    'td:first-child span',
    'td[class*="name"] a',
    'td[class*="name"]',
    '[data-testid="lp-name"]',
    'a[href*="/profiles/investor"]',
    'a[href*="/lp/"]',
]

_SEL_ROW_TYPE = [
    'td[class*="type"]',
    'td[class*="investor-type"]',
    '[data-testid="investor-type"]',
    'td:nth-child(2)',
]

_SEL_ROW_HQ = [
    'td[class*="hq"]',
    'td[class*="location"]',
    'td[class*="country"]',
    '[data-testid="hq-location"]',
    'td:nth-child(3)',
]

_SEL_ROW_AUM = [
    'td[class*="aum"]',
    'td[class*="assets"]',
    '[data-testid="aum"]',
    'td:nth-child(4)',
]

_SEL_NEXT_PAGE = [
    'button[aria-label="Next page"]',
    'button[aria-label="next"]',
    'a[aria-label="Next page"]',
    '[data-testid="pagination-next"]',
    'button[class*="next"]',
    'li[class*="next"] a',
    'button:has(svg[class*="chevron-right"])',
    '.pagination-next',
    'button[title="Next"]',
]


def _find_rows(driver) -> List[Any]:
    for sel in _SEL_TABLE_ROWS:
        try:
            elems = driver.find_elements("css selector", sel)
            # Filter out header rows (no meaningful text in first cell)
            data_rows = [
                e for e in elems
                if e.find_elements("css selector", "td") and e.text.strip()
            ]
            if data_rows:
                return data_rows
        except Exception:
            continue
    return []


def _extract_row(row_element) -> Optional[PitchBookLPRecord]:
    try:
        name = _first_text(row_element, _SEL_ROW_NAME)
        if not name:
            name = row_element.text.split("\n")[0].strip()
        if not name or len(name) < 2:
            return None

        # Get profile URL
        href = _first_attr(row_element, _SEL_ROW_NAME, "href")
        if href and not href.startswith("http"):
            href = PB_BASE_URL + href
        pitchbook_url = href

        investor_type = _first_text(row_element, _SEL_ROW_TYPE)
        hq_raw = _first_text(row_element, _SEL_ROW_HQ)

        # Split "City, Country" HQ strings
        hq_city, hq_country = None, None
        if hq_raw:
            parts = [p.strip() for p in hq_raw.split(",")]
            if len(parts) >= 2:
                hq_city = parts[0]
                hq_country = parts[-1]
            else:
                hq_country = hq_raw.strip()

        aum_usd = _first_text(row_element, _SEL_ROW_AUM)

        return PitchBookLPRecord(
            investor_name=name,
            investor_type=investor_type,
            hq_country=hq_country,
            hq_city=hq_city,
            aum_usd=aum_usd,
            pitchbook_url=pitchbook_url,
        )
    except Exception as exc:
        logger.debug("Row parse error: %s", exc)
        return None


def _click_next_page(driver) -> bool:
    """Click the next-page button. Returns True if clicked, False if not found/disabled."""
    for sel in _SEL_NEXT_PAGE:
        try:
            elems = driver.find_elements("css selector", sel)
            for el in elems:
                if el.is_displayed() and el.is_enabled():
                    # Check it's not a disabled next button
                    aria_disabled = el.get_attribute("aria-disabled")
                    class_attr = el.get_attribute("class") or ""
                    if aria_disabled == "true" or "disabled" in class_attr:
                        return False
                    driver.execute_script("arguments[0].click();", el)
                    return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Collect LPs from search
# ---------------------------------------------------------------------------

def _collect_lps(driver, max_lps: int) -> List[PitchBookLPRecord]:
    console.print(f"[dim]Loading LP search: {PB_LP_SEARCH_URL}[/dim]")
    driver.get(PB_LP_SEARCH_URL)
    time.sleep(PAGE_LOAD_PAUSE)

    seen_names: Set[str] = set()
    records: List[PitchBookLPRecord] = []
    stale_pages = 0
    page_num = 1

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Collecting LPs from PitchBook…", total=max_lps)

        while len(records) < max_lps and stale_pages < MAX_STALE_PAGES:
            time.sleep(BETWEEN_PAGES_PAUSE)
            rows = _find_rows(driver)

            if not rows:
                console.print(
                    f"[yellow]Page {page_num}: no rows found — "
                    "PitchBook DOM may have changed or Cloudflare is blocking.[/yellow]"
                )
                stale_pages += 1
                if stale_pages == 1:
                    # Wait longer and retry once
                    time.sleep(5)
                    rows = _find_rows(driver)
                if not rows:
                    break

            new_this_page = 0
            for row in rows:
                if len(records) >= max_lps:
                    break
                rec = _extract_row(row)
                if rec and rec.investor_name not in seen_names:
                    seen_names.add(rec.investor_name)
                    records.append(rec)
                    new_this_page += 1
                    progress.advance(task)

            logger.debug("Page %d: %d new LPs extracted", page_num, new_this_page)

            if new_this_page == 0:
                stale_pages += 1
            else:
                stale_pages = 0

            if len(records) >= max_lps:
                break

            if not _click_next_page(driver):
                console.print(f"[dim]No more pages (stopped at page {page_num})[/dim]")
                break

            page_num += 1
            time.sleep(PAGE_LOAD_PAUSE)

    console.print(f"[bold]Collected {len(records)} unique LPs from PitchBook ({page_num} page(s))[/bold]")
    return records


# ---------------------------------------------------------------------------
# Gate integration
# ---------------------------------------------------------------------------

def _run_gate_for_record(
    con,
    record: PitchBookLPRecord,
    delay_ms: int,
) -> Optional[Dict[str, Any]]:
    from contra.gate.runner import run_gate

    try:
        analyst_facts = record.to_analyst_facts()
        result = run_gate(
            con,
            name=record.investor_name,
            analyst_facts=analyst_facts,
            compact_web=True,
        )
        return {
            "name": record.investor_name,
            "type": record.investor_type,
            "hq": record.hq_country,
            "verdict": result.assessment.recommendation,
            "yes": result.yes,
            "review": result.is_review,
            "confidence": result.confidence,
            "summary": result.summary or "",
        }
    except Exception as exc:
        logger.warning("Gate error for %s: %s", record.investor_name, exc)
        return {
            "name": record.investor_name,
            "type": record.investor_type,
            "hq": record.hq_country,
            "verdict": "error",
            "yes": False,
            "review": False,
            "confidence": "n/a",
            "summary": str(exc),
        }
    finally:
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_scrape(
    max_lps: int = 200,
    headless: bool = True,
    dry_run: bool = False,
    delay_ms: int = 2000,
    use_sso: bool = False,
    clear_session: bool = False,
    use_brave: bool = False,
    connect_port: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Full PitchBook LP discovery pipeline.

    Parameters
    ----------
    max_lps        : stop after collecting this many LPs
    headless       : run Chrome headlessly (ignored for Brave/SSO modes)
    dry_run        : collect + dedup but skip gate calls and CRM writes
    delay_ms       : pause between gate calls in milliseconds
    use_sso        : automated SSO login via IESE / institutional IdP
    clear_session  : delete saved session cookies and force a fresh login
    use_brave      : use the existing Brave browser profile (already logged in —
                     no login step needed; Brave must not be running beforehand)
    connect_port   : attach to an already-running Brave/Chrome instance via CDP
                     (launch Brave with --remote-debugging-port=<port> first)

    Returns a summary dict with counts: scraped, skipped_crm, skipped_fuzzy,
    skipped_checkpoint, gated, yes, review, no, errors.
    """
    from agents.db import get_conn

    email = os.getenv("PITCHBOOK_EMAIL", "").strip()
    password = os.getenv("PITCHBOOK_PASSWORD", "").strip()

    brave_mode = use_brave or (connect_port is not None)

    if not brave_mode:
        if not email:
            raise RuntimeError("PITCHBOOK_EMAIL must be set in .env before running.")
        if not use_sso and not password:
            raise RuntimeError(
                "PITCHBOOK_PASSWORD must be set in .env, or use --sso for SSO login."
            )

    # Wipe saved session if caller requested a fresh auth
    if clear_session and COOKIE_PATH.exists():
        COOKIE_PATH.unlink()
        console.print("[dim]Cleared saved session cookies — will authenticate fresh[/dim]")

    # Determine effective headless flag
    effective_headless = headless and not use_sso and not brave_mode

    checkpoint: Set[str] = _load_checkpoint()
    if checkpoint:
        console.print(f"[dim]Checkpoint: {len(checkpoint)} names already processed, will skip[/dim]")

    con = get_conn()
    known_names = _load_known_names(con)
    console.print(f"[dim]Loaded {len(known_names)} known allocator/CRM names for fuzzy dedup[/dim]")

    # ── Build the right driver ────────────────────────────────────────────────
    if brave_mode:
        console.print("[bold cyan]Using Brave browser (existing session — no login needed)[/bold cyan]")
        driver = _build_brave_driver(connect_port=connect_port)
    else:
        driver = _build_driver(headless=effective_headless)

    records: List[PitchBookLPRecord] = []
    try:
        if brave_mode:
            # Navigate to PitchBook — session may or may not still be alive
            console.print("[dim]Navigating to PitchBook to check session…[/dim]")
            driver.get(PB_BASE_URL)
            time.sleep(PAGE_LOAD_PAUSE)

            if "/login" in driver.current_url:
                # Session expired — log in via SSO in this Brave window
                console.print(
                    "[yellow]PitchBook session expired — running SSO login in Brave…[/yellow]"
                )
                if not email:
                    driver.quit()
                    raise RuntimeError("PITCHBOOK_EMAIL must be set in .env for SSO login.")
                if not _login_sso(driver, email, password):
                    driver.quit()
                    raise RuntimeError(
                        "SSO login failed inside Brave.\n"
                        "Check PITCHBOOK_EMAIL / PITCHBOOK_PASSWORD in .env."
                    )
            else:
                console.print(f"[green]Session active — already logged in[/green]")

            _save_cookies(driver)
        else:
            if not _login(driver, email, password, use_sso=use_sso):
                driver.quit()
                raise RuntimeError(
                    "PitchBook login failed. "
                    + ("SSO credentials wrong or timed out."
                       if use_sso else "Check PITCHBOOK_EMAIL / PITCHBOOK_PASSWORD in .env.")
                )
            _save_cookies(driver)

        time.sleep(2)
        records = _collect_lps(driver, max_lps)
    finally:
        driver.quit()

    stats: Dict[str, int] = {
        "scraped": len(records),
        "skipped_checkpoint": 0,
        "skipped_crm": 0,
        "skipped_fuzzy": 0,
        "gated": 0,
        "yes": 0,
        "review": 0,
        "no": 0,
        "errors": 0,
    }
    results_table: List[Dict[str, Any]] = []

    console.print(f"\n[bold]Running LP Gate on {len(records)} scraped LPs…[/bold]")
    if dry_run:
        console.print("[yellow]DRY RUN — gate will run but CRM writes are skipped[/yellow]")

    for i, record in enumerate(records, 1):
        name = record.investor_name

        if name in checkpoint:
            stats["skipped_checkpoint"] += 1
            logger.debug("Checkpoint skip: %s", name)
            continue

        if _is_crm_duplicate(con, name):
            stats["skipped_crm"] += 1
            checkpoint.add(name)
            console.print(f"  [dim][{i}/{len(records)}] SKIP (in CRM): {name}[/dim]")
            continue

        is_fuzzy, matched = _is_fuzzy_duplicate(name, known_names)
        if is_fuzzy:
            stats["skipped_fuzzy"] += 1
            checkpoint.add(name)
            console.print(
                f"  [dim][{i}/{len(records)}] SKIP (fuzzy≥{FUZZY_THRESHOLD} → '{matched}'): {name}[/dim]"
            )
            continue

        stats["gated"] += 1
        if dry_run:
            console.print(f"  [cyan][{i}/{len(records)}] DRY RUN gate: {name}[/cyan]")
            checkpoint.add(name)
            continue

        res = _run_gate_for_record(con, record, delay_ms)
        if res:
            verdict = res["verdict"]
            checkpoint.add(name)
            results_table.append(res)

            hq_tag = f" [{res['hq']}]" if res.get("hq") else ""
            if verdict == "yes":
                stats["yes"] += 1
                known_names.append(name)
                console.print(
                    f"  [green][{i}/{len(records)}] YES  ({res['confidence']}): {name}{hq_tag}[/green]"
                )
            elif verdict == "review":
                stats["review"] += 1
                known_names.append(name)
                console.print(
                    f"  [yellow][{i}/{len(records)}] REVIEW ({res['confidence']}): {name}{hq_tag}[/yellow]"
                )
            elif verdict == "error":
                stats["errors"] += 1
                console.print(
                    f"  [red][{i}/{len(records)}] ERROR: {name} — {res['summary'][:80]}[/red]"
                )
            else:
                stats["no"] += 1
                console.print(
                    f"  [dim][{i}/{len(records)}] NO   ({res['confidence']}): {name}{hq_tag}[/dim]"
                )

        _save_checkpoint(checkpoint)

    con.close()
    _print_summary(stats, results_table)
    return stats


def _print_summary(stats: Dict[str, int], results: List[Dict[str, Any]]) -> None:
    console.print()
    table = Table(title="PitchBook Scrape Summary", show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="dim")
    table.add_column("Count", justify="right")

    table.add_row("Scraped from PitchBook", str(stats["scraped"]))
    table.add_row("Skipped (already in CRM)", str(stats["skipped_crm"]))
    table.add_row(f"Skipped (fuzzy ≥{FUZZY_THRESHOLD})", str(stats["skipped_fuzzy"]))
    table.add_row("Skipped (checkpoint)", str(stats["skipped_checkpoint"]))
    table.add_row("Gated (new)", str(stats["gated"]))
    table.add_row("[green]Gate YES[/green]", str(stats["yes"]))
    table.add_row("[yellow]Gate REVIEW[/yellow]", str(stats["review"]))
    table.add_row("[dim]Gate NO[/dim]", str(stats["no"]))
    table.add_row("[red]Errors[/red]", str(stats["errors"]))
    console.print(table)

    if results:
        passed = [r for r in results if r["verdict"] in ("yes", "review")]
        if passed:
            console.print()
            detail = Table(title="Saved LPs (YES / REVIEW)", show_header=True)
            detail.add_column("Investor", style="bold")
            detail.add_column("Type")
            detail.add_column("HQ")
            detail.add_column("Verdict")
            detail.add_column("Confidence")
            detail.add_column("Summary")
            for r in passed:
                verdict_str = "[green]YES[/green]" if r["verdict"] == "yes" else "[yellow]REVIEW[/yellow]"
                detail.add_row(
                    r["name"],
                    r.get("type") or "—",
                    r.get("hq") or "—",
                    verdict_str,
                    r.get("confidence", "—"),
                    (r.get("summary") or "")[:60],
                )
            console.print(detail)


# ---------------------------------------------------------------------------
# Direct run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Scrape PitchBook LP search into FundingStack CRM")
    parser.add_argument("--max", type=int, default=200, help="Max LPs to collect (default 200)")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Show browser window")
    parser.add_argument("--dry-run", action="store_true", help="Collect + dedup but skip gate + CRM writes")
    parser.add_argument("--delay-ms", type=int, default=2000, help="Pause between gate calls in ms")
    args = parser.parse_args()

    run_scrape(
        max_lps=args.max,
        headless=args.headless,
        dry_run=args.dry_run,
        delay_ms=args.delay_ms,
    )
