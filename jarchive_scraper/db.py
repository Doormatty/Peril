from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
import random
import sqlite3
from typing import Iterable, Iterator

from .config import ScraperConfig


QUEUE_STATUSES = {"pending", "fetched", "failed", "parsed"}
URL_TYPES = {"season_index", "season", "game"}
PARSER_VERSION = "2026-05-16.1"
CRAWL_TABLES = ("fetches", "queue", "fetch_attempts", "parse_errors")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    try:
        conn.execute("BEGIN")
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    init_main_db(conn)
    init_crawl_db(conn)
    conn.commit()


def init_storage(
    data_conn: sqlite3.Connection,
    crawl_conn: sqlite3.Connection,
) -> None:
    init_main_db(data_conn)
    init_crawl_db(crawl_conn)
    migrate_legacy_crawl_state(data_conn, crawl_conn)
    sync_queue_air_dates(data_conn, crawl_conn)
    drop_migrated_crawl_tables(data_conn, crawl_conn)
    data_conn.commit()
    crawl_conn.commit()


def init_main_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS seasons (
            season_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            date_start TEXT,
            date_end TEXT,
            date_range_text TEXT,
            archived_game_count INTEGER,
            source_url TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            show_number TEXT,
            air_date TEXT,
            season_id TEXT,
            title TEXT,
            notes TEXT,
            source_url TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (season_id) REFERENCES seasons(season_id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS contestants (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            name TEXT NOT NULL,
            position_order INTEGER NOT NULL,
            notes TEXT,
            FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE,
            UNIQUE (game_id, position_order)
        );

        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            name TEXT NOT NULL,
            round_order INTEGER NOT NULL,
            FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE,
            UNIQUE (game_id, name)
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            round_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            board_position INTEGER NOT NULL,
            FOREIGN KEY (round_id) REFERENCES rounds(id) ON DELETE CASCADE,
            UNIQUE (round_id, board_position)
        );

        CREATE TABLE IF NOT EXISTS clues (
            id INTEGER PRIMARY KEY,
            category_id INTEGER NOT NULL,
            row_value INTEGER,
            dollar_value TEXT,
            clue_text TEXT,
            correct_response TEXT,
            clue_order INTEGER NOT NULL,
            is_daily_double INTEGER NOT NULL DEFAULT 0,
            source_clue_id TEXT,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
            UNIQUE (category_id, row_value, source_clue_id)
        );

        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY,
            clue_id INTEGER,
            contestant TEXT,
            response_text TEXT NOT NULL,
            correctness TEXT,
            FOREIGN KEY (clue_id) REFERENCES clues(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            contestant TEXT,
            score INTEGER,
            FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_games_source_url ON games(source_url);
        CREATE INDEX IF NOT EXISTS idx_games_air_date ON games(air_date);
        """
    )


def init_crawl_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fetches (
            id INTEGER PRIMARY KEY,
            original_url TEXT NOT NULL,
            canonical_url TEXT NOT NULL UNIQUE,
            url_type TEXT NOT NULL CHECK (url_type IN ('season_index', 'season', 'game')),
            status_code INTEGER,
            content_hash TEXT,
            raw_file_path TEXT,
            first_fetched_at TEXT,
            last_attempted_at TEXT NOT NULL,
            parser_state TEXT NOT NULL DEFAULT 'unparsed',
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            url_type TEXT NOT NULL CHECK (url_type IN ('season_index', 'season', 'game')),
            status TEXT NOT NULL CHECK (status IN ('pending', 'fetched', 'failed', 'parsed')),
            discovered_from TEXT,
            discovered_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            air_date TEXT
        );

        CREATE TABLE IF NOT EXISTS fetch_attempts (
            id INTEGER PRIMARY KEY,
            canonical_url TEXT NOT NULL,
            attempted_at TEXT NOT NULL,
            status_code INTEGER,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS parse_errors (
            id INTEGER PRIMARY KEY,
            fetch_id INTEGER NOT NULL,
            parser_version TEXT NOT NULL,
            error_text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (fetch_id) REFERENCES fetches(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_queue_status_type ON queue(status, url_type);
        CREATE INDEX IF NOT EXISTS idx_queue_air_date ON queue(url_type, status, air_date);
        CREATE INDEX IF NOT EXISTS idx_fetches_status ON fetches(status_code);
        CREATE INDEX IF NOT EXISTS idx_fetches_parser_state ON fetches(parser_state);
        CREATE INDEX IF NOT EXISTS idx_fetch_attempts_attempted_at ON fetch_attempts(attempted_at);
        """
    )
    add_missing_column(conn, "queue", "air_date", "TEXT")
    drop_legacy_fetch_columns(conn)


def drop_legacy_fetch_columns(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(fetches)")}
    for column in ("response_headers_json", "source_permission_note"):
        if column in columns:
            conn.execute(f"ALTER TABLE fetches DROP COLUMN {column}")


def add_missing_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone()
    return row is not None


def table_count(conn: sqlite3.Connection, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    if not table_exists(conn, table):
        return []
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def migrate_legacy_crawl_state(
    data_conn: sqlite3.Connection,
    crawl_conn: sqlite3.Connection,
) -> None:
    for table in CRAWL_TABLES:
        if not table_exists(data_conn, table) or table_count(crawl_conn, table) > 0:
            continue

        source_columns = set(table_columns(data_conn, table))
        target_columns = table_columns(crawl_conn, table)
        columns = [column for column in target_columns if column in source_columns]
        if not columns:
            continue

        column_sql = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        rows = data_conn.execute(f"SELECT {column_sql} FROM {table}").fetchall()
        if not rows:
            continue

        crawl_conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})",
            ([row[column] for column in columns] for row in rows),
        )


def drop_migrated_crawl_tables(
    data_conn: sqlite3.Connection,
    crawl_conn: sqlite3.Connection,
) -> None:
    for table in reversed(CRAWL_TABLES):
        if not table_exists(data_conn, table):
            continue
        if table_count(crawl_conn, table) < table_count(data_conn, table):
            continue
        data_conn.execute(f"DROP TABLE {table}")


def sync_queue_air_dates(
    data_conn: sqlite3.Connection,
    crawl_conn: sqlite3.Connection,
) -> int:
    if not table_exists(data_conn, "games") or not table_exists(crawl_conn, "queue"):
        return 0
    rows = data_conn.execute(
        """
        SELECT source_url, air_date
        FROM games
        WHERE source_url IS NOT NULL
          AND air_date IS NOT NULL
        """
    ).fetchall()
    cur = crawl_conn.executemany(
        """
        UPDATE queue
        SET air_date = ?
        WHERE canonical_url = ?
          AND url_type = 'game'
          AND air_date IS NULL
        """,
        ((row["air_date"], row["source_url"]) for row in rows),
    )
    return cur.rowcount


def ensure_storage(config: ScraperConfig) -> None:
    config.raw_dir.mkdir(parents=True, exist_ok=True)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    assert config.crawl_db_path is not None
    config.crawl_db_path.parent.mkdir(parents=True, exist_ok=True)


def enqueue(
    conn: sqlite3.Connection,
    canonical_url: str,
    url_type: str,
    discovered_from: str | None = None,
    air_date: str | None = None,
) -> bool:
    if url_type not in URL_TYPES:
        raise ValueError(f"Unsupported URL type: {url_type}")
    now = utc_now()
    existing = conn.execute(
        "SELECT 1 FROM queue WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()
    if existing is not None:
        if discovered_from is not None or air_date is not None:
            conn.execute(
                """
                UPDATE queue
                SET discovered_from = COALESCE(discovered_from, ?),
                    air_date = COALESCE(air_date, ?),
                    updated_at = ?
                WHERE canonical_url = ?
                """,
                (discovered_from, air_date, now, canonical_url),
            )
        return False

    conn.execute(
        """
        INSERT OR IGNORE INTO queue
            (canonical_url, url_type, status, discovered_from, discovered_at, updated_at, air_date)
        VALUES (?, ?, 'pending', ?, ?, ?, ?)
        """,
        (canonical_url, url_type, discovered_from, now, now, air_date),
    )
    return True


def successful_fetch_exists(conn: sqlite3.Connection, canonical_url: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM fetches
        WHERE canonical_url = ? AND status_code = 200 AND raw_file_path IS NOT NULL
        """,
        (canonical_url,),
    ).fetchone()
    return row is not None


def get_fetch(conn: sqlite3.Connection, canonical_url: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM fetches WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()


def record_fetch_success(
    conn: sqlite3.Connection,
    *,
    original_url: str,
    canonical_url: str,
    url_type: str,
    status_code: int,
    content_hash: str,
    raw_file_path: str,
) -> sqlite3.Row:
    now = utc_now()
    record_fetch_attempt(conn, canonical_url, now, status_code, None)
    existing = get_fetch(conn, canonical_url)
    first_fetched_at = (
        existing["first_fetched_at"]
        if existing and existing["first_fetched_at"]
        else now
    )
    conn.execute(
        """
        INSERT INTO fetches
            (original_url, canonical_url, url_type, status_code, content_hash,
             raw_file_path, first_fetched_at, last_attempted_at, parser_state, error)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'unparsed', NULL)
        ON CONFLICT(canonical_url) DO UPDATE SET
            original_url = excluded.original_url,
            url_type = excluded.url_type,
            status_code = excluded.status_code,
            content_hash = excluded.content_hash,
            raw_file_path = excluded.raw_file_path,
            first_fetched_at = COALESCE(fetches.first_fetched_at, excluded.first_fetched_at),
            last_attempted_at = excluded.last_attempted_at,
            error = NULL
        """,
        (
            original_url,
            canonical_url,
            url_type,
            status_code,
            content_hash,
            raw_file_path,
            first_fetched_at,
            now,
        ),
    )
    mark_queue_status(conn, canonical_url, "fetched", increment_attempts=True)
    row = get_fetch(conn, canonical_url)
    assert row is not None
    return row


def record_fetch_failure(
    conn: sqlite3.Connection,
    *,
    original_url: str,
    canonical_url: str,
    url_type: str,
    status_code: int | None,
    error: str,
) -> None:
    now = utc_now()
    record_fetch_attempt(conn, canonical_url, now, status_code, error)
    conn.execute(
        """
        INSERT INTO fetches
            (original_url, canonical_url, url_type, status_code, content_hash,
             raw_file_path, first_fetched_at, last_attempted_at, parser_state, error)
        VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, 'unparsed', ?)
        ON CONFLICT(canonical_url) DO UPDATE SET
            original_url = excluded.original_url,
            url_type = excluded.url_type,
            status_code = excluded.status_code,
            last_attempted_at = excluded.last_attempted_at,
            error = excluded.error
        """,
        (
            original_url,
            canonical_url,
            url_type,
            status_code,
            now,
            error,
        ),
    )
    mark_queue_status(conn, canonical_url, "failed", error, increment_attempts=True)


def mark_queue_status(
    conn: sqlite3.Connection,
    canonical_url: str,
    status: str,
    error: str | None = None,
    *,
    increment_attempts: bool = False,
) -> None:
    if status not in QUEUE_STATUSES:
        raise ValueError(f"Unsupported queue status: {status}")
    conn.execute(
        f"""
        UPDATE queue
        SET status = ?,
            updated_at = ?,
            last_error = ?,
            attempts = attempts + ?
        WHERE canonical_url = ?
        """,
        (status, utc_now(), error, 1 if increment_attempts else 0, canonical_url),
    )


def record_fetch_attempt(
    conn: sqlite3.Connection,
    canonical_url: str,
    attempted_at: str,
    status_code: int | None,
    error: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_attempts (canonical_url, attempted_at, status_code, error)
        VALUES (?, ?, ?, ?)
        """,
        (canonical_url, attempted_at, status_code, error),
    )


def next_pending_url(
    conn: sqlite3.Connection,
    *,
    recent_game_years: int = 10,
    recent_game_bias: float = 0.9,
    today: date | None = None,
    random_value: float | None = None,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM queue
        WHERE status = 'pending' AND url_type IN ('season_index', 'season')
        ORDER BY CASE url_type WHEN 'season_index' THEN 0 WHEN 'season' THEN 1 ELSE 2 END,
                 discovered_at ASC, id ASC
        LIMIT 1
        """
    ).fetchone()
    if row is not None:
        return row

    if random_value is None:
        random_value = random.random()
    if recent_game_bias > 0 and random_value < recent_game_bias:
        recent_row = conn.execute(
            """
            SELECT * FROM queue
            WHERE status = 'pending'
              AND url_type = 'game'
              AND air_date IS NOT NULL
              AND air_date >= ?
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (recent_game_cutoff(recent_game_years, today=today),),
        ).fetchone()
        if recent_row is not None:
            return recent_row

    return conn.execute(
        """
        SELECT * FROM queue
        WHERE status = 'pending' AND url_type = 'game'
        ORDER BY RANDOM()
        LIMIT 1
        """
    ).fetchone()


def recent_game_cutoff(years: int, *, today: date | None = None) -> str:
    current = today or datetime.now(timezone.utc).date()
    try:
        cutoff = current.replace(year=current.year - years)
    except ValueError:
        cutoff = current.replace(year=current.year - years, month=2, day=28)
    return cutoff.isoformat()


def count_requests_today(conn: sqlite3.Connection) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    row = conn.execute(
        """
        SELECT COUNT(*) AS count FROM fetch_attempts
        WHERE substr(attempted_at, 1, 10) = ?
        """,
        (today,),
    ).fetchone()
    return int(row["count"])


def reset_retryable_failures(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        """
        UPDATE queue
        SET status = 'pending', updated_at = ?, last_error = NULL
        WHERE status = 'failed'
          AND canonical_url IN (
              SELECT canonical_url FROM fetches
              WHERE status_code IS NULL OR status_code >= 500
          )
        """,
        (utc_now(),),
    )
    return cur.rowcount


def iter_fetches_for_parse(
    conn: sqlite3.Connection,
    canonical_url: str | None = None,
) -> Iterable[sqlite3.Row]:
    if canonical_url:
        rows = conn.execute(
            """
            SELECT * FROM fetches
            WHERE canonical_url = ? AND status_code = 200 AND raw_file_path IS NOT NULL
            """,
            (canonical_url,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM fetches
            WHERE status_code = 200 AND raw_file_path IS NOT NULL
            ORDER BY CASE url_type WHEN 'season_index' THEN 0 WHEN 'season' THEN 1 ELSE 2 END,
                     first_fetched_at ASC
            """
        ).fetchall()
    return rows


def set_parser_state(
    conn: sqlite3.Connection, fetch_id: int, canonical_url: str, state: str
) -> None:
    conn.execute(
        "UPDATE fetches SET parser_state = ? WHERE id = ?", (state, fetch_id)
    )
    if state == "parsed":
        mark_queue_status(conn, canonical_url, "parsed")


def record_parse_error(conn: sqlite3.Connection, fetch_id: int, error_text: str) -> None:
    conn.execute(
        """
        INSERT INTO parse_errors (fetch_id, parser_version, error_text, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (fetch_id, PARSER_VERSION, error_text, utc_now()),
    )
    conn.execute(
        "UPDATE fetches SET parser_state = 'error' WHERE id = ?", (fetch_id,)
    )


def upsert_seasons(conn: sqlite3.Connection, seasons: Iterable[object]) -> int:
    count = 0
    for season in seasons:
        conn.execute(
            """
            INSERT INTO seasons
                (season_id, name, date_start, date_end, date_range_text,
                 archived_game_count, source_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(season_id) DO UPDATE SET
                name = excluded.name,
                date_start = excluded.date_start,
                date_end = excluded.date_end,
                date_range_text = excluded.date_range_text,
                archived_game_count = excluded.archived_game_count,
                source_url = excluded.source_url,
                updated_at = excluded.updated_at
            """,
            (
                season.season_id,
                season.name,
                season.date_start,
                season.date_end,
                season.date_range_text,
                season.archived_game_count,
                season.url,
                utc_now(),
            ),
        )
        count += 1
    return count


def upsert_game_summaries(conn: sqlite3.Connection, games: Iterable[object]) -> int:
    count = 0
    for game in games:
        season_id = existing_season_id(conn, game.season_id)
        conn.execute(
            """
            INSERT INTO games
                (game_id, show_number, air_date, season_id, title, notes, source_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                show_number = COALESCE(excluded.show_number, games.show_number),
                air_date = COALESCE(excluded.air_date, games.air_date),
                season_id = COALESCE(excluded.season_id, games.season_id),
                title = COALESCE(excluded.title, games.title),
                notes = COALESCE(excluded.notes, games.notes),
                source_url = excluded.source_url,
                updated_at = excluded.updated_at
            """,
            (
                game.game_id,
                game.show_number,
                game.air_date,
                season_id,
                game.title,
                game.notes,
                game.url,
                utc_now(),
            ),
        )
        count += 1
    return count


def replace_game_detail(conn: sqlite3.Connection, detail: object) -> None:
    season_id = existing_season_id(conn, detail.season_id)
    conn.execute(
        """
        INSERT INTO games
            (game_id, show_number, air_date, season_id, title, notes, source_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            show_number = COALESCE(excluded.show_number, games.show_number),
            air_date = COALESCE(excluded.air_date, games.air_date),
            season_id = COALESCE(excluded.season_id, games.season_id),
            title = COALESCE(excluded.title, games.title),
            notes = COALESCE(excluded.notes, games.notes),
            source_url = excluded.source_url,
            updated_at = excluded.updated_at
        """,
        (
            detail.game_id,
            detail.show_number,
            detail.air_date,
            season_id,
            detail.title,
            detail.notes,
            detail.url,
            utc_now(),
        ),
    )
    conn.execute("DELETE FROM contestants WHERE game_id = ?", (detail.game_id,))
    conn.execute("DELETE FROM scores WHERE game_id = ?", (detail.game_id,))
    conn.execute("DELETE FROM rounds WHERE game_id = ?", (detail.game_id,))

    for contestant in detail.contestants:
        conn.execute(
            """
            INSERT INTO contestants (game_id, name, position_order, notes)
            VALUES (?, ?, ?, ?)
            """,
            (
                detail.game_id,
                contestant.name,
                contestant.position_order,
                contestant.notes,
            ),
        )

    for score in detail.scores:
        conn.execute(
            """
            INSERT INTO scores (game_id, stage, contestant, score)
            VALUES (?, ?, ?, ?)
            """,
            (detail.game_id, score.stage, score.contestant, score.score),
        )

    for round_data in detail.rounds:
        cur = conn.execute(
            """
            INSERT INTO rounds (game_id, name, round_order)
            VALUES (?, ?, ?)
            """,
            (detail.game_id, round_data.name, round_data.round_order),
        )
        round_id = cur.lastrowid
        for category in round_data.categories:
            cur = conn.execute(
                """
                INSERT INTO categories (round_id, name, board_position)
                VALUES (?, ?, ?)
                """,
                (round_id, category.name, category.board_position),
            )
            category_id = cur.lastrowid
            for clue in category.clues:
                cur = conn.execute(
                    """
                    INSERT INTO clues
                        (category_id, row_value, dollar_value, clue_text, correct_response,
                         clue_order, is_daily_double, source_clue_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        category_id,
                        clue.row,
                        clue.value,
                        clue.clue_text,
                        clue.correct_response,
                        clue.clue_order,
                        1 if clue.is_daily_double else 0,
                        clue.source_clue_id,
                    ),
                )
                clue_id = cur.lastrowid
                for response in clue.responses:
                    conn.execute(
                        """
                        INSERT INTO responses
                            (clue_id, contestant, response_text, correctness)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            clue_id,
                            response.contestant,
                            response.response_text,
                            response.correctness,
                        ),
                )


def existing_season_id(conn: sqlite3.Connection, season_id: str | None) -> str | None:
    if season_id is None:
        return None
    row = conn.execute(
        "SELECT 1 FROM seasons WHERE season_id = ?", (season_id,)
    ).fetchone()
    return season_id if row is not None else None


def status_counts(
    data_conn: sqlite3.Connection,
    crawl_conn: sqlite3.Connection | None = None,
    *,
    recent_game_years: int = 10,
) -> dict[str, object]:
    crawl_conn = crawl_conn or data_conn
    queue_rows = crawl_conn.execute(
        """
        SELECT url_type, status, COUNT(*) AS count
        FROM queue GROUP BY url_type, status ORDER BY url_type, status
        """
    ).fetchall()
    fetch_rows = crawl_conn.execute(
        """
        SELECT COALESCE(CAST(status_code AS TEXT), 'network_error') AS status_code,
               COUNT(*) AS count
        FROM fetches GROUP BY status_code ORDER BY status_code
        """
    ).fetchall()
    parsed = {
        "seasons": data_conn.execute("SELECT COUNT(*) AS count FROM seasons").fetchone()["count"],
        "games": data_conn.execute("SELECT COUNT(*) AS count FROM games").fetchone()["count"],
        "clues": data_conn.execute("SELECT COUNT(*) AS count FROM clues").fetchone()["count"],
        "parse_errors": crawl_conn.execute("SELECT COUNT(*) AS count FROM parse_errors").fetchone()["count"],
    }
    cutoff = recent_game_cutoff(recent_game_years)
    crawl = {
        "requests_today": count_requests_today(crawl_conn),
        "pending_games_total": crawl_conn.execute(
            """
            SELECT COUNT(*) AS count FROM queue
            WHERE status = 'pending' AND url_type = 'game'
            """
        ).fetchone()["count"],
        "pending_games_with_dates": crawl_conn.execute(
            """
            SELECT COUNT(*) AS count FROM queue q
            WHERE q.status = 'pending'
              AND q.url_type = 'game'
              AND q.air_date IS NOT NULL
            """
        ).fetchone()["count"],
        "pending_recent_games": crawl_conn.execute(
            """
            SELECT COUNT(*) AS count FROM queue q
            WHERE q.status = 'pending'
              AND q.url_type = 'game'
              AND q.air_date IS NOT NULL
              AND q.air_date >= ?
            """,
            (cutoff,),
        ).fetchone()["count"],
        "recent_game_cutoff": cutoff,
    }
    return {
        "queue": [dict(row) for row in queue_rows],
        "fetches": [dict(row) for row in fetch_rows],
        "parsed": parsed,
        "crawl": crawl,
    }
