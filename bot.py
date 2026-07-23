#!/usr/bin/env python3
"""
Browser Automation Bot — GrantFox + Drips Wave (Stellar)
=========================================================
Usage:
  python bot.py                          # run forever (both platforms)
  python bot.py --platform drips         # Drips Wave only
  python bot.py --platform grantfox      # GrantFox only
  python bot.py --login                  # one-time login (both platforms)
  python bot.py --login --platform drips # one-time login (Drips only)
  python bot.py --once                   # single cycle then exit
"""

import os
import sys
import io
import json
import time
import logging
import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PWTimeout

load_dotenv()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
GITHUB_USERNAME        = os.getenv("GITHUB_USERNAME", "")
GITHUB_PASSWORD        = os.getenv("GITHUB_PASSWORD", "")
GITHUB_OTP_SECRET      = os.getenv("GITHUB_OTP_SECRET", "")
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "")

GRANTFOX_URL           = "https://contribute.grantfox.xyz"
DRIPS_URL              = "https://www.drips.network/wave/stellar/issues"

ACCEPTANCE_TIMEOUT_HRS = float(os.getenv("ACCEPTANCE_TIMEOUT_HOURS", "24"))
POLL_INTERVAL_SEC      = int(os.getenv("POLL_INTERVAL_SECONDS", "1800"))
MAX_ACTIVE             = int(os.getenv("MAX_ACTIVE_APPLICATIONS", "3"))
HEADLESS               = os.getenv("HEADLESS", "true").lower() == "true"

SESSION_DIR            = Path("sessions")
STATE_FILE             = Path("state.json")
LOG_FILE               = "bot.log"

# ─── LOGGING (UTF-8 safe on Windows) ──────────────────────────────────────────
def _safe(msg: str) -> str:
    """Strip non-ASCII so Windows cp1252 terminal never crashes."""
    return msg.encode("ascii", errors="replace").decode("ascii")

class SafeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        record.msg = _safe(str(record.msg))
        super().emit(record)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_file_h   = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_h.setFormatter(_fmt)
_stream_h = SafeStreamHandler(sys.stdout)
_stream_h.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_file_h, _stream_h])
log = logging.getLogger(__name__)

# ─── TELEGRAM ──────────────────────────────────────────────────────────────────
def tg(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ─── STATE ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {
        "applications": {},
        "skipped": [],
        "accepted": [],
        "stats": {"applied": 0, "accepted": 0, "timed_out": 0}
    }

def save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, indent=2))

# ─── BROWSER SETUP ─────────────────────────────────────────────────────────────
def make_context(pw, platform: str) -> BrowserContext:
    SESSION_DIR.mkdir(exist_ok=True)
    storage = SESSION_DIR / f"{platform}.json"
    browser = pw.chromium.launch(
        headless=HEADLESS,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    )
    ctx = browser.new_context(
        storage_state=str(storage) if storage.exists() else None,
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )
    return ctx

def save_session(ctx: BrowserContext, platform: str):
    SESSION_DIR.mkdir(exist_ok=True)
    ctx.storage_state(path=str(SESSION_DIR / f"{platform}.json"))
    log.info(f"Session saved for {platform}")

# ─── GITHUB OAUTH HELPER ───────────────────────────────────────────────────────
def github_login(page: Page):
    log.info("Performing GitHub OAuth login...")
    page.wait_for_selector("input[name='login']", timeout=10000)
    page.fill("input[name='login']", GITHUB_USERNAME)
    page.fill("input[name='password']", GITHUB_PASSWORD)
    page.click("input[type='submit']")
    page.wait_for_timeout(2000)

    if page.query_selector("input[name='app_otp'], input#otp"):
        otp = get_totp()
        if otp:
            page.fill("input[name='app_otp'], input#otp", otp)
            page.click("button[type='submit']")
            page.wait_for_timeout(2000)
        else:
            log.warning("2FA required — enter your SMS code in the browser")
            input("Enter your 2FA code in the browser, then press ENTER here...")

    if page.query_selector("button[name='authorize']"):
        page.click("button[name='authorize']")
        page.wait_for_timeout(2000)

    log.info("GitHub OAuth complete")

def get_totp() -> str:
    secret = GITHUB_OTP_SECRET
    if not secret:
        return ""
    try:
        import pyotp
        return pyotp.TOTP(secret).now()
    except ImportError:
        return ""

# ══════════════════════════════════════════════════════════════════════════════
#  GRANTFOX
# ══════════════════════════════════════════════════════════════════════════════
class GrantFoxBot:
    PLATFORM = "grantfox"

    def __init__(self, pw):
        self.ctx = make_context(pw, self.PLATFORM)
        self.page = self.ctx.new_page()

    def close(self):
        save_session(self.ctx, self.PLATFORM)
        self.ctx.close()

    def ensure_logged_in(self) -> bool:
        try:
            self.page.goto(f"{GRANTFOX_URL}/issues", wait_until="networkidle", timeout=60000)
        except PWTimeout:
            log.warning("GrantFox: slow load on login check, continuing...")
        self.page.wait_for_timeout(3000)

        body = self.page.inner_text("body").lower()
        not_logged_in = (
            "sign in" in body
            or self.page.query_selector("a[href*='login'], button:has-text('Sign in')")
        )

        if not_logged_in:
            log.info("GrantFox: not logged in, starting OAuth...")
            btn = self.page.query_selector(
                "a:has-text('Sign In'), a:has-text('Sign in'), "
                "a:has-text('Log in'), button:has-text('Sign In'), "
                "button:has-text('Log in'), a[href*='login']"
            )
            if btn:
                btn.click()
            else:
                self.page.goto(f"{GRANTFOX_URL}/login", wait_until="networkidle", timeout=30000)
            self.page.wait_for_timeout(2000)

            if "github.com/login" in self.page.url:
                github_login(self.page)

            try:
                self.page.wait_for_url(f"{GRANTFOX_URL}/**", timeout=30000)
            except PWTimeout:
                log.warning("GrantFox: wait_for_url timed out after login")

            save_session(self.ctx, self.PLATFORM)
            tg("GrantFox: logged in successfully!")
            return True

        log.info("GrantFox: session active")
        return True

    def register_for_campaign(self, issue_url: str) -> bool:
        """
        GrantFox requires registering for a campaign before you can apply.
        If we see 'CLICK HERE TO REGISTER' on an issue page, click it.
        Returns True if registration was done or already registered.
        """
        try:
            # Go to issues list to find the register button
            self.page.goto(f"{GRANTFOX_URL}/issues", wait_until="networkidle", timeout=60000)
            self.page.wait_for_timeout(2000)
            body = self.page.inner_text("body").lower()

            if "not registered" in body or "click here to register" in body:
                log.info("GrantFox: campaign registration required — clicking Register...")
                reg_btn = self.page.query_selector(
                    "a:has-text('REGISTER'), button:has-text('REGISTER'), "
                    "a:has-text('Register'), a:has-text('register'), "
                    "[href*='register'], [href*='campaign']"
                )
                if reg_btn:
                    reg_btn.click()
                    self.page.wait_for_timeout(3000)
                    log.info("GrantFox: registration clicked")
                    tg("GrantFox: registered for campaign! Now applying for issues...")
                    return True
                else:
                    # Alert user — must register manually
                    log.warning("GrantFox: REGISTRATION REQUIRED but could not find button")
                    log.warning("GrantFox: Open https://contribute.grantfox.xyz/issues and click REGISTER manually, then restart the bot")
                    tg(
                        "*GrantFox: Action Required!*\n\n"
                        "You need to register for the campaign before applying.\n"
                        "Open: https://contribute.grantfox.xyz/issues\n"
                        "Click the REGISTER button, then restart the bot."
                    )
                    return False
            return True  # Already registered
        except Exception as e:
            log.error(f"GrantFox registration error: {e}")
            return False

    def fetch_open_issues(self) -> list[dict]:
        issues = []
        try:
            self.page.goto(f"{GRANTFOX_URL}/issues", wait_until="networkidle", timeout=60000)
            self.page.wait_for_timeout(4000)

            body_text = self.page.inner_text("body")
            safe_preview = _safe(body_text[:200])
            log.info(f"GrantFox page preview: {safe_preview}")

            # Re-login if session expired
            if "sign in" in body_text.lower():
                log.warning("GrantFox: session expired — re-logging in")
                self.ensure_logged_in()
                self.page.goto(f"{GRANTFOX_URL}/issues", wait_until="networkidle", timeout=60000)
                self.page.wait_for_timeout(4000)
                body_text = self.page.inner_text("body")

            # Check campaign registration
            if "not registered" in body_text.lower():
                registered = self.register_for_campaign(self.page.url)
                if not registered:
                    return []
                # Reload after registration
                self.page.goto(f"{GRANTFOX_URL}/issues", wait_until="networkidle", timeout=60000)
                self.page.wait_for_timeout(3000)

            # Find all issue links — GrantFox uses <a href="/org/.../issue/N"> wrapping each card
            all_links = self.page.query_selector_all("a[href]")
            log.info(f"GrantFox: total links on page: {len(all_links)}")

            seen_urls = set()
            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    if not href:
                        continue

                    # GrantFox issue URL pattern: /org/.../repo/.../issue/N
                    if "/issue/" not in href:
                        continue

                    url = href if href.startswith("http") else f"{GRANTFOX_URL}{href}"
                    if url in seen_urls:
                        continue

                    card_text = link.inner_text().strip()
                    if not card_text or len(card_text) < 5:
                        continue

                    # Must contain an issue number like #123
                    if not re.search(r"#\d+", card_text):
                        continue

                    # Skip if already assigned/applied
                    if any(x in card_text.lower() for x in ["assigned", "in progress"]):
                        continue

                    # Get the longest line as the title (that's the issue title)
                    lines = [l.strip() for l in card_text.splitlines() if l.strip()]
                    title = max(lines, key=len) if lines else card_text[:80]

                    seen_urls.add(url)
                    issues.append({
                        "title": title,
                        "url": url,
                        "platform": self.PLATFORM
                    })
                    log.info(f"GrantFox issue: {_safe(title[:60])} -> {url}")

                except Exception:
                    continue

        except PWTimeout:
            log.warning("GrantFox: timed out loading issues page")
        except Exception as e:
            log.error(f"GrantFox fetch_issues error: {e}")

        log.info(f"GrantFox: found {len(issues)} open issues")
        return issues

    def apply(self, issue: dict) -> bool:
        try:
            self.page.goto(issue["url"], wait_until="networkidle", timeout=60000)
            self.page.wait_for_timeout(3000)

            page_body = self.page.inner_text("body").lower()

            # Check for campaign registration requirement on issue page too
            if "not registered" in page_body:
                registered = self.register_for_campaign(issue["url"])
                if not registered:
                    return False
                self.page.goto(issue["url"], wait_until="networkidle", timeout=60000)
                self.page.wait_for_timeout(3000)
                page_body = self.page.inner_text("body").lower()

            # Check if already applied
            if "applied" in page_body or "withdraw" in page_body:
                log.info(f"GrantFox: already applied for {_safe(issue['title'][:40])}")
                return True

            # Find Apply button — use JS click to bypass disabled state issues
            btn = self.page.query_selector(
                "button:has-text('Apply'), "
                "button:has-text('Request Assignment'), "
                "button:has-text('Claim'), "
                "a:has-text('Apply')"
            )

            if not btn:
                log.warning(f"GrantFox: no Apply button on {issue['url']}")
                # Screenshot for debugging
                self.page.screenshot(path=f"debug_apply_{issue['url'].split('/')[-1]}.png")
                log.info("GrantFox: saved screenshot for debugging")
                return False

            # Check if button is disabled
            is_disabled = btn.get_attribute("disabled")
            if is_disabled is not None:
                log.warning(f"GrantFox: Apply button is disabled on {issue['url']} — may need campaign registration")
                return False

            btn.click(timeout=10000)
            self.page.wait_for_timeout(2000)

            # Confirm dialog
            confirm = self.page.query_selector(
                "button:has-text('Confirm'), button:has-text('Submit'), "
                "button:has-text('Yes'), button:has-text('OK')"
            )
            if confirm:
                confirm.click()
                self.page.wait_for_timeout(1500)

            log.info(f"GrantFox: applied for {_safe(issue['title'][:50])}")
            return True

        except PWTimeout:
            log.warning(f"GrantFox: apply timed out on {issue['url']} — button may be disabled")
            return False
        except Exception as e:
            log.error(f"GrantFox apply error on {issue['url']}: {e}")
            return False

    def check_assigned(self, issue: dict) -> bool:
        try:
            self.page.goto(issue["url"], wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(2000)
            content = self.page.inner_text("body").lower()
            return (
                GITHUB_USERNAME.lower() in content
                and any(w in content for w in ["assigned to", "assignee", "working on"])
            )
        except Exception:
            return False

    def cancel_application(self, issue: dict):
        try:
            self.page.goto(issue["url"], wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(2000)
            btn = self.page.query_selector(
                "button:has-text('Withdraw'), button:has-text('Cancel'), button:has-text('Remove')"
            )
            if btn:
                btn.click()
                self.page.wait_for_timeout(1500)
                log.info(f"GrantFox: cancelled application for {_safe(issue['title'][:40])}")
            else:
                log.warning("GrantFox: no cancel button found")
        except Exception as e:
            log.error(f"GrantFox cancel error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
#  DRIPS WAVE
# ══════════════════════════════════════════════════════════════════════════════
class DripsBot:
    PLATFORM = "drips"

    def __init__(self, pw):
        self.ctx = make_context(pw, self.PLATFORM)
        self.page = self.ctx.new_page()

    def close(self):
        save_session(self.ctx, self.PLATFORM)
        self.ctx.close()

    def ensure_logged_in(self) -> bool:
        try:
            self.page.goto(DRIPS_URL, wait_until="networkidle", timeout=60000)
        except PWTimeout:
            log.warning("Drips Wave: slow load on login check, continuing...")
        self.page.wait_for_timeout(3000)

        body = self.page.inner_text("body").lower()
        if "log in" in body or "sign in" in body or self.page.query_selector("a[href*='/login']"):
            log.info("Drips Wave: not logged in, starting OAuth...")
            login_btn = self.page.query_selector(
                "a:has-text('Log in'), button:has-text('Sign in'), a[href*='/login']"
            )
            if login_btn:
                login_btn.click()
            self.page.wait_for_timeout(2000)

            if "github.com/login" in self.page.url:
                github_login(self.page)

            try:
                self.page.wait_for_url("https://www.drips.network/**", timeout=30000)
            except PWTimeout:
                log.warning("Drips: wait_for_url timed out after login")

            save_session(self.ctx, self.PLATFORM)
            tg("Drips Wave: logged in successfully!")
            return True

        log.info("Drips Wave: session active")
        return True

    def fetch_open_issues(self) -> list[dict]:
        issues = []
        try:
            self.page.goto(DRIPS_URL, wait_until="networkidle", timeout=60000)
            self.page.wait_for_timeout(4000)

            body_text = self.page.inner_text("body")
            log.info(f"Drips page preview: {_safe(body_text[:200])}")

            all_links = self.page.query_selector_all("a[href]")
            log.info(f"Drips: total links on page: {len(all_links)}")

            seen_urls = set()
            for link in all_links:
                try:
                    href = link.get_attribute("href") or ""
                    if not href or "/issues/" not in href and "/issue/" not in href:
                        continue

                    url = href if href.startswith("http") else f"https://www.drips.network{href}"
                    if url in seen_urls:
                        continue

                    card_text = link.inner_text().strip()
                    if not card_text or len(card_text) < 5:
                        continue
                    if any(x in card_text.lower() for x in ["applied", "assigned", "closed"]):
                        continue

                    lines = [l.strip() for l in card_text.splitlines() if l.strip()]
                    title = max(lines, key=len) if lines else card_text[:80]

                    seen_urls.add(url)
                    issues.append({
                        "title": title,
                        "url": url,
                        "platform": self.PLATFORM
                    })
                    log.info(f"Drips issue: {_safe(title[:60])}")

                except Exception:
                    continue

        except PWTimeout:
            log.warning("Drips Wave: timed out loading issues")
        except Exception as e:
            log.error(f"Drips fetch_issues error: {e}")

        log.info(f"Drips Wave: found {len(issues)} open issues")
        return issues

    def apply(self, issue: dict) -> bool:
        try:
            self.page.goto(issue["url"], wait_until="networkidle", timeout=60000)
            self.page.wait_for_timeout(3000)

            page_body = self.page.inner_text("body").lower()
            if "applied" in page_body or "withdraw" in page_body:
                log.info(f"Drips: already applied for {_safe(issue['title'][:40])}")
                return True

            btn = self.page.query_selector(
                "button:has-text('Apply'), "
                "button:has-text('Apply to issue'), "
                "a:has-text('Apply')"
            )
            if not btn:
                log.warning(f"Drips: no Apply button on {issue['url']}")
                return False

            is_disabled = btn.get_attribute("disabled")
            if is_disabled is not None:
                log.warning(f"Drips: Apply button is disabled on {issue['url']}")
                return False

            btn.click(timeout=10000)
            self.page.wait_for_timeout(2000)

            confirm = self.page.query_selector(
                "button:has-text('Confirm'), button:has-text('Submit application'), "
                "button:has-text('Yes')"
            )
            if confirm:
                confirm.click()
                self.page.wait_for_timeout(1500)

            log.info(f"Drips Wave: applied for {_safe(issue['title'][:50])}")
            return True

        except PWTimeout:
            log.warning(f"Drips: apply timed out on {issue['url']}")
            return False
        except Exception as e:
            log.error(f"Drips apply error on {issue['url']}: {e}")
            return False

    def check_assigned(self, issue: dict) -> bool:
        try:
            self.page.goto(issue["url"], wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(2000)
            content = self.page.inner_text("body").lower()
            return (
                GITHUB_USERNAME.lower() in content
                and any(w in content for w in ["assigned", "you are working on"])
            )
        except Exception:
            return False

    def cancel_application(self, issue: dict):
        try:
            self.page.goto(issue["url"], wait_until="domcontentloaded", timeout=60000)
            self.page.wait_for_timeout(2000)
            btn = self.page.query_selector(
                "button:has-text('Withdraw'), button:has-text('Cancel application')"
            )
            if btn:
                btn.click()
                self.page.wait_for_timeout(1500)
                log.info(f"Drips: cancelled for {_safe(issue['title'][:40])}")
        except Exception as e:
            log.error(f"Drips cancel error: {e}")


# ─── PLATFORM REGISTRY ─────────────────────────────────────────────────────────
ALL_BOTS = {"grantfox": GrantFoxBot, "drips": DripsBot}
PLATFORM_URLS = {"grantfox": f"{GRANTFOX_URL}/issues", "drips": DRIPS_URL}


# ─── ONE-TIME LOGIN FLOW ───────────────────────────────────────────────────────
def do_login(platforms: list[str]):
    log.info(f"Starting one-time login for: {', '.join(platforms)} (headless=False)...")
    with sync_playwright() as pw:
        for platform in platforms:
            url = PLATFORM_URLS[platform]
            SESSION_DIR.mkdir(exist_ok=True)
            browser = pw.chromium.launch(headless=False)
            ctx = browser.new_context(viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(url)
            print(f"\n>>> {platform.upper()}: Log in in the browser window.")
            print(">>> When fully logged in and issues are visible, press ENTER here.")
            print(">>> DO NOT close the browser before pressing ENTER.")
            try:
                input()
                ctx.storage_state(path=str(SESSION_DIR / f"{platform}.json"))
                print(f">>> Session saved for {platform}!")
            except Exception as e:
                print(f">>> ERROR saving {platform} session: {e}")
            finally:
                browser.close()
    print(f"\nLogin complete for: {', '.join(platforms)}")
    print("Run `python bot.py` to start the bot.")


# ─── MAIN CYCLE ────────────────────────────────────────────────────────────────
def run_cycle(state: dict, platforms: list[str]):
    log.info(f"=== Cycle start | platforms: {', '.join(platforms)} ===")

    with sync_playwright() as pw:
        bots = {name: cls(pw) for name, cls in ALL_BOTS.items() if name in platforms}

        try:
            # 1. Login checks
            for name, bot in bots.items():
                try:
                    bot.ensure_logged_in()
                except Exception as e:
                    log.error(f"{name}: login check failed: {e}")

            # 2. Check active applications
            for key, app in list(state["applications"].items()):
                platform = app["platform"]
                if platform not in platforms:
                    continue
                bot = bots.get(platform)
                if not bot:
                    continue

                applied_at = datetime.fromisoformat(app["applied_at"])
                deadline   = applied_at + timedelta(hours=ACCEPTANCE_TIMEOUT_HRS)
                now        = datetime.utcnow()

                try:
                    if bot.check_assigned(app):
                        state["accepted"].append({**app, "accepted_at": now.isoformat()})
                        del state["applications"][key]
                        state["stats"]["accepted"] += 1
                        save_state(state)
                        tg(
                            f"Issue Assigned!\n\n"
                            f"Platform: {platform.upper()}\n"
                            f"Title: {app['title']}\n"
                            f"URL: {app['url']}\n\n"
                            f"Time to ship it, Yusuf!"
                        )
                        log.info(f"ACCEPTED: {_safe(app['title'][:50])}")

                    elif now >= deadline:
                        bot.cancel_application(app)
                        state["skipped"].append(app["url"])
                        del state["applications"][key]
                        state["stats"]["timed_out"] += 1
                        save_state(state)
                        tg(
                            f"Application Timed Out\n\n"
                            f"Platform: {platform.upper()}\n"
                            f"Title: {app['title']}\n"
                            f"Cancelled - hunting next issue..."
                        )
                    else:
                        hrs_left = (deadline - now).total_seconds() / 3600
                        log.info(f"Pending ({platform}): {_safe(app['title'][:40])} - {hrs_left:.1f}h left")

                except Exception as e:
                    log.error(f"Status check error for {key}: {e}")

            # 3. Find and apply for new issues
            if len(state["applications"]) < MAX_ACTIVE:
                slots = MAX_ACTIVE - len(state["applications"])
                applied_this_cycle = 0

                for name, bot in bots.items():
                    if applied_this_cycle >= slots:
                        break
                    try:
                        issues = bot.fetch_open_issues()
                        for issue in issues:
                            if applied_this_cycle >= slots:
                                break
                            key = f"{name}:{issue['url']}"
                            if key in state["applications"] or issue["url"] in state["skipped"]:
                                continue

                            success = bot.apply(issue)
                            if success:
                                now = datetime.utcnow()
                                state["applications"][key] = {
                                    "platform":   name,
                                    "url":        issue["url"],
                                    "title":      issue["title"],
                                    "applied_at": now.isoformat()
                                }
                                state["stats"]["applied"] += 1
                                applied_this_cycle += 1
                                save_state(state)
                                tg(
                                    f"Applied for Issue!\n\n"
                                    f"Platform: {name.upper()}\n"
                                    f"Title: {issue['title']}\n"
                                    f"URL: {issue['url']}\n"
                                    f"Auto-cancel in {ACCEPTANCE_TIMEOUT_HRS:.0f}h if no response."
                                )
                                time.sleep(3)
                    except Exception as e:
                        log.error(f"{name}: issue hunt error: {e}")

        finally:
            for bot in bots.values():
                try:
                    bot.close()
                except Exception:
                    pass

    s = state["stats"]
    log.info(f"Stats | Applied:{s['applied']} Accepted:{s['accepted']} Timed-out:{s['timed_out']}")
    log.info("=== Cycle end ===")


# ─── ENTRY POINT ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="GrantFox + Drips Wave Issue Bot")
    parser.add_argument("--login",    action="store_true")
    parser.add_argument("--once",     action="store_true")
    parser.add_argument("--platform", choices=["grantfox", "drips", "both"], default="both")
    args = parser.parse_args()

    platforms = ["grantfox", "drips"] if args.platform == "both" else [args.platform]

    if args.login:
        do_login(platforms)
        return

    if not GITHUB_USERNAME:
        log.error("GITHUB_USERNAME not set in .env")
        sys.exit(1)

    label = "GrantFox + Drips Wave" if len(platforms) > 1 else platforms[0].upper()
    tg(
        f"Browser Bot Started\n\n"
        f"User: {GITHUB_USERNAME}\n"
        f"Platform: {label}\n"
        f"Timeout: {ACCEPTANCE_TIMEOUT_HRS:.0f}h\n"
        f"Poll: every {POLL_INTERVAL_SEC//60} min"
    )

    state = load_state()

    if args.once:
        run_cycle(state, platforms)
        return

    while True:
        try:
            run_cycle(state, platforms)
        except Exception as e:
            log.error(f"Cycle crashed: {e}")
            tg(f"Bot Error\n{e}")
        log.info(f"Sleeping {POLL_INTERVAL_SEC}s...")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
