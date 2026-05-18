import assert from "node:assert/strict";
import test from "node:test";

import {
  answerMatches,
  baseClueAmount,
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
  listPlayableGames,
  maxWager,
  normalizeAnswerText,
  parseDollarValue,
  progressKey,
  scoringAmount,
  searchableGameText,
  searchableSeasonText,
  seasonProgressLabel,
  seasonTotalGames
} from "../web/gameLogic.mjs";

test("databaseUrlCandidates prefers the colocated database when served from /web/", () => {
  assert.deepEqual(
    databaseUrlCandidates({
      pathname: "/web/index.html",
      config: { dbUrl: "jarchive.sqlite3" }
    }),
    ["jarchive.sqlite3", "../jarchive.sqlite3"]
  );
});

test("databaseUrlCandidates honors an explicit db query parameter", () => {
  assert.deepEqual(
    databaseUrlCandidates({
      search: "?db=/data/custom.sqlite3",
      pathname: "/web/index.html",
      config: { dbUrl: "jarchive.sqlite3" }
    }),
    ["/data/custom.sqlite3"]
  );
});

test("listPlayableGames matches all search terms and respects the limit", () => {
  const games = [
    gameSummary("8811", "9210", "2024-04-01", "Seattle Teachers Tournament"),
    gameSummary("8812", "9211", "2024-04-02", "Portland Writers Week"),
    gameSummary("8813", "9212", "2023-01-10", "Seattle Champions")
  ];

  assert.deepEqual(
    listPlayableGames(games, "Seattle 2024", 10).map((game) => game.game_id),
    ["8811"]
  );
  assert.deepEqual(
    listPlayableGames(games, "", 2).map((game) => game.game_id),
    ["8811", "8812"]
  );
});

test("round helpers select playable rounds and board rows", () => {
  const game = {
    rounds: [
      { name: "Intro", clue_count: 0 },
      { name: "Jeopardy!", clue_count: 3 }
    ]
  };
  const category = {
    clues: [
      { id: 1, row_value: 3 },
      { id: 2, row_value: null }
    ]
  };

  assert.equal(firstPlayableRoundIndex(game), 1);
  assert.deepEqual(clueRows([category]), [1, 3]);
  assert.equal(findClueForRow(category, 3).id, 1);
});

test("clue labels and base amounts follow round and daily double rules", () => {
  const singleRound = { name: "Jeopardy!" };
  const doubleRound = { name: "Double Jeopardy!" };
  const finalRound = { name: "Final Jeopardy!" };
  const dailyDouble = {
    row_value: 2,
    is_daily_double: true
  };

  assert.equal(baseClueAmount(singleRound, dailyDouble), 400);
  assert.equal(baseClueAmount(doubleRound, dailyDouble), 800);
  assert.equal(clueLabel(singleRound, dailyDouble), "$400");
  assert.equal(clueValueLabel(singleRound, dailyDouble), "$400 · Daily Double");
  assert.equal(clueLabel(finalRound, { is_daily_double: false }), "Final");
  assert.equal(clueLabel({ name: "Tiebreaker" }, { is_final_jeopardy: true }), "Final");
  assert.equal(isFinalRound(finalRound), true);
  assert.equal(isFinalRound({ name: "Tiebreaker" }, { is_final_jeopardy: true }), true);
});

test("wager scoring clamps invalid and excessive wager input", () => {
  const round = { name: "Double Jeopardy!" };
  const clue = {
    row_value: 3,
    value_amount: 1200,
    is_daily_double: true
  };

  assert.equal(maxWager(round, clue, 500), 1200);
  assert.equal(defaultWager(round, clue, 1200), 1200);
  assert.equal(scoringAmount(round, clue, { score: 500, wagerValue: "9999" }), 1200);
  assert.equal(scoringAmount(round, clue, { score: 500, wagerValue: "-50" }), 0);
  assert.equal(scoringAmount(round, clue, { score: 500, wagerValue: "abc" }), 0);
});

test("final round wager cannot exceed the current score", () => {
  const round = { name: "Final Jeopardy!" };
  const clue = { value_amount: 0, is_daily_double: false };

  assert.equal(maxWager(round, clue, -200), 0);
  assert.equal(defaultWager(round, clue, 2500), 2500);
  assert.equal(scoringAmount(round, clue, { score: 2500, wagerValue: "3000" }), 2500);
});

test("regular clue scoring is derived from round and row", () => {
  const round = { name: "Double Jeopardy!" };
  const clue = { row_value: 4, is_daily_double: false };

  assert.equal(clueLabel(round, clue), "$1,600");
  assert.equal(scoringAmount(round, clue, { score: 0, wagerValue: "" }), 1600);
});

test("formatting helpers keep UI labels stable", () => {
  assert.equal(parseDollarValue("$1,200"), 1200);
  assert.equal(parseDollarValue("-$400"), -400);
  assert.equal(parseDollarValue("not archived"), 0);
  assert.equal(formatMoney(-1200), "-$1,200");
  assert.equal(progressKey("8811"), "peril-game:8811");
  assert.equal(
    gameMeta({ air_date: "2024-04-01", show_number: "9210", clue_count: 61 }),
    "2024-04-01 · #9210 · 61 clues"
  );
});

test("answer matching accepts normalized player attempts", () => {
  assert.equal(normalizeAnswerText("Robert  Jordan!"), "robert jordan");
  assert.equal(answerMatches("robert  jordan", "Robert Jordan"), true);
  assert.equal(answerMatches("Who is Robert Jordan?", "Robert Jordan"), true);
  assert.equal(answerMatches("what's H2O", "H2O"), true);
  assert.equal(answerMatches("George Washington", "<em>George Washington</em> (first president)"), true);
  assert.equal(answerMatches("Robert", "Robert Jordan"), false);
  assert.equal(answerMatches("", "Robert Jordan"), false);
});

test("season helpers prefer archived totals for progress labels", () => {
  const season = {
    season_id: "42",
    name: "Season 42",
    date_range_text: "2025-09-08 to 2026-07-24",
    archived_game_count: 180,
    known_game_count: 438,
    parsed_game_count: 18
  };

  assert.equal(seasonTotalGames(season), 180);
  assert.equal(seasonProgressLabel(season), "18/180");
  assert.match(searchableSeasonText(season), /season 42/);
});

test("season helpers fall back to known totals without an archived count", () => {
  assert.equal(
    seasonProgressLabel({
      archived_game_count: null,
      known_game_count: 12,
      parsed_game_count: 5
    }),
    "5/12"
  );
});

function gameSummary(gameId, showNumber, airDate, title) {
  const game = {
    game_id: gameId,
    show_number: showNumber,
    air_date: airDate,
    title,
    clue_count: 61
  };
  return {
    ...game,
    searchText: searchableGameText(game)
  };
}
