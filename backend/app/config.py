from pydantic_settings import BaseSettings
from functools import lru_cache
from typing import ClassVar


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Satellite Monitor API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1

    # CORS
    CORS_ORIGINS: list[str] = ["*"]

    # TLE sources (Celestrak)
    TLE_SOURCES: ClassVar[dict[str, str]] = {
        "stations":   "https://celestrak.org/SATCAT/stations.txt",
        "starlink":   "https://celestrak.org/SATCAT/starlink.txt",
        "gps":        "https://celestrak.org/SATCAT/gps-ops.txt",
        "resource":   "https://celestrak.org/SATCAT/resource.txt",
        "active":     "https://celestrak.org/SATCAT/active.txt",
    }
    TLE_LOCAL_FILE: str = "tle_data.txt"
    TLE_UPDATE_INTERVAL_H: int = 24          # авто-обновление каждые N часов

    # Orbit classification thresholds (km)
    LEO_MAX_KM: float = 2_000.0
    MEO_MAX_KM: float = 35_786.0              # всё что выше — GEO / HEO

    # Passes prediction
    DEFAULT_PASS_DAYS: int = 3
    MAX_PASS_DAYS: int = 10
    MIN_ELEVATION_DEG: float = 5.0            # минимальная высота над горизонтом

    # Orbit track
    DEFAULT_TRACK_MINUTES: int = 90
    DEFAULT_TRACK_STEPS: int = 360
    MAX_TRACK_STEPS: int = 1440

    # Coverage zone
    COVERAGE_STEP_DEG: float = 1.0           # шаг при построении зоны видимости

    # WebSocket
    WS_BROADCAST_INTERVAL_S: float = 5.0    # интервал рассылки позиций

    # Cache TTL (seconds)
    POSITION_CACHE_TTL_S: int = 5
    PASSES_CACHE_TTL_S: int = 300

    # Performance
    MAX_SIMULTANEOUS_SATELLITES: int = 500
    BULK_POSITION_LIMIT: int = 200

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    return Settings()