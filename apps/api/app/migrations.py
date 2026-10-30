from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .database import Base


def ensure_schema(engine: Engine) -> None:
    """Create new tables and apply the bounded SQLite migration for legacy projects."""
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "sqlite":
        return

    statements: list[str] = []
    project_columns = {column["name"] for column in inspect(engine).get_columns("projects")}
    if "tenant_id" not in project_columns:
        statements.append("ALTER TABLE projects ADD COLUMN tenant_id VARCHAR")
    if "owner_id" not in project_columns:
        statements.append("ALTER TABLE projects ADD COLUMN owner_id VARCHAR")

    task_columns = {column["name"] for column in inspect(engine).get_columns("generation_tasks")}
    if "lease_epoch" not in task_columns:
        statements.append("ALTER TABLE generation_tasks ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0")
    if "circuit_probe_epoch" not in task_columns:
        statements.append("ALTER TABLE generation_tasks ADD COLUMN circuit_probe_epoch INTEGER")

    circuit_columns = {column["name"] for column in inspect(engine).get_columns("generation_circuit_states")}
    if "probe_epoch" not in circuit_columns:
        statements.append("ALTER TABLE generation_circuit_states ADD COLUMN probe_epoch INTEGER NOT NULL DEFAULT 0")
    if "half_open_lease_epoch" not in circuit_columns:
        statements.append("ALTER TABLE generation_circuit_states ADD COLUMN half_open_lease_epoch INTEGER")
    if "half_open_issuer" not in circuit_columns:
        statements.append("ALTER TABLE generation_circuit_states ADD COLUMN half_open_issuer VARCHAR")

    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_tenant_id ON projects (tenant_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_owner_id ON projects (owner_id)"))
