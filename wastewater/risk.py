"""Activity levels, trends and forecast probabilities.

Levels follow the spirit of CDC's Wastewater Viral Activity Levels: an area's
current smoothed level is ranked against its own previous two years.

=========  ==================================================
Very low   lower than 80% of weeks in the past two years
Low        20th-40th percentile
Moderate   40th-60th percentile
High       60th-80th percentile
Very high  higher than 80% of weeks in the past two years
=========  ==================================================
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .model import QUANTILES

CATEGORIES = [
    {"id": "very_low", "label": "Very low", "min": 0},
    {"id": "low", "label": "Low", "min": 20},
    {"id": "moderate", "label": "Moderate", "min": 40},
    {"id": "high", "label": "High", "min": 60},
    {"id": "very_high", "label": "Very high", "min": 80},
]
CATEGORY_IDS = [c["id"] for c in CATEGORIES]
EDGES = (20, 40, 60, 80)

# Weekly change in the smoothed level that counts as rising / falling.
TREND_STEADY = math.log(1.10)
TREND_FAST = math.log(1.30)


def category(index: float | None) -> str | None:
    if index is None or (isinstance(index, float) and math.isnan(index)):
        return None
    return CATEGORY_IDS[int(np.searchsorted(EDGES, index, side="right"))]


def trend(ys: pd.Series) -> dict | None:
    """Average weekly change of the smoothed level over the last two weeks."""
    s = ys.dropna()
    if len(s) < 3 or (s.index[-1] - s.index[-3]).days > 21:
        return None
    slope = (s.iloc[-1] - s.iloc[-3]) / 2
    if slope >= TREND_FAST:
        label = "rising_fast"
    elif slope >= TREND_STEADY:
        label = "rising"
    elif slope <= -TREND_FAST:
        label = "falling_fast"
    elif slope <= -TREND_STEADY:
        label = "falling"
    else:
        label = "steady"
    return {"pct_per_week": round(100 * (math.exp(slope) - 1), 1), "label": label}


def quantile_cdf(x: float, qvals: np.ndarray) -> float:
    """P(value <= x) from forecast quantiles, linear in between, linear tails."""
    q = np.asarray(qvals, dtype=float)
    levels = np.asarray(QUANTILES, dtype=float)
    lo_tail = q[0] - max(q[1] - q[0], 1e-6)
    hi_tail = q[-1] + max(q[-1] - q[-2], 1e-6)
    xs = np.concatenate([[lo_tail], q, [hi_tail]])
    ps = np.concatenate([[0.0], levels, [1.0]])
    # Ties between quantiles would make interp ambiguous; nudge them apart.
    xs = xs + np.arange(len(xs)) * 1e-9
    return float(np.interp(x, xs, ps))


def category_probabilities(qvals: np.ndarray, edges: np.ndarray) -> list[float]:
    """Probability of landing in each level band, given band edges (same scale)."""
    cdf = [0.0, *[quantile_cdf(e, qvals) for e in edges], 1.0]
    probs = np.clip(np.diff(cdf), 0, 1)
    probs = probs / probs.sum() if probs.sum() > 0 else probs
    return [round(float(p), 3) for p in probs]
