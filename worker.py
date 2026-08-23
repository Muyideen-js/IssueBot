#!/usr/bin/env python3
import os
import signal
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from automation import run_user_cycle
from database import SessionLocal, init_db
from models import BotSettings, User, utcnow


STOP_REQUESTED = False


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
