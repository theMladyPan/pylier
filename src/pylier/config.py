"""Runtime configuration for pylier.

Settings are read from environment variables (prefixed ``PYLIER_``) or .env,
mirroring logfire's configuration ergonomics. The recorder consults the global
level to decide which nodes are captured; the render/server paths use the
sidecar path.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from pylier.model import Level


class Settings(BaseSettings):
    """Pylier runtime settings.

    Attributes:
        level: Global capture verbosity. Nodes with a higher level rank than
            this are dropped. Mirrors logfire's ``min_level``.
        sidecar_path: Directory where the JSONL trace sidecar is written. When
            set, the recorder also appends events to disk for offline replay /
            cross-process consumers (OTel receiver, viewer server).
        sidecar_name: Filename within ``sidecar_path`` for the active sidecar.
        server_port: Port used by ``pylier.serve()`` for the live viewer.
        preview_limit: Max chars of value preview captured at DEBUG+ levels.
        capture_values: If True, recorder stores the full serialized payload on
            each edge for click-to-inspect debugging (like logfire capturing
            whatever you pass). Disabled by default; binary payloads are
            always truncated to a summary. Settable via ``PYLIER_CAPTURE_VALUES``.
        value_limit: Max chars of a serialized payload value.
    """

    model_config = SettingsConfigDict(env_prefix="PYLIER_", env_file=".env", extra="ignore")

    level: Level = Level.INFO
    sidecar_path: Path | None = None
    sidecar_name: str = "pylier-trace.jsonl"
    server_port: int = 8765
    preview_limit: int = 80
    capture_values: bool = False
    value_limit: int = 2000


@lru_cache
def get_settings() -> Settings:
    """Return the cached process-wide settings instance."""
    return Settings()


def reload_settings() -> Settings:
    """Drop the cache and re-read settings (used by tests / config reloads)."""
    get_settings.cache_clear()
    return get_settings()


__all__ = ["Settings", "get_settings", "reload_settings"]
