#!/usr/bin/env python3
import json
import os
import signal
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from database import SessionLocal, init_db
from models import ActivityLog, BotSettings, IssueRecord, User, utcnow
from security import decrypt_secret, encrypt_secret
from wave_service import WaveClient, WaveError, issue_points, issue_repo, issue_url


STOP_REQUESTED = False


def log_activity(db, user_id: int, message: str, level: str = "info") -> None:
    db.add(ActivityLog(user_id=user_id, message=message[:1000], level=level))


def _issue_id(issue: dict) -> str:
    return str(issue.get("id") or issue.get("issueId") or "")


def _application_issue(application: dict) -> dict:
    return application.get("issue") or application


def _draft(issue: dict, repo: str) -> str:
    title = (issue.get("title") or "this issue").strip()
    return (
        f"I reviewed {title}. I can investigate the existing implementation and submit a tested fix "
        f"that follows {repo or 'the repository'}'s contribution guidelines."
    )[:500]


def run_user_cycle(db, user: User, settings: BotSettings) -> None:
    settings.last_run_at = utcnow()
    settings.last_error = ""
    db.commit()

    try:
        raw_session = decrypt_secret(settings.drips_session_encrypted)
        if not raw_session:
            raise WaveError("Connect a Drips session before enabling monitoring")
        session_state = json.loads(raw_session)
        client = WaveClient(session_state)

        pending, session_changed = client.fetch_applications("pending")
        accepted, accepted_changed = client.fetch_applications("accepted")
        issues, issues_changed = client.fetch_open_issues()

        if session_changed or accepted_changed or issues_changed:
            settings.drips_session_encrypted = encrypt_secret(json.dumps(client.session_state))

        pending_ids = {_issue_id(_application_issue(app)) for app in pending}
        accepted_ids = {_issue_id(_application_issue(app)) for app in accepted}

        records = db.scalars(select(IssueRecord).where(IssueRecord.user_id == user.id)).all()
        by_issue = {record.issue_id: record for record in records}
        newly_accepted = []
        for status, applications in (("pending", pending), ("accepted", accepted)):
            for application in applications:
                issue = _application_issue(application)
                issue_id = _issue_id(issue)
                if not issue_id:
                    continue
                record = by_issue.get(issue_id)
                if not record:
                    record = IssueRecord(
                        user_id=user.id,
                        issue_id=issue_id,
                        title=(issue.get("title") or "Untitled issue")[:1000],
                        repo=issue_repo(issue)[:255],
                        url=issue_url(issue),
                        points=issue_points(issue),
                        status=status,
                    )
                    db.add(record)
                    records.append(record)
                    by_issue[issue_id] = record
                if status == "accepted" and record.status != "accepted":
                    newly_accepted.append(record)
                record.status = status

        for record in records:
            if record.status == "pending" and record.issue_id not in pending_ids | accepted_ids:
                record.status = "inactive"

        candidate_count = sum(record.status == "candidate" for record in records)
        slots = max(0, settings.max_candidates - candidate_count)
        created = 0
        for issue in issues:
            if created >= slots:
                break
            issue_id = _issue_id(issue)
            if not issue_id or issue_id in by_issue or issue_id in pending_ids or issue_id in accepted_ids:
                continue
            repo = issue_repo(issue)
            record = IssueRecord(
                user_id=user.id,
                issue_id=issue_id,
                title=(issue.get("title") or "Untitled issue")[:1000],
                repo=repo[:255],
                url=issue_url(issue),
                points=issue_points(issue),
                application_text=_draft(issue, repo),
                status="candidate",
            )
            db.add(record)
            created += 1

        settings.last_success_at = utcnow()
        log_activity(
            db,
            user.id,
            f"Wave scan complete: {len(pending)} pending, {len(accepted)} accepted, {created} new candidates.",
        )
        if created:
            log_activity(
                db,
                user.id,
                f"{created} new Wave candidate{'s are' if created != 1 else ' is'} ready for review.",
            )
        for record in newly_accepted:
            log_activity(db, user.id, f"Assignment accepted: {record.title}")
        db.commit()
    except Exception as exc:
        db.rollback()
        settings = db.get(BotSettings, settings.id)
        settings.last_run_at = utcnow()
        settings.last_error = str(exc)[:1000]
        log_activity(db, user.id, f"Wave scan failed: {exc}", "error")
        db.commit()


def run_once() -> None:
    init_db()
    with SessionLocal() as db:
        settings_rows = db.scalars(
            select(BotSettings)
            .join(User)
            .where(BotSettings.enabled.is_(True), User.is_active.is_(True))
        ).all()
        now = utcnow()
        for settings in settings_rows:
            due_at = (settings.last_run_at or datetime.min) + timedelta(minutes=settings.poll_minutes)
            if due_at > now:
                continue
            user = db.get(User, settings.user_id)
            run_user_cycle(db, user, settings)


def _stop(*_args) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def main() -> None:
    global STOP_REQUESTED
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _stop)

    interval = max(15, int(os.getenv("WORKER_TICK_SECONDS", "30")))
    while not STOP_REQUESTED:
        run_once()
        for _ in range(interval):
            if STOP_REQUESTED:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
