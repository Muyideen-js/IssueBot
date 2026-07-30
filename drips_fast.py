#!/usr/bin/env python3
"""
Drips Wave Smart Bot v3
=======================
- Fills ALL 15 slots immediately
- Max 2 per repo spread across repos
- Priority repos applied first
- Auto-adds repos where you get assigned to priority list
- Rotates stale applications every 1hr
- Gemini custom messages with fallback
"""

import os, sys, json, time, logging, argparse, requests, base64
from datetime import datetime, timezone, timedelta
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
PRIORITY_FILE    = Path("drips_priority.json")

GITHUB_USERNAME  = os.getenv("GITHUB_USERNAME", "")
GITHUB_PASSWORD  = os.getenv("GITHUB_PASSWORD", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
HEADLESS         = os.getenv("HEADLESS", "true").lower() == "true"
ROTATE_HOURS     = float(os.getenv("ROTATE_HOURS", "0.5"))
MAX_SLOTS        = int(os.getenv("MAX_SLOTS", "15"))
MAX_PER_REPO     = int(os.getenv("MAX_PER_REPO", "2"))
FALLBACK_MSG     = "Hi, i can fix this"

# Base priority repos — more added automatically when assigned
BASE_PRIORITY = [
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

# ─── STATE ─────────────────────────────────────────────────────────────────────
def load_set(path: Path) -> set:
    if path.exists():
        try:
            return set(json.loads(path.read_text()))
        except Exception:
            pass
    return set()

def save_set(path: Path, data: set):
    path.write_text(json.dumps(list(data)))

def load_priority() -> list:
    if PRIORITY_FILE.exists():
        try:
            return json.loads(PRIORITY_FILE.read_text())
        except Exception:
            pass
    return list(BASE_PRIORITY)

def save_priority(repos: list):
    PRIORITY_FILE.write_text(json.dumps(repos, indent=2))

def add_to_priority(repo: str):
    """Auto-add repo to priority list when we get assigned there."""
    repos = load_priority()
    if repo and repo not in repos:
        repos.insert(0, repo)  # add to front
        save_priority(repos)
        log.info(f"Added to priority: {repo}")
        tg(f"*Priority repo added:* `{repo}`")

# ─── GEMINI ────────────────────────────────────────────────────────────────────
def gemini_message(title: str, body: str, repo: str) -> str:
    if not GEMINI_API_KEY:
        return FALLBACK_MSG
    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": (
                    f"Write a 1-2 sentence GitHub issue application.\n"
                    f"Repo: {repo}\nIssue: {title}\nDescription: {(body or '')[:200]}\n\n"
                    f"Rules: be specific, mention the issue, ask to be assigned, under 40 words, no emojis.\n"
                    f"Return only the message."
                )}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 80}
            },
            timeout=10
        )
        if r.status_code == 200:
            return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return FALLBACK_MSG
    except Exception:
        return FALLBACK_MSG

# ─── WAVE TOKEN ────────────────────────────────────────────────────────────────
def get_cookie_value(name: str) -> str:
    try:
        data = json.loads(SESSION_FILE.read_text())
        for c in (data.get("cookies") or []):
            if c.get("name") == name:
                return c.get("value", "")
    except Exception:
        pass
    return ""

def token_valid(token: str) -> bool:
    if not token:
        return False
    try:
        parts = token.split(".")
        if len(parts) == 3:
            pad = 4 - len(parts[1]) % 4
            payload = json.loads(base64.b64decode(parts[1] + "=" * pad))
            exp = payload.get("exp", 0)
            remaining = exp - datetime.now(timezone.utc).timestamp()
            log.info(f"Token expires in {remaining/60:.1f} min")
            return remaining > 60
    except Exception:
        pass
    return False

def refresh_token() -> str:
    """Get new access token using refresh token — no browser needed."""
    refresh = get_cookie_value("wave_refresh_token")
    if not refresh:
        return ""
    try:
        r = requests.post(
            f"{WAVE_API}/auth/refresh",
            json={"refreshToken": refresh},
            headers={"content-type": "application/json"},
            timeout=15
        )
        log.info(f"Token refresh: {r.status_code} {r.text[:100]}")
        if r.status_code == 200:
            data = r.json()
            token = data.get("accessToken") or data.get("access_token") or data.get("token") or ""
            if token:
                # Save new token to session file
                try:
                    sess = json.loads(SESSION_FILE.read_text())
                    now  = datetime.now(timezone.utc).timestamp()
                    for c in (sess.get("cookies") or []):
                        if c.get("name") == "wave_access_token":
                            c["value"]   = token
                            c["expires"] = now + 900
                    SESSION_FILE.write_text(json.dumps(sess))
                except Exception:
                    pass
                return token
    except Exception as e:
        log.error(f"refresh_token error: {e}")
    return ""

def get_valid_token() -> str:
    token = get_cookie_value("wave_access_token")
    if token_valid(token):
        return token
    log.info("Token expired — refreshing...")
    new_token = refresh_token()
    if new_token:
        return new_token
    log.warning("Refresh failed — need browser session")
    return ""

# ─── HEADERS ───────────────────────────────────────────────────────────────────
def hdrs(token: str = "") -> dict:
    t = token or get_valid_token() or get_cookie_value("wave_access_token")
    h = {
        "content-type": "application/json",
        "referer": "https://www.drips.network/",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36",
        "x-timezone": "Africa/Lagos",
    }
    if t:
        h["authorization"] = f"Bearer {t}"
    # Also add cookies
    try:
        data = json.loads(SESSION_FILE.read_text())
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in (data.get("cookies") or [])])
        if cookie_str:
            h["cookie"] = cookie_str
    except Exception:
        pass
    return h

# ─── BROWSER SESSION ───────────────────────────────────────────────────────────
def get_browser_session() -> str:
    """Open browser, refresh session cookies and extract wave token."""
    log.info("Opening browser for fresh session...")
    token = ""

    with sync_playwright() as pw:
        storage = str(SESSION_FILE) if SESSION_FILE.exists() else None
        browser = pw.chromium.launch(headless=HEADLESS, args=["--no-sandbox"])
        ctx = browser.new_context(
            storage_state=storage,
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/150.0.0.0 Safari/537.36"
        )
        page = ctx.new_page()
        tokens = []

        def on_req(req):
            if "wave-api.drips.network" in req.url:
                w = req.headers.get("authorization", "").replace("Bearer ", "")
                if w and len(w) > 20: tokens.append(w)

        page.on("request", on_req)

        # Load main page first
        try:
            page.goto(DRIPS_URL, wait_until="networkidle", timeout=60000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)

        # Check if logged in
        body = page.inner_text("body").lower()
        if "log in" in body or "sign in" in body:
            log.error("Not logged in! Run: python bot.py --login --platform drips")
            browser.close()
            return ""

        # Navigate to a specific issue to trigger auth API calls
        # Use a known issue URL format
        issue_links = page.query_selector_all("a[href*='/wave/stellar/issues/']")
        if issue_links:
            href = issue_links[0].get_attribute("href") or ""
            issue_url = href if href.startswith("http") else f"https://www.drips.network{href}"
            log.info(f"Navigating to issue: {issue_url}")
            try:
                page.goto(issue_url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)
            except PWTimeout:
                pass

        # Save refreshed session
        SESSION_FILE.parent.mkdir(exist_ok=True)
        ctx.storage_state(path=str(SESSION_FILE))

        # Extract token from cookies
        if tokens:
            token = tokens[-1]
            # Also save to session file
            try:
                sess = json.loads(SESSION_FILE.read_text())
                now  = datetime.now(timezone.utc).timestamp()
                found = False
                for c in (sess.get("cookies") or []):
                    if c.get("name") == "wave_access_token":
                        c["value"]   = token
                        c["expires"] = now + 900
                        found = True
                if not found:
                    (sess.setdefault("cookies", [])).append({
                        "name": "wave_access_token", "value": token,
                        "domain": ".drips.network", "path": "/",
                        "expires": now + 900, "httpOnly": False,
                        "secure": True, "sameSite": "Lax"
                    })
                SESSION_FILE.write_text(json.dumps(sess))
            except Exception as e:
                log.error(f"Could not update session: {e}")
        else:
            # No token from requests, try reading from cookies
            token = get_cookie_value("wave_access_token")
            log.info(f"No token from requests, reading from cookies: {'found' if token else 'not found'}")

        browser.close()

    log.info(f"Browser session done: token={'yes' if token else 'no'}")
    return token

# ─── REPO HELPER ───────────────────────────────────────────────────────────────
def get_repo(issue: dict) -> str:
    repo = issue.get("repo") or issue.get("repository") or ""
    if isinstance(repo, dict):
        return repo.get("fullName") or repo.get("full_name") or ""
    for field in ["gitHubIssueUrl", "htmlUrl", "html_url"]:
        url = issue.get(field) or ""
        if "github.com" in url:
            parts = url.replace("https://github.com/", "").split("/")
            if len(parts) >= 2:
                return f"{parts[0]}/{parts[1]}"
    return str(repo)

def is_priority(issue: dict, priority_repos: list) -> bool:
    repo = get_repo(issue).lower()
    return any(p.lower() in repo or repo in p.lower() for p in priority_repos)

# ─── API CALLS ─────────────────────────────────────────────────────────────────
def fetch_issues(token: str) -> list[dict]:
    """Fetch latest 100 open available issues."""
    try:
        r = requests.get(
            f"{WAVE_API}/issues?limit=100&offset=0&waveProgramId={WAVE_PROGRAM_ID}&state=open&sortBy=updatedAt",
            headers=hdrs(token), timeout=30
        )
        log.info(f"Fetch issues: {r.status_code}")
        if r.status_code != 200:
            return []
        data  = r.json()
        batch = data.get("data") or []
        total = (data.get("pagination") or {}).get("total") or len(batch)
        log.info(f"Total open: {total} | Fetched: {len(batch)}")
        # Filter out issues that already have accepted applications
        available = [i for i in batch if not (i.get("acceptedApplicationsCount") or 0)]
        log.info(f"Available (no accepted app): {len(available)}")
        return available
    except Exception as e:
        log.error(f"fetch_issues: {e}")
        return []

def fetch_my_apps(token: str) -> list[dict]:
    """Fetch my current applications."""
    for url in [
        f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/applications?status=pending&limit=50",
        f"{WAVE_API}/user/applications?waveProgramId={WAVE_PROGRAM_ID}&status=pending&limit=50",
        f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/applications?limit=50",
    ]:
        try:
            r = requests.get(url, headers=hdrs(token), timeout=15)
            log.info(f"My apps [{r.status_code}] from {url.split('?')[0].split('/')[-2:]}: {r.text[:80]}")
            if r.status_code == 200:
                d = r.json()
                result = d if isinstance(d, list) else (d.get("data") or d.get("applications") or [])
                if isinstance(result, list) and result:
                    log.info(f"Found {len(result)} active applications")
                    return result
        except Exception as e:
            log.error(f"fetch_my_apps: {e}")
    log.warning("Could not fetch my applications — assuming 0 active")
    return []

def fetch_assigned(token: str) -> list[dict]:
    """Fetch issues assigned to me — to auto-add repos to priority."""
    for url in [
        f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/applications?status=accepted&limit=50",
        f"{WAVE_API}/user/applications?waveProgramId={WAVE_PROGRAM_ID}&status=accepted&limit=50",
    ]:
        try:
            r = requests.get(url, headers=hdrs(token), timeout=15)
            if r.status_code == 200:
                d = r.json()
                result = d if isinstance(d, list) else (d.get("data") or d.get("applications") or [])
                if isinstance(result, list):
                    return result
        except Exception:
            pass
    return []

def withdraw_app(app: dict, token: str) -> bool:
    app_id   = app.get("id")
    issue    = app.get("issue") or {}
    issue_id = app.get("issueId") or issue.get("id")
    title    = issue.get("title") or "Unknown"
    if not app_id or not issue_id:
        return False
    try:
        r = requests.delete(
            f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/issues/{issue_id}/applications/{app_id}",
            headers=hdrs(token), timeout=15
        )
        if r.status_code in (200, 204):
            log.info(f"Withdrawn: {title[:50]}")
            return True
        log.warning(f"Withdraw {r.status_code}: {r.text[:60]}")
        return False
    except Exception as e:
        log.error(f"withdraw: {e}")
        return False

def apply_issue(issue: dict, token: str) -> str:
    """Returns: ok | taken | quota | expired | error"""
    iid   = issue.get("id")
    title = issue.get("title", "")
    body  = issue.get("body", "")
    repo  = get_repo(issue)
    msg   = gemini_message(title, body, repo)

    try:
        r = requests.post(
            f"{WAVE_API}/wave-programs/{WAVE_PROGRAM_ID}/issues/{iid}/applications",
            headers=hdrs(token),
            json={"applicationText": msg},
            timeout=15
        )
        if r.status_code == 201:
            log.info(f"Applied [{repo}]: {title[:55]}")
            return "ok"

        err = ""
        try:    err = r.json().get("error", "")
        except: err = r.text[:80]

        if r.status_code == 400:
            if "accepted application" in err:              return "taken"
            if "already applied" in err.lower():          return "taken"
            if "quota" in err.lower() or "at most" in err: 
                log.warning(f"QUOTA: {err}")
                return "quota"
        if r.status_code in (401, 403): return "expired"
        if r.status_code == 429:
            time.sleep(5)
            return "error"

        log.warning(f"{r.status_code}: {err[:60]}")
        return "error"
    except Exception as e:
        log.error(f"apply: {e}")
        return "error"

# ─── MAIN CYCLE ────────────────────────────────────────────────────────────────
def run_cycle():
    log.info("=== Cycle start ===")

    # Get valid token
    token = get_valid_token()
    if not token:
        log.info("No valid token — trying browser...")
        token = get_browser_session()
    if not token:
        log.error("Cannot get token — aborting cycle")
        return

    # Load state
    seen     = load_set(SEEN_FILE)
    applied  = load_set(APPLIED_FILE)
    priority = load_priority()
    now_utc  = datetime.now(timezone.utc)

    # ── Check assigned issues → auto-add to priority ──────────────────────────
    assigned = fetch_assigned(token)
    for app in assigned:
        issue = app.get("issue") or {}
        repo  = get_repo(issue)
        if repo:
            add_to_priority(repo)
    priority = load_priority()

    # ── Fetch my active applications ─────────────────────────────────────────
    my_apps = fetch_my_apps(token)
    log.info(f"Active: {len(my_apps)}/{MAX_SLOTS}")

    # Count per repo
    repo_counts: dict[str, int] = {}
    for app in my_apps:
        repo = get_repo(app.get("issue") or app)
        repo_counts[repo] = repo_counts.get(repo, 0) + 1

    # ── Rotate stale applications (> ROTATE_HOURS) ───────────────────────────
    withdrawn = 0
    still_active = []
    for app in my_apps:
        try:
            applied_at = datetime.fromisoformat(
                (app.get("appliedAt") or app.get("createdAt") or "").replace("Z", "+00:00")
            )
            age_hrs = (now_utc - applied_at).total_seconds() / 3600
        except Exception:
            age_hrs = 0

        if age_hrs >= ROTATE_HOURS:
            issue    = app.get("issue") or {}
            issue_id = app.get("issueId") or issue.get("id")
            repo     = get_repo(issue)
            if withdraw_app(app, token):
                withdrawn += 1
                applied.discard(issue_id)
                repo_counts[repo] = max(0, repo_counts.get(repo, 1) - 1)
                time.sleep(0.3)
        else:
            still_active.append(app)

    if withdrawn:
        save_set(APPLIED_FILE, applied)
        log.info(f"Rotated {withdrawn} stale apps")

    # ── Fetch available issues ────────────────────────────────────────────────
    issues = fetch_issues(token)
    if not issues:
        log.info("No issues available")
        return

    # Update seen
    for i in issues:
        seen.add(i.get("id"))
    save_set(SEEN_FILE, seen)

    # ── Sort: priority first, then others ────────────────────────────────────
    not_applied = [i for i in issues if i.get("id") not in applied]
    priority_issues = [i for i in not_applied if is_priority(i, priority)]
    normal_issues   = [i for i in not_applied if not is_priority(i, priority)]
    apply_order     = priority_issues + normal_issues

    log.info(f"To apply: {len(apply_order)} ({len(priority_issues)} priority)")

    # ── Fill ALL available slots ──────────────────────────────────────────────
    free_slots    = MAX_SLOTS - len(still_active)
    applied_count = 0
    log.info(f"Free slots: {free_slots}")

    # Count repos that still have slots available
    skipped_repos = set()

    for issue in apply_order:
        if applied_count >= free_slots:
            log.info(f"All {MAX_SLOTS} slots filled")
            break

        iid  = issue.get("id")
        repo = get_repo(issue)

        # Max 2 per repo — skip but continue to next repo
        if repo_counts.get(repo, 0) >= MAX_PER_REPO:
            skipped_repos.add(repo)
            continue

        result = apply_issue(issue, token)

        if result == "ok":
            applied.add(iid)
            save_set(APPLIED_FILE, applied)
            applied_count += 1
            repo_counts[repo] = repo_counts.get(repo, 0) + 1
            time.sleep(0.8)

        elif result == "taken":
            applied.add(iid)
            save_set(APPLIED_FILE, applied)

        elif result == "quota":
            log.info("Quota reached")
            break

        elif result == "expired":
            log.warning("Token expired mid-cycle — refreshing")
            token = get_valid_token() or get_browser_session()
            if not token:
                break

        else:
            time.sleep(0.3)

    log.info(f"Applied this cycle: {applied_count}")
    if applied_count > 0:
        tg(
            f"*Drips Bot: {applied_count} Applied*\n"
            f"Slots: {len(still_active) + applied_count}/{MAX_SLOTS}\n"
            f"Priority: {len(priority_issues)} available"
        )

    log.info("=== Cycle end ===")

# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()

    log.info(f"Drips Smart Bot v3 | Slots:{MAX_SLOTS} | Per-repo:{MAX_PER_REPO} | Rotate:{ROTATE_HOURS}h")
    tg(
        f"*Drips Smart Bot Started*\n"
        f"Priority repos: {len(load_priority())}\n"
        f"Max per repo: {MAX_PER_REPO}\n"
        f"Rotate after: {ROTATE_HOURS}h\n"
        f"Gemini: {'on' if GEMINI_API_KEY else 'off (fallback)'}"
    )

    while True:
        try:
            run_cycle()
        except Exception as e:
            log.error(f"Cycle crashed: {e}")
            tg(f"*Bot Error:* `{str(e)[:100]}`")

        if not args.watch:
            break

        log.info("Sleeping 5 min...")
        time.sleep(300)

if __name__ == "__main__":
    main()