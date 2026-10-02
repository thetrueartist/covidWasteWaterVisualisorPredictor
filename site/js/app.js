import {
  ACTIVITIES,
  LEVEL_IDS,
  activityById,
  advise,
  expectedLevel,
  lowestWeek,
  mostLikely,
  planWeeks,
} from "./advisor.js";
import { drawChart, fillTable } from "./chart.js";
import { daysBetween, parseISO, startOfToday, toISO, weekEnding } from "./dates.js";
import { formatDate, formatDateTime, formatPct, formatSignedPct, formatValue } from "./format.js";

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

const state = {
  index: null,
  labels: {},
  countryId: null,
  countries: new Map(),
  region: null,
  weeks: store.get("weeks", 52),
  scale: store.get("scale", "linear"),
  activity: store.get("activity", "restaurant"),
  when: 0,
  vulnerable: store.get("vulnerable", false),
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

// ---------- small DOM helpers ----------
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
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
  return h("span", { class: "chip", dataset: { level: levelId ?? "none" } }, text ?? (levelId ? state.labels[levelId] : "No data"));
}

function trendIcon(label) {
  if (!label) return svgIcon(ICONS.flat, 14);
  if (label.startsWith("rising")) return svgIcon(ICONS.up, 14);
  if (label.startsWith("falling")) return svgIcon(ICONS.down, 14);
  return svgIcon(ICONS.flat, 14);
}

function levelOfIndex(idx) {
  return idx == null ? null : LEVEL_IDS[Math.min(4, Math.floor(idx / 20))];
}

// ---------- data ----------
async function getJSON(path) {
  const res = await fetch(DATA_DIR + path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

async function loadCountry(id) {
  if (!state.countries.has(id)) {
    const meta = state.index.countries.find((c) => c.id === id);
    state.countries.set(id, { meta, data: await getJSON(meta.file) });
  }
  return state.countries.get(id);
}

function currentCountry() {
  return state.countries.get(state.countryId);
}

function readHash() {
  const raw = decodeURIComponent(location.hash.slice(1));
  const [country, region] = raw.split("~");
  return { country, region };
}

function writeHash() {
  const hash = `#${state.countryId}~${state.region.id}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

// ---------- selection ----------
async function selectCountry(id, regionId) {
  const meta = state.index.countries.find((c) => c.id === id) ?? state.index.countries.find((c) => c.id === state.index.default_country);
  document.getElementById("main").setAttribute("aria-busy", "true");
  const { data } = await loadCountry(meta.id);
  state.countryId = meta.id;
  state.group = "";
  state.search = "";
  state.showAll = false;
  $("area-search").value = "";
  store.set("country", meta.id);
  const region = data.regions.find((r) => r.id === regionId) ?? data.regions.find((r) => r.id === meta.default_region) ?? data.regions[0];
  renderCountryPicker();
  renderAreaPicker();
  selectRegion(region.id);
  renderAreas();
  renderAbout();
  document.getElementById("main").setAttribute("aria-busy", "false");
}

function selectRegion(id) {
  const { data } = currentCountry();
  state.region = data.regions.find((r) => r.id === id) ?? data.regions[0];
  store.set(`region:${state.countryId}`, state.region.id);
  $("area-select").value = state.region.id;
  writeHash();
  renderRegion();
  markCurrentRow();
}

// ---------- pickers ----------
function renderCountryPicker() {
  const sel = $("country-select");
  setChildren(sel, ...state.index.countries.map((c) => h("option", { value: c.id }, `${c.flag} ${c.name}`)));
  sel.value = state.countryId;
}

function regionOptionText(r) {
  return r.parent && r.level !== "region" ? `${r.name} (${r.parent})` : r.name;
}

function renderAreaPicker() {
  const sel = $("area-select");
  const groups = new Map();
  for (const r of currentCountry().data.regions) {
    if (!groups.has(r.group)) groups.set(r.group, []);
    groups.get(r.group).push(r);
  }
  setChildren(sel, 
    ...[...groups].map(([group, regions]) =>
      h("optgroup", { label: group }, regions.map((r) => h("option", { value: r.id }, regionOptionText(r)))),
    ),
  );
}

// ---------- region views ----------
function renderRegion() {
  const r = state.region;
  const weeks = planWeeks(r, state.today, 4);
  if (!weeks[state.when]?.available) state.when = Math.max(0, weeks.findIndex((w) => w.available));
  renderVerdict(r, weeks);
  renderWeeks(r);
  renderChart();
  renderPlanner(weeks);
  $("verdict").hidden = false;
  $("desk").hidden = false;
  $("areas-card").hidden = false;
  $("about").hidden = false;
  $("load-status").hidden = true;
  document.title = `${r.name} · Sewer Signal`;
}

function renderVerdict(r, weeks) {
  const { meta } = currentCountry();
  const latest = r.latest;
  const where = r.level === "national" ? "Whole country" : [r.group, r.level !== "region" ? r.parent : null].filter(Boolean).join(" · ");
  $("verdict-eyebrow").textContent = `${r.name} · ${where} · latest sample ${formatDate(latest.source_date, true)}`;

  const heading = $("verdict-heading");
  setChildren(heading, chip(latest.category), h("span", {}, TREND_TEXT[latest.trend?.label] ?? ""));
  heading.setAttribute("aria-label", `${state.labels[latest.category]} ${TREND_TEXT[latest.trend?.label] ?? ""}`.trim());

  const now = weeks[0];
  const nowLine = $("verdict-now");
  let nowLevel = latest.category;
  if (now.available && now.source === "forecast") {
    const ml = mostLikely(now.probs);
    nowLevel = LEVEL_IDS[Math.min(4, Math.round(expectedLevel(now.probs) - 0.5))];
    setChildren(nowLine, 
      `Estimate for this week (to ${formatDate(now.date)}): most likely `,
      h("strong", {}, state.labels[ml.id].toLowerCase()),
      `, ${formatPct(ml.p)} chance. Reports run ${lagText(latest.date, now.date)} behind.`,
    );
  } else if (now.available) {
    nowLine.textContent = `This week's reading is in: ${state.labels[latest.category].toLowerCase()}.`;
  } else {
    nowLine.textContent = "There's no estimate for this week yet.";
  }
  $("verdict-advice").textContent = GENERAL_ADVICE[nowLevel] ?? "";

  const unit = meta.source.unit;
  const pctOfPeak = r.window.max > 0 ? latest.value / r.window.max : null;
  const stats = [
    ["Level", `${latest.index}/100`, `Higher than ${latest.index}% of weeks in the past two years`],
    [
      "Trend",
      h("span", { class: "trend" }, trendIcon(latest.trend?.label), latest.trend ? `${formatSignedPct(latest.trend.pct_per_week)}/wk` : "–"),
      latest.trend ? `${TREND_WORD[latest.trend.label]} over the last two weeks` : "Not enough recent data",
    ],
    ["Latest level", h("span", {}, formatValue(latest.value), " ", h("span", { class: "unit" }, unit)), `Smoothed, week ending ${formatDate(latest.date)}`],
    ["Versus 2-year peak", pctOfPeak == null ? "–" : formatPct(pctOfPeak), `Peak ${formatValue(r.window.max)} ${unit}`],
  ];
  setChildren($("stats"), 
    ...stats.map(([label, value, note]) => h("div", { class: "stat" }, h("dt", {}, label), h("dd", {}, value, h("span", { class: "stat-note" }, note)))),
  );

  const age = daysBetween(parseISO(latest.date), state.today);
  const stale = $("stale-warning");
  stale.hidden = age <= 42;
  stale.textContent = `The newest data for this area is from ${formatDate(latest.date, true)}, ${Math.round(age / 7)} weeks ago, so treat the forecast with caution.`;
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
    { class: "prob-bar", role: "img", "aria-label": LEVEL_IDS.map((id, i) => `${state.labels[id]} ${formatPct(probs[i])}`).join(", ") },
    LEVEL_IDS.map((id, i) => (probs[i] >= 0.005 ? h("span", { class: `lvl-fill-${id}`, style: `flex:${probs[i]}`, title: `${state.labels[id]}: ${formatPct(probs[i])}` }) : null)),
  );
}

function renderWeeks(r) {
  const list = $("weeks");
  if (!r.forecast.length) {
    setChildren(list, h("li", { class: "week" }, "No forecast for this area: it doesn't have enough recent history."));
    return;
  }
  const thisWeek = toISO(weekEnding(state.today));
  const upcoming = r.forecast.map((f) => ({ ...f, available: true }));
  const futureIdx = upcoming.map((f, i) => (f.date >= thisWeek ? i : -1)).filter((i) => i >= 0);
  const best = lowestWeek(futureIdx.map((i) => upcoming[i]));
  const bestIdx = best >= 0 ? futureIdx[best] : -1;
  setChildren(list, 
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
  setChildren($("level-key"), 
    ...LEVEL_IDS.map((id, k) => h("li", {}, h("span", { class: "swatch", style: `background:var(--lvl-${k + 1})` }), state.labels[id])),
  );
}

let chartFrame = 0;
function renderChart() {
  const r = state.region;
  const { meta } = currentCountry();
  $("chart-sub").textContent = `${meta.source.metric} (${meta.source.unit})`;
  setChildren($("chart-legend"), 
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
    unit: meta.source.unit,
    today: state.today,
    labels: state.labels,
    tooltip: $("tooltip"),
  });
  fillTable($("data-table"), r, { unit: meta.source.unit, labels: state.labels });
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
  setChildren($("activity-list"), 
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
            renderPlanner(planWeeks(state.region, state.today, 4));
          },
        }),
        h("label", { for: `act-${a.id}` }, a.label, h("small", {}, a.detail)),
      ),
    ),
  );
  $("vulnerable").checked = state.vulnerable;
}

function renderPlanner(weeks) {
  setChildren($("when-buttons"), 
    ...weeks.map((w, i) =>
      h(
        "button",
        {
          type: "button",
          "aria-pressed": String(i === state.when),
          disabled: !w.available,
          onclick: () => {
            state.when = i;
            renderPlanner(weeks);
          },
        },
        w.label,
        h("small", {}, `to ${formatDate(w.date)}`),
      ),
    ),
  );

  const out = $("advice");
  const activity = activityById(state.activity);
  const result = advise({ weeks, weekIndex: state.when, activity, vulnerable: state.vulnerable });
  if (!result) {
    setChildren(out, h("p", { class: "advice-why" }, "No forecast reaches that week yet. Try an earlier week."));
    return;
  }
  const { verdict, level, week, tips, better } = result;
  const when = week.label.toLowerCase();
  const why =
    week.source === "measured"
      ? `${activity.label}, ${when}: the latest reading is ${state.labels[level.id].toLowerCase()}.`
      : `${activity.label}, ${when}: levels most likely ${state.labels[level.id].toLowerCase()} (${formatPct(level.p)} chance).`;
  setChildren(out, 
    h("div", { class: "advice-head" }, h("span", { class: "status-icon", dataset: { status: verdict.id } }, svgIcon(ICONS[verdict.id], 18)), h("p", { class: "advice-title" }, verdict.label)),
    h("p", { class: "advice-why" }, why, state.vulnerable || activity.protectsOthers ? " Includes extra caution for people at higher risk." : ""),
    h("ul", {}, tips.map((t) => h("li", {}, t))),
    better
      ? h(
          "p",
          { class: "better-week" },
          h("strong", {}, `${better.week.label} (to ${formatDate(better.week.date)})`),
          ` looks better: ${better.verdict.label.toLowerCase()}.`,
        )
      : null,
  );
}

// ---------- areas table ----------
function areaRows() {
  const { data } = currentCountry();
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
  const { data } = currentCountry();
  // distribution of current levels (areas below national)
  const areas = data.regions.filter((r) => r.level !== "national");
  const counts = LEVEL_IDS.map((id) => areas.filter((r) => r.latest.category === id).length);
  const total = counts.reduce((a, b) => a + b, 0);
  $("areas-sub").textContent = total
    ? `${total} areas reporting. ${LEVEL_IDS.map((id, i) => `${counts[i]} ${state.labels[id].toLowerCase()}`).join(", ")}.`
    : "Only a national series is published.";
  setChildren($("distribution"), 
    ...LEVEL_IDS.map((id, i) => (counts[i] ? h("span", { class: `lvl-fill-${id}`, style: `flex:${counts[i]}`, title: `${state.labels[id]}: ${counts[i]}` }) : null)),
  );
  $("distribution").hidden = total === 0;

  const groups = [...new Set(data.regions.map((r) => r.group))];
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
    const tr = h(
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
    return tr;
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
  selectRegion(id);
  $("verdict").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
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

  const model = idx.model;
  const t = $("metrics-table");
  const head = h("thead", {}, h("tr", {}, ["Weeks ahead", "Typical miss", "No-change miss", "Better by", "Right level"].map((c, i) => h("th", { scope: "col", class: i ? "num" : null }, c))));
  const body = h(
    "tbody",
    {},
    model.by_horizon.map((m) =>
      h(
        "tr",
        {},
        h("td", {}, String(m.horizon_weeks)),
        h("td", { class: "num" }, `±${Math.round(m.typical_error_pct)}%`),
        h("td", { class: "num" }, `±${Math.round(m.typical_error_pct_no_change)}%`),
        h("td", { class: "num" }, formatSignedPct(100 * m.skill_vs_no_change_final)),
        h("td", { class: "num" }, `${Math.round(100 * m.category_accuracy)}%`),
      ),
    ),
  );
  setChildren(t, head, body);

  const { meta } = currentCountry();
  const countryRows = model.by_country?.[state.countryId] ?? [];
  const fallback = model.fallback?.[state.countryId] ?? [];
  const skills = countryRows.filter((r) => r.uses_model).map((r) => r.skill_vs_no_change);
  let countryNote = "";
  if (countryRows.length) {
    if (!skills.length) {
      countryNote = `For ${meta.name} the model didn't beat no-change in testing, so its forecasts assume levels stay where they are, with the model's uncertainty range.`;
    } else {
      const lo = Math.round(100 * Math.min(...skills));
      const hi = Math.round(100 * Math.max(...skills));
      countryNote = `For ${meta.name} the model was ${lo === hi ? `${lo}%` : `${lo}–${hi}%`} more accurate than no-change${fallback.length ? `, and uses no-change ${fallback.length === 1 ? `for ${fallback[0]}` : `for ${fallback[0]}–${fallback[fallback.length - 1]}`} weeks ahead, where it wasn't` : ""}.`;
    }
  }
  const baseline = model.by_horizon.map((m) => Math.round(100 * m.category_accuracy_no_change));
  $("metrics-note").textContent =
    `Tested on ${model.n_series} series, on weeks from ${formatDate(model.holdout_start, true)} onward that the model never saw. "Typical miss" is how far the forecast usually lands from the real level. "Right level" counts forecasts that landed in the correct band; assuming no change managed ${Math.min(...baseline)}–${Math.max(...baseline)}%. ${countryNote}`;

  const src = meta.source;
  setChildren($("source"), 
    h("a", { href: src.url, target: "_blank", rel: "noopener" }, src.publisher),
    `. ${src.metric}. ${src.notes} Licence: ${src.license}.`,
  );
  setChildren($("footer-text"), 
    `Data rebuilt ${formatDateTime(idx.generated_at)}. Public data from health agencies in six countries. `,
    h("a", { href: REPO_URL, target: "_blank", rel: "noopener" }, "Source code"),
    ". Not medical advice.",
  );
}

// ---------- events ----------
function bindEvents() {
  $("country-select").addEventListener("change", (e) => selectCountry(e.target.value, store.get(`region:${e.target.value}`, null)));
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
    state.scale = b.dataset.scale;
    store.set("scale", state.scale);
    renderChart();
  });
  $("vulnerable").addEventListener("change", (e) => {
    state.vulnerable = e.target.checked;
    store.set("vulnerable", state.vulnerable);
    renderPlanner(planWeeks(state.region, state.today, 4));
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
    const { country, region } = readHash();
    if (country && country !== state.countryId) selectCountry(country, region);
    else if (region && region !== state.region?.id) selectRegion(region);
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
  if (!state.index.countries.length) {
    $("load-status").textContent = "The last data build produced no countries. Check the build log.";
    return;
  }
  state.labels = Object.fromEntries(state.index.categories.map((c) => [c.id, c.label]));
  renderActivities();
  bindEvents();
  const fromHash = readHash();
  const country = state.index.countries.some((c) => c.id === fromHash.country) ? fromHash.country : store.get("country", state.index.default_country);
  const region = fromHash.country === country ? fromHash.region : store.get(`region:${country}`, null);
  await selectCountry(country, region);
}

init();
