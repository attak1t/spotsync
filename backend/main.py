import asyncio
import json
import time
import uuid
import redis
from typing import List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import bcrypt
from sqlalchemy import text
from sqlalchemy.orm import Session
from pydantic import BaseModel

from backend.config import settings
from backend.database import get_db, create_tables
from backend.models import Job, Track
from backend.spotify import SpotifyClient
from backend.tasks import download_track, redis_client as tasks_redis_client

# Create tables on startup
create_tables()

app = FastAPI(title="SpotSync", version="1.0.0")

# Mount frontend static files
app.mount("/static", StaticFiles(directory="frontend"), name="static")

# Templates for login page
templates = Jinja2Templates(directory="frontend")

# Security
security = HTTPBasic()

# Pydantic models
class JobCreate(BaseModel):
    query: str

class JobResponse(BaseModel):
    id: str
    created_at: str
    spotify_url: str
    playlist_name: Optional[str]
    status: str
    track_count: int

class TrackResponse(BaseModel):
    id: str
    spotify_id: str
    title: str
    artist: str
    album: str
    status: str
    file_path: Optional[str]
    error_message: Optional[str]
    lidarr_import_status: Optional[str]

class JobDetailResponse(JobResponse):
    tracks: List[TrackResponse]

# Authentication
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode(), hashed_password.encode())

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

# Create admin password hash on first run
ADMIN_PASSWORD_HASH = get_password_hash(settings.ADMIN_PASSWORD)

def authenticate(credentials: HTTPBasicCredentials):
    if credentials.username != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not verify_password(credentials.password, ADMIN_PASSWORD_HASH):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# Dependency for authenticated requests
def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    return authenticate(credentials)

# Routes
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Serve the frontend SPA."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/auth/login")
async def login(credentials: HTTPBasicCredentials = Depends(security)):
    """Basic auth login endpoint."""
    authenticate(credentials)
    return {"message": "Login successful"}

# Job endpoints
@app.post("/api/jobs", response_model=JobResponse)
async def create_job(
    job_data: JobCreate,
    db: Session = Depends(get_db),
    current_user: str = Depends(get_current_user)
):
    """Submit a new Spotify URL for download."""
    spotify = SpotifyClient()
    query_str = job_data.query.strip()

    try:
        # Determine if input is a Spotify URL
        is_spotify_url = "open.spotify.com" in query_str

        if is_spotify_url:
            # Spotify URL handling
            if spotify.is_configured():
                playlist_name, tracks = spotify.resolve_url(query_str)
                # Parse URL type to add to each track
                import re
                patterns = {
                    "track": r"https://open\.spotify\.com/track/([a-zA-Z0-9]+)",
                    "album": r"https://open\.spotify\.com/album/([a-zA-Z0-9]+)",
                    "playlist": r"https://open\.spotify\.com/playlist/([a-zA-Z0-9]+)",
                }
                url_type = None
                for pattern_type, pattern in patterns.items():
                    if re.search(pattern, query_str):
                        url_type = pattern_type
                        break

                # Add url_type and original_url to each track
                for track in tracks:
                    track["url_type"] = url_type or "track"
                    track["original_url"] = f"https://open.spotify.com/track/{track['id']}"
            else:
                # Without Spotify API, we can still handle any Spotify URL
                # but we won't have metadata until spotdl processes it
                import re
                # Parse URL type
                patterns = {
                    "track": r"https://open\.spotify\.com/track/([a-zA-Z0-9]+)",
                    "album": r"https://open\.spotify\.com/album/([a-zA-Z0-9]+)",
                    "playlist": r"https://open\.spotify\.com/playlist/([a-zA-Z0-9]+)",
                }

                url_type = None
                obj_id = None
                for pattern_type, pattern in patterns.items():
                    match = re.search(pattern, query_str)
                    if match:
                        url_type = pattern_type
                        obj_id = match.group(1)
                        break

                if not url_type:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid Spotify URL. Supported: track, album, playlist"
                    )

                # Set playlist_name for UI display
                playlist_name = f"{url_type.capitalize()}: {obj_id}" if url_type in ["playlist", "album"] else None
                # Create a single placeholder track with the original URL
                # For playlists/albums, spotdl will handle downloading all tracks
                tracks = [{
                    "id": obj_id,
                    "title": f"{url_type.capitalize()}: {obj_id}",
                    "artist": "",
                    "album": "",
                    "duration_ms": 0,
                    "track_number": 1,
                    "disc_number": 1,
                    "url_type": url_type,  # Store URL type for download logic
                    "original_url": query_str,  # Store original URL for spotdl
                }]
        else:
            # Search queries are not supported (Spotify API is no longer free)
            raise HTTPException(
                status_code=400,
                detail="Search queries are not supported. Please use Spotify URLs (track, playlist, or album links)."
            )

        # Create job
        job = Job(
            spotify_url=query_str,  # Store either URL or query
            playlist_name=playlist_name,
            track_count=len(tracks),
            status="pending"
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        # Create track records
        for track_data in tracks:
            track = Track(
                job_id=job.id,
                spotify_id=track_data["id"],
                title=track_data["title"],
                artist=track_data["artist"],
                album=track_data["album"],
                duration_ms=track_data["duration_ms"],
                status="queued",
                url_type=track_data.get("url_type", "track"),
                original_url=track_data.get("original_url", f"https://open.spotify.com/track/{track_data['id']}")
            )
            db.add(track)

        db.commit()

        # Start downloading tracks
        for track in job.tracks:
            download_track.delay(track.id)

        job.status = "running"
        db.commit()

        return JobResponse(
            id=job.id,
            created_at=job.created_at.isoformat(),
            spotify_url=job.spotify_url,
            playlist_name=job.playlist_name,
            status=job.status,
            track_count=job.track_count
        )

    except HTTPException:
        # Re-raise HTTP exceptions (e.g., auth errors)
        raise
    except ValueError as e:
        print(f"ERROR in create_job (ValueError): {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"ERROR in create_job (Exception): {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"Error creating job: {str(e)}")

@app.get("/api/jobs", response_model=List[JobResponse])
async def list_jobs(
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: str = Depends(get_current_user)
):
    """List all jobs."""
    jobs = db.query(Job).order_by(Job.created_at.desc()).offset(skip).limit(limit).all()
    return [
        JobResponse(
            id=job.id,
            created_at=job.created_at.isoformat(),
            spotify_url=job.spotify_url,
            playlist_name=job.playlist_name,
            status=job.status,
            track_count=job.track_count
        )
        for job in jobs
    ]

@app.get("/api/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: str = Depends(get_current_user)
):
    """Get job details with tracks."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobDetailResponse(
        id=job.id,
        created_at=job.created_at.isoformat(),
        spotify_url=job.spotify_url,
        playlist_name=job.playlist_name,
        status=job.status,
        track_count=job.track_count,
        tracks=[
            TrackResponse(
                id=track.id,
                spotify_id=track.spotify_id,
                title=track.title,
                artist=track.artist,
                album=track.album,
                status=track.status,
                file_path=track.file_path,
                error_message=track.error_message,
                lidarr_import_status=track.lidarr_import_status
            )
            for track in job.tracks
        ]
    )

@app.post("/api/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: str = Depends(get_current_user)
):
    """Retry failed tracks in a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    failed_tracks = db.query(Track).filter(
        Track.job_id == job_id,
        Track.status == "failed"
    ).all()

    for track in failed_tracks:
        track.status = "queued"
        track.error_message = None
        download_track.delay(track.id)

    db.commit()

    return {"message": f"Retrying {len(failed_tracks)} failed tracks"}

@app.delete("/api/jobs/{job_id}")
async def delete_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: str = Depends(get_current_user)
):
    """Delete a job and its associated tracks."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Revoke any active Celery tasks for this job's tracks
    from backend.tasks import celery_app
    for track in job.tracks:
        if track.celery_task_id:
            celery_app.control.revoke(track.celery_task_id, terminate=True)

    # Delete job (cascade will delete tracks due to relationship)
    db.delete(job)
    db.commit()

    return {"message": "Job deleted successfully"}

# WebSocket endpoint for live progress
@app.websocket("/ws/jobs/{job_id}")
async def websocket_job_progress(websocket: WebSocket, job_id: str):
    """WebSocket endpoint for real-time job progress."""
    await websocket.accept()

    # Verify job exists
    db = next(get_db())
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        await websocket.close(code=1008, reason="Job not found")
        return

    # Subscribe to Redis pub/sub channel for this job
    pubsub = tasks_redis_client.pubsub()
    pubsub.subscribe(f"ws:job:{job_id}")

    try:
        # Send initial state
        job_data = await get_job(job_id, db, "admin")  # Simplified auth for WebSocket
        await websocket.send_json({
            "event": "initial_state",
            "job": job_data.dict()
        })

        # Forward pub/sub messages to the WebSocket client.
        # Run the blocking Redis get_message in a thread pool so it doesn't
        # stall the event loop, and detect client disconnection concurrently.
        loop = asyncio.get_event_loop()

        async def pubsub_forwarder():
            while True:
                message = await loop.run_in_executor(
                    None,
                    lambda: pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                )
                if message and message["type"] == "message":
                    try:
                        event_data = json.loads(message["data"])
                        await websocket.send_json(event_data)
                    except (json.JSONDecodeError, Exception):
                        pass

        async def disconnect_waiter():
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass

        pubsub_task = asyncio.create_task(pubsub_forwarder())
        disconnect_task = asyncio.create_task(disconnect_waiter())
        try:
            done, pending = await asyncio.wait(
                {pubsub_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (pubsub_task, disconnect_task):
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    except WebSocketDisconnect:
        pass
    finally:
        pubsub.close()
        db.close()

# Health checks
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": time.time()}

@app.get("/health/db")
async def health_check_db(db: Session = Depends(get_db)):
    """Database health check."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8222)