import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ACTIVITIES,
  LEVEL_IDS,
  activityById,
  advise,
  expectedLevel,
  lowestWeek,
  oneHot,
  planWeeks,
  riskScore,
  verdictForScore,
  verdictRank,
} from "../../site/js/advisor.js";
import { weekEnding, toISO } from "../../site/js/dates.js";

const rank = (probs, activity, vulnerable = false) => verdictRank(verdictForScore(riskScore(probs, activity, vulnerable)).id);

test("week ending is the following Sunday (or today if Sunday)", () => {
  assert.equal(toISO(weekEnding(new Date(2026, 9, 2))), "2026-10-04"); // Friday
  assert.equal(toISO(weekEnding(new Date(2026, 9, 4))), "2026-10-04"); // Sunday
  assert.equal(toISO(weekEnding(new Date(2026, 9, 5))), "2026-10-11"); // Monday
});

test("higher levels never give a gentler verdict", () => {
  for (const a of ACTIVITIES) {
    let prev = -1;
    for (const id of LEVEL_IDS) {
      const r = rank(oneHot(id), a);
      assert.ok(r >= prev, `${a.id} at ${id}`);
      prev = r;
    }
  }
});

test("being at higher risk never gives a gentler verdict", () => {
  for (const a of ACTIVITIES) {
    for (const id of LEVEL_IDS) assert.ok(rank(oneHot(id), a, true) >= rank(oneHot(id), a, false));
  }
});

test("sensible anchors", () => {
  assert.equal(verdictForScore(riskScore(oneHot("very_high"), activityById("outdoors"), false)).id, "go");
  assert.equal(verdictForScore(riskScore(oneHot("very_low"), activityById("crowd"), false)).id, "go");
  assert.equal(verdictForScore(riskScore(oneHot("very_high"), activityById("crowd"), false)).id, "avoid");
  assert.equal(verdictForScore(riskScore(oneHot("high"), activityById("visit_vulnerable"), false)).id, "postpone");
});

test("expected level averages over uncertainty", () => {
  assert.equal(expectedLevel(oneHot("very_low")), 0.5);
  assert.equal(expectedLevel([0, 0, 0.5, 0.5, 0]), 3);
});

const region = {
  latest: { date: "2026-09-20", category: "moderate", index: 55 },
  forecast: ["2026-09-27", "2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25", "2026-11-01"].map((date, i) => ({
    date,
    probs: i < 3 ? [0, 0, 0.1, 0.8, 0.1] : [0.3, 0.5, 0.2, 0, 0],
    category: i < 3 ? "high" : "low",
    index: i < 3 ? 70 : 30,
  })),
};

test("planWeeks maps calendar weeks onto forecast weeks", () => {
  const weeks = planWeeks(region, new Date(2026, 9, 2), 4);
  assert.deepEqual(weeks.map((w) => w.date), ["2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25"]);
  assert.ok(weeks.every((w) => w.available && w.source === "forecast"));
  const late = planWeeks(region, new Date(2026, 10, 20), 2);
  assert.equal(late[0].available, false);
});

test("planWeeks uses the measurement when this week has been reported", () => {
  const weeks = planWeeks(region, new Date(2026, 8, 18), 2);
  assert.equal(weeks[0].source, "measured");
  assert.deepEqual(weeks[0].probs, oneHot("moderate"));
});

test("advise suggests a gentler week when there is one", () => {
  const weeks = planWeeks(region, new Date(2026, 9, 2), 4);
  const result = advise({ weeks, weekIndex: 0, activity: activityById("crowd"), vulnerable: false });
  assert.ok(result.better, "expected a better week");
  assert.equal(result.better.week.date, "2026-10-18");
  assert.ok(result.tips.length > 0);
  assert.equal(lowestWeek(weeks), 2);
});

test("adviseEach keeps viruses separate and finds a week that suits all of them", async () => {
  const { adviseEach } = await import("../../site/js/advisor.js");
  const mk = (cats) => ({
    latest: { date: "2026-09-20", category: "low", index: 30 },
    forecast: ["2026-09-27", "2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25", "2026-11-01"].map((date, i) => ({
      date, probs: oneHot(cats[i]), category: cats[i], index: 50,
    })),
  });
  const today = new Date(2026, 9, 2);
  const byVirus = {
    covid: planWeeks(mk(["very_low", "very_low", "very_low", "very_low", "very_low", "very_low"]), today, 4),
    flu: planWeeks(mk(["high", "very_high", "very_high", "low", "low", "low"]), today, 4),
  };
  const out = adviseEach({ byVirus, weekIndex: 0, activity: activityById("crowd"), vulnerable: false });
  assert.equal(out.results.covid.verdict.id, "go");
  assert.equal(out.results.flu.verdict.id, "avoid"); // this week = w/e 4 Oct: very high flu + packed crowd
  assert.equal(out.results.covid.level.id, "very_low"); // verdicts stay per virus
  assert.equal(out.strictest.virus, "flu");
  assert.equal(out.better.week.date, "2026-10-18"); // first week flu drops to low
  assert.ok(out.tips.length > 0);
});

test("adviseEach never calls a week better when a virus has no forecast for it", async () => {
  const { adviseEach } = await import("../../site/js/advisor.js");
  const mk = (cats) => ({
    latest: { date: "2026-09-20", category: "low", index: 30 },
    forecast: cats.map((c, i) => ({
      date: ["2026-09-27", "2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25", "2026-11-01"][i], probs: oneHot(c), category: c, index: 50,
    })),
  });
  const today = new Date(2026, 9, 2);
  const byVirus = {
    covid: planWeeks(mk(["very_low", "very_low", "very_low", "very_low", "very_low", "very_low"]), today, 4),
    rsv: planWeeks(mk(["very_high", "very_high"]), today, 4), // stale area: forecast stops early
  };
  assert.ok(!byVirus.rsv[2].available);
  const out = adviseEach({ byVirus, weekIndex: 0, activity: activityById("crowd"), vulnerable: true });
  assert.equal(out.strictest.virus, "rsv");
  assert.equal(out.better, null); // the weeks after it have no RSV forecast, so they're not "better"
});
