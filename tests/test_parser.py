from __future__ import annotations

from pathlib import Path

from jarchive_scraper.parser import Response, parse_game_page, parse_season_index, parse_season_page


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
    assert games[0].title == "Champions Wildcard final"


def test_parse_game_page_fixture(fixture_dir: Path) -> None:
    detail = parse_game_page(
        (fixture_dir / "game_8811.html").read_text(encoding="utf-8"),
        source_url="https://j-archive.com/showgame.php?game_id=8811",
    )

    assert detail.game_id == "8811"
    assert detail.show_number == "8950"
    assert detail.air_date == "2024-01-15"
    assert detail.season_id == "40"
    assert detail.title is None
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
    assert detail.rounds[0].categories[0].clues[0].is_triple_stumper is False
    assert detail.rounds[0].categories[0].clues[0].responses == [
        Response(contestant="Bob", response_text="What is oxygen?", correctness=0),
        Response(contestant="Alice", response_text=None, correctness=1),
    ]
    assert detail.rounds[0].categories[1].clues[0].is_daily_double is True
    assert detail.rounds[0].categories[1].clues[0].is_triple_stumper is True
    assert detail.rounds[0].categories[1].clues[0].responses == []
    assert detail.rounds[2].categories[0].clues[0].correct_response == "Paris"
    assert detail.rounds[2].categories[0].clues[0].is_final_jeopardy is True
    assert [response.response_text for response in detail.rounds[2].categories[0].clues[0].responses] == [
        "What is Paris?",
        "What is Lyon?",
    ]
    assert len(detail.scores) == 3


def test_parse_game_page_marks_final_jeopardy_triple_stumper_when_all_responses_wrong() -> None:
    detail = parse_game_page(
        """
        <html>
          <body>
            <div id="game_title">Show #1234, aired 2024-01-15</div>
            <div id="final_jeopardy_round">
              <div class="category_name">NEWSPAPER FAMILIES</div>
              <table>
                <tr>
                  <td id="clue_FJ" class="clue_text">A college &amp; an oceanographic institution are named for this newspaper family</td>
                  <td id="clue_FJ_r" class="clue_text" style="display:none;">
                    <table>
                      <tr><td class="wrong">Gus</td><td rowspan="2" valign="top">Who are the Hearsts?</td></tr>
                      <tr><td>$3,500</td></tr>
                      <tr><td class="wrong">Gwen</td><td rowspan="2" valign="top">Who is Hearst?</td></tr>
                      <tr><td>$4,801</td></tr>
                      <tr><td class="wrong">Chris</td><td rowspan="2" valign="top">Who is Pulitzer?</td></tr>
                      <tr><td>$600</td></tr>
                    </table>
                    <em class="correct_response">the Scripps family</em>
                  </td>
                </tr>
              </table>
            </div>
          </body>
        </html>
        """,
        source_url="https://j-archive.com/showgame.php?game_id=1234",
    )

    clue = detail.rounds[0].categories[0].clues[0]
    assert clue.is_final_jeopardy is True
    assert clue.is_triple_stumper is True
    assert [response.correctness for response in clue.responses] == [0, 0, 0]


def test_parse_game_page_normalizes_special_titles() -> None:
    detail = parse_game_page(
        """
        <html>
          <head><title>J! Archive - Primetime Celebrity Jeopardy! game #7, aired 2024-01-02</title></head>
          <body></body>
        </html>
        """,
        source_url="https://j-archive.com/showgame.php?game_id=999",
    )

    assert detail.air_date == "2024-01-02"
    assert detail.title == "Primetime Celebrity Jeopardy! game #7"


def test_parse_season_page_discards_plain_show_listing_titles() -> None:
    games = parse_season_page(
        """
        <html><body>
          <a href="/showgame.php?game_id=1">Show #4596</a>, aired 2004-09-06 Ken Jennings vs. Betsey Casman vs. J.D. Smith<br>
          <a href="/showgame.php?game_id=2">#4597</a>, aired 2004-09-07 Ken Jennings vs. Player A vs. Player B<br>
        </body></html>
        """,
        source_url="https://j-archive.com/showseason.php?season=21",
    )

    assert games[0].title is None
    assert games[1].title is None


def test_parse_season_page_trims_special_listing_tails() -> None:
    games = parse_season_page(
        """
        <html><body>
          <a href="/showgame.php?game_id=8">Super Jeopardy! #8</a>, aired 1990-08-04 Keith Walker vs. Bruce Seymour vs. Roger Storm<br>
        </body></html>
        """,
        source_url="https://j-archive.com/showseason.php?season=superjeopardy",
    )

    assert games[0].title == "Super Jeopardy! #8"


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
