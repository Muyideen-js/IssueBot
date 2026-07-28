#!/usr/bin/env python3
"""
Drips Wave Fast Apply Bot
=========================
Uses Playwright to grab auth cookies + turnstile token ONCE,
then fires POST requests to ALL open issues back to back via API.
Much faster than clicking each button individually.

Usage:
  python drips_fast.py          # apply for all open issues
  python drips_fast.py --watch  # apply then watch for new issues every 10 min
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
WAVE_PROGRAM_ID   = "fdc01c95-806f-4b6a-998b-a6ed37e0d81b"
WAVE_API          = "https://wave-api.drips.network/api"
DRIPS_URL         = "https://www.drips.network/wave/stellar/issues"
SESSION_FILE      = Path("sessions/drips.json")
APPLIED_FILE      = Path("drips_applied.json")

GITHUB_USERNAME   = os.getenv("GITHUB_USERNAME", "")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
HEADLESS          = os.getenv("HEADLESS", "true").lower() == "true"

APPLICATION_TEXT  = os.getenv(
    "APPLICATION_TEXT",
    "Hi! I'm a full-stack developer experienced with TypeScript and the Stellar ecosystem. "
    "I've reviewed this issue and I'm confident I can deliver a quality fix. "
    "Please assign this to me!"
)

# ─── LOGGING ───────────────────────────────────────────────────────────────────
import io as _io

class SafeHandler(logging.StreamHandler):
    def emit(self, record):
        record.msg = str(record.msg).encode("ascii", errors="replace").decode("ascii")
        super().emit(record)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh  = logging.FileHandler("bot.log", encoding="utf-8")
_fh.setFormatter(_fmt)
_sh  = SafeHandler(sys.stdout)
_sh.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_fh, _sh])
log = logging.getLogger(__name__)

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─── APPLIED TRACKER ───────────────────────────────────────────────────────────
def load_applied() -> set:
    if APPLIED_FILE.exists():
        return set(json.loads(APPLIED_FILE.read_text()))
    return set()

def save_applied(applied: set):
    APPLIED_FILE.write_text(json.dumps(list(applied)))

# ─── BROWSER: GET AUTH COOKIES + TURNSTILE TOKEN ───────────────────────────────
def get_auth_session() -> dict:
    """
    Open Drips Wave in browser, intercept the auth cookies and
    a fresh turnstile token from a real Apply click.
    Returns dict with cookies and token ready for API calls.
    """
    log.info("Opening browser to grab auth session...")

    captured = {"cookies": None, "turnstile": None, "wave_token": None}

    with sync_playwright() as pw:
        storage = str(SESSION_FILE) if SESSION_FILE.exists() else None
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            storage_state=storage,
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        # Intercept API requests to steal the turnstile token
        turnstile_tokens = []
        wave_tokens = []

        def on_request(req):
            if "wave-api.drips.network" in req.url:
                headers = req.headers
                t = headers.get("x-turnstile-token")
                w = headers.get("authorization") or headers.get("x-auth-token")
                if t:
                    turnstile_tokens.append(t)
                if w:
                    wave_tokens.append(w)

        page.on("request", on_request)

        # Load issues page
        log.info("Loading Drips Wave issues page...")
        try:
            page.goto(DRIPS_URL, wait_until="networkidle", timeout=60000)
        except PWTimeout:
            log.warning("Page load timeout — continuing anyway")
        page.wait_for_timeout(4000)

        # Check if logged in
        body = page.inner_text("body").lower()
        if "log in" in body or "sign in" in body:
            log.error("Not logged in! Run: python bot.py --login --platform drips")
            browser.close()
            return {}

        # Click Apply on the FIRST available issue to get a fresh turnstile token
        log.info("Clicking Apply on first issue to capture turnstile token...")
        apply_btn = page.query_selector(
            "button:has-text('Apply'), button:has-text('Apply to issue')"
        )
        if apply_btn:
            apply_btn.click()
            page.wait_for_timeout(3000)

            # Confirm if modal appears
            confirm = page.query_selector(
                "button:has-text('Confirm'), button:has-text('Submit')"
            )
            if confirm:
                confirm.click()
                page.wait_for_timeout(2000)

        # Grab cookies
        cookies = ctx.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        captured["cookies"] = cookie_str

        if turnstile_tokens:
            captured["turnstile"] = turnstile_tokens[-1]
            log.info("Turnstile token captured!")
        else:
            log.warning("No turnstile token captured — trying page evaluation...")
            try:
                token = page.evaluate("window.__turnstileToken || ''")
                if token:
                    captured["turnstile"] = token
            except Exception:
                pass

        if wave_tokens:
            captured["wave_token"] = wave_tokens[-1]

        # Save session
        SESSION_FILE.parent.mkdir(exist_ok=True)
        ctx.storage_state(path=str(SESSION_FILE))
        browser.close()

    log.info(f"Auth session ready. Cookies: {'yes' if captured['cookies'] else 'no'}, "
             f"Turnstile: {'yes' if captured['turnstile'] else 'no'}")
    return captured

# ─── API: FETCH ALL OPEN ISSUES ────────────────────────────────────────────────
def fetch_all_issues(auth: dict) -> list[dict]:
    """Fetch ALL open issues from the Drips Wave API."""
    issues = []
    limit  = 100
    offset = 0

    headers = {
        "content-type": "application/json",
        "referer": "https://www.drips.network/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "x-timezone": "Africa/Lagos",
    }
    if auth.get("cookies"):
        headers["cookie"] = auth["cookies"]
    if auth.get("wave_token"):
        headers["authorization"] = auth["wave_token"]

    while True:
        url = (
            f"{WAVE_API}/issues"
            f"?limit={limit}&offset={offset}"
            f"&waveProgramId={WAVE_PROGRAM_ID}"
            f"&state=open"
            f"&sortBy=updatedAt"
        )
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            batch = data.get("data") or data if isinstance(data, list) else []
            if not batch:
                break
            issues.extend(batch)
            log.info(f"Fetched {len(issues)} issues so far...")
            if len(batch) < limit:
                break
            offset += limit
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Failed to fetch issues: {e}")
            break

    log.info(f"Total open issues found: {len(issues)}")
    return issues

# ─── API: APPLY FOR ONE ISSUE ──────────────────────────────────────────────────
def apply_for_issue(issue: dict, auth: dict) -> bool:
    """Fire a POST request to apply for one issue."""
    issue_id   = issue.get("id")
    issue_title = issue.get("title", "Unknown")

    if not issue_id:
        return False

    url = f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/issues/{issue_id}/applications"

    headers = {
        "content-type": "application/json",
        "referer": "https://www.drips.network/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "x-timezone": "Africa/Lagos",
    }
    if auth.get("cookies"):
        headers["cookie"] = auth["cookies"]
    if auth.get("turnstile"):
        headers["x-turnstile-token"] = auth["turnstile"]
    if auth.get("wave_token"):
        headers["authorization"] = auth["wave_token"]

    payload = {"applicationText": APPLICATION_TEXT}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=15)

        if r.status_code == 201:
            log.info(f"Applied: {issue_title[:60]}")
            return True
        elif r.status_code == 409:
            log.info(f"Already applied: {issue_title[:50]}")
            return True   # count as done
        elif r.status_code == 403:
            log.warning(f"Forbidden (slot limit or Cloudflare): {issue_title[:40]}")
            return False
        elif r.status_code == 429:
            log.warning("Rate limited — sleeping 5s...")
            time.sleep(5)
            return False
        else:
            log.warning(f"Unexpected {r.status_code} for {issue_title[:40]}: {r.text[:100]}")
            return False

    except Exception as e:
        log.error(f"Apply error for {issue_title[:40]}: {e}")
        return False

# ─── MAIN APPLY LOOP ───────────────────────────────────────────────────────────
def apply_all(watch: bool = False):
    applied = load_applied()

    log.info("Getting auth session from browser...")
    auth = get_auth_session()
    if not auth:
        log.error("Could not get auth session. Run: python bot.py --login --platform drips")
        return

    tg(
        f"*Drips Fast Bot Started*\n\n"
        f"User: `{GITHUB_USERNAME}`\n"
        f"Mode: {'Watch (runs every 10 min)' if watch else 'Single run'}\n"
        f"Fetching all open issues..."
    )

    while True:
        log.info("=== Fetching all open issues ===")
        issues = fetch_all_issues(auth)

        new_issues = [i for i in issues if i.get("id") not in applied]
        log.info(f"New issues to apply for: {len(new_issues)}")

        if not new_issues:
            log.info("No new issues — all caught up!")
            tg("No new issues to apply for right now.")
        else:
            success = 0
            fail    = 0
            for issue in new_issues:
                ok = apply_for_issue(issue, auth)
                if ok:
                    applied.add(issue["id"])
                    save_applied(applied)
                    success += 1
                else:
                    fail += 1

                # Small delay between requests — be polite
                time.sleep(0.8)

            log.info(f"Done: {success} applied, {fail} failed")
            tg(
                f"*Drips Fast Bot Done!*\n\n"
                f"Applied: {success}\n"
                f"Failed: {fail}\n"
                f"Total tracked: {len(applied)}"
            )

            # If we got 403s, token may be stale — refresh
            if fail > success:
                log.info("Too many failures — refreshing auth session...")
                auth = get_auth_session()

        if not watch:
            break

        log.info("Watching for new issues — sleeping 10 minutes...")
        time.sleep(600)

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true",
                        help="Keep running, check for new issues every 10 min")
    args = parser.parse_args()
    apply_all(watch=args.watch)

if __name__ == "__main__":
    main()
