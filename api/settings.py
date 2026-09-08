"""Load and validate configuration for the FastAPI service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from einthusan_dl import load_config


@dataclass(frozen=True)
class Settings:
    api_key: str
    core_config: dict


def load_settings(core_config: dict | None = None) -> Settings:
    """Build Settings for the API.

    `core_config` is injectable so tests don't need a real .env file;
    production callers omit it and it's loaded from einthusan_dl.load_config().
    """
    api_key = os.environ.get("EINTHUSAN_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "EINTHUSAN_API_KEY is not set. Set it in .env or the environment "
            "before starting the API server."
        )
    resolved_config = core_config if core_config is not None else load_config()
    return Settings(api_key=api_key, core_config=resolved_config)


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor for production use (api/__main__.py)."""
    return load_settings()
