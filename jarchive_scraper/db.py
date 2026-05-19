from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
import random
import re
import sqlite3
from typing import Iterable, Iterator

from .config import ScraperConfig


QUEUE_STATUSES = {"pending", "fetched", "failed", "parsed"}
URL_TYPES = {"season_index", "season", "game"}
PARSER_VERSION = "2026-05-18.1"
CRAWL_TABLES = ("fetches", "queue", "fetch_attempts", "parse_errors")
NAME_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?", re.IGNORECASE)
FETCH_SELECT_COLUMNS = """
    id,
    original_url,
    COALESCE(canonical_url, original_url) AS canonical_url,
    canonical_url AS stored_canonical_url,
    url_type,
    status_code,
    content_hash,
    raw_file_path,
    parser_state,
    error
"""
RESPONSE_NAME_ALIASES = {
    "al": ("alan", "allen", "alfred", "alexander"),
    "alex": ("alexander", "alexandra", "alexandria"),
    "andy": ("andrew", "andrea"),
    "barb": ("barbara",),
    "ben": ("benjamin",),
    "beth": ("elizabeth",),
    "bill": ("william",),
    "bob": ("robert",),
    "bobby": ("robert",),
    "cathy": ("catherine", "katherine", "kathryn"),
    "chris": ("christopher", "christine", "christina", "christian"),
    "cindy": ("cynthia",),
    "dan": ("daniel", "danielle"),
    "dave": ("david",),
    "deb": ("deborah", "debra"),
    "ed": ("edward", "edwin", "edmund"),
    "frank": ("francis",),
    "jim": ("james",),
    "joe": ("joseph",),
    "kate": ("katherine", "kathryn", "catherine"),
    "kathy": ("katherine", "kathryn", "catherine"),
    "ken": ("kenneth",),
    "liz": ("elizabeth",),
    "mike": ("michael",),
    "nick": ("nicholas",),
    "pat": ("patrick", "patricia"),
    "rob": ("robert",),
    "ron": ("ronald",),
    "sam": ("samuel", "samantha"),
    "steve": ("stephen", "steven"),
    "sue": ("susan", "suzanne"),
    "tom": ("thomas",),
    "tony": ("anthony",),
}


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
            FOREIGN KEY (season_id) REFERENCES seasons(season_id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS contestants (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            name TEXT NOT NULL,
            notes TEXT,
            FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE,
            UNIQUE (game_id, name)
        );

        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            round_order INTEGER NOT NULL,
            name TEXT NOT NULL,
            board_position INTEGER NOT NULL,
            FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE,
            UNIQUE (game_id, round_order, board_position)
        );

        CREATE TABLE IF NOT EXISTS clues (
            id INTEGER PRIMARY KEY,
            category_id INTEGER NOT NULL,
            row_value INTEGER,
            clue_text TEXT,
            correct_response TEXT,
            is_daily_double INTEGER NOT NULL DEFAULT 0,
            is_final_jeopardy INTEGER NOT NULL DEFAULT 0,
            is_triple_stumper INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
            UNIQUE (category_id, row_value)
        );

        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY,
            clue_id INTEGER NOT NULL,
            contestant_id INTEGER NOT NULL,
            response_text TEXT,
            correctness INTEGER NOT NULL CHECK (correctness IN (0, 1)),
            FOREIGN KEY (clue_id) REFERENCES clues(id) ON DELETE CASCADE,
            FOREIGN KEY (contestant_id) REFERENCES contestants(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scores (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            contestant TEXT,
            score INTEGER,
            FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_games_air_date ON games(air_date);
        CREATE INDEX IF NOT EXISTS idx_categories_game_round ON categories(game_id, round_order);
        CREATE INDEX IF NOT EXISTS idx_clues_category ON clues(category_id);
        CREATE INDEX IF NOT EXISTS idx_responses_clue ON responses(clue_id);
        """
    )
    migrate_categories_schema(conn)
    migrate_responses_schema(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_responses_contestant ON responses(contestant_id)"
    )


def migrate_categories_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "categories"):
        return

    expected_columns = {
        "id",
        "game_id",
        "round_order",
        "name",
        "board_position",
    }
    if set(table_columns(conn, "categories")) == expected_columns:
        return

    conn.execute("DROP TABLE IF EXISTS responses")
    conn.execute("DROP TABLE IF EXISTS clues")
    conn.execute("DROP TABLE IF EXISTS categories")
    conn.executescript(
        """
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            round_order INTEGER NOT NULL,
            name TEXT NOT NULL,
            board_position INTEGER NOT NULL,
            FOREIGN KEY (game_id) REFERENCES games(game_id) ON DELETE CASCADE,
            UNIQUE (game_id, round_order, board_position)
        );

        CREATE TABLE clues (
            id INTEGER PRIMARY KEY,
            category_id INTEGER NOT NULL,
            row_value INTEGER,
            clue_text TEXT,
            correct_response TEXT,
            is_daily_double INTEGER NOT NULL DEFAULT 0,
            is_final_jeopardy INTEGER NOT NULL DEFAULT 0,
            is_triple_stumper INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE,
            UNIQUE (category_id, row_value)
        );

        CREATE TABLE responses (
            id INTEGER PRIMARY KEY,
            clue_id INTEGER NOT NULL,
            contestant_id INTEGER NOT NULL,
            response_text TEXT,
            correctness INTEGER NOT NULL CHECK (correctness IN (0, 1)),
            FOREIGN KEY (clue_id) REFERENCES clues(id) ON DELETE CASCADE,
            FOREIGN KEY (contestant_id) REFERENCES contestants(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_categories_game_round ON categories(game_id, round_order);
        CREATE INDEX IF NOT EXISTS idx_clues_category ON clues(category_id);
        CREATE INDEX IF NOT EXISTS idx_responses_clue ON responses(clue_id);
        CREATE INDEX IF NOT EXISTS idx_responses_contestant ON responses(contestant_id);
        """
    )


def migrate_responses_schema(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "responses"):
        return

    expected_columns = {
        "id",
        "clue_id",
        "contestant_id",
        "response_text",
        "correctness",
    }
    if set(table_columns(conn, "responses")) == expected_columns:
        return

    conn.execute("DROP TABLE responses")
    conn.executescript(
        """
        CREATE TABLE responses (
            id INTEGER PRIMARY KEY,
            clue_id INTEGER NOT NULL,
            contestant_id INTEGER NOT NULL,
            response_text TEXT,
            correctness INTEGER NOT NULL CHECK (correctness IN (0, 1)),
            FOREIGN KEY (clue_id) REFERENCES clues(id) ON DELETE CASCADE,
            FOREIGN KEY (contestant_id) REFERENCES contestants(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_responses_clue ON responses(clue_id);
        CREATE INDEX IF NOT EXISTS idx_responses_contestant ON responses(contestant_id);
        """
    )


def init_crawl_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS fetches (
            id INTEGER PRIMARY KEY,
            original_url TEXT NOT NULL,
            canonical_url TEXT,
            url_type TEXT NOT NULL CHECK (url_type IN ('season_index', 'season', 'game')),
            status_code INTEGER,
            content_hash TEXT,
            raw_file_path TEXT,
            parser_state TEXT NOT NULL DEFAULT 'unparsed',
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            url_type TEXT NOT NULL CHECK (url_type IN ('season_index', 'season', 'game')),
            status TEXT NOT NULL CHECK (status IN ('pending', 'fetched', 'failed', 'parsed')),
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
        """
    )
    migrate_queue_schema(conn)
    migrate_fetches_schema(conn)
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_queue_status_type ON queue(status, url_type);
        CREATE INDEX IF NOT EXISTS idx_queue_air_date ON queue(url_type, status, air_date);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fetches_effective_url
            ON fetches(COALESCE(canonical_url, original_url));
        CREATE INDEX IF NOT EXISTS idx_fetches_status ON fetches(status_code);
        CREATE INDEX IF NOT EXISTS idx_fetches_parser_state ON fetches(parser_state);
        CREATE INDEX IF NOT EXISTS idx_fetch_attempts_attempted_at ON fetch_attempts(attempted_at);
        """
    )


def migrate_queue_schema(conn: sqlite3.Connection) -> None:
    expected_columns = [
        "id",
        "canonical_url",
        "url_type",
        "status",
        "updated_at",
        "attempts",
        "last_error",
        "air_date",
    ]
    if table_columns(conn, "queue") == expected_columns:
        return

    source_columns = set(table_columns(conn, "queue"))
    updated_at_expr = "updated_at" if "updated_at" in source_columns else "?"
    if "updated_at" not in source_columns and "discovered_at" in source_columns:
        updated_at_expr = "discovered_at"
    attempts_expr = "COALESCE(attempts, 0)" if "attempts" in source_columns else "0"
    last_error_expr = "last_error" if "last_error" in source_columns else "NULL"
    air_date_expr = "air_date" if "air_date" in source_columns else "NULL"

    conn.execute(
        """
        CREATE TABLE queue_new (
            id INTEGER PRIMARY KEY,
            canonical_url TEXT NOT NULL UNIQUE,
            url_type TEXT NOT NULL CHECK (url_type IN ('season_index', 'season', 'game')),
            status TEXT NOT NULL CHECK (status IN ('pending', 'fetched', 'failed', 'parsed')),
            updated_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            air_date TEXT
        )
        """
    )
    conn.execute(
        f"""
        INSERT INTO queue_new
            (id, canonical_url, url_type, status, updated_at, attempts, last_error, air_date)
        SELECT
            id,
            canonical_url,
            url_type,
            status,
            {updated_at_expr},
            {attempts_expr},
            {last_error_expr},
            {air_date_expr}
        FROM queue
        """,
        (() if "updated_at" in source_columns or "discovered_at" in source_columns else (utc_now(),)),
    )
    conn.execute("DROP TABLE queue")
    conn.execute("ALTER TABLE queue_new RENAME TO queue")


def migrate_fetches_schema(conn: sqlite3.Connection) -> None:
    expected_columns = [
        "id",
        "original_url",
        "canonical_url",
        "url_type",
        "status_code",
        "content_hash",
        "raw_file_path",
        "parser_state",
        "error",
    ]
    column_info = {
        row["name"]: row
        for row in conn.execute("PRAGMA table_info(fetches)")
    }
    if (
        table_columns(conn, "fetches") == expected_columns
        and not column_info["canonical_url"]["notnull"]
    ):
        normalize_fetch_canonical_urls(conn)
        return

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            """
            CREATE TABLE fetches_new (
                id INTEGER PRIMARY KEY,
                original_url TEXT NOT NULL,
                canonical_url TEXT,
                url_type TEXT NOT NULL CHECK (url_type IN ('season_index', 'season', 'game')),
                status_code INTEGER,
                content_hash TEXT,
                raw_file_path TEXT,
                parser_state TEXT NOT NULL DEFAULT 'unparsed',
                error TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO fetches_new
                (id, original_url, canonical_url, url_type, status_code, content_hash,
                 raw_file_path, parser_state, error)
            SELECT
                id,
                original_url,
                NULLIF(canonical_url, original_url),
                url_type,
                status_code,
                content_hash,
                raw_file_path,
                COALESCE(parser_state, 'unparsed'),
                error
            FROM fetches
            """
        )
        conn.execute("DROP TABLE fetches")
        conn.execute("ALTER TABLE fetches_new RENAME TO fetches")
    finally:
        conn.execute(f"PRAGMA foreign_keys = {foreign_keys}")


def normalize_fetch_canonical_urls(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        UPDATE fetches
        SET canonical_url = NULL
        WHERE canonical_url = original_url
        """
    )


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


def resolve_response_contestant_id(
    contestants: Iterable[tuple[int, str]],
    raw_name: str | None,
) -> int | None:
    if not raw_name:
        return None

    contestant_rows = [
        (contestant_id, name, name_tokens(name))
        for contestant_id, name in contestants
    ]
    response_tokens = name_tokens(raw_name)
    if not response_tokens:
        return None

    exact_name = " ".join(response_tokens)
    exact_matches = [
        contestant_id
        for contestant_id, _name, tokens in contestant_rows
        if " ".join(tokens) == exact_name
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    response_key = response_tokens[0]
    for token in (response_key, *RESPONSE_NAME_ALIASES.get(response_key, ())):
        first_name_matches = [
            contestant_id
            for contestant_id, _name, tokens in contestant_rows
            if tokens and tokens[0] == token
        ]
        if len(first_name_matches) == 1:
            return first_name_matches[0]

    token_matches = [
        contestant_id
        for contestant_id, _name, tokens in contestant_rows
        if response_key in tokens
    ]
    if len(token_matches) == 1:
        return token_matches[0]

    return None


def name_tokens(value: str | None) -> list[str]:
    if not value:
        return []
    return [match.group(0).casefold() for match in NAME_TOKEN_RE.finditer(value)]


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
    if table_exists(crawl_conn, "fetches"):
        normalize_fetch_canonical_urls(crawl_conn)


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
    game_columns = set(table_columns(data_conn, "games"))
    if "source_url" in game_columns:
        rows = data_conn.execute(
            """
            SELECT source_url, air_date
            FROM games
            WHERE source_url IS NOT NULL
              AND air_date IS NOT NULL
            """
        ).fetchall()
    else:
        rows = data_conn.execute(
            """
            SELECT
              'https://j-archive.com/showgame.php?game_id=' || game_id AS source_url,
              air_date
            FROM games
            WHERE air_date IS NOT NULL
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
        if air_date is not None:
            conn.execute(
                """
                UPDATE queue
                SET air_date = COALESCE(air_date, ?),
                    updated_at = ?
                WHERE canonical_url = ?
                """,
                (air_date, now, canonical_url),
            )
        return False

    conn.execute(
        """
        INSERT OR IGNORE INTO queue
            (canonical_url, url_type, status, updated_at, air_date)
        VALUES (?, ?, 'pending', ?, ?)
        """,
        (canonical_url, url_type, now, air_date),
    )
    return True


def successful_fetch_exists(conn: sqlite3.Connection, canonical_url: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM fetches
        WHERE COALESCE(canonical_url, original_url) = ?
          AND status_code = 200
          AND raw_file_path IS NOT NULL
        """,
        (canonical_url,),
    ).fetchone()
    return row is not None


def get_fetch(conn: sqlite3.Connection, canonical_url: str) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT {FETCH_SELECT_COLUMNS}
        FROM fetches
        WHERE COALESCE(canonical_url, original_url) = ?
        """,
        (canonical_url,),
    ).fetchone()


def stored_fetch_canonical_url(original_url: str, canonical_url: str) -> str | None:
    return None if original_url == canonical_url else canonical_url


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
    conn.execute(
        """
        INSERT INTO fetches
            (original_url, canonical_url, url_type, status_code, content_hash,
             raw_file_path, parser_state, error)
        VALUES (?, ?, ?, ?, ?, ?, 'unparsed', NULL)
        ON CONFLICT DO UPDATE SET
            original_url = excluded.original_url,
            canonical_url = excluded.canonical_url,
            url_type = excluded.url_type,
            status_code = excluded.status_code,
            content_hash = excluded.content_hash,
            raw_file_path = excluded.raw_file_path,
            error = NULL
        """,
        (
            original_url,
            stored_fetch_canonical_url(original_url, canonical_url),
            url_type,
            status_code,
            content_hash,
            raw_file_path,
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
             raw_file_path, parser_state, error)
        VALUES (?, ?, ?, ?, NULL, NULL, 'unparsed', ?)
        ON CONFLICT DO UPDATE SET
            original_url = excluded.original_url,
            canonical_url = excluded.canonical_url,
            url_type = excluded.url_type,
            status_code = excluded.status_code,
            error = excluded.error
        """,
        (
            original_url,
            stored_fetch_canonical_url(original_url, canonical_url),
            url_type,
            status_code,
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
                 id ASC
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
              SELECT COALESCE(canonical_url, original_url) FROM fetches
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
            f"""
            SELECT {FETCH_SELECT_COLUMNS}
            FROM fetches
            WHERE COALESCE(canonical_url, original_url) = ?
              AND status_code = 200
              AND raw_file_path IS NOT NULL
            """,
            (canonical_url,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {FETCH_SELECT_COLUMNS}
            FROM fetches
            WHERE status_code = 200 AND raw_file_path IS NOT NULL
            ORDER BY CASE url_type WHEN 'season_index' THEN 0 WHEN 'season' THEN 1 ELSE 2 END,
                     id ASC
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
                (game_id, show_number, air_date, season_id, title, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id) DO UPDATE SET
                show_number = COALESCE(excluded.show_number, games.show_number),
                air_date = COALESCE(excluded.air_date, games.air_date),
                season_id = COALESCE(excluded.season_id, games.season_id),
                title = COALESCE(excluded.title, games.title),
                notes = COALESCE(excluded.notes, games.notes)
            """,
            (
                game.game_id,
                game.show_number,
                game.air_date,
                season_id,
                game.title,
                game.notes,
            ),
        )
        count += 1
    return count


def replace_game_detail(conn: sqlite3.Connection, detail: object) -> None:
    season_id = existing_season_id(conn, detail.season_id)
    conn.execute(
        """
        INSERT INTO games
            (game_id, show_number, air_date, season_id, title, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            show_number = COALESCE(excluded.show_number, games.show_number),
            air_date = COALESCE(excluded.air_date, games.air_date),
            season_id = COALESCE(excluded.season_id, games.season_id),
            title = COALESCE(excluded.title, games.title),
            notes = COALESCE(excluded.notes, games.notes)
        """,
        (
            detail.game_id,
            detail.show_number,
            detail.air_date,
            season_id,
            detail.title,
            detail.notes,
        ),
    )
    conn.execute("DELETE FROM contestants WHERE game_id = ?", (detail.game_id,))
    conn.execute("DELETE FROM scores WHERE game_id = ?", (detail.game_id,))
    conn.execute("DELETE FROM categories WHERE game_id = ?", (detail.game_id,))

    contestants: list[tuple[int, str]] = []
    for contestant in detail.contestants:
        cur = conn.execute(
            """
            INSERT INTO contestants (game_id, name, notes)
            VALUES (?, ?, ?)
            """,
            (
                detail.game_id,
                contestant.name,
                contestant.notes,
            ),
        )
        contestants.append((int(cur.lastrowid), contestant.name))

    for score in detail.scores:
        conn.execute(
            """
            INSERT INTO scores (game_id, stage, contestant, score)
            VALUES (?, ?, ?, ?)
            """,
            (detail.game_id, score.stage, score.contestant, score.score),
        )

    for round_data in detail.rounds:
        for category in round_data.categories:
            cur = conn.execute(
                """
                INSERT INTO categories
                    (game_id, round_order, name, board_position)
                VALUES (?, ?, ?, ?)
                """,
                (
                    detail.game_id,
                    round_data.round_order,
                    category.name,
                    category.board_position,
                ),
            )
            category_id = cur.lastrowid
            for clue in category.clues:
                cur = conn.execute(
                    """
                    INSERT INTO clues
                        (category_id, row_value, clue_text, correct_response,
                         is_daily_double, is_final_jeopardy, is_triple_stumper)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        category_id,
                        clue.row,
                        clue.clue_text,
                        clue.correct_response,
                        1 if clue.is_daily_double else 0,
                        1 if clue.is_final_jeopardy else 0,
                        1 if clue.is_triple_stumper else 0,
                    ),
                )
                clue_id = cur.lastrowid
                for response in clue.responses:
                    if response.correctness not in (0, 1):
                        continue
                    contestant_id = resolve_response_contestant_id(
                        contestants,
                        response.contestant,
                    )
                    if contestant_id is None:
                        continue
                    conn.execute(
                        """
                        INSERT INTO responses
                            (clue_id, contestant_id, response_text, correctness)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            clue_id,
                            contestant_id,
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
