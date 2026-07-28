#!/usr/bin/env python3
"""
Drips Wave Smart Bot
====================
- Watches for NEW issues dropped on Drips Wave
- Applies IMMEDIATELY when a new issue appears
- Withdraws applications pending > 1hr and reapplies to new ones
- Keeps 15 slots always filled with the FRESHEST issues

Usage:
  python drips_fast.py           # single run (for GitHub Actions)
  python drips_fast.py --watch   # loop every 5 min (local PC)
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
WAVE_PROGRAM_ID  = "fdc01c95-806f-4b6a-998b-a6ed37e0d81b"
WAVE_API         = "https://wave-api.drips.network/api"
DRIPS_URL        = "https://www.drips.network/wave/stellar/issues"
SESSION_FILE     = Path("sessions/drips.json")
SEEN_FILE        = Path("drips_seen.json")       # all issue IDs we've ever seen
APPLIED_FILE     = Path("drips_applied.json")    # issue IDs we applied to

GITHUB_USERNAME  = os.getenv("GITHUB_USERNAME", "")
GITHUB_PASSWORD  = os.getenv("GITHUB_PASSWORD", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
HEADLESS         = os.getenv("HEADLESS", "true").lower() == "true"
ROTATE_HOURS     = float(os.getenv("ROTATE_HOURS", "1"))    # withdraw after 1hr
MAX_SLOTS        = int(os.getenv("MAX_SLOTS", "15"))

APPLICATION_TEXT = os.getenv(
    "APPLICATION_TEXT",
    "Hi! I'm a full-stack developer with TypeScript and Stellar ecosystem experience. "
    "I've reviewed this issue and can deliver a quality fix. Please assign this to me!"
)

# ─── LOGGING ───────────────────────────────────────────────────────────────────
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
        log.error(f"Telegram: {e}")

# ─── STATE ─────────────────────────────────────────────────────────────────────
def load_json(path: Path, default) -> set | list:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return set(data) if isinstance(default, set) else data
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(list(data) if isinstance(data, set) else data))

# ─── HEADERS ───────────────────────────────────────────────────────────────────
def make_headers(auth: dict) -> dict:
    h = {
        "content-type": "application/json",
        "referer": "https://www.drips.network/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        ),
        "x-timezone": "Africa/Lagos",
    }
    if auth.get("cookies"):    h["cookie"]              = auth["cookies"]
    if auth.get("turnstile"):  h["x-turnstile-token"]   = auth["turnstile"]
    if auth.get("wave_token"): h["authorization"]        = auth["wave_token"]
    return h

# ─── SESSION ───────────────────────────────────────────────────────────────────
def session_valid(auth: dict) -> bool:
    try:
        r = requests.get(
            f"{WAVE_API}/user/me",
            headers=make_headers(auth),
            timeout=10
        )
        return r.status_code == 200
    except Exception:
        return False

def get_auth_session() -> dict:
    log.info("Opening browser to grab session...")
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

        turnstile_tokens = []
        wave_tokens = []

        def on_request(req):
            if "wave-api.drips.network" in req.url:
                h = req.headers
                t = h.get("x-turnstile-token")
                w = h.get("authorization")
                if t: turnstile_tokens.append(t)
                if w: wave_tokens.append(w)

        page.on("request", on_request)

        try:
            page.goto(DRIPS_URL, wait_until="networkidle", timeout=60000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)

        body = page.inner_text("body").lower()
        if "log in" in body or "sign in" in body:
            log.error("Not logged in! Run: python bot.py --login --platform drips")
            browser.close()
            return {}

        # Navigate to first issue to trigger API calls
        first_link = page.query_selector("a[href*='/issues/']")
        if first_link:
            href = first_link.get_attribute("href") or ""
            url  = href if href.startswith("http") else f"https://www.drips.network{href}"
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except PWTimeout:
                pass
            page.wait_for_timeout(3000)

            # Try clicking Apply to get turnstile token
            page.evaluate("""
                () => {
                    const btn = Array.from(document.querySelectorAll('button'))
                        .find(b => b.textContent.trim().toLowerCase() === 'apply' && !b.disabled);
                    if (btn) btn.click();
                }
            """)
            page.wait_for_timeout(3000)
            page.evaluate("""
                () => {
                    const btn = Array.from(document.querySelectorAll('button'))
                        .find(b => ['confirm','submit','yes'].some(w =>
                            b.textContent.trim().toLowerCase().includes(w)));
                    if (btn) btn.click();
                }
            """)
            page.wait_for_timeout(2000)

        cookies = ctx.cookies()
        captured["cookies"]    = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        captured["turnstile"]  = turnstile_tokens[-1] if turnstile_tokens else None
        captured["wave_token"] = wave_tokens[-1] if wave_tokens else None

        SESSION_FILE.parent.mkdir(exist_ok=True)
        ctx.storage_state(path=str(SESSION_FILE))
        browser.close()

    log.info(f"Session | Cookies: {'yes' if captured['cookies'] else 'no'} | Turnstile: {'yes' if captured['turnstile'] else 'no'}")
    return captured

# ─── FETCH ISSUES ──────────────────────────────────────────────────────────────
def fetch_latest_issues(auth: dict, limit: int = 100) -> list[dict]:
    """
    Fetch the most recently updated open issues.
    Sorted by updatedAt so newest drops appear first.
    """
    headers = make_headers(auth)
    issues  = []
    seen    = set()

    url = (
        f"{WAVE_API}/issues"
        f"?limit={limit}&offset=0"
        f"&waveProgramId={WAVE_PROGRAM_ID}"
        f"&state=open&sortBy=updatedAt"
    )
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 401:
            log.warning("Session expired")
            return []
        data  = r.json()
        batch = data.get("data") or []
        pagination = data.get("pagination") or {}
        total = pagination.get("total") or 0
        log.info(f"Total open issues on platform: {total}")

        for issue in batch:
            iid = issue.get("id")
            if iid and iid not in seen:
                seen.add(iid)
                issues.append(issue)

    except Exception as e:
        log.error(f"fetch_latest_issues error: {e}")

    return issues

# ─── FETCH MY APPLICATIONS ─────────────────────────────────────────────────────
def fetch_my_applications(auth: dict) -> list[dict]:
    headers = make_headers(auth)
    for url in [
        f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/applications?status=pending&limit=50",
        f"{WAVE_API}/user/applications?waveProgramId={WAVE_PROGRAM_ID}&status=pending&limit=50",
    ]:
        try:
            r = requests.get(url, headers=headers, timeout=30)
            log.info(f"My apps [{r.status_code}]: {r.text[:150]}")
            if r.status_code == 200:
                d = r.json()
                result = d if isinstance(d, list) else (d.get("data") or d.get("applications") or [])
                if result:
                    return result
        except Exception as e:
            log.error(f"fetch_my_applications error: {e}")
    return []

# ─── WITHDRAW ──────────────────────────────────────────────────────────────────
def withdraw(app: dict, auth: dict) -> bool:
    app_id   = app.get("id")
    issue    = app.get("issue") or {}
    issue_id = app.get("issueId") or issue.get("id")
    title    = issue.get("title") or "Unknown"

    if not app_id or not issue_id:
        log.warning(f"Cannot withdraw — missing fields: {list(app.keys())}")
        return False

    url = f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/issues/{issue_id}/applications/{app_id}"
    try:
        r = requests.delete(url, headers=make_headers(auth), timeout=15)
        if r.status_code in (200, 204):
            log.info(f"Withdrawn: {title[:60]}")
            return True
        log.warning(f"Withdraw {r.status_code}: {r.text[:80]}")
        return False
    except Exception as e:
        log.error(f"Withdraw error: {e}")
        return False

# ─── APPLY ─────────────────────────────────────────────────────────────────────
def apply(issue: dict, auth: dict) -> str:
    """Returns: ok | taken | quota | expired | error"""
    issue_id = issue.get("id")
    title    = issue.get("title", "")

    url = f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/issues/{issue_id}/applications"
    try:
        r = requests.post(
            url,
            headers=make_headers(auth),
            json={"applicationText": APPLICATION_TEXT},
            timeout=15
        )

        if r.status_code == 201:
            log.info(f"Applied: {title[:70]}")
            return "ok"

        err = ""
        try: err = r.json().get("error", "")
        except Exception: err = r.text[:100]

        if r.status_code == 400:
            if "accepted application" in err:    return "taken"
            if "already applied" in err.lower(): return "taken"
            if "quota" in err.lower() or "at most" in err.lower():
                log.warning(f"QUOTA FULL: {err}")
                return "quota"
            log.warning(f"400: {err[:80]}")
            return "error"

        if r.status_code == 401: return "expired"
        if r.status_code == 429:
            time.sleep(5)
            return "error"

        log.warning(f"{r.status_code}: {err[:80]}")
        return "error"

    except Exception as e:
        log.error(f"Apply error: {e}")
        return "error"

# ─── MAIN CYCLE ────────────────────────────────────────────────────────────────
def run_cycle(auth: dict) -> tuple[dict, bool]:
    """
    One cycle:
    1. Fetch my current applications
    2. Withdraw ones pending > ROTATE_HOURS
    3. Fetch latest issues
    4. Apply to NEW issues (ones we haven't seen before) first
    5. Fill remaining slots with any available issues

    Returns (auth, needs_session_refresh)
    """
    log.info("=== Cycle start ===")
    now_utc  = datetime.now(timezone.utc)
    seen     = load_json(SEEN_FILE, set())
    applied  = load_json(APPLIED_FILE, set())

    # ── Step 1: Check my applications ────────────────────────────────────────
    my_apps = fetch_my_applications(auth)
    log.info(f"Active applications: {len(my_apps)}")

    withdrawn  = 0
    still_open = 0
    withdrawn_issue_ids = set()

    for app in my_apps:
        applied_at_str = app.get("appliedAt") or app.get("createdAt") or ""
        try:
            applied_at = datetime.fromisoformat(applied_at_str.replace("Z", "+00:00"))
            age_hours  = (now_utc - applied_at).total_seconds() / 3600
        except Exception:
            age_hours  = 0

        if age_hours >= ROTATE_HOURS:
            issue    = app.get("issue") or {}
            issue_id = app.get("issueId") or issue.get("id")
            log.info(f"Withdrawing ({age_hours:.1f}h old): {issue.get('title','')[:50]}")
            if withdraw(app, auth):
                withdrawn += 1
                if issue_id:
                    withdrawn_issue_ids.add(issue_id)
                    # Remove from applied so we can reapply if still open
                    applied.discard(issue_id)
            time.sleep(0.5)
        else:
            still_open += 1

    if withdrawn > 0:
        save_json(APPLIED_FILE, applied)
        log.info(f"Withdrawn: {withdrawn} | Still active: {still_open}")
        tg(
            f"*Drips Bot: Rotated {withdrawn} applications*\n"
            f"Still active: {still_open} | Filling slots..."
        )

    # ── Step 2: Fetch latest issues ───────────────────────────────────────────
    free_slots = MAX_SLOTS - still_open
    log.info(f"Free slots: {free_slots}")

    if free_slots <= 0:
        log.info("All 15 slots full — nothing to apply for this cycle")
        log.info("=== Cycle end ===")
        return auth, False

    issues = fetch_latest_issues(auth, limit=100)
    if not issues:
        log.info("No issues returned — possible session issue")
        return auth, True

    # ── Step 3: Detect truly NEW issues (never seen before) ───────────────────
    new_issues = []
    other_issues = []

    for issue in issues:
        iid = issue.get("id")
        if iid not in seen:
            new_issues.append(issue)   # brand new drop
        elif iid not in applied:
            other_issues.append(issue) # seen before but not applied

    # Update seen tracker
    for issue in issues:
        seen.add(issue.get("id"))
    save_json(SEEN_FILE, seen)

    if new_issues:
        log.info(f"NEW issues just dropped: {len(new_issues)}")
        tg(
            f"*Drips Bot: {len(new_issues)} New Issues Dropped!*\n"
            f"Applying immediately..."
        )

    # ── Step 4: Apply — new issues first, then fill with others ──────────────
    priority_list = new_issues + other_issues  # new drops get priority
    applied_count = 0
    need_refresh  = False

    for issue in priority_list:
        if applied_count >= free_slots:
            break

        iid = issue.get("id")
        if iid in applied:
            continue

        result = apply(issue, auth)

        if result == "ok":
            applied.add(iid)
            save_json(APPLIED_FILE, applied)
            applied_count += 1
            time.sleep(0.8)

        elif result == "taken":
            applied.add(iid)
            save_json(APPLIED_FILE, applied)

        elif result == "quota":
            log.info("Quota reached — all 15 slots filled")
            break

        elif result == "expired":
            log.warning("Session expired mid-apply")
            need_refresh = True
            break

        else:
            time.sleep(0.5)

    log.info(f"Applied this cycle: {applied_count}")

    if applied_count > 0:
        tg(
            f"*Drips Bot: Applied!*\n\n"
            f"New applications: {applied_count}\n"
            f"Slots used: {still_open + applied_count}/{MAX_SLOTS}"
        )

    log.info("=== Cycle end ===")
    return auth, need_refresh

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true",
                        help="Loop every 5 min (use on PC — GitHub Actions uses schedule)")
    args = parser.parse_args()

    log.info(f"Drips Smart Bot | Rotate after {ROTATE_HOURS}h | Slots: {MAX_SLOTS}")

    auth = get_auth_session()
    if not auth:
        log.error("No session. Run: python bot.py --login --platform drips")
        sys.exit(1)

    tg(
        f"*Drips Smart Bot Started*\n\n"
        f"User: `{GITHUB_USERNAME}`\n"
        f"Rotate: every {ROTATE_HOURS}h\n"
        f"Slots: {MAX_SLOTS}"
    )

    while True:
        # Refresh session if expired
        if not session_valid(auth):
            log.info("Session expired — refreshing...")
            auth = get_auth_session()
            if not auth:
                log.error("Session refresh failed — waiting 5 min")
                time.sleep(300)
                continue

        auth, need_refresh = run_cycle(auth)

        if need_refresh:
            log.info("Refreshing session after cycle...")
            auth = get_auth_session()

        if not args.watch:
            break

        log.info("Sleeping 5 minutes...")
        time.sleep(300)

if __name__ == "__main__":
    main()
