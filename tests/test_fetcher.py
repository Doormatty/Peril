from __future__ import annotations

from dataclasses import dataclass

from jarchive_scraper import db
from jarchive_scraper.config import ScraperConfig
from jarchive_scraper.fetcher import Fetcher


@dataclass
class FakeResponse:
    status_code: int
    content: bytes
    headers: dict[str, str]


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> FakeResponse:
        self.calls.append((url, headers))
        return FakeResponse(200, b"<html>saved</html>", {"content-type": "text/html"})


class FixedRandom:
    def uniform(self, low: float, high: float) -> float:
        assert low == 10
        assert high == 20
        return 13.5


def test_successful_urls_are_skipped_without_second_request(conn, config: ScraperConfig) -> None:
    url = "https://j-archive.com/showgame.php?game_id=8811"
    db.enqueue(conn, url, "game")
    session = FakeSession()
    fetcher = Fetcher(config, session=session, sleeper=lambda seconds: None)

    first = fetcher.fetch(conn, url, no_initial_delay=True)
    second = fetcher.fetch(conn, url, no_initial_delay=True)

    assert first.skipped is False
    assert second.skipped is True
    assert len(session.calls) == 1
    assert db.successful_fetch_exists(conn, url)


def test_fetcher_sleeps_injected_random_delay_before_request(conn, config: ScraperConfig) -> None:
    config = ScraperConfig(
        db_path=config.db_path,
        raw_dir=config.raw_dir,
        min_delay_seconds=10,
        max_delay_seconds=20,
        user_agent=config.user_agent,
    )
    url = "https://j-archive.com/showgame.php?game_id=8811"
    db.enqueue(conn, url, "game")
    sleeps: list[float] = []
    session = FakeSession()
    fetcher = Fetcher(
        config,
        session=session,
        sleeper=sleeps.append,
        rng=FixedRandom(),
    )

    result = fetcher.fetch(conn, url)

    assert result.status_code == 200
    assert sleeps == [13.5]
    assert len(session.calls) == 1
