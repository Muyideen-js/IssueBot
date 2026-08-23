#!/usr/bin/env python3
"""One-time desktop connector for linking a Drips session to the hosted portal."""

import argparse
import sys

import requests
from playwright.sync_api import sync_playwright


DRIPS_URL = "https://www.drips.network/wave/stellar/issues"


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect your Drips account to IssueBot")
    parser.add_argument("--portal", help="IssueBot portal URL")
    parser.add_argument("--code", help="One-time connection code")
    args = parser.parse_args()

    portal = (args.portal or input("IssueBot website URL: ")).strip().rstrip("/")
    code = (args.code or input("One-time connection code: ")).strip()
    if not portal.startswith("https://") and "localhost" not in portal:
        raise SystemExit("The portal must use HTTPS")

    print("Opening Drips. Sign in with your own GitHub account and complete any prompts.")
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 850})
        page = context.new_page()
        page.goto(DRIPS_URL, wait_until="domcontentloaded", timeout=60_000)
        input("When Drips shows your logged-in issue page, return here and press ENTER...")
        page.reload(wait_until="networkidle", timeout=60_000)
        session_state = context.storage_state()
        browser.close()

    session_state = {
        "cookies": [
            cookie
            for cookie in session_state.get("cookies", [])
            if str(cookie.get("domain", "")).lower().lstrip(".").endswith("drips.network")
        ],
        "origins": [
            origin
            for origin in session_state.get("origins", [])
            if "drips.network" in str(origin.get("origin", "")).lower()
        ],
    }

    response = requests.post(
        f"{portal}/api/connect-session",
        json={"code": code, "session": session_state},
        timeout=30,
    )
    if response.status_code != 200:
        try:
            error = response.json().get("error")
        except Exception:
            error = response.text
        raise SystemExit(f"Connection failed: {error}")
    print("Drips connected successfully. You can close this window.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
