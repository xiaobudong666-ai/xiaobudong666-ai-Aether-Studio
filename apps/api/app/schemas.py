from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .task_status import canonical_task_status

MAX_SAFE_INTEGER = 9_007_199_254_740_991

class RationalTimeSchema(BaseModel):
    value: int = Field(..., ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)
    timescale: int = Field(..., gt=0, le=MAX_SAFE_INTEGER)

class ClipSchema(BaseModel):
    id: str
    trackId: str
    materialId: str
    start: RationalTimeSchema
    duration: RationalTimeSchema
    sourceIn: RationalTimeSchema
    volume: float = Field(default=1.0, ge=0, le=4)
    opacity: float = Field(default=1.0, ge=0, le=1)
    x: int = 0
    y: int = 0
    width: Optional[int] = Field(default=None, gt=0, le=7680)
    height: Optional[int] = Field(default=None, gt=0, le=4320)
    text: Optional[str] = Field(default=None, max_length=2_000)

class TrackSchema(BaseModel):
    id: str
    name: str
    type: Literal["video", "audio", "subtitle"]
    clips: List[ClipSchema]

class TimelineSchema(BaseModel):
    version: Literal["1.1"] = "1.1"
    tracks: List[TrackSchema]

class MaterialSchema(BaseModel):
    id: str
    name: str
    url: str
    type: Literal["video", "audio", "image"]
    contentType: Optional[str] = None
    duration: Optional[RationalTimeSchema] = None
    sizeBytes: int = Field(default=0, ge=0)

class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    timeline: TimelineSchema
    materials: List[MaterialSchema]
    revision: int
    createdAt: str
    updatedAt: str

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1)

class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    timeline: Optional[TimelineSchema] = None
    materials: Optional[List[MaterialSchema]] = None
    expectedRevision: int = Field(..., ge=0)


class MoneyPrinterGenerateRequest(BaseModel):
    video_subject: str = Field(..., min_length=1)
    video_aspect: Optional[str] = "9:16"
    voice_name: Optional[str] = "en-US-JennyNeural"
    video_concat_mode: Optional[str] = "random"
    video_clip_duration: Optional[int] = 5


class GenerationTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    videoSubject: str = Field(..., min_length=1, max_length=500)
    videoAspect: Literal["16:9", "9:16", "1:1"] = "9:16"
    voiceName: str = Field(default="en-US-JennyNeural", min_length=1, max_length=120)
    videoConcatMode: Literal["random", "sequential"] = "random"
    videoClipDuration: int = Field(default=5, ge=1, le=10)
    outputCount: int = Field(default=1, ge=1, le=4)
    inputAssetVersionIds: List[str] = Field(default_factory=list, max_length=20)
    idempotencyKey: UUID
    capabilitySnapshotHash: str = Field(..., min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    expectedProjectRevision: int = Field(..., ge=0)
    confirmExternalGeneration: Literal[True]

    @field_validator("videoSubject", "voiceName")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("inputAssetVersionIds")
    @classmethod
    def unique_asset_versions(cls, value: List[str]) -> List[str]:
        if any(not item.strip() for item in value):
            raise ValueError("input asset version identifiers must not be blank")
        if len(set(value)) != len(value):
            raise ValueError("input asset version identifiers must be unique")
        return value


class GenerationWorkerHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerationWorkerTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "SUBMITTING", "RUNNING", "INGESTING", "FAILED", "CANCELED", "UNKNOWN", "PARTIAL"
    ]
    progress: int = Field(..., ge=0, le=100)
    message: str = Field(..., min_length=1, max_length=500)
    upstreamJobId: Optional[str] = Field(default=None, min_length=1, max_length=128)
    providerArtifactId: Optional[str] = Field(default=None, min_length=1, max_length=256)
    errorCode: Optional[str] = Field(default=None, min_length=1, max_length=120)
    errorMessage: Optional[str] = Field(default=None, min_length=1, max_length=500)
    retryable: bool = False


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=1_024)


class CreateUserRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    displayName: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=12, max_length=1_024)
    role: Literal["owner", "editor", "viewer"]


class WorkerTaskUpdateRequest(BaseModel):
    status: Literal[
        "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELED", "PARTIAL", "UNKNOWN"
    ]
    progress: int = Field(..., ge=0, le=100)
    message: str = Field(..., min_length=1, max_length=2_000)
    upstreamJobId: Optional[str] = Field(default=None, max_length=128)
    error: Optional[str] = Field(default=None, max_length=4_000)
    retryable: bool = False

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: str) -> str:
        return canonical_task_status(value)


class CreateRightsSnapshotRequest(BaseModel):
    status: Literal["ALLOWED", "DENIED", "REVOKED", "UNKNOWN"]
    purpose: str = Field(..., min_length=1, max_length=120)
    territory: str = Field(default="GLOBAL", min_length=1, max_length=120)
    validFrom: Optional[datetime] = None
    validUntil: Optional[datetime] = None
    evidenceRef: Optional[str] = Field(default=None, max_length=2_000)

    @field_validator("validUntil")
    @classmethod
    def validate_window(cls, value: Optional[datetime], info):
        valid_from = info.data.get("validFrom")
        if value is not None and valid_from is not None and value <= valid_from:
            raise ValueError("validUntil must be later than validFrom")
        return value


class AdoptCandidateRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2_000)
    supersedesId: Optional[str] = Field(default=None, max_length=128)
