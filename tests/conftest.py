from __future__ import annotations

from pathlib import Path

import pytest

from jarchive_scraper import db
from jarchive_scraper.config import ScraperConfig


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def config(tmp_path: Path) -> ScraperConfig:
    return ScraperConfig(
        db_path=tmp_path / "jarchive.sqlite3",
        crawl_db_path=tmp_path / "jarchive_crawl.sqlite3",
        raw_dir=tmp_path / "raw_html",
        min_delay_seconds=0,
        max_delay_seconds=0,
        user_agent=BROWSER_UA,
    )


@pytest.fixture
def conn(config: ScraperConfig):
    db.ensure_storage(config)
    connection = db.connect(config.db_path)
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()
