// The live track record: turns data/track-record.json (written by
// `python -m wastewater score`) into the rows and sentences the About panel
// shows. Pure functions with no DOM, so node can test them.
//
// The file is data, not code: only known viruses, countries, horizons and
// reasons are read, every number is checked, and anything else is dropped.

import { addDays, parseISO } from "./dates.js";
import { formatDate } from "./format.js";

export const VIRUSES = ["covid", "flu", "rsv"];
// The countries the archive takes, with their names (the same as each source's
// name), so a country that has dropped out of today's data still gets its name.
export const COUNTRY_NAMES = {
  scotland: "Scotland",
  usa: "United States",
  canada: "Canada",
  germany: "Germany",
  netherlands: "Netherlands",
  "new-zealand": "New Zealand",
};
export const COUNTRIES = Object.keys(COUNTRY_NAMES);
export const HORIZONS = [1, 2, 3, 4, 5, 6];
export const MIN_WEEKS = 8; // fewer checked weeks than this: no numbers yet
export const ESTABLISHED_WEEKS = 26; // a headline only from here on
const TIMESTAMP = /^(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}Z$/;

// Why some forecasts weren't checked, as the page says it ("not settled yet" is reported separately).
export const REASONS = {
  "issued after target known": "saved after their target week had been reported",
  "area stopped reporting": "from areas that stopped reporting before their target week was reported",
  "interpolated target": "whose target week was filled in between measurements",
  "target missing": "whose target week is missing from the later data",
  "no overlap": "that couldn't be lined up with the later data",
};
const WAITING = "not settled yet";
// Why the workflow couldn't update the track record (it writes "available": false).
const UNAVAILABLE = ["fetch", "score"];

const own = (obj, key) => (obj && typeof obj === "object" && Object.hasOwn(obj, key) ? obj[key] : undefined);
const finite = (x) => (typeof x === "number" && Number.isFinite(x) ? x : null);
const share = (x) => {
  const v = finite(x);
  return v != null && v >= 0 && v <= 1 ? v : null;
};
const count = (x) => (Number.isSafeInteger(x) && x >= 0 ? x : 0);

function interval(x, lo, hi) {
  if (!Array.isArray(x) || x.length !== 2) return null;
  const [a, b] = x.map(finite);
  return a != null && b != null && a <= b && a >= lo && b <= hi ? [a, b] : null;
}

/** "collecting", "early" or "established", from the number of data weeks checked. */
export function statusFor(weeks) {
  if (weeks < MIN_WEEKS) return "collecting";
  return weeks < ESTABLISHED_WEEKS ? "early" : "established";
}

/** One horizon's summary with only the fields the page uses, or null if it isn't one. */
export function cleanRow(r) {
  const h = own(r, "h");
  if (!HORIZONS.includes(h)) return null;
  const weeks = count(own(r, "issue_weeks"));
  return {
    h,
    n: count(own(r, "n")),
    weeks,
    status: statusFor(weeks),
    rightLevel: share(own(r, "right_level")),
    rightNoChange: share(own(r, "right_level_no_change")),
    coverage90: share(own(r, "coverage_90")),
    relativeWis: finite(own(r, "relative_wis")),
    gainCI: interval(own(r, "right_level_gain_ci90"), -1, 1),
  };
}

function cleanRows(list) {
  const byH = new Map();
  for (const r of Array.isArray(list) ? list : []) {
    const row = cleanRow(r);
    if (row && !byH.has(row.h)) byH.set(row.h, row);
  }
  return HORIZONS.filter((h) => byH.has(h)).map((h) => byH.get(h));
}

/**
 * The parts of track-record.json the page uses, checked. Null if it isn't a
 * track record (missing or invalid); {state: "off"} from a copy that isn't
 * saving its forecasts; {state: "unavailable", reason} when this update
 * couldn't fetch or score the archive; otherwise {state: "ok", ...}.
 */
export function parseTrackRecord(data) {
  if (!data || typeof data !== "object" || Array.isArray(data) || own(data, "schema") !== 1) return null;
  if (own(data, "recording") === false) return { state: "off" }; // a build that isn't saving its forecasts
  if (own(data, "available") === false) {
    const reason = own(data, "reason");
    return { state: "unavailable", reason: UNAVAILABLE.includes(reason) ? reason : null };
  }
  const since = TIMESTAMP.exec(own(data, "since") ?? "")?.[1] ?? null;
  const byVirus = own(data, "by_virus");
  const viruses = {};
  for (const v of VIRUSES) {
    const entry = own(byVirus, v);
    if (!entry || typeof entry !== "object") continue;
    if (own(entry, "available") === false) {
      // the scorer couldn't summarise this virus this time
      viruses[v] = { issues: count(own(entry, "issues")), unavailable: true, rows: [], countries: {}, unscored: {} };
      continue;
    }
    const countries = {};
    for (const c of COUNTRIES) {
      const rows = cleanRows(own(own(entry, "by_country"), c));
      if (rows.length) countries[c] = rows;
    }
    const unscored = {};
    for (const reason of [...Object.keys(REASONS), WAITING]) unscored[reason] = count(own(own(entry, "unscored"), reason));
    viruses[v] = { issues: count(own(entry, "issues")), rows: cleanRows(own(entry, "by_horizon")), countries, unscored };
  }
  const gaps = (Array.isArray(own(data, "gaps")) ? data.gaps : [])
    .filter((g) => VIRUSES.includes(own(g, "virus")) && COUNTRIES.includes(own(g, "country")) && TIMESTAMP.test(own(g, "newest") ?? ""))
    .map((g) => ({ virus: g.virus, country: g.country, newest: g.newest.slice(0, 10) }));
  return { state: "ok", since, issues: count(own(data, "issues")), viruses, gaps };
}

/** All six horizons for one virus, with empty "collecting" rows where nothing is checked yet. */
export function horizonRows(record, virus) {
  const rows = own(record?.viruses, virus)?.rows ?? [];
  return HORIZONS.map((h) => rows.find((r) => r.h === h) ?? cleanRow({ h, n: 0, issue_weeks: 0 }));
}

const pct = (x) => (x == null ? "–" : `${Math.round(100 * x)}%`);
// Percentage points to one decimal, as shown ("|| 0" turns -0 into 0).
const tenth = (x) => Math.round(10 * x) / 10 || 0;
const pctTenth = (x) => (x == null ? "–" : `${tenth(100 * x).toFixed(1)}%`);
const signed = (x) => {
  const r = tenth(x);
  return `${r > 0 ? "+" : r < 0 ? "−" : "±"}${Math.abs(r).toFixed(1)}`;
};
// A gain whose whole 90% range, as shown, stays within this many points of 0 (ends
// included) is "about the same".
export const SAME_POINTS = 1;

/** The verdict on the gain over no change, from its 90% range exactly as the table shows it. */
export function gainVerdict(ci) {
  if (!ci) return null;
  const lo = tenth(100 * ci[0]);
  const hi = tenth(100 * ci[1]);
  if (lo >= -SAME_POINTS && hi <= SAME_POINTS) return "about the same as no change";
  if (lo > 0) return "better than no change";
  if (hi < 0) return "worse than no change";
  return "not clearly different from no change";
}

/** The text for each cell of one row of the table. */
export function rowCells(row) {
  const ready = row.status !== "collecting";
  const gain = ready && row.rightLevel != null && row.rightNoChange != null ? 100 * (row.rightLevel - row.rightNoChange) : null;
  const ci = ready && row.gainCI ? ` (${signed(100 * row.gainCI[0])} to ${signed(100 * row.gainCI[1])})` : "";
  return [
    String(row.h),
    ready ? String(row.weeks) : `${row.weeks} of ${MIN_WEEKS}`,
    ready ? pct(row.rightLevel) : "–",
    ready ? pct(row.rightNoChange) : "–",
    gain == null ? "–" : `${signed(gain)}${ci}`,
    ready ? pct(row.coverage90) : "–",
  ];
}

/** Roughly when a horizon will have `weeks` weeks checked, if saving carries on every week. */
export function expectedDate(since, h, weeks = MIN_WEEKS) {
  if (typeof since !== "string") return null;
  return addDays(parseISO(since), 7 * (h + weeks));
}

/** Sentences under the table: when results come, how settled they are, and any headline. */
export function statusNotes(rows, since, today) {
  const notes = [];
  const collecting = rows.filter((r) => r.status === "collecting");
  if (collecting.length) {
    const first = collecting[0];
    const when = expectedDate(since, first.h);
    const future = when && (!today || when > today);
    notes.push(
      `Numbers appear for each row once ${MIN_WEEKS} weeks of forecasts have been checked` +
        (future ? `: about ${formatDate(when, true)} for ${first.h} week${first.h === 1 ? "" : "s"} ahead, a week later for each week further ahead.` : "."),
    );
  }
  if (rows.some((r) => r.status !== "collecting")) {
    notes.push(
      `"Difference" is the right-level rate minus assuming no change's, in percentage points, with its 90% range in brackets. A range that includes 0 means no clear difference, and one that stays within −${SAME_POINTS} to +${SAME_POINTS} means about the same.`,
    );
  }
  if (rows.some((r) => r.status === "early")) {
    notes.push(`With fewer than ${ESTABLISHED_WEEKS} weeks checked, results are still settling and can move a lot.`);
  }
  const head = headline(rows);
  if (head) notes.push(head);
  return notes;
}

/** A plain verdict for horizons checked for 26 weeks or more; null before that. */
export function headline(rows) {
  const done = rows.filter((r) => r.status === "established" && r.rightLevel != null && r.rightNoChange != null);
  if (!done.length) return null;
  const parts = done.map((r) => {
    const verdict = gainVerdict(r.gainCI);
    // "81% against 81%, better than no change" would read as a contradiction: show tenths then.
    const clear = verdict === "better than no change" || verdict === "worse than no change";
    const show = clear && pct(r.rightLevel) === pct(r.rightNoChange) ? pctTenth : pct;
    return `${r.h} week${r.h === 1 ? "" : "s"} ahead ${show(r.rightLevel)} against ${show(r.rightNoChange)}${verdict ? `, ${verdict}` : ""}`;
  });
  return `Over ${ESTABLISHED_WEEKS} or more weeks of checks, forecasts landed at the right level: ${parts.join("; ")}.`;
}

/** Forecasts that weren't checked, and why ("not settled yet" aside). Null if none. */
export function unscoredNote(record, virus) {
  const unscored = own(record?.viruses, virus)?.unscored;
  if (!unscored) return null;
  const parts = Object.entries(REASONS)
    .filter(([key]) => unscored[key] > 0)
    .map(([key, text]) => `${unscored[key].toLocaleString("en")} ${text}`);
  return parts.length ? `Not counted: ${parts.join(", ")}.` : null;
}

/** How many forecasts are still waiting for their target week to settle. */
export function waiting(record, virus) {
  return own(record?.viruses, virus)?.unscored?.[WAITING] ?? 0;
}

/** Countries that have stopped saving this virus's forecasts: [{country, name, newest}]. */
export function gapsFor(record, virus) {
  return (record?.gaps ?? [])
    .filter((g) => g.virus === virus)
    .map(({ country, newest }) => ({ country, name: COUNTRY_NAMES[country], newest }));
}

/** A country's rows, once it has enough weeks checked to show numbers. */
export function countryRows(record, virus, country) {
  const rows = own(own(record?.viruses, virus)?.countries, country) ?? [];
  return rows.filter((r) => r.status !== "collecting");
}

// A failure can repeat (if its cause is in the saved data, say), so this is no promise.
const AGAIN = "It may be back after the next daily update.";

/** The opening sentence for one virus. */
export function introText(record, virus, label) {
  if (!record) return "The live track record couldn't be loaded.";
  if (record.state === "off") return "This copy of the site isn't recording its forecasts, so it has no live track record.";
  if (record.state === "unavailable") {
    return record.reason === "fetch"
      ? `The saved forecasts couldn't be fetched for this update, so today's forecasts weren't saved and the live track record couldn't be updated. ${AGAIN}`
      : `The live track record couldn't be updated this time, so it isn't shown here. ${AGAIN}`;
  }
  const started = record.since ? `Recording started ${formatDate(record.since, true)}.` : "Recording has started.";
  const v = own(record.viruses, virus);
  if (v?.unavailable) return `${started} The ${label} track record couldn't be updated this time, so it isn't shown here. ${AGAIN}`;
  if (!v || !v.issues) return `${started} No ${label} forecasts have been saved yet.`;
  const checked = v.rows.reduce((sum, r) => sum + r.n, 0);
  // Not "every": a day whose save failed (after the deploy, say) leaves a gap the page can't see.
  const Label = label.charAt(0).toUpperCase() + label.slice(1);
  return (
    `${Label} forecasts are saved on the day they're published and checked once the week after their target week has been reported. ` +
    "If saving fails on a day, that day is missing from the record. " +
    `${started} ${checked.toLocaleString("en")} checked so far, ${waiting(record, virus).toLocaleString("en")} waiting for their week to be reported.`
  );
}
