"""
LinkedIn Top-3 URL Scraper
==========================
- Gold clients     → search by name only
- Diamond/Emerald  → search by name + company

For each row:
  1. Build the search query
  2. Open LinkedIn people search
  3. Collect all /in/ profile links → keep top 3
  4. Write result to CSV immediately
  5. Move to next row

Setup:
    pip install playwright pandas
    playwright install chromium

Run:
    python linkedin_scraper.py                        (all rows)
    python linkedin_scraper.py --start 0 --end 20    (first 20)
    python linkedin_scraper.py --start 20             (resume from row 20)
"""

import re
import csv
import json
import time
import random
import logging
import argparse
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Settings ──────────────────────────────────────────────────────────────────
INPUT_FILE      = "client_names.csv"
OUTPUT_FILE     = "linkedin_results.csv"
AUTH_STATE_FILE = "li_auth_state.json"
COOKIES_FILE    = "li_cookies.json"
LOG_FILE        = "linkedin_scraper.log"
PAGE_SETTLE_SEC = 4      # seconds to wait after page load
DELAY_MIN_SEC   = 4      # min pause between rows
DELAY_MAX_SEC   = 8      # max pause between rows

JUNK_COMPANIES = {
    "", "-", "n/a", "na", "nil", "none", "self", "self employed",
    "housewife", "retired", "student", "removed from sow list",
}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Output columns ────────────────────────────────────────────────────────────
OUT_COLS = [
    "client_code", "ClientName", "clean_name", "affluence_bucket",
    "company_names", "email", "mobile_no", "pan_no", "address1",
    "li_1_url", "li_2_url", "li_3_url",
    "search_status", "keywords_used",
]

# ── Skip patterns ─────────────────────────────────────────────────────────────
SKIP_RE = re.compile(
    r"\b(HUF|R\.?W\.?A\.?|TRUST|SOCIETY|FOUNDATION|CHARITABLE|DECEASED)\b",
    re.IGNORECASE,
)


def should_skip(name: str) -> bool:
    return len(name.strip()) < 3 or bool(SKIP_RE.search(name))


# ── Query builder ─────────────────────────────────────────────────────────────
def build_query(name: str, company: str, bucket: str) -> str:
    """
    Gold              → name only
    Diamond / Emerald → name + company  (if company is meaningful)
    """
    if bucket.strip().lower() in ("diamond", "emerald"):
        co = company.strip()
        if co.lower() not in JUNK_COMPANIES and len(co) > 1:
            return f"{name.strip()} {co}"
    return name.strip()


# ── Session ───────────────────────────────────────────────────────────────────
def create_context(browser):
    kwargs = dict(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 900},
        locale="en-US",
    )
    if Path(AUTH_STATE_FILE).exists():
        kwargs["storage_state"] = AUTH_STATE_FILE
        log.info("Auth state loaded from %s", AUTH_STATE_FILE)
    return browser.new_context(**kwargs)


def save_session(context):
    context.storage_state(path=AUTH_STATE_FILE)
    Path(COOKIES_FILE).write_text(
        json.dumps(context.cookies()), encoding="utf-8"
    )
    log.info("Session saved.")


def login(page, context):
    """Open LinkedIn and wait for the user to log in manually."""
    # Inject saved cookies if available
    if Path(COOKIES_FILE).exists():
        try:
            cookies = json.loads(Path(COOKIES_FILE).read_text(encoding="utf-8"))
            context.add_cookies(cookies)
        except Exception:
            pass

    page.goto("https://www.linkedin.com/feed/",
              wait_until="domcontentloaded", timeout=20_000)
    page.wait_for_timeout(2000)

    if "feed" in page.url or "mynetwork" in page.url:
        log.info("Existing session is active — no login needed.")
        return

    # Need manual login
    page.goto("https://www.linkedin.com/login",
              wait_until="domcontentloaded", timeout=20_000)

    print("\n" + "=" * 60)
    print("  Browser is open. Please log into LinkedIn.")
    print("  Once you can see your feed, come back here")
    print("  and press ENTER to start scraping.")
    print("=" * 60)
    input("\n  >> Press ENTER after logging in: ")

    page.wait_for_timeout(3000)
    save_session(context)
    log.info("Manual login complete.")


# ── URL extractor ─────────────────────────────────────────────────────────────
def get_top3_urls(page, query: str) -> list[str]:
    """
    Search LinkedIn for the query and return up to 3 profile URLs.
    Grabs every a[href*='/in/'] on the page — no card-selector dependency.
    """
    url = (
        "https://www.linkedin.com/search/results/people/?"
        + urlencode({"keywords": query, "origin": "GLOBAL_SEARCH_HEADER"})
    )
    log.info("  Searching: %s", url)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=25_000)
    except PWTimeout:
        log.warning("  Timeout — retrying once")
        page.wait_for_timeout(3000)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    # Let JavaScript render the results
    page.wait_for_timeout(PAGE_SETTLE_SEC * 1000)

    # Check for auth wall
    if any(x in page.url for x in ("authwall", "/login", "checkpoint", "challenge")):
        log.warning("  Hit auth wall at %s", page.url)
        return []

    # Pull every /in/ href from the page
    all_hrefs: list[str] = page.eval_on_selector_all(
        "a[href*='/in/']",
        "els => els.map(e => e.href)"
    )

    urls   = []
    seen   = set()
    SKIP_SLUGS = {
        "", "me", "feed", "mynetwork", "jobs", "messaging",
        "notifications", "search", "learning", "premium", "in",
    }

    for href in all_hrefs:
        # Strip query params and trailing slash
        clean = re.sub(r"\?.*$", "", href).rstrip("/")

        # Must be a profile URL: linkedin.com/in/<slug>  (no deeper path)
        if not re.search(r"linkedin\.com/in/[^/]+$", clean):
            continue

        slug = clean.split("/")[-1].lower()
        if slug in SKIP_SLUGS:
            continue

        if clean not in seen:
            seen.add(clean)
            urls.append(clean)

        if len(urls) == 3:
            break

    return urls


# ── CSV writer ────────────────────────────────────────────────────────────────
def write_result(writer, row: pd.Series, urls: list[str],
                 query: str, status: str):
    writer.writerow({
        "client_code":      row.get("client_code", ""),
        "ClientName":       row.get("ClientName", ""),
        "clean_name":       row.get("clean_name", ""),
        "affluence_bucket": row.get("affluence_bucket", ""),
        "company_names":    row.get("company_names", ""),
        "email":            row.get("email", ""),
        "mobile_no":        row.get("mobile_no", ""),
        "pan_no":           row.get("pan_no", ""),
        "address1":         row.get("address1", ""),
        "li_1_url":         urls[0] if len(urls) > 0 else "",
        "li_2_url":         urls[1] if len(urls) > 1 else "",
        "li_3_url":         urls[2] if len(urls) > 2 else "",
        "search_status":    status,
        "keywords_used":    query,
    })


# ── Main ──────────────────────────────────────────────────────────────────────
def run(input_csv: str, output_csv: str, start: int, end: int, headless: bool):
    df    = pd.read_csv(input_csv)
    total = len(df)
    end   = min(end, total) if end > 0 else total
    chunk = df.iloc[start:end]
    log.info("Processing rows %d – %d  (total in file: %d)", start, end, total)

    # Resume: find already-done client_codes
    done: set[str] = set()
    file_exists = Path(output_csv).exists()
    if file_exists:
        done = set(pd.read_csv(output_csv)["client_code"].astype(str))
        log.info("Resuming — %d rows already done", len(done))

    out_file = open(output_csv, "a", newline="", encoding="utf-8")
    writer   = csv.DictWriter(out_file, fieldnames=OUT_COLS)
    if not file_exists:
        writer.writeheader()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox",
                  "--disable-blink-features=AutomationControlled"],
        )
        context = create_context(browser)
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = context.new_page()

        # ── Login ──────────────────────────────────────────────────────────
        login(page, context)
        page.wait_for_timeout(random.randint(2000, 3000))

        # ── Row-by-row ─────────────────────────────────────────────────────
        for idx, row in chunk.iterrows():
            code    = str(row["client_code"])
            name    = str(row.get("clean_name", "") or "").strip()
            company = str(row.get("company_names", "") or "").strip()
            bucket  = str(row.get("affluence_bucket", "") or "").strip()

            # Skip already done
            if code in done:
                log.info("Skip (done): %s", code)
                continue

            # Skip non-searchable
            if should_skip(name):
                log.info("Skip (non-person): [%s] %s", code, name)
                write_result(writer, row, [], "", "skipped")
                out_file.flush()
                continue

            # Build query
            query = build_query(name, company, bucket)
            log.info("Row %-4d | %-10s | %-10s | query: %s",
                     idx, code, bucket, query)

            # Search
            try:
                urls = get_top3_urls(page, query)

                # Fallback for Diamond/Emerald: retry name-only if no results
                if not urls and bucket.lower() in ("diamond", "emerald") \
                        and company.lower() not in JUNK_COMPANIES:
                    log.info("  No results — retrying name only")
                    page.wait_for_timeout(random.randint(2000, 3000))
                    urls  = get_top3_urls(page, name)
                    query = name

                status = "found" if urls else "not_found"
                log.info("  Result: %s | URLs: %s", status, urls)

            except Exception as exc:
                log.error("  Error: %s", exc)
                urls, status = [], "error"

            # Write immediately — no batching
            write_result(writer, row, urls, query, status)
            out_file.flush()

            # Pause before next search
            delay = random.uniform(DELAY_MIN_SEC, DELAY_MAX_SEC)
            log.info("  Waiting %.1fs...\n", delay)
            time.sleep(delay)

        out_file.close()
        context.close()
        browser.close()

    # ── Final summary ─────────────────────────────────────────────────────────
    final = pd.read_csv(output_csv)
    log.info("=" * 55)
    log.info("DONE | Found: %d | Not found: %d | Skipped: %d | Errors: %d",
             (final["search_status"] == "found").sum(),
             (final["search_status"] == "not_found").sum(),
             (final["search_status"] == "skipped").sum(),
             (final["search_status"] == "error").sum())
    log.info("Saved → %s", output_csv)
    log.info("=" * 55)


# ── CLI ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LinkedIn Top-3 URL Scraper")
    ap.add_argument("--input",    default=INPUT_FILE,  help="Input CSV path")
    ap.add_argument("--output",   default=OUTPUT_FILE, help="Output CSV path")
    ap.add_argument("--start",    type=int, default=0, help="Start row index")
    ap.add_argument("--end",      type=int, default=0, help="End row (0=all)")
    ap.add_argument("--headless", action="store_true", help="Headless mode")
    args = ap.parse_args()
    run(args.input, args.output, args.start, args.end, args.headless)
