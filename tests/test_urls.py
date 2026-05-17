from __future__ import annotations

import pytest

from jarchive_scraper.urls import UrlNotAllowed, canonicalize_url


def test_canonicalizes_allowed_urls() -> None:
    result = canonicalize_url("http://j-archive.com/showgame.php?game_id=8811#clue")

    assert result.canonical_url == "https://j-archive.com/showgame.php?game_id=8811"
    assert result.url_type == "game"


def test_canonicalizes_relative_season_url() -> None:
    result = canonicalize_url("/showseason.php?season=40")

    assert result.canonical_url == "https://j-archive.com/showseason.php?season=40"
    assert result.url_type == "season"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/showgame.php?game_id=8811",
        "https://j-archive.com/media/logo.png",
        "https://j-archive.com/showgame.php?game_id=8811&x=1",
        "https://j-archive.com/showseason.php",
    ],
)
def test_rejects_urls_outside_permission_scope(url: str) -> None:
    with pytest.raises(UrlNotAllowed):
        canonicalize_url(url)
