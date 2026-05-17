import {
  SEARCH_DELAY_MS,
  clueLabel,
  clueRows,
  clueValueLabel,
  databaseUrlCandidates,
  defaultWager,
  findClueForRow,
  firstPlayableRoundIndex,
  formatMoney,
  gameMeta,
  isFinalRound,
  maxWager,
  parseDollarValue,
  placeholders,
  progressKey as buildProgressKey,
  scoringAmount,
  searchableGameText,
  searchableSeasonText,
  seasonProgressLabel,
  seasonTotalGames
} from "./gameLogic.mjs";

const dom = {
  app: document.querySelector("#app"),
  score: document.querySelector("#score"),
  gamePanelToggle: document.querySelector("#game-panel-toggle"),
  sidebarContent: document.querySelector(".sidebar-content"),
  gameSearch: document.querySelector("#game-search"),
  randomGame: document.querySelector("#random-game"),
  gameStatus: document.querySelector("#game-status"),
  gameList: document.querySelector("#game-list"),
  gameKicker: document.querySelector("#game-kicker"),
  gameTitle: document.querySelector("#game-title"),
  resetGame: document.querySelector("#reset-game"),
  roundTabs: document.querySelector("#round-tabs"),
  board: document.querySelector("#board"),
  clueDialog: document.querySelector("#clue-dialog"),
  clueCategory: document.querySelector("#clue-category"),
  clueValue: document.querySelector("#clue-value"),
  clueText: document.querySelector("#clue-text"),
  wagerArea: document.querySelector("#wager-area"),
  wagerInput: document.querySelector("#wager-input"),
  playerResponse: document.querySelector("#player-response"),
  correctResponse: document.querySelector("#correct-response"),
  closeClue: document.querySelector("#close-clue"),
  revealResponse: document.querySelector("#reveal-response"),
  markCorrect: document.querySelector("#mark-correct"),
  markWrong: document.querySelector("#mark-wrong"),
  markPass: document.querySelector("#mark-pass")
};

const state = {
  db: null,
  seasons: [],
  allGames: [],
  games: [],
  currentGame: null,
  activeRoundIndex: 0,
  activeClue: null,
  score: 0,
  answered: new Set(),
  expandedSeasonIds: new Set(),
  collapsedSeasonIds: new Set(),
  gamePanelCollapsed: false,
  searchTimer: 0,
  loadingGameId: null
};

const EFFECTIVE_SEASON_ID_SQL = `
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
`;

init().catch((error) => {
  console.error(error);
  setStatus(error.message || "Could not load game data.", true);
});

async function init() {
  setStatus("Loading sql.js...");
  await loadSqlJsScript();
  const SQL = await window.initSqlJs({
    locateFile: (filename) => {
      if (filename.endsWith(".wasm")) {
        return getConfig().sqlWasmUrl;
      }
      return filename;
    }
  });

  setStatus("Loading database...");
  const databaseBytes = await fetchDatabaseBytes();
  state.db = new SQL.Database(new Uint8Array(databaseBytes));
  state.allGames = loadPlayableGameSummaries();
  state.seasons = loadSeasonSummaries();

  bindEvents();
  refreshGameList();
  if (state.games.length > 0) {
    await loadGame(state.games[0].game_id);
  } else {
    renderEmptyBoard("No playable games found.");
  }
}

function bindEvents() {
  dom.gameSearch.addEventListener("input", () => {
    window.clearTimeout(state.searchTimer);
    state.searchTimer = window.setTimeout(refreshGameList, SEARCH_DELAY_MS);
  });

  dom.gamePanelToggle.addEventListener("click", () => {
    setGamePanelCollapsed(!state.gamePanelCollapsed);
  });

  dom.gameList.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : event.target.parentElement;
    const seasonToggle = target?.closest(".season-toggle");
    if (seasonToggle && dom.gameList.contains(seasonToggle)) {
      toggleSeason(seasonToggle.dataset.seasonId);
      return;
    }

    const gameButton = target?.closest(".game-option");
    if (!gameButton || !dom.gameList.contains(gameButton)) {
      return;
    }
    loadGame(gameButton.dataset.gameId);
  });

  dom.randomGame.addEventListener("click", async () => {
    if (state.games.length === 0) {
      return;
    }
    const game = state.games[Math.floor(Math.random() * state.games.length)];
    await loadGame(game.game_id);
  });

  dom.resetGame.addEventListener("click", () => {
    if (!state.currentGame) {
      return;
    }
    state.score = 0;
    state.answered = new Set();
    saveProgress();
    renderScore();
    renderBoard();
  });

  dom.closeClue.addEventListener("click", closeClueDialog);
  dom.revealResponse.addEventListener("click", revealResponse);
  dom.markCorrect.addEventListener("click", () => scoreActiveClue("correct"));
  dom.markWrong.addEventListener("click", () => scoreActiveClue("wrong"));
  dom.markPass.addEventListener("click", () => scoreActiveClue("pass"));
}

function setGamePanelCollapsed(collapsed) {
  state.gamePanelCollapsed = collapsed;
  dom.app.classList.toggle("game-panel-collapsed", collapsed);
  dom.sidebarContent.hidden = collapsed;
  dom.gamePanelToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  dom.gamePanelToggle.setAttribute(
    "aria-label",
    collapsed ? "Expand game selection" : "Collapse game selection"
  );
}

async function loadSqlJsScript() {
  if (window.initSqlJs) {
    return;
  }
  await new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = getConfig().sqlJsUrl;
    script.async = true;
    script.onload = resolve;
    script.onerror = () => reject(new Error("Could not load sql.js."));
    document.head.append(script);
  });
}

async function fetchDatabaseBytes() {
  const urls = databaseUrlCandidates({
    search: window.location.search,
    pathname: window.location.pathname,
    config: getConfig()
  });
  let lastError = null;
  for (const url of urls) {
    try {
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`${response.status} ${response.statusText}`);
      }
      return await response.arrayBuffer();
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(`Could not load SQLite database. Last error: ${lastError?.message || "unknown"}`);
}

function refreshGameList({ updateStatus = true, autoExpandSeason = true } = {}) {
  const query = dom.gameSearch.value.trim();
  const games = filterPlayableGames(query);
  const seasons = filterSeasons(query, games);
  state.games = games;
  if (autoExpandSeason) {
    ensureExpandedSeason(seasons, games, query);
  }
  renderGameList(seasons, games, query);
  dom.randomGame.disabled = games.length === 0;
  if (updateStatus) {
    const gameWord = games.length === 1 ? "game" : "games";
    const seasonWord = seasons.length === 1 ? "season" : "seasons";
    setStatus(`${games.length} parsed ${gameWord} across ${seasons.length} ${seasonWord}`);
  }
}

function filterPlayableGames(query) {
  const terms = searchTerms(query);
  if (terms.length === 0) {
    return state.allGames;
  }

  const seasonsById = new Map(state.seasons.map((season) => [seasonKey(season.season_id), season]));
  return state.allGames.filter((game) => {
    const season = seasonsById.get(seasonKey(game.season_id));
    const searchText = `${game.searchText} ${season?.searchText || ""}`;
    return terms.every((term) => searchText.includes(term));
  });
}

function filterSeasons(query, games) {
  const terms = searchTerms(query);
  if (terms.length === 0) {
    return state.seasons;
  }

  const gameSeasonIds = new Set(games.map((game) => seasonKey(game.season_id)));
  return state.seasons.filter((season) => {
    return (
      gameSeasonIds.has(seasonKey(season.season_id)) ||
      terms.every((term) => season.searchText.includes(term))
    );
  });
}

function searchTerms(query) {
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
}

function ensureExpandedSeason(seasons, games, query) {
  if (query) {
    return;
  }

  const currentSeasonId = state.currentGame?.game.season_id;
  if (currentSeasonId != null) {
    expandSeason(currentSeasonId);
    return;
  }

  if (seasons.some((season) => isSeasonExpanded(season.season_id, ""))) {
    return;
  }

  const gameSeasonIds = new Set(games.map((game) => seasonKey(game.season_id)));
  const firstSeasonWithGames = seasons.find((season) => gameSeasonIds.has(seasonKey(season.season_id)));
  const firstSeason = firstSeasonWithGames || seasons[0];
  if (firstSeason) {
    expandSeason(firstSeason.season_id);
  }
}

function toggleSeason(seasonId) {
  const key = seasonKey(seasonId);
  if (!key) {
    return;
  }
  if (isSeasonExpanded(key, dom.gameSearch.value.trim())) {
    state.expandedSeasonIds.delete(key);
    state.collapsedSeasonIds.add(key);
  } else {
    state.collapsedSeasonIds.delete(key);
    state.expandedSeasonIds.add(key);
  }
  refreshGameList({ updateStatus: false, autoExpandSeason: false });
}

function expandSeason(seasonId) {
  const key = seasonKey(seasonId);
  if (!key || state.collapsedSeasonIds.has(key)) {
    return;
  }
  state.expandedSeasonIds.add(key);
}

function isSeasonExpanded(seasonId, query) {
  const key = seasonKey(seasonId);
  return !state.collapsedSeasonIds.has(key) && (Boolean(query) || state.expandedSeasonIds.has(key));
}

function loadPlayableGameSummaries() {
  return allRows(
    `
    SELECT
      g.game_id,
      g.show_number,
      g.air_date,
      ${EFFECTIVE_SEASON_ID_SQL} AS season_id,
      g.title,
      COUNT(DISTINCT r.id) AS round_count,
      COUNT(DISTINCT ca.id) AS category_count,
      COUNT(c.id) AS clue_count
    FROM games g
    LEFT JOIN seasons stored_season ON stored_season.season_id = g.season_id
    JOIN rounds r ON r.game_id = g.game_id
    JOIN categories ca ON ca.round_id = r.id
    JOIN clues c ON c.category_id = ca.id
    GROUP BY g.game_id
    ORDER BY
      g.air_date DESC,
      CAST(g.game_id AS INTEGER) DESC,
      g.game_id DESC
    `
  ).map((game) => ({
    ...game,
    searchText: searchableGameText(game)
  }));
}

function loadSeasonSummaries() {
  return allRows(
    `
    WITH effective_games AS (
      SELECT
        g.game_id,
        ${EFFECTIVE_SEASON_ID_SQL} AS season_id
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
      JOIN rounds r ON r.game_id = eg.game_id
      JOIN categories ca ON ca.round_id = r.id
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
    `
  ).map((season) => ({
    ...season,
    searchText: searchableSeasonText(season)
  }));
}

function renderGameList(seasons, games, query) {
  const groups = groupGamesBySeason(games);
  const nodes = [];
  for (const season of seasons) {
    const seasonId = seasonKey(season.season_id);
    const expanded = isSeasonExpanded(seasonId, query);
    const seasonGames = groups.get(seasonId) || [];
    const section = document.createElement("section");
    section.className = "season-group";

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "season-toggle";
    toggle.dataset.seasonId = seasonId;
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");

    const text = document.createElement("span");
    text.className = "season-text";
    const title = document.createElement("strong");
    title.textContent = season.name || `Season ${season.season_id}`;
    const meta = document.createElement("span");
    meta.textContent = season.date_range_text || "No date range";
    text.append(title, meta);

    const count = document.createElement("span");
    count.className = "season-count";
    count.textContent = seasonProgressLabel(season);
    count.setAttribute(
      "aria-label",
      `${season.parsed_game_count || 0} parsed of ${seasonTotalGames(season)} games`
    );
    toggle.append(text, count);
    section.append(toggle);

    if (expanded) {
      const gameList = document.createElement("div");
      gameList.className = "season-games";
      if (seasonGames.length === 0) {
        const empty = document.createElement("p");
        empty.className = "season-empty";
        empty.textContent = "No parsed games available.";
        gameList.append(empty);
      } else {
        for (const game of seasonGames) {
          gameList.append(createGameButton(game));
        }
      }
      section.append(gameList);
    }
    nodes.push(section);
  }

  if (nodes.length === 0) {
    const empty = document.createElement("p");
    empty.className = "season-empty";
    empty.textContent = "No seasons match this search.";
    nodes.push(empty);
  }
  dom.gameList.replaceChildren(...nodes);
}

function createGameButton(game) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "game-option";
  button.dataset.gameId = game.game_id;
  button.setAttribute(
    "aria-current",
    state.currentGame?.game.game_id === game.game_id ? "true" : "false"
  );

  const title = document.createElement("strong");
  title.textContent = game.title || `Show #${game.show_number || game.game_id}`;
  const meta = document.createElement("span");
  meta.textContent = gameMeta(game);
  button.append(title, meta);
  return button;
}

function groupGamesBySeason(games) {
  const groups = new Map();
  for (const game of games) {
    const key = seasonKey(game.season_id);
    const group = groups.get(key) || [];
    group.push(game);
    groups.set(key, group);
  }
  return groups;
}

function seasonKey(seasonId) {
  return seasonId == null ? "" : String(seasonId);
}

async function loadGame(gameId) {
  if (!state.db || state.loadingGameId === gameId) {
    return;
  }
  state.loadingGameId = gameId;
  setStatus(`Loading game ${gameId}...`);
  try {
    const game = getGame(gameId);
    if (!game) {
      setStatus(`Game ${gameId} was not found.`, true);
      return;
    }
    state.currentGame = game;
    state.collapsedSeasonIds.delete(seasonKey(game.game.season_id));
    state.activeRoundIndex = firstPlayableRoundIndex(game);
    loadProgress();
    renderScore();
    renderGameHeader();
    refreshGameList({ updateStatus: false });
    renderRoundTabs();
    renderBoard();
    dom.resetGame.disabled = false;
    setStatus(`Game ${gameId} loaded`);
  } finally {
    state.loadingGameId = null;
  }
}

function getGame(gameId) {
  const game = oneRow(
    `
    SELECT
      g.game_id,
      g.show_number,
      g.air_date,
      ${EFFECTIVE_SEASON_ID_SQL} AS season_id,
      g.title,
      g.notes,
      g.source_url
    FROM games g
    LEFT JOIN seasons stored_season ON stored_season.season_id = g.season_id
    WHERE g.game_id = ?
    `,
    [gameId]
  );
  if (!game) {
    return null;
  }

  const roundRows = allRows(
    `
    SELECT id, name, round_order
    FROM rounds
    WHERE game_id = ?
    ORDER BY round_order, id
    `,
    [gameId]
  );
  const roundIds = roundRows.map((round) => round.id);
  const categoryRows = roundIds.length > 0
    ? allRows(
        `
        SELECT id, round_id, name, board_position
        FROM categories
        WHERE round_id IN (${placeholders(roundIds.length)})
        ORDER BY round_id, board_position, id
        `,
        roundIds
      )
    : [];
  const categoryIds = categoryRows.map((category) => category.id);
  const clueRows = categoryIds.length > 0
    ? allRows(
        `
        SELECT
          id,
          category_id,
          row_value,
          dollar_value,
          clue_text,
          correct_response,
          clue_order,
          is_daily_double,
          source_clue_id
        FROM clues
        WHERE category_id IN (${placeholders(categoryIds.length)})
        ORDER BY category_id, COALESCE(row_value, clue_order), clue_order, id
        `,
        categoryIds
      )
    : [];

  const cluesByCategory = new Map();
  for (const clue of clueRows) {
    const categoryClues = cluesByCategory.get(clue.category_id) || [];
    categoryClues.push({
      ...clue,
      value_amount: parseDollarValue(clue.dollar_value),
      is_daily_double: Boolean(clue.is_daily_double)
    });
    cluesByCategory.set(clue.category_id, categoryClues);
  }

  const categoriesByRound = new Map();
  for (const category of categoryRows) {
    const roundCategories = categoriesByRound.get(category.round_id) || [];
    roundCategories.push({
      ...category,
      clues: cluesByCategory.get(category.id) || []
    });
    categoriesByRound.set(category.round_id, roundCategories);
  }

  const rounds = roundRows.map((round) => {
    const categories = categoriesByRound.get(round.id) || [];
    return {
      ...round,
      categories,
      clue_count: categories.reduce((sum, category) => sum + category.clues.length, 0)
    };
  });

  return {
    game: {
      ...game,
      clue_count: rounds.reduce((sum, round) => sum + round.clue_count, 0)
    },
    contestants: allRows(
      `
      SELECT name, position_order, notes
      FROM contestants
      WHERE game_id = ?
      ORDER BY position_order
      `,
      [gameId]
    ),
    rounds
  };
}

function renderGameHeader() {
  const game = state.currentGame?.game;
  if (!game) {
    return;
  }
  dom.gameKicker.textContent = [game.air_date, game.show_number ? `Show #${game.show_number}` : null]
    .filter(Boolean)
    .join(" · ");
  dom.gameTitle.textContent = game.title || `Game ${game.game_id}`;
}

function renderRoundTabs() {
  dom.roundTabs.replaceChildren();
  const rounds = state.currentGame?.rounds || [];
  rounds.forEach((round, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "round-tab";
    button.textContent = round.name;
    button.setAttribute("aria-selected", index === state.activeRoundIndex ? "true" : "false");
    button.disabled = round.clue_count === 0;
    button.addEventListener("click", () => {
      state.activeRoundIndex = index;
      renderRoundTabs();
      renderBoard();
    });
    dom.roundTabs.append(button);
  });
}

function renderBoard() {
  const round = currentRound();
  if (!round) {
    renderEmptyBoard("No rounds are available.");
    return;
  }
  const categories = round.categories.filter((category) => category.clues.length > 0);
  if (categories.length === 0) {
    renderEmptyBoard("No clues are available for this round.");
    return;
  }

  dom.board.className = "board";
  dom.board.style.setProperty("--columns", String(categories.length));
  const cells = [];

  for (const category of categories) {
    const cell = document.createElement("div");
    cell.className = "category";
    cell.textContent = category.name;
    cells.push(cell);
  }

  const rows = clueRows(categories);
  for (const row of rows) {
    for (const category of categories) {
      const clue = findClueForRow(category, row);
      if (!clue) {
        const placeholder = document.createElement("div");
        placeholder.className = "clue-placeholder";
        cells.push(placeholder);
        continue;
      }
      const button = document.createElement("button");
      button.type = "button";
      button.className = "clue-tile";
      if (state.answered.has(String(clue.id))) {
        button.classList.add("answered");
        button.disabled = true;
      }
      button.textContent = clueLabel(round, clue);
      button.addEventListener("click", () => openClueDialog(round, category, clue));
      cells.push(button);
    }
  }
  dom.board.replaceChildren(...cells);
}

function renderEmptyBoard(message) {
  dom.board.className = "empty-state";
  dom.board.removeAttribute("style");
  dom.board.textContent = message;
}

function openClueDialog(round, category, clue) {
  state.activeClue = { round, category, clue };
  dom.clueCategory.textContent = category.name;
  dom.clueValue.textContent = clueValueLabel(round, clue);
  dom.clueText.textContent = clue.clue_text || "";
  dom.playerResponse.value = "";
  dom.correctResponse.hidden = true;
  dom.correctResponse.textContent = "";
  dom.revealResponse.hidden = false;
  dom.markCorrect.hidden = true;
  dom.markWrong.hidden = true;
  dom.markPass.hidden = true;
  configureWager(round, clue);
  dom.clueDialog.showModal();
  dom.playerResponse.focus();
}

function configureWager(round, clue) {
  const needsWager = clue.is_daily_double || isFinalRound(round);
  dom.wagerArea.hidden = !needsWager;
  if (!needsWager) {
    dom.wagerInput.value = "";
    return;
  }
  const max = maxWager(round, clue, state.score);
  dom.wagerInput.max = String(max);
  dom.wagerInput.min = "0";
  dom.wagerInput.value = String(defaultWager(round, clue, max));
}

function revealResponse() {
  if (!state.activeClue) {
    return;
  }
  const response = state.activeClue.clue.correct_response || "No archived response.";
  dom.correctResponse.textContent = response;
  dom.correctResponse.hidden = false;
  dom.revealResponse.hidden = true;
  dom.markCorrect.hidden = false;
  dom.markWrong.hidden = false;
  dom.markPass.hidden = false;
  dom.markCorrect.focus();
}

function scoreActiveClue(result) {
  if (!state.activeClue) {
    return;
  }
  const { round, clue } = state.activeClue;
  const amount = scoringAmount(round, clue, {
    score: state.score,
    wagerValue: dom.wagerInput.value
  });
  if (result === "correct") {
    state.score += amount;
  } else if (result === "wrong") {
    state.score -= amount;
  }
  state.answered.add(String(clue.id));
  saveProgress();
  renderScore();
  renderBoard();
  closeClueDialog();
}

function closeClueDialog() {
  state.activeClue = null;
  if (dom.clueDialog.open) {
    dom.clueDialog.close();
  }
}

function currentRound() {
  return state.currentGame?.rounds[state.activeRoundIndex] || null;
}

function renderScore() {
  dom.score.textContent = formatMoney(state.score);
}

function saveProgress() {
  if (!state.currentGame) {
    return;
  }
  const payload = {
    score: state.score,
    answered: [...state.answered]
  };
  window.localStorage.setItem(currentProgressKey(), JSON.stringify(payload));
}

function loadProgress() {
  state.score = 0;
  state.answered = new Set();
  if (!state.currentGame) {
    return;
  }
  const raw = window.localStorage.getItem(currentProgressKey());
  if (!raw) {
    return;
  }
  try {
    const payload = JSON.parse(raw);
    state.score = Number.isFinite(payload.score) ? payload.score : 0;
    state.answered = new Set(Array.isArray(payload.answered) ? payload.answered.map(String) : []);
  } catch {
    state.score = 0;
    state.answered = new Set();
  }
}

function allRows(sql, params = []) {
  const stmt = state.db.prepare(sql);
  try {
    stmt.bind(params);
    const rows = [];
    while (stmt.step()) {
      rows.push(stmt.getAsObject());
    }
    return rows;
  } finally {
    stmt.free();
  }
}

function oneRow(sql, params = []) {
  return allRows(sql, params)[0] || null;
}

function currentProgressKey() {
  return buildProgressKey(state.currentGame.game.game_id);
}

function setStatus(message, isError = false) {
  dom.gameStatus.textContent = message;
  dom.gameStatus.style.color = isError ? "var(--red)" : "";
}

function getConfig() {
  return window.PERIL_CONFIG || {};
}
