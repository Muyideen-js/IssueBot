"""Isolate the complete test suite from the local IssueBot database."""

import base64
import os
import tempfile
from pathlib import Path


TEST_DATABASE_DIR = tempfile.TemporaryDirectory()
os.environ["DATABASE_URL"] = f"sqlite:///{Path(TEST_DATABASE_DIR.name) / 'suite.db'}"
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("APP_SECRET", "test-app-secret")
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY",
    base64.urlsafe_b64encode(b"1" * 32).decode(),
)


def pytest_sessionfinish(session, exitstatus):
    del session, exitstatus
    TEST_DATABASE_DIR.cleanup()
