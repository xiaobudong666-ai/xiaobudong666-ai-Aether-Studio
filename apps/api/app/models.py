from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
)

from .database import Base


class DBTenant(Base):
    __tablename__ = "tenants"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    slug = Column(String, nullable=False, unique=True, index=True)
    project_quota = Column(Integer, default=50, nullable=False)
    storage_quota_bytes = Column(BigInteger, default=50 * 1024**3, nullable=False)
    used_storage_bytes = Column(BigInteger, default=0, nullable=False)
    concurrent_render_quota = Column(Integer, default=2, nullable=False)
    monthly_render_seconds_quota = Column(Integer, default=36_000, nullable=False)
    render_seconds_used = Column(Integer, default=0, nullable=False)
    quota_period = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class DBUser(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    email = Column(String, nullable=False, unique=True, index=True)
    display_name = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class DBSession(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=False)


class DBProject(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, index=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    owner_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    timeline = Column(JSON, nullable=False)
    materials = Column(JSON, nullable=False)
    revision = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class DBRenderTask(Base):
    __tablename__ = "render_tasks"

    id = Column(String, primary_key=True, index=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    requested_by = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    progress = Column(Integer, default=0, nullable=False)
    message = Column(String, nullable=False)
    engine = Column(String, default="video-use", nullable=False)
    render_payload = Column(JSON, nullable=False)
    upstream_job_id = Column(String, nullable=True, unique=True, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    max_attempts = Column(Integer, default=3, nullable=False)
    reserved_seconds = Column(Integer, default=0, nullable=False)
    lease_owner = Column(String, nullable=True, index=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    error = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DBExternalTask(Base):
    __tablename__ = "external_tasks"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    requested_by = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    engine = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class DBAssetVersion(Base):
    __tablename__ = "asset_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "media_id", "version_no",
            name="uq_asset_version_project_media_version",
        ),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    media_id = Column(String, nullable=False, index=True)
    version_no = Column(Integer, nullable=False)
    sha256 = Column(String, nullable=False, index=True)
    media_type = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    size_bytes = Column(BigInteger, nullable=False)
    probe_json = Column(JSON, nullable=False)
    created_by = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DBRightsSnapshot(Base):
    __tablename__ = "rights_snapshots"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    asset_version_id = Column(String, ForeignKey("asset_versions.id"), nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    purpose = Column(String, nullable=False)
    territory = Column(String, nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    evidence_ref = Column(String, nullable=True)
    captured_by = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    captured_at = Column(DateTime(timezone=True), nullable=False)


class DBCandidate(Base):
    __tablename__ = "candidates"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    task_id = Column(String, ForeignKey("render_tasks.id"), nullable=False, unique=True, index=True)
    artifact_ref = Column(String, nullable=False)
    input_revision = Column(Integer, nullable=False)
    status = Column(String, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DBAdoption(Base):
    __tablename__ = "adoptions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_adoption_tenant_idempotency",
        ),
        UniqueConstraint("candidate_id", name="uq_adoption_candidate"),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    candidate_id = Column(String, ForeignKey("candidates.id"), nullable=False, index=True)
    adopted_by = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    adopted_at = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String, nullable=False)
    supersedes_id = Column(String, ForeignKey("adoptions.id"), nullable=True, index=True)
    idempotency_key = Column(String, nullable=False)


class DBMasterRevision(Base):
    __tablename__ = "master_revisions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "revision_no",
            name="uq_master_revision_project_revision",
        ),
        UniqueConstraint("adoption_id", name="uq_master_revision_adoption"),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    adoption_id = Column(String, ForeignKey("adoptions.id"), nullable=False, index=True)
    revision_no = Column(Integer, nullable=False)
    artifact_ref = Column(String, nullable=False)
    sha256 = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


def _reject_immutable_update(_mapper, _connection, target) -> None:
    raise ValueError(f"{target.__class__.__name__} is immutable")


for immutable_model in (DBAssetVersion, DBRightsSnapshot, DBMasterRevision):
    event.listen(immutable_model, "before_update", _reject_immutable_update)
