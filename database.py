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
