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
import worker  # noqa: E402
from database import Base, SessionLocal, engine  # noqa: E402
from models import BotSettings, EnrollmentToken, IssueRecord, User, utcnow  # noqa: E402
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

    def test_post_without_csrf_is_rejected(self):
        response = self.client.post("/login", data={"username": "admin", "password": "x"})
        self.assertEqual(response.status_code, 400)

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

            class FakeClient:
                def __init__(self, session_state):
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

            with patch.object(worker, "WaveClient", FakeClient):
                worker.run_user_cycle(db, user, settings)

            records = db.query(IssueRecord).filter_by(user_id=user.id).all()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].status, "candidate")
            self.assertIn("Fix the test suite", records[0].application_text)


if __name__ == "__main__":
    unittest.main()
