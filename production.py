#!/usr/bin/env python3
"""Run the web portal and monitor together on a single Render free instance."""

import os
import signal
import subprocess
import sys

from database import init_db


def main() -> None:
    init_db()
    port = os.getenv("PORT", "10000")
    worker = subprocess.Popen([sys.executable, "worker.py"])
    web = subprocess.Popen([
        sys.executable,
        "-m",
        "gunicorn",
        "dashboard:app",
        "--bind",
        f"0.0.0.0:{port}",
        "--workers",
        "1",
        "--threads",
        "4",
        "--timeout",
        "120",
    ])

    def stop(*_args) -> None:
        for process in (web, worker):
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        exit_code = web.wait()
    finally:
        stop()
        for process in (web, worker):
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
