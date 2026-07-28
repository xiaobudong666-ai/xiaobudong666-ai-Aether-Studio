import datetime
from sqlalchemy import Column, String, Integer, DateTime, JSON
from .database import Base

class DBProject(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    timeline = Column(JSON, nullable=False)
    materials = Column(JSON, nullable=False)
    revision = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, nullable=False)
