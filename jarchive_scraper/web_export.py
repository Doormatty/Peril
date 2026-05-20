from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable


EFFECTIVE_SEASON_ID_SQL = """
  COALESCE(
    CASE
      WHEN stored_season.season_id IS NOT NULL
        AND g.air_date IS NOT NULL
        AND stored_season.date_start IS NOT NULL
        AND stored_season.date_end IS NOT NULL
        AND g.air_date BETWEEN stored_season.date_start AND stored_season.date_end
      THEN g.season_id
    END,
    (
      SELECT date_season.season_id
      FROM seasons date_season
      WHERE g.air_date IS NOT NULL
        AND date_season.date_start IS NOT NULL
        AND date_season.date_end IS NOT NULL
        AND date_season.season_id NOT GLOB '*[^0-9]*'
        AND date_season.season_id <> ''
        AND g.air_date BETWEEN date_season.date_start AND date_season.date_end
      ORDER BY CAST(date_season.season_id AS INTEGER) DESC
      LIMIT 1
    ),
    g.season_id
  )
"""


@dataclass(frozen=True)
class ExportResult:
    catalog_path: Path
    shard_paths: list[Path]
    game_count: int
    season_count: int


def export_web_assets(
    source_db_path: Path,
    web_dir: Path,
    *,
    seasons_per_shard: int = 5,
) -> ExportResult:
    if seasons_per_shard < 1:
        raise ValueError("seasons_per_shard must be >= 1")

    web_dir.mkdir(parents=True, exist_ok=True)
    shards_dir = web_dir / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    for stale_shard in shards_dir.glob("jarchive_*.sqlite3"):
        stale_shard.unlink()

    with sqlite3.connect(source_db_path) as source_conn:
        source_conn.row_factory = sqlite3.Row
        games = _load_game_catalog(source_conn, seasons_per_shard)
        seasons = _load_season_catalog(source_conn)
        shards = _group_games_by_shard(games)
        shard_paths = [
            _write_shard(source_conn, shards_dir, shard_name, shard_games)
            for shard_name, shard_games in sorted(shards.items())
        ]

    catalog = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_database": source_db_path.name,
        "seasons_per_shard": seasons_per_shard,
        "shards": [
            {
                "name": shard_path.name,
                "path": f"shards/{shard_path.name}",
                "bytes": shard_path.stat().st_size,
            }
            for shard_path in shard_paths
        ],
        "seasons": seasons,
        "games": games,
    }
    catalog_path = web_dir / "catalog.json"
    catalog_path.write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf-8")

    return ExportResult(
        catalog_path=catalog_path,
        shard_paths=shard_paths,
        game_count=len(games),
        season_count=len(seasons),
    )


def _load_game_catalog(
    conn: sqlite3.Connection,
    seasons_per_shard: int,
) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        SELECT
          g.game_id,
          g.show_number,
          g.air_date,
          {EFFECTIVE_SEASON_ID_SQL} AS season_id,
          g.title,
          COUNT(c.id) AS clue_count
        FROM games g
        LEFT JOIN seasons stored_season ON stored_season.season_id = g.season_id
        JOIN categories ca ON ca.game_id = g.game_id
        JOIN clues c ON c.category_id = ca.id
        GROUP BY g.game_id
        ORDER BY
          g.air_date DESC,
          CAST(g.game_id AS INTEGER) DESC,
          g.game_id DESC
        """
    ).fetchall()
    return [
        {
            "game_id": row["game_id"],
            "show_number": row["show_number"],
            "air_date": row["air_date"],
            "season_id": row["season_id"],
            "title": row["title"],
            "clue_count": row["clue_count"],
            "shard": _shard_name(row["season_id"], seasons_per_shard),
        }
        for row in rows
    ]


def _load_season_catalog(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        f"""
        WITH effective_games AS (
          SELECT
            g.game_id,
            {EFFECTIVE_SEASON_ID_SQL} AS season_id
          FROM games g
          LEFT JOIN seasons stored_season ON stored_season.season_id = g.season_id
        ),
        known_counts AS (
          SELECT season_id, COUNT(*) AS known_game_count
          FROM effective_games
          GROUP BY season_id
        ),
        parsed_games AS (
          SELECT DISTINCT eg.game_id, eg.season_id
          FROM effective_games eg
          JOIN categories ca ON ca.game_id = eg.game_id
          JOIN clues c ON c.category_id = ca.id
        ),
        parsed_counts AS (
          SELECT season_id, COUNT(*) AS parsed_game_count
          FROM parsed_games
          GROUP BY season_id
        )
        SELECT
          s.season_id,
          s.name,
          s.date_start,
          s.date_end,
          s.date_range_text,
          s.archived_game_count,
          COALESCE(k.known_game_count, 0) AS known_game_count,
          COALESCE(p.parsed_game_count, 0) AS parsed_game_count
        FROM seasons s
        LEFT JOIN known_counts k ON k.season_id = s.season_id
        LEFT JOIN parsed_counts p ON p.season_id = s.season_id
        ORDER BY
          CAST(s.season_id AS INTEGER) DESC,
          s.season_id DESC
        """
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _group_games_by_shard(games: Iterable[dict[str, object]]) -> dict[str, list[str]]:
    shards: dict[str, list[str]] = {}
    for game in games:
        shard = str(game["shard"])
        game_ids = shards.setdefault(shard, [])
        game_ids.append(str(game["game_id"]))
    return shards


def _write_shard(
    source_conn: sqlite3.Connection,
    shards_dir: Path,
    shard_name: str,
    game_ids: list[str],
) -> Path:
    shard_path = shards_dir / shard_name
    if shard_path.exists():
        shard_path.unlink()

    with sqlite3.connect(shard_path) as shard_conn:
        shard_conn.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;

            CREATE TABLE contestants (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            );

            CREATE TABLE categories (
                id INTEGER PRIMARY KEY,
                game_id TEXT NOT NULL,
                round_order INTEGER NOT NULL,
                name TEXT NOT NULL,
                board_position INTEGER NOT NULL
            );

            CREATE TABLE clues (
                id INTEGER PRIMARY KEY,
                category_id INTEGER NOT NULL,
                row_value INTEGER,
                clue_text TEXT,
                correct_response TEXT,
                is_daily_double INTEGER NOT NULL DEFAULT 0,
                is_final_jeopardy INTEGER NOT NULL DEFAULT 0,
                is_triple_stumper INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE responses (
                id INTEGER PRIMARY KEY,
                clue_id INTEGER NOT NULL,
                contestant_id INTEGER NOT NULL,
                response_text TEXT,
                correctness INTEGER NOT NULL
            );
            """
        )
        placeholders = ",".join("?" for _ in game_ids)

        _copy_rows(
            source_conn,
            shard_conn,
            f"""
            SELECT DISTINCT c.id, c.name
            FROM contestants c
            JOIN responses r ON r.contestant_id = c.id
            JOIN clues cl ON cl.id = r.clue_id
            JOIN categories ca ON ca.id = cl.category_id
            WHERE ca.game_id IN ({placeholders})
            ORDER BY c.id
            """,
            "INSERT INTO contestants (id, name) VALUES (:id, :name)",
            game_ids,
        )
        _copy_rows(
            source_conn,
            shard_conn,
            f"""
            SELECT id, game_id, round_order, name, board_position
            FROM categories
            WHERE game_id IN ({placeholders})
            ORDER BY id
            """,
            """
            INSERT INTO categories (id, game_id, round_order, name, board_position)
            VALUES (:id, :game_id, :round_order, :name, :board_position)
            """,
            game_ids,
        )
        _copy_rows(
            source_conn,
            shard_conn,
            f"""
            SELECT
              cl.id,
              cl.category_id,
              cl.row_value,
              cl.clue_text,
              cl.correct_response,
              cl.is_daily_double,
              cl.is_final_jeopardy,
              cl.is_triple_stumper
            FROM clues cl
            JOIN categories ca ON ca.id = cl.category_id
            WHERE ca.game_id IN ({placeholders})
            ORDER BY cl.id
            """,
            """
            INSERT INTO clues (
              id,
              category_id,
              row_value,
              clue_text,
              correct_response,
              is_daily_double,
              is_final_jeopardy,
              is_triple_stumper
            )
            VALUES (
              :id,
              :category_id,
              :row_value,
              :clue_text,
              :correct_response,
              :is_daily_double,
              :is_final_jeopardy,
              :is_triple_stumper
            )
            """,
            game_ids,
        )
        _copy_rows(
            source_conn,
            shard_conn,
            f"""
            SELECT r.id, r.clue_id, r.contestant_id, r.response_text, r.correctness
            FROM responses r
            JOIN clues cl ON cl.id = r.clue_id
            JOIN categories ca ON ca.id = cl.category_id
            WHERE ca.game_id IN ({placeholders})
            ORDER BY r.id
            """,
            """
            INSERT INTO responses (id, clue_id, contestant_id, response_text, correctness)
            VALUES (:id, :clue_id, :contestant_id, :response_text, :correctness)
            """,
            game_ids,
        )
        shard_conn.executescript(
            """
            CREATE INDEX idx_categories_game_round ON categories(game_id, round_order);
            CREATE INDEX idx_clues_category ON clues(category_id);
            CREATE INDEX idx_responses_clue ON responses(clue_id);
            VACUUM;
            """
        )

    return shard_path


def _copy_rows(
    source_conn: sqlite3.Connection,
    target_conn: sqlite3.Connection,
    select_sql: str,
    insert_sql: str,
    params: list[str],
) -> None:
    rows = [_row_dict(row) for row in source_conn.execute(select_sql, params)]
    if rows:
        target_conn.executemany(insert_sql, rows)


def _row_dict(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def _shard_name(season_id: object, seasons_per_shard: int) -> str:
    try:
        season_number = int(str(season_id))
    except (TypeError, ValueError):
        return "jarchive_misc.sqlite3"
    start = ((season_number - 1) // seasons_per_shard) * seasons_per_shard + 1
    end = start + seasons_per_shard - 1
    return f"jarchive_s{start:02d}_s{end:02d}.sqlite3"
