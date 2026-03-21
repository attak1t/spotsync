import httpx
from backend.config import settings

class SyncthingClient:
    """Client for Syncthing REST API."""

    def __init__(self):
        self.base_url = settings.SYNCTHING_URL.rstrip("/")
        self.api_key = settings.SYNCTHING_API_KEY
        self.headers = {"X-API-Key": self.api_key}
        self._client = httpx.Client(timeout=30.0)

    def is_configured(self) -> bool:
        """Return True if Syncthing API is configured."""
        return bool(self.api_key)

    def trigger_rescan(self, folder_id: str = None) -> bool:
        """
        Trigger a rescan of a Syncthing folder.

        Args:
            folder_id: Folder ID to rescan (defaults to SYNCTHING_FOLDER_ID from settings)

        Returns:
            True if successful
        """
        if not self.is_configured():
            return False

        if folder_id is None:
            folder_id = settings.SYNCTHING_FOLDER_ID

        try:
            response = self._client.post(
                f"{self.base_url}/rest/db/scan",
                params={"folder": folder_id},
                headers=self.headers
            )
            response.raise_for_status()
            return True
        except Exception:
            return False

    def test_connection(self) -> bool:
        """Test connection to Syncthing API."""
        if not self.is_configured():
            return False

        try:
            response = self._client.get(
                f"{self.base_url}/rest/system/status",
                headers=self.headers
            )
            response.raise_for_status()
            return True
        except Exception:
            return False