from __future__ import annotations

from dataclasses import dataclass
import hashlib
import logging
import random
import time
from pathlib import Path
from typing import Callable, Protocol

import requests

from . import db
from .config import ScraperConfig, require_browser_user_agent
from .urls import canonicalize_url


logger = logging.getLogger(__name__)


class HttpSession(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float): ...


@dataclass(frozen=True)
class FetchResult:
    canonical_url: str
    url_type: str
    status_code: int | None
    skipped: bool
    raw_file_path: str | None = None
    error: str | None = None


class Fetcher:
    def __init__(
        self,
        config: ScraperConfig,
        *,
        session: HttpSession | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.sleeper = sleeper
        self.rng = rng or random.Random()

    def choose_delay(self) -> float:
        if self.config.max_delay_seconds == self.config.min_delay_seconds:
            return self.config.min_delay_seconds
        return self.rng.uniform(
            self.config.min_delay_seconds, self.config.max_delay_seconds
        )

    def fetch(
        self,
        conn,
        url: str,
        *,
        no_initial_delay: bool = False,
    ) -> FetchResult:
        canonical = canonicalize_url(url, self.config.base_url)
        if db.successful_fetch_exists(conn, canonical.canonical_url):
            row = db.get_fetch(conn, canonical.canonical_url)
            if row:
                db.mark_queue_status(conn, canonical.canonical_url, "fetched")
                logger.info(
                    "Skipping saved HTTP 200 %s page: %s",
                    canonical.url_type,
                    canonical.canonical_url,
                )
                return FetchResult(
                    canonical.canonical_url,
                    canonical.url_type,
                    int(row["status_code"]),
                    skipped=True,
                    raw_file_path=row["raw_file_path"],
                )

        user_agent = require_browser_user_agent(self.config)
        if not no_initial_delay:
            delay = self.choose_delay()
            logger.info("Sleeping %.1fs before request", delay)
            self.sleeper(delay)
        else:
            logger.debug("Initial delay skipped by CLI flag")

        try:
            logger.info("Requesting %s page: %s", canonical.url_type, canonical.canonical_url)
            response = self.session.get(
                canonical.canonical_url,
                headers={"User-Agent": user_agent},
                timeout=30.0,
            )
        except requests.RequestException as exc:
            logger.warning("Network failure for %s: %s", canonical.canonical_url, exc)
            db.record_fetch_failure(
                conn,
                original_url=canonical.original_url,
                canonical_url=canonical.canonical_url,
                url_type=canonical.url_type,
                status_code=None,
                error=str(exc),
            )
            return FetchResult(
                canonical.canonical_url,
                canonical.url_type,
                None,
                skipped=False,
                error=str(exc),
            )

        if response.status_code != 200:
            logger.warning(
                "HTTP %s for %s", response.status_code, canonical.canonical_url
            )
            db.record_fetch_failure(
                conn,
                original_url=canonical.original_url,
                canonical_url=canonical.canonical_url,
                url_type=canonical.url_type,
                status_code=response.status_code,
                error=f"HTTP {response.status_code}",
            )
            return FetchResult(
                canonical.canonical_url,
                canonical.url_type,
                response.status_code,
                skipped=False,
                error=f"HTTP {response.status_code}",
            )

        body = response.content
        content_hash = hashlib.sha256(body).hexdigest()
        raw_path = self.raw_path_for_url(canonical.canonical_url)
        if not raw_path.exists():
            raw_path.write_bytes(body)
            logger.info(
                "Saved HTTP 200 %s page to %s", canonical.url_type, raw_path
            )
        else:
            logger.info("Raw file already present: %s", raw_path)

        db.record_fetch_success(
            conn,
            original_url=canonical.original_url,
            canonical_url=canonical.canonical_url,
            url_type=canonical.url_type,
            status_code=response.status_code,
            content_hash=content_hash,
            raw_file_path=str(raw_path),
        )
        return FetchResult(
            canonical.canonical_url,
            canonical.url_type,
            response.status_code,
            skipped=False,
            raw_file_path=str(raw_path),
        )

    def raw_path_for_url(self, canonical_url: str) -> Path:
        digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        return self.config.raw_dir / f"{digest[:24]}.html"
