import httpx
from typing import Dict, Any
from backend.config import settings

class LidarrClient:
    """Client for Lidarr REST API."""

    def __init__(self):
        self.base_url = settings.LIDARR_URL.rstrip("/")
        self.api_key = settings.LIDARR_API_KEY
        self.headers = {"X-Api-Key": self.api_key}
        self._client = httpx.Client(timeout=60.0)

    def is_configured(self) -> bool:
        """Return True if Lidarr API is configured."""
        return bool(self.api_key)

    def manual_import(self, file_path: str, disable_release_switching: bool = True) -> Dict[str, Any]:
        """
        Import a file using Lidarr's Manual Import API.

        Args:
            file_path: Absolute path to the audio file
            disable_release_switching: Prevent Lidarr from rejecting tracks it doesn't recognise

        Returns:
            Lidarr API response
        """
        if not self.is_configured():
            return {"skipped": True, "reason": "Lidarr API key not configured"}

        payload = {
            "path": file_path,
            "importMode": "Move",  # or "Copy"
            "disableReleaseSwitching": disable_release_switching,
        }

        response = self._client.post(
            f"{self.base_url}/api/v1/manualimport",
            json=payload,
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def trigger_scan(self) -> Dict[str, Any]:
        """Trigger a RescanFolders command in Lidarr."""
        if not self.is_configured():
            return {"skipped": True, "reason": "Lidarr API key not configured"}

        payload = {"name": "RescanFolders"}

        response = self._client.post(
            f"{self.base_url}/api/v1/command",
            json=payload,
            headers=self.headers
        )
        response.raise_for_status()
        return response.json()

    def test_connection(self) -> bool:
        """Test connection to Lidarr API."""
        if not self.is_configured():
            return False

        try:
            response = self._client.get(
                f"{self.base_url}/api/v1/system/status",
                headers=self.headers
            )
            response.raise_for_status()
            return True
        except Exception:
            return False