from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://j-archive.com/"
DEFAULT_DB_PATH = "jarchive.sqlite3"
DEFAULT_CRAWL_DB_NAME = "jarchive_crawl.sqlite3"
DEFAULT_RAW_DIR = "raw_html"
DEFAULT_MIN_DELAY_SECONDS = 30.0
DEFAULT_MAX_DELAY_SECONDS = 120.0
DEFAULT_MAX_REQUESTS_PER_DAY = 250
DEFAULT_RECENT_GAME_YEARS = 10
DEFAULT_RECENT_GAME_BIAS = 0.9


class ConfigError(ValueError):
    """Raised when scraper configuration is invalid."""


@dataclass(frozen=True)
class ScraperConfig:
    base_url: str = DEFAULT_BASE_URL
    db_path: Path = Path(DEFAULT_DB_PATH)
    crawl_db_path: Path | None = None
    raw_dir: Path = Path(DEFAULT_RAW_DIR)
    min_delay_seconds: float = DEFAULT_MIN_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS
    max_requests_per_day: int = DEFAULT_MAX_REQUESTS_PER_DAY
    max_requests_per_run: int | None = None
    recent_game_years: int = DEFAULT_RECENT_GAME_YEARS
    recent_game_bias: float = DEFAULT_RECENT_GAME_BIAS
    exclude_current_season: bool = True
    user_agent: str | None = None


def load_config(path: str | Path | None = None, **overrides: Any) -> ScraperConfig:
    data: dict[str, Any] = {}
    if path:
        config_path = Path(path)
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        elif str(path) != "jarchive_config.json":
            raise ConfigError(f"Config file does not exist: {config_path}")

    cleaned = {key: value for key, value in data.items() if value is not None}
    for key, value in overrides.items():
        if value is not None:
            cleaned[key] = value

    db_path = Path(cleaned.get("db_path", DEFAULT_DB_PATH))
    crawl_db_path = (
        Path(cleaned["crawl_db_path"])
        if cleaned.get("crawl_db_path") is not None
        else default_crawl_db_path(db_path)
    )

    config = ScraperConfig(
        base_url=str(cleaned.get("base_url", DEFAULT_BASE_URL)),
        db_path=db_path,
        crawl_db_path=crawl_db_path,
        raw_dir=Path(cleaned.get("raw_dir", DEFAULT_RAW_DIR)),
        min_delay_seconds=float(
            cleaned.get("min_delay_seconds", DEFAULT_MIN_DELAY_SECONDS)
        ),
        max_delay_seconds=float(
            cleaned.get("max_delay_seconds", DEFAULT_MAX_DELAY_SECONDS)
        ),
        max_requests_per_day=int(
            cleaned.get("max_requests_per_day", DEFAULT_MAX_REQUESTS_PER_DAY)
        ),
        max_requests_per_run=(
            int(cleaned["max_requests_per_run"])
            if cleaned.get("max_requests_per_run") is not None
            else None
        ),
        recent_game_years=int(
            cleaned.get("recent_game_years", DEFAULT_RECENT_GAME_YEARS)
        ),
        recent_game_bias=float(
            cleaned.get("recent_game_bias", DEFAULT_RECENT_GAME_BIAS)
        ),
        exclude_current_season=bool(
            cleaned.get("exclude_current_season", True)
        ),
        user_agent=cleaned.get("user_agent"),
    )
    validate_config(config)
    return config


def default_crawl_db_path(db_path: Path) -> Path:
    return db_path.with_name(DEFAULT_CRAWL_DB_NAME)


def with_paths(
    config: ScraperConfig,
    db_path: str | None,
    raw_dir: str | None,
    crawl_db_path: str | None = None,
) -> ScraperConfig:
    updates: dict[str, Any] = {}
    if db_path is not None:
        new_db_path = Path(db_path)
        updates["db_path"] = new_db_path
        if crawl_db_path is None:
            updates["crawl_db_path"] = default_crawl_db_path(new_db_path)
    if crawl_db_path is not None:
        updates["crawl_db_path"] = Path(crawl_db_path)
    if raw_dir is not None:
        updates["raw_dir"] = Path(raw_dir)
    return replace(config, **updates) if updates else config


def validate_config(config: ScraperConfig) -> None:
    if config.min_delay_seconds < 0:
        raise ConfigError("min_delay_seconds must be >= 0")
    if config.max_delay_seconds < config.min_delay_seconds:
        raise ConfigError("max_delay_seconds must be >= min_delay_seconds")
    if config.max_requests_per_day < 1:
        raise ConfigError("max_requests_per_day must be >= 1")
    if config.max_requests_per_run is not None and config.max_requests_per_run < 1:
        raise ConfigError("max_requests_per_run must be >= 1 when set")
    if config.recent_game_years < 1:
        raise ConfigError("recent_game_years must be >= 1")
    if not 0 <= config.recent_game_bias <= 1:
        raise ConfigError("recent_game_bias must be between 0 and 1")
    if config.crawl_db_path is not None and config.crawl_db_path == config.db_path:
        raise ConfigError("crawl_db_path must be separate from db_path")


def require_browser_user_agent(config: ScraperConfig) -> str:
    user_agent = (config.user_agent or "").strip()
    browser_tokens = ("Mozilla/", "AppleWebKit/", "Chrome/", "Firefox/", "Safari/")
    if len(user_agent) < 40 or not any(token in user_agent for token in browser_tokens):
        raise ConfigError(
            "Fetching requires user_agent in config to be one valid browser "
            "User-Agent string."
        )
    return user_agent
