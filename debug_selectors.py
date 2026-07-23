#!/usr/bin/env python3
"""
Selector debugger — run this ONCE to find the right CSS selectors
for the current GrantFox and Drips Wave UI.

Usage:
  python debug_selectors.py grantfox
  python debug_selectors.py drips
"""

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

SESSION_DIR = Path("sessions")

URLS = {
    "grantfox": "https://contribute.grantfox.xyz/issues",
    "drips":    "https://www.drips.network/wave/stellar/issues"
}

# Candidates to try for issue cards
CARD_SELECTORS = [
    "article",
    "li[class*='issue']",
    "[class*='IssueCard']",
    "[class*='issue-card']",
    "[class*='issue-item']",
    "[data-testid*='issue']",
    "[class*='IssueListItem']",
    "[class*='issue-row']",
]

# Candidates for Apply button
APPLY_SELECTORS = [
    "button:has-text('Apply')",
    "button:has-text('Apply to issue')",
    "button:has-text('Request Assignment')",
    "button:has-text('Claim')",
    "a:has-text('Apply')",
]

def debug(platform: str):
    url = URLS[platform]
    storage = SESSION_DIR / f"{platform}.json"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(
            storage_state=str(storage) if storage.exists() else None,
            viewport={"width": 1280, "height": 800}
        )
        page = ctx.new_page()
        page.goto(url)
        page.wait_for_timeout(4000)

        print(f"\n=== {platform.upper()} selector scan ===")
        print(f"Page URL: {page.url}")
        print(f"Page title: {page.title()}\n")

        # Find issue cards
        print("--- Issue card selectors ---")
        for sel in CARD_SELECTORS:
            try:
                els = page.query_selector_all(sel)
                if els:
                    sample = els[0].inner_text()[:80].replace("\n", " ").strip()
                    print(f"  ✓ {sel!r:50s} → {len(els)} elements | sample: {sample!r}")
                else:
                    print(f"  ✗ {sel!r}")
            except Exception as e:
                print(f"  ! {sel!r} → error: {e}")

        # Try to open first issue and find Apply button
        print("\n--- Apply button selectors (on issue detail page) ---")
        links = page.query_selector_all("a[href*='/issues/']")
        if links:
            first_href = links[0].get_attribute("href")
            if first_href:
                issue_url = first_href if first_href.startswith("http") else f"https://{platform}.xyz{first_href}"
                page.goto(issue_url)
                page.wait_for_timeout(3000)
                for sel in APPLY_SELECTORS:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            print(f"  ✓ {sel!r}")
                        else:
                            print(f"  ✗ {sel!r}")
                    except Exception as e:
                        print(f"  ! {sel!r} → {e}")
        else:
            print("  (no issue links found on listing page — may need to log in first)")

        print("\n>>> Pausing — inspect the browser, then press ENTER to close.")
        input()
        browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in URLS:
        print("Usage: python debug_selectors.py [grantfox|drips]")
        sys.exit(1)
    debug(sys.argv[1])
