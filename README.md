# J! Archive Scraper

Permission-scoped, intentionally slow scraper for `https://j-archive.com/`.

This project stores raw HTML permanently and treats it as the durable source of
truth. The main SQLite database stores parsed records for the app, while a
separate crawl SQLite database stores queue state, fetch metadata, request
attempts, and parse errors. Fetching is single-threaded, rate-limited, and
idempotent: a URL with a saved HTTP 200 body is never requested again.

## Setup

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp jarchive_config.example.json jarchive_config.json
```

Edit `jarchive_config.json` and set `user_agent` to one valid browser User-Agent
string. The scraper sends that value exactly and does not rotate identities.

## Commands

```bash
python -m jarchive_scraper init
python -m jarchive_scraper discover-seasons
python -m jarchive_scraper fetch --limit 5
python -m jarchive_scraper parse
python -m jarchive_scraper parse --workers 4
python -m jarchive_scraper status
python -m jarchive_scraper status --json
python -m jarchive_scraper enqueue 'https://j-archive.com/showseason.php?season=40'
```

CLI progress and diagnostics are logged with Rich to stderr. Use `-v` for debug
logs, `--quiet` to suppress info logs, and `--log-file crawl.log` to keep a
plain-text debug log for long runs. Human `status` output is a Rich table;
`status --json` is stable machine-readable output.

By default `db_path` is `jarchive.sqlite3` and crawl state is stored beside it in
`jarchive_crawl.sqlite3`. Override the crawl database with `--crawl-db` or
`crawl_db_path` in config. The `fetch` command only updates raw HTML plus the
crawl database, so it can run without opening the main app database after
bootstrap. Parsed app data is updated by `parse`. On first startup after
upgrading an older single-database layout, crawl tables are copied into the crawl
database and then removed from the main database after the copy is complete.
The `parse` command defaults to one worker for deterministic local runs. Use
`parse --workers N` to parse saved raw HTML in multiple worker processes while
keeping all SQLite writes serialized in the parent process.

By default the current season is excluded during discovery because current
season pages can change while this scraper deliberately avoids re-downloading a
successfully saved page.

## Single Player SPA

The playable single-player app is in `web/`. It is a static browser app that
loads `jarchive.sqlite3` with the vendored `sql.js` runtime, then runs read-only
SQLite queries in the browser.

For local development from the repo root:

```bash
python3 -m http.server 8000
```

Open `http://localhost:8000/web/`. The app first tries `web/jarchive.sqlite3`,
then falls back to the repo-root `jarchive.sqlite3`. For deployment, put
`index.html`, `app.js`, `gameLogic.mjs`, `styles.css`, and `jarchive.sqlite3` in
the same static directory. You can override the database URL with
`?db=/path/to/jarchive.sqlite3`.

The checked-in `web/vendor/sql-wasm.js` and `web/vendor/sql-wasm.wasm` files are
the only runtime library assets the app needs.

## Fast Testing

The browser app keeps its reusable game rules in `web/gameLogic.mjs`, which is
covered by Node's built-in test runner. Use this for routine UI logic checks
instead of launching Playwright:

```bash
npm test
npm run check:web
```

The scraper and parser tests still run through pytest:

```bash
.venv/bin/python -m pytest -q
```

Useful local-only flows:

```bash
python -m jarchive_scraper discover-seasons --dry-run --from-file tests/fixtures/season_index.html
python -m jarchive_scraper parse --file tests/fixtures/season_40.html --url 'https://j-archive.com/showseason.php?season=40'
```

## Safety Defaults

- J! Archive only: `https://j-archive.com/listseasons.php`,
  `showseason.php`, and `showgame.php`.
- Fetching has no concurrency, no background workers, and no speculative
  prefetch. Parsing saved raw HTML can use `parse --workers N`; database writes
  remain serialized.
- Random sleep before each network request after the first request in a run.
  Use `--initial-delay` to also sleep before the first network request.
- Default delay range: 30 to 120 seconds.
- Default request cap: 250 attempts per UTC day.
- Pending game selection is 90% biased toward games with an air date in the
  last 10 years. If no recent pending games are known, it falls back to all
  pending games.
- Raw HTML is stored under `raw_html/`.
