#!/usr/bin/env python3
"""Build a self-contained IssueBot connector for 64-bit Linux."""

import os
import subprocess
import sys


if not sys.platform.startswith("linux"):
    raise SystemExit("The Linux connector must be built on Linux")

build_env = os.environ.copy()
# Store Chromium beside the Playwright package so PyInstaller collects it.
build_env["PLAYWRIGHT_BROWSERS_PATH"] = "0"

subprocess.run([sys.executable, "-m", "PyInstaller", "--version"], check=True)
subprocess.run(
    [sys.executable, "-m", "playwright", "install", "chromium"],
    check=True,
    env=build_env,
)
subprocess.run(
    [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--onefile",
        "--name",
        "issuebot-connector",
        "--hidden-import",
        "playwright",
        "--collect-all",
        "playwright",
        "connector.py",
    ],
    check=True,
    env=build_env,
)
print("Built dist/issuebot-connector")
