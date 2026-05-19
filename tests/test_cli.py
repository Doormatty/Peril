from __future__ import annotations

import json
from pathlib import Path

from jarchive_scraper import db
from jarchive_scraper.cli import enqueue_children_from_fetch, main
from jarchive_scraper.fetcher import FetchResult


def run_cli(tmp_path: Path, *args: str) -> int:
    return main(
        [
            "--db",
            str(tmp_path / "jarchive.sqlite3"),
            "--raw-dir",
            str(tmp_path / "raw_html"),
            *args,
        ]
    )


def crawl_db_path(tmp_path: Path) -> Path:
    return tmp_path / "jarchive_crawl.sqlite3"


def test_cli_init_enqueue_and_status(tmp_path: Path, capsys) -> None:
    assert run_cli(tmp_path, "init") == 0
    capsys.readouterr()
    assert run_cli(
        tmp_path,
        "enqueue",
        "https://j-archive.com/showseason.php?season=40",
    ) == 0
    capsys.readouterr()
    assert run_cli(tmp_path, "status", "--json") == 0

    output = capsys.readouterr().out
    status = json.loads(output)
    assert status["queue"] == [
        {"count": 1, "status": "pending", "url_type": "season"}
    ]
    assert status["crawl"]["pending_games_total"] == 0

    assert run_cli(tmp_path, "status") == 0
    rich_output = capsys.readouterr().out
    assert "Queue" in rich_output
    assert "Crawl" in rich_output


def test_discover_dry_run_from_fixture_enqueues_without_fetching(tmp_path: Path, fixture_dir: Path) -> None:
    assert run_cli(
        tmp_path,
        "discover-seasons",
        "--dry-run",
        "--from-file",
        str(fixture_dir / "season_index.html"),
    ) == 0

    conn = db.connect(crawl_db_path(tmp_path))
    try:
        fetch_count = conn.execute("SELECT COUNT(*) AS count FROM fetches").fetchone()["count"]
        queued = conn.execute(
            "SELECT canonical_url FROM queue ORDER BY canonical_url"
        ).fetchall()
    finally:
        conn.close()

    assert fetch_count == 0
    assert [row["canonical_url"] for row in queued] == [
        "https://j-archive.com/showseason.php?season=39",
        "https://j-archive.com/showseason.php?season=40",
    ]


def test_fetch_skips_initial_delay_by_default(tmp_path: Path, monkeypatch) -> None:
    raw_file = tmp_path / "raw.html"
    raw_file.write_text("<html></html>", encoding="utf-8")
    delay_flags: list[bool] = []

    class FakeFetcher:
        def __init__(self, config) -> None:
            pass

        def fetch(self, conn, url: str, *, no_initial_delay: bool = False) -> FetchResult:
            delay_flags.append(no_initial_delay)
            return FetchResult(url, "game", 200, skipped=False, raw_file_path=str(raw_file))

    monkeypatch.setattr("jarchive_scraper.cli.Fetcher", FakeFetcher)
    assert run_cli(
        tmp_path,
        "enqueue",
        "https://j-archive.com/showgame.php?game_id=8811",
    ) == 0
    assert run_cli(tmp_path, "fetch", "--limit", "1") == 0

    assert delay_flags == [True]


def test_fetch_initial_delay_opt_in(tmp_path: Path, monkeypatch) -> None:
    raw_file = tmp_path / "raw.html"
    raw_file.write_text("<html></html>", encoding="utf-8")
    delay_flags: list[bool] = []

    class FakeFetcher:
        def __init__(self, config) -> None:
            pass

        def fetch(self, conn, url: str, *, no_initial_delay: bool = False) -> FetchResult:
            delay_flags.append(no_initial_delay)
            return FetchResult(url, "game", 200, skipped=False, raw_file_path=str(raw_file))

    monkeypatch.setattr("jarchive_scraper.cli.Fetcher", FakeFetcher)
    assert run_cli(
        tmp_path,
        "enqueue",
        "https://j-archive.com/showgame.php?game_id=8811",
    ) == 0
    assert run_cli(tmp_path, "fetch", "--limit", "1", "--initial-delay") == 0

    assert delay_flags == [False]


def test_parse_from_fixture_flow_is_idempotent(tmp_path: Path, fixture_dir: Path) -> None:
    assert run_cli(
        tmp_path,
        "parse",
        "--url",
        "https://j-archive.com/listseasons.php",
        "--file",
        str(fixture_dir / "season_index.html"),
    ) == 0
    assert run_cli(
        tmp_path,
        "parse",
        "--url",
        "https://j-archive.com/showseason.php?season=40",
        "--file",
        str(fixture_dir / "season_40.html"),
    ) == 0
    assert run_cli(
        tmp_path,
        "parse",
        "--url",
        "https://j-archive.com/showgame.php?game_id=8811",
        "--file",
        str(fixture_dir / "game_8811.html"),
    ) == 0
    assert run_cli(
        tmp_path,
        "parse",
        "--url",
        "https://j-archive.com/showgame.php?game_id=8811",
        "--file",
        str(fixture_dir / "game_8811.html"),
    ) == 0

    conn = db.connect(tmp_path / "jarchive.sqlite3")
    try:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                "count"
            ]
            for table in (
                "seasons",
                "games",
                "contestants",
                "categories",
                "clues",
                "responses",
                "scores",
            )
        }
    finally:
        conn.close()

    crawl_conn = db.connect(crawl_db_path(tmp_path))
    try:
        parse_error_count = crawl_conn.execute(
            "SELECT COUNT(*) AS count FROM parse_errors"
        ).fetchone()["count"]
    finally:
        crawl_conn.close()

    assert counts == {
        "seasons": 3,
        "games": 2,
        "contestants": 3,
        "categories": 4,
        "clues": 4,
        "responses": 5,
        "scores": 3,
    }
    assert parse_error_count == 0


def test_parse_workers_parse_saved_fetches_with_serialized_writes(tmp_path: Path, fixture_dir: Path) -> None:
    assert run_cli(tmp_path, "init") == 0

    saved_fetches = [
        (
            "https://j-archive.com/listseasons.php",
            "season_index",
            fixture_dir / "season_index.html",
        ),
        (
            "https://j-archive.com/showseason.php?season=40",
            "season",
            fixture_dir / "season_40.html",
        ),
        (
            "https://j-archive.com/showgame.php?game_id=8811",
            "game",
            fixture_dir / "game_8811.html",
        ),
    ]
    crawl_conn = db.connect(crawl_db_path(tmp_path))
    try:
        for url, url_type, raw_file in saved_fetches:
            db.enqueue(crawl_conn, url, url_type)
            db.record_fetch_success(
                crawl_conn,
                original_url=url,
                canonical_url=url,
                url_type=url_type,
                status_code=200,
                content_hash="fixture",
                raw_file_path=str(raw_file),
            )
        crawl_conn.commit()
    finally:
        crawl_conn.close()

    assert run_cli(tmp_path, "parse", "--workers", "2") == 0

    data_conn = db.connect(tmp_path / "jarchive.sqlite3")
    crawl_conn = db.connect(crawl_db_path(tmp_path))
    try:
        assert data_conn.execute("SELECT COUNT(*) AS count FROM seasons").fetchone()["count"] == 3
        assert data_conn.execute("SELECT COUNT(*) AS count FROM games").fetchone()["count"] == 2
        assert data_conn.execute("SELECT COUNT(*) AS count FROM clues").fetchone()["count"] == 4
        parser_states = crawl_conn.execute(
            "SELECT parser_state FROM fetches ORDER BY canonical_url"
        ).fetchall()
        parse_error_count = crawl_conn.execute(
            "SELECT COUNT(*) AS count FROM parse_errors"
        ).fetchone()["count"]
    finally:
        data_conn.close()
        crawl_conn.close()

    assert [row["parser_state"] for row in parser_states] == ["parsed", "parsed", "parsed"]
    assert parse_error_count == 0


def test_parse_rejects_non_positive_workers(tmp_path: Path) -> None:
    assert run_cli(tmp_path, "parse", "--workers", "0") == 2


def test_fetched_season_child_enqueue_records_game_dates_on_queue(conn, config, fixture_dir: Path) -> None:
    count = enqueue_children_from_fetch(
        conn,
        config,
        "https://j-archive.com/showseason.php?season=40",
        "season",
        str(fixture_dir / "season_40.html"),
    )

    rows = conn.execute(
        "SELECT canonical_url, air_date FROM queue ORDER BY canonical_url"
    ).fetchall()

    assert count == 2
    assert [dict(row) for row in rows] == [
        {
            "air_date": "2024-01-15",
            "canonical_url": "https://j-archive.com/showgame.php?game_id=8811",
        },
        {
            "air_date": "2024-01-16",
            "canonical_url": "https://j-archive.com/showgame.php?game_id=8812",
        },
    ]


def test_fetch_does_not_write_parsed_data_db(tmp_path: Path, monkeypatch, fixture_dir: Path) -> None:
    raw_file = fixture_dir / "season_40.html"

    class FakeFetcher:
        def __init__(self, config) -> None:
            pass

        def fetch(self, conn, url: str, *, no_initial_delay: bool = False) -> FetchResult:
            return FetchResult(url, "season", 200, skipped=False, raw_file_path=str(raw_file))

    monkeypatch.setattr("jarchive_scraper.cli.Fetcher", FakeFetcher)
    assert run_cli(
        tmp_path,
        "enqueue",
        "https://j-archive.com/showseason.php?season=40",
    ) == 0
    assert run_cli(tmp_path, "fetch", "--limit", "1") == 0

    data_conn = db.connect(tmp_path / "jarchive.sqlite3")
    crawl_conn = db.connect(crawl_db_path(tmp_path))
    try:
        game_count = (
            data_conn.execute("SELECT COUNT(*) AS count FROM games").fetchone()["count"]
            if db.table_exists(data_conn, "games")
            else 0
        )
        queued_games = crawl_conn.execute(
            """
            SELECT canonical_url, air_date FROM queue
            WHERE url_type = 'game'
            ORDER BY canonical_url
            """
        ).fetchall()
    finally:
        data_conn.close()
        crawl_conn.close()

    assert game_count == 0
    assert [dict(row) for row in queued_games] == [
        {
            "air_date": "2024-01-15",
            "canonical_url": "https://j-archive.com/showgame.php?game_id=8811",
        },
        {
            "air_date": "2024-01-16",
            "canonical_url": "https://j-archive.com/showgame.php?game_id=8812",
        },
    ]
