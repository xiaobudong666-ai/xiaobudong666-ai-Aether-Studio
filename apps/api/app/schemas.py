from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

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


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    password: str = Field(..., min_length=1, max_length=1_024)


class CreateUserRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=320)
    displayName: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=12, max_length=1_024)
    role: Literal["owner", "editor", "viewer"]


class WorkerTaskUpdateRequest(BaseModel):
    status: Literal["processing", "completed", "failed", "queued"]
    progress: int = Field(..., ge=0, le=100)
    message: str = Field(..., min_length=1, max_length=2_000)
    upstreamJobId: Optional[str] = Field(default=None, max_length=128)
    error: Optional[str] = Field(default=None, max_length=4_000)
    retryable: bool = False
