import { parseISO } from "./dates.js";

const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumSignificantDigits: 3 });
const plain = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 3 });
const pct = new Intl.NumberFormat(undefined, { style: "percent", maximumFractionDigits: 0 });

export function formatValue(v) {
  if (v == null || Number.isNaN(v)) return "–";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a < 0.01) return v.toExponential(1);
  return a < 1000 ? plain.format(v) : compact.format(v);
}

export function formatPct(p) {
  if (p == null) return "–";
  if (p > 0 && p < 0.01) return "<1%";
  if (p < 1 && p > 0.99) return ">99%";
  return pct.format(p);
}

export function formatSignedPct(x) {
  if (x == null) return "–";
  const rounded = Math.round(x);
  return `${rounded > 0 ? "+" : rounded < 0 ? "−" : "±"}${Math.abs(rounded)}%`;
}

const dayMonth = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
const dayMonthYear = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });

export function formatDate(d, withYear = false) {
  const date = typeof d === "string" ? parseISO(d) : d;
  return (withYear ? dayMonthYear : dayMonth).format(date);
}

export function formatDateTime(isoTimestamp) {
  const d = new Date(isoTimestamp);
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(d);
}
