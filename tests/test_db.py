from __future__ import annotations

from datetime import date

from jarchive_scraper import db
from jarchive_scraper.config import ScraperConfig
from jarchive_scraper.parser import parse_game_page


def test_queue_transitions_through_fetched_failed_and_parsed(conn, config: ScraperConfig) -> None:
    game_url = "https://j-archive.com/showgame.php?game_id=8811"
    season_url = "https://j-archive.com/showseason.php?season=40"

    assert db.enqueue(conn, game_url, "game") is True
    db.record_fetch_success(
        conn,
        original_url=game_url,
        canonical_url=game_url,
        url_type="game",
        status_code=200,
        content_hash="abc",
        raw_file_path=str(config.raw_dir / "game.html"),
    )
    row = conn.execute("SELECT * FROM queue WHERE canonical_url = ?", (game_url,)).fetchone()
    assert row["status"] == "fetched"
    assert row["attempts"] == 1
    assert db.count_requests_today(conn) == 1

    fetch = db.get_fetch(conn, game_url)
    assert fetch is not None
    db.set_parser_state(conn, fetch["id"], game_url, "parsed")
    row = conn.execute("SELECT * FROM queue WHERE canonical_url = ?", (game_url,)).fetchone()
    assert row["status"] == "parsed"

    assert db.enqueue(conn, season_url, "season") is True
    db.record_fetch_failure(
        conn,
        original_url=season_url,
        canonical_url=season_url,
        url_type="season",
        status_code=503,
        error="HTTP 503",
    )
    row = conn.execute("SELECT * FROM queue WHERE canonical_url = ?", (season_url,)).fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert db.count_requests_today(conn) == 2

    assert db.reset_retryable_failures(conn) == 1
    row = conn.execute("SELECT * FROM queue WHERE canonical_url = ?", (season_url,)).fetchone()
    assert row["status"] == "pending"


def test_crawl_schema_removes_redundant_queue_and_fetch_fields(conn) -> None:
    assert set(db.table_columns(conn, "queue")) == {
        "id",
        "canonical_url",
        "url_type",
        "status",
        "updated_at",
        "attempts",
        "last_error",
        "air_date",
    }
    assert set(db.table_columns(conn, "fetches")) == {
        "id",
        "original_url",
        "canonical_url",
        "url_type",
        "status_code",
        "content_hash",
        "raw_file_path",
        "parser_state",
        "error",
    }


def test_main_schema_removes_redundant_round_and_clue_fields(conn) -> None:
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "rounds" not in tables

    assert set(db.table_columns(conn, "games")) == {
        "game_id",
        "show_number",
        "air_date",
        "season_id",
        "title",
        "notes",
    }
    assert set(db.table_columns(conn, "contestants")) == {
        "id",
        "game_id",
        "name",
        "notes",
    }
    assert set(db.table_columns(conn, "categories")) == {
        "id",
        "game_id",
        "round_order",
        "name",
        "board_position",
    }
    assert set(db.table_columns(conn, "clues")) == {
        "id",
        "category_id",
        "row_value",
        "clue_text",
        "correct_response",
        "is_daily_double",
        "is_final_jeopardy",
        "is_triple_stumper",
    }
    assert set(db.table_columns(conn, "responses")) == {
        "id",
        "clue_id",
        "contestant_id",
        "response_text",
        "correctness",
    }


def test_replace_game_detail_stores_round_metadata_and_response_flags(conn, fixture_dir) -> None:
    detail = parse_game_page(
        (fixture_dir / "game_8811.html").read_text(encoding="utf-8"),
        source_url="https://j-archive.com/showgame.php?game_id=8811",
    )

    db.replace_game_detail(conn, detail)

    category = conn.execute(
        """
        SELECT round_order, name, board_position
        FROM categories
        WHERE game_id = ? AND board_position = 1
        ORDER BY round_order
        LIMIT 1
        """,
        ("8811",),
    ).fetchone()
    assert dict(category) == {
        "round_order": 1,
        "name": "SCIENCE",
        "board_position": 1,
    }

    clue_flags = conn.execute(
        """
        SELECT
          SUM(is_final_jeopardy) AS final_count,
          SUM(is_triple_stumper) AS triple_count,
          SUM(is_daily_double) AS daily_double_count
        FROM clues
        """
    ).fetchone()
    assert clue_flags["final_count"] == 1
    assert clue_flags["triple_count"] == 1
    assert clue_flags["daily_double_count"] == 1

    responses = conn.execute(
        """
        SELECT c.name AS contestant, r.response_text, r.correctness
        FROM responses r
        JOIN contestants c ON c.id = r.contestant_id
        ORDER BY r.id
        """
    ).fetchall()
    assert [dict(row) for row in responses] == [
        {
            "contestant": "Bob Example",
            "response_text": "What is oxygen?",
            "correctness": 0,
        },
        {"contestant": "Alice Example", "response_text": None, "correctness": 1},
        {"contestant": "Carol Example", "response_text": None, "correctness": 1},
        {
            "contestant": "Alice Example",
            "response_text": "What is Paris?",
            "correctness": 1,
        },
        {
            "contestant": "Bob Example",
            "response_text": "What is Lyon?",
            "correctness": 0,
        },
    ]


def test_response_contestant_resolver_handles_display_names_and_aliases() -> None:
    contestants = [
        (1, "John Michael Higgins"),
        (2, "Matt Rogers"),
        (3, "Robert Nashin"),
    ]

    assert db.resolve_response_contestant_id(contestants, "John Michael Higgins") == 1
    assert db.resolve_response_contestant_id(contestants, "Michael!") == 1
    assert db.resolve_response_contestant_id(contestants, "Matt") == 2
    assert db.resolve_response_contestant_id(contestants, "Bob") == 3
    assert db.resolve_response_contestant_id(contestants, "Unknown") is None


def test_init_db_migrates_legacy_fetch_schema(tmp_path) -> None:
    legacy_db = tmp_path / "legacy.sqlite3"
    conn = db.connect(legacy_db)
    try:
        conn.execute(
            """
            CREATE TABLE fetches (
                id INTEGER PRIMARY KEY,
                original_url TEXT NOT NULL,
                canonical_url TEXT NOT NULL UNIQUE,
                url_type TEXT NOT NULL CHECK (url_type IN ('season_index', 'season', 'game')),
                status_code INTEGER,
                response_headers_json TEXT,
                content_hash TEXT,
                raw_file_path TEXT,
                first_fetched_at TEXT,
                last_attempted_at TEXT NOT NULL,
                parser_state TEXT NOT NULL DEFAULT 'unparsed',
                source_permission_note TEXT NOT NULL,
                error TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO fetches
                (original_url, canonical_url, url_type, status_code, response_headers_json,
                 content_hash, raw_file_path, first_fetched_at, last_attempted_at,
                 parser_state, source_permission_note, error)
            VALUES (?, ?, 'game', 200, '{}', 'abc', 'raw.html', ?, ?, 'unparsed', 'old note', NULL)
            """,
            (
                "https://j-archive.com/showgame.php?game_id=1",
                "https://j-archive.com/showgame.php?game_id=1",
                "2026-05-16T00:00:00+00:00",
                "2026-05-16T00:00:00+00:00",
            ),
        )

        db.init_db(conn)

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(fetches)")}
        assert "response_headers_json" not in columns
        assert "source_permission_note" not in columns
        assert "first_fetched_at" not in columns
        assert "last_attempted_at" not in columns
        row = conn.execute(
            "SELECT original_url, canonical_url, content_hash FROM fetches"
        ).fetchone()
        assert row["original_url"] == "https://j-archive.com/showgame.php?game_id=1"
        assert row["canonical_url"] is None
        assert row["content_hash"] == "abc"
        fetch = db.get_fetch(conn, "https://j-archive.com/showgame.php?game_id=1")
        assert fetch is not None
        assert fetch["canonical_url"] == "https://j-archive.com/showgame.php?game_id=1"
    finally:
        conn.close()


def test_pending_game_selection_is_biased_to_recent_air_dates(conn) -> None:
    old_url = "https://j-archive.com/showgame.php?game_id=1000"
    recent_url = "https://j-archive.com/showgame.php?game_id=2000"
    db.enqueue(conn, old_url, "game", air_date="2014-01-01")
    db.enqueue(conn, recent_url, "game", air_date="2024-01-01")

    row = db.next_pending_url(
        conn,
        recent_game_years=10,
        recent_game_bias=1.0,
        today=date(2026, 5, 16),
        random_value=0.0,
    )

    assert row is not None
    assert row["canonical_url"] == recent_url


def test_pending_game_selection_falls_back_when_no_recent_games(conn) -> None:
    old_url = "https://j-archive.com/showgame.php?game_id=1000"
    db.enqueue(conn, old_url, "game", air_date="2014-01-01")

    row = db.next_pending_url(
        conn,
        recent_game_years=10,
        recent_game_bias=1.0,
        today=date(2026, 5, 16),
        random_value=0.0,
    )

    assert row is not None
    assert row["canonical_url"] == old_url


def test_init_storage_migrates_legacy_crawl_tables(tmp_path) -> None:
    data_conn = db.connect(tmp_path / "jarchive.sqlite3")
    crawl_conn = db.connect(tmp_path / "jarchive_crawl.sqlite3")
    try:
        db.init_db(data_conn)
        game_url = "https://j-archive.com/showgame.php?game_id=1"
        db.enqueue(data_conn, game_url, "game", air_date="2024-01-01")
        data_conn.execute(
            """
            INSERT INTO games (game_id, air_date)
            VALUES ('1', '2024-01-01')
            """,
        )
        data_conn.execute("UPDATE queue SET air_date = NULL WHERE canonical_url = ?", (game_url,))
        db.record_fetch_success(
            data_conn,
            original_url=game_url,
            canonical_url=game_url,
            url_type="game",
            status_code=200,
            content_hash="abc",
            raw_file_path="raw.html",
        )
        data_conn.commit()

        db.init_storage(data_conn, crawl_conn)

        assert db.table_count(crawl_conn, "queue") == 1
        assert db.table_count(crawl_conn, "fetches") == 1
        migrated = crawl_conn.execute(
            "SELECT canonical_url, air_date FROM queue"
        ).fetchone()
        assert migrated["canonical_url"] == game_url
        assert migrated["air_date"] == "2024-01-01"
        assert not db.table_exists(data_conn, "queue")
        assert not db.table_exists(data_conn, "fetches")
    finally:
        data_conn.close()
        crawl_conn.close()
