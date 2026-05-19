from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup, Tag

from .urls import canonicalize_url


@dataclass(frozen=True)
class SeasonSummary:
    season_id: str
    name: str
    date_start: str | None
    date_end: str | None
    date_range_text: str | None
    archived_game_count: int | None
    url: str
    is_current: bool = False


@dataclass(frozen=True)
class GameSummary:
    game_id: str
    show_number: str | None
    air_date: str | None
    season_id: str | None
    title: str | None
    notes: str | None
    url: str


@dataclass(frozen=True)
class Contestant:
    name: str
    notes: str | None = None


@dataclass(frozen=True)
class Response:
    contestant: str | None = None
    response_text: str | None = None
    correctness: int | None = None


@dataclass(frozen=True)
class Clue:
    row: int | None
    clue_text: str | None
    correct_response: str | None
    is_daily_double: bool = False
    is_final_jeopardy: bool = False
    is_triple_stumper: bool = False
    responses: list[Response] = field(default_factory=list)


@dataclass(frozen=True)
class Category:
    name: str
    board_position: int
    clues: list[Clue] = field(default_factory=list)


@dataclass(frozen=True)
class Round:
    name: str
    round_order: int
    categories: list[Category] = field(default_factory=list)


@dataclass(frozen=True)
class Score:
    stage: str
    contestant: str | None
    score: int | None


@dataclass(frozen=True)
class GameDetail:
    game_id: str
    show_number: str | None
    air_date: str | None
    season_id: str | None
    title: str | None
    notes: str | None
    url: str
    contestants: list[Contestant] = field(default_factory=list)
    rounds: list[Round] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)


def parse_season_index(html: str, *, source_url: str) -> list[SeasonSummary]:
    soup = BeautifulSoup(html, "lxml")
    seasons: list[SeasonSummary] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "showseason.php" not in href:
            continue
        try:
            canonical = canonicalize_url(href)
        except ValueError:
            continue
        season_id = _query_value(canonical.canonical_url, "season")
        line = _anchor_line(anchor)
        name = _clean_text(anchor.get_text(" ", strip=True))
        count_match = re.search(r"\((\d+)\s+games?\s+archived\)", line, re.I)
        archived_game_count = int(count_match.group(1)) if count_match else None
        date_range_text = _extract_date_range(line, name)
        date_start, date_end = _date_bounds(date_range_text)
        seasons.append(
            SeasonSummary(
                season_id=season_id,
                name=name,
                date_start=date_start,
                date_end=date_end,
                date_range_text=date_range_text,
                archived_game_count=archived_game_count,
                url=canonical.canonical_url,
                is_current=False,
            )
        )

    if seasons:
        first = seasons[0]
        seasons[0] = SeasonSummary(
            season_id=first.season_id,
            name=first.name,
            date_start=first.date_start,
            date_end=first.date_end,
            date_range_text=first.date_range_text,
            archived_game_count=first.archived_game_count,
            url=first.url,
            is_current=True,
        )
    return seasons


def parse_season_page(html: str, *, source_url: str) -> list[GameSummary]:
    soup = BeautifulSoup(html, "lxml")
    season_id = _query_value(source_url, "season")
    games: list[GameSummary] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if "showgame.php" not in href:
            continue
        try:
            canonical = canonicalize_url(href)
        except ValueError:
            continue
        if canonical.canonical_url in seen:
            continue
        seen.add(canonical.canonical_url)
        game_id = _query_value(canonical.canonical_url, "game_id")
        line = _anchor_line(anchor)
        show_number, air_date = _show_and_airdate(line)
        title = _clean_title(line, anchor.get_text(" ", strip=True))
        games.append(
            GameSummary(
                game_id=game_id,
                show_number=show_number,
                air_date=air_date,
                season_id=season_id,
                title=title,
                notes=None,
                url=canonical.canonical_url,
            )
        )
    return games


def parse_game_page(html: str, *, source_url: str) -> GameDetail:
    soup = BeautifulSoup(html, "lxml")
    game_id = _query_value(source_url, "game_id")
    page_text = _clean_text(soup.get_text("\n", strip=True))
    title_match = re.search(
        r"Show\s+#(?P<show>[\w-]+),\s+aired\s+(?P<date>\d{4}-\d{2}-\d{2})",
        page_text,
        re.I,
    )
    page_title = _page_title(soup)
    if not title_match and page_title:
        title_match = re.search(
            r"Show\s+#(?P<show>[\w-]+),\s+aired\s+(?P<date>\d{4}-\d{2}-\d{2})",
            page_title,
            re.I,
        )
    show_number = title_match.group("show") if title_match else None
    air_date = title_match.group("date") if title_match else _air_date_from_title(page_title)
    title = _normalize_game_title(page_title)
    season_id = _season_link_id(soup)

    detail = GameDetail(
        game_id=game_id,
        show_number=show_number,
        air_date=air_date,
        season_id=season_id,
        title=title,
        notes=None,
        url=canonicalize_url(source_url).canonical_url,
        contestants=_parse_contestants(soup),
        rounds=_parse_rounds(soup),
        scores=_parse_scores(soup),
    )
    return detail


def _parse_contestants(soup: BeautifulSoup) -> list[Contestant]:
    contestants: list[Contestant] = []
    root = soup.find(id="contestants") or soup
    candidates = root.select(".contestant")
    if not candidates:
        candidates = root.select("a[href*='showplayer.php']")

    seen: set[str] = set()
    for candidate in candidates:
        text = _clean_text(candidate.get_text(" ", strip=True))
        if not text:
            continue
        if isinstance(candidate, Tag) and candidate.name == "a":
            name = text
            parent_text = _clean_text(candidate.parent.get_text(" ", strip=True))
            notes = parent_text.removeprefix(name).lstrip(" ,:-") or None
        else:
            parts = text.split(",", 1)
            name = parts[0].strip()
            notes = parts[1].strip() if len(parts) > 1 else None
        if not name or name in seen:
            continue
        seen.add(name)
        contestants.append(Contestant(name, notes))
    return contestants


def _parse_rounds(soup: BeautifulSoup) -> list[Round]:
    round_specs = [
        ("jeopardy_round", "Jeopardy", 1),
        ("double_jeopardy_round", "Double Jeopardy", 2),
        ("final_jeopardy_round", "Final Jeopardy", 3),
        ("tiebreaker_round", "Tiebreaker", 4),
    ]
    rounds: list[Round] = []
    for element_id, name, order in round_specs:
        root = soup.find(id=element_id)
        if not root:
            continue
        if name == "Final Jeopardy":
            rounds.append(_parse_final_round(root, name, order))
        else:
            rounds.append(_parse_board_round(root, name, order))
    return rounds


def _parse_board_round(root: Tag, name: str, order: int) -> Round:
    category_cells = root.select(".category_name")
    categories: dict[int, Category] = {}
    for index, cell in enumerate(category_cells, start=1):
        categories[index] = Category(
            name=_clean_text(cell.get_text(" ", strip=True)),
            board_position=index,
            clues=[],
        )

    for clue_cell in root.select("td.clue"):
        if not isinstance(clue_cell, Tag):
            continue
        source_clue_id = clue_cell.get("id")
        if not source_clue_id:
            clue_text_node = clue_cell.select_one(".clue_text")
            source_clue_id = clue_text_node.get("id") if clue_text_node else None
        column, row = _clue_position(str(source_clue_id or ""))
        if column is None or row is None:
            continue
        if column not in categories:
            categories[column] = Category(f"Category {column}", column, [])
        clue_text_node = clue_cell.select_one(".clue_text") or clue_cell
        response_node = _response_node_for(root, str(source_clue_id or ""))
        correct_response = _correct_response(clue_cell, response_node)
        responses, is_triple_stumper = _parse_responses(response_node)
        is_daily_double = bool(
            clue_cell.select_one(".clue_value_daily_double")
            or "daily_double" in " ".join(clue_cell.get("class", []))
        )
        categories[column].clues.append(
            Clue(
                row=row,
                clue_text=_clean_text(clue_text_node.get_text(" ", strip=True)),
                correct_response=correct_response,
                is_daily_double=is_daily_double,
                is_final_jeopardy=False,
                is_triple_stumper=is_triple_stumper,
                responses=responses,
            )
        )

    return Round(
        name=name,
        round_order=order,
        categories=[categories[key] for key in sorted(categories)],
    )


def _parse_final_round(root: Tag, name: str, order: int) -> Round:
    category_node = root.select_one(".category_name")
    clue_node = root.select_one(".clue_text")
    source_clue_id = str(clue_node.get("id")) if clue_node and clue_node.get("id") else "clue_FJ"
    response_node = _response_node_for(root, source_clue_id)
    correct_response = _correct_response(root, response_node)
    responses, is_triple_stumper = _parse_responses(response_node)
    is_triple_stumper = is_triple_stumper or _all_responses_wrong(responses)
    category = Category(
        name=_clean_text(category_node.get_text(" ", strip=True)) if category_node else "Final Jeopardy",
        board_position=1,
        clues=[
            Clue(
                row=1,
                clue_text=_clean_text(clue_node.get_text(" ", strip=True)) if clue_node else None,
                correct_response=correct_response,
                is_final_jeopardy=True,
                is_triple_stumper=is_triple_stumper,
                responses=responses,
            )
        ],
    )
    return Round(name=name, round_order=order, categories=[category])


def _all_responses_wrong(responses: list[Response]) -> bool:
    return bool(responses) and all(response.correctness == 0 for response in responses)


def _parse_responses(root: Tag | None) -> tuple[list[Response], bool]:
    responses: list[Response] = []
    if root is None:
        return responses, False

    wrong_response_texts = _parenthesized_response_texts(root)
    is_triple_stumper = False
    seen: set[tuple[str | None, str | None, int | None]] = set()
    for node in root.select(".right, .wrong, .response, .right_response, .wrong_response"):
        text = _clean_text(node.get_text(" ", strip=True))
        if not text:
            continue
        classes = set(node.get("class", []))
        correctness = None
        if "right" in classes or "right_response" in classes:
            correctness = 1
        elif "wrong" in classes or "wrong_response" in classes:
            correctness = 0
        if correctness is None:
            continue
        if text.lower() == "triple stumper":
            is_triple_stumper = True
            continue

        contestant = text
        response_text = _adjacent_response_text(node)
        if response_text is None and correctness == 0:
            response_text = wrong_response_texts.get(contestant)

        key = (contestant, response_text, correctness)
        if key in seen:
            continue
        seen.add(key)
        responses.append(
            Response(
                contestant=contestant,
                response_text=response_text,
                correctness=correctness,
            )
        )
    return responses, is_triple_stumper


def _parse_scores(soup: BeautifulSoup) -> list[Score]:
    scores: list[Score] = []
    for table in soup.select("table.scores, #scores"):
        for row in table.select("tr"):
            cells = [_clean_text(cell.get_text(" ", strip=True)) for cell in row.select("th,td")]
            if len(cells) < 2:
                continue
            stage = cells[0]
            for value in cells[1:]:
                score = _parse_score(value)
                scores.append(Score(stage=stage, contestant=None, score=score))
    return scores


def _response_node_for(root: Tag, source_clue_id: str) -> Tag | None:
    if not source_clue_id:
        return None
    response_id = source_clue_id if source_clue_id.endswith("_r") else f"{source_clue_id}_r"
    node = root.find(id=response_id)
    return node if isinstance(node, Tag) else None


def _correct_response(visible_root: Tag, response_root: Tag | None) -> str | None:
    for root in (response_root, visible_root):
        if root is None:
            continue
        correct_node = root.select_one(".correct_response")
        if correct_node:
            text = _clean_text(correct_node.get_text(" ", strip=True))
            if text:
                return text
    return None


def _parenthesized_response_texts(root: Tag) -> dict[str, str]:
    correct_node = root.select_one(".correct_response")
    transcript = _clean_text(_text_before(root, correct_node))
    responses: dict[str, str] = {}
    for match in re.finditer(r"\((?P<name>[^:()]{1,80}):\s*(?P<response>[^()]*)\)", transcript):
        name = _clean_text(match.group("name"))
        response = _clean_text(match.group("response"))
        if name and response:
            responses[name] = response
    return responses


def _text_before(root: Tag, stop_node: Tag | None) -> str:
    parts: list[str] = []
    for child in root.children:
        if stop_node is not None and child is stop_node:
            break
        if isinstance(child, Tag):
            if stop_node is not None and stop_node in child.descendants:
                break
            if child.name == "br":
                parts.append("\n")
            elif child.name != "table":
                parts.append(child.get_text(" ", strip=True))
        else:
            parts.append(str(child))
    return " ".join(parts)


def _adjacent_response_text(node: Tag) -> str | None:
    row = node.find_parent("tr")
    if not row:
        return None
    cells = [cell for cell in row.find_all("td", recursive=False)]
    try:
        index = cells.index(node)
    except ValueError:
        return None
    for cell in cells[index + 1 :]:
        classes = set(cell.get("class", []))
        if classes.intersection({"right", "wrong", "right_response", "wrong_response"}):
            continue
        text = _clean_text(cell.get_text(" ", strip=True))
        if text and not _parse_score(text):
            return text
    return None


def _parse_score(text: str) -> int | None:
    match = re.search(r"-?\$?[\d,]+", text)
    if not match:
        return None
    score_text = match.group(0).replace("$", "").replace(",", "")
    if not score_text or not re.search(r"\d", score_text):
        return None
    return int(score_text)


def _query_value(url: str, key: str) -> str:
    parsed = urlparse(url)
    values = parse_qs(parsed.query).get(key)
    if not values:
        raise ValueError(f"Missing {key!r} in URL: {url}")
    return values[0]


def _anchor_line(anchor: Tag) -> str:
    parts = [anchor.get_text(" ", strip=True)]
    for sibling in anchor.next_siblings:
        if isinstance(sibling, Tag):
            if sibling.name == "br" or sibling.name == "a":
                break
            text = sibling.get_text(" ", strip=True)
        else:
            text = str(sibling)
        if text:
            parts.append(text)
    line = _clean_text(" ".join(parts))
    if len(line) <= len(parts[0]) + 2:
        parent = anchor.find_parent(["tr", "p", "li", "div"])
        if parent:
            return _clean_text(parent.get_text(" ", strip=True))
    return line


def _extract_date_range(line: str, name: str) -> str | None:
    text = line.replace(name, "", 1)
    text = re.sub(r"\(\d+\s+games?\s+archived\)", "", text, flags=re.I)
    text = _clean_text(text)
    return text or None


def _date_bounds(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", text)
    if not dates:
        return None, None
    if len(dates) == 1:
        return dates[0], None
    return dates[0], dates[1]


def _show_and_airdate(text: str) -> tuple[str | None, str | None]:
    match = re.search(
        r"#(?P<show>[\w-]+)\s*,\s+aired\s+(?P<date>\d{4}-\d{2}-\d{2})",
        text,
        re.I,
    )
    if not match:
        return None, None
    return match.group("show"), match.group("date")


def _clean_title(line: str, anchor_text: str) -> str | None:
    show_listing = re.match(
        r"^\s*(?:Show\s+)?#?[\w-]+\s*,\s+aired\s+\d{4}-\d{2}-\d{2}\s*(?P<separator>[:,-])?\s*(?P<rest>.*)$",
        line,
        re.I,
    )
    if show_listing:
        if show_listing.group("separator") == ":":
            return _normalize_game_title(show_listing.group("rest"))
        return None

    return _normalize_game_title(line)


def _season_link_id(soup: BeautifulSoup) -> str | None:
    anchors = soup.find_all("a", href=re.compile(r"showseason\.php\?season="))
    for anchor in anchors:
        if anchor.find_parent(id="navbar"):
            continue
        anchor_text = _clean_text(anchor.get_text(" ", strip=True))
        if not re.search(r"\bseason\b", anchor_text, re.I):
            continue
        try:
            canonical = canonicalize_url(str(anchor["href"]))
        except ValueError:
            continue
        return _query_value(canonical.canonical_url, "season")
    return None


def _page_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return _clean_text(soup.title.string)
    heading = soup.find(["h1", "h2"])
    return _clean_text(heading.get_text(" ", strip=True)) if heading else None


def _air_date_from_title(title: str | None) -> str | None:
    if not title:
        return None
    match = re.search(r"\baired\s+(\d{4}-\d{2}-\d{2})\b", title, re.I)
    return match.group(1) if match else None


def _normalize_game_title(title: str | None) -> str | None:
    if not title:
        return None
    normalized = _clean_text(title)
    normalized = re.sub(r"^J!\s+Archive\s*-\s*", "", normalized, flags=re.I)
    normalized = re.sub(
        r"\s*,\s*aired\s+\d{4}-\d{2}-\d{2}.*$",
        "",
        normalized,
        flags=re.I,
    )
    normalized = re.sub(
        r"\s*-\s*[A-Za-z]+,\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}\s*$",
        "",
        normalized,
    )
    normalized = _clean_text(normalized)
    if re.fullmatch(r"(?:Show\s+)?#?[\w-]+", normalized, flags=re.I):
        return None
    return normalized or None


def _clue_position(source_clue_id: str) -> tuple[int | None, int | None]:
    match = re.search(r"clue_(?:J|DJ)_(\d+)_(\d+)", source_clue_id)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
