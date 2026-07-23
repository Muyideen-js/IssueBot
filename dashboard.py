#!/usr/bin/env python3
"""
Issue Bot Dashboard — Flask server with GitHub OAuth & Multi-User Support
========================================================================
- Each user authenticates via GitHub OAuth.
- Per-user dashboard isolation: users only see their own state, stats, and logs.
- Per-user bot process & Drips Wave / GrantFox authorization control.
"""

import os
import json
import time
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
import requests
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "change-this-secret-key-123")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
GITHUB_CLIENT_ID     = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
DASHBOARD_PASSWORD   = os.environ.get("DASHBOARD_PASSWORD", "issuebot2024")
SYNC_SECRET          = os.environ.get("SYNC_SECRET", "sync-secret-123")

# Directories
SESSION_DIR = Path("sessions")
STATE_DIR   = Path("states")
LOG_DIR     = Path("logs")
USER_DIR    = Path("users")

SESSION_DIR.mkdir(exist_ok=True)
STATE_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)
USER_DIR.mkdir(exist_ok=True)

# Active bot background processes per user: { username: subprocess.Popen }
_bot_processes = {}

# In-memory user stores: { username: { state, logs, last_sync } }
_user_stores = {}

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def get_user_state_file(username: str) -> Path:
    return STATE_DIR / f"state_{username}.json"

def get_user_log_file(username: str) -> Path:
    return LOG_DIR / f"bot_{username}.log"

def get_user_config_file(username: str) -> Path:
    return USER_DIR / f"user_{username}.json"

def get_user_store(username: str) -> dict:
    if username not in _user_stores:
        state_file = get_user_state_file(username)
        state = {
            "applications": {},
            "skipped": [],
            "accepted": [],
            "stats": {"applied": 0, "accepted": 0, "timed_out": 0}
        }
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        
        logs = []
        log_file = get_user_log_file(username)
        if log_file.exists():
            try:
                logs = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-100:]
            except Exception:
                pass

        _user_stores[username] = {
            "state": state,
            "logs": logs,
            "last_sync": None
        }
    return _user_stores[username]

def get_user_settings(username: str) -> dict:
    conf_file = get_user_config_file(username)
    if conf_file.exists():
        try:
            return json.loads(conf_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_user_settings(username: str, data: dict):
    conf_file = get_user_config_file(username)
    conf_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

def is_bot_running(username: str) -> bool:
    proc = _bot_processes.get(username)
    if proc and proc.poll() is None:
        return True
    return False

def check_session_authed(username: str, platform: str) -> bool:
    p = SESSION_DIR / f"{username}_{platform}.json"
    if not p.exists():
        p = SESSION_DIR / f"{platform}.json"
    return p.exists()

# ─── AUTH DECORATOR ────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_current_username() -> str:
    user = session.get("user")
    if user and isinstance(user, dict) and user.get("username"):
        return user["username"]
    return session.get("legacy_user", "default_user")

# ─── AUTH ROUTES ───────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            session["user"] = {
                "username": "admin",
                "avatar_url": "https://github.com/github.png",
                "name": "Admin User"
            }
            return redirect(url_for("index"))
        error = "Invalid password."

    github_enabled = bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET)
    return render_template("login.html", error=error, github_enabled=github_enabled)

@app.route("/login/github")
def login_github():
    if not GITHUB_CLIENT_ID:
        return jsonify({"error": "GITHUB_CLIENT_ID not configured in server environment"}), 400
    redirect_uri = url_for("login_github_callback", _external=True)
    github_url = f"https://github.com/login/oauth/authorize?client_id={GITHUB_CLIENT_ID}&redirect_uri={redirect_uri}&scope=user:email"
    return redirect(github_url)

@app.route("/login/github/callback")
def login_github_callback():
    code = request.args.get("code")
    if not code:
        return redirect(url_for("login"))

    try:
        # Exchange code for access token
        token_res = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code": code
            },
            timeout=15
        )
        token_json = token_res.json()
        access_token = token_json.get("access_token")

        if not access_token:
            return render_template("login.html", error="GitHub OAuth failed: missing access token.", github_enabled=True)

        # Get GitHub user profile
        user_res = requests.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "IssueBot-Dashboard"
            },
            timeout=15
        )
        user_data = user_res.json()
        username  = user_data.get("login")

        if not username:
            return render_template("login.html", error="Failed to fetch GitHub profile.", github_enabled=True)

        # Set user session
        session["logged_in"] = True
        session["user"] = {
            "username": username,
            "avatar_url": user_data.get("avatar_url", "https://github.com/github.png"),
            "name": user_data.get("name") or username,
            "id": user_data.get("id")
        }

        # Initialize user store & config if new
        get_user_store(username)
        settings = get_user_settings(username)
        if not settings.get("github_username"):
            settings["github_username"] = username
            save_user_settings(username, settings)

        return redirect(url_for("index"))
    except Exception as e:
        return render_template("login.html", error=f"OAuth error: {str(e)}", github_enabled=True)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── BOT SYNC ENDPOINT ─────────────────────────────────────────────────────────
@app.route("/sync", methods=["POST"])
def sync():
    """bot.py calls this every cycle to push state + logs for a specific user."""
    auth = request.headers.get("X-Sync-Secret", "")
    if auth != SYNC_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True) or {}
    username = request.headers.get("X-User-Name") or data.get("username") or "default_user"

    store = get_user_store(username)
    if data.get("state"):
        store["state"] = data["state"]
        # Save to disk
        state_file = get_user_state_file(username)
        state_file.write_text(json.dumps(data["state"], indent=2), encoding="utf-8")

    if data.get("logs"):
        store["logs"] = data["logs"][-100:]
        log_file = get_user_log_file(username)
        log_file.write_text("\n".join(store["logs"]), encoding="utf-8")

    store["last_sync"] = datetime.utcnow().isoformat()
    return jsonify({"ok": True})

# ─── DASHBOARD ROUTES ──────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/me")
@login_required
def api_me():
    username = get_current_username()
    user_info = session.get("user", {"username": username, "avatar_url": "", "name": username})
    
    return jsonify({
        "user": user_info,
        "bot_running": is_bot_running(username),
        "drips_authed": check_session_authed(username, "drips"),
        "grantfox_authed": check_session_authed(username, "grantfox")
    })

@app.route("/api/state")
@login_required
def api_state():
    username = get_current_username()
    store = get_user_store(username)
    state = store["state"]
    apps  = state.get("applications", {})
    timeout_hrs = float(os.environ.get("ACCEPTANCE_TIMEOUT_HOURS", "24"))

    active = []
    for key, app_data in apps.items():
        try:
            applied_at = datetime.fromisoformat(app_data["applied_at"])
            deadline   = applied_at + timedelta(hours=timeout_hrs)
            hours_left = max(0, (deadline - datetime.utcnow()).total_seconds() / 3600)
        except Exception:
            hours_left = timeout_hrs
        active.append({
            "key":        key,
            "title":      app_data.get("title", ""),
            "url":        app_data.get("url", ""),
            "platform":   app_data.get("platform", ""),
            "applied_at": app_data.get("applied_at", ""),
            "hours_left": round(hours_left, 1),
        })

    accepted = list(reversed(state.get("accepted", [])[-20:]))

    return jsonify({
        "stats":     state.get("stats", {}),
        "active":    active,
        "accepted":  accepted,
        "skipped":   len(state.get("skipped", [])),
        "last_sync": store["last_sync"],
        "bot_running": is_bot_running(username)
    })

@app.route("/api/logs")
@login_required
def api_logs():
    username = get_current_username()
    store = get_user_store(username)
    return jsonify({"lines": store["logs"]})

@app.route("/api/settings", methods=["GET", "POST"])
@login_required
def api_settings():
    username = get_current_username()
    if request.method == "POST":
        data = request.get_json(force=True) or {}
        settings = get_user_settings(username)
        settings["github_username"]   = data.get("github_username", username)
        if data.get("github_password"):
            settings["github_password"] = data.get("github_password")
        if data.get("github_otp_secret") is not None:
            settings["github_otp_secret"] = data.get("github_otp_secret")
        if data.get("telegram_bot_token") is not None:
            settings["telegram_bot_token"] = data.get("telegram_bot_token")
        if data.get("telegram_chat_id") is not None:
            settings["telegram_chat_id"] = data.get("telegram_chat_id")

        save_user_settings(username, settings)
        return jsonify({"ok": True, "message": "Settings saved successfully!"})

    settings = get_user_settings(username)
    # Mask password for API response
    masked = dict(settings)
    if masked.get("github_password"):
        masked["github_password"] = "••••••••"
    return jsonify(masked)

# ─── BOT CONTROL ENDPOINTS ─────────────────────────────────────────────────────
@app.route("/api/bot/start", methods=["POST"])
@login_required
def api_bot_start():
    username = get_current_username()
    if is_bot_running(username):
        return jsonify({"ok": True, "message": "Bot is already running."})

    cmd = [sys.executable, "bot.py", "--username", username]
    proc = subprocess.Popen(cmd)
    _bot_processes[username] = proc

    return jsonify({"ok": True, "message": f"Issue Bot started for @{username}."})

@app.route("/api/bot/stop", methods=["POST"])
@login_required
def api_bot_stop():
    username = get_current_username()
    proc = _bot_processes.get(username)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
        _bot_processes.pop(username, None)
    return jsonify({"ok": True, "message": f"Issue Bot stopped for @{username}."})

@app.route("/api/bot/auth-drips", methods=["POST"])
@login_required
def api_bot_auth_drips():
    """Trigger automated login for Drips Wave using user's configured GitHub credentials."""
    username = get_current_username()
    settings = get_user_settings(username)
    if not settings.get("github_password") and not os.getenv("GITHUB_PASSWORD"):
        return jsonify({"error": "Please configure your GitHub Password in Bot Settings first."}), 400

    cmd = [sys.executable, "bot.py", "--username", username, "--once", "--platform", "drips"]
    proc = subprocess.Popen(cmd)
    _bot_processes[username] = proc

    return jsonify({"ok": True, "message": "Drips Wave authentication cycle initiated."})

# ─── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
