import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=datetime.utcnow)
    spotify_url = Column(String, nullable=False)
    playlist_name = Column(String)
    status = Column(String, default="pending")  # pending|running|done|failed
    track_count = Column(Integer, default=0)
    tracks = relationship("Track", back_populates="job", cascade="all, delete-orphan")

class Track(Base):
    __tablename__ = "tracks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id = Column(String, ForeignKey("jobs.id"))
    spotify_id = Column(String)
    title = Column(String)
    artist = Column(String)
    album = Column(String)
    duration_ms = Column(Integer)
    status = Column(String, default="queued")  # queued | downloading | done | failed | imported
    file_path = Column(String)
    error_message = Column(String)
    lidarr_import_status = Column(String)
    celery_task_id = Column(String)
    url_type = Column(String)  # track, album, playlist
    original_url = Column(String)  # Original Spotify URL for playlists/albums
    job = relationship("Job", back_populates="tracks")