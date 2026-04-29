from pydantic_settings import BaseSettings
from typing import Optional

class Settings(BaseSettings):
    # Spotify API (optional - only needed for metadata resolution)
    SPOTIFY_CLIENT_ID: Optional[str] = None
    SPOTIFY_CLIENT_SECRET: Optional[str] = None

    # Lidarr (optional - skip import if API key not set)
    LIDARR_URL: str = "http://lidarr:8686"
    LIDARR_API_KEY: str = ""

    # Syncthing (optional - skip sync if API key not set)
    SYNCTHING_URL: str = "http://syncthing:8384"
    SYNCTHING_API_KEY: str = ""
    SYNCTHING_FOLDER_ID: str = "music"

    # Auth
    ADMIN_PASSWORD: str = "changeme"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # spotdl output format
    SPOTDL_OUTPUT: str = "/music/{artist}/{album}/{title}.{output-ext}"
    SPOTDL_FORMAT: str = "mp3"
    SPOTDL_BITRATE: str = "320k"

    # App
    SECRET_KEY: str = "change_this_to_a_random_string"

    # Database
    DATABASE_URL: str = "sqlite:////data/spotsync.db"

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()