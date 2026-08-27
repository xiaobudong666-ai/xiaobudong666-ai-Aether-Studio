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
    inspect as sqlalchemy_inspect,
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


class DBGenerationTask(Base):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key",
            name="uq_generation_task_tenant_idempotency",
        ),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    requested_by = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, index=True)
    progress = Column(Integer, default=0, nullable=False)
    message = Column(String, nullable=False)
    request_json = Column(JSON, nullable=False)
    request_hash = Column(String, nullable=False)
    capability_snapshot_json = Column(JSON, nullable=False)
    capability_snapshot_hash = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=False)
    upstream_job_id = Column(String, nullable=True, index=True)
    provider_artifact_id = Column(String, nullable=True, index=True)
    media_id = Column(String, nullable=True, index=True)
    asset_version_id = Column(String, ForeignKey("asset_versions.id"), nullable=True, index=True)
    attempts = Column(Integer, default=1, nullable=False)
    max_attempts = Column(Integer, default=3, nullable=False)
    lease_owner = Column(String, nullable=True, index=True)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    cancel_requested_at = Column(DateTime(timezone=True), nullable=True)
    error_code = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DBGenerationAttempt(Base):
    __tablename__ = "generation_attempts"
    __table_args__ = (
        UniqueConstraint(
            "generation_task_id", "attempt_no",
            name="uq_generation_attempt_task_number",
        ),
    )

    id = Column(String, primary_key=True)
    generation_task_id = Column(String, ForeignKey("generation_tasks.id"), nullable=False, index=True)
    attempt_no = Column(Integer, nullable=False)
    status = Column(String, nullable=False, index=True)
    submission_started_at = Column(DateTime(timezone=True), nullable=True)
    upstream_job_id = Column(String, nullable=True, index=True)
    reconciliation_state = Column(String, nullable=False)
    error_code = Column(String, nullable=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class DBGenerationEvent(Base):
    __tablename__ = "generation_events"

    id = Column(String, primary_key=True)
    generation_task_id = Column(String, ForeignKey("generation_tasks.id"), nullable=False, index=True)
    attempt_id = Column(String, ForeignKey("generation_attempts.id"), nullable=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    from_status = Column(String, nullable=True)
    to_status = Column(String, nullable=True)
    actor_type = Column(String, nullable=False)
    actor_id = Column(String, nullable=False)
    metadata_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DBGenerationProviderConfigVersion(Base):
    __tablename__ = "generation_provider_config_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "version",
            name="uq_generation_provider_config_version",
        ),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(String, nullable=False, index=True)
    policy_json = Column(JSON, nullable=False)
    policy_hash = Column(String, nullable=False, index=True)
    created_by = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    published_by = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    supersedes_id = Column(
        String,
        ForeignKey("generation_provider_config_versions.id"),
        nullable=True,
        index=True,
    )


class DBGenerationProviderAttestation(Base):
    __tablename__ = "generation_provider_attestations"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    provider = Column(String, nullable=False, index=True)
    worker_id = Column(String, nullable=False, index=True)
    operator_mode = Column(String, nullable=False)
    config_version_id = Column(
        String,
        ForeignKey("generation_provider_config_versions.id"),
        nullable=True,
        index=True,
    )
    policy_hash = Column(String, nullable=True, index=True)
    adapter_version = Column(String, nullable=False)
    upstream_pin = Column(String, nullable=False)
    healthy = Column(Boolean, nullable=False)
    capabilities_json = Column(JSON, nullable=False)
    reason_code = Column(String, nullable=True)
    checked_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)


class DBGenerationUsageEntry(Base):
    __tablename__ = "generation_usage_entries"
    __table_args__ = (
        UniqueConstraint(
            "reservation_key", "kind",
            name="uq_generation_usage_reservation_kind",
        ),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.id"), nullable=False, index=True)
    task_id = Column(String, ForeignKey("generation_tasks.id"), nullable=False, index=True)
    attempt_id = Column(String, ForeignKey("generation_attempts.id"), nullable=False, index=True)
    kind = Column(String, nullable=False, index=True)
    request_units = Column(Integer, nullable=False)
    generated_seconds = Column(Integer, nullable=False)
    reservation_key = Column(String, nullable=False, index=True)
    config_version_id = Column(
        String,
        ForeignKey("generation_provider_config_versions.id"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)


class DBGenerationCircuitState(Base):
    __tablename__ = "generation_circuit_states"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider",
            name="uq_generation_circuit_tenant_provider",
        ),
    )

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    state = Column(String, nullable=False, index=True)
    failure_timestamps_json = Column(JSON, nullable=False)
    opened_at = Column(DateTime(timezone=True), nullable=True)
    cooldown_until = Column(DateTime(timezone=True), nullable=True)
    half_open_task_id = Column(String, ForeignKey("generation_tasks.id"), nullable=True, index=True)
    disabled_reason_code = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False)


class DBGenerationProviderEvent(Base):
    __tablename__ = "generation_provider_events"

    id = Column(String, primary_key=True)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=True, index=True)
    provider = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    actor_type = Column(String, nullable=False)
    actor_id = Column(String, nullable=False)
    metadata_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)


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


def _validate_provider_config_publish(_mapper, _connection, target) -> None:
    state = sqlalchemy_inspect(target)
    changed = {
        attribute.key
        for attribute in state.attrs
        if attribute.history.has_changes()
    }
    if not changed.issubset({"status", "published_by", "published_at"}):
        raise ValueError("DBGenerationProviderConfigVersion is immutable")
    previous_status = state.attrs.status.history.deleted
    old_status = previous_status[0] if previous_status else target.status
    if (old_status, target.status) not in {
        ("DRAFT", "PUBLISHED"),
        ("PUBLISHED", "SUPERSEDED"),
    }:
        raise ValueError("DBGenerationProviderConfigVersion has an illegal lifecycle transition")


for immutable_model in (
    DBAssetVersion,
    DBRightsSnapshot,
    DBMasterRevision,
    DBGenerationEvent,
    DBGenerationProviderAttestation,
    DBGenerationUsageEntry,
    DBGenerationProviderEvent,
):
    event.listen(immutable_model, "before_update", _reject_immutable_update)
    event.listen(immutable_model, "before_delete", _reject_immutable_update)

event.listen(
    DBGenerationProviderConfigVersion,
    "before_update",
    _validate_provider_config_publish,
)
event.listen(
    DBGenerationProviderConfigVersion,
    "before_delete",
    _reject_immutable_update,
)
