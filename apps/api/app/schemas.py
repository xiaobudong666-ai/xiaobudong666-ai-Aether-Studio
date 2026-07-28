from pydantic import BaseModel, Field
from typing import List, Optional, Literal

class RationalTimeSchema(BaseModel):
    value: int
    timescale: int = Field(..., gt=0)

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
    id: str
    name: str
    timeline: TimelineSchema
    materials: List[MaterialSchema]
    revision: int
    createdAt: str
    updatedAt: str

    class Config:
        from_attributes = True

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1)

class UpdateProjectRequest(BaseModel):
    name: Optional[str] = None
    timeline: Optional[TimelineSchema] = None
    materials: Optional[List[MaterialSchema]] = None
    expectedRevision: int
