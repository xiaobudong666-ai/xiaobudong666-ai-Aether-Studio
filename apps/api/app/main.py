import asyncio
import uuid
import datetime
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Dict

from .database import Base, engine, get_db
from .models import DBProject
from .schemas import (
    CreateProjectRequest,
    UpdateProjectRequest,
    ProjectResponse,
    TimelineSchema
)

# Create database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Aether Studio API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mocked background tasks in-memory state
# Keys: taskId, Values: TaskProgressPayload
active_tasks: Dict[str, dict] = {}

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "api",
        "database": "sqlite",
        "journal_mode": "WAL",
        "timestamp": datetime.datetime.utcnow().isoformat()
    }

@app.get("/projects", response_model=List[ProjectResponse])
def list_projects(db: Session = Depends(get_db)):
    db_projects = db.query(DBProject).all()
    results = []
    for p in db_projects:
        results.append(ProjectResponse(
            id=p.id,
            name=p.name,
            timeline=p.timeline,
            materials=p.materials,
            revision=p.revision,
            createdAt=p.created_at.isoformat() + "Z",
            updatedAt=p.updated_at.isoformat() + "Z"
        ))
    return results

@app.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(req: CreateProjectRequest, db: Session = Depends(get_db)):
    project_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow()

    # Base empty Canonical Timeline v1.1
    default_timeline = {
        "version": "1.1",
        "tracks": []
    }

    db_project = DBProject(
        id=project_id,
        name=req.name,
        timeline=default_timeline,
        materials=[],
        revision=1,
        created_at=now,
        updated_at=now
    )
    db.add(db_project)
    db.commit()
    db.refresh(db_project)

    return ProjectResponse(
        id=db_project.id,
        name=db_project.name,
        timeline=db_project.timeline,
        materials=db_project.materials,
        revision=db_project.revision,
        createdAt=db_project.created_at.isoformat() + "Z",
        updatedAt=db_project.updated_at.isoformat() + "Z"
    )

@app.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, db: Session = Depends(get_db)):
    p = db.query(DBProject).filter(DBProject.id == project_id).first()
    if not p:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROJECT_NOT_FOUND",
                "message": f"Project {project_id} not found"
            }
        )
    return ProjectResponse(
        id=p.id,
        name=p.name,
        timeline=p.timeline,
        materials=p.materials,
        revision=p.revision,
        createdAt=p.created_at.isoformat() + "Z",
        updatedAt=p.updated_at.isoformat() + "Z"
    )

@app.put("/projects/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, req: UpdateProjectRequest, db: Session = Depends(get_db)):
    p = db.query(DBProject).filter(DBProject.id == project_id).first()
    if not p:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "PROJECT_NOT_FOUND",
                "message": f"Project {project_id} not found"
            }
        )

    # Check for concurrency conflict
    if p.revision != req.expectedRevision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CONCURRENCY_CONFLICT",
                "message": f"Revision conflict: requested revision {req.expectedRevision} but current is {p.revision}"
            }
        )

    # Apply changes
    if req.name is not None:
        p.name = req.name
    if req.timeline is not None:
        # Pydantic dict serialization
        p.timeline = req.timeline.model_dump()
    if req.materials is not None:
        p.materials = [m.model_dump() for m in req.materials]

    p.revision += 1
    p.updated_at = datetime.datetime.utcnow()

    db.commit()
    db.refresh(p)

    return ProjectResponse(
        id=p.id,
        name=p.name,
        timeline=p.timeline,
        materials=p.materials,
        revision=p.revision,
        createdAt=p.created_at.isoformat() + "Z",
        updatedAt=p.updated_at.isoformat() + "Z"
    )

@app.post("/projects/{project_id}/render")
async def start_render_task(project_id: str, db: Session = Depends(get_db)):
    p = db.query(DBProject).filter(DBProject.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    task_id = str(uuid.uuid4())
    active_tasks[task_id] = {
        "taskId": task_id,
        "projectId": project_id,
        "progress": 0,
        "status": "pending",
        "message": "Initializing background render task"
    }

    # Simulate work asynchronously
    async def simulate_task():
        try:
            await asyncio.sleep(2)
            active_tasks[task_id]["status"] = "processing"
            active_tasks[task_id]["progress"] = 20
            active_tasks[task_id]["message"] = "Processing timeline frames..."

            await asyncio.sleep(2)
            active_tasks[task_id]["progress"] = 50
            active_tasks[task_id]["message"] = "Generating 480p proxy with FFmpeg..."

            await asyncio.sleep(2)
            active_tasks[task_id]["progress"] = 80
            active_tasks[task_id]["message"] = "Merging audio layers..."

            await asyncio.sleep(2)
            active_tasks[task_id]["progress"] = 100
            active_tasks[task_id]["status"] = "completed"
            active_tasks[task_id]["message"] = "Render successfully completed"
        except Exception as e:
            active_tasks[task_id]["status"] = "failed"
            active_tasks[task_id]["message"] = f"Task failed: {str(e)}"

    asyncio.create_task(simulate_task())

    return {"taskId": task_id, "status": "pending"}

@app.get("/events")
async def sse_events():
    async def event_generator():
        while True:
            # Send heartbeat
            yield f"event: heartbeat\ndata: {{\"timestamp\": \"{datetime.datetime.utcnow().isoformat()}Z\"}}\n\n"

            # Send active tasks status updates
            for task_id, task in list(active_tasks.items()):
                yield f"event: task_progress\ndata: {{\n" \
                      f"  \"taskId\": \"{task['taskId']}\",\n" \
                      f"  \"projectId\": \"{task['projectId']}\",\n" \
                      f"  \"progress\": {task['progress']},\n" \
                      f"  \"status\": \"{task['status']}\",\n" \
                      f"  \"message\": \"{task['message']}\"\n" \
                      f"}}\n\n"

                # Clean up finished tasks from active streaming list after completion/failure
                if task["status"] in ["completed", "failed"]:
                    active_tasks.pop(task_id, None)

            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
