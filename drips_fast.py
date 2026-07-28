#!/usr/bin/env python3
"""
Drips Wave Smart Bot
====================
- Priority repos get applications first
- Max 2 applications per repo
- Gemini writes custom message per issue (fallback: "Hi, i can fix this")
- Withdraws non-priority applications to make room for priority ones
- Runs on GitHub Actions every 5 minutes

Usage:
  python drips_fast.py           # single run (GitHub Actions)
  python drips_fast.py --watch   # loop every 5 min (local PC)
"""

import os
import sys
import json
import time
import logging
import argparse
import requests
import base64
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
SEEN_FILE        = Path("drips_seen.json")
APPLIED_FILE     = Path("drips_applied.json")

GITHUB_USERNAME  = os.getenv("GITHUB_USERNAME", "")
GITHUB_PASSWORD  = os.getenv("GITHUB_PASSWORD", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
HEADLESS         = os.getenv("HEADLESS", "true").lower() == "true"
ROTATE_HOURS     = float(os.getenv("ROTATE_HOURS", "1"))
MAX_SLOTS        = int(os.getenv("MAX_SLOTS", "15"))
MAX_PER_REPO     = int(os.getenv("MAX_PER_REPO", "2"))

FALLBACK_MESSAGE = "Hi, i can fix this"

# ─── PRIORITY REPOS ────────────────────────────────────────────────────────────
# These get applications first. Others withdrawn to make room.
PRIORITY_REPOS = [
    "Fluxora-Org/Fluxora-Backend",
    "ancore-org/ancore",
    "Talenttrust/Talenttrust-Contracts",
    "Talenttrust/Talenttrust-Backend",
    "Talenttrust/Talenttrust-Frontend",
    "Akanimoh12/Stellar-iPredict",
    "Chronopay-Org/ChronoPay-Backend",
    "Stellabill/stellabill-contracts",
    "enliven17/talos-stellar",
]

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

# ─── GEMINI ────────────────────────────────────────────────────────────────────
def gemini_message(issue_title: str, issue_body: str, repo: str) -> str:
    """
    Generate a custom application message using Gemini.
    Falls back to default message if API fails or limit hit.
    """
    if not GEMINI_API_KEY:
        return FALLBACK_MESSAGE

    prompt = (
        f"Write a short 2-sentence GitHub issue application message for this issue.\n"
        f"Repo: {repo}\n"
        f"Issue: {issue_title}\n"
        f"Description: {(issue_body or '')[:300]}\n\n"
        f"Rules:\n"
        f"- Max 2 sentences\n"
        f"- Sound like a confident developer\n"
        f"- Mention one specific thing from the issue\n"
        f"- End with asking to be assigned\n"
        f"- Do NOT use emojis\n"
        f"- Keep it under 50 words\n"
        f"Return only the message, nothing else."
    )

    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            params={"key": GEMINI_API_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"temperature": 0.3, "maxOutputTokens": 100}},
            timeout=15
        )
        if r.status_code == 200:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            log.info(f"Gemini message: {text[:60]}")
            return text
        elif r.status_code == 429:
            log.warning("Gemini rate limit hit — using fallback message")
            return FALLBACK_MESSAGE
        else:
            log.warning(f"Gemini {r.status_code} — using fallback")
            return FALLBACK_MESSAGE
    except Exception as e:
        log.warning(f"Gemini error: {e} — using fallback")
        return FALLBACK_MESSAGE

# ─── STATE ─────────────────────────────────────────────────────────────────────
def load_json(path: Path, default):
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return set(data) if isinstance(default, set) else data
        except Exception:
            pass
    return default

def save_json(path: Path, data):
    path.write_text(json.dumps(list(data) if isinstance(data, set) else data, indent=2))

# ─── WAVE TOKEN ────────────────────────────────────────────────────────────────
def extract_wave_token(session_file: Path) -> str:
    """Extract wave_access_token from sessions/drips.json."""
    try:
        data = json.loads(session_file.read_text())
        for c in (data.get("cookies") or []):
            if c.get("name") == "wave_access_token":
                return c.get("value", "")
    except Exception as e:
        log.error(f"extract_wave_token: {e}")
    return ""

def extract_refresh_token(session_file: Path) -> str:
    """Extract wave_refresh_token from sessions/drips.json."""
    try:
        data = json.loads(session_file.read_text())
        for c in (data.get("cookies") or []):
            if c.get("name") == "wave_refresh_token":
                return c.get("value", "")
    except Exception as e:
        log.error(f"extract_refresh_token: {e}")
    return ""

def refresh_access_token() -> str:
    """
    Use wave_refresh_token to get a new wave_access_token.
    No browser needed — pure API call.
    """
    refresh_token = extract_refresh_token(SESSION_FILE)
    if not refresh_token:
        log.warning("No refresh token found")
        return ""

    try:
        r = requests.post(
            f"{WAVE_API}/auth/refresh",
            json={"refreshToken": refresh_token},
            headers={"content-type": "application/json"},
            timeout=15
        )
        log.info(f"Token refresh: {r.status_code} — {r.text[:150]}")
        if r.status_code == 200:
            data = r.json()
            new_token = data.get("accessToken") or data.get("access_token") or data.get("token") or ""
            if new_token:
                log.info("Got new access token!")
                # Update session file with new token
                update_session_token(new_token)
                return new_token
    except Exception as e:
        log.error(f"refresh_access_token error: {e}")
    return ""

def update_session_token(new_token: str):
    """Update wave_access_token in sessions/drips.json."""
    try:
        data = json.loads(SESSION_FILE.read_text())
        now  = datetime.now(timezone.utc).timestamp()
        for c in (data.get("cookies") or []):
            if c.get("name") == "wave_access_token":
                c["value"]   = new_token
                c["expires"] = now + 900   # 15 min
        SESSION_FILE.write_text(json.dumps(data))
        log.info("Session file updated with new token")
    except Exception as e:
        log.error(f"update_session_token: {e}")

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
    if auth.get("cookies"):   h["cookie"]            = auth["cookies"]
    if auth.get("turnstile"): h["x-turnstile-token"] = auth["turnstile"]
    token = auth.get("wave_token") or extract_wave_token(SESSION_FILE)
    if token:
        h["authorization"] = f"Bearer {token}"
    return h

# ─── SESSION ───────────────────────────────────────────────────────────────────
def token_expires_in(token: str) -> float:
    """Return seconds until token expires. Negative = already expired."""
    try:
        parts = token.split(".")
        if len(parts) == 3:
            padding = 4 - len(parts[1]) % 4
            payload = json.loads(base64.b64decode(parts[1] + "=" * padding))
            exp = payload.get("exp", 0)
            return exp - datetime.now(timezone.utc).timestamp()
    except Exception:
        pass
    return -1

def get_valid_token() -> str:
    """Get a valid wave_access_token, refreshing if needed."""
    token = extract_wave_token(SESSION_FILE)
    if token:
        expires_in = token_expires_in(token)
        log.info(f"Token expires in: {expires_in/60:.1f} min")
        if expires_in > 60:   # still valid
            return token
        log.info("Token expiring soon — refreshing...")

    # Try refresh token first (no browser needed)
    new_token = refresh_access_token()
    if new_token:
        return new_token

    # Last resort — open browser
    log.info("Refresh failed — opening browser...")
    auth = get_auth_session()
    return auth.get("wave_token") or extract_wave_token(SESSION_FILE)

def session_valid(auth: dict) -> bool:
    try:
        r = requests.get(
            f"{WAVE_API}/user/me",
            headers=make_headers(auth),
            timeout=10
        )
        log.info(f"Session check: {r.status_code}")
        return r.status_code == 200
    except Exception:
        return False

# ─── BROWSER SESSION ───────────────────────────────────────────────────────────
def get_auth_session() -> dict:
    log.info("Opening browser...")
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
                w = h.get("authorization", "").replace("Bearer ", "")
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

        first_link = page.query_selector("a[href*='/issues/']")
        if first_link:
            href = first_link.get_attribute("href") or ""
            url  = href if href.startswith("http") else f"https://www.drips.network{href}"
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except PWTimeout:
                pass
            page.wait_for_timeout(3000)

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

    log.info(f"Session | Turnstile: {'yes' if captured['turnstile'] else 'no'}")
    return captured

# ─── REPO HELPERS ──────────────────────────────────────────────────────────────
def get_repo(issue: dict) -> str:
    """Extract owner/repo from issue data."""
    repo = issue.get("repo") or issue.get("repository") or ""
    if isinstance(repo, dict):
        return repo.get("fullName") or repo.get("full_name") or ""
    gh_url = issue.get("gitHubIssueUrl") or issue.get("htmlUrl") or ""
    if "github.com" in gh_url:
        parts = gh_url.replace("https://github.com/", "").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
    return repo

def is_priority(issue: dict) -> bool:
    repo = get_repo(issue).lower()
    return any(p.lower() in repo or repo in p.lower() for p in PRIORITY_REPOS)

# ─── FETCH ISSUES ──────────────────────────────────────────────────────────────
def fetch_latest_issues(auth: dict) -> list[dict]:
    headers = make_headers(auth)
    issues  = []
    seen    = set()

    url = (
        f"{WAVE_API}/issues?limit=100&offset=0"
        f"&waveProgramId={WAVE_PROGRAM_ID}"
        f"&state=open&sortBy=updatedAt"
    )
    try:
        r = requests.get(url, headers=headers, timeout=30)
        log.info(f"Issues fetch: {r.status_code}")
        if r.status_code in (401, 403):
            return []
        data  = r.json()
        batch = data.get("data") or []
        pagination = data.get("pagination") or {}
        total = pagination.get("total") or 0
        log.info(f"Total open issues: {total}")

        for issue in batch:
            iid = issue.get("id")
            accepted = issue.get("acceptedApplicationsCount") or 0
            if iid and iid not in seen and accepted == 0:
                seen.add(iid)
                issues.append(issue)

    except Exception as e:
        log.error(f"fetch_latest_issues: {e}")

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
            log.info(f"My apps [{r.status_code}]: {r.text[:100]}")
            if r.status_code == 200:
                d = r.json()
                result = d if isinstance(d, list) else (d.get("data") or d.get("applications") or [])
                if isinstance(result, list):
                    return result
        except Exception as e:
            log.error(f"fetch_my_applications: {e}")
    return []

# ─── WITHDRAW ──────────────────────────────────────────────────────────────────
def withdraw(app: dict, auth: dict) -> bool:
    app_id   = app.get("id")
    issue    = app.get("issue") or {}
    issue_id = app.get("issueId") or issue.get("id")
    title    = issue.get("title") or "Unknown"

    if not app_id or not issue_id:
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
def apply_for_issue(issue: dict, auth: dict) -> str:
    """Returns: ok | taken | quota | expired | error"""
    issue_id = issue.get("id")
    title    = issue.get("title", "")
    body     = issue.get("body", "")
    repo     = get_repo(issue)

    # Generate application message
    msg = gemini_message(title, body, repo)

    url = f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/issues/{issue_id}/applications"
    try:
        r = requests.post(
            url,
            headers=make_headers(auth),
            json={"applicationText": msg},
            timeout=15
        )

        if r.status_code == 201:
            priority_tag = " [PRIORITY]" if is_priority(issue) else ""
            log.info(f"Applied{priority_tag}: {title[:60]}")
            return "ok"

        err = ""
        try:    err = r.json().get("error", "")
        except: err = r.text[:100]

        if r.status_code == 400:
            if "accepted application" in err:          return "taken"
            if "already applied" in err.lower():       return "taken"
            if "quota" in err.lower() or "at most" in err.lower():
                log.warning(f"QUOTA FULL: {err}")
                return "quota"
            log.warning(f"400: {err[:80]}")
            return "error"

        if r.status_code in (401, 403): return "expired"
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
    log.info("=== Cycle start ===")
    now_utc = datetime.now(timezone.utc)

    seen    = load_json(SEEN_FILE, set())
    applied = load_json(APPLIED_FILE, set())

    # ── Step 1: Refresh token if needed ──────────────────────────────────────
    token = get_valid_token()
    if token:
        auth["wave_token"] = token

    # ── Step 2: Fetch my current applications ────────────────────────────────
    my_apps = fetch_my_applications(auth)
    log.info(f"Active applications: {len(my_apps)}")

    # Count per repo
    repo_counts: dict[str, int] = {}
    for app in my_apps:
        repo = get_repo(app.get("issue") or app)
        repo_counts[repo] = repo_counts.get(repo, 0) + 1

    # ── Step 3: Fetch latest issues ──────────────────────────────────────────
    issues = fetch_latest_issues(auth)
    if not issues:
        log.info("No issues returned")
        return auth, True

    # Separate new drops vs seen before
    new_drops   = [i for i in issues if i.get("id") not in seen]
    other       = [i for i in issues if i.get("id") in seen and i.get("id") not in applied]

    # Update seen
    for i in issues:
        seen.add(i.get("id"))
    save_json(SEEN_FILE, seen)

    if new_drops:
        priority_new = [i for i in new_drops if is_priority(i)]
        log.info(f"New drops: {len(new_drops)} ({len(priority_new)} priority)")
        if priority_new:
            tg(f"*{len(priority_new)} Priority Issues Dropped!*\nApplying immediately...")

    # ── Step 4: Check if priority issues need slots freed ────────────────────
    priority_issues = [i for i in (new_drops + other) if is_priority(i) and i.get("id") not in applied]

    if priority_issues:
        # Count how many priority slots we need
        priority_need = min(len(priority_issues), MAX_SLOTS)
        current_priority = sum(1 for app in my_apps if is_priority(app.get("issue") or app))
        current_non_priority = len(my_apps) - current_priority
        free_slots = MAX_SLOTS - len(my_apps)
        slots_needed = priority_need - free_slots

        if slots_needed > 0 and current_non_priority > 0:
            # Withdraw non-priority apps (oldest first) to make room
            non_priority_apps = [
                app for app in my_apps
                if not is_priority(app.get("issue") or app)
            ]
            # Sort by oldest first
            non_priority_apps.sort(key=lambda a: a.get("appliedAt") or "")

            withdrawn = 0
            for app in non_priority_apps:
                if withdrawn >= slots_needed:
                    break
                issue    = app.get("issue") or {}
                issue_id = app.get("issueId") or issue.get("id")
                if withdraw(app, auth):
                    withdrawn += 1
                    if issue_id:
                        applied.discard(issue_id)
                    # Update repo counts
                    repo = get_repo(issue)
                    repo_counts[repo] = max(0, repo_counts.get(repo, 1) - 1)
                    time.sleep(0.3)

            if withdrawn:
                save_json(APPLIED_FILE, applied)
                my_apps = [a for a in my_apps if a not in non_priority_apps[:withdrawn]]
                log.info(f"Freed {withdrawn} slots for priority issues")

    # ── Step 5: Also rotate stale applications (> ROTATE_HOURS) ─────────────
    withdrawn_stale = 0
    for app in list(my_apps):
        applied_at_str = app.get("appliedAt") or app.get("createdAt") or ""
        try:
            applied_at = datetime.fromisoformat(applied_at_str.replace("Z", "+00:00"))
            age_hours  = (now_utc - applied_at).total_seconds() / 3600
        except Exception:
            age_hours = 0

        if age_hours >= ROTATE_HOURS:
            issue    = app.get("issue") or {}
            issue_id = app.get("issueId") or issue.get("id")
            if withdraw(app, auth):
                withdrawn_stale += 1
                if issue_id:
                    applied.discard(issue_id)
                repo = get_repo(issue)
                repo_counts[repo] = max(0, repo_counts.get(repo, 1) - 1)
                time.sleep(0.3)

    if withdrawn_stale:
        save_json(APPLIED_FILE, applied)
        log.info(f"Rotated {withdrawn_stale} stale applications (>{ROTATE_HOURS}h)")

    # ── Step 6: Apply — priority first, then others ──────────────────────────
    # Refresh my_apps count after withdrawals
    remaining_apps = fetch_my_applications(auth)
    free_slots = MAX_SLOTS - len(remaining_apps)
    log.info(f"Free slots after rotation: {free_slots}")

    if free_slots <= 0:
        log.info("All slots full")
        log.info("=== Cycle end ===")
        return auth, False

    # Build apply list: priority new drops first, then priority seen, then others
    priority_new   = [i for i in new_drops if is_priority(i) and i.get("id") not in applied]
    priority_other = [i for i in other    if is_priority(i) and i.get("id") not in applied]
    normal_new     = [i for i in new_drops if not is_priority(i) and i.get("id") not in applied]
    normal_other   = [i for i in other    if not is_priority(i) and i.get("id") not in applied]

    apply_order = priority_new + priority_other + normal_new + normal_other

    applied_count = 0
    need_refresh  = False

    # Track per-repo counts for this cycle
    cycle_repo_counts: dict[str, int] = {}
    for app in remaining_apps:
        repo = get_repo(app.get("issue") or app)
        cycle_repo_counts[repo] = cycle_repo_counts.get(repo, 0) + 1

    for issue in apply_order:
        if applied_count >= free_slots:
            break

        iid  = issue.get("id")
        repo = get_repo(issue)

        # Max 2 per repo
        if cycle_repo_counts.get(repo, 0) >= MAX_PER_REPO:
            log.info(f"Skipping (max {MAX_PER_REPO}/repo reached): {repo}")
            continue

        result = apply_for_issue(issue, auth)

        if result == "ok":
            applied.add(iid)
            save_json(APPLIED_FILE, applied)
            applied_count += 1
            cycle_repo_counts[repo] = cycle_repo_counts.get(repo, 0) + 1
            time.sleep(0.8)

        elif result == "taken":
            applied.add(iid)
            save_json(APPLIED_FILE, applied)

        elif result == "quota":
            log.info("Quota reached — slots full")
            break

        elif result == "expired":
            need_refresh = True
            break

        else:
            time.sleep(0.5)

    log.info(f"Applied this cycle: {applied_count}")
    if applied_count > 0:
        tg(
            f"*Drips Bot: {applied_count} Applied*\n"
            f"Slots used: {len(remaining_apps) + applied_count}/{MAX_SLOTS}"
        )

    log.info("=== Cycle end ===")
    return auth, need_refresh

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true",
                        help="Loop every 5 min (local PC). GitHub Actions uses schedule.")
    args = parser.parse_args()

    log.info(f"Drips Smart Bot | Priority repos: {len(PRIORITY_REPOS)} | Max/repo: {MAX_PER_REPO}")

    auth = {}

    # Try to use existing session first (no browser)
    token = get_valid_token()
    if token:
        auth["wave_token"] = token
        if session_valid(auth):
            log.info("Using existing session — no browser needed")
        else:
            auth = get_auth_session()
    else:
        auth = get_auth_session()

    if not auth:
        log.error("No session. Run: python bot.py --login --platform drips")
        sys.exit(1)

    tg(
        f"*Drips Smart Bot Started*\n\n"
        f"Priority repos: {len(PRIORITY_REPOS)}\n"
        f"Max per repo: {MAX_PER_REPO}\n"
        f"Rotate after: {ROTATE_HOURS}h\n"
        f"Gemini: {'enabled' if GEMINI_API_KEY else 'disabled (fallback)'}"
    )

    while True:
        if not session_valid(auth):
            log.info("Session invalid — refreshing...")
            token = get_valid_token()
            if token:
                auth["wave_token"] = token
            else:
                auth = get_auth_session()

        auth, need_refresh = run_cycle(auth)

        if need_refresh:
            token = get_valid_token()
            if token:
                auth["wave_token"] = token
            else:
                auth = get_auth_session()

        if not args.watch:
            break

        log.info("Sleeping 5 minutes...")
        time.sleep(300)

if __name__ == "__main__":
    main()