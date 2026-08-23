#!/usr/bin/env python3
"""Run the private IssueBot portal and background worker locally."""

import subprocess
import sys

from database import init_db


def main() -> None:
    print("Starting IssueBot at http://localhost:5000")
    init_db()
    worker = subprocess.Popen([sys.executable, "worker.py"])
    try:
        subprocess.run([sys.executable, "dashboard.py"])
    except KeyboardInterrupt:
        pass
    finally:
        worker.terminate()
        worker.wait(timeout=10)


if __name__ == "__main__":
    main()
