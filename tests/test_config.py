"""Settings from the environment."""

from __future__ import annotations

from pathlib import Path

from workshop_analytics.config import Settings


def test_defaults_suit_a_laptop() -> None:
    settings = Settings.from_environment({})

    assert settings.database_url == "sqlite:///analytics.db"
    assert settings.signing_key == ""
    assert settings.denied_tokens_file is None
    assert settings.allowed_origins == ()
    assert settings.active_allowance == 150.0
    assert settings.cookie_secure is None


def test_every_variable_is_read() -> None:
    settings = Settings.from_environment(
        {
            "DATABASE_URL": "sqlite:////data/analytics.db",
            "TOKEN_SIGNING_KEY": " abc ",
            "DENIED_TOKENS_FILE": "/etc/workshop-analytics/denied.txt",
            "ALLOWED_ORIGINS": "https://a.example, https://b.example",
            "ACTIVE_ALLOWANCE": "90",
            "AWAY_ALLOWANCE": "600",
            "SILENT_LIMIT": "1200",
            "LIVE_LINGER": "60",
            "MAX_BODY_BYTES": "1024",
            "MAX_BATCH": "10",
            "RATE_LIMIT_PER_MINUTE": "5",
            "SESSION_HOURS": "2",
            "COOKIE_SECURE": "true",
        }
    )

    assert settings.database_url == "sqlite:////data/analytics.db"
    assert settings.signing_key == "abc"
    assert settings.denied_tokens_file == Path("/etc/workshop-analytics/denied.txt")
    assert settings.allowed_origins == ("https://a.example", "https://b.example")
    assert settings.active_allowance == 90.0
    assert settings.away_allowance == 600.0
    assert settings.silent_limit == 1200.0
    assert settings.linger == 60.0
    assert settings.max_body_bytes == 1024
    assert settings.max_batch == 10
    assert settings.rate_limit_per_minute == 5
    assert settings.session_hours == 2.0
    assert settings.cookie_secure is True
    assert Settings.from_environment({"COOKIE_SECURE": "off"}).cookie_secure is False
