import base64
import hashlib
import os
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch


TEST_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(TEST_DIR.name) / 'portal.db'}"
os.environ["APP_ENV"] = "development"
os.environ["APP_SECRET"] = "test-app-secret"
os.environ["CREDENTIAL_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(b"1" * 32).decode()
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin-password-123"

import dashboard  # noqa: E402
import automation  # noqa: E402
import worker  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
from models import ActivityLog, BotSettings, EnrollmentToken, IssueRecord, User, utcnow  # noqa: E402
from security import decrypt_secret, encrypt_secret  # noqa: E402


class PortalTestCase(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        engine.dispose()
        TEST_DIR.cleanup()

    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        dashboard.bootstrap_admin()
        self.app = dashboard.create_app({"TESTING": True, "SECRET_KEY": "test-session-secret"})
        self.client = self.app.test_client()

    def csrf(self, path="/login"):
        response = self.client.get(path)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def login(self, username, password, path="/login"):
        token = self.csrf(path)
        return self.client.post(
            path,
            data={"csrf_token": token, "username": username, "password": password},
            follow_redirects=False,
        )

    def test_admin_creates_user_and_user_must_change_password(self):
        response = self.login("admin", "admin-password-123", "/admin/login")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/admin")
        token = self.csrf("/admin")
        response = self.client.post(
            "/admin",
            data={
                "csrf_token": token,
                "username": "alice",
                "temporary_password": "temporary-123",
            },
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            alice = db.query(User).filter_by(username="alice").one()
            self.assertTrue(alice.must_change_password)
            self.assertFalse(alice.is_admin)

        token = self.csrf("/admin")
        self.client.post("/admin/logout", data={"csrf_token": token})
        response = self.login("alice", "temporary-123")
        self.assertEqual(response.headers["Location"], "/account")

    def test_admin_and_user_sites_are_separate(self):
        response = self.login("admin", "admin-password-123")
        self.assertIn(b"Invalid username or password", response.data)

        response = self.login("admin", "admin-password-123", "/admin/login")
        self.assertEqual(response.headers["Location"], "/admin")
        self.assertEqual(self.client.get("/dashboard").headers["Location"], "/admin")
        token = self.csrf("/admin")
        self.client.post("/admin/logout", data={"csrf_token": token})

        with SessionLocal() as db:
            user = User(username="normal-user", must_change_password=False)
            user.set_password("normal-password-123")
            db.add(user)
            db.flush()
            db.add(BotSettings(user_id=user.id))
            db.commit()
        self.login("normal-user", "normal-password-123")
        self.assertEqual(self.client.get("/admin").status_code, 403)

    def test_admin_can_delete_user_and_all_user_data(self):
        with SessionLocal() as db:
            user = User(username="delete-me", must_change_password=False)
            user.set_password("delete-password-123")
            db.add(user)
            db.flush()
            user_id = user.id
            db.add(BotSettings(user_id=user_id))
            db.add(IssueRecord(user_id=user_id, issue_id="delete-issue", title="Delete me"))
            db.commit()

        self.login("admin", "admin-password-123", "/admin/login")
        token = self.csrf("/admin")
        response = self.client.post(
            f"/admin/users/{user_id}/delete",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 302)
        with SessionLocal() as db:
            self.assertIsNone(db.get(User, user_id))
            self.assertEqual(db.query(IssueRecord).filter_by(user_id=user_id).count(), 0)

    def test_connection_code_imports_encrypted_session(self):
        with SessionLocal() as db:
            user = User(username="bob", must_change_password=False)
            user.set_password("bob-password-123")
            db.add(user)
            db.flush()
            db.add(BotSettings(user_id=user.id))
            raw_code = "one-time-test-code"
            db.add(EnrollmentToken(
                user_id=user.id,
                token_hash=hashlib.sha256(raw_code.encode()).hexdigest(),
                expires_at=utcnow() + timedelta(minutes=15),
            ))
            db.commit()
            user_id = user.id

        response = self.client.post(
            "/api/connect-session",
            json={
                "code": raw_code,
                "session": {"cookies": [
                    {"name": "wave_refresh_token", "value": "secret", "domain": ".drips.network"},
                    {"name": "github_session", "value": "must-not-store", "domain": ".github.com"},
                ]},
            },
        )
        self.assertEqual(response.status_code, 200)
        reused = self.client.post(
            "/api/connect-session",
            json={
                "code": raw_code,
                "session": {"cookies": [
                    {"name": "wave_refresh_token", "value": "secret", "domain": ".drips.network"}
                ]},
            },
        )
        self.assertEqual(reused.status_code, 401)
        with SessionLocal() as db:
            settings = db.query(BotSettings).filter_by(user_id=user_id).one()
            decrypted = decrypt_secret(settings.drips_session_encrypted)
            self.assertIn("wave_refresh_token", decrypted)
            self.assertNotIn("github.com", decrypted)
            enrollment = db.query(EnrollmentToken).one()
            self.assertIsNotNone(enrollment.used_at)

    def test_user_disconnects_only_their_own_drips_session(self):
        with SessionLocal() as db:
            owner = User(username="disconnect-user", must_change_password=False)
            owner.set_password("disconnect-password-123")
            other = User(username="connected-other", must_change_password=False)
            other.set_password("connected-other-password-123")
            db.add_all([owner, other])
            db.flush()
            owner_id = owner.id
            other_id = other.id
            db.add_all([
                BotSettings(
                    user_id=owner_id,
                    enabled=True,
                    drips_session_encrypted=encrypt_secret('{"cookies": []}'),
                    last_error="old session error",
                ),
                BotSettings(
                    user_id=other_id,
                    enabled=True,
                    drips_session_encrypted=encrypt_secret('{"cookies": [{"name": "keep"}]}'),
                ),
            ])
            db.commit()

        self.login("disconnect-user", "disconnect-password-123")
        token = self.csrf("/dashboard")
        response = self.client.post(
            "/dashboard/disconnect-session",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Drips session disconnected", response.data)

        with SessionLocal() as db:
            owner_settings = db.query(BotSettings).filter_by(user_id=owner_id).one()
            other_settings = db.query(BotSettings).filter_by(user_id=other_id).one()
            self.assertEqual(owner_settings.drips_session_encrypted, "")
            self.assertFalse(owner_settings.enabled)
            self.assertEqual(owner_settings.last_error, "")
            self.assertNotEqual(other_settings.drips_session_encrypted, "")
            self.assertTrue(other_settings.enabled)

    def test_user_clears_only_their_own_activity_logs(self):
        with SessionLocal() as db:
            owner = User(username="clear-logs-user", must_change_password=False)
            owner.set_password("clear-logs-password-123")
            other = User(username="logs-other", must_change_password=False)
            other.set_password("logs-other-password-123")
            db.add_all([owner, other])
            db.flush()
            owner_id = owner.id
            other_id = other.id
            db.add_all([BotSettings(user_id=owner_id), BotSettings(user_id=other_id)])
            db.add_all([
                ActivityLog(user_id=owner_id, message="owner log one"),
                ActivityLog(user_id=owner_id, message="owner log two"),
                ActivityLog(user_id=other_id, message="other log"),
            ])
            db.commit()

        self.login("clear-logs-user", "clear-logs-password-123")
        token = self.csrf("/dashboard")
        response = self.client.post(
            "/dashboard/clear-logs",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Activity logs cleared", response.data)

        with SessionLocal() as db:
            self.assertEqual(db.query(ActivityLog).filter_by(user_id=owner_id).count(), 0)
            self.assertEqual(db.query(ActivityLog).filter_by(user_id=other_id).count(), 1)

    def test_user_cannot_modify_another_users_issue(self):
        with SessionLocal() as db:
            owner = User(username="owner", must_change_password=False)
            owner.set_password("owner-password-123")
            intruder = User(username="intruder", must_change_password=False)
            intruder.set_password("intruder-password-123")
            db.add_all([owner, intruder])
            db.flush()
            db.add_all([BotSettings(user_id=owner.id), BotSettings(user_id=intruder.id)])
            issue = IssueRecord(user_id=owner.id, issue_id="abc", title="Private candidate")
            db.add(issue)
            db.commit()
            issue_id = issue.id

        self.login("intruder", "intruder-password-123")
        token = self.csrf("/dashboard")
        response = self.client.post(
            f"/issues/{issue_id}/dismiss",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 404)

    def test_blocked_repository_cannot_be_applied_to_manually(self):
        with SessionLocal() as db:
            user = User(username="blocked-repo-user", must_change_password=False)
            user.set_password("blocked-repo-password-123")
            db.add(user)
            db.flush()
            db.add(BotSettings(
                user_id=user.id,
                drips_session_encrypted=encrypt_secret('{"cookies": []}'),
            ))
            record = IssueRecord(
                user_id=user.id,
                issue_id="blocked-issue",
                title="Must never apply",
                repo="Consulting-Manao/tansu",
                status="candidate",
            )
            db.add(record)
            db.commit()
            record_id = record.id

        self.login("blocked-repo-user", "blocked-repo-password-123")
        token = self.csrf("/dashboard")
        with patch.object(dashboard, "WaveClient") as wave_client:
            response = self.client.post(
                f"/issues/{record_id}/approve",
                data={
                    "csrf_token": token,
                    "application_text": "I can implement and test this requested change.",
                },
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Applications to this repository are blocked", response.data)
        wave_client.assert_not_called()
        with SessionLocal() as db:
            self.assertEqual(db.get(IssueRecord, record_id).status, "candidate")

    def test_post_without_csrf_is_rejected(self):
        response = self.client.post("/settings", data={})
        self.assertEqual(response.status_code, 400)

    def test_vercel_entrypoint_exports_the_portal(self):
        import app as vercel_entrypoint

        self.assertIs(vercel_entrypoint.app, dashboard.app)

    def test_scheduled_scan_rejects_unsigned_requests(self):
        with patch.object(dashboard, "verify_qstash_request", return_value=False):
            response = self.client.post("/api/scan", data=b"{}")

        self.assertEqual(response.status_code, 401)

    def test_scheduled_scan_runs_a_bounded_batch(self):
        result = {"status": "complete", "processed": 2, "failed": 0}
        with (
            patch.object(dashboard, "verify_qstash_request", return_value=True),
            patch.object(dashboard, "run_once", return_value=result) as run_once,
        ):
            response = self.client.post("/api/scan", data=b"{}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), result)
        run_once.assert_called_once_with(max_users=3, time_budget_seconds=220)

    def test_scheduler_runs_never_scanned_users_before_previous_users(self):
        with SessionLocal() as db:
            previous_user = User(username="previous-user", must_change_password=False)
            previous_user.set_password("previous-user-password-123")
            new_user = User(username="new-user", must_change_password=False)
            new_user.set_password("new-user-password-123")
            db.add_all([previous_user, new_user])
            db.flush()
            db.add_all([
                BotSettings(
                    user_id=previous_user.id,
                    enabled=True,
                    poll_minutes=5,
                    last_run_at=utcnow() - timedelta(minutes=10),
                ),
                BotSettings(
                    user_id=new_user.id,
                    enabled=True,
                    poll_minutes=5,
                    last_run_at=None,
                ),
            ])
            db.commit()

        scanned_users = []

        def record_scan(_db, user, _settings):
            scanned_users.append(user.username)

        with patch.object(worker, "run_user_cycle", side_effect=record_scan):
            result = worker.run_once(max_users=1)

        self.assertEqual(result["processed"], 1)
        self.assertEqual(scanned_users, ["new-user"])

    def test_uptime_head_requests_succeed_without_redirects(self):
        root = self.client.head("/")
        health = self.client.head("/health")
        self.assertEqual(root.status_code, 204)
        self.assertEqual(health.status_code, 204)
        self.assertEqual(root.data, b"")
        self.assertEqual(health.data, b"")

    def test_user_ai_keys_are_encrypted_and_never_rendered(self):
        with SessionLocal() as db:
            user = User(username="ai-user", must_change_password=False)
            user.set_password("ai-user-password-123")
            db.add(user)
            db.flush()
            db.add(BotSettings(user_id=user.id))
            db.commit()
            user_id = user.id

        self.login("ai-user", "ai-user-password-123")
        token = self.csrf("/settings")
        response = self.client.post(
            "/settings",
            data={
                "csrf_token": token,
                "action": "save",
                "poll_minutes": "5",
                "max_active_applications": "15",
                "max_per_repo": "2",
                "stale_minutes": "30",
                "preferred_ai_provider": "deepseek",
                "fallback_message": "Hi, I can fix this",
                "priority_repos": "Fluxora-Org\nowner/repo",
                "gemini_api_key": "gemini-secret-key",
                "deepseek_api_key": "deepseek-secret-key",
                "openai_api_key": "openai-secret-key",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"gemini-secret-key", response.data)
        self.assertNotIn(b"deepseek-secret-key", response.data)
        self.assertNotIn(b"openai-secret-key", response.data)
        with SessionLocal() as db:
            settings = db.query(BotSettings).filter_by(user_id=user_id).one()
            self.assertEqual(decrypt_secret(settings.gemini_api_key_encrypted), "gemini-secret-key")
            self.assertEqual(decrypt_secret(settings.deepseek_api_key_encrypted), "deepseek-secret-key")
            self.assertEqual(decrypt_secret(settings.openai_api_key_encrypted), "openai-secret-key")
            self.assertEqual(settings.preferred_ai_provider, "deepseek")
            self.assertEqual(settings.max_active_applications, 15)

    def test_user_can_choose_a_model_per_ai_provider(self):
        with SessionLocal() as db:
            user = User(username="model-user", must_change_password=False)
            user.set_password("model-user-password-123")
            db.add(user)
            db.flush()
            db.add(BotSettings(user_id=user.id))
            db.commit()
            user_id = user.id

        self.login("model-user", "model-user-password-123")
        token = self.csrf("/settings")
        response = self.client.post(
            "/settings",
            data={
                "csrf_token": token,
                "action": "save",
                "poll_minutes": "5",
                "max_active_applications": "15",
                "max_per_repo": "2",
                "stale_minutes": "30",
                "preferred_ai_provider": "deepseek",
                "fallback_message": "Hi, I can fix this",
                "priority_repos": "",
                "gemini_model": "gemini-3.7-pro",
                "deepseek_model": "deepseek-reasoner",
                "openai_model": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with SessionLocal() as db:
            settings = db.query(BotSettings).filter_by(user_id=user_id).one()
            self.assertEqual(settings.gemini_model, "gemini-3.7-pro")
            self.assertEqual(settings.deepseek_model, "deepseek-reasoner")
            self.assertEqual(settings.openai_model, "")
        self.assertIn(b"gemini-3.7-pro", response.data)

    def test_expired_login_form_recovers_with_fresh_page(self):
        stale_token = self.csrf("/login")
        with self.client.session_transaction() as browser_session:
            browser_session.clear()
        response = self.client.post(
            "/login",
            data={"csrf_token": stale_token, "username": "someone", "password": "invalid"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Your sign-in page expired", response.data)
        self.assertIn(b"Welcome back", response.data)

    def test_worker_creates_candidates_for_only_the_selected_user(self):
        with SessionLocal() as db:
            user = User(username="worker-user", must_change_password=False)
            user.set_password("worker-password-123")
            db.add(user)
            db.flush()
            settings = BotSettings(
                user_id=user.id,
                enabled=True,
                drips_session_encrypted=encrypt_secret('{"cookies": []}'),
            )
            db.add(settings)
            db.commit()

            applied = []

            class FakeClient:
                def __init__(self, session_state, on_refresh=None):
                    self.session_state = session_state

                def fetch_applications(self, status):
                    return [], False

                def fetch_open_issues(self):
                    return [{
                        "id": "issue-1",
                        "title": "Fix the test suite",
                        "repository": {"fullName": "example/repo"},
                        "htmlUrl": "https://github.com/example/repo/issues/1",
                        "points": 150,
                    }], False

                def apply(self, issue_id, message):
                    applied.append((issue_id, message))
                    return True, "Application submitted", False

                def withdraw(self, application):
                    return True, "Application withdrawn", False

            with patch.object(automation, "WaveClient", FakeClient):
                automation.run_user_cycle(db, user, settings)

            records = db.query(IssueRecord).filter_by(user_id=user.id).all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, "pending")
            self.assertEqual(records[0].application_text, "Hi, I can fix this")
            self.assertEqual(applied, [("issue-1", "Hi, I can fix this")])

    def test_automatic_worker_enforces_two_applications_per_repository(self):
        with SessionLocal() as db:
            user = User(username="limit-user", must_change_password=False)
            user.set_password("limit-user-password-123")
            db.add(user)
            db.flush()
            settings = BotSettings(
                user_id=user.id,
                enabled=True,
                max_active_applications=15,
                max_per_repo=2,
                drips_session_encrypted=encrypt_secret('{"cookies": []}'),
            )
            db.add(settings)
            db.commit()
            applied = []

            class FakeClient:
                def __init__(self, session_state, on_refresh=None):
                    self.session_state = session_state

                def fetch_applications(self, status):
                    return [], False

                def fetch_open_issues(self):
                    return [
                        {
                            "id": f"same-repo-{number}",
                            "title": f"Issue {number}",
                            "repository": {"fullName": "owner/repo"},
                        }
                        for number in range(3)
                    ], False

                def apply(self, issue_id, message):
                    applied.append(issue_id)
                    return True, "Application submitted", False

                def withdraw(self, application):
                    return True, "Application withdrawn", False

            with patch.object(automation, "WaveClient", FakeClient):
                automation.run_user_cycle(db, user, settings)

            self.assertEqual(len(applied), 2)
            self.assertEqual(
                db.query(IssueRecord).filter_by(user_id=user.id, status="pending").count(),
                2,
            )

    def test_priority_issue_preempts_non_priority_when_slots_are_full(self):
        with SessionLocal() as db:
            user = User(username="priority-user", must_change_password=False)
            user.set_password("priority-user-password-123")
            db.add(user)
            db.flush()
            settings = BotSettings(
                user_id=user.id,
                enabled=True,
                max_active_applications=1,
                max_per_repo=2,
                priority_repos="priority-owner",
                drips_session_encrypted=encrypt_secret('{"cookies": []}'),
            )
            db.add(settings)
            old_record = IssueRecord(
                user_id=user.id,
                issue_id="normal-issue",
                title="Normal issue",
                repo="normal-owner/repo",
                status="pending",
                applied_at=utcnow(),
            )
            db.add(old_record)
            db.commit()
            withdrawn = []
            applied = []

            class FakeClient:
                def __init__(self, session_state, on_refresh=None):
                    self.session_state = session_state

                def fetch_applications(self, status):
                    if status == "pending":
                        return [{
                            "id": "normal-app",
                            "issue": {
                                "id": "normal-issue",
                                "title": "Normal issue",
                                "repository": {"fullName": "normal-owner/repo"},
                            },
                        }], False
                    return [], False

                def fetch_open_issues(self):
                    return [{
                        "id": "priority-issue",
                        "title": "Priority issue",
                        "repository": {"fullName": "priority-owner/repo"},
                    }], False

                def apply(self, issue_id, message):
                    applied.append(issue_id)
                    return True, "Application submitted", False

                def withdraw(self, application):
                    withdrawn.append(application["id"])
                    return True, "Application withdrawn", False

            with patch.object(automation, "WaveClient", FakeClient):
                automation.run_user_cycle(db, user, settings)

            self.assertEqual(withdrawn, ["normal-app"])
            self.assertEqual(applied, ["priority-issue"])
            self.assertEqual(db.query(IssueRecord).filter_by(issue_id="normal-issue").one().status, "preempted")
            self.assertEqual(db.query(IssueRecord).filter_by(issue_id="priority-issue").one().status, "pending")

    def test_stale_application_is_withdrawn_and_replaced(self):
        with SessionLocal() as db:
            user = User(username="rotation-user", must_change_password=False)
            user.set_password("rotation-user-password-123")
            db.add(user)
            db.flush()
            settings = BotSettings(
                user_id=user.id,
                enabled=True,
                max_active_applications=1,
                stale_minutes=30,
                drips_session_encrypted=encrypt_secret('{"cookies": []}'),
            )
            db.add(settings)
            db.add(IssueRecord(
                user_id=user.id,
                issue_id="stale-issue",
                title="Stale issue",
                repo="old/repo",
                status="pending",
                applied_at=utcnow() - timedelta(minutes=31),
            ))
            db.commit()
            withdrawn = []
            applied = []

            class FakeClient:
                def __init__(self, session_state, on_refresh=None):
                    self.session_state = session_state

                def fetch_applications(self, status):
                    if status == "pending":
                        return [{
                            "id": "stale-app",
                            "issue": {
                                "id": "stale-issue",
                                "title": "Stale issue",
                                "repository": {"fullName": "old/repo"},
                            },
                        }], False
                    return [], False

                def fetch_open_issues(self):
                    return [{
                        "id": "fresh-issue",
                        "title": "Fresh issue",
                        "repository": {"fullName": "new/repo"},
                    }], False

                def withdraw(self, application):
                    withdrawn.append(application["id"])
                    return True, "Application withdrawn", False

                def apply(self, issue_id, message):
                    applied.append(issue_id)
                    return True, "Application submitted", False

            with patch.object(automation, "WaveClient", FakeClient):
                automation.run_user_cycle(db, user, settings)

            self.assertEqual(withdrawn, ["stale-app"])
            self.assertEqual(applied, ["fresh-issue"])
            self.assertEqual(db.query(IssueRecord).filter_by(issue_id="stale-issue").one().status, "expired")
            self.assertEqual(db.query(IssueRecord).filter_by(issue_id="fresh-issue").one().status, "pending")


if __name__ == "__main__":
    unittest.main()
