import re
import httpx
from typing import Dict, List, Optional, Tuple
from backend.config import settings

class SpotifyClient:
    """Client for Spotify Web API. Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET."""

    TOKEN_URL = "https://accounts.spotify.com/api/token"
    API_URL = "https://api.spotify.com/v1"

    def __init__(self):
        self.client_id = settings.SPOTIFY_CLIENT_ID
        self.client_secret = settings.SPOTIFY_CLIENT_SECRET
        self._token: Optional[str] = None
        self._client = httpx.Client(timeout=30.0)

    def is_configured(self) -> bool:
        """Return True if Spotify API credentials are configured."""
        # Check if credentials exist and are not placeholder/default values
        if not self.client_id or not self.client_secret:
            return False

        # Common placeholder values to ignore
        placeholders = [
            "your_client_id_here",
            "your_client_secret_here",
            "your_spotify_client_id",
            "your_spotify_client_secret",
        ]

        id_lower = self.client_id.lower()
        secret_lower = self.client_secret.lower()

        for placeholder in placeholders:
            if placeholder in id_lower or placeholder in secret_lower:
                return False

        return True

    def get_token(self) -> str:
        """Get access token using client credentials flow."""
        if not self.is_configured():
            raise ValueError("Spotify API credentials not configured")

        response = self._client.post(
            self.TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )
        response.raise_for_status()
        self._token = response.json()["access_token"]
        return self._token

    def _ensure_token(self):
        """Ensure we have a valid access token."""
        if not self._token:
            self.get_token()

    def _request(self, method: str, path: str, **kwargs) -> Dict:
        """Make authenticated request to Spotify API."""
        self._ensure_token()
        url = f"{self.API_URL}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"

        response = self._client.request(method, url, headers=headers, **kwargs)
        response.raise_for_status()
        return response.json()

    def resolve_url(self, spotify_url: str) -> Tuple[Optional[str], List[Dict]]:
        """
        Resolve a Spotify URL to playlist/album name and list of tracks.

        Returns:
            Tuple of (playlist/album name or None, list of track dicts)

        Track dict format:
            {
                "id": "spotify_track_id",
                "title": "Track Title",
                "artist": "Artist Name",
                "album": "Album Name",
                "duration_ms": 123456,
                "track_number": 1,
                "disc_number": 1,
            }
        """
        if not self.is_configured():
            # Without credentials, we can only handle single track URLs
            # and won't have metadata
            track_id = self._extract_track_id(spotify_url)
            if track_id:
                return None, [{"id": track_id, "title": "", "artist": "", "album": "", "duration_ms": 0}]
            else:
                raise ValueError("Spotify API credentials not configured and URL is not a single track")

        # Parse URL type and ID
        url_type, obj_id = self._parse_spotify_url(spotify_url)

        if url_type == "track":
            track_data = self._request("GET", f"/tracks/{obj_id}")
            return None, [self._format_track(track_data)]

        elif url_type == "album":
            album_data = self._request("GET", f"/albums/{obj_id}")
            tracks_data = self._request("GET", f"/albums/{obj_id}/tracks", params={"limit": 50})
            album_name = album_data["name"]
            tracks = []
            for track in tracks_data["items"]:
                # Get full track info for duration
                full_track = self._request("GET", f"/tracks/{track['id']}")
                tracks.append(self._format_track(full_track))
            return album_name, tracks

        elif url_type == "playlist":
            playlist_data = self._request("GET", f"/playlists/{obj_id}")
            playlist_name = playlist_data["name"]
            tracks = []
            offset = 0
            limit = 100

            while True:
                playlist_tracks = self._request(
                    "GET",
                    f"/playlists/{obj_id}/tracks",
                    params={"offset": offset, "limit": limit}
                )

                for item in playlist_tracks["items"]:
                    if item["track"]:  # Sometimes tracks are None if unavailable
                        tracks.append(self._format_track(item["track"]))

                if len(playlist_tracks["items"]) < limit:
                    break
                offset += limit

            return playlist_name, tracks

        else:
            raise ValueError(f"Unsupported Spotify URL type: {url_type}")

    def _parse_spotify_url(self, url: str) -> Tuple[str, str]:
        """Parse Spotify URL and return (type, id)."""
        patterns = {
            "track": r"https://open\.spotify\.com/track/([a-zA-Z0-9]+)",
            "album": r"https://open\.spotify\.com/album/([a-zA-Z0-9]+)",
            "playlist": r"https://open\.spotify\.com/playlist/([a-zA-Z0-9]+)",
        }

        for url_type, pattern in patterns.items():
            match = re.search(pattern, url)
            if match:
                return url_type, match.group(1)

        raise ValueError(f"Invalid Spotify URL: {url}")

    def _extract_track_id(self, url: str) -> Optional[str]:
        """Extract track ID from URL without API credentials."""
        match = re.search(r"https://open\.spotify\.com/track/([a-zA-Z0-9]+)", url)
        return match.group(1) if match else None

    def _format_track(self, track_data: Dict) -> Dict:
        """Format Spotify track API response to our track dict format."""
        artists = ", ".join(artist["name"] for artist in track_data["artists"])
        return {
            "id": track_data["id"],
            "title": track_data["name"],
            "artist": artists,
            "album": track_data["album"]["name"] if "album" in track_data else "",
            "duration_ms": track_data["duration_ms"],
            "track_number": track_data.get("track_number", 1),
            "disc_number": track_data.get("disc_number", 1),
        }