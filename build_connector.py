#!/usr/bin/env python3
"""Build the user-facing Windows connector without bundling service source or secrets."""

import subprocess
import sys


subprocess.run([sys.executable, "-m", "PyInstaller", "--version"], check=True)
subprocess.run([
    sys.executable,
    "-m",
    "PyInstaller",
    "--onefile",
    "--name",
    "issuebot-connector",
    "--hidden-import",
    "playwright",
    "--collect-all",
    "playwright",
    "connector.py",
], check=True)
print("Built dist\\issuebot-connector.exe")
