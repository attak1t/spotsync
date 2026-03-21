import json
import subprocess
import time
import redis
import mutagen
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from celery import Celery
from celery.utils.log import get_task_logger
from sqlalchemy.orm import Session

from backend.config import settings
from backend.database import SessionLocal, create_tables
from backend.models import Track, Job
from backend.lidarr import LidarrClient
from backend.syncthing import SyncthingClient

logger = get_task_logger(__name__)


def find_recent_audio_file(max_age_seconds: int = 300) -> str:
    """Find most recently created audio file in common directories.
    Returns path to most recent file, or None if none found.
    """
    audio_extensions = {'.mp3', '.flac', '.m4a', '.opus', '.ogg', '.wav'}
    recent_file = None
    recent_time = 0

    cutoff_time = time.time() - max_age_seconds

    # Check multiple possible locations where spotdl might save files
    search_dirs = [
        "/music",           # Primary music directory
        "/app",             # App working directory
        "/tmp",             # Temp directory
        "/",                # Root (unlikely)
        os.getcwd(),        # Current working directory
    ]

    # Also check for downloads in subdirectories of these locations
    for base_dir in search_dirs:
        if not os.path.exists(base_dir):
            continue

        try:
            for root, dirs, files in os.walk(base_dir):
                for file in files:
                    if any(file.lower().endswith(ext) for ext in audio_extensions):
                        file_path = os.path.join(root, file)
                        try:
                            # Check file size > 1KB to avoid empty/corrupt files
                            if os.path.getsize(file_path) < 1024:
                                continue

                            mtime = os.path.getmtime(file_path)
                            if mtime > cutoff_time and mtime > recent_time:
                                recent_time = mtime
                                recent_file = file_path
                        except (OSError, IOError):
                            continue
        except (OSError, IOError):
            continue

    return recent_file

def parse_audio_metadata(file_path):
    """Parse title, artist, album from audio file metadata."""
    try:
        audio = mutagen.File(file_path, easy=True)
        if audio is None:
            return None

        # Extract common tags
        title = audio.get('title', [None])[0]
        artist = audio.get('artist', [None])[0]
        album = audio.get('album', [None])[0]

        # If easy tags not found, try to get from regular tags
        if not all([title, artist, album]):
            audio = mutagen.File(file_path)
            if hasattr(audio, 'tags'):
                if 'TIT2' in audio.tags:
                    title = str(audio.tags['TIT2'])
                if 'TPE1' in audio.tags:
                    artist = str(audio.tags['TPE1'])
                if 'TALB' in audio.tags:
                    album = str(audio.tags['TALB'])

        return {
            'title': title,
            'artist': artist,
            'album': album
        }
    except Exception as e:
        # Use module logger (defined later in module)
        logger.warning(f"Failed to parse audio metadata for {file_path}: {e}")
        return None

# Ensure database tables exist before processing tasks
create_tables()

# Celery app
celery_app = Celery(
    "spotsync",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

# Optional Celery configuration
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Redis client for WebSocket pub/sub
redis_client = redis.from_url(settings.REDIS_URL)

def emit_ws(job_id: str, track_id: str, status: str, percent: int = 0, error: str = None):
    """Publish WebSocket event to Redis channel."""
    event = {
        "event": "track_update",
        "job_id": job_id,
        "track_id": track_id,
        "status": status,
        "percent": percent,
        "error": error,
        "timestamp": time.time(),
    }
    redis_client.publish(f"ws:job:{job_id}", json.dumps(event))

def emit_spotdl_output(job_id: str, track_id: str, output: str):
    """Publish spotdl output to Redis channel."""
    event = {
        "event": "spotdl_output",
        "job_id": job_id,
        "track_id": track_id,
        "output": output.strip(),
        "timestamp": time.time(),
    }
    redis_client.publish(f"ws:job:{job_id}", json.dumps(event))

def emit_job_update(job_id: str):
    """Publish job update event to Redis channel."""
    event = {
        "event": "job_updated",
        "job_id": job_id,
        "timestamp": time.time(),
    }
    redis_client.publish(f"ws:job:{job_id}", json.dumps(event))

def check_job_completion(job_id: str):
    """Check if all tracks in a job are done and update job status."""
    db = next(get_db())
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return

        # Check if all tracks are processed
        pending_tracks = db.query(Track).filter(
            Track.job_id == job.id,
            Track.status.notin_(["done", "imported", "failed", "processed"])
        ).count()

        if pending_tracks == 0:
            # All tracks processed
            failed_tracks = db.query(Track).filter(
                Track.job_id == job.id,
                Track.status == "failed"
            ).count()

            new_status = "done" if failed_tracks == 0 else "failed"

            # Only update if status changed
            if job.status != new_status:
                job.status = new_status
                db.commit()

                # Send job completion event
                event = {
                    "event": "job_complete",
                    "job_id": job.id,
                    "status": job.status,
                    "timestamp": time.time(),
                }
                redis_client.publish(f"ws:job:{job.id}", json.dumps(event))
                logger.info(f"Job {job.id} marked as {new_status} (failed tracks: {failed_tracks})")
    finally:
        db.close()

def get_db() -> Session:
    """Get database session (separate from FastAPI dependency)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@celery_app.task(bind=True, max_retries=3)
def download_track(self, track_id: str):
    """Download a single track using spotdl."""
    import re
    import os
    db = next(get_db())

    try:
        track = db.query(Track).filter(Track.id == track_id).first()
        if not track:
            logger.error(f"Track {track_id} not found")
            return

        job = track.job

        # Update status
        track.status = "downloading"
        track.celery_task_id = self.request.id
        db.commit()

        emit_ws(job.id, track.id, "downloading", 0)

        # Check if this is a playlist/album URL or query
        url_type = getattr(track, 'url_type', 'track')
        if url_type == 'query':
            # Search queries are no longer supported (Spotify API is no longer free)
            logger.error(f"Search query detected but not supported: {track.title}")
            track.status = "failed"
            track.error_message = "Search queries are not supported. Please use Spotify URLs (track, playlist, or album links)."
            db.commit()
            emit_ws(job.id, track.id, "failed", 0, error="Search queries are not supported")
            return

        elif url_type in ['playlist', 'album']:
            # For playlists/albums, we need to get track list first
            # Use spotdl --save-file to get track URLs
            import tempfile
            import os

            with tempfile.NamedTemporaryFile(mode='w+', suffix='.spotdl', delete=False) as f:
                temp_file = f.name

            try:
                # Get track list - try without operation first (defaults to download)
                cmd_list = [
                    "spotdl",
                    track.original_url or f"https://open.spotify.com/{url_type}/{track.spotify_id}",
                    "--save-file", temp_file,
                    "--log-level", "INFO",
                    "--ytm-data",
                ]

                logger.info(f"Getting track list: {' '.join(cmd_list)}")
                process = subprocess.Popen(
                    cmd_list,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                )

                # Read output line by line
                while True:
                    output = process.stdout.readline()
                    if output == '' and process.poll() is not None:
                        break
                    if output:
                        logger.info(f"spotdl: {output.strip()}")
                        emit_spotdl_output(job.id, track.id, output)

                # Get remaining output and return code
                stdout, stderr = process.communicate(timeout=300)
                returncode = process.returncode

                if returncode != 0:
                    # Send any remaining stderr as output
                    if stderr:
                        for line in stderr.splitlines():
                            emit_spotdl_output(job.id, track.id, line)
                    raise Exception(f"Failed to get track list: {stderr}")

                # Read track URLs from file
                with open(temp_file, 'r') as f:
                    track_urls = [line.strip() for line in f if line.strip()]

                if not track_urls:
                    raise Exception("No tracks found in playlist/album")

                # Create child track records
                child_tracks = []
                for i, track_url in enumerate(track_urls):
                    # Extract track ID from URL
                    import re
                    match = re.search(r"https://open\.spotify\.com/track/([a-zA-Z0-9]+)", track_url)
                    if not match:
                        logger.warning(f"Could not parse track URL: {track_url}")
                        continue

                    child_spotify_id = match.group(1)
                    child_track = Track(
                        job_id=job.id,
                        spotify_id=child_spotify_id,
                        title=f"Track {i+1}",
                        artist="",
                        album="",
                        duration_ms=0,
                        status="queued",
                        url_type="track",
                        original_url=track_url
                    )
                    db.add(child_track)
                    child_tracks.append(child_track)

                db.commit()

                # Update job track count and playlist name
                job.track_count = len(child_tracks)
                job.playlist_name = f"{url_type.capitalize()} ({len(child_tracks)} tracks)"
                db.commit()
                emit_job_update(job.id)

                # Mark parent track as processed (we'll keep it but not download)
                track.status = "processed"
                track.title = f"{url_type.capitalize()} ({len(child_tracks)} tracks)"
                db.commit()

                emit_ws(job.id, track.id, "processed", 100)

                # Start downloading each child track
                for child_track in child_tracks:
                    download_track.delay(child_track.id)

                # Check completion now in case playlist had 0 valid tracks;
                # for non-empty playlists this is a no-op (children still queued).
                check_job_completion(job.id)

                # Clean up and return - parent track is done
                os.unlink(temp_file)
                return

            except Exception as e:
                logger.exception(f"Error processing {url_type}: {e}")
                track.status = "failed"
                track.error_message = str(e)[:500]
                db.commit()
                emit_ws(job.id, track.id, "failed", 0, error=str(e)[:200])

                if 'temp_file' in locals() and os.path.exists(temp_file):
                    os.unlink(temp_file)
                raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))

        # For single tracks, use the original logic
        # Build spotdl command
        spotify_url = f"https://open.spotify.com/track/{track.spotify_id}"
        output_template = settings.SPOTDL_OUTPUT

        # Check if /music directory exists and is writable
        music_base = "/music"
        if os.path.exists(music_base):
            if os.access(music_base, os.W_OK):
                logger.info(f"Music directory {music_base} exists and is writable")
            else:
                logger.warning(f"Music directory {music_base} exists but is not writable")
        else:
            logger.warning(f"Music directory {music_base} does not exist, attempting to create")
            try:
                os.makedirs(music_base, exist_ok=True)
                logger.info(f"Created music directory {music_base}")
            except Exception as e:
                logger.error(f"Failed to create music directory {music_base}: {e}")

        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.spotdl', delete=False) as _tf:
            save_file = _tf.name

        try:
            cmd = [
                "spotdl",
                spotify_url,
                "--output", output_template,
                "--format", settings.SPOTDL_FORMAT,
                "--bitrate", settings.SPOTDL_BITRATE,
                "--log-level", "INFO",
                "--ytm-data",
                "--save-file", save_file,
            ]

            logger.info(f"Running spotdl: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )

            # Stream output for progress updates and WebSocket forwarding
            start_time = time.time()
            last_update = start_time

            while True:
                output = process.stdout.readline()
                if output == '' and process.poll() is not None:
                    break
                if output:
                    logger.info(f"spotdl: {output.strip()}")
                    emit_spotdl_output(job.id, track.id, output)
                    current_time = time.time()
                    if current_time - last_update > 5:
                        elapsed = current_time - start_time
                        percent = min(95, int((elapsed / 30) * 100))
                        emit_ws(job.id, track.id, "downloading", percent)
                        last_update = current_time

            stdout, stderr = process.communicate()
            returncode = process.returncode

            if returncode != 0:
                if stdout:
                    for line in stdout.splitlines():
                        emit_spotdl_output(job.id, track.id, line)
                if stderr:
                    for line in stderr.splitlines():
                        emit_spotdl_output(job.id, track.id, line)
                error_msg = stderr or stdout or "Unknown error"
                logger.error(f"spotdl failed: {error_msg}")
                track.status = "failed"
                track.error_message = error_msg[:500]
                db.commit()
                emit_ws(job.id, track.id, "failed", 0, error=error_msg[:200])
                raise self.retry(exc=Exception(error_msg), countdown=60 * (2 ** self.request.retries))

            # Resolve output file path from the JSON save file spotdl wrote
            actual_file_path = None
            try:
                with open(save_file) as f:
                    songs = json.load(f)
                if songs and songs[0].get('file_name'):
                    raw = songs[0]['file_name']
                    if not os.path.isabs(raw):
                        raw = os.path.join('/music', raw)
                    actual_file_path = raw
                    logger.info(f"File path from save file: {actual_file_path}")
            except Exception as e:
                logger.warning(f"Could not read spotdl save file: {e}")

            # Fallback: scan filesystem for recently created audio file
            if actual_file_path is None:
                recent_file = find_recent_audio_file()
                if recent_file:
                    actual_file_path = recent_file
                    logger.info(f"Found recently created audio file: {actual_file_path}")

        finally:
            if os.path.exists(save_file):
                os.unlink(save_file)

        from urllib.parse import quote

        if actual_file_path is not None:
            file_path = actual_file_path
            logger.info(f"Using file path: {file_path}")
        else:
            safe_artist = quote(track.artist or "Unknown", safe='')[:50]
            safe_album = quote(track.album or "Unknown", safe='')[:50]
            safe_title = quote(track.title or "Unknown", safe='')[:50]
            extension = settings.SPOTDL_FORMAT
            template = settings.SPOTDL_OUTPUT.replace('{output-ext}', extension)
            file_path = template.format(
                artist=safe_artist,
                album=safe_album,
                title=safe_title,
                track_id=track.spotify_id,
            )
            logger.warning(f"Could not determine file path from spotdl output, using template: {file_path}")

        # Ensure directory exists
        if dir_part := os.path.dirname(file_path):
            os.makedirs(dir_part, exist_ok=True)

        # Try to parse metadata from downloaded file
        final_file_path = None
        metadata = None

        # Check if file exists at the expected path
        if os.path.exists(file_path):
            metadata = parse_audio_metadata(file_path)
            final_file_path = file_path
        else:
            # File might have been moved by spotdl after metadata processing
            # Wait a bit and search for files matching metadata
            logger.info(f"File not found at expected path {file_path}, searching for recently created audio files...")
            time.sleep(2)  # Give spotdl time to finish post-processing

            # Search for recently created audio files
            recent_file = find_recent_audio_file()
            if recent_file:
                logger.info(f"Found recent audio file: {recent_file}")
                metadata = parse_audio_metadata(recent_file)
                if metadata:
                    final_file_path = recent_file
                    logger.info(f"Parsed metadata from recent file: {metadata}")

            # If still not found, try to search by expected metadata pattern
            if not final_file_path and (track.artist and track.artist != "Unknown" and track.title and not track.title.startswith("Track ")):
                # Construct expected directory pattern
                import shutil
                from urllib.parse import unquote

                # Try to find file in music directory with similar artist/album/title
                music_base = "/music"
                if os.path.exists(music_base):
                    for root, dirs, files in os.walk(music_base):
                        for file in files:
                            if any(file.lower().endswith(ext) for ext in ['.mp3', '.flac', '.m4a', '.opus', '.ogg', '.wav']):
                                file_path_candidate = os.path.join(root, file)
                                # Check if file was modified recently (last 2 minutes)
                                if os.path.getmtime(file_path_candidate) > time.time() - 120:
                                    file_metadata = parse_audio_metadata(file_path_candidate)
                                    if file_metadata:
                                        # Check if metadata matches track — require both title AND
                                        # artist to match to avoid false positives under concurrency
                                        title_match = (track.title.lower() in file_metadata.get('title', '').lower() or
                                                      file_metadata.get('title', '').lower() in track.title.lower())
                                        artist_match = (track.artist.lower() in file_metadata.get('artist', '').lower() or
                                                       file_metadata.get('artist', '').lower() in track.artist.lower())

                                        if title_match and artist_match:
                                            final_file_path = file_path_candidate
                                            metadata = file_metadata
                                            logger.info(f"Found matching file by metadata: {final_file_path}")
                                            break
                        if final_file_path:
                            break

        if final_file_path and metadata:
            # Update track with metadata if fields are empty or placeholder
            if not track.title or track.title.startswith("Track ") or track.title.startswith("Playlist:") or track.title.startswith("Album:"):
                track.title = metadata['title'] or track.title
            if not track.artist or track.artist == "":
                track.artist = metadata['artist'] or track.artist
            if not track.album or track.album == "":
                track.album = metadata['album'] or track.album
            logger.info(f"Updated track metadata from audio file: {track.title} by {track.artist}")
            file_path = final_file_path  # Update with actual file path
        elif final_file_path:
            # File found but no metadata parsed
            logger.warning(f"Found audio file but could not parse metadata: {final_file_path}")
            file_path = final_file_path
        else:
            logger.warning(f"Downloaded file not found, spotdl may have saved elsewhere")

        # Update track with file path
        track.status = "done"
        track.file_path = file_path
        db.commit()

        emit_ws(job.id, track.id, "done", 100)

        # Queue Lidarr import
        import_to_lidarr.delay(track.id)

        # Check if job is now complete (e.g., last track finished)
        check_job_completion(job.id)

    except Exception as exc:
        logger.exception(f"Error downloading track {track_id}")

        # Update track status — close the original session before reassigning
        db.close()
        db = next(get_db())
        track = db.query(Track).filter(Track.id == track_id).first()
        if track:
            track.status = "failed"
            track.error_message = str(exc)[:500]
            db.commit()

            emit_ws(track.job.id, track.id, "failed", 0, error=str(exc)[:200])

            # Check if job is now complete (e.g., all tracks failed or finished)
            check_job_completion(track.job.id)

        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))

    finally:
        db.close()

@celery_app.task
def import_to_lidarr(track_id: str):
    """Import a downloaded track into Lidarr."""
    db = next(get_db())

    try:
        track = db.query(Track).filter(Track.id == track_id).first()
        if not track or track.status != "done":
            current_status = track.status if track else "not found"
            logger.debug(f"Track {track_id} skipped for Lidarr import (status={current_status})")
            return

        job = track.job

        # Import to Lidarr
        lidarr = LidarrClient()
        result = lidarr.manual_import(track.file_path, disable_release_switching=True)

        # Check if import was skipped (API not configured)
        if isinstance(result, dict) and result.get("skipped"):
            track.lidarr_import_status = "skipped"
            emit_ws(job.id, track.id, "skipped", 100)
        else:
            track.lidarr_import_status = "imported"
            emit_ws(job.id, track.id, "imported", 100)

        db.commit()

        # Trigger Syncthing rescan (will be skipped if not configured)
        syncthing = SyncthingClient()
        syncthing.trigger_rescan()

        # Check if job is now complete
        check_job_completion(job.id)

    except Exception as exc:
        logger.exception(f"Error importing track {track_id} to Lidarr")

        track.lidarr_import_status = "failed"
        track.error_message = str(exc)[:500]
        db.commit()

        emit_ws(track.job.id, track.id, "import_failed", 0, error=str(exc)[:200])

    finally:
        db.close()