export const DEFAULT_LIMIT = 90;
export const SEARCH_DELAY_MS = 180;
export const STORAGE_PREFIX = "peril-game:";

export function databaseUrlCandidates({ search = "", pathname = "", config = {} } = {}) {
  const params = new URLSearchParams(search);
  const configured = params.get("db") || config.dbUrl || "jarchive.sqlite3";
  const urls = [];

  if (!params.has("db") && !configured.includes("/") && pathname.includes("/web/")) {
    urls.push(`../${configured}`);
  }
  urls.push(configured);
  if (!configured.includes("/") && !configured.startsWith(".")) {
    urls.push(`../${configured}`);
  }
  return [...new Set(urls)];
}

export function listPlayableGames(allGames, search, limit = DEFAULT_LIMIT) {
  const terms = search
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (terms.length === 0) {
    return allGames.slice(0, limit);
  }

  const matches = [];
  for (const game of allGames) {
    if (terms.every((term) => game.searchText.includes(term))) {
      matches.push(game);
      if (matches.length >= limit) {
        break;
      }
    }
  }
  return matches;
}

export function searchableGameText(game) {
  return [game.game_id, game.show_number, game.air_date, game.title]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function searchableSeasonText(season) {
  return [
    season.season_id,
    season.name,
    season.date_start,
    season.date_end,
    season.date_range_text
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function seasonTotalGames(season) {
  const parsed = Number(season.parsed_game_count) || 0;
  const archived = Number(season.archived_game_count) || 0;
  const known = Number(season.known_game_count) || 0;
  return Math.max(parsed, archived > 0 ? archived : known);
}

export function seasonProgressLabel(season) {
  return `${Number(season.parsed_game_count) || 0}/${seasonTotalGames(season)}`;
}

export function firstPlayableRoundIndex(game) {
  const index = game.rounds.findIndex((round) => round.clue_count > 0);
  return index === -1 ? 0 : index;
}

export function clueRows(categories) {
  const rows = new Set();
  for (const category of categories) {
    for (const clue of category.clues) {
      rows.add(clue.row_value || clue.clue_order || 1);
    }
  }
  return [...rows].sort((a, b) => a - b);
}

export function findClueForRow(category, row) {
  return category.clues.find((clue) => (clue.row_value || clue.clue_order || 1) === row);
}

export function clueLabel(round, clue) {
  if (isFinalRound(round)) {
    return "Final";
  }
  if (clue.is_daily_double) {
    return formatMoney(baseClueAmount(round, clue));
  }
  return clue.dollar_value || "$?";
}

export function clueValueLabel(round, clue) {
  const value = isFinalRound(round)
    ? "Final"
    : clue.is_daily_double
      ? formatMoney(baseClueAmount(round, clue))
      : clue.dollar_value || "Clue";
  const parts = [value];
  if (clue.is_daily_double) {
    parts.push("Daily Double");
  }
  return parts.join(" · ");
}

export function isFinalRound(round) {
  return /final/i.test(round.name || "");
}

export function scoringAmount(round, clue, { score, wagerValue } = {}) {
  if (clue.is_daily_double || isFinalRound(round)) {
    const max = maxWager(round, clue, score);
    const value = Number.parseInt(wagerValue, 10);
    if (!Number.isFinite(value)) {
      return 0;
    }
    return Math.max(0, Math.min(value, max));
  }
  return clue.value_amount || 0;
}

export function maxWager(round, clue, score = 0) {
  if (isFinalRound(round)) {
    return Math.max(0, score);
  }
  const faceValue = baseClueAmount(round, clue);
  return Math.max(score, faceValue);
}

export function defaultWager(round, clue, max) {
  if (isFinalRound(round)) {
    return max;
  }
  return Math.min(max, baseClueAmount(round, clue));
}

export function baseClueAmount(round, clue) {
  if (!clue.row_value) {
    return clue.value_amount || 0;
  }
  const multiplier = /double/i.test(round.name || "") ? 400 : 200;
  return clue.row_value * multiplier;
}

export function progressKey(gameId) {
  return `${STORAGE_PREFIX}${gameId}`;
}

export function placeholders(count) {
  return Array.from({ length: count }, () => "?").join(", ");
}

export function parseDollarValue(value) {
  if (!value) {
    return 0;
  }
  const match = String(value).match(/-?\$?[\d,]+/);
  if (!match) {
    return 0;
  }
  return Number.parseInt(match[0].replace(/\$|,/g, ""), 10) || 0;
}

export function formatMoney(value) {
  const sign = value < 0 ? "-" : "";
  return `${sign}$${Math.abs(value).toLocaleString()}`;
}

export function gameMeta(game) {
  return [
    game.air_date || "No air date",
    game.show_number ? `#${game.show_number}` : null,
    `${game.clue_count} clues`
  ]
    .filter(Boolean)
    .join(" · ");
}
