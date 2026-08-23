"""Manual browser smoke check for a locally running IssueBot portal."""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import SessionLocal
from models import BotSettings, User


BASE_URL = "http://127.0.0.1:5000"
ADMIN_USERNAME = os.getenv("SMOKE_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("SMOKE_ADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD", "admin-password-123")
USER_USERNAME = "browser-user"
USER_PASSWORD = "browser-user-password-123"


with SessionLocal() as db:
    user = db.query(User).filter_by(username=USER_USERNAME).one_or_none()
    if not user:
        user = User(username=USER_USERNAME, must_change_password=False)
        user.set_password(USER_PASSWORD)
        db.add(user)
        db.flush()
        db.add(BotSettings(user_id=user.id))
    else:
        user.set_password(USER_PASSWORD)
        user.must_change_password = False
        user.is_active = True
    db.commit()


with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    console_errors = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

    page.goto(f"{BASE_URL}/admin/login", wait_until="networkidle")
    assert page.get_by_role("heading", name="Administrator sign in").is_visible()
    page.get_by_label("Admin username").fill(ADMIN_USERNAME)
    page.get_by_label("Password").fill(ADMIN_PASSWORD)
    page.get_by_role("button", name="Open administration").click()
    page.wait_for_url(f"{BASE_URL}/admin")
    assert page.get_by_role("heading", name="User accounts").is_visible()
    assert page.get_by_role("heading", name="Create user").is_visible()
    assert page.get_by_text(USER_USERNAME, exact=True).is_visible()
    assert not page.get_by_role("link", name="Dashboard", exact=True).is_visible()
    page.screenshot(path=str(Path("admin-ui.png").resolve()), full_page=True)
    print("Separate admin site: PASS")

    page.get_by_role("button", name="Sign out").click()
    page.wait_for_url(f"{BASE_URL}/admin/login")
    page.goto(f"{BASE_URL}/login", wait_until="networkidle")
    page.get_by_label("Username").fill(USER_USERNAME)
    page.get_by_label("Password").fill(USER_PASSWORD)
    page.get_by_role("button", name="Continue to workspace").click()
    page.wait_for_url(f"{BASE_URL}/dashboard")
    assert page.get_by_role("heading", name="Your workspace").is_visible()
    assert not page.get_by_text("IssueBot Admin", exact=True).is_visible()
    page.screenshot(path=str(Path("dashboard-ui.png").resolve()), full_page=True)
    print("Separate user site: PASS")

    page.get_by_role("link", name="Setup", exact=True).click()
    page.wait_for_url(f"{BASE_URL}/settings")
    assert page.get_by_role("heading", name="Drips connection").is_visible()
    assert "Telegram" not in page.locator("body").inner_text()
    page.screenshot(path=str(Path("setup-ui.png").resolve()), full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="networkidle")
    assert page.get_by_role("heading", name="Bot setup").is_visible()
    page.screenshot(path=str(Path("mobile-ui.png").resolve()), full_page=True)
    print("User setup without Telegram: PASS")

    assert not console_errors, f"Browser console errors: {console_errors}"
    browser.close()

with SessionLocal() as db:
    user = db.query(User).filter_by(username=USER_USERNAME).one_or_none()
    if user:
        db.delete(user)
        db.commit()
