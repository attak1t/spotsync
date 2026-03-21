# SpotSync — Project History & Bug Tracker

## Project Overview

Self-hosted Spotify → spotdl → Lidarr → Syncthing pipeline with a web GUI.
Built in Python (FastAPI + Celery + SQLite), served from Docker Compose.

---

## Build History

### v0.1 — Initial Scaffolding
Phases 1–4 of the dev plan implemented:
- `backend/models.py`, `backend/database.py` — SQLAlchemy 2 Job/Track models
- `backend/config.py` — pydantic-settings environment loading
- `backend/spotify.py` — Spotify client credentials auth + URL resolver
- `backend/lidarr.py` — Manual Import API client
- `backend/syncthing.py` — Rescan trigger client
- `backend/tasks.py` — Celery tasks: `download_track`, `import_to_lidarr`
- `backend/main.py` — FastAPI app: auth, job CRUD, WebSocket progress feed
- `frontend/index.html`, `frontend/app.js`, `frontend/style.css` — SPA
- `Dockerfile`, `docker-compose.yml`, `requirements.txt` — containerisation
- `.env.example`, `README.md`, `spotsync-dev-plan.md`
- `test_pipeline.py` — smoke test

### v0.3 — Bug fixes
- **BUG-001** fixed: removed debug `print()` statements leaking auth credentials in `main.py`
- **BUG-002** fixed: moved `logger` declaration before `parse_audio_metadata` in `tasks.py`
- **BUG-003** fixed: rewrote WebSocket handler to forward pub/sub events concurrently with disconnect detection using `asyncio` tasks; also runs blocking `get_message()` in thread pool executor (addresses BUG-004 side-effect)
- **BUG-005** fixed: renamed shadowed `track_id` inner variable to `child_spotify_id` in playlist loop
- **BUG-006** fixed: added `db.close()` before session reassignment in exception handler
- **BUG-008** fixed: wrapped raw SQL string in `text()` for SQLAlchemy 2 compatibility; added `from sqlalchemy import text` import
- **BUG-009** fixed: guarded `os.makedirs` with walrus-operator check for empty dirname
- **BUG-013** fixed: removed unused `HttpUrl` import from `main.py`
- **BUG-015** fixed: removed three `DEBUG` print statements from `spotify.py`
- **BUG-004** addressed: `get_message()` now runs in a thread pool executor (via BUG-003 fix), so the synchronous Redis client no longer blocks the async event loop
- **BUG-007** fixed: added `check_job_completion(job.id)` after child task dispatch loop — covers the 0-valid-tracks edge case where job would otherwise stay stuck in "downloading" forever
- **BUG-011** fixed: changed fallback metadata match from OR to AND (title AND artist must both match); tightened recency window from 5 min to 2 min to reduce false positives under concurrent downloads
- **BUG-012** fixed: changed `logger.error` to `logger.debug` with clearer status message in `import_to_lidarr` for non-"done" tracks (e.g. "processed" parent tracks)
- **BUG-014** fixed: replaced manual Base64 decode in `get_current_user` with `Depends(security)` — removed redundant decoding and `import base64`
- **BUG-010** fixed: replaced 5-pattern regex stdout parser (`parse_file_path_from_output`, now deleted) with `--save-file <tmp>.spotdl`; after download spotdl writes a JSON array and `songs[0]['file_name']` gives the exact output path; temp file cleaned in `finally`; `find_recent_audio_file` kept as fallback, template path as last resort; removed top-level `import re`

### v0.2 — Playlist/Album Support
Added handling for playlist and album URLs without Spotify API configured:
- Single placeholder Track record created for playlist/album jobs
- `download_track` task expanded: detects `url_type in ['playlist', 'album']`, runs
  `spotdl --save-file` to get track list, creates child Track records, queues each one
- Parent track marked `status="processed"` rather than "done"
- Job `track_count` updated after track list resolved
- `original_url` and `url_type` columns added to Track model

---

## Known Bugs

### CRITICAL

#### BUG-001 — DEBUG prints leak auth info ✅ FIXED (v0.3)
**Files:** `backend/main.py:105,107`
```python
print(f"DEBUG: Decoded username='{username}', password length={len(password)}")
print(f"DEBUG: Auth decode error: {e}")
```
These appear in container logs. Remove before any shared/production deployment.
**Fix:** Delete both lines.

#### BUG-002 — `parse_audio_metadata` uses `logger` before it is defined ✅ FIXED (v0.3)
**File:** `backend/tasks.py:153`
```python
logger.warning(f"Failed to parse audio metadata for {file_path}: {e}")
```
`logger = get_task_logger(__name__)` is defined at line 175, **after** `parse_audio_metadata`
at line 123. Any call to `parse_audio_metadata` during module import or early task execution
will raise `NameError: name 'logger' is not defined`.
**Fix:** Move `logger = get_task_logger(__name__)` to before `parse_audio_metadata`, or
replace the `logger.warning` call inside `parse_audio_metadata` with a plain `print()` /
`logging.getLogger(__name__).warning()`.

---

### HIGH

#### BUG-003 — WebSocket receive loop blocks pub/sub forwarding ✅ FIXED (v0.3)
**File:** `backend/main.py:396-413`
```python
while True:
    message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
    if message and message["type"] == "message":
        await websocket.send_json(event_data)

    # Also check for client disconnection
    try:
        await websocket.receive_text()   # ← BLOCKS here waiting for client to type
    except WebSocketDisconnect:
        break
    except Exception:
        pass  # swallows everything
```
`receive_text()` is a blocking await that returns only when the *client* sends a message.
Because this is inside the same loop as `get_message()`, the pub/sub poll runs exactly once
and then the coroutine stalls waiting for incoming client text. Events from Celery workers
are silently dropped until the client sends something (which the frontend never does).
`except Exception: pass` also swallows `WebSocketDisconnect`, so the disconnect path is
unreachable in practice.
**Fix:** Use two concurrent tasks via `asyncio.gather` or `asyncio.wait` — one forwarding
Redis pub/sub to the WebSocket, one awaiting `receive_text()` to detect client disconnect.

#### BUG-004 — Race condition: subscribe after sending initial state ✅ ADDRESSED (v0.3)
**File:** `backend/main.py:383-385`
```python
pubsub.subscribe(f"ws:job:{job_id}")   # line 384
# ...
job_data = await get_job(...)          # line 389 — initial state fetched AFTER subscribe
await websocket.send_json(...)
```
Actually the subscribe occurs before the initial state fetch in the current code (line 384
before 389), so the race is minor. However: `tasks_redis_client.pubsub()` is a synchronous
Redis client used inside an async handler. `pubsub.get_message()` is a synchronous blocking
call inside an async function, which will block the event loop for up to 1 second per poll.
**Fix:** Use `aioredis` (async Redis client) and `async for` on the pub/sub channel, or run
the synchronous subscribe in a thread pool executor.

#### BUG-005 — `track_id` variable shadowed in `download_track` ✅ FIXED (v0.3)
**File:** `backend/tasks.py:354-360`
```python
@celery_app.task(bind=True, max_retries=3)
def download_track(self, track_id: str):   # function parameter
    ...
    for i, track_url in enumerate(track_urls):
        match = re.search(r"...", track_url)
        if not match:
            continue
        track_id = match.group(1)           # ← overwrites function parameter!
```
After the first iteration of the loop the `track_id` parameter is overwritten with the
Spotify ID of the first child track. Subsequent uses of `track_id` (e.g. in exception
handler at line 639: `db.query(Track).filter(Track.id == track_id)`) now look up the wrong
record, causing the error to be silently lost or applied to a different track.
**Fix:** Rename the inner variable (e.g. `child_spotify_id = match.group(1)`).

#### BUG-006 — Original `db` session leaked in exception handler ✅ FIXED (v0.3)
**File:** `backend/tasks.py:634-652`
```python
def download_track(self, track_id: str):
    db = next(get_db())          # line 266 — original session
    try:
        ...
    except Exception as exc:
        db = next(get_db())      # line 638 — NEW session, overwrites variable
        ...
    finally:
        db.close()               # only closes the session from line 638
```
The original session from line 266 is never closed when an exception occurs (unless
SQLAlchemy's `finally` inside `get_db()` fires, which it doesn't when using
`next(generator)`). The original session leaks.
**Fix:** Add `db.close()` at the top of the except block before reassigning `db`, or
restructure to use a single session throughout.

#### BUG-007 — `check_job_completion` may fire prematurely for playlist jobs ✅ FIXED (v0.3)
**File:** `backend/tasks.py:213-251`
The completion check runs immediately after the playlist/album parent track is marked
`"processed"` (line 388), before the child tracks are queued (line 392). If the job
contained only the parent placeholder track, `pending_tracks == 0` at that moment and
the job is incorrectly marked `"done"` before any child tracks download.
**Fix:** Call `check_job_completion` only after all child download tasks are queued, or
exclude `"processed"` from the terminal-states list until child tracks are created.

---

### MEDIUM

#### BUG-008 — `db.execute("SELECT 1")` requires `text()` in SQLAlchemy 2 ✅ FIXED (v0.3)
**File:** `backend/main.py:431`
```python
db.execute("SELECT 1")
```
SQLAlchemy 2.0 requires `from sqlalchemy import text; db.execute(text("SELECT 1"))`.
Raw string execution raises `ObjectNotExecutableError` at runtime.
**Fix:** `db.execute(text("SELECT 1"))`.

#### BUG-009 — `os.makedirs` called with empty string when file has no directory ✅ FIXED (v0.3)
**File:** `backend/tasks.py:548`
```python
os.makedirs(os.path.dirname(file_path), exist_ok=True)
```
If `file_path` has no directory component (e.g. just `"track.mp3"`),
`os.path.dirname` returns `""` and `os.makedirs("")` raises `FileNotFoundError`.
**Fix:** Guard with `if dir_part := os.path.dirname(file_path): os.makedirs(dir_part, exist_ok=True)`.

#### BUG-010 — Fragile spotdl output parsing ✅ FIXED (v0.3)
**File:** `backend/tasks.py:21-76`
Five overlapping regex patterns try to extract the output file path from spotdl's stdout.
spotdl's log format changes across versions and can vary by log level. This is the single
most likely source of "downloaded OK but track still shows failed" errors.
**Mitigation already in place:** filesystem fallback at line 497-501.
**Better fix:** Run spotdl with `--save-file` and read the `.spotdl` JSON, which contains
the output path in a structured format, instead of parsing human-readable log lines.

#### BUG-011 — Concurrent downloads can cause wrong-file metadata match ✅ FIXED (v0.3)
**File:** `backend/tasks.py:573-602`
When the expected file path is not found, the fallback searches `/music` for any file
modified in the last 5 minutes whose title/artist roughly matches. With `--concurrency=2`
workers this can match a different track's file. The match condition is lenient
(substring, case-insensitive, OR not AND).
**Fix:** Add a strict AND condition (`title_match and artist_match`) and tighten the
time window, or better: use the `--save-file` approach (BUG-010).

#### BUG-012 — `import_to_lidarr` silently does nothing when track is "processed" ✅ FIXED (v0.3)
**File:** `backend/tasks.py:662-664`
```python
if not track or track.status != "done":
    logger.error(f"Track {track_id} not ready for import")
    return
```
Playlist/album parent tracks end up with `status="processed"`, not `"done"`. When
`import_to_lidarr.delay(track.id)` is never explicitly called for the parent, this is
harmless. But the log message says "not ready for import" which is misleading if it
ever fires.

---

### LOW

#### BUG-013 — Unused import: `HttpUrl` ✅ FIXED (v0.3)
**File:** `backend/main.py:14`
```python
from pydantic import BaseModel, HttpUrl
```
`HttpUrl` is imported but never used.
**Fix:** Remove `HttpUrl` from the import.

#### BUG-014 — Redundant manual Base64 decoding ✅ FIXED (v0.3)
**File:** `backend/main.py:88-115`
`get_current_user` manually decodes the `Authorization: Basic …` header and constructs
an `HTTPBasicCredentials` object, then passes it to `authenticate()`. FastAPI's
`HTTPBasic` security dependency (already instantiated as `security`) does this
automatically. The manual path is redundant and slightly fragile.
**Fix:** Inject `credentials: HTTPBasicCredentials = Depends(security)` directly in
`get_current_user`, removing the manual base64 decode.

#### BUG-015 — DEBUG prints in `spotify.py` ✅ FIXED (v0.3)
**File:** `backend/spotify.py:22,38,41`
Three `print(f"DEBUG: ...")` statements log credential lengths and placeholder detection.
Harmless in isolation but pollutes container logs.
**Fix:** Remove or replace with `logger.debug(...)`.

---

## Unimplemented Features (from dev plan Phase 5)

- [ ] Docker Compose healthchecks for api, worker, redis
- [ ] Settings API endpoints (`GET /api/settings`, `PUT /api/settings`)
- [ ] ntfy.sh push notifications on job completion
- [ ] Syncthing folder ID is hardcoded to `"music"` — should come from `SYNCTHING_FOLDER_ID` env var

---

## Fix Priority Order

1. BUG-002 — NameError crash on audio metadata parse (causes silent task failure)
2. BUG-003 — WebSocket never forwards events to browser (breaks live progress entirely)
3. BUG-005 — `track_id` shadowed (wrong track updated on error for playlist jobs)
4. BUG-007 — Job marked done before child tracks queued
5. BUG-006 — DB session leak (will exhaust SQLite connections over time)
6. BUG-001, BUG-015 — Remove debug prints
7. BUG-008 — SQLAlchemy 2 text() fix (breaks /health/db endpoint)
8. BUG-009 — makedirs empty string crash
9. BUG-010, BUG-011 — Improve file path detection robustness
10. BUG-013, BUG-014 — Cleanup / dead code removal
