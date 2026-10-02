// Calendar helpers. Dates in the data are ISO "YYYY-MM-DD" week-ending Sundays,
// handled as local calendar dates so time zones never shift a week.

export function parseISO(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(y, m - 1, d);
}

export function toISO(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export function addDays(date, days) {
  const out = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  out.setDate(out.getDate() + days);
  return out;
}

/** The Sunday that ends the Monday-Sunday week containing `date`. */
export function weekEnding(date) {
  return addDays(date, (7 - date.getDay()) % 7);
}

export function startOfToday() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

export function daysBetween(a, b) {
  return Math.round((b - a) / 86400000);
}
