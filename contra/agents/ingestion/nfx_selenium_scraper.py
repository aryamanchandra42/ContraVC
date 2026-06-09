"""
NFX Signal investor scraper — https://signal.nfx.com/investors

Scrolls the NFX Signal investor list, extracts each profile into an
NfxInvestorRecord, deduplicates against the CRM + allocators database via
three layers (exact CRM match → fuzzy allocator match → checkpoint), then
runs every genuinely new investor through the LP Gate.  Investors that pass
(YES / REVIEW) are persisted to contra.duckdb automatically (the gate runner
calls persist_gate_findings internally).

Entry points
------------
- CLI:   contra nfx-scrape  (added to contra/contra/cli.py)
- Direct: python -m agents.ingestion.nfx_selenium_scraper
"""

from __future__ import annotations

import json
import logging
import os
import time
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

NFX_BASE_URL = "https://signal.nfx.com"
NFX_INVESTORS_URL = f"{NFX_BASE_URL}/investors"
NFX_LOGIN_URL = f"{NFX_BASE_URL}/login"

CHECKPOINT_PATH = Path(__file__).resolve().parent.parent.parent / "processed_data" / "nfx_scrape_checkpoint.json"

# Fuzzy-match threshold: 0–100; ≥85 → treat as known duplicate
FUZZY_THRESHOLD = 85

# How long to wait (seconds) for JS to render after scroll
SCROLL_PAUSE = 2.0

# Max scroll attempts without finding new cards before giving up
MAX_STALE_SCROLLS = 6

# ---------------------------------------------------------------------------
# Selenium selectors (NFX Signal is a React SPA — selectors may need updating
# if the site changes.  Multiple fallbacks are tried in order.)
# ---------------------------------------------------------------------------

# Login page
_SEL_EMAIL = 'input[type="email"], input[name="email"], #email'
_SEL_PASSWORD = 'input[type="password"], input[name="password"], #password'
_SEL_SUBMIT = 'button[type="submit"], button.btn-primary, input[type="submit"]'

# Investor list cards — try in order until one returns results
_SEL_CARDS = [
    '[data-testid="investor-card"]',
    '[data-testid="lp-card"]',
    '.investor-card',
    '.lp-card',
    'article[class*="investor"]',
    'article[class*="card"]',
    'div[class*="InvestorCard"]',
    'div[class*="investor-card"]',
    'div[class*="LpCard"]',
    # broad fallback: any anchor whose href matches /investors/
    'a[href*="/investors/"]',
]

# Within a card — fields
_SEL_NAME = [
    '[data-testid="investor-name"]',
    '[data-testid="name"]',
    'h2', 'h3',
    '.investor-name',
    '.name',
    'strong',
]
_SEL_FIRM = [
    '[data-testid="firm-name"]',
    '[data-testid="firm"]',
    '.firm-name',
    '.firm',
    'span[class*="firm"]',
]
_SEL_LINK = [
    'a[href*="/investors/"]',
    'a[href*="/lps/"]',
    'a',
]
_SEL_CHECK = [
    '[data-testid="check-size"]',
    '[data-testid="sweet-spot"]',
    '.check-size',
    'span[class*="check"]',
    'span[class*="sweet"]',
]
_SEL_LOCATION = [
    '[data-testid="location"]',
    '[data-testid="geo"]',
    '.location',
    '.geo',
    'span[class*="location"]',
    'span[class*="geo"]',
]


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> Set[str]:
    """Return set of investor names already processed in previous runs."""
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
# Deduplication helpers
# ---------------------------------------------------------------------------

def _load_known_names(con) -> List[str]:
    """Load all canonical allocator names for fuzzy-match dedup."""
    rows = con.execute("SELECT canonical_name FROM allocators").fetchall()
    names = [r[0] for r in rows if r[0]]
    crm_rows = con.execute("SELECT investor_name FROM crm_leads").fetchall()
    names.extend(r[0] for r in crm_rows if r[0])
    return names


def _is_crm_duplicate(con, name: str) -> bool:
    """Layer 1 — exact/near-exact CRM lookup."""
    from contra.intelligence.brief import _crm_lookup  # local import to avoid circular
    in_crm, _ = _crm_lookup(con, name)
    return in_crm


def _is_fuzzy_duplicate(name: str, known_names: List[str]) -> Tuple[bool, Optional[str]]:
    """Layer 2 — fuzzy match against all known allocator + CRM names."""
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
# Selenium helpers
# ---------------------------------------------------------------------------

def _build_driver(headless: bool = True):
    """Build a Chrome WebDriver with webdriver-manager auto-download."""
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


def _wait_for(driver, css: str, timeout: float = 15.0):
    """Wait until at least one element matching css appears. Returns elements list."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, css))
    )
    return driver.find_elements("css selector", css)


def _first_text(element, selectors: List[str]) -> Optional[str]:
    """Try each CSS selector within element; return first non-empty text found."""
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
    """Try each CSS selector within element; return first non-empty attribute value."""
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
# Login
# ---------------------------------------------------------------------------

def _login(driver, username: str, password: str) -> bool:
    """Navigate to login page and authenticate. Returns True on success."""
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    console.print(f"[dim]Navigating to login: {NFX_LOGIN_URL}[/dim]")
    driver.get(NFX_LOGIN_URL)
    time.sleep(2)

    # Fill email
    try:
        elems = _wait_for(driver, _SEL_EMAIL, timeout=15)
        elems[0].clear()
        elems[0].send_keys(username)
    except Exception as exc:
        console.print(f"[red]Could not find email field: {exc}[/red]")
        return False

    # Fill password
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

    # Submit
    try:
        submit_elems = driver.find_elements("css selector", _SEL_SUBMIT)
        if not submit_elems:
            console.print("[red]Could not find submit button[/red]")
            return False
        submit_elems[0].click()
    except Exception as exc:
        console.print(f"[red]Submit failed: {exc}[/red]")
        return False

    # Wait for redirect away from login page (up to 20s)
    try:
        WebDriverWait(driver, 20).until(
            lambda d: "/login" not in d.current_url
        )
        console.print("[green]Login successful[/green]")
        return True
    except Exception:
        # May still be on login — check for error messages
        page = driver.page_source.lower()
        if "invalid" in page or "incorrect" in page or "wrong" in page:
            console.print("[red]Login failed — check NFX_USERNAME / NFX_PASSWORD[/red]")
        else:
            console.print("[yellow]Login redirect not detected; proceeding anyway[/yellow]")
        return True  # optimistic — let next step fail if truly not logged in


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------

def _find_cards(driver) -> List[Any]:
    """Try each card selector until one returns results."""
    for sel in _SEL_CARDS:
        try:
            elems = driver.find_elements("css selector", sel)
            if elems:
                return elems
        except Exception:
            continue
    return []


def _parse_check_size(raw: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse a raw check-size string like '$50K–$500K' or '$1M sweet spot'.
    Returns (sweet_spot, check_min, check_max).
    """
    if not raw:
        return None, None, None

    import re

    raw = raw.strip()

    # Range: $X–$Y or $X-$Y or $X to $Y
    range_match = re.search(
        r"\$?([\d,.]+\s*[KkMmBb]?)\s*(?:–|-|to)\s*\$?([\d,.]+\s*[KkMmBb]?)",
        raw,
        re.IGNORECASE,
    )
    if range_match:
        lo = "$" + range_match.group(1).strip().replace(" ", "")
        hi = "$" + range_match.group(2).strip().replace(" ", "")
        return None, lo, hi

    # Single value
    single = re.search(r"\$?([\d,.]+\s*[KkMmBb]?)", raw, re.IGNORECASE)
    if single:
        val = "$" + single.group(1).strip().replace(" ", "")
        return val, None, None

    return raw, None, None


def _extract_card(element) -> Optional["NfxInvestorRecord"]:  # noqa: F821
    """Extract one investor record from a card WebElement. Returns None on failure."""
    from contra.gate.batch_models import NfxInvestorRecord  # local import

    try:
        name = _first_text(element, _SEL_NAME)
        if not name:
            # Try the element's own text as last resort
            name = element.text.split("\n")[0].strip()
        if not name:
            return None

        firm = _first_text(element, _SEL_FIRM)

        # Profile URL
        href = _first_attr(element, _SEL_LINK, "href")
        if href and not href.startswith("http"):
            href = NFX_BASE_URL + href
        nfx_url = href

        # Check size
        raw_check = _first_text(element, _SEL_CHECK)
        sweet_spot, check_min, check_max = _parse_check_size(raw_check)

        # Locations
        locations = _first_text(element, _SEL_LOCATION)

        return NfxInvestorRecord(
            investor_name=name,
            firm_name=firm,
            nfx_url=nfx_url,
            sweet_spot=sweet_spot,
            check_min=check_min,
            check_max=check_max,
            locations=locations,
        )
    except Exception as exc:
        logger.debug("Card parse error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Scroll + collect
# ---------------------------------------------------------------------------

def _collect_investors(driver, max_investors: int) -> List["NfxInvestorRecord"]:  # noqa: F821
    """
    Scroll the NFX investors page, extract records until max_investors is reached
    or no new cards are found after MAX_STALE_SCROLLS consecutive scrolls.
    """
    console.print(f"[dim]Loading: {NFX_INVESTORS_URL}[/dim]")
    driver.get(NFX_INVESTORS_URL)
    time.sleep(3)

    seen_names: Set[str] = set()
    records: List[Any] = []
    stale_count = 0
    last_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scrolling investors…", total=max_investors)

        while len(records) < max_investors and stale_count < MAX_STALE_SCROLLS:
            cards = _find_cards(driver)
            new_this_scroll = 0

            for card in cards:
                if len(records) >= max_investors:
                    break
                rec = _extract_card(card)
                if rec and rec.investor_name not in seen_names:
                    seen_names.add(rec.investor_name)
                    records.append(rec)
                    new_this_scroll += 1
                    progress.advance(task)

            if new_this_scroll == 0 and len(records) == last_count:
                stale_count += 1
            else:
                stale_count = 0

            last_count = len(records)

            # Scroll down
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(SCROLL_PAUSE)

    console.print(f"[bold]Collected {len(records)} unique investors from NFX Signal[/bold]")
    return records


# ---------------------------------------------------------------------------
# Gate + CRM persist
# ---------------------------------------------------------------------------

def _run_gate_for_record(
    con,
    record: "NfxInvestorRecord",  # noqa: F821
    delay_ms: int,
) -> Optional[Dict[str, Any]]:
    """Run the LP Gate for one record. Returns a result summary dict or None on error."""
    from contra.gate.runner import run_gate

    try:
        analyst_facts = record.to_analyst_facts()
        result = run_gate(
            con,
            name=record.investor_name,
            analyst_facts=analyst_facts,
            nfx_url=record.nfx_url,
            compact_web=True,
            screening_mode="nfx_individual",
        )
        return {
            "name": record.investor_name,
            "firm": record.firm_name,
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
            "firm": record.firm_name,
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
# Main scrape function
# ---------------------------------------------------------------------------

def run_scrape(
    max_investors: int = 500,
    headless: bool = True,
    dry_run: bool = False,
    delay_ms: int = 1200,
) -> Dict[str, Any]:
    """
    Full scrape pipeline.

    Returns a summary dict with counts: scraped, skipped_crm, skipped_fuzzy,
    skipped_checkpoint, gated, yes, review, no, errors.
    """
    from agents.db import get_conn

    username = os.getenv("NFX_USERNAME", "").strip()
    password = os.getenv("NFX_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError(
            "NFX_USERNAME and NFX_PASSWORD must be set in .env before running the scraper."
        )

    # --- Checkpoint (Layer 3 dedup: already processed in prior runs) ----------
    checkpoint: Set[str] = _load_checkpoint()
    if checkpoint:
        console.print(f"[dim]Checkpoint: {len(checkpoint)} names already processed, will skip[/dim]")

    # --- DuckDB connection (writable for gate persist) -----------------------
    con = get_conn()
    known_names = _load_known_names(con)
    console.print(f"[dim]Loaded {len(known_names)} known allocator/CRM names for fuzzy dedup[/dim]")

    # --- Selenium setup + login + collect ------------------------------------
    driver = _build_driver(headless=headless)
    records: List[Any] = []
    try:
        if not _login(driver, username, password):
            driver.quit()
            raise RuntimeError("NFX login failed — check credentials.")
        time.sleep(2)
        records = _collect_investors(driver, max_investors)
    finally:
        driver.quit()

    # --- Gate each new investor ---------------------------------------------
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

    console.print(f"\n[bold]Running LP Gate on {len(records)} scraped investors…[/bold]")
    if dry_run:
        console.print("[yellow]DRY RUN — gate will run but CRM writes are skipped[/yellow]")

    for i, record in enumerate(records, 1):
        name = record.investor_name

        # Layer 3: checkpoint
        if name in checkpoint:
            stats["skipped_checkpoint"] += 1
            logger.debug("Checkpoint skip: %s", name)
            continue

        # Layer 1: CRM exact match
        if _is_crm_duplicate(con, name):
            stats["skipped_crm"] += 1
            checkpoint.add(name)
            console.print(f"  [dim][{i}/{len(records)}] SKIP (in CRM): {name}[/dim]")
            continue

        # Layer 2: fuzzy match against known allocators
        is_fuzzy, matched = _is_fuzzy_duplicate(name, known_names)
        if is_fuzzy:
            stats["skipped_fuzzy"] += 1
            checkpoint.add(name)
            console.print(
                f"  [dim][{i}/{len(records)}] SKIP (fuzzy≥{FUZZY_THRESHOLD} → '{matched}'): {name}[/dim]"
            )
            continue

        # Gate run
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

            if verdict == "yes":
                stats["yes"] += 1
                known_names.append(name)  # prevent same run fuzzy false-positives
                console.print(
                    f"  [green][{i}/{len(records)}] YES  ({res['confidence']}): {name}[/green]"
                )
            elif verdict == "review":
                stats["review"] += 1
                known_names.append(name)
                console.print(
                    f"  [yellow][{i}/{len(records)}] REVIEW ({res['confidence']}): {name}[/yellow]"
                )
            elif verdict == "error":
                stats["errors"] += 1
                console.print(
                    f"  [red][{i}/{len(records)}] ERROR: {name} — {res['summary'][:80]}[/red]"
                )
            else:
                stats["no"] += 1
                console.print(
                    f"  [dim][{i}/{len(records)}] NO   ({res['confidence']}): {name}[/dim]"
                )

        # Save checkpoint after every investor so we can safely interrupt
        _save_checkpoint(checkpoint)

    con.close()

    # --- Summary table -------------------------------------------------------
    _print_summary(stats, results_table)
    return stats


def _print_summary(stats: Dict[str, int], results: List[Dict[str, Any]]) -> None:
    console.print()
    table = Table(title="NFX Scrape Summary", show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="dim")
    table.add_column("Count", justify="right")

    table.add_row("Scraped from NFX", str(stats["scraped"]))
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
            detail = Table(title="Saved Investors (YES / REVIEW)", show_header=True)
            detail.add_column("Investor", style="bold")
            detail.add_column("Firm")
            detail.add_column("Verdict")
            detail.add_column("Confidence")
            detail.add_column("Summary")
            for r in passed:
                verdict_str = f"[green]YES[/green]" if r["verdict"] == "yes" else "[yellow]REVIEW[/yellow]"
                detail.add_row(
                    r["name"],
                    r.get("firm") or "—",
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

    parser = argparse.ArgumentParser(description="Scrape NFX Signal investors into FundingStack CRM")
    parser.add_argument("--max", type=int, default=500, help="Max investors to scrape (default 500)")
    parser.add_argument("--no-headless", dest="headless", action="store_false", help="Show browser window")
    parser.add_argument("--dry-run", action="store_true", help="Scrape + dedup but skip gate + CRM writes")
    parser.add_argument("--delay-ms", type=int, default=1200, help="Pause between gate calls in ms")
    args = parser.parse_args()

    run_scrape(
        max_investors=args.max,
        headless=args.headless,
        dry_run=args.dry_run,
        delay_ms=args.delay_ms,
    )
