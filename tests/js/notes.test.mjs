import assert from "node:assert/strict";
import { test } from "node:test";

import { isStale, noEstimateText, noForecastWhy, plannerNoForecast, showGeneralAdvice, staleWarning } from "../../site/js/notes.js";

const area = (extra = {}) => ({ forecast: [{ h: 1 }], ...extra });
const withheld = (why) => area({ forecast: [], no_forecast: why });

test("an area whose forecast was withheld for old data is flagged as old, even under 6 weeks", () => {
  // The build stops forecasting at 35 days; the general warning starts at 42.
  const old = withheld("old_data");
  assert.equal(isStale(old, 37), true);
  assert.match(staleWarning(old, "2026-08-30", 37), /^The newest data for this area is from .*, 5 weeks ago, too old to forecast from\.$/);
  assert.equal(isStale(area(), 37), false);
  assert.equal(staleWarning(area(), "2026-08-30", 37), null);
  // older still: the same note, never "treat the forecast with caution" when there's no forecast
  assert.match(staleWarning(old, "2026-08-16", 51), /7 weeks ago, too old to forecast from\.$/);
});

test("other areas only get the warning from 6 weeks", () => {
  assert.equal(staleWarning(area(), "2026-08-23", 42), null);
  assert.match(staleWarning(area(), "2026-08-16", 50), /7 weeks ago, so treat the forecast with caution\.$/);
  assert.match(staleWarning(withheld(undefined), "2026-08-16", 50), /7 weeks ago\.$/);
});

test("no advice for this week from an old reading alone", () => {
  assert.equal(showGeneralAdvice(false, withheld("old_data"), 37), false);
  assert.equal(showGeneralAdvice(false, area({ forecast: [] }), 50), false);
  assert.equal(showGeneralAdvice(true, area(), 50), true); // there's an estimate for this week
  assert.equal(showGeneralAdvice(false, area(), 14), true); // a recent reading
});

test("the page says why there's no forecast, without promising one", () => {
  assert.equal(noForecastWhy(withheld("old_data")), "its newest data was more than 5 weeks old when the forecasts were made");
  assert.equal(noForecastWhy(withheld("future_data")), "its newest data is dated more than a week after the forecasts were made");
  assert.equal(noForecastWhy(withheld(undefined)), "it doesn't have enough recent history");
  assert.equal(noForecastWhy(withheld("<script>")), "it doesn't have enough recent history");
  assert.equal(noEstimateText(withheld("old_data")), "There's no estimate for this week.");
  assert.equal(noEstimateText(area()), "There's no estimate for this week yet.");
  assert.equal(plannerNoForecast(withheld("old_data"), ""), "No forecast: the newest data is too old.");
  assert.equal(plannerNoForecast(withheld("future_data"), " (Bavaria-wide: no local data)"),
    "No forecast: the newest data is dated more than a week ahead (Bavaria-wide: no local data).");
  assert.equal(plannerNoForecast(area(), ""), "No forecast reaches that week yet.");
});
