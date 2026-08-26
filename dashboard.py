#!/usr/bin/env python3
import json
import hashlib
import os
import re
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import delete, desc, select

from ai_writer import DEFAULT_MODELS, PROVIDERS, SUGGESTED_MODELS, test_provider
from automation import BLOCKED_REPOSITORIES
from database import SessionLocal, init_db
from models import ActivityLog, BotSettings, EnrollmentToken, IssueRecord, User, utcnow
from security import decrypt_secret, encrypt_secret
from wave_service import WaveClient, WaveError


USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,40}$")
LOGIN_ATTEMPTS: dict[str, deque[datetime]] = defaultdict(deque)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.getenv("APP_SECRET", "development-change-me"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("APP_ENV", "development").lower() == "production",
        MAX_CONTENT_LENGTH=1_000_000,
    )
    if test_config:
        app.config.update(test_config)

    if os.getenv("APP_ENV", "development").lower() == "production":
        if app.config["SECRET_KEY"] == "development-change-me":
            raise RuntimeError("APP_SECRET is required in production")
        if not os.getenv("CREDENTIAL_ENCRYPTION_KEY"):
            raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is required in production")

    init_db()
    bootstrap_admin()

    @app.before_request
    def load_user_and_check_csrf():
        g.db = SessionLocal()
        g.user = g.db.get(User, session.get("user_id")) if session.get("user_id") else None
        if g.user and not g.user.is_active:
            session.clear()
            g.user = None
        if request.method == "POST" and request.endpoint != "connect_session_api":
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not expected or not secrets.compare_digest(expected, supplied):
                if request.endpoint in {"login", "admin_login"}:
                    session.clear()
                    flash("Your sign-in page expired. Please sign in again.", "error")
                    return redirect(request.path)
                abort(400, "Invalid CSRF token")

    @app.teardown_request
    def close_db(_error=None):
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def template_context():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return {"current_user": getattr(g, "user", None), "csrf_token": token}

    def user_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            if g.user.is_admin:
                return redirect(url_for("admin_dashboard"))
            if g.user.must_change_password and request.endpoint not in {"account", "logout"}:
                return redirect(url_for("account"))
            return view(*args, **kwargs)

        return wrapped

    def admin_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.user:
                return redirect(url_for("admin_login", next=request.path))
            if not g.user.is_admin:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    @app.route("/health", methods=["GET", "HEAD"])
    def health():
        if request.method == "HEAD":
            return "", 204
        return {"ok": True}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("admin_dashboard" if g.user.is_admin else "user_dashboard"))
        if request.method == "POST":
            client_key = request.remote_addr or "unknown"
            now = utcnow()
            attempts = LOGIN_ATTEMPTS[client_key]
            while attempts and attempts[0] < now - timedelta(minutes=15):
                attempts.popleft()
            if len(attempts) >= 8:
                flash("Too many login attempts. Try again in 15 minutes.", "error")
                return render_template("login.html"), 429

            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            user = g.db.scalar(select(User).where(User.username == username))
            if user and user.is_active and not user.is_admin and user.check_password(password):
                attempts.clear()
                session.clear()
                session["user_id"] = user.id
                session["csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("account") if user.must_change_password else url_for("user_dashboard"))
            attempts.append(now)
            flash("Invalid username or password.", "error")
        return render_template("login.html")

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if g.user:
            return redirect(url_for("admin_dashboard" if g.user.is_admin else "user_dashboard"))
        if request.method == "POST":
            client_key = f"admin:{request.remote_addr or 'unknown'}"
            now = utcnow()
            attempts = LOGIN_ATTEMPTS[client_key]
            while attempts and attempts[0] < now - timedelta(minutes=15):
                attempts.popleft()
            if len(attempts) >= 8:
                flash("Too many login attempts. Try again in 15 minutes.", "error")
                return render_template("admin_login.html"), 429
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("password", "")
            user = g.db.scalar(select(User).where(User.username == username))
            if user and user.is_active and user.is_admin and user.check_password(password):
                attempts.clear()
                session.clear()
                session["user_id"] = user.id
                session["csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("admin_dashboard"))
            attempts.append(now)
            flash("Invalid administrator credentials.", "error")
        return render_template("admin_login.html")

    @app.post("/logout")
    @user_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.post("/admin/logout")
    @admin_required
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    @app.route("/account", methods=["GET", "POST"])
    @user_required
    def account():
        if request.method == "POST":
            current = request.form.get("current_password", "")
            password = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            if not g.user.check_password(current):
                flash("Current password is incorrect.", "error")
            elif len(password) < 10:
                flash("New password must contain at least 10 characters.", "error")
            elif password != confirm:
                flash("New passwords do not match.", "error")
            else:
                g.user.set_password(password)
                g.user.must_change_password = False
                g.db.commit()
                flash("Password updated.", "success")
                return redirect(url_for("user_dashboard"))
        return render_template("account.html")

    @app.route("/", methods=["GET", "HEAD"])
    def index():
        if request.method == "HEAD":
            return "", 204
        if not g.user:
            return redirect(url_for("login"))
        return redirect(url_for("admin_dashboard" if g.user.is_admin else "user_dashboard"))

    @app.get("/dashboard")
    @user_required
    def user_dashboard():
        records = g.db.scalars(
            select(IssueRecord)
            .where(IssueRecord.user_id == g.user.id)
            .order_by(desc(IssueRecord.updated_at))
        ).all()
        logs = g.db.scalars(
            select(ActivityLog)
            .where(ActivityLog.user_id == g.user.id)
            .order_by(desc(ActivityLog.created_at))
            .limit(30)
        ).all()
        settings = get_or_create_settings(g.db, g.user.id)
        return render_template(
            "index.html",
            candidates=[record for record in records if record.status == "candidate"],
            pending=[record for record in records if record.status == "pending"],
            accepted=[record for record in records if record.status == "accepted"],
            logs=logs,
            settings=settings,
        )

    @app.post("/dashboard/disconnect-session")
    @user_required
    def disconnect_drips_session():
        bot_settings = get_or_create_settings(g.db, g.user.id)
        bot_settings.drips_session_encrypted = ""
        bot_settings.enabled = False
        bot_settings.last_error = ""
        g.db.add(ActivityLog(user_id=g.user.id, message="Drips session disconnected."))
        g.db.commit()
        flash("Drips session disconnected and automation paused.", "success")
        return redirect(url_for("user_dashboard"))

    @app.post("/dashboard/clear-logs")
    @user_required
    def clear_activity_logs():
        g.db.execute(delete(ActivityLog).where(ActivityLog.user_id == g.user.id))
        g.db.commit()
        flash("Activity logs cleared.", "success")
        return redirect(url_for("user_dashboard"))

    @app.route("/settings", methods=["GET", "POST"])
    @user_required
    def settings():
        bot_settings = get_or_create_settings(g.db, g.user.id)
        if request.method == "POST":
            enabled = request.form.get("enabled") == "on"
            poll_minutes = bounded_int(request.form.get("poll_minutes"), 2, 60, 5)
            max_active = bounded_int(request.form.get("max_active_applications"), 1, 15, 15)
            max_per_repo = bounded_int(request.form.get("max_per_repo"), 1, 2, 2)
            stale_minutes = bounded_int(request.form.get("stale_minutes"), 5, 1440, 30)
            preferred = request.form.get("preferred_ai_provider", "gemini").strip().lower()
            fallback_message = request.form.get("fallback_message", "").strip()
            priority_repos_raw = request.form.get("priority_repos", "")
            action = request.form.get("action", "save")

            session_text = request.form.get("drips_session", "").strip()
            uploaded = request.files.get("drips_session_file")
            if uploaded and uploaded.filename:
                session_text = uploaded.read().decode("utf-8").strip()

            try:
                priorities = normalize_priority_repos(priority_repos_raw)
                if session_text:
                    session_text = normalize_drips_session(session_text)
                    bot_settings.drips_session_encrypted = encrypt_secret(session_text)
                if request.form.get("clear_drips") == "yes":
                    bot_settings.drips_session_encrypted = ""
                    enabled = False

                if enabled and not bot_settings.drips_session_encrypted:
                    raise ValueError("Connect a Drips session before enabling monitoring")
                if preferred not in PROVIDERS:
                    raise ValueError("Choose a supported preferred AI provider")
                if not 5 <= len(fallback_message) <= 500:
                    raise ValueError("Fallback message must contain 5–500 characters")

                for provider in PROVIDERS:
                    api_key = request.form.get(f"{provider}_api_key", "").strip()
                    encrypted_field = f"{provider}_api_key_encrypted"
                    if api_key:
                        setattr(bot_settings, encrypted_field, encrypt_secret(api_key))
                    if request.form.get(f"clear_{provider}_api_key") == "yes":
                        setattr(bot_settings, encrypted_field, "")
                    model = request.form.get(f"{provider}_model", "").strip()
                    if len(model) > 80:
                        raise ValueError(f"{provider.title()} model name is too long")
                    setattr(bot_settings, f"{provider}_model", model)

                bot_settings.enabled = enabled
                bot_settings.poll_minutes = poll_minutes
                bot_settings.max_active_applications = max_active
                bot_settings.max_per_repo = max_per_repo
                bot_settings.stale_minutes = stale_minutes
                bot_settings.preferred_ai_provider = preferred
                bot_settings.fallback_message = fallback_message
                bot_settings.priority_repos = priorities
                g.db.commit()
                if action.startswith("test_"):
                    provider = action.removeprefix("test_")
                    if provider not in PROVIDERS:
                        raise ValueError("Unknown AI provider")
                    api_key = decrypt_secret(getattr(bot_settings, f"{provider}_api_key_encrypted"))
                    test_provider(provider, api_key, getattr(bot_settings, f"{provider}_model"))
                    flash(f"{provider.title()} connection successful.", "success")
                else:
                    flash("Automation settings saved.", "success")
                return redirect(url_for("settings"))
            except Exception as exc:
                g.db.rollback()
                flash(str(exc), "error")

        return render_template(
            "settings.html",
            settings=bot_settings,
            has_drips=bool(bot_settings.drips_session_encrypted),
            connected_ai={
                provider: bool(getattr(bot_settings, f"{provider}_api_key_encrypted"))
                for provider in PROVIDERS
            },
            default_models=DEFAULT_MODELS,
            suggested_models=SUGGESTED_MODELS,
        )

    @app.post("/settings/connection-code")
    @user_required
    def connection_code():
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        g.db.add(EnrollmentToken(
            user_id=g.user.id,
            token_hash=token_hash,
            expires_at=utcnow() + timedelta(minutes=15),
        ))
        g.db.commit()
        flash(f"One-time connector code (valid for 15 minutes): {raw_token}", "success")
        return redirect(url_for("settings"))

    @app.post("/api/connect-session")
    def connect_session_api():
        data = request.get_json(silent=True) or {}
        raw_token = str(data.get("code") or "")
        session_state = data.get("session")
        if not raw_token or not isinstance(session_state, dict):
            return {"error": "code and session are required"}, 400

        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        enrollment = g.db.scalar(
            select(EnrollmentToken).where(
                EnrollmentToken.token_hash == token_hash,
                EnrollmentToken.used_at.is_(None),
                EnrollmentToken.expires_at > utcnow(),
            )
        )
        if not enrollment:
            return {"error": "connection code is invalid or expired"}, 401
        try:
            raw_session = normalize_drips_session(json.dumps(session_state))
            bot_settings = get_or_create_settings(g.db, enrollment.user_id)
            bot_settings.drips_session_encrypted = encrypt_secret(raw_session)
            bot_settings.last_error = ""
            enrollment.used_at = utcnow()
            g.db.add(ActivityLog(user_id=enrollment.user_id, message="Drips session connected."))
            g.db.commit()
            return {"ok": True}
        except ValueError as exc:
            g.db.rollback()
            return {"error": str(exc)}, 400

    @app.post("/issues/<int:record_id>/approve")
    @user_required
    def approve_issue(record_id: int):
        record = owned_record_or_404(g.db, g.user.id, record_id)
        if record.status != "candidate":
            flash("That issue is no longer awaiting approval.", "error")
            return redirect(url_for("user_dashboard"))
        if record.repo.strip().lower() in BLOCKED_REPOSITORIES:
            flash("Applications to this repository are blocked.", "error")
            return redirect(url_for("user_dashboard"))

        application_text = request.form.get("application_text", "").strip()
        if len(application_text) < 20 or len(application_text) > 1000:
            flash("Application message must contain 20–1000 characters.", "error")
            return redirect(url_for("user_dashboard"))

        bot_settings = get_or_create_settings(g.db, g.user.id)
        try:
            session_state = json.loads(decrypt_secret(bot_settings.drips_session_encrypted))
            client = WaveClient(session_state)
            pending, changed_before = client.fetch_applications("pending")
            if len(pending) >= 15:
                raise WaveError("Drips pending-application limit is already full")
            ok, message, changed_after = client.apply(record.issue_id, application_text)
            if changed_before or changed_after:
                bot_settings.drips_session_encrypted = encrypt_secret(json.dumps(client.session_state))
            if not ok:
                raise WaveError(message)

            record.application_text = application_text
            record.status = "pending"
            record.applied_at = utcnow()
            g.db.add(ActivityLog(user_id=g.user.id, message=f"Applied: {record.title}"))
            g.db.commit()
            flash("Application submitted to Drips.", "success")

        except Exception as exc:
            g.db.rollback()
            flash(str(exc), "error")
        return redirect(url_for("user_dashboard"))

    @app.post("/issues/<int:record_id>/dismiss")
    @user_required
    def dismiss_issue(record_id: int):
        record = owned_record_or_404(g.db, g.user.id, record_id)
        if record.status == "candidate":
            record.status = "dismissed"
            g.db.commit()
            flash("Candidate dismissed.", "success")
        return redirect(url_for("user_dashboard"))

    @app.route("/admin", methods=["GET", "POST"])
    @admin_required
    def admin_dashboard():
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            password = request.form.get("temporary_password", "")
            if not USERNAME_RE.fullmatch(username):
                flash("Username must be 3–40 letters, numbers, dots, underscores, or hyphens.", "error")
            elif len(password) < 10:
                flash("Temporary password must contain at least 10 characters.", "error")
            elif g.db.scalar(select(User).where(User.username == username)):
                flash("That username already exists.", "error")
            else:
                user = User(username=username, is_admin=False, must_change_password=True)
                user.set_password(password)
                g.db.add(user)
                g.db.flush()
                g.db.add(BotSettings(user_id=user.id))
                g.db.commit()
                flash(f"Created {username}. Give them the temporary password through a secure channel.", "success")
                return redirect(url_for("admin_dashboard"))

        users = g.db.scalars(select(User).where(User.is_admin.is_(False)).order_by(User.username)).all()
        return render_template("admin_users.html", users=users)

    @app.get("/admin/users")
    @admin_required
    def admin_users():
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/users/<int:user_id>/toggle")
    @admin_required
    def admin_toggle_user(user_id: int):
        user = g.db.get(User, user_id)
        if not user or user.is_admin:
            abort(404)
        user.is_active = not user.is_active
        if user.settings and not user.is_active:
            user.settings.enabled = False
        g.db.commit()
        flash(f"{user.username} is now {'active' if user.is_active else 'disabled'}.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/users/<int:user_id>/reset-password")
    @admin_required
    def admin_reset_password(user_id: int):
        user = g.db.get(User, user_id)
        if not user or user.is_admin:
            abort(404)
        password = request.form.get("temporary_password", "")
        if len(password) < 10:
            flash("Temporary password must contain at least 10 characters.", "error")
        else:
            user.set_password(password)
            user.must_change_password = True
            g.db.commit()
            flash(f"Password reset for {user.username}.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.post("/admin/users/<int:user_id>/delete")
    @admin_required
    def admin_delete_user(user_id: int):
        user = g.db.get(User, user_id)
        if not user or user.is_admin:
            abort(404)
        username = user.username
        for model in (IssueRecord, ActivityLog, EnrollmentToken, BotSettings):
            g.db.execute(delete(model).where(model.user_id == user_id))
        g.db.delete(user)
        g.db.commit()
        flash(f"Deleted {username} and all of their IssueBot data.", "success")
        return redirect(url_for("admin_dashboard"))

    return app


def bootstrap_admin() -> None:
    username = os.getenv("ADMIN_USERNAME", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not username or not password:
        return
    if len(password) < 12:
        raise RuntimeError("ADMIN_PASSWORD must contain at least 12 characters")
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == username))
        if admin:
            if not admin.is_admin:
                admin.is_admin = True
                db.commit()
            return
        admin = User(username=username, is_admin=True, must_change_password=False)
        admin.set_password(password)
        db.add(admin)
        db.flush()
        db.add(BotSettings(user_id=admin.id))
        db.commit()


def get_or_create_settings(db, user_id: int) -> BotSettings:
    settings = db.scalar(select(BotSettings).where(BotSettings.user_id == user_id))
    if settings:
        return settings
    settings = BotSettings(user_id=user_id)
    db.add(settings)
    db.commit()
    return settings


def normalize_drips_session(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Drips session must be a valid JSON file") from exc
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        raise ValueError("Drips session JSON does not contain a cookies list")
    drips_cookies = [
        cookie
        for cookie in cookies
        if isinstance(cookie, dict)
        and str(cookie.get("domain", "")).lower().lstrip(".").endswith("drips.network")
    ]
    names = {cookie.get("name") for cookie in drips_cookies}
    if not {"wave_access_token", "wave_refresh_token"}.intersection(names):
        raise ValueError("Drips session does not contain a Wave access or refresh token")
    origins = [
        origin
        for origin in data.get("origins", [])
        if isinstance(origin, dict) and "drips.network" in str(origin.get("origin", "")).lower()
    ]
    return json.dumps({"cookies": drips_cookies, "origins": origins})


def bounded_int(raw: str | None, minimum: int, maximum: int, default: int) -> int:
    try:
        return max(minimum, min(maximum, int(raw or default)))
    except ValueError:
        return default


def normalize_priority_repos(raw: str) -> str:
    values = []
    for value in re.split(r"[,\n]", raw or ""):
        normalized = value.strip().strip("/")
        if not normalized or normalized.lower() in {item.lower() for item in values}:
            continue
        if len(normalized) > 255 or not re.fullmatch(
            r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", normalized
        ):
            raise ValueError(f"Invalid priority repository: {normalized[:80]}")
        values.append(normalized)
    if len(values) > 100:
        raise ValueError("Priority list cannot contain more than 100 repositories")
    return "\n".join(values)


def owned_record_or_404(db, user_id: int, record_id: int) -> IssueRecord:
    record = db.scalar(
        select(IssueRecord).where(IssueRecord.id == record_id, IssueRecord.user_id == user_id)
    )
    if not record:
        abort(404)
    return record


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=False)
