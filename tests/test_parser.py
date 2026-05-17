from __future__ import annotations

from pathlib import Path

from jarchive_scraper.parser import parse_game_page, parse_season_index, parse_season_page


def test_parse_season_index_fixture(fixture_dir: Path) -> None:
    seasons = parse_season_index(
        (fixture_dir / "season_index.html").read_text(encoding="utf-8"),
        source_url="https://j-archive.com/listseasons.php",
    )

    assert [season.season_id for season in seasons] == ["41", "40", "39"]
    assert seasons[0].is_current is True
    assert seasons[1].archived_game_count == 230
    assert seasons[1].url == "https://j-archive.com/showseason.php?season=40"


def test_parse_season_page_fixture(fixture_dir: Path) -> None:
    games = parse_season_page(
        (fixture_dir / "season_40.html").read_text(encoding="utf-8"),
        source_url="https://j-archive.com/showseason.php?season=40",
    )

    assert [game.game_id for game in games] == ["8811", "8812"]
    assert games[0].show_number == "8950"
    assert games[0].air_date == "2024-01-15"
    assert games[0].season_id == "40"


def test_parse_game_page_fixture(fixture_dir: Path) -> None:
    detail = parse_game_page(
        (fixture_dir / "game_8811.html").read_text(encoding="utf-8"),
        source_url="https://j-archive.com/showgame.php?game_id=8811",
    )

    assert detail.game_id == "8811"
    assert detail.show_number == "8950"
    assert detail.air_date == "2024-01-15"
    assert detail.season_id == "40"
    assert [contestant.name for contestant in detail.contestants] == [
        "Alice Example",
        "Bob Example",
        "Carol Example",
    ]
    assert [round_data.name for round_data in detail.rounds] == [
        "Jeopardy",
        "Double Jeopardy",
        "Final Jeopardy",
    ]
    assert detail.rounds[0].categories[0].clues[0].correct_response == "H2O"
    assert detail.rounds[0].categories[1].clues[0].is_daily_double is True
    assert detail.rounds[2].categories[0].clues[0].correct_response == "Paris"
    assert len(detail.scores) == 3


def test_parse_game_page_ignores_navbar_season_links() -> None:
    detail = parse_game_page(
        """
        <html>
          <body>
            <div id="navbar">
              <a href="showseason.php?season=42">[current season]</a>
              <a href="showseason.php?season=41">[last season]</a>
            </div>
            <div id="game_title"><h1>Show #62 - Tuesday, December 4, 1984</h1></div>
          </body>
        </html>
        """,
        source_url="https://j-archive.com/showgame.php?game_id=355",
    )

    assert detail.season_id is None


def test_parse_game_page_uses_non_navbar_season_link() -> None:
    detail = parse_game_page(
        """
        <html>
          <body>
            <div id="navbar">
              <a href="showseason.php?season=42">[current season]</a>
            </div>
            <div id="game_title"><h1>Show #8950, aired 2024-01-15</h1></div>
            <a href="/showseason.php?season=40">Season 40</a>
          </body>
        </html>
        """,
        source_url="https://j-archive.com/showgame.php?game_id=8811",
    )

    assert detail.season_id == "40"
