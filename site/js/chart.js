// Hand-built SVG time series: smoothed trend, weekly measurements, forecast
// fan (50% and 90% ranges), level guide lines, today marker, and a crosshair
// readout that follows the pointer or the arrow keys.

import { addDays, daysBetween, parseISO } from "./dates.js";
import { formatDate, formatPct, formatValue } from "./format.js";
import { LEVEL_IDS, mostLikely } from "./advisor.js";

const NS = "http://www.w3.org/2000/svg";
const MONTHS = new Intl.DateTimeFormat(undefined, { month: "short" });

function svg(name, attrs = {}, parent = null) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

function niceStep(raw) {
  const pow = 10 ** Math.floor(Math.log10(raw));
  const f = raw / pow;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * pow;
}

function linearTicks(max, count) {
  if (!(max > 0) || !Number.isFinite(max)) max = 1; // an all-zero window still needs a scale
  const step = niceStep(max / count);
  const ticks = [];
  for (let v = 0; v <= max + step * 1e-9; v += step) ticks.push(v);
  return ticks;
}

function logTicks(min, max) {
  const ticks = [];
  for (let e = Math.floor(Math.log10(min)); e <= Math.ceil(Math.log10(max)); e++) {
    for (const m of [1, 2, 5]) {
      const v = m * 10 ** e;
      if (v >= min && v <= max) ticks.push(v);
    }
  }
  if (ticks.length > 7) return ticks.filter((v) => Math.abs(Math.log10(v) % 1) < 1e-9);
  return ticks;
}

function monthTicks(from, to, widthPx) {
  const months = daysBetween(from, to) / 30.4;
  const every = [1, 2, 3, 6, 12].find((n) => widthPx / (months / n) >= 64) ?? 12;
  const ticks = [];
  const d = new Date(from.getFullYear(), from.getMonth() + 1, 1);
  while (d <= to) {
    if (d.getMonth() % every === 0) ticks.push(new Date(d));
    d.setMonth(d.getMonth() + 1);
  }
  return ticks;
}

function pathFrom(points) {
  let d = "";
  let pen = false;
  for (const p of points) {
    if (p == null) {
      pen = false;
      continue;
    }
    d += `${pen ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`;
    pen = true;
  }
  return d;
}

/**
 * Draw the chart for `region` into `container`.
 * opts: { weeks (0 = all), scale ("linear"|"log"), unit, today (Date), labels ({levelId: label}), tooltip (element) }
 */
export function drawChart(container, region, opts) {
  container.replaceChildren();
  const width = Math.max(280, Math.floor(container.clientWidth));
  const narrow = width < 560;
  const height = Math.round(Math.min(380, Math.max(240, width * 0.46)));
  const m = { top: 22, right: narrow ? 12 : 84, bottom: 28, left: 50 };
  const pw = width - m.left - m.right;
  const ph = height - m.top - m.bottom;
  const logScale = opts.scale === "log";

  // ---- data ----
  const start = parseISO(region.start);
  const hist = region.smooth.map((s, i) => ({
    date: addDays(start, 7 * i),
    smooth: s,
    raw: region.raw[i],
    index: region.index[i],
  }));
  const firstData = hist.findIndex((p) => p.smooth != null);
  let lastObs = hist.length - 1;
  while (lastObs > 0 && hist[lastObs].smooth == null) lastObs--;
  const from = opts.weeks > 0 ? Math.max(firstData, hist.length - opts.weeks) : firstData;
  const vis = hist.slice(Math.max(0, Math.min(from, hist.length - 1)));
  const fc = region.forecast.map((f) => ({ date: parseISO(f.date), q: f.q, f }));
  const anchor = hist[lastObs];

  // ---- scales ----
  const smoothVals = vis.map((p) => p.smooth).filter((v) => v != null);
  const rawVals = vis.map((p) => p.raw).filter((v) => v != null);
  const maxSmooth = Math.max(...smoothVals, ...fc.map((f) => f.q[2]));
  const cap = maxSmooth * 1.8;
  const yMaxData = Math.max(
    maxSmooth,
    Math.min(Math.max(0, ...rawVals), cap),
    Math.min(Math.max(0, ...fc.map((f) => f.q[4])), cap),
  );

  let y, yTicks, yMin, yMax;
  if (logScale) {
    const positives = [...smoothVals, ...rawVals, ...fc.map((f) => f.q[0])].filter((v) => v > 0);
    const top = yMaxData > 0 && Number.isFinite(yMaxData) ? yMaxData : 1;
    yMin = positives.length ? Math.max(Math.min(...positives) * 0.8, top / 1e5) : top / 100;
    yMax = top * 1.25;
    const lmin = Math.log10(yMin);
    const lmax = Math.log10(yMax);
    y = (v) => ph - ((Math.log10(Math.max(v, yMin)) - lmin) / (lmax - lmin)) * ph;
    yTicks = logTicks(yMin, yMax);
  } else {
    yTicks = linearTicks(yMaxData * 1.05, 4);
    yMin = 0;
    yMax = yTicks[yTicks.length - 1];
    y = (v) => ph - (Math.min(v, yMax) / yMax) * ph;
  }
  const x0 = vis[0].date;
  const x1 = addDays(fc.length ? fc[fc.length - 1].date : hist[hist.length - 1].date, 2);
  const span = x1 - x0;
  const x = (d) => ((d - x0) / span) * pw;

  // ---- frame ----
  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: "img",
    tabindex: "0",
    "aria-label": `${region.name}: wastewater level over time with forecast. Use the arrow keys to read values week by week.`,
  });
  container.appendChild(root);
  const g = svg("g", { transform: `translate(${m.left},${m.top})` }, root);
  const clipId = `clip-${Math.random().toString(36).slice(2, 8)}`;
  const defs = svg("defs", {}, root);
  svg("rect", { x: 0, y: -4, width: pw, height: ph + 4 }, svg("clipPath", { id: clipId }, defs));

  // gridlines + y labels
  for (const t of yTicks) {
    const ty = y(t);
    svg("line", { x1: 0, x2: pw, y1: ty, y2: ty, stroke: "var(--line)", "stroke-width": 1 }, g);
    const label = svg("text", { x: -8, y: ty + 4, "text-anchor": "end", class: "tick-label" }, g);
    label.textContent = formatValue(t);
  }
  // x ticks
  for (const t of monthTicks(x0, x1, pw)) {
    const tx = x(t);
    if (tx < 0 || tx > pw) continue;
    svg("line", { x1: tx, x2: tx, y1: ph, y2: ph + 4, stroke: "var(--axis)", "stroke-width": 1 }, g);
    const label = svg("text", { x: tx, y: ph + 18, "text-anchor": "middle", class: "tick-label" }, g);
    label.textContent = `${MONTHS.format(t)}${t.getMonth() === 0 ? ` ${t.getFullYear()}` : ""}`;
  }

  // forecast zone
  const fx = x(anchor.date);
  if (fc.length) {
    svg("rect", { x: fx, y: 0, width: Math.max(0, pw - fx), height: ph, fill: "var(--forecast-zone)" }, g);
  }

  // level guide lines and band labels
  const edges = region.thresholds;
  edges.forEach((v) => {
    if (v <= yMin || v >= yMax) return;
    svg("line", { x1: 0, x2: pw, y1: y(v), y2: y(v), stroke: "var(--axis)", "stroke-width": 1 }, g);
  });
  if (!narrow) {
    const bounds = [yMin, ...edges, yMax];
    LEVEL_IDS.forEach((id, k) => {
      const lo = Math.max(bounds[k], yMin);
      const hi = Math.min(bounds[k + 1], yMax);
      if (hi <= lo) return;
      const top = y(hi);
      const bottom = y(lo);
      if (bottom - top < 14) return;
      const cy = (top + bottom) / 2;
      svg("rect", { x: pw + 10, y: cy - 4.5, width: 9, height: 9, rx: 2, fill: `var(--lvl-${k + 1})` }, g);
      const t = svg("text", { x: pw + 24, y: cy + 4, class: "band-label" }, g);
      t.textContent = opts.labels[id];
    });
  }

  // baseline
  svg("line", { x1: 0, x2: pw, y1: ph, y2: ph, stroke: "var(--axis)", "stroke-width": 1 }, g);

  const plot = svg("g", { "clip-path": `url(#${clipId})` }, g);

  // forecast fan
  if (fc.length) {
    const band = (lo, hi, fill) => {
      const upper = [[fx, y(anchor.smooth)], ...fc.map((f) => [x(f.date), y(f.q[hi])])];
      const lower = [...fc.map((f) => [x(f.date), y(f.q[lo])])].reverse();
      svg("path", { d: `${pathFrom(upper)}L${lower.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("L")}Z`, fill }, plot);
    };
    band(0, 4, "var(--series-wash)");
    band(1, 3, "var(--series-wash-2)");
    svg("path", {
      d: pathFrom([[fx, y(anchor.smooth)], ...fc.map((f) => [x(f.date), y(f.q[2])])]),
      fill: "none",
      stroke: "var(--series)",
      "stroke-width": 2,
      "stroke-dasharray": "5 4",
      "stroke-linecap": "round",
    }, plot);
  }

  // measurements
  for (const p of vis) {
    if (p.raw == null) continue;
    svg("circle", { cx: x(p.date), cy: y(p.raw), r: 2.4, fill: "var(--muted)", "fill-opacity": 0.55 }, plot);
  }

  // smoothed trend
  svg("path", {
    d: pathFrom(vis.map((p) => (p.smooth == null ? null : [x(p.date), y(p.smooth)]))),
    fill: "none",
    stroke: "var(--series)",
    "stroke-width": 2,
    "stroke-linejoin": "round",
    "stroke-linecap": "round",
  }, plot);
  svg("circle", { cx: fx, cy: y(anchor.smooth), r: 4.5, fill: "var(--series)", stroke: "var(--surface)", "stroke-width": 2 }, g);

  // today marker
  const tx = x(opts.today);
  if (tx >= 0 && tx <= pw) {
    svg("line", { x1: tx, x2: tx, y1: 0, y2: ph, stroke: "var(--ink-3)", "stroke-width": 1 }, g);
    const label = svg("text", { x: tx, y: -8, "text-anchor": "middle", class: "band-label" }, g);
    label.textContent = "Today";
  }

  // ---- hover / keyboard readout ----
  const points = [
    ...vis.filter((p) => p.smooth != null || p.raw != null).map((p) => ({ kind: "obs", date: p.date, p, latest: p === anchor })),
    ...fc.map((f) => ({ kind: "fc", date: f.date, f: f.f })),
  ];
  const cross = svg("line", { y1: 0, y2: ph, stroke: "var(--ink-2)", "stroke-width": 1, visibility: "hidden" }, g);
  const marker = svg("circle", { r: 4.5, fill: "var(--series)", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, g);
  const hit = svg("rect", { x: 0, y: 0, width: pw, height: ph, fill: "transparent" }, g);
  let current = -1;

  const show = (i, clientX, clientY) => {
    current = Math.max(0, Math.min(points.length - 1, i));
    const pt = points[current];
    const px = x(pt.date);
    const value = pt.kind === "obs" ? pt.p.smooth ?? pt.p.raw : pt.f.q[2];
    cross.setAttribute("x1", px);
    cross.setAttribute("x2", px);
    cross.setAttribute("visibility", "visible");
    marker.setAttribute("cx", px);
    marker.setAttribute("cy", y(value));
    marker.setAttribute("visibility", "visible");
    fillTooltip(opts.tooltip, pt, opts);
    const box = root.getBoundingClientRect();
    const scale = box.width / width;
    const cx = clientX ?? box.left + (m.left + px) * scale;
    const cy = clientY ?? box.top + (m.top + y(value)) * scale;
    placeTooltip(opts.tooltip, cx, cy);
  };
  const hide = () => {
    cross.setAttribute("visibility", "hidden");
    marker.setAttribute("visibility", "hidden");
    opts.tooltip.hidden = true;
  };
  const nearest = (clientX) => {
    const box = root.getBoundingClientRect();
    const px = (clientX - box.left) * (width / box.width) - m.left;
    const t = x0.getTime() + (px / pw) * span;
    let best = 0;
    points.forEach((pt, i) => {
      if (Math.abs(pt.date - t) < Math.abs(points[best].date - t)) best = i;
    });
    return best;
  };
  hit.addEventListener("pointermove", (e) => show(nearest(e.clientX), e.clientX, e.clientY));
  hit.addEventListener("pointerdown", (e) => show(nearest(e.clientX), e.clientX, e.clientY));
  hit.addEventListener("pointerleave", hide);
  root.addEventListener("blur", hide);
  root.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -1, ArrowRight: 1, Home: -1e6, End: 1e6 }[e.key];
    if (step == null) return;
    e.preventDefault();
    const firstForecast = points.findIndex((p) => p.kind === "fc");
    const latest = firstForecast > 0 ? firstForecast - 1 : points.length - 1;
    show(current < 0 ? latest : current + step);
  });
}

function row(parent, keyClass, label, value) {
  const r = document.createElement("div");
  r.className = "tt-row";
  const left = document.createElement("span");
  if (keyClass) {
    const key = document.createElement("i");
    key.className = keyClass;
    left.appendChild(key);
  }
  left.appendChild(document.createTextNode(label));
  const right = document.createElement("strong");
  right.textContent = value;
  r.append(left, right);
  parent.appendChild(r);
}

/** A weekly reading as text. Zero means below the lab's detection limit, not "no virus". */
function measured(raw, unit = "") {
  if (raw == null) return "no sample";
  if (raw === 0) return "below detection";
  return `${formatValue(raw)}${unit}`;
}

function fillTooltip(tip, pt, opts) {
  tip.replaceChildren();
  const unit = opts.unit ? ` ${opts.unit}` : "";
  const head = document.createElement("div");
  head.className = "tt-date";
  head.textContent = `Week ending ${formatDate(pt.date, true)}${pt.kind === "fc" ? " · forecast" : pt.latest ? " · provisional" : ""}`;
  tip.appendChild(head);
  if (pt.kind === "obs") {
    const p = pt.p;
    row(tip, "tt-key", "Trend", `${formatValue(p.smooth)}${unit}`);
    row(tip, "tt-key-dot", "Measured", measured(p.raw, unit));
    if (p.index != null) {
      const id = LEVEL_IDS[Math.min(4, Math.floor(p.index / 20))];
      row(tip, null, "Level", `${opts.labels[id]} (${p.index}/100)`);
    }
  } else {
    const f = pt.f;
    row(tip, "tt-key", "Most likely", `${formatValue(f.q[2])}${unit}`);
    row(tip, null, "Likely range", `${formatValue(f.q[1])}–${formatValue(f.q[3])}`);
    row(tip, null, "Wider range", `${formatValue(f.q[0])}–${formatValue(f.q[4])}`);
    const ml = mostLikely(f.probs);
    row(tip, null, "Level", `${opts.labels[ml.id]} (${formatPct(ml.p)})`);
  }
  tip.hidden = false;
}

function placeTooltip(tip, cx, cy) {
  const pad = 14;
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  let left = cx + pad;
  if (left + w > window.innerWidth - 8) left = cx - pad - w;
  let top = cy - h - pad;
  if (top < 8) top = cy + pad;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${Math.max(8, top)}px`;
}

/** Accessible table of recent weeks plus the forecast. */
export function fillTable(table, region, opts) {
  table.replaceChildren();
  const unit = opts.unit ? ` (${opts.unit})` : "";
  const thead = document.createElement("thead");
  const hr = document.createElement("tr");
  for (const [text, num] of [["Week ending", false], [`Measured${unit}`, true], [`Trend${unit}`, true], ["Level", false]]) {
    const th = document.createElement("th");
    th.scope = "col";
    th.textContent = text;
    if (num) th.className = "num";
    hr.appendChild(th);
  }
  thead.appendChild(hr);
  const body = document.createElement("tbody");
  const add = (cells) => {
    const tr = document.createElement("tr");
    cells.forEach(([text, num]) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (num) td.className = "num";
      tr.appendChild(td);
    });
    body.appendChild(tr);
  };
  for (const f of [...region.forecast].reverse()) {
    const ml = mostLikely(f.probs);
    add([
      [`${formatDate(f.date, true)} (forecast)`],
      ["–", true],
      [`${formatValue(f.q[2])} (${formatValue(f.q[1])}–${formatValue(f.q[3])})`, true],
      [`${opts.labels[ml.id]}, ${formatPct(ml.p)} likely`],
    ]);
  }
  const start = parseISO(region.start);
  for (let i = region.smooth.length - 1, n = 0; i >= 0 && n < 26; i--) {
    if (region.smooth[i] == null && region.raw[i] == null) continue;
    n++;
    const provisional = n === 1 ? " (provisional)" : "";
    const idx = region.index[i];
    const id = idx == null ? null : LEVEL_IDS[Math.min(4, Math.floor(idx / 20))];
    add([
      [`${formatDate(addDays(start, 7 * i), true)}${provisional}`],
      [measured(region.raw[i]), true],
      [formatValue(region.smooth[i]), true],
      [id ? `${opts.labels[id]} (${idx}/100)` : "–"],
    ]);
  }
  table.append(thead, body);
}
