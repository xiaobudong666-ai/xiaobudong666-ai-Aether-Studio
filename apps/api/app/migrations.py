from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .database import Base


def ensure_schema(engine: Engine) -> None:
    """Create new tables and apply the bounded SQLite migration for legacy projects."""
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "sqlite":
        return

    columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    statements: list[str] = []
    if "tenant_id" not in columns:
        statements.append("ALTER TABLE projects ADD COLUMN tenant_id VARCHAR")
    if "owner_id" not in columns:
        statements.append("ALTER TABLE projects ADD COLUMN owner_id VARCHAR")
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_tenant_id ON projects (tenant_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_owner_id ON projects (owner_id)"))
