import {
  ACTIVITIES,
  LEVEL_IDS,
  activityById,
  adviseEach,
  expectedLevel,
  lowestWeek,
  mostLikely,
  planWeeks,
} from "./advisor.js";
import { drawChart, fillTable } from "./chart.js";
import { daysBetween, parseISO, startOfToday, toISO, weekEnding } from "./dates.js";
import { formatDate, formatDateTime, formatPct, formatSignedPct, formatValue } from "./format.js";
import { noEstimateText, noForecastWhy, plannerNoForecast, showGeneralAdvice, staleWarning } from "./notes.js";
import {
  countryRows,
  gapsFor,
  horizonRows,
  introText,
  parseTrackRecord,
  rowCells,
  statusNotes,
  unscoredNote,
} from "./track.js";

const DATA_DIR = "data/";
const REPO_URL = "https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor";
const $ = (id) => document.getElementById(id);

const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(`sewer-signal:${key}`);
      return v == null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`sewer-signal:${key}`, JSON.stringify(value));
    } catch {
      /* storage unavailable: settings just won't persist */
    }
  },
};

/** A saved preference, if it's one of the values the page offers. */
function oneOf(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

const state = {
  index: null,
  labels: {},
  virusLabels: {},
  countryId: null,
  virus: store.get("virus", "covid"),
  files: new Map(), // "country-virus" -> pending fetch
  data: new Map(), // "country-virus" -> loaded data
  region: null,
  fellBack: null, // area asked for that this virus doesn't cover
  weeks: oneOf(store.get("weeks", 52), [26, 52, 104, 0], 52),
  scale: store.get("scale", "linear") === "log" ? "log" : "linear",
  activity: store.get("activity", "restaurant"),
  when: 0,
  vulnerable: false, // deliberately not saved: health-related, and a github.io origin is shared
  planViruses: store.get("planViruses", ["covid", "flu", "rsv"]),
  where: "here",
  trip: store.get("trip", null), // {country, region}
  sort: { key: "index", dir: -1 },
  group: "",
  search: "",
  showAll: false,
  today: startOfToday(),
};

const TREND_TEXT = {
  rising_fast: "and rising fast",
  rising: "and rising",
  steady: "and steady",
  falling: "and falling",
  falling_fast: "and falling fast",
};
const TREND_WORD = {
  rising_fast: "Rising fast",
  rising: "Rising",
  steady: "Steady",
  falling: "Falling",
  falling_fast: "Falling fast",
};

const GENERAL_ADVICE = {
  very_low: "A good time for things you've been putting off. Normal precautions are enough for most people.",
  low: "A good window for most plans. If you're at higher risk, a mask in packed indoor places still helps.",
  moderate: "Fine for most plans. Consider a mask in crowded indoor places, especially around vulnerable people.",
  high: "Plenty of virus about. Prefer outdoor or well-ventilated places and mask in crowds.",
  very_high: "Very high. Postpone crowded indoor plans if you can and mask in shared indoor air.",
};

/** Own property only, so data values like "constructor" can't reach Object.prototype. */
function own(obj, key) {
  return obj && typeof key === "string" && Object.hasOwn(obj, key) ? obj[key] : undefined;
}

/** Only https links from the data become clickable. */
function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

/** Virus name as it reads mid-sentence: "flu", but "COVID-19" and "RSV". */
function inText(virus) {
  const label = own(state.virusLabels, virus) ?? String(virus);
  return label === "Flu" ? "flu" : label;
}

// ---------- small DOM helpers ----------
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k === "style") Object.assign(node.style, v); // CSSOM, allowed by the CSP
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

/** replaceChildren that skips null/false, so optional pieces can be inlined. */
function setChildren(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c != null && c !== false));
}

function svgIcon(paths, size = 16) {
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("width", size);
  s.setAttribute("height", size);
  s.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", "currentColor");
    p.setAttribute("stroke-width", "2.5");
    p.setAttribute("stroke-linecap", "round");
    p.setAttribute("stroke-linejoin", "round");
    s.appendChild(p);
  }
  return s;
}

const ICONS = {
  go: ["M5 12.5l4.5 4.5L19 7.5"],
  precautions: ["M12 6v8", "M12 18.5v.01"],
  postpone: ["M9 6.5v11", "M15 6.5v11"],
  avoid: ["M7 7l10 10", "M17 7L7 17"],
  up: ["M12 19V5", "M6 11l6-6 6 6"],
  down: ["M12 5v14", "M6 13l6 6 6-6"],
  flat: ["M5 12h14", "M13 6l6 6-6 6"],
};

function chip(levelId, text) {
  const known = own(state.labels, levelId);
  return h("span", { class: "chip", dataset: { level: known ? levelId : "none" } }, text ?? known ?? "No data");
}

function trendIcon(label) {
  if (!label) return svgIcon(ICONS.flat, 14);
  if (label.startsWith("rising")) return svgIcon(ICONS.up, 14);
  if (label.startsWith("falling")) return svgIcon(ICONS.down, 14);
  return svgIcon(ICONS.flat, 14);
}

// ---------- data ----------
async function getJSON(path) {
  const res = await fetch(DATA_DIR + path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

function countryMeta(id) {
  return state.index.countries.find((c) => c.id === id);
}

function virusMeta(countryId, virus) {
  return countryMeta(countryId)?.viruses?.[virus] ?? null;
}

function virusesOf(countryId) {
  const meta = countryMeta(countryId);
  return state.index.viruses.map((v) => v.id).filter((v) => meta?.viruses?.[v]);
}

async function loadData(countryId, virus) {
  const key = `${countryId}-${virus}`;
  if (state.data.has(key)) return state.data.get(key);
  if (!state.files.has(key)) {
    const meta = virusMeta(countryId, virus);
    if (!meta) return null;
    state.files.set(key, getJSON(meta.file));
  }
  try {
    const data = await state.files.get(key);
    state.data.set(key, data);
    return data;
  } finally {
    state.files.delete(key);
  }
}

function currentMeta() {
  return virusMeta(state.countryId, state.virus);
}

function currentData() {
  return state.data.get(`${state.countryId}-${state.virus}`);
}

/** The area with this id, or the virus's national series when it doesn't cover that area. */
function findRegion(data, meta, id) {
  return (
    data.regions.find((r) => r.id === id) ??
    data.regions.find((r) => r.id === meta.default_region) ??
    data.regions[0]
  );
}

function unitOf(region, meta) {
  return region?.unit ?? meta?.signal?.unit ?? "";
}

function readHash() {
  let raw = "";
  try {
    raw = decodeURIComponent(location.hash.slice(1));
  } catch {
    raw = "";
  }
  const [country, region, virus] = raw.split("~");
  return { country, region, virus };
}

function writeHash() {
  const parts = [state.countryId, state.region.id];
  if (state.virus !== "covid") parts.push(state.virus);
  const hash = `#${parts.join("~")}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

// ---------- selection ----------
let showToken = 0;
async function show(countryId, regionId, virus) {
  const token = ++showToken;
  const meta = countryMeta(countryId) ?? countryMeta(state.index.default_country);
  const available = virusesOf(meta.id);
  const wanted = virus ?? state.virus;
  const chosen = available.includes(wanted) ? wanted : available[0];
  $("main").setAttribute("aria-busy", "true");
  let data;
  try {
    data = await loadData(meta.id, chosen);
  } catch (err) {
    if (token !== showToken) return; // a newer selection has taken over; its result is what counts
    throw err;
  }
  if (token !== showToken) return; // a newer selection started while this one loaded

  const countryChanged = meta.id !== state.countryId;
  state.countryId = meta.id;
  state.virus = chosen;
  store.set("country", meta.id);
  store.set("virus", chosen);
  if (countryChanged) {
    state.group = "";
    state.search = "";
    state.showAll = false;
    $("area-search").value = "";
  }
  const vMeta = currentMeta();
  const wantedRegion = regionId ?? (countryChanged ? null : state.region?.id) ?? store.get(`region:${meta.id}`, null);
  state.region = findRegion(data, vMeta, wantedRegion);
  state.fellBack = wantedRegion && state.region.id !== wantedRegion ? { id: wantedRegion, wantedVirus: wanted } : null;
  if (!state.fellBack) store.set(`region:${meta.id}`, state.region.id);
  // Only a real virus id gets a note; anything else (say from a hand-edited link) is ignored.
  if (wanted !== chosen && own(state.virusLabels, wanted)) state.fellBack = { ...(state.fellBack ?? {}), virusMissing: wanted };

  renderVirusButtons();
  renderCountryPicker();
  renderAreaPicker();
  $("area-select").value = state.region.id;
  writeHash();
  renderRegion();
  renderAreas();
  renderAbout();
  $("main").setAttribute("aria-busy", "false");
}

function selectRegion(id) {
  return safeShow(state.countryId, id, state.virus);
}

/** show(), with load failures reported on the page instead of thrown. */
function safeShow(...args) {
  return show(...args).catch((err) => {
    $("main").setAttribute("aria-busy", "false");
    const message = `Couldn't load that data (${err.message}). Check your connection and try again.`;
    if ($("verdict").hidden) {
      $("load-status").textContent = message; // nothing shown yet, so say it where the user is looking
      return;
    }
    const note = $("virus-note");
    note.hidden = false;
    note.textContent = message;
  });
}

// ---------- pickers ----------
function renderVirusButtons() {
  const available = virusesOf(state.countryId);
  const meta = countryMeta(state.countryId);
  setChildren(
    $("virus-buttons"),
    ...state.index.viruses.map((v) =>
      h(
        "button",
        {
          type: "button",
          "aria-pressed": String(v.id === state.virus),
          disabled: !available.includes(v.id),
          title: available.includes(v.id) ? null : `${meta.name} doesn't publish ${inText(v.id)} data`,
          onclick: () => safeShow(state.countryId, state.fellBack?.id ?? state.region.id, v.id),
        },
        v.label,
      ),
    ),
  );
}

function renderCountryPicker() {
  const sel = $("country-select");
  setChildren(sel, ...state.index.countries.map((c) => h("option", { value: c.id }, `${c.flag} ${c.name}`)));
  sel.value = state.countryId;
}

function regionOptionText(r) {
  return r.parent && r.level !== "region" ? `${r.name} (${r.parent})` : r.name;
}

function fillAreaSelect(sel, regions) {
  const groups = new Map();
  for (const r of regions) {
    if (!groups.has(r.group)) groups.set(r.group, []);
    groups.get(r.group).push(r);
  }
  setChildren(
    sel,
    ...[...groups].map(([group, rs]) =>
      h("optgroup", { label: group }, rs.map((r) => h("option", { value: r.id }, regionOptionText(r)))),
    ),
  );
}

function renderAreaPicker() {
  fillAreaSelect($("area-select"), currentData().regions);
}

// ---------- region views ----------
function renderRegion() {
  const r = state.region;
  const weeks = planWeeks(r, state.today, 4);
  if (!weeks[state.when]?.available) state.when = Math.max(0, weeks.findIndex((w) => w.available));
  renderVerdict(r, weeks);
  renderWeeks(r);
  renderChart();
  renderPlanner();
  $("verdict").hidden = false;
  $("desk").hidden = false;
  $("areas-card").hidden = false;
  $("about").hidden = false;
  $("load-status").hidden = true;
  document.title = `${r.name} · ${own(state.virusLabels, state.virus)} · Sewer Signal`;
}

function renderVerdict(r, weeks) {
  const meta = currentMeta();
  const latest = r.latest;
  const where = r.level === "national" ? "Whole country" : [r.group, r.level !== "region" ? r.parent : null].filter(Boolean).join(" · ");
  $("verdict-eyebrow").textContent = `${own(state.virusLabels, state.virus)} · ${r.name} · ${where} · latest ${meta.signal.kind === "lab tests" ? "lab data" : "sample"} ${formatDate(latest.source_date, true)}`;

  const notes = [];
  if (state.fellBack?.virusMissing) {
    notes.push(`${countryMeta(state.countryId).name} doesn't publish ${inText(state.fellBack.virusMissing)} data, so this shows ${inText(state.virus)}.`);
  } else if (state.fellBack?.id) {
    notes.push(`There's no ${inText(state.virus)} data for that area, so this shows ${r.name}.`);
  }
  if (meta.signal.kind === "lab tests") {
    notes.push(`Based on laboratory tests, not wastewater: ${countryMeta(state.countryId).name} doesn't publish ${inText(state.virus)} wastewater data.`);
  }
  const note = $("virus-note");
  note.hidden = notes.length === 0;
  note.textContent = notes.join(" ");

  const heading = $("verdict-heading");
  setChildren(heading, chip(latest.category), h("span", {}, own(TREND_TEXT, latest.trend?.label) ?? ""));
  heading.setAttribute("aria-label", `${own(state.virusLabels, state.virus)}: ${own(state.labels, latest.category)} ${own(TREND_TEXT, latest.trend?.label) ?? ""}`.trim());

  const now = weeks[0];
  const nowLine = $("verdict-now");
  let nowLevel = latest.category;
  if (now.available && now.source === "forecast") {
    const ml = mostLikely(now.probs);
    nowLevel = LEVEL_IDS[Math.min(4, Math.round(expectedLevel(now.probs) - 0.5))];
    setChildren(
      nowLine,
      `Estimate for this week (to ${formatDate(now.date)}): most likely `,
      h("strong", {}, own(state.labels, ml.id).toLowerCase()),
      `, ${formatPct(ml.p)} chance. Reports run ${lagText(latest.date, now.date)} behind.`,
    );
  } else if (now.available) {
    nowLine.textContent = `This week's reading is in: ${own(state.labels, latest.category).toLowerCase()}.`;
  } else {
    nowLine.textContent = noEstimateText(r);
  }
  const age = daysBetween(parseISO(latest.date), state.today);
  const advice = $("verdict-advice");
  advice.textContent = showGeneralAdvice(now.available, r, age) ? own(GENERAL_ADVICE, nowLevel) ?? "" : "";
  advice.hidden = !advice.textContent;

  const unit = unitOf(r, meta);
  const pctOfPeak = r.window.max > 0 ? latest.value / r.window.max : null;
  const stats = [
    ["Level", `${latest.index}/100`, `Higher than ${latest.index}% of weeks in the past two years`],
    [
      "Trend",
      h("span", { class: "trend" }, trendIcon(latest.trend?.label), latest.trend ? `${formatSignedPct(latest.trend.pct_per_week)}/wk` : "–"),
      latest.trend ? `${own(TREND_WORD, latest.trend.label)} over the last two weeks` : "Not enough recent data",
    ],
    ["Latest level", h("span", {}, formatValue(latest.value), " ", h("span", { class: "unit" }, unit)), `Smoothed, week ending ${formatDate(latest.date)}`],
    ["Versus 2-year peak", pctOfPeak == null ? "–" : formatPct(pctOfPeak), `Peak ${formatValue(r.window.max)} ${unit}`],
  ];
  setChildren(
    $("stats"),
    ...stats.map(([label, value, noteText]) => h("div", { class: "stat" }, h("dt", {}, label), h("dd", {}, value, h("span", { class: "stat-note" }, noteText)))),
  );

  const stale = $("stale-warning");
  const warning = staleWarning(r, latest.date, age);
  stale.hidden = warning == null;
  stale.textContent = warning ?? "";
}

function lagText(fromISO, toISO_) {
  const lag = Math.max(1, Math.round(daysBetween(parseISO(fromISO), parseISO(toISO_)) / 7));
  return `${lag} week${lag === 1 ? "" : "s"}`;
}

function weekName(dateISO) {
  const thisWeek = toISO(weekEnding(state.today));
  const diff = Math.round(daysBetween(parseISO(thisWeek), parseISO(dateISO)) / 7);
  if (diff === -1) return "Last week";
  if (diff < 0) return `${-diff} weeks ago`;
  if (diff === 0) return "This week";
  if (diff === 1) return "Next week";
  return `In ${diff} weeks`;
}

function probBar(probs) {
  return h(
    "div",
    { class: "prob-bar", role: "img", "aria-label": LEVEL_IDS.map((id, i) => `${own(state.labels, id)} ${formatPct(probs[i])}`).join(", ") },
    LEVEL_IDS.map((id, i) => (probs[i] >= 0.005 ? h("span", { class: `lvl-fill-${id}`, style: { flex: String(Number(probs[i]) || 0) }, title: `${own(state.labels, id)}: ${formatPct(probs[i])}` }) : null)),
  );
}

function renderWeeks(r) {
  const list = $("weeks");
  if (!r.forecast.length) {
    setChildren(list, h("li", { class: "week" }, `No forecast for this area: ${noForecastWhy(r)}.`));
    return;
  }
  const thisWeek = toISO(weekEnding(state.today));
  const upcoming = r.forecast.map((f) => ({ ...f, available: true }));
  const futureIdx = upcoming.map((f, i) => (f.date >= thisWeek ? i : -1)).filter((i) => i >= 0);
  const best = lowestWeek(futureIdx.map((i) => upcoming[i]));
  const bestIdx = best >= 0 ? futureIdx[best] : -1;
  setChildren(
    list,
    ...upcoming.map((f, i) => {
      const ml = mostLikely(f.probs);
      const lowOrBelow = f.probs[0] + f.probs[1];
      const past = f.date < thisWeek;
      return h(
        "li",
        { class: "week", dataset: { past: String(past), best: String(i === bestIdx) } },
        h("div", { class: "week-top" }, h("span", { class: "week-name" }, weekName(f.date)), h("span", { class: "week-date" }, `w/e ${formatDate(f.date)}`)),
        chip(ml.id),
        probBar(f.probs),
        h("p", { class: "week-odds" }, h("strong", {}, formatPct(lowOrBelow)), " chance of low or very low"),
        past ? h("span", { class: "week-tag week-tag-muted" }, "Not reported yet") : null,
        i === bestIdx ? h("span", { class: "week-tag" }, "Lowest ahead") : null,
      );
    }),
  );
  setChildren(
    $("level-key"),
    ...LEVEL_IDS.map((id, k) => h("li", {}, h("span", { class: "swatch", style: { background: `var(--lvl-${k + 1})` } }), own(state.labels, id))),
  );
}

let chartFrame = 0;
function renderChart() {
  const r = state.region;
  const meta = currentMeta();
  const unit = unitOf(r, meta);
  $("chart-heading").textContent = `${own(state.virusLabels, state.virus)} ${meta.signal.kind === "lab tests" ? "in lab tests" : "in wastewater"}`;
  $("chart-sub").textContent = `${meta.signal.metric} (${unit})`;
  setChildren(
    $("chart-legend"),
    h("li", {}, lineKey(false), "Trend (smoothed)"),
    h("li", {}, dotKey(), "Weekly measurement"),
    r.forecast.length ? h("li", {}, lineKey(true), "Forecast") : null,
    r.forecast.length ? h("li", {}, bandKey(), "Likely and wider forecast range") : null,
  );
  for (const b of $("range-buttons").querySelectorAll("button")) b.setAttribute("aria-pressed", String(Number(b.dataset.weeks) === state.weeks));
  for (const b of $("scale-buttons").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.scale === state.scale));
  drawChart($("chart"), r, {
    weeks: state.weeks,
    scale: state.scale,
    unit,
    today: state.today,
    labels: state.labels,
    tooltip: $("tooltip"),
  });
  fillTable($("data-table"), r, { unit, labels: state.labels });
}

function lineKey(dashed) {
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("width", "18");
  s.setAttribute("height", "10");
  s.setAttribute("aria-hidden", "true");
  const l = document.createElementNS(ns, "line");
  Object.entries({ x1: 1, x2: 17, y1: 5, y2: 5, stroke: "var(--series)", "stroke-width": 2, "stroke-linecap": "round" }).forEach(([k, v]) => l.setAttribute(k, v));
  if (dashed) l.setAttribute("stroke-dasharray", "4 3");
  s.appendChild(l);
  return s;
}

function dotKey() {
  return h("span", { class: "tt-key-dot", "aria-hidden": "true" });
}

function bandKey() {
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("width", "18");
  s.setAttribute("height", "12");
  s.setAttribute("aria-hidden", "true");
  const outer = document.createElementNS(ns, "rect");
  Object.entries({ x: 0, y: 0, width: 18, height: 12, rx: 2, fill: "var(--series-wash)" }).forEach(([k, v]) => outer.setAttribute(k, v));
  const inner = document.createElementNS(ns, "rect");
  Object.entries({ x: 0, y: 3, width: 18, height: 6, fill: "var(--series-wash-2)" }).forEach(([k, v]) => inner.setAttribute(k, v));
  s.append(outer, inner);
  return s;
}

// ---------- planner ----------
function renderActivities() {
  setChildren(
    $("activity-list"),
    ...ACTIVITIES.map((a) =>
      h(
        "div",
        { class: "activity" },
        h("input", {
          type: "radio",
          name: "activity",
          id: `act-${a.id}`,
          value: a.id,
          checked: a.id === state.activity,
          onchange: () => {
            state.activity = a.id;
            store.set("activity", a.id);
            renderPlanner();
          },
        }),
        h("label", { for: `act-${a.id}` }, a.label, h("small", {}, a.detail)),
      ),
    ),
  );
  $("vulnerable").checked = state.vulnerable;
}

/** The place the planner is answering for: here, or a trip destination. */
function plannerPlace() {
  if (state.where === "trip" && state.trip && countryMeta(state.trip.country)) {
    return { country: state.trip.country, region: state.trip.region };
  }
  return { country: state.countryId, region: state.fellBack?.id ?? state.region.id };
}

let tripToken = 0;
/** Fill the "somewhere else" pickers. Never throws: a failed load shows up in the planner. */
async function renderTripPickers() {
  const token = ++tripToken;
  const box = $("trip-pickers");
  box.hidden = state.where !== "trip";
  for (const b of $("where-buttons").querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.where === state.where));
  if (state.where !== "trip") return;
  if (!state.trip || !countryMeta(state.trip.country)) state.trip = { country: state.countryId, region: state.region.id };
  const cSel = $("trip-country");
  setChildren(cSel, ...state.index.countries.map((c) => h("option", { value: c.id }, `${c.flag} ${c.name}`)));
  cSel.value = state.trip.country;
  const virus = virusesOf(state.trip.country)[0];
  let data;
  try {
    data = await loadData(state.trip.country, virus);
  } catch {
    if (token === tripToken) setChildren($("trip-area"));
    return; // renderPlanner reports the failure for this place
  }
  if (token !== tripToken) return; // the choice changed while this loaded
  fillAreaSelect($("trip-area"), data.regions);
  if (!data.regions.some((r) => r.id === state.trip.region)) state.trip.region = virusMeta(state.trip.country, virus).default_region;
  $("trip-area").value = state.trip.region;
  store.set("trip", state.trip);
}

let plannerToken = 0;
async function renderPlanner() {
  const token = ++plannerToken;
  const place = plannerPlace();
  const available = virusesOf(place.country);

  setChildren(
    $("virus-checks"),
    ...state.index.viruses.map((v) =>
      h(
        "label",
        { class: "virus-check", "data-disabled": String(!available.includes(v.id)) },
        h("input", {
          type: "checkbox",
          id: `check-${v.id}`,
          value: v.id,
          checked: available.includes(v.id) && state.planViruses.includes(v.id),
          disabled: !available.includes(v.id),
          onchange: (e) => {
            const set = new Set(state.planViruses);
            if (e.target.checked) set.add(v.id);
            else set.delete(v.id);
            state.planViruses = state.index.viruses.map((x) => x.id).filter((x) => set.has(x));
            store.set("planViruses", state.planViruses);
            renderPlanner();
          },
        }),
        h("span", {}, v.label),
      ),
    ),
  );

  const viruses = available.filter((v) => state.planViruses.includes(v));
  const byVirus = {};
  const used = {};
  try {
    for (const v of viruses) {
      const data = await loadData(place.country, v);
      const meta = virusMeta(place.country, v);
      const region = findRegion(data, meta, place.region);
      byVirus[v] = planWeeks(region, state.today, 4);
      used[v] = { region, meta, exact: region.id === place.region };
    }
  } catch (err) {
    if (token === plannerToken) setChildren($("advice"), h("p", { class: "advice-why" }, `Couldn't load the data for that place (${err.message}).`));
    return;
  }
  if (token !== plannerToken) return; // a newer render started meanwhile

  const placeName = (() => {
    const first = Object.values(used)[0];
    const exact = Object.values(used).find((u) => u.exact);
    return (exact ?? first)?.region.name ?? "";
  })();
  $("where-note").textContent =
    state.where === "trip" ? `Checking ${placeName}, ${countryMeta(place.country).name}.` : `Checking ${placeName}.`;

  // A week can be picked if any ticked virus has a forecast for it.
  const baseWeeks = byVirus[viruses[0]] ?? planWeeks(state.region, state.today, 4);
  const weeksForButtons = baseWeeks.map((w, i) => ({ ...w, available: viruses.some((v) => byVirus[v][i]?.available) }));
  if (!weeksForButtons[state.when]?.available) state.when = Math.max(0, weeksForButtons.findIndex((w) => w.available));
  setChildren(
    $("when-buttons"),
    ...weeksForButtons.map((w, i) =>
      h(
        "button",
        {
          type: "button",
          "aria-pressed": String(i === state.when),
          disabled: !w.available,
          onclick: () => {
            state.when = i;
            renderPlanner();
          },
        },
        w.label,
        h("small", {}, `to ${formatDate(w.date)}`),
      ),
    ),
  );

  const out = $("advice");
  if (!viruses.length) {
    setChildren(out, h("p", { class: "advice-why" }, "Tick at least one virus to check."));
    return;
  }
  const activity = activityById(state.activity);
  const advice = adviseEach({ byVirus, weekIndex: state.when, activity, vulnerable: state.vulnerable });
  const rows = viruses.map((v) => {
    const r = advice.results[v];
    const u = used[v];
    const scope = u.exact ? "" : ` (${u.region.name}-wide: no local data)`;
    const lab = u.meta.signal.kind === "lab tests" ? " Based on lab tests." : "";
    if (!r) {
      return h("li", { class: "verdict-row" }, h("span", { class: "status-icon status-none" }), h("div", {}, h("strong", {}, own(state.virusLabels, v)), h("span", { class: "verdict-detail" }, plannerNoForecast(u.region, scope))));
    }
    const detail =
      r.week.source === "measured"
        ? `Latest reading ${own(state.labels, r.level.id).toLowerCase()}${scope}.${lab}`
        : `Most likely ${own(state.labels, r.level.id).toLowerCase()} (${formatPct(r.level.p)})${scope}.${lab}`;
    return h(
      "li",
      { class: "verdict-row" },
      h("span", { class: "status-icon", dataset: { status: r.verdict.id } }, svgIcon(ICONS[r.verdict.id], 16)),
      h("div", {}, h("strong", {}, `${own(state.virusLabels, v)}: ${r.verdict.label}`), h("span", { class: "verdict-detail" }, detail)),
    );
  });
  const week = weeksForButtons[state.when];
  setChildren(
    out,
    h("p", { class: "advice-why" }, `${activity.label}, ${week.label.toLowerCase()}`, state.vulnerable || activity.protectsOthers ? ", with extra caution for people at higher risk." : "."),
    h("ul", { class: "verdicts" }, rows),
    advice.strictest && advice.tips.length
      ? [
          h(
            "p",
            { class: "tips-for" },
            viruses.length > 1
              ? `Tips for ${own(state.virusLabels, advice.strictest.virus)}, the strictest of ${viruses.every((v) => advice.results[v]) ? "these" : "those with a forecast"}:`
              : "Tips:",
          ),
          h("ul", { class: "tips" }, advice.tips.map((t) => h("li", {}, t))),
        ]
      : null,
    advice.better
      ? h(
          "p",
          { class: "better-week" },
          h("strong", {}, `${advice.better.week.label} (to ${formatDate(advice.better.week.date)})`),
          viruses.length > 1
            ? ` looks better for all of them: at worst ${advice.better.verdict.label.toLowerCase()}.`
            : ` looks better: ${advice.better.verdict.label.toLowerCase()}.`,
        )
      : null,
  );
}

// ---------- areas table ----------
function areaRows() {
  const data = currentData();
  const q = state.search.trim().toLowerCase();
  return data.regions
    .filter((r) => (!state.group || r.group === state.group) && (!q || `${r.name} ${r.parent ?? ""}`.toLowerCase().includes(q)))
    .map((r) => {
      const next = planWeeks(r, state.today, 2)[1];
      return { r, next: next?.available ? next : null };
    });
}

function sortValue(row, key) {
  if (key === "name") return row.r.name.toLowerCase();
  if (key === "index") return row.r.latest.index ?? -1;
  if (key === "trend") return row.r.latest.trend?.pct_per_week ?? -Infinity;
  if (key === "outlook") return row.next ? expectedLevel(row.next.probs) : -1;
  return 0;
}

function renderAreas() {
  const data = currentData();
  const areas = data.regions.filter((r) => r.level !== "national");
  const counts = LEVEL_IDS.map((id) => areas.filter((r) => r.latest.category === id).length);
  const total = counts.reduce((a, b) => a + b, 0);
  $("areas-heading").textContent = `All areas: ${own(state.virusLabels, state.virus)}`;
  $("areas-sub").textContent = total
    ? `${total} areas reporting. ${LEVEL_IDS.map((id, i) => `${counts[i]} ${own(state.labels, id).toLowerCase()}`).join(", ")}.`
    : "Only a national series is published.";
  setChildren(
    $("distribution"),
    ...LEVEL_IDS.map((id, i) => (counts[i] ? h("span", { class: `lvl-fill-${id}`, style: { flex: String(counts[i]) }, title: `${own(state.labels, id)}: ${counts[i]}` }) : null)),
  );
  $("distribution").hidden = total === 0;

  const groups = [...new Set(data.regions.map((r) => r.group))];
  if (state.group && !groups.includes(state.group)) state.group = "";
  const gf = $("group-filter");
  setChildren(gf, h("option", { value: "" }, "All areas"), ...groups.map((g) => h("option", { value: g }, g)));
  gf.value = state.group;
  fillAreaRows();
}

function fillAreaRows() {
  const rows = areaRows();
  const { key, dir } = state.sort;
  rows.sort((a, b) => {
    const va = sortValue(a, key);
    const vb = sortValue(b, key);
    return (va < vb ? -1 : va > vb ? 1 : 0) * dir || a.r.name.localeCompare(b.r.name);
  });
  const limit = state.showAll || rows.length <= 40 ? rows.length : 30;
  const tbody = $("areas-table").querySelector("tbody");
  const trs = rows.slice(0, limit).map(({ r, next }) => {
    const nextLevel = next ? mostLikely(next.probs).id : null;
    return h(
      "tr",
      {
        tabindex: "0",
        dataset: { id: r.id },
        onclick: () => selectRegionFromTable(r.id),
        onkeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            selectRegionFromTable(r.id);
          }
        },
      },
      h("td", {}, h("span", { class: "area-name" }, r.name), h("span", { class: "area-group" }, r.parent && r.level !== "region" ? `${r.group} · ${r.parent}` : r.group)),
      h("td", {}, chip(r.latest.category), h("span", { class: "idx" }, `${r.latest.index}/100`)),
      h("td", { class: "num" }, h("span", { class: "trend" }, trendIcon(r.latest.trend?.label), r.latest.trend ? formatSignedPct(r.latest.trend.pct_per_week) : "–")),
      h("td", {}, nextLevel ? chip(nextLevel) : h("span", { class: "idx" }, "–")),
    );
  });
  if (rows.length > limit) {
    const showAll = h("button", { type: "button", onclick: () => { state.showAll = true; fillAreaRows(); } }, "Show all");
    trs.push(h("tr", {}, h("td", { colspan: "4", class: "more-rows" }, `Showing ${limit} of ${rows.length}.`, showAll)));
  }
  if (!rows.length) trs.push(h("tr", {}, h("td", { colspan: "4", class: "more-rows" }, "No areas match that search.")));
  setChildren(tbody, ...trs);
  for (const b of $("areas-table").querySelectorAll("th button")) {
    if (b.dataset.sort === key) b.setAttribute("aria-sort", dir < 0 ? "descending" : "ascending");
    else b.removeAttribute("aria-sort");
  }
  markCurrentRow();
}

function selectRegionFromTable(id) {
  selectRegion(id).then(() =>
    $("verdict").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" }),
  );
}

function markCurrentRow() {
  for (const tr of $("areas-table").querySelectorAll("tbody tr[data-id]")) {
    if (tr.dataset.id === state.region?.id) tr.setAttribute("aria-current", "true");
    else tr.removeAttribute("aria-current");
  }
}

// ---------- about ----------
function renderAbout() {
  const idx = state.index;
  const ranges = ["below the 20th percentile", "20th to 40th percentile", "40th to 60th percentile", "60th to 80th percentile", "above the 80th percentile"];
  setChildren($("level-table"), ...LEVEL_IDS.map((id, i) => h("li", {}, chip(id), `${ranges[i]} of the past two years`)));

  const label = own(state.virusLabels, state.virus);
  const model = idx.models[state.virus];
  $("metrics-heading").textContent = `${label} model: re-run on the past year`;
  const t = $("metrics-table");
  const pct = (x) => (Number.isFinite(x) ? `${Math.round(100 * x)}%` : "–");
  const pair = (a, b) => (Number.isFinite(a) ? `${pct(a)} (${pct(b)})` : "–");
  const head = h(
    "thead",
    {},
    h("tr", {}, ["Weeks after the data", "Right level", "Leaving out quiet weeks", "Vs no change"].map((c, i) => h("th", { scope: "col", class: i ? "num" : null }, c))),
  );
  const body = h(
    "tbody",
    {},
    model.by_horizon.map((m) =>
      h(
        "tr",
        {},
        h("td", {}, String(m.horizon_weeks)),
        h("td", { class: "num" }, pair(m.category_accuracy, m.category_accuracy_no_change)),
        h("td", { class: "num" }, pair(m.category_accuracy_not_very_low, m.category_accuracy_not_very_low_no_change)),
        h("td", { class: "num" }, Number.isFinite(m.relative_wis) ? formatSignedPct(100 * (1 - m.relative_wis)) : formatSignedPct(100 * m.skill_vs_no_change_final)),
      ),
    ),
  );
  setChildren(t, head, body);

  const country = countryMeta(state.countryId);
  const countryRows = model.by_country?.[state.countryId] ?? [];
  const fallback = model.fallback?.[state.countryId] ?? [];
  const skills = countryRows.filter((r) => r.uses_model).map((r) => r.skill_vs_no_change);
  let countryNote = "";
  if (countryRows.length) {
    if (!skills.length) {
      countryNote = `For ${country.name} the ${inText(state.virus)} model didn't beat no change in testing, so its forecasts assume levels stay where they are, with the model's uncertainty range.`;
    } else if (fallback.length) {
      countryNote = `For ${country.name} it uses no change ${fallback.length === 1 ? `${fallback[0]} weeks after the data` : `${fallback[0]}–${fallback[fallback.length - 1]} weeks after the data`}, where the model didn't beat it.`;
    }
  }
  const quiet = model.by_horizon.find((m) => Number.isFinite(m.share_starting_very_low))?.share_starting_very_low;
  const last = model.by_horizon[model.by_horizon.length - 1];
  const leansHigh = Number.isFinite(last?.bias_log) && last.bias_log > Math.log(1.15);
  $("metrics-note").textContent = [
    `Forecasts re-made for ${model.n_series} ${inText(state.virus)} series over the year from ${formatDate(model.holdout_start, true)}, using today's data.`,
    `The model never trained on those weeks, but its ranges, the switch to no change and which inputs it uses were tuned on them, so read this as a best case, not as a record of what the site said at the time. That record is the live track record below.`,
    `Brackets show the result of simply assuming no change.`,
    Number.isFinite(quiet) ? `"Quiet weeks" started at very low, where any method does well; they were ${pct(quiet)} of the forecasts.` : "",
    `"Vs no change" compares the whole forecast range with assuming no change but with the same uncertainty.`,
    `The planner's "This week" is usually 2–3 weeks after the latest data.`,
    `Forecasts are least reliable around peaks and at the start of new waves.`,
    leansHigh ? `Further ahead, this forecast has tended to run high: after a peak, levels have usually fallen faster than it shows.` : "",
    countryNote,
  ]
    .filter(Boolean)
    .join(" ");

  renderTrackRecord();

  const signal = currentMeta().signal;
  setChildren(
    $("source"),
    safeUrl(country.source.url)
      ? h("a", { href: safeUrl(country.source.url), target: "_blank", rel: "noopener noreferrer" }, country.source.publisher)
      : country.source.publisher,
    `. ${signal.metric}. ${signal.notes} Licence: ${country.source.license}.`,
  );
  setChildren(
    $("footer-text"),
    `Data rebuilt ${formatDateTime(idx.generated_at)}. Public data from health agencies in six countries. `,
    h("a", { href: REPO_URL, target: "_blank", rel: "noopener noreferrer" }, "Source code"),
    ". Not medical advice.",
  );
}

// ---------- live track record ----------
const LIVE_COLUMNS = ["Weeks after the data", "Weeks checked", "Right level", "No change", "Difference", "In 90% range"];
let trackRecord = null; // pending fetch of data/track-record.json, parsed (null if missing or invalid)

function loadTrackRecord() {
  trackRecord ??= getJSON("track-record.json").then(parseTrackRecord, () => null);
  return trackRecord;
}

/** Forecasts saved before their outcome was known, checked once the data came in. */
function renderTrackRecord() {
  const virus = state.virus;
  const countryId = state.countryId;
  loadTrackRecord()
    .then((record) => {
      if (virus === state.virus && countryId === state.countryId) drawTrackRecord(record);
    })
    .catch(() => drawTrackRecord(null));
}

function liveTable(table, rows) {
  setChildren(
    table,
    h("thead", {}, h("tr", {}, LIVE_COLUMNS.map((c, i) => h("th", { scope: "col", class: i ? "num" : null }, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, rowCells(r).map((cell, i) => h("td", { class: i ? "num" : null }, cell))))),
  );
}

function archiveLink() {
  return h(
    "p",
    {},
    "Every saved forecast is public on the ",
    h("a", { href: `${REPO_URL}/tree/forecast-archive`, target: "_blank", rel: "noopener noreferrer" }, "forecast-archive branch"),
    ".",
  );
}

function drawTrackRecord(record) {
  const label = own(state.virusLabels, state.virus) ?? "";
  $("live-heading").textContent = `${label}: live track record`;
  $("live-intro").textContent = introText(record, state.virus, inText(state.virus));
  const table = $("live-table");
  const v = record?.state === "ok" ? own(record.viruses, state.virus) : undefined;
  if (!v || !v.issues || v.unavailable) {
    table.hidden = true;
    // Recording, but not shown this time: the saved forecasts are still there to see.
    setChildren($("live-notes"), record?.state === "unavailable" || v?.unavailable ? archiveLink() : null);
    $("live-country").hidden = true;
    return;
  }
  const rows = horizonRows(record, state.virus);
  liveTable(table, rows);
  table.hidden = false;

  const notes = [
    ...statusNotes(rows, record.since, state.today),
    "As in the back-test, weeks are counted from the newest week of data, which is usually one to two weeks old when a forecast is published.",
  ];
  const unscored = unscoredNote(record, state.virus);
  if (unscored) notes.push(unscored);
  const gaps = gapsFor(record, state.virus);
  if (gaps.length) {
    const where = gaps.map((g) => `${countryMeta(g.country)?.name ?? g.name} (since ${formatDate(g.newest, true)})`);
    notes.push(`No new ${inText(state.virus)} forecasts have been saved lately for ${where.join(", ")}.`);
  }
  setChildren(
    $("live-notes"),
    notes.map((n) => h("p", {}, n)),
    archiveLink(),
  );

  const here = countryRows(record, state.virus, state.countryId);
  const box = $("live-country");
  if (here.length) {
    $("live-country-heading").textContent = `In ${countryMeta(state.countryId)?.name ?? ""}`;
    liveTable($("live-country-table"), here);
  }
  box.hidden = !here.length;
}

// ---------- events ----------
function bindEvents() {
  $("country-select").addEventListener("change", (e) => safeShow(e.target.value, store.get(`region:${e.target.value}`, null), state.virus));
  $("area-select").addEventListener("change", (e) => selectRegion(e.target.value));
  $("range-buttons").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.weeks = Number(b.dataset.weeks);
    store.set("weeks", state.weeks);
    renderChart();
  });
  $("scale-buttons").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.scale = b.dataset.scale === "log" ? "log" : "linear";
    store.set("scale", state.scale);
    renderChart();
  });
  $("vulnerable").addEventListener("change", (e) => {
    state.vulnerable = e.target.checked;
    renderPlanner();
  });
  $("where-buttons").addEventListener("click", async (e) => {
    const b = e.target.closest("button[data-where]");
    if (!b) return;
    state.where = b.dataset.where === "trip" ? "trip" : "here";
    await renderTripPickers();
    renderPlanner();
  });
  $("trip-country").addEventListener("change", async (e) => {
    state.trip = { country: e.target.value, region: null };
    await renderTripPickers();
    renderPlanner();
  });
  $("trip-area").addEventListener("change", (e) => {
    state.trip = { ...state.trip, region: e.target.value };
    store.set("trip", state.trip);
    renderPlanner();
  });
  $("group-filter").addEventListener("change", (e) => {
    state.group = e.target.value;
    state.showAll = false;
    fillAreaRows();
  });
  $("area-search").addEventListener("input", (e) => {
    state.search = e.target.value;
    state.showAll = false;
    fillAreaRows();
  });
  $("areas-table").querySelector("thead").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-sort]");
    if (!b) return;
    const key = b.dataset.sort;
    state.sort = state.sort.key === key ? { key, dir: -state.sort.dir } : { key, dir: key === "name" ? 1 : -1 };
    fillAreaRows();
  });
  window.addEventListener("hashchange", () => {
    const { country, region, virus } = readHash();
    if (!country || !countryMeta(country)) return;
    if (country !== state.countryId || (region && region !== state.region?.id) || (virus ?? "covid") !== state.virus) {
      safeShow(country, region, virus ?? "covid");
    }
  });
  new ResizeObserver(() => {
    cancelAnimationFrame(chartFrame);
    chartFrame = requestAnimationFrame(() => state.region && renderChart());
  }).observe($("chart"));
}

async function init() {
  try {
    state.index = await getJSON("index.json");
  } catch (err) {
    $("load-status").textContent = `Couldn't load the data (${err.message}). If you're running this locally, build it first with "python -m wastewater build" and serve the site folder.`;
    return;
  }
  if (!isValidIndex(state.index)) {
    $("load-status").textContent =
      "This data was made by a different version of Sewer Signal. Rebuild it with \"python -m wastewater build\", or wait for the next daily refresh.";
    return;
  }
  if (!state.index.countries.length || !state.index.viruses.length) {
    $("load-status").textContent = "The last data build produced no usable data. Check the build log.";
    return;
  }
  try {
    state.labels = Object.assign(Object.create(null), Object.fromEntries(state.index.categories.map((c) => [c.id, c.label])));
    state.virusLabels = Object.assign(Object.create(null), Object.fromEntries(state.index.viruses.map((v) => [v.id, v.label])));
    const virusIds = state.index.viruses.map((v) => v.id);
    if (!own(state.virusLabels, state.virus)) state.virus = virusIds[0];
    state.planViruses = Array.isArray(state.planViruses) ? virusIds.filter((v) => state.planViruses.includes(v)) : virusIds;
    if (!(state.trip && typeof state.trip.country === "string")) state.trip = null;
    state.activity = activityById(state.activity).id;
    try {
      localStorage.removeItem("sewer-signal:vulnerable"); // saved by older versions
    } catch {
      /* storage unavailable */
    }
    renderActivities();
    bindEvents();
    const fromHash = readHash();
    const country = countryMeta(fromHash.country) ? fromHash.country : store.get("country", state.index.default_country);
    const region = fromHash.country === country ? fromHash.region : store.get(`region:${country}`, null);
    await show(countryMeta(country) ? country : state.index.default_country, region, fromHash.country === country ? fromHash.virus ?? virusIds[0] : state.virus);
  } catch (err) {
    $("load-status").textContent = `Couldn't load the data (${err.message}).`;
  }
}

/** The shape this version of the page expects from index.json. */
function isValidIndex(ix) {
  return (
    ix != null &&
    Array.isArray(ix.countries) &&
    Array.isArray(ix.categories) &&
    Array.isArray(ix.viruses) &&
    ix.countries.every((c) => c && typeof c.id === "string" && c.viruses && typeof c.viruses === "object")
  );
}

init();
