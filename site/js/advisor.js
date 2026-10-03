// Go / avoid advice. Pure functions, no DOM, so they can be unit tested.
//
// Risk score = level + activity exposure (+ vulnerability)
//   level:     expected level band, 0.5 (very low) .. 4.5 (very high),
//              averaged over the forecast's probabilities so uncertainty counts
//   exposure:  how much shared indoor air the activity involves
//   vulnerable: +1.25 if you, or someone you'll be around, is at higher risk
// Score thresholds: < 3 go, < 4.5 precautions, < 6 postpone, otherwise avoid.

import { addDays, toISO, weekEnding } from "./dates.js";

export const LEVEL_IDS = ["very_low", "low", "moderate", "high", "very_high"];

export const ACTIVITIES = [
  { id: "outdoors", label: "Outdoors", detail: "Walks, parks, outdoor tables", exposure: -2 },
  { id: "errands", label: "Shops & errands", detail: "Supermarket, short bus or tube", exposure: -0.25 },
  { id: "small_gathering", label: "Small get-together", detail: "Under 10 people, indoors", exposure: 0.5 },
  { id: "work", label: "Office or classroom", detail: "Hours in shared air", exposure: 1 },
  { id: "restaurant", label: "Restaurant or pub", detail: "Eating and drinking indoors", exposure: 1.25 },
  { id: "gym", label: "Gym or class", detail: "Heavy breathing indoors", exposure: 1.25 },
  { id: "travel", label: "Flight or long trip", detail: "Plane, train or coach", exposure: 1.5 },
  { id: "crowd", label: "Gig, club or big event", detail: "Packed indoor crowd", exposure: 2 },
  { id: "visit_vulnerable", label: "Visiting someone vulnerable", detail: "Older relative, care home, hospital", exposure: 0.5, protectsOthers: true },
];

export const VULNERABLE_BONUS = 1.25;

export const VERDICTS = {
  go: { id: "go", label: "Go for it", max: 3 },
  precautions: { id: "precautions", label: "Go, with precautions", max: 4.5 },
  postpone: { id: "postpone", label: "Consider postponing", max: 6 },
  avoid: { id: "avoid", label: "Avoid if you can", max: Infinity },
};
const VERDICT_ORDER = ["go", "precautions", "postpone", "avoid"];

export function activityById(id) {
  return ACTIVITIES.find((a) => a.id === id) ?? ACTIVITIES[0];
}

/** Expected level band (0.5-4.5) from the five level probabilities. */
export function expectedLevel(probs) {
  return probs.reduce((sum, p, i) => sum + p * (i + 0.5), 0);
}

export function oneHot(levelId) {
  return LEVEL_IDS.map((id) => (id === levelId ? 1 : 0));
}

export function riskScore(probs, activity, vulnerable) {
  const bonus = vulnerable || activity.protectsOthers ? VULNERABLE_BONUS : 0;
  return expectedLevel(probs) + activity.exposure + bonus;
}

export function verdictForScore(score) {
  return VERDICTS[VERDICT_ORDER.find((id) => score < VERDICTS[id].max)];
}

export function verdictRank(id) {
  return VERDICT_ORDER.indexOf(id);
}

/**
 * The weeks someone can plan for, starting with the one containing `today`.
 * Each has the level probabilities: measured if that week has data, otherwise
 * from the forecast. Weeks beyond the forecast are marked unavailable.
 */
export function planWeeks(region, today, count = 4) {
  const thisWeek = weekEnding(today);
  const byDate = new Map(region.forecast.map((f) => [f.date, f]));
  const latestDate = region.latest.date;
  const names = ["This week", "Next week", "In 2 weeks", "In 3 weeks", "In 4 weeks"];
  const weeks = [];
  for (let k = 0; k < count; k++) {
    const date = toISO(addDays(thisWeek, 7 * k));
    const base = { key: `w${k}`, label: names[k] ?? `In ${k} weeks`, date };
    const f = byDate.get(date);
    if (f) {
      weeks.push({ ...base, available: true, source: "forecast", probs: f.probs, category: f.category, index: f.index, forecast: f });
    } else if (date <= latestDate) {
      const cat = region.latest.category;
      weeks.push({ ...base, available: true, source: "measured", probs: oneHot(cat), category: cat, index: region.latest.index });
    } else {
      weeks.push({ ...base, available: false });
    }
  }
  return weeks;
}

/** Most likely level and its probability. */
export function mostLikely(probs) {
  let best = 0;
  probs.forEach((p, i) => {
    if (p > probs[best]) best = i;
  });
  return { id: LEVEL_IDS[best], p: probs[best] };
}

export function tipsFor(verdictId, activity, vulnerable) {
  const tips = [];
  const a = activity.id;
  if (verdictId === "go") {
    tips.push("Stay home if you have symptoms, as always.");
    if (a === "visit_vulnerable") tips.push("A rapid test on the day is a cheap extra check.");
    return tips;
  }
  if (verdictId === "avoid") tips.push("If it can wait, wait. If it can't, keep it as short as you can.");
  if (verdictId === "postpone") tips.push("If you can move it, a lower week will cut your chance of catching it.");

  if (a === "outdoors") {
    tips.push("Outdoors is still your safest option. Keep shared indoor bits short.");
  } else if (a === "errands") {
    tips.push("Wear a well-fitting FFP2/N95 mask in busy shops and on public transport.");
    tips.push("Go at quieter times if you can.");
  } else if (a === "small_gathering") {
    tips.push("Open windows or meet outside. Fresh air makes a big difference.");
    tips.push("Ask people to test that morning, especially anyone with a sniffle.");
  } else if (a === "work") {
    tips.push("Ventilate: open windows or run an air purifier.");
    tips.push("Mask in meetings and shared rooms. Work from home if you're able to.");
  } else if (a === "restaurant") {
    tips.push("Ask for outdoor seating or a table near an open door or window.");
    tips.push("Go at quieter times. Busy rooms with poor airflow are riskiest.");
  } else if (a === "gym") {
    tips.push("Pick off-peak times and spaces with good airflow.");
    tips.push("Wipe down and keep your distance during hard sets.");
  } else if (a === "travel") {
    tips.push("Mask in the airport or station, while boarding and on board.");
    tips.push("Plane air is filtered in flight. The queues and boarding are the riskier part.");
  } else if (a === "crowd") {
    tips.push("Mask in the crowd and step outside for breaks.");
    tips.push("Stand near doors or vents rather than in the packed middle.");
  } else if (a === "visit_vulnerable") {
    tips.push("Take a rapid test on the day, before you go.");
    tips.push("Mask indoors and open a window. Meet outside if you can.");
  }
  if (vulnerable && verdictId !== "go") {
    tips.push("If you do catch it, ask about antivirals early. They work best in the first few days.");
  }
  return tips;
}

/**
 * Advice for one activity in one of the plannable weeks. Also looks for
 * another plannable week with a gentler verdict.
 */
export function advise({ weeks, weekIndex, activity, vulnerable }) {
  const week = weeks[weekIndex];
  if (!week || !week.available) return null;
  const score = riskScore(week.probs, activity, vulnerable);
  const verdict = verdictForScore(score);

  let better = null;
  weeks.forEach((w, i) => {
    if (i === weekIndex || !w.available) return;
    const s = riskScore(w.probs, activity, vulnerable);
    const v = verdictForScore(s);
    if (verdictRank(v.id) < verdictRank(verdict.id) && (!better || s < better.score)) {
      better = { week: w, score: s, verdict: v };
    }
  });

  return {
    week,
    score,
    verdict,
    level: mostLikely(week.probs),
    tips: tipsFor(verdict.id, activity, vulnerable),
    better,
  };
}

/** Index of the plannable week with the lowest expected level. */
export function lowestWeek(weeks) {
  let best = -1;
  weeks.forEach((w, i) => {
    if (!w.available) return;
    if (best < 0 || expectedLevel(w.probs) < expectedLevel(weeks[best].probs) - 1e-9) best = i;
  });
  return best;
}

/**
 * Advice for several viruses at once, kept separate. `byVirus` maps a virus
 * id to its plannable weeks (from planWeeks). Returns one result per virus,
 * the strictest verdict among them (so tips can match it), and a week where
 * every virus has a forecast and the strictest verdict would be gentler, if
 * there is one.
 */
export function adviseEach({ byVirus, weekIndex, activity, vulnerable }) {
  const results = {};
  for (const [virus, weeks] of Object.entries(byVirus)) {
    results[virus] = advise({ weeks, weekIndex, activity, vulnerable });
  }
  const strictestAt = (i) => {
    let worst = null;
    for (const [virus, weeks] of Object.entries(byVirus)) {
      const w = weeks[i];
      if (!w || !w.available) continue;
      const score = riskScore(w.probs, activity, vulnerable);
      const verdict = verdictForScore(score);
      if (!worst || verdictRank(verdict.id) > verdictRank(worst.verdict.id) || (verdict.id === worst.verdict.id && score > worst.score)) {
        worst = { virus, verdict, score, week: w };
      }
    }
    return worst;
  };
  const strictest = strictestAt(weekIndex);
  // A week only counts as better if every virus has a forecast for it:
  // an unknown is never better than a known risk.
  const coversAll = (i) => Object.values(byVirus).every((weeks) => weeks[i]?.available);
  let better = null;
  if (strictest) {
    const count = Math.max(...Object.values(byVirus).map((w) => w.length));
    for (let i = 0; i < count; i++) {
      if (i === weekIndex || !coversAll(i)) continue;
      const s = strictestAt(i);
      if (s && verdictRank(s.verdict.id) < verdictRank(strictest.verdict.id) && (!better || s.score < better.score)) {
        better = s;
      }
    }
  }
  return {
    results,
    strictest,
    tips: strictest ? tipsFor(strictest.verdict.id, activity, vulnerable) : [],
    better,
  };
}
