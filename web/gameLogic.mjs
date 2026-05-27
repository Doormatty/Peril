export const DEFAULT_LIMIT = 90;
export const SEARCH_DELAY_MS = 180;
export const STORAGE_PREFIX = "peril-game:";

export function databaseUrlCandidates({ search = "", pathname = "", config = {} } = {}) {
  const params = new URLSearchParams(search);
  const configured = params.get("db") || config.dbUrl || "jarchive.sqlite3";
  const urls = [];

  urls.push(configured);
  if (!params.has("db") && !configured.includes("/") && !configured.startsWith(".") && pathname.includes("/web/")) {
    urls.push(`../${configured}`);
  }
  return [...new Set(urls)];
}

export function catalogUrl({ search = "", config = {} } = {}) {
  const params = new URLSearchParams(search);
  return params.get("catalog") || config.catalogUrl || "catalog.json";
}

export function shardUrl(shardName, { search = "", config = {} } = {}) {
  const shard = String(shardName || "");
  if (!shard) {
    throw new Error("Game has no shard assignment.");
  }
  if (/^(?:[a-z]+:)?\/\//i.test(shard) || shard.startsWith("/") || shard.startsWith(".")) {
    return shard;
  }
  const params = new URLSearchParams(search);
  const base = params.get("shardBase") || config.shardBaseUrl || "shards/";
  return `${base.replace(/\/?$/, "/")}${shard}`;
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
      rows.add(clue.row_value || 1);
    }
  }
  return [...rows].sort((a, b) => a - b);
}

export function findClueForRow(category, row) {
  return category.clues.find((clue) => (clue.row_value || 1) === row);
}

export function clueLabel(round, clue) {
  if (isFinalRound(round, clue)) {
    return "Final";
  }
  const amount = baseClueAmount(round, clue);
  return amount ? formatMoney(amount) : "$?";
}

export function clueValueLabel(round, clue) {
  const value = isFinalRound(round, clue)
    ? "Final"
    : baseClueAmount(round, clue)
      ? formatMoney(baseClueAmount(round, clue))
      : "Clue";
  const parts = [value];
  if (clue.is_daily_double) {
    parts.push("Daily Double");
  }
  if (clue.is_triple_stumper) {
    parts.push("Triple Stumper");
  }
  return parts.join(" · ");
}

export function isFinalRound(round, clue = null) {
  return Boolean(clue?.is_final_jeopardy) || /final/i.test(round?.name || "");
}

export function requiresWager(round, clue) {
  return Boolean(clue?.is_daily_double) || isFinalRound(round, clue);
}

export function scoringAmount(round, clue, { score, wagerValue } = {}) {
  if (requiresWager(round, clue)) {
    const max = maxWager(round, clue, score);
    const value = Number.parseInt(wagerValue, 10);
    if (!Number.isFinite(value)) {
      return 0;
    }
    return Math.max(0, Math.min(value, max));
  }
  return baseClueAmount(round, clue);
}

export function maxWager(round, clue, score = 0) {
  if (isFinalRound(round, clue)) {
    return Math.max(0, score);
  }
  const faceValue = baseClueAmount(round, clue);
  return Math.max(score, faceValue);
}

export function defaultWager(round, clue, max) {
  if (isFinalRound(round, clue)) {
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

export function answeredCluePopupText(round, category, clue) {
  return [
    category?.name ? `Category: ${category.name}` : null,
    `Value: ${clueValueLabel(round, clue)}`,
    "",
    `Question: ${clue?.clue_text || "Not archived"}`,
    "",
    `Answer: ${clue?.correct_response || "Not archived"}`
  ]
    .filter((line) => line !== null)
    .join("\n");
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

export function normalizeAnswerText(value) {
  return String(value ?? "")
    .replace(/<[^>]*>/g, " ")
    .replace(/&(?:nbsp|amp|quot|apos|#39|#x27);/gi, decodeAnswerEntity)
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[&]/g, " and ")
    .replace(/[^a-z0-9]+/gi, " ")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

export function answerMatches(attempt, correctResponse) {
  const attemptVariants = answerTextVariants(attempt);
  const correctVariants = new Set(answerTextVariants(correctResponse));
  return attemptVariants.some((variant) => correctVariants.has(variant));
}

function answerTextVariants(value) {
  const values = new Set([String(value ?? "")]);
  for (const text of [...values]) {
    values.add(text.replace(/\([^)]*\)/g, " "));
    values.add(text.replace(/\[[^\]]*\]/g, " "));
  }

  const variants = new Set();
  for (const text of values) {
    const normalized = normalizeAnswerText(text);
    if (!normalized) {
      continue;
    }
    variants.add(normalized);
    const stripped = normalized.replace(
      /^(?:who|what|where|when|why|how)\s+(?:is|are|was|were|am|be|been|being|s)\s+/,
      ""
    );
    if (stripped) {
      variants.add(stripped);
    }
  }
  return [...variants];
}

function decodeAnswerEntity(entity) {
  switch (entity.toLowerCase()) {
    case "&nbsp;":
      return " ";
    case "&amp;":
      return "&";
    case "&quot;":
      return "\"";
    case "&apos;":
    case "&#39;":
    case "&#x27;":
      return "'";
    default:
      return " ";
  }
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
