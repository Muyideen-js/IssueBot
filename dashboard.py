#!/usr/bin/env python3
"""
Issue Bot Dashboard — Flask server
Bot pushes state/logs to this server every cycle.
Friends view the dashboard from anywhere via Render URL.
"""

import os
import json
import time
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET_KEY", "change-this-secret-key-123")

# ─── CONFIG ────────────────────────────────────────────────────────────────────
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "issuebot2024")
SYNC_SECRET        = os.environ.get("SYNC_SECRET", "sync-secret-123")

# ─── IN-MEMORY STORE ───────────────────────────────────────────────────────────
# bot.py pushes updates here every cycle via POST /sync
_store = {
    "state": {
        "applications": {},
        "skipped": [],
        "accepted": [],
        "stats": {"applied": 0, "accepted": 0, "timed_out": 0}
    },
    "logs": [],
    "last_sync": None
}

# ─── AUTH ──────────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("index"))
        error = "Wrong password. Try again."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── BOT SYNC ENDPOINT ─────────────────────────────────────────────────────────
@app.route("/sync", methods=["POST"])
def sync():
    """bot.py calls this every cycle to push state + logs."""
    auth = request.headers.get("X-Sync-Secret", "")
    if auth != SYNC_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    if data.get("state"):
        _store["state"] = data["state"]
    if data.get("logs"):
        _store["logs"] = data["logs"][-100:]   # keep last 100 lines
    _store["last_sync"] = datetime.utcnow().isoformat()

    return jsonify({"ok": True})

# ─── DASHBOARD ROUTES ──────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/api/state")
@login_required
def api_state():
    state = _store["state"]
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
        "last_sync": _store["last_sync"],
    })

@app.route("/api/logs")
@login_required
def api_logs():
    return jsonify({"lines": _store["logs"]})

# ─── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
