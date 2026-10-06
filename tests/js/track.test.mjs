import assert from "node:assert/strict";
import { test } from "node:test";

import { toISO } from "../../site/js/dates.js";
import {
  COUNTRIES,
  COUNTRY_NAMES,
  HORIZONS,
  cleanRow,
  countryRows,
  expectedDate,
  gainVerdict,
  gapsFor,
  headline,
  horizonRows,
  introText,
  parseTrackRecord,
  rowCells,
  statusFor,
  statusNotes,
  unscoredNote,
  waiting,
} from "../../site/js/track.js";

const row = (h, weeks, extra = {}) => ({
  h,
  n: weeks * 50,
  issue_weeks: weeks,
  status: "ignored, worked out from the weeks",
  right_level: 0.72,
  right_level_no_change: 0.68,
  coverage_90: 0.88,
  relative_wis: 0.9,
  right_level_gain_ci90: [0.01, 0.07],
  ...extra,
});

const sample = () => ({
  schema: 1,
  generated_at: "2026-12-01T05:30:00Z",
  since: "2026-10-06T05:17:00Z",
  issues: 40,
  by_virus: {
    covid: {
      issues: 30,
      by_horizon: [row(1, 30), row(2, 12), row(3, 3), { h: 4, n: 0, issue_weeks: 0, status: "collecting" }],
      by_country: { germany: [row(1, 9), row(2, 2)], atlantis: [row(1, 30)] },
      unscored: { "not settled yet": 120, "interpolated target": 4, "issued after target known": 0, "no overlap": 1, "made up": 9 },
    },
    ebola: { issues: 3, by_horizon: [row(1, 30)] },
  },
  gaps: [
    { virus: "covid", country: "germany", newest: "2026-11-15T05:17:00Z", days_behind: 16 },
    { virus: "covid", country: "atlantis", newest: "2026-11-15T05:17:00Z" },
    { virus: "flu", country: "usa", newest: "not a date" },
  ],
});

test("the status follows the weeks checked, not the file's label", () => {
  assert.deepEqual([0, 7, 8, 25, 26, 99].map(statusFor), ["collecting", "collecting", "early", "early", "established", "established"]);
  assert.equal(cleanRow(row(1, 3, { status: "established" })).status, "collecting");
});

test("only known viruses, countries, horizons and reasons are read", () => {
  const rec = parseTrackRecord(sample());
  assert.deepEqual(Object.keys(rec.viruses), ["covid"]);
  assert.deepEqual(Object.keys(rec.viruses.covid.countries), ["germany"]);
  assert.equal(rec.since, "2026-10-06");
  assert.deepEqual(gapsFor(rec, "covid"), [{ country: "germany", name: "Germany", newest: "2026-11-15" }]);
  assert.deepEqual(gapsFor(rec, "flu"), []);
  assert.equal(rec.viruses.covid.unscored["made up"], undefined);
  assert.ok(COUNTRIES.every((c) => typeof c === "string"));
});

test("anything that isn't a track record is refused", () => {
  for (const junk of [null, 5, "x", [], {}, { schema: 2, by_virus: {} }, { schema: "1" }]) assert.equal(parseTrackRecord(junk), null);
  // what a build that isn't recording writes
  assert.deepEqual(parseTrackRecord({ schema: 1, recording: false, generated_at: "2026-10-06T05:30:00+00:00" }), { state: "off" });
  // prototype keys in the data can't reach Object.prototype
  const rec = parseTrackRecord(JSON.parse('{"schema":1,"by_virus":{"__proto__":{"issues":5},"constructor":{"issues":1}}}'));
  assert.deepEqual(rec.viruses, {});
  assert.equal({}.issues, undefined);
});

test("non-finite, out-of-range or wrongly typed numbers are dropped", () => {
  const r = cleanRow(row(2, 10, { right_level: 1.5, right_level_no_change: "0.5", coverage_90: -0.1, relative_wis: Infinity, right_level_gain_ci90: [0.2, 0.1], n: 3.5, issue_weeks: -4 }));
  assert.deepEqual(
    [r.rightLevel, r.rightNoChange, r.coverage90, r.relativeWis, r.gainCI, r.n, r.weeks, r.status],
    [null, null, null, null, null, 0, 0, "collecting"],
  );
  assert.equal(cleanRow({ h: 7 }), null);
  assert.equal(cleanRow({ h: "1" }), null);
  assert.equal(cleanRow(null), null);
  assert.deepEqual(rowCells(cleanRow(row(1, 10, { right_level: NaN, right_level_gain_ci90: [NaN, 1] }))), ["1", "10", "–", "68%", "–", "88%"]);
});

test("every horizon gets a row, and repeated horizons count once", () => {
  const data = sample();
  data.by_virus.covid.by_horizon.push(row(1, 2));
  const rows = horizonRows(parseTrackRecord(data), "covid");
  assert.deepEqual(rows.map((r) => r.h), HORIZONS);
  assert.deepEqual(rows.map((r) => r.status), ["established", "early", "collecting", "collecting", "collecting", "collecting"]);
  assert.equal(rows[0].weeks, 30);
  assert.deepEqual(horizonRows(null, "covid").map((r) => r.weeks), [0, 0, 0, 0, 0, 0]);
});

test("numbers only show from 8 weeks, with the difference and its range in points", () => {
  const rows = horizonRows(parseTrackRecord(sample()), "covid");
  assert.deepEqual(rowCells(rows[0]), ["1", "30", "72%", "68%", "+4.0 (+1.0 to +7.0)", "88%"]);
  assert.deepEqual(rowCells(rows[2]), ["3", "3 of 8", "–", "–", "–", "–"]);
  const worse = cleanRow(row(1, 9, { right_level: 0.6, right_level_gain_ci90: [-0.12, -0.0001] }));
  assert.deepEqual(rowCells(worse).slice(4, 5), ["−8.0 (−12.0 to ±0.0)"]);
});

test("the verdict follows the range as the table shows it", () => {
  // a tiny effect: equal percentages and a range of about zero must not read "worse"
  const tiny = cleanRow(row(4, 150, { right_level: 0.8105, right_level_no_change: 0.811, right_level_gain_ci90: [-0.0011, -0.0001] }));
  assert.deepEqual(rowCells(tiny).slice(2, 5), ["81%", "81%", "−0.1 (−0.1 to ±0.0)"]);
  assert.match(headline([tiny]), /4 weeks ahead 81% against 81%, about the same as no change\./);
  // a range whose end shows as 0 isn't "worse", whatever the unrounded number
  assert.match(headline([cleanRow(row(1, 30, { right_level_gain_ci90: [-0.12, -0.0001] }))]), /not clearly different/);
  assert.equal(gainVerdict([-0.12, -0.0004]), "not clearly different from no change");
  assert.equal(gainVerdict([-0.12, -0.0006]), "worse than no change"); // shows as −0.1
  assert.equal(gainVerdict([0.0004, 0.05]), "not clearly different from no change");
  assert.equal(gainVerdict([0.0006, 0.05]), "better than no change");
  // within 1 point either side, ends included: about the same, even when it leaves out 0
  assert.equal(gainVerdict([0.002, 0.009]), "about the same as no change");
  assert.equal(gainVerdict([-0.0094, 0.0094]), "about the same as no change");
  assert.equal(gainVerdict([-0.0096, 0.0094]), "about the same as no change"); // shows as −1.0 to +0.9
  assert.equal(gainVerdict([0.002, 0.0095]), "about the same as no change"); // shows as +0.2 to +1.0
  assert.equal(gainVerdict([0.002, 0.0105]), "better than no change"); // shows as +0.2 to +1.1
  assert.equal(gainVerdict([-0.0106, -0.002]), "worse than no change"); // shows as −1.1 to −0.2
  assert.equal(gainVerdict(null), null);
});

test("a range the note calls about the same is never headlined as better or worse", () => {
  const edge = cleanRow(row(1, 30, { right_level: 0.8125, right_level_no_change: 0.8075, right_level_gain_ci90: [0.001, 0.01] }));
  assert.deepEqual(rowCells(edge).slice(4, 5), ["+0.5 (+0.1 to +1.0)"]);
  assert.match(headline([edge]), /about the same as no change/);
  const notes = statusNotes([edge], "2026-10-06", new Date(2027, 5, 1));
  assert.match(notes.join(" "), /one that stays within −1 to \+1 means about the same/);
});

test("the headline never shows equal percentages next to better or worse", () => {
  // a small but clear gain: whole percentages would both read 81%
  const small = cleanRow(row(1, 30, { right_level: 0.8124, right_level_no_change: 0.8081, right_level_gain_ci90: [0.001, 0.02] }));
  assert.deepEqual(rowCells(small).slice(2, 5), ["81%", "81%", "+0.4 (+0.1 to +2.0)"]);
  assert.match(headline([small]), /1 week ahead 81\.2% against 80\.8%, better than no change/);
  // otherwise whole percentages, as in the table
  assert.match(headline([cleanRow(row(1, 26))]), /1 week ahead 72% against 68%/);
});

test("the headline only appears after 26 weeks, and says when the gain isn't clear", () => {
  assert.equal(headline([cleanRow(row(1, 25))]), null);
  assert.match(headline([cleanRow(row(1, 26))]), /1 week ahead 72% against 68%, better than no change/);
  assert.match(headline([cleanRow(row(2, 40, { right_level_gain_ci90: [-0.02, 0.05] }))]), /2 weeks ahead .*not clearly different/);
  assert.match(headline([cleanRow(row(3, 40, { right_level_gain_ci90: [-0.1, -0.02] }))]), /worse than no change/);
});

test("collecting rows say roughly when numbers will appear", () => {
  assert.equal(toISO(expectedDate("2026-10-06", 1)), "2026-12-08"); // the 8th week saved, checked 2 weeks later
  assert.equal(toISO(expectedDate("2026-10-06", 6)), "2027-01-12");
  const rows = horizonRows(parseTrackRecord(sample()), "covid");
  const notes = statusNotes(rows, "2026-10-06", new Date(2026, 9, 20));
  assert.match(notes[0], /once 8 weeks .* for 3 weeks ahead/);
  assert.match(notes[1], /percentage points, with its 90% range/);
  assert.match(notes[2], /still settling/);
  assert.match(notes[3], /^Over 26 or more weeks/);
  // once the estimate has passed, no date is promised
  const late = statusNotes(rows, "2026-10-06", new Date(2027, 5, 1));
  assert.match(late[0], /checked\.$/);
});

test("unchecked forecasts are explained, except those still waiting", () => {
  const rec = parseTrackRecord(sample());
  assert.equal(unscoredNote(rec, "covid"), "Not counted: 4 whose target week was filled in between measurements, 1 that couldn't be lined up with the later data.");
  assert.equal(unscoredNote(rec, "flu"), null);
});

test("forecasts from areas that stopped reporting are not counted, and not shown as waiting", () => {
  const data = sample();
  data.by_virus.covid.unscored["area stopped reporting"] = 17;
  const rec = parseTrackRecord(data);
  assert.match(unscoredNote(rec, "covid"), /^Not counted: 17 from areas that stopped reporting before their target week was reported, 4 /);
  assert.equal(waiting(rec, "covid"), 120);
  assert.match(introText(rec, "covid", "COVID-19"), /120 waiting/);
});

test("a country's rows show once it has 8 weeks checked", () => {
  const rec = parseTrackRecord(sample());
  assert.deepEqual(countryRows(rec, "covid", "germany").map((r) => r.h), [1]);
  assert.deepEqual(countryRows(rec, "covid", "usa"), []);
  assert.deepEqual(countryRows(rec, "covid", "__proto__"), []);
});

test("a country that has dropped out of today's data still gets its name", () => {
  const data = sample();
  data.gaps = [{ virus: "covid", country: "scotland", newest: "2026-09-01T05:17:00Z" }];
  assert.deepEqual(gapsFor(parseTrackRecord(data), "covid"), [{ country: "scotland", name: "Scotland", newest: "2026-09-01" }]);
  assert.deepEqual(COUNTRIES, ["scotland", "usa", "canada", "germany", "netherlands", "new-zealand"]);
  assert.equal(COUNTRY_NAMES["new-zealand"], "New Zealand");
});

test("a record that couldn't be fetched or scored says so, not that the site isn't recording", () => {
  const fetch = parseTrackRecord({ schema: 1, recording: true, available: false, reason: "fetch", generated_at: "2026-10-06T05:40:00Z" });
  assert.deepEqual(fetch, { state: "unavailable", reason: "fetch" });
  assert.match(introText(fetch, "covid", "COVID-19"), /^The saved forecasts couldn't be fetched for this update, so today's forecasts weren't saved/);
  const score = parseTrackRecord({ schema: 1, recording: true, available: false, reason: "score" });
  assert.match(introText(score, "covid", "COVID-19"), /^The live track record couldn't be updated this time.* next daily update\.$/);
  assert.deepEqual(parseTrackRecord({ schema: 1, available: false, reason: "<script>" }), { state: "unavailable", reason: null });
  for (const rec of [fetch, score]) {
    assert.doesNotMatch(introText(rec, "covid", "COVID-19"), /isn't recording/);
    assert.deepEqual(horizonRows(rec, "covid").map((r) => r.n), [0, 0, 0, 0, 0, 0]);
  }
  // missing or broken: it couldn't be loaded (it was never "not recording")
  assert.equal(introText(null, "covid", "COVID-19"), "The live track record couldn't be loaded.");
  // one virus the scorer couldn't summarise
  const data = sample();
  data.by_virus.covid = { issues: 30, available: false };
  const one = parseTrackRecord(data);
  assert.equal(one.viruses.covid.unavailable, true);
  assert.match(introText(one, "covid", "COVID-19"), /The COVID-19 track record couldn't be updated this time/);
});

test("the opening sentence covers not recording, nothing yet, and running", () => {
  assert.match(introText(parseTrackRecord({ schema: 1, recording: false }), "covid", "COVID-19"), /isn't recording/);
  const rec = parseTrackRecord(sample());
  assert.match(introText(rec, "flu", "flu"), /Recording started .*2026\. No flu forecasts have been saved yet\./);
  assert.match(introText(rec, "covid", "COVID-19"), /2,250 checked so far, 120 waiting/);
  assert.match(introText(parseTrackRecord({ schema: 1, by_virus: {} }), "rsv", "RSV"), /^Recording has started\./);
});

test("the opening sentence doesn't promise more than the workflow can", () => {
  // A day whose save failed leaves a gap, and nothing on the page records it, so no "every".
  const data = sample();
  data.by_virus.flu = { issues: 5, by_horizon: [row(1, 2)] };
  const rec = parseTrackRecord(data);
  const covid = introText(rec, "covid", "COVID-19");
  assert.match(covid, /^COVID-19 forecasts are saved on the day they're published and checked once the week after their target week has been reported\. If saving fails on a day, that day is missing from the record\./);
  assert.match(introText(rec, "flu", "flu"), /^Flu forecasts are saved on the day/);
  assert.doesNotMatch(covid, /[Ee]very/);
  // a record that couldn't be updated may not be back the next day either
  for (const r of [{ state: "unavailable", reason: "fetch" }, { state: "unavailable", reason: "score" }]) {
    assert.match(introText(r, "covid", "COVID-19"), /It may be back after the next daily update\.$/);
  }
  data.by_virus.covid = { issues: 30, available: false };
  assert.match(introText(parseTrackRecord(data), "covid", "COVID-19"), /It may be back after the next daily update\.$/);
});
