import re
from unittest.mock import patch

import pytest

import automation
import dashboard
from database import Base, SessionLocal, engine
from models import BotSettings, IssueRecord, User
from security import encrypt_secret


@pytest.fixture(autouse=True)
def _release_database():
    yield
    engine.dispose()


def _issue(issue_id):
    return {"id": issue_id, "title": f"Issue {issue_id}", "repository": {"fullName": "example/repo"}}


def _fake_client(open_issues, accepted=()):
    class FakeClient:
        def __init__(self, session_state, on_refresh=None):
            self.session_state = session_state

        def fetch_applications(self, status):
            if status == "accepted":
                return [{"id": f"app-{i}", "issue": _issue(i)} for i in accepted], False
            return [], False

        def fetch_open_issues(self):
            return [_issue(i) for i in open_issues], False

        def apply(self, issue_id, message):
            return False, "not applying in this test", False

        def withdraw(self, application):
            return True, "Application withdrawn", False

    return FakeClient


def _setup_user(db, username):
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    user = User(username=username, must_change_password=False)
    user.set_password(f"{username}-password-123")
    db.add(user)
    db.flush()
    settings = BotSettings(
        user_id=user.id, enabled=True, drips_session_encrypted=encrypt_secret('{"cookies": []}')
    )
    db.add(settings)
    db.commit()
    return user, settings


def _statuses(db, user_id):
    return {r.issue_id: r.status for r in db.query(IssueRecord).filter_by(user_id=user_id)}


def test_scan_drops_stale_queue_and_finished_assignments_then_restores_reopened_issues():
    with SessionLocal() as db:
        user, settings = _setup_user(db, "history-user")
        db.add_all([
            IssueRecord(user_id=user.id, issue_id="old", title="Old", status="candidate"),
            IssueRecord(user_id=user.id, issue_id="done", title="Done", status="accepted"),
            IssueRecord(user_id=user.id, issue_id="still", title="Still", status="accepted"),
        ])
        db.commit()

        with patch.object(automation, "WaveClient", _fake_client(["new"], accepted=["still"])):
            automation.run_user_cycle(db, user, settings)
        assert _statuses(db, user.id) == {
            "old": "closed", "done": "completed", "still": "accepted", "new": "candidate",
        }

        with patch.object(automation, "WaveClient", _fake_client(["old", "new"], accepted=["still"])):
            automation.run_user_cycle(db, user, settings)
        assert _statuses(db, user.id)["old"] == "candidate"


def test_clear_history_removes_only_the_current_users_records():
    with SessionLocal() as db:
        user, _settings = _setup_user(db, "clear-user")
        other = User(username="other-user", must_change_password=False)
        other.set_password("other-user-password-123")
        db.add(other)
        db.flush()
        db.add_all([
            IssueRecord(user_id=user.id, issue_id="mine", title="Mine", status="candidate"),
            IssueRecord(user_id=other.id, issue_id="theirs", title="Theirs", status="candidate"),
        ])
        db.commit()
        user_id, other_id = user.id, other.id

    app = dashboard.create_app({"TESTING": True, "SECRET_KEY": "test-session-secret"})
    client = app.test_client()
    token = re.search(rb'name="csrf_token" value="([^"]+)"', client.get("/login").data).group(1).decode()
    client.post("/login", data={
        "csrf_token": token, "username": "clear-user", "password": "clear-user-password-123",
    })
    token = re.search(rb'name="csrf_token" value="([^"]+)"', client.get("/dashboard").data).group(1).decode()
    response = client.post("/dashboard/clear-history", data={"csrf_token": token})

    assert response.status_code == 302
    with SessionLocal() as db:
        assert db.query(IssueRecord).filter_by(user_id=user_id).count() == 0
        assert db.query(IssueRecord).filter_by(user_id=other_id).count() == 1
