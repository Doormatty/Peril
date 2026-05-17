from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import shutil
import sys
from typing import Iterable

from rich.table import Table

from . import db
from .config import ConfigError, load_config, with_paths
from .fetcher import Fetcher
from .logging_utils import configure_logging, stderr_console, stdout_console
from .parser import parse_game_page, parse_season_index, parse_season_page
from .urls import canonicalize_url, season_index_url


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParseJob:
    fetch_id: int
    url_type: str
    canonical_url: str
    raw_file_path: str


@dataclass(frozen=True)
class ParsedFetch:
    job: ParseJob
    payload: object | None = None
    error: str | None = None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(
        verbose=args.verbose,
        quiet=args.quiet,
        log_file=args.log_file,
    )
    try:
        config = load_config(
            args.config,
            db_path=args.db,
            crawl_db_path=args.crawl_db,
            raw_dir=args.raw_dir,
            user_agent=args.user_agent,
        )
        config = with_paths(config, args.db, args.raw_dir, args.crawl_db)
        db.ensure_storage(config)
        assert config.crawl_db_path is not None
        if args.needs_data_db:
            with db.connect(config.db_path) as data_conn, db.connect(config.crawl_db_path) as crawl_conn:
                db.init_storage(data_conn, crawl_conn)
                logger.debug("Database initialized at %s", config.db_path)
                logger.debug("Crawl database initialized at %s", config.crawl_db_path)
                return args.func(args, config, data_conn, crawl_conn)

        crawl_db_existed = config.crawl_db_path.exists()
        with db.connect(config.crawl_db_path) as crawl_conn:
            db.init_crawl_db(crawl_conn)
            maybe_migrate_crawl_state(config, crawl_conn, crawl_db_existed)
            crawl_conn.commit()
            logger.debug("Crawl database initialized at %s", config.crawl_db_path)
            return args.func(args, config, None, crawl_conn)
    except (ConfigError, ValueError) as exc:
        stderr_console.print(f"[red]error:[/red] {exc}")
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jarchive-scraper")
    parser.add_argument("--config", default="jarchive_config.json")
    parser.add_argument("--db", help="SQLite database path")
    parser.add_argument("--crawl-db", help="SQLite crawl-state database path")
    parser.add_argument("--raw-dir", help="Raw HTML directory")
    parser.add_argument("--user-agent", help="Override configured browser User-Agent")
    parser.add_argument("--verbose", "-v", action="count", default=0)
    parser.add_argument("--quiet", "-q", action="store_true")
    parser.add_argument("--log-file", help="Write detailed plain-text logs to this file")
    subparsers = parser.add_subparsers(required=True)

    init = subparsers.add_parser("init")
    init.set_defaults(func=cmd_init, needs_data_db=True)

    discover = subparsers.add_parser("discover-seasons")
    discover.add_argument("--dry-run", action="store_true", help="Do not fetch the index")
    discover.add_argument("--from-file", help="Use a local season-index HTML file")
    discover.add_argument("--print-only", action="store_true", help="Print discovered URLs without queue writes")
    discover.add_argument("--include-current", action="store_true")
    discover.add_argument("--initial-delay", action="store_true", help="Sleep before the first network request")
    discover.add_argument("--no-initial-delay", action="store_true", help=argparse.SUPPRESS)
    discover.set_defaults(func=cmd_discover_seasons, needs_data_db=False)

    fetch = subparsers.add_parser("fetch")
    fetch.add_argument("--limit", type=int, help="Maximum URLs to process this run")
    fetch.add_argument("--initial-delay", action="store_true", help="Sleep before the first network request")
    fetch.add_argument("--no-initial-delay", action="store_true", help=argparse.SUPPRESS)
    fetch.add_argument("--retry-failed", action="store_true", help="Reset retryable failed URLs before fetching")
    fetch.set_defaults(func=cmd_fetch, needs_data_db=False)

    parse = subparsers.add_parser("parse")
    parse.add_argument("--url", help="Parse only this canonicalizable URL")
    parse.add_argument("--file", help="Import and parse this local raw HTML file; requires --url")
    parse.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parser worker processes to use before serialized DB writes",
    )
    parse.set_defaults(func=cmd_parse, needs_data_db=True)

    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    status.set_defaults(func=cmd_status, needs_data_db=True)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("urls", nargs="+")
    enqueue.set_defaults(func=cmd_enqueue, needs_data_db=False)
    return parser


def maybe_migrate_crawl_state(config, crawl_conn, crawl_db_existed: bool) -> None:
    if crawl_db_existed or not config.db_path.exists():
        return
    with db.connect(config.db_path) as data_conn:
        if not any(db.table_exists(data_conn, table) for table in db.CRAWL_TABLES):
            return
        db.migrate_legacy_crawl_state(data_conn, crawl_conn)
        db.sync_queue_air_dates(data_conn, crawl_conn)
        db.drop_migrated_crawl_tables(data_conn, crawl_conn)
        data_conn.commit()


def cmd_init(args, config, data_conn, crawl_conn) -> int:
    logger.info("Initialized storage")
    logger.info("Database: %s", config.db_path)
    logger.info("Crawl database: %s", config.crawl_db_path)
    logger.info("Raw HTML directory: %s", config.raw_dir)
    stdout_console.print(f"database: {config.db_path}")
    stdout_console.print(f"crawl_database: {config.crawl_db_path}")
    stdout_console.print(f"raw_html: {config.raw_dir}")
    return 0


def cmd_enqueue(args, config, data_conn, crawl_conn) -> int:
    added = 0
    for url in args.urls:
        canonical = canonicalize_url(url, config.base_url)
        if db.enqueue(crawl_conn, canonical.canonical_url, canonical.url_type):
            added += 1
            logger.info("Enqueued %s: %s", canonical.url_type, canonical.canonical_url)
        else:
            logger.info("Already queued: %s", canonical.canonical_url)
    crawl_conn.commit()
    stdout_console.print(f"added: {added}")
    return 0


def cmd_discover_seasons(args, config, data_conn, crawl_conn) -> int:
    canonical = canonicalize_url(season_index_url(), config.base_url)
    html: str
    fetch_row = None

    if args.from_file and args.dry_run:
        logger.info("Discovering seasons from local file without importing raw HTML: %s", args.from_file)
        html = Path(args.from_file).read_text(encoding="utf-8")
    elif args.from_file:
        fetch_row = import_raw_file(crawl_conn, config, args.from_file, canonical.canonical_url)
        logger.info("Imported season index fixture as saved raw HTML: %s", fetch_row["raw_file_path"])
        html = Path(fetch_row["raw_file_path"]).read_text(encoding="utf-8")
    else:
        existing = db.get_fetch(crawl_conn, canonical.canonical_url)
        if existing and existing["status_code"] == 200 and existing["raw_file_path"]:
            logger.info("Using saved season index: %s", existing["raw_file_path"])
            html = Path(existing["raw_file_path"]).read_text(encoding="utf-8")
        elif args.dry_run:
            raise ValueError(
                "discover-seasons --dry-run needs an existing saved index or --from-file"
            )
        else:
            db.enqueue(crawl_conn, canonical.canonical_url, canonical.url_type)
            crawl_conn.commit()
            logger.info("Fetching season index before discovery")
            result = Fetcher(config).fetch(
                crawl_conn,
                canonical.canonical_url,
                no_initial_delay=skip_initial_delay(args, 0),
            )
            crawl_conn.commit()
            if result.status_code != 200 or not result.raw_file_path:
                raise ValueError(f"Could not fetch season index: {result.error}")
            html = Path(result.raw_file_path).read_text(encoding="utf-8")

    seasons = parse_season_index(html, source_url=canonical.canonical_url)
    include_current = args.include_current or not config.exclude_current_season
    queued = 0
    skipped_current = 0
    for season in seasons:
        if season.is_current and not include_current:
            skipped_current += 1
            continue
        if args.print_only:
            stdout_console.print(season.url)
            continue
        if db.enqueue(crawl_conn, season.url, "season", canonical.canonical_url):
            queued += 1
    crawl_conn.commit()
    logger.info(
        "Discovered %s seasons; queued %s; skipped current %s",
        len(seasons),
        queued,
        skipped_current,
    )
    stdout_console.print(f"discovered seasons: {len(seasons)}")
    if not args.print_only:
        stdout_console.print(f"queued seasons: {queued}")
    if fetch_row:
        stdout_console.print(f"source: {fetch_row['raw_file_path']}")
    return 0


def cmd_fetch(args, config, data_conn, crawl_conn) -> int:
    if args.retry_failed:
        reset = db.reset_retryable_failures(crawl_conn)
        crawl_conn.commit()
        logger.info("Retryable failures reset: %s", reset)

    run_limit = args.limit or config.max_requests_per_run
    processed = 0
    fetcher = Fetcher(config)
    logger.info(
        "Starting fetch loop: run_limit=%s daily_cap=%s recent_game_bias=%.2f recent_game_years=%s",
        run_limit or "none",
        config.max_requests_per_day,
        config.recent_game_bias,
        config.recent_game_years,
    )
    while run_limit is None or processed < run_limit:
        row = db.next_pending_url(
            crawl_conn,
            recent_game_years=config.recent_game_years,
            recent_game_bias=config.recent_game_bias,
        )
        if row is None:
            logger.info("No pending URLs remain")
            break
        if db.count_requests_today(crawl_conn) >= config.max_requests_per_day:
            logger.warning("Daily request cap reached")
            break
        logger.info(
            "Selected pending %s URL: %s",
            row["url_type"],
            row["canonical_url"],
        )
        result = fetcher.fetch(
            crawl_conn,
            row["canonical_url"],
            no_initial_delay=skip_initial_delay(args, processed),
        )
        processed += 1
        if result.status_code == 200 and result.raw_file_path:
            children = enqueue_children_from_fetch(
                crawl_conn,
                config,
                result.canonical_url,
                result.url_type,
                result.raw_file_path,
            )
            logger.info(
                "%s %s page: %s; queued_children=%s",
                "Skipped saved" if result.skipped else "Fetched",
                result.url_type,
                result.canonical_url,
                children,
            )
        else:
            logger.warning(
                "Failed %s page: %s error=%s",
                result.url_type,
                result.canonical_url,
                result.error,
            )
        crawl_conn.commit()
    stdout_console.print(f"processed: {processed}")
    return 0


def cmd_parse(args, config, data_conn, crawl_conn) -> int:
    if args.workers < 1:
        raise ValueError("parse --workers must be >= 1")
    canonical_url = None
    if args.file and not args.url:
        raise ValueError("parse --file requires --url")
    if args.url:
        canonical = canonicalize_url(args.url, config.base_url)
        canonical_url = canonical.canonical_url
        if args.file:
            import_raw_file(crawl_conn, config, args.file, canonical_url)
            crawl_conn.commit()

    rows = list(db.iter_fetches_for_parse(crawl_conn, canonical_url))
    logger.info("Parsing %s saved fetches", len(rows))
    parsed = 0
    errors = 0
    jobs = [
        ParseJob(
            fetch_id=row["id"],
            url_type=row["url_type"],
            canonical_url=row["canonical_url"],
            raw_file_path=row["raw_file_path"],
        )
        for row in rows
    ]

    for result in parse_jobs(jobs, args.workers):
        try:
            if result.error:
                raise ValueError(result.error)
            apply_parsed_fetch(data_conn, crawl_conn, config, result.job, result.payload)
        except Exception as exc:  # Keep parsing independent saved pages.
            logger.exception("Parse failed for %s", result.job.canonical_url)
            db.record_parse_error(crawl_conn, result.job.fetch_id, str(exc))
            errors += 1
        else:
            db.set_parser_state(
                crawl_conn,
                result.job.fetch_id,
                result.job.canonical_url,
                "parsed",
            )
            parsed += 1
            logger.info("Parsed %s page: %s", result.job.url_type, result.job.canonical_url)
        data_conn.commit()
        crawl_conn.commit()
    stdout_console.print(f"parsed: {parsed}")
    stdout_console.print(f"errors: {errors}")
    return 1 if errors else 0


def parse_jobs(jobs: list[ParseJob], workers: int) -> Iterable[ParsedFetch]:
    if workers == 1 or len(jobs) <= 1:
        for job in jobs:
            yield parse_job(job)
        return

    worker_count = min(workers, len(jobs))
    logger.info("Using %s parser worker processes", worker_count)
    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        yield from executor.map(parse_job, jobs)


def cmd_status(args, config, data_conn, crawl_conn) -> int:
    status = db.status_counts(
        data_conn,
        crawl_conn,
        recent_game_years=config.recent_game_years,
    )
    if args.json:
        stdout_console.print(json.dumps(status, indent=2, sort_keys=True))
    else:
        render_status(status, config)
    return 0


def import_raw_file(crawl_conn, config, file_path: str, url: str):
    canonical = canonicalize_url(url, config.base_url)
    source = Path(file_path)
    if not source.exists():
        raise ValueError(f"Raw HTML file does not exist: {source}")
    data = source.read_bytes()
    digest = hashlib.sha256(canonical.canonical_url.encode("utf-8")).hexdigest()
    destination = config.raw_dir / f"{digest[:24]}.html"
    if source.resolve() != destination.resolve():
        shutil.copyfile(source, destination)
        logger.info("Copied fixture %s to %s", source, destination)
    db.enqueue(crawl_conn, canonical.canonical_url, canonical.url_type)
    row = db.record_fetch_success(
        crawl_conn,
        original_url=canonical.original_url,
        canonical_url=canonical.canonical_url,
        url_type=canonical.url_type,
        status_code=200,
        content_hash=hashlib.sha256(data).hexdigest(),
        raw_file_path=str(destination),
    )
    return row


def enqueue_children_from_fetch(crawl_conn, config, canonical_url: str, url_type: str, raw_file_path: str) -> int:
    html = Path(raw_file_path).read_text(encoding="utf-8")
    count = 0
    if url_type == "season_index":
        seasons = parse_season_index(html, source_url=canonical_url)
        skipped_current = 0
        for season in seasons:
            if season.is_current and config.exclude_current_season:
                skipped_current += 1
                continue
            if db.enqueue(crawl_conn, season.url, "season", canonical_url):
                count += 1
        logger.info(
            "Parsed season index children: seasons=%s queued=%s skipped_current=%s",
            len(seasons),
            count,
            skipped_current,
        )
    elif url_type == "season":
        games = parse_season_page(html, source_url=canonical_url)
        for game in games:
            if db.enqueue(
                crawl_conn,
                game.url,
                "game",
                canonical_url,
                air_date=game.air_date,
            ):
                count += 1
        logger.info("Parsed season children: games=%s queued=%s", len(games), count)
    return count


def parse_job(job: ParseJob) -> ParsedFetch:
    try:
        html = Path(job.raw_file_path).read_text(encoding="utf-8")
        payload = parse_html(job.url_type, html, job.canonical_url)
    except Exception as exc:
        return ParsedFetch(job=job, error=str(exc))
    return ParsedFetch(job=job, payload=payload)


def parse_html(url_type: str, html: str, canonical_url: str) -> object:
    if url_type == "season_index":
        return parse_season_index(html, source_url=canonical_url)
    if url_type == "season":
        return parse_season_page(html, source_url=canonical_url)
    if url_type == "game":
        return parse_game_page(html, source_url=canonical_url)
    raise ValueError(f"Unsupported URL type: {url_type}")


def apply_parsed_fetch(data_conn, crawl_conn, config, job: ParseJob, payload: object) -> None:
    if job.url_type == "season_index":
        seasons = payload
        db.upsert_seasons(data_conn, seasons)
        for season in seasons:
            if season.is_current and config.exclude_current_season:
                continue
            db.enqueue(crawl_conn, season.url, "season", job.canonical_url)
    elif job.url_type == "season":
        games = payload
        db.upsert_game_summaries(data_conn, games)
        for game in games:
            db.enqueue(
                crawl_conn,
                game.url,
                "game",
                job.canonical_url,
                air_date=game.air_date,
            )
    elif job.url_type == "game":
        db.replace_game_detail(data_conn, payload)
    else:
        raise ValueError(f"Unsupported URL type: {job.url_type}")


def parse_fetch_row(data_conn, crawl_conn, config, row) -> None:
    job = ParseJob(
        fetch_id=row["id"],
        url_type=row["url_type"],
        canonical_url=row["canonical_url"],
        raw_file_path=row["raw_file_path"],
    )
    result = parse_job(job)
    if result.error:
        raise ValueError(result.error)
    apply_parsed_fetch(data_conn, crawl_conn, config, job, result.payload)


def skip_initial_delay(args, processed: int) -> bool:
    return processed == 0 and (args.no_initial_delay or not args.initial_delay)


def render_status(status: dict[str, object], config) -> None:
    queue_table = Table(title="Queue")
    queue_table.add_column("Type")
    queue_table.add_column("Status")
    queue_table.add_column("Count", justify="right")
    for row in status["queue"]:
        queue_table.add_row(row["url_type"], row["status"], str(row["count"]))
    if not status["queue"]:
        queue_table.add_row("-", "-", "0")

    fetch_table = Table(title="Fetches")
    fetch_table.add_column("HTTP Status")
    fetch_table.add_column("Count", justify="right")
    for row in status["fetches"]:
        fetch_table.add_row(str(row["status_code"]), str(row["count"]))
    if not status["fetches"]:
        fetch_table.add_row("-", "0")

    parsed_table = Table(title="Parsed")
    parsed_table.add_column("Table")
    parsed_table.add_column("Rows", justify="right")
    for key, value in status["parsed"].items():
        parsed_table.add_row(key, str(value))

    crawl = status["crawl"]
    crawl_table = Table(title="Crawl")
    crawl_table.add_column("Metric")
    crawl_table.add_column("Value", justify="right")
    crawl_table.add_row("requests_today", str(crawl["requests_today"]))
    crawl_table.add_row("pending_games_total", str(crawl["pending_games_total"]))
    crawl_table.add_row("pending_games_with_dates", str(crawl["pending_games_with_dates"]))
    crawl_table.add_row("pending_recent_games", str(crawl["pending_recent_games"]))
    crawl_table.add_row("recent_game_cutoff", str(crawl["recent_game_cutoff"]))
    crawl_table.add_row("recent_game_bias", f"{config.recent_game_bias:.2f}")

    stdout_console.print(queue_table)
    stdout_console.print(fetch_table)
    stdout_console.print(parsed_table)
    stdout_console.print(crawl_table)
