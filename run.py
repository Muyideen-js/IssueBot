#!/usr/bin/env python3
"""
run.py — starts both bot.py and dashboard.py together.

Usage:
  python run.py                          # both platforms
  python run.py --platform grantfox      # GrantFox only
  python run.py --platform drips         # Drips Wave only
  python run.py --login                  # login flow
  python run.py --login --platform drips # login Drips only
"""

import subprocess
import sys
import time
import os

def main():
    args = sys.argv[1:]

    # If it's a login flow, just run bot.py directly — no dashboard needed
    if "--login" in args:
        subprocess.run([sys.executable, "bot.py"] + args)
        return

    print("Starting Issue Bot Dashboard on http://localhost:5000 ...")
    print("Starting Issue Bot automation...")
    print("Press Ctrl+C to stop both.\n")

    # Start dashboard in background
    dashboard = subprocess.Popen(
        [sys.executable, "dashboard.py"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)  # give dashboard a moment to start

    print("Dashboard running at http://localhost:5000")
    print(f"Bot running with args: {args or ['--platform both']}\n")

    # Run bot in foreground (Ctrl+C will stop it)
    try:
        subprocess.run([sys.executable, "bot.py"] + args)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        dashboard.terminate()
        print("Dashboard stopped.")

if __name__ == "__main__":
    main()
