"""Settings, read from the environment.

Every knob the service has is an environment variable, so the same
image runs on a laptop, in a container and in a cluster with nothing
but its environment changing. `Settings.from_environment()` is the one
place the names are read; the rest of the service takes a `Settings`
value and never touches `os.environ` itself.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATABASE_URL = "sqlite:///analytics.db"

SESSION_COOKIE = "workshop_analytics_session"


@dataclass(frozen=True)
class Settings:
    """The service's configuration, with the defaults a laptop wants.

    Durations are seconds. The status thresholds are the allowances the
    live view applies over the extension's heartbeat intervals (60
    seconds visible, 5 minutes hidden): more than two missed beats
    before a session stops reading as active or away, so one delayed
    batch does not flicker the display.
    """

    database_url: str = DEFAULT_DATABASE_URL
    signing_key: str = field(default="", repr=False)
    denied_tokens_file: Path | None = None
    allowed_origins: tuple[str, ...] = ()
    active_allowance: float = 150.0
    away_allowance: float = 750.0
    silent_limit: float = 1800.0
    linger: float = 300.0
    max_body_bytes: int = 2 * 1024 * 1024
    max_batch: int = 1000
    rate_limit_per_minute: int = 120
    session_hours: float = 12.0
    cookie_secure: bool | None = None

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Build settings from environment variables, defaults for the rest."""

        values = dict(os.environ if environ is None else environ)

        def text(name: str, default: str) -> str:
            return values.get(name, default).strip() or default

        def number(name: str, default: float) -> float:
            raw = values.get(name, "").strip()

            return float(raw) if raw else default

        def count(name: str, default: int) -> int:
            raw = values.get(name, "").strip()

            return int(raw) if raw else default

        def flag(name: str) -> bool | None:
            raw = values.get(name, "").strip().lower()

            if raw in {"1", "true", "yes", "on"}:
                return True

            if raw in {"0", "false", "no", "off"}:
                return False

            return None

        denied = values.get("DENIED_TOKENS_FILE", "").strip()
        origins = tuple(
            origin.strip()
            for origin in values.get("ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )

        return cls(
            database_url=text("DATABASE_URL", DEFAULT_DATABASE_URL),
            signing_key=values.get("TOKEN_SIGNING_KEY", "").strip(),
            denied_tokens_file=Path(denied) if denied else None,
            allowed_origins=origins,
            active_allowance=number("ACTIVE_ALLOWANCE", 150.0),
            away_allowance=number("AWAY_ALLOWANCE", 750.0),
            silent_limit=number("SILENT_LIMIT", 1800.0),
            linger=number("LIVE_LINGER", 300.0),
            max_body_bytes=count("MAX_BODY_BYTES", 2 * 1024 * 1024),
            max_batch=count("MAX_BATCH", 1000),
            rate_limit_per_minute=count("RATE_LIMIT_PER_MINUTE", 120),
            session_hours=number("SESSION_HOURS", 12.0),
            cookie_secure=flag("COOKIE_SECURE"),
        )
