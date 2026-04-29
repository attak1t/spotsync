# SpotSync

Self-hosted Spotify → spotdl → Lidarr → Syncthing pipeline with a web GUI.

## Overview

SpotSync is a Dockerized web application that lets you paste a Spotify playlist, album, or track URL into a browser, download the audio via `spotdl`, import the result into Lidarr, and sync the files to your Android device (OuterTune / Auxio) via Syncthing — all with live progress feedback in the browser.

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
/music (shared bind mount, maps to /storage/media/Music on host)
  ├──► Lidarr   (import via REST API)
  └──► Syncthing (watches directory, syncs to device)
```

## Quick Start

### Local Testing (Mac/Linux/Windows)

For initial testing on your local machine:

```bash
git clone <repository-url>
cd spotsync
cp .env.example .env
```

Edit `.env` and fill in:
- **Lidarr URL**: If testing without Lidarr, leave API key blank (tracks won't be imported)
- **Syncthing URL**: If testing without Syncthing, leave API key blank (no automatic sync)
- **Spotify API credentials**: Optional, for playlist/album metadata
- **Admin password**: Change from default "changeme"

For Mac testing with host services:
- `LIDARR_URL=http://host.docker.internal:8686`
- `SYNCTHING_URL=http://host.docker.internal:8384`

Start SpotSync only (requires Docker):
```bash
docker compose up -d api worker redis
```

Access the web interface at `http://localhost:8222` (admin/your_password).

### Server Deployment (with existing Lidarr/Syncthing)

For production deployment alongside existing services:

1. **Clone and configure**:
   ```bash
   git clone <repository-url>
   cd spotsync
   cp .env.example .env
   ```

2. **Edit `.env`**:
   - `LIDARR_URL`: Point to existing Lidarr (e.g., `http://lidarr:8686` if in same Docker network)
   - `LIDARR_API_KEY`: From Lidarr Settings > General
   - `SYNCTHING_URL`: Point to existing Syncthing
   - `SYNCTHING_API_KEY`: From Syncthing Settings > Actions > Show API Key
   - `ADMIN_PASSWORD`: Change from default
   - Optional: Spotify API credentials for metadata

3. **Update volume mounts** in `docker-compose.yml`:
   Change `/storage/media/Chris/Music:/music` to your music library path
   and `./data:/data` to a persistent location for the SQLite database

4. **Start services**:
   ```bash
   docker compose up -d
   ```

5. **Configure Syncthing**:
   - Ensure Syncthing is monitoring your music library folder
   - The folder ID in Syncthing should match `SYNCTHING_FOLDER_ID` (default: "music")

6. **Access SpotSync**:
   Open `http://your-server:8222` and login

## Features

- **Web Interface**: Single-page app with login, dashboard, and job submission
- **Live Progress**: WebSocket updates for download and import progress
- **Spotify Support**: Playlist, album, and single track URLs
- **Metadata**: Optional Spotify API integration for track metadata
- **Lidarr Integration**: Automatic import with manual import API
- **Syncthing Sync**: Automatic folder rescan after import
- **Retry Logic**: Automatic retry for failed downloads
- **Job History**: View past jobs and track status

## Environment Variables

See `.env.example` for all available options. Key variables:

| Variable | Description | Required |
|----------|-------------|----------|
| `DATABASE_URL` | SQLite database path (absolute path: `/data/spotsync.db`) | No |
| `LIDARR_URL` | Lidarr server URL | No |
| `LIDARR_API_KEY` | Lidarr API key | No (skip import if blank) |
| `SYNCTHING_URL` | Syncthing server URL | No |
| `SYNCTHING_API_KEY` | Syncthing API key | No (skip sync if blank) |
| `SYNCTHING_FOLDER_ID` | Syncthing folder to rescan (default: "music") | No |
| `SPOTIFY_CLIENT_ID` | Spotify API client ID | No (optional for metadata) |
| `SPOTIFY_CLIENT_SECRET` | Spotify API client secret | No (optional for metadata) |
| `ADMIN_PASSWORD` | Web interface password | Yes |
| `REDIS_URL` | Redis connection URL | No |
| `SECRET_KEY` | Session secret key | Yes |
| `SPOTDL_OUTPUT` | spotdl file output template | No |
| `SPOTDL_FORMAT` | Audio format (mp3, flac, etc.) | No |
| `SPOTDL_BITRATE` | Audio bitrate | No |

## API Endpoints

- `POST /api/jobs` - Submit a Spotify URL
- `GET /api/jobs` - List all jobs
- `GET /api/jobs/{job_id}` - Get job details with tracks
- `POST /api/jobs/{job_id}/retry` - Retry failed tracks
- `WS /ws/jobs/{job_id}` - WebSocket for live progress

## Development

### Project Structure

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
└── requirements.txt
```

### Running Tests

```bash
python test_pipeline.py "https://open.spotify.com/track/..."
```

### Useful Commands

```bash
# Start everything
docker compose up -d

# View logs
docker compose logs -f

# Rebuild after code changes
docker compose up -d --build api worker

# Check Celery queue
docker compose exec worker celery -A backend.tasks inspect active

# Run spotdl manually for testing (uses /music volume mount)
docker compose exec worker spotdl "https://open.spotify.com/track/..." --output /music/test
```

## Known Issues & Solutions

### SQLite "unable to open database file"
The `DATABASE_URL` must use an absolute path (`sqlite:////data/spotsync.db`) matching the `/data` volume mount. The Docker image includes `mkdir -p /data` to ensure the directory exists. If you encounter this error, verify your `DATABASE_URL` is an absolute path and that the host `./data` directory is writable.

### spotdl Output Parsing
spotdl's stdout format changes across versions. The current implementation uses a simple progress estimator. For better parsing, consider contributing a more robust parser.

### Partial Album Imports in Lidarr
Always uses Lidarr's Manual Import API with `disableReleaseSwitching: true` to allow individual track imports.

### Spotify API Credentials
Optional. Without credentials, only single track URLs are supported and metadata won't be available until after download.

## License

MIT

## Acknowledgments

- [spotdl](https://github.com/spotDL/spotify-downloader) for Spotify to audio conversion
- [Lidarr](https://lidarr.audio/) for music library management
- [Syncthing](https://syncthing.net/) for file synchronization
- [FastAPI](https://fastapi.tiangolo.com/) and [Celery](https://docs.celeryq.dev/) for the backend