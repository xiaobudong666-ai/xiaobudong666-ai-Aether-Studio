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
