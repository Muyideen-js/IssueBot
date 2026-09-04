import os
import time

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import DeclarativeBase, sessionmaker


load_dotenv()
load_dotenv(".env.local")


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///issuebot.db")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _database_url()
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    import models  # noqa: F401

    for attempt in range(10):
        try:
            Base.metadata.create_all(engine)
            _remove_legacy_telegram_columns()
            _add_automation_columns()
            return
        except (OperationalError, ProgrammingError) as exc:
            message = str(exc).lower()
            if attempt == 9 or ("already exists" not in message and "duplicate" not in message):
                raise
            time.sleep(0.2)


def _remove_legacy_telegram_columns() -> None:
    """Upgrade databases created before notifications moved into the portal."""
    table_names = inspect(engine).get_table_names()
    if "bot_settings" not in table_names:
        return
    columns = {column["name"] for column in inspect(engine).get_columns("bot_settings")}
    legacy_columns = columns & {"telegram_token_encrypted", "telegram_chat_id_encrypted"}
    for column in sorted(legacy_columns):
        statement = (
            f'ALTER TABLE bot_settings DROP COLUMN IF EXISTS "{column}"'
            if engine.dialect.name == "postgresql"
            else f'ALTER TABLE bot_settings DROP COLUMN "{column}"'
        )
        try:
            with engine.begin() as connection:
                connection.execute(text(statement))
        except (OperationalError, ProgrammingError) as exc:
            message = str(exc).lower()
            if "no such column" not in message and "does not exist" not in message:
                raise


def _add_automation_columns() -> None:
    """Upgrade existing installations without requiring a migration service."""
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    additions = {
        "bot_settings": {
            "gemini_api_key_encrypted": "TEXT NOT NULL DEFAULT ''",
            "deepseek_api_key_encrypted": "TEXT NOT NULL DEFAULT ''",
            "openai_api_key_encrypted": "TEXT NOT NULL DEFAULT ''",
            "groq_api_key_encrypted": "TEXT NOT NULL DEFAULT ''",
            "preferred_ai_provider": "VARCHAR(16) NOT NULL DEFAULT 'gemini'",
            "fallback_message": "TEXT NOT NULL DEFAULT 'Hi, I can fix this'",
            "max_active_applications": "INTEGER NOT NULL DEFAULT 15",
            "max_per_repo": "INTEGER NOT NULL DEFAULT 2",
            "stale_minutes": "INTEGER NOT NULL DEFAULT 30",
            "priority_repos": (
                "TEXT NOT NULL DEFAULT 'Fluxora-Org\nTalenttrust\nChronopay'"
            ),
            "gemini_model": "VARCHAR(80) NOT NULL DEFAULT ''",
            "deepseek_model": "VARCHAR(80) NOT NULL DEFAULT ''",
            "openai_model": "VARCHAR(80) NOT NULL DEFAULT ''",
            "groq_model": "VARCHAR(80) NOT NULL DEFAULT ''",
        },
        "issue_records": {
            "application_id": "VARCHAR(120) NOT NULL DEFAULT ''",
        },
    }
    for table_name, columns in additions.items():
        if table_name not in table_names:
            continue
        existing = {
            column["name"] for column in inspect(engine).get_columns(table_name)
        }
        for column_name, definition in columns.items():
            if column_name in existing:
                continue
            with engine.begin() as connection:
                connection.execute(text(
                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {definition}'
                ))
