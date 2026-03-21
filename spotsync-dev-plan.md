# SpotSync — Development Plan
> Self-hosted Spotify → spotdl → Lidarr → Syncthing pipeline with a web GUI

---

## What We're Building

A Dockerised web application that lets you paste a Spotify playlist, album, or track URL into a browser, download the audio via `spotdl`, import the result into Lidarr, and sync the files to your Android device (OuterTune / Auxio) via Syncthing — all with live progress feedback in the browser.

---

## Tech Stack

| Layer | Technology | Reason |
|---|---|---|
| Backend API | Python 3.12 + FastAPI | spotdl is Python-native; FastAPI has native WebSocket + async |
| Task queue | Celery 5 | Long-running downloads need a real queue with retries |
| Broker / backend | Redis 7 (Alpine) | Lightweight; Celery default |
| Database | SQLite via SQLAlchemy 2 | Single-file, no extra container, plenty for this workload |
| Auth | Session cookies + bcrypt | Single admin user; self-hosted |
| Frontend | Vanilla JS (single HTML file) | No build step; served by FastAPI |
| Containerisation | Docker Compose | All services declared in one file |
| Music library | Lidarr (LinuxServer image) | Metadata, artwork, library management |
| Sync | Syncthing (LinuxServer image) | P2P sync to Android |
| Downloader | spotdl (pinned version) | Spotify → audio |

---

## Architecture

```
Browser
  │  HTTP (login, job submit)
  │  WebSocket (live progress)
  ▼
FastAPI  ──read/write──►  SQLite
  │
  │  enqueue task
  ▼
Redis ◄──────────────────────────┐
  │                              │
  │  dequeue                     │ status update
  ▼                              │
Celery Worker ─────────────────►─┘
  │  shells out to spotdl
  │  writes files
  ▼
/music (shared bind mount)
  ├──► Lidarr   (import via REST API)
  └──► Syncthing (watches directory, syncs to device)
```

All five services share the same `./music` host directory via Docker bind mount. No copying between containers.

---

## Project Structure

```
spotsync/
├── backend/
│   ├── main.py            # FastAPI app, auth, WebSocket endpoint
│   ├── tasks.py           # Celery tasks (download + Lidarr import)
│   ├── spotify.py         # Spotify Web API client (playlist resolution)
│   ├── lidarr.py          # Lidarr REST API client
│   ├── syncthing.py       # Syncthing REST API client
│   ├── models.py          # SQLAlchemy models (Job, Track)
│   ├── database.py        # DB session setup
│   └── config.py          # Settings from environment variables
├── frontend/
│   ├── index.html         # SPA shell (login + dashboard)
│   ├── app.js             # WebSocket client, job submission, UI logic
│   └── style.css
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Data Models

### Job

Represents one submitted URL (playlist, album, or track).

```python
class Job(Base):
    __tablename__ = "jobs"

    id            = Column(String, primary_key=True, default=lambda: str(uuid4()))
    created_at    = Column(DateTime, default=datetime.utcnow)
    spotify_url   = Column(String, nullable=False)
    playlist_name = Column(String)
    status        = Column(String, default="pending")  # pending|running|done|failed
    track_count   = Column(Integer, default=0)
    tracks        = relationship("Track", back_populates="job")
```

### Track

Represents one individual song within a Job.

```python
class Track(Base):
    __tablename__ = "tracks"

    id                  = Column(String, primary_key=True, default=lambda: str(uuid4()))
    job_id              = Column(String, ForeignKey("jobs.id"))
    spotify_id          = Column(String)
    title               = Column(String)
    artist              = Column(String)
    album               = Column(String)
    duration_ms         = Column(Integer)
    status              = Column(String, default="queued")
    # queued | downloading | done | failed | imported
    file_path           = Column(String)
    error_message       = Column(String)
    lidarr_import_status = Column(String)
    celery_task_id      = Column(String)
    job                 = relationship("Job", back_populates="tracks")
```

---

## API Endpoints

### Auth

| Method | Path | Description |
|---|---|---|
| `POST` | `/auth/login` | Form login, sets session cookie |
| `POST` | `/auth/logout` | Clears session |

### Jobs

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs` | Submit a Spotify URL, returns `job_id` |
| `GET` | `/api/jobs` | List all jobs (paginated) |
| `GET` | `/api/jobs/{job_id}` | Get job + track status |
| `POST` | `/api/jobs/{job_id}/retry` | Re-queue failed tracks |

### WebSocket

| Path | Description |
|---|---|
| `WS /ws/jobs/{job_id}` | Streams `{ track_id, status, percent, error? }` events |

### Settings

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/settings` | Read current config |
| `PUT` | `/api/settings` | Update Lidarr/Syncthing credentials, spotdl defaults |

---

## WebSocket Event Schema

```json
{
  "event": "track_update",
  "track_id": "abc123",
  "status": "downloading",
  "percent": 47,
  "title": "Song Name",
  "artist": "Artist Name",
  "error": null
}
```

Other event types: `job_complete`, `import_done`, `sync_triggered`.

---

## Celery Task Flow

```python
# tasks.py (pseudocode)

@celery.task(bind=True, max_retries=3)
def download_track(self, track_id: str):
    track = db.get(Track, track_id)
    emit_ws(track.job_id, track_id, "downloading")

    try:
        result = subprocess.run(
            ["spotdl", track.spotify_url, "--output", "/music/{artist}/{album}"],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)

        track.status = "done"
        track.file_path = parse_output_path(result.stdout)
        db.commit()
        emit_ws(track.job_id, track_id, "done")

        import_to_lidarr.delay(track_id)

    except Exception as exc:
        track.status = "failed"
        track.error_message = str(exc)
        db.commit()
        emit_ws(track.job_id, track_id, "failed", error=str(exc))
        raise self.retry(exc=exc, countdown=30)


@celery.task
def import_to_lidarr(track_id: str):
    track = db.get(Track, track_id)
    lidarr = LidarrClient()
    # Use manual import endpoint — works for partial albums
    lidarr.manual_import(track.file_path, disable_release_switching=True)
    track.lidarr_import_status = "imported"
    db.commit()
    emit_ws(track.job_id, track_id, "imported")
    syncthing.trigger_rescan("/music")
```

---

## Spotify Integration

SpotSync uses the **Spotify Web API with client credentials flow** (no user login required, read-only metadata).

```python
# spotify.py
import httpx

class SpotifyClient:
    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_URL   = "https://api.spotify.com/v1"

    def get_token(self):
        r = httpx.post(self.TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": settings.SPOTIFY_CLIENT_ID,
            "client_secret": settings.SPOTIFY_CLIENT_SECRET,
        })
        return r.json()["access_token"]

    def resolve_url(self, spotify_url: str) -> dict:
        """Returns playlist name + list of track dicts."""
        # Handles /playlist/, /album/, /track/ URLs
        ...
```

You need a free Spotify Developer account and a registered app at developer.spotify.com to get `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

---

## Lidarr Integration

Use Lidarr's **Manual Import API** rather than the automatic scan — this handles partial albums correctly.

```python
# lidarr.py
import httpx

class LidarrClient:
    def __init__(self):
        self.base = settings.LIDARR_URL
        self.headers = {"X-Api-Key": settings.LIDARR_API_KEY}

    def manual_import(self, file_path: str, disable_release_switching=True):
        payload = {
            "path": file_path,
            "importMode": "Move",          # or "Copy"
            "disableReleaseSwitching": disable_release_switching,
        }
        r = httpx.post(
            f"{self.base}/api/v1/manualimport",
            json=payload,
            headers=self.headers
        )
        r.raise_for_status()
        return r.json()

    def trigger_scan(self):
        r = httpx.post(
            f"{self.base}/api/v1/command",
            json={"name": "RescanFolders"},
            headers=self.headers
        )
        r.raise_for_status()
```

**Key detail:** `disableReleaseSwitching: true` prevents Lidarr from rejecting tracks it doesn't recognise as part of a complete release.

---

## Syncthing Integration

Thin fire-and-forget layer. Just trigger a folder rescan after each import.

```python
# syncthing.py
import httpx

class SyncthingClient:
    def __init__(self):
        self.base = settings.SYNCTHING_URL   # e.g. http://syncthing:8384
        self.headers = {"X-API-Key": settings.SYNCTHING_API_KEY}

    def trigger_rescan(self, folder_id: str = "music"):
        httpx.post(
            f"{self.base}/rest/db/scan",
            params={"folder": folder_id},
            headers=self.headers
        )
```

Syncthing's folder ID is configured once in its own UI. The `folder_id` defaults to `"music"` — match whatever you name your folder in Syncthing.

---

## Docker Compose

```yaml
# docker-compose.yml
services:

  api:
    build: .
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data          # SQLite + session store
      - ./music:/music        # shared music library
    env_file: .env
    depends_on:
      - redis
    command: uvicorn backend.main:app --host 0.0.0.0 --port 8080

  worker:
    build: .
    volumes:
      - ./data:/data
      - ./music:/music
    env_file: .env
    depends_on:
      - redis
    command: celery -A backend.tasks worker --loglevel=info --concurrency=2

  redis:
    image: redis:7-alpine
    volumes:
      - redis-data:/data

  lidarr:
    image: lscr.io/linuxserver/lidarr:latest
    ports:
      - "8686:8686"
    volumes:
      - ./lidarr-config:/config
      - ./music:/music
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=UTC

  syncthing:
    image: lscr.io/linuxserver/syncthing:latest
    ports:
      - "8384:8384"     # Web UI
      - "22000:22000"   # Sync protocol (TCP)
      - "22000:22000/udp"
    volumes:
      - ./syncthing-config:/config
      - ./music:/music
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=UTC

volumes:
  redis-data:
```

---

## Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Install spotdl system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default command (overridden in docker-compose for worker)
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

---

## requirements.txt

```
fastapi==0.115.0
uvicorn[standard]==0.30.0
celery==5.4.0
redis==5.0.8
sqlalchemy==2.0.35
httpx==0.27.2
bcrypt==4.2.0
python-multipart==0.0.12
itsdangerous==2.2.0          # session signing
spotdl==4.2.10               # PIN this version
pydantic-settings==2.5.2
```

---

## Environment Variables (.env.example)

```env
# Spotify API (get from developer.spotify.com)
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here

# Lidarr
LIDARR_URL=http://lidarr:8686
LIDARR_API_KEY=your_lidarr_api_key_here

# Syncthing
SYNCTHING_URL=http://syncthing:8384
SYNCTHING_API_KEY=your_syncthing_api_key_here
SYNCTHING_FOLDER_ID=music

# Auth (change before first run)
ADMIN_PASSWORD=changeme

# Redis
REDIS_URL=redis://redis:6379/0

# spotdl output format
SPOTDL_OUTPUT=/music/{artist}/{album}/{title}
SPOTDL_FORMAT=mp3
SPOTDL_BITRATE=320k

# App
SECRET_KEY=change_this_to_a_random_string
```

---

## Frontend Behaviour

The entire frontend is a single HTML file (`frontend/index.html`) served by FastAPI.

**Login page:** Simple username/password form. On success, redirects to dashboard.

**Dashboard:**
- "New Download" button opens a modal with a text field for the Spotify URL
- Active jobs list with per-track progress bars, status badges, and ETA
- Completed jobs list with Lidarr import status and Syncthing sync status
- Failed tracks show the error and a Retry button

**WebSocket client (app.js):**
```javascript
function connectJob(jobId) {
  const ws = new WebSocket(`ws://${location.host}/ws/jobs/${jobId}`);

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    updateTrackRow(msg.track_id, msg.status, msg.percent, msg.error);
  };

  ws.onclose = () => {
    // Reconnect with exponential backoff
    setTimeout(() => connectJob(jobId), 2000);
  };
}
```

---

## Build Phases

### Phase 1 — Core Pipeline (no GUI, CLI only)
Goal: Spotify URL in → spotdl runs → file on disk → Lidarr import triggered.

- `spotify.py` — resolve playlist URL to track list
- `tasks.py` — shell out to spotdl, parse output path
- `lidarr.py` — manual import call
- Test with `celery -A backend.tasks call download_track --args='["<track_id>"]'`

### Phase 2 — API + Queue
Goal: Submit jobs via HTTP, watch them run.

- `models.py` + `database.py` — SQLite schema
- `main.py` — FastAPI app, `/api/jobs` endpoints, auth middleware
- Verify with `curl -X POST /api/jobs -d '{"url": "..."}'`

### Phase 3 — WebSocket Progress Feed
Goal: Browser receives live events.

- WebSocket endpoint in `main.py`
- `emit_ws()` helper that publishes to a Redis pub/sub channel; WebSocket handler subscribes
- Test with a browser console WebSocket client

### Phase 4 — Web GUI
Goal: Usable browser interface.

- `frontend/index.html` — login page + dashboard SPA
- `frontend/app.js` — job submission, WebSocket consumer, DOM updates
- Serve static files from FastAPI with `StaticFiles`

### Phase 5 — Syncthing + Polish
Goal: End-to-end automated, production-ready.

- `syncthing.py` — rescan trigger after import
- Retry logic for failed tracks (Celery `max_retries`)
- Settings page (Lidarr/Syncthing credentials, spotdl quality)
- Docker Compose healthchecks
- Optional: ntfy.sh push notifications on job completion

---

## Known Pain Points

### spotdl output parsing
spotdl's stdout format changes across versions. **Pin the version in requirements.txt** and write a dedicated parser for that version's output. Consider running spotdl with `--log-level DEBUG` to get structured output, or parse the final line which typically contains the output file path.

### Partial album imports in Lidarr
Lidarr's automatic scan (`DownloadedAlbumsScan`) expects complete albums. Always use the **Manual Import API** (`/api/v1/manualimport`) with `disableReleaseSwitching: true`. This allows individual tracks to be imported regardless of whether the full album is present.

### Shared file path contract
Both the Celery worker container and the Lidarr container must refer to `/music` as the same physical path. The bind mount in `docker-compose.yml` handles this — never use container-internal paths or environment variables that differ between services.

### spotdl and Spotify credentials
spotdl needs `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` at runtime. Pass them as environment variables — spotdl reads them from the environment automatically in recent versions.

### WebSocket reconnection
Celery tasks can take minutes. The browser WebSocket must handle disconnects gracefully (tab backgrounded, mobile screen off). Implement exponential backoff reconnection in `app.js`, and on reconnect fetch the current job state via `GET /api/jobs/{job_id}` to catch up on any events missed.

### ffmpeg in Docker
spotdl depends on `ffmpeg` for audio conversion. It must be installed in the same container as the Celery worker. The Dockerfile above includes the `apt-get install ffmpeg` step.

---

## Claude Code Prompts to Get Started

Use these prompts sequentially in a Claude Code session to scaffold the project:

```
1. "Create the project structure for SpotSync as described in spotsync-dev-plan.md.
   Initialise the directories and create empty placeholder files."

2. "Implement backend/models.py and backend/database.py using SQLAlchemy 2
   with the Job and Track models from the dev plan."

3. "Implement backend/config.py using pydantic-settings to load all
   environment variables from .env.example."

4. "Implement backend/spotify.py — a Spotify client credentials auth flow
   that resolves playlist, album, and track URLs to a list of track dicts."

5. "Implement backend/lidarr.py — a Lidarr API client with manual_import()
   and trigger_scan() methods."

6. "Implement backend/tasks.py — Celery tasks for download_track and
   import_to_lidarr, including WebSocket event emission via Redis pub/sub."

7. "Implement backend/main.py — FastAPI app with session auth, job submission
   endpoint, WebSocket progress endpoint, and static file serving."

8. "Implement the frontend (index.html, app.js, style.css) — a single-page
   app with login, dashboard, job submission modal, and live WebSocket
   progress updates."

9. "Write the Dockerfile and docker-compose.yml as specified in the dev plan."

10. "Write a smoke test script (test_pipeline.py) that submits a single
    Spotify track URL to the API and polls until the job completes."
```

---

## First-Run Checklist

- [ ] Copy `.env.example` to `.env` and fill in all values
- [ ] Create a Spotify Developer app at developer.spotify.com and copy client ID + secret
- [ ] Run `docker compose up -d`
- [ ] Open Lidarr at `http://localhost:8686`, complete setup wizard, copy API key to `.env`
- [ ] Open Syncthing at `http://localhost:8384`, add the `/music` folder, pair with your device, copy API key to `.env`
- [ ] Restart containers: `docker compose restart api worker`
- [ ] Open SpotSync at `http://localhost:8080` and paste a Spotify URL

---

## Useful Commands

```bash
# Start everything
docker compose up -d

# Tail logs for all services
docker compose logs -f

# Tail only the worker (where spotdl runs)
docker compose logs -f worker

# Rebuild after code changes
docker compose up -d --build api worker

# Open a shell in the worker container
docker compose exec worker bash

# Run spotdl manually inside the worker for testing
docker compose exec worker spotdl "https://open.spotify.com/track/..." --output /music/test

# Check Celery queue
docker compose exec worker celery -A backend.tasks inspect active

# Wipe SQLite and start fresh (destructive)
rm data/spotsync.db && docker compose restart api worker
```