#!/usr/bin/env python3
import os
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select, text

from automation import run_user_cycle
from database import SessionLocal, engine, init_db
from models import BotSettings, User, utcnow


STOP_REQUESTED = False
SCAN_LOCK_KEY = 4_971_756_636_225_215_491
_PROCESS_SCAN_LOCK = threading.Lock()


@contextmanager
def _scan_lock():
    """Prevent scheduler retries or overlapping deployments from scanning twice."""
    if not _PROCESS_SCAN_LOCK.acquire(blocking=False):
        yield False
        return

    connection = None
    acquired = True
    try:
        if engine.dialect.name == "postgresql":
            connection = engine.connect()
            acquired = bool(connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": SCAN_LOCK_KEY},
            ))
        yield acquired
    finally:
        if connection is not None:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": SCAN_LOCK_KEY},
                )
            connection.close()
        _PROCESS_SCAN_LOCK.release()


def run_once(*, max_users: int | None = None, time_budget_seconds: int | None = None) -> dict:
    """Run a bounded batch of due users and return a scheduler-friendly summary."""
    init_db()
    started = time.monotonic()
    deadline = started + time_budget_seconds if time_budget_seconds is not None else None
    processed = 0
    failed = 0

    with _scan_lock() as acquired:
        if not acquired:
            return {"status": "busy", "processed": 0, "failed": 0}

        with SessionLocal() as db:
            settings_rows = db.scalars(
                select(BotSettings)
                .join(User)
                .where(BotSettings.enabled.is_(True), User.is_active.is_(True))
                .order_by(
                    BotSettings.last_run_at.asc().nullsfirst(),
                    BotSettings.id.asc(),
                )
            ).all()
            now = utcnow()
            for settings in settings_rows:
                due_at = (settings.last_run_at or datetime.min) + timedelta(minutes=settings.poll_minutes)
                if due_at > now:
                    continue
                if max_users is not None and processed >= max_users:
                    break
                if time_budget_seconds is not None and time.monotonic() - started >= time_budget_seconds:
                    break
                user = db.get(User, settings.user_id)
                run_user_cycle(db, user, settings, deadline=deadline)
                processed += 1
                if settings.last_error:
                    failed += 1

    return {"status": "complete", "processed": processed, "failed": failed}


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
