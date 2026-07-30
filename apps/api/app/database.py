import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()


def _prepare_sqlite_directory(database_url: str) -> None:
    if not database_url.startswith("sqlite:///") or database_url.endswith(":memory:"):
        return
    raw_path = database_url.removeprefix("sqlite:///")
    db_path = Path(f"/{raw_path.lstrip('/')}" if raw_path.startswith("/") else raw_path)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)


def build_engine(database_url: str):
    _prepare_sqlite_directory(database_url)
    connect_args = (
        {"check_same_thread": False, "timeout": 30}
        if database_url.startswith("sqlite")
        else {}
    )
    created_engine = create_engine(database_url, connect_args=connect_args)

    if not database_url.startswith("sqlite"):
        return created_engine

    @event.listens_for(created_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA busy_timeout=30000;")
        cursor.close()

    return created_engine


DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///aether.db")
engine = build_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
