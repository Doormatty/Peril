from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse


HOST = "j-archive.com"
BASE_URL = "https://j-archive.com/"
SEASON_INDEX_PATH = "/listseasons.php"
SEASON_PATH = "/showseason.php"
GAME_PATH = "/showgame.php"
ALLOWED_PATHS = {SEASON_INDEX_PATH, SEASON_PATH, GAME_PATH}


class UrlNotAllowed(ValueError):
    """Raised when a URL is outside the permitted J! Archive crawl scope."""


@dataclass(frozen=True)
class CanonicalUrl:
    original_url: str
    canonical_url: str
    url_type: str


def canonicalize_url(url: str, base_url: str = BASE_URL) -> CanonicalUrl:
    joined = urljoin(base_url, url)
    parsed = urlparse(joined)
    host = (parsed.hostname or "").lower()
    if host != HOST:
        raise UrlNotAllowed(f"Only {HOST} URLs are allowed: {url}")

    path = parsed.path or "/"
    if path not in ALLOWED_PATHS:
        raise UrlNotAllowed(f"Path is outside the allowed crawl scope: {path}")

    query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
    query: list[tuple[str, str]] = []
    url_type = "unknown"

    if path == SEASON_INDEX_PATH:
        url_type = "season_index"
        query = []
    elif path == SEASON_PATH:
        url_type = "season"
        season = _single_query_value(query_pairs, "season", url)
        query = [("season", season)]
    elif path == GAME_PATH:
        url_type = "game"
        game_id = _single_query_value(query_pairs, "game_id", url)
        query = [("game_id", game_id)]

    canonical = urlunparse(
        ("https", HOST, path, "", urlencode(sorted(query)), "")
    )
    return CanonicalUrl(url, canonical, url_type)


def _single_query_value(pairs: list[tuple[str, str]], key: str, url: str) -> str:
    values = [value for candidate, value in pairs if candidate == key and value]
    unknown = [candidate for candidate, _ in pairs if candidate != key]
    if unknown:
        raise UrlNotAllowed(f"Unexpected query parameters for {url}: {unknown}")
    if len(values) != 1:
        raise UrlNotAllowed(f"Expected exactly one {key!r} query parameter: {url}")
    return values[0]


def season_index_url() -> str:
    return "https://j-archive.com/listseasons.php"
