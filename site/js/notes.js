// What the page says about an area's own data and forecast: why there's no
// forecast, and whether the data is too old to go on. Pure functions with no
// DOM, so node can test them.

import { formatDate } from "./format.js";

// Data older than this gets a warning even when there's a forecast.
export const STALE_DAYS = 42;

// Why the build published no forecast for an area ("no_forecast" in its data).
// Anything else, or nothing, means it hasn't enough recent history.
const OLD = "old_data"; // newest data more than 35 days old when the forecasts were made
const AHEAD = "future_data"; // newest data dated more than 7 days after they were made

/** Whether an area's data is too old to give advice from (`age` in days). */
export function isStale(region, age) {
  return age > STALE_DAYS || region?.no_forecast === OLD;
}

/** The warning about an area's old data, or null when it doesn't need one. */
export function staleWarning(region, latestDate, age) {
  if (!isStale(region, age)) return null;
  const end = region.forecast?.length ? ", so treat the forecast with caution" : region.no_forecast === OLD ? ", too old to forecast from" : "";
  return `The newest data for this area is from ${formatDate(latestDate, true)}, ${Math.round(age / 7)} weeks ago${end}.`;
}

/** Whether the general advice for a level fits: it needs this week's reading or an estimate, not just an old reading. */
export function showGeneralAdvice(nowAvailable, region, age) {
  return nowAvailable || !isStale(region, age);
}

/** The end of "No forecast for this area: ...". */
export function noForecastWhy(region) {
  if (region?.no_forecast === OLD) return "its newest data was more than 5 weeks old when the forecasts were made";
  if (region?.no_forecast === AHEAD) return "its newest data is dated more than a week after the forecasts were made";
  return "it doesn't have enough recent history";
}

/** The line under the verdict when there's no estimate for this week. */
export function noEstimateText(region) {
  return region?.no_forecast === OLD || region?.no_forecast === AHEAD
    ? "There's no estimate for this week."
    : "There's no estimate for this week yet.";
}

/** The planner's line for a virus with no forecast for the chosen week (`scope` names a wider area, or is ""). */
export function plannerNoForecast(region, scope) {
  if (region?.no_forecast === OLD) return `No forecast: the newest data is too old${scope}.`;
  if (region?.no_forecast === AHEAD) return `No forecast: the newest data is dated more than a week ahead${scope}.`;
  return `No forecast reaches that week yet${scope}.`;
}
