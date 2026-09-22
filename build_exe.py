#!/usr/bin/env python3
"""
Build drips_fast.exe for Windows distribution.
Run this on your Windows PC:
  python build_exe.py
"""

import subprocess
import sys
import os

def build():
    print("Installing PyInstaller...")
    subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    print("\nBuilding exe...")
    subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "drips_fast",
        "--hidden-import", "playwright",
        "--hidden-import", "dotenv",
        "--hidden-import", "requests",
        "--collect-all", "playwright",
        "drips_fast.py"
    ], check=True)

    print("\n✅ Done! Find your exe at: dist\\drips_fast.exe")
    print("\nShare with friends:")
    print("  1. dist\\drips_fast.exe")
    print("  2. .env.example (they rename to .env and fill in)")
    print("  3. setup.txt (instructions below)")

if __name__ == "__main__":
    build()