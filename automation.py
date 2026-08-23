"""Automatic Drips Wave application scheduling for one portal user."""

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ai_writer import generate_application_message
from models import ActivityLog, BotSettings, IssueRecord, User, utcnow
from security import decrypt_secret, encrypt_secret
from wave_service import WaveClient, WaveError, issue_points, issue_repo, issue_url


def log_activity(db, user_id: int, message: str, level: str = "info") -> None:
    db.add(ActivityLog(user_id=user_id, message=message[:1000], level=level))


def _issue_id(issue: dict) -> str:
    return str(issue.get("id") or issue.get("issueId") or "")


def _application_issue(application: dict) -> dict:
    return application.get("issue") or application


def _priority_names(raw: str) -> set[str]:
    return {value.strip().lower() for value in re.split(r"[,\n]", raw or "") if value.strip()}


def _is_priority_repo(repo: str, priorities: set[str]) -> bool:
    normalized = (repo or "").strip().lower()
    owner = normalized.split("/", 1)[0]
    return normalized in priorities or owner in priorities


def _add_priority_repo(settings: BotSettings, repo: str) -> bool:
    normalized = (repo or "").strip()
    if not normalized or _is_priority_repo(normalized, _priority_names(settings.priority_repos)):
        return False
    values = [value.strip() for value in re.split(r"[,\n]", settings.priority_repos or "") if value.strip()]
    settings.priority_repos = "\n".join([*values, normalized])
    return True


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _application_time(application: dict, record: IssueRecord | None, now: datetime) -> datetime:
    for field in ("appliedAt", "createdAt", "created_at", "updatedAt"):
        parsed = _parse_timestamp(application.get(field))
        if parsed:
            return parsed
    return record.applied_at if record and record.applied_at else now


def _provider_keys(settings: BotSettings) -> dict[str, str]:
    return {
        "gemini": decrypt_secret(settings.gemini_api_key_encrypted),
        "deepseek": decrypt_secret(settings.deepseek_api_key_encrypted),
        "openai": decrypt_secret(settings.openai_api_key_encrypted),
    }


def _find_preemption_victim(
    pending: list[dict], priorities: set[str], by_issue: dict[str, IssueRecord], now: datetime
) -> dict | None:
    eligible = []
    for application in pending:
        issue = _application_issue(application)
        issue_id = _issue_id(issue)
        record = by_issue.get(issue_id)
        repo = issue_repo(issue) or (record.repo if record else "")
        if not _is_priority_repo(repo, priorities):
            eligible.append((_application_time(application, record, now), application))
    return min(eligible, key=lambda item: item[0])[1] if eligible else None


def _rank_issues(issues: list[dict], priorities: set[str]) -> list[dict]:
    newest_first = sorted(
        issues,
        key=lambda issue: str(issue.get("updatedAt") or issue.get("createdAt") or ""),
        reverse=True,
    )
    return sorted(
        newest_first,
        key=lambda issue: 0 if _is_priority_repo(issue_repo(issue), priorities) else 1,
    )


def run_user_cycle(db, user: User, settings: BotSettings) -> None:
    settings.last_run_at = utcnow()
    settings.last_error = ""
    db.commit()

    try:
        raw_session = decrypt_secret(settings.drips_session_encrypted)
        if not raw_session:
            raise WaveError("Connect a Drips session before enabling monitoring")
        client = WaveClient(json.loads(raw_session))
        pending, session_changed = client.fetch_applications("pending")
        accepted, accepted_changed = client.fetch_applications("accepted")
        issues, issues_changed = client.fetch_open_issues()
        session_changed = session_changed or accepted_changed or issues_changed
        now = utcnow()

        records = db.scalars(select(IssueRecord).where(IssueRecord.user_id == user.id)).all()
        by_issue = {record.issue_id: record for record in records}
        pending_ids = {_issue_id(_application_issue(app)) for app in pending}
        accepted_ids = {_issue_id(_application_issue(app)) for app in accepted}
        newly_accepted = []

        for status, applications in (("pending", pending), ("accepted", accepted)):
            for application in applications:
                issue = _application_issue(application)
                issue_id = _issue_id(issue)
                if not issue_id:
                    continue
                record = by_issue.get(issue_id)
                previous_status = record.status if record else None
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
                if status == "accepted" and previous_status != "accepted":
                    newly_accepted.append(record)
                record.status = status
                record.application_id = str(application.get("id") or "")
                if status == "pending" and not record.applied_at:
                    record.applied_at = _application_time(application, record, now)

        for record in records:
            if record.status == "pending" and record.issue_id not in pending_ids | accepted_ids:
                record.status = "inactive"

        for record in newly_accepted:
            promoted = _add_priority_repo(settings, record.repo)
            suffix = " Repository added to priorities." if promoted else ""
            log_activity(db, user.id, f"Assignment accepted: {record.title}.{suffix}")

        priorities = _priority_names(settings.priority_repos)
        stale_before = now - timedelta(minutes=settings.stale_minutes)
        active_pending = []
        stale_withdrawn = 0
        for application in pending:
            issue_id = _issue_id(_application_issue(application))
            record = by_issue.get(issue_id)
            if _application_time(application, record, now) >= stale_before:
                active_pending.append(application)
                continue
            ok, message, changed = client.withdraw(application)
            session_changed = session_changed or changed
            if not ok:
                log_activity(db, user.id, f"Could not rotate {record.title if record else issue_id}: {message}", "error")
                active_pending.append(application)
                continue
            stale_withdrawn += 1
            if record:
                record.status = "expired"
            log_activity(db, user.id, f"Rotated stale application: {record.title if record else issue_id}")
        pending = active_pending

        available = []
        current_pending_ids = {_issue_id(_application_issue(app)) for app in pending}
        for issue in issues:
            issue_id = _issue_id(issue)
            if not issue_id or issue_id in accepted_ids or issue_id in current_pending_ids:
                continue
            record = by_issue.get(issue_id)
            if record and record.status != "candidate":
                continue
            if not record:
                record = IssueRecord(
                    user_id=user.id,
                    issue_id=issue_id,
                    title=(issue.get("title") or "Untitled issue")[:1000],
                    repo=issue_repo(issue)[:255],
                    url=issue_url(issue),
                    points=issue_points(issue),
                    status="candidate",
                )
                db.add(record)
                records.append(record)
                by_issue[issue_id] = record
            available.append(issue)

        provider_keys = _provider_keys(settings)
        applied = 0
        repo_counts = Counter(issue_repo(_application_issue(app)).lower() for app in pending)
        for issue in _rank_issues(available, priorities):
            repo = issue_repo(issue)
            repo_key = repo.lower()
            if repo_counts[repo_key] >= settings.max_per_repo:
                continue
            if len(pending) >= settings.max_active_applications:
                if not _is_priority_repo(repo, priorities):
                    continue
                victim = _find_preemption_victim(pending, priorities, by_issue, now)
                if not victim:
                    continue
                ok, message, changed = client.withdraw(victim)
                session_changed = session_changed or changed
                if not ok:
                    log_activity(db, user.id, f"Could not make room for priority issue: {message}", "error")
                    continue
                victim_issue = _application_issue(victim)
                victim_id = _issue_id(victim_issue)
                victim_repo = issue_repo(victim_issue).lower()
                repo_counts[victim_repo] = max(0, repo_counts[victim_repo] - 1)
                pending.remove(victim)
                if by_issue.get(victim_id):
                    by_issue[victim_id].status = "preempted"
                log_activity(db, user.id, f"Withdrew a non-priority application for {repo}")
            message, provider = generate_application_message(
                issue, repo, provider_keys, settings.preferred_ai_provider, settings.fallback_message
            )
            ok, detail, changed = client.apply(_issue_id(issue), message)
            session_changed = session_changed or changed
            if not ok:
                log_activity(db, user.id, f"Application failed for {issue.get('title') or _issue_id(issue)}: {detail}", "error")
                continue
            record = by_issue[_issue_id(issue)]
            record.application_text = message
            record.status = "pending"
            record.applied_at = now
            pending.append({"issue": issue})
            repo_counts[repo_key] += 1
            applied += 1
            log_activity(db, user.id, f"Applied automatically via {provider}: {record.title}")

        if session_changed:
            settings.drips_session_encrypted = encrypt_secret(json.dumps(client.session_state))
        settings.last_success_at = now
        log_activity(
            db,
            user.id,
            f"Wave scan complete: {len(pending)} pending, {len(accepted)} accepted, "
            f"{applied} applied, {stale_withdrawn} stale withdrawn.",
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        settings = db.get(BotSettings, settings.id)
        settings.last_run_at = utcnow()
        settings.last_error = str(exc)[:1000]
        log_activity(db, user.id, f"Wave scan failed: {exc}", "error")
        db.commit()
