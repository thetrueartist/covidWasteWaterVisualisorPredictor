"""Turn published measurements into regular weekly, log-scale series.

Every source ends up on the same footing:

* one value per week (weeks end on Sunday), the mean of that week's samples;
* ``y``: log(value + offset), with gaps of up to three weeks interpolated;
* ``ys``: a causal exponential smooth of ``y`` (only past data, no peeking),
  which is what the site shows as "the level" and what the model forecasts;
* ``index``: 0-100, where the current smoothed level ranks among the previous
  two years of that same series. Units differ wildly between countries, so
  each area is compared with its own history, like CDC's activity levels.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .sources.base import RawSeries, week_ending

MAX_GAP_WEEKS = 3
SMOOTH_ALPHA = 0.5
WINDOW_WEEKS = 104
MIN_WINDOW = 26
OFFSET_FRACTION = 0.02


@dataclass
class Prepared:
    country: str
    hemisphere: str
    raw: RawSeries
    weekly: pd.Series
    offset: float
    y: pd.Series
    ys: pd.Series
    index: pd.Series

    @property
    def key(self) -> str:
        return f"{self.country}/{self.raw.region_id}"

    @property
    def last_date(self) -> pd.Timestamp | None:
        return self.ys.last_valid_index()

    def to_units(self, log_values):
        """Map log-scale values back to the source's units."""
        return np.exp(log_values) - self.offset


def to_weekly(values: pd.Series) -> pd.Series:
    """Mean per Monday-Sunday week on a gap-free weekly index (NaN = no data)."""
    if values.empty:
        return pd.Series(dtype=float)
    weeks = week_ending(values.index).to_numpy()
    weekly = values.groupby(weeks).mean()
    full = pd.date_range(weekly.index.min(), weekly.index.max(), freq="W-SUN")
    return weekly.reindex(full)


def fill_short_gaps(y: pd.Series, max_gap: int = MAX_GAP_WEEKS) -> pd.Series:
    """Linearly interpolate runs of at most ``max_gap`` missing weeks."""
    isna = y.isna().to_numpy()
    if not isna.any():
        return y
    interp = y.interpolate(limit_area="inside")
    out = y.copy()
    run_id = np.cumsum(~isna)
    for rid in np.unique(run_id[isna]):
        idx = np.flatnonzero(isna & (run_id == rid))
        if len(idx) <= max_gap:
            out.iloc[idx] = interp.iloc[idx]
    return out


def causal_smooth(y: pd.Series, alpha: float = SMOOTH_ALPHA) -> pd.Series:
    """Exponential smoothing that restarts after each gap."""
    vals = y.to_numpy(dtype=float)
    out = np.full_like(vals, np.nan)
    prev = np.nan
    for i, v in enumerate(vals):
        if np.isnan(v):
            prev = np.nan
            continue
        prev = v if np.isnan(prev) else alpha * v + (1 - alpha) * prev
        out[i] = prev
    return pd.Series(out, index=y.index)


def percentile_rank(value: float, window: np.ndarray) -> float:
    """Mid-rank percentile (0-100) of ``value`` within ``window``."""
    window = window[~np.isnan(window)]
    if window.size == 0 or np.isnan(value):
        return np.nan
    below = np.count_nonzero(window < value)
    equal = np.count_nonzero(window == value)
    return 100.0 * (below + 0.5 * equal) / window.size


def rolling_index(ys: pd.Series, window: int = WINDOW_WEEKS, min_periods: int = MIN_WINDOW) -> pd.Series:
    vals = ys.to_numpy(dtype=float)
    out = np.full_like(vals, np.nan)
    for i, v in enumerate(vals):
        if np.isnan(v):
            continue
        w = vals[max(0, i - window + 1) : i + 1]
        if np.count_nonzero(~np.isnan(w)) >= min_periods:
            out[i] = percentile_rank(v, w)
    return pd.Series(out, index=ys.index)


def log_offset(weekly: pd.Series) -> float:
    positive = weekly[weekly > 0]
    return float(OFFSET_FRACTION * positive.median()) if len(positive) else 1.0


def prepare(raw: RawSeries, country: str, hemisphere: str) -> Prepared | None:
    weekly = to_weekly(raw.values)
    if weekly.notna().sum() < 8:
        return None
    offset = log_offset(weekly)
    y = fill_short_gaps(np.log(weekly + offset))
    ys = causal_smooth(y)
    return Prepared(
        country=country,
        hemisphere=hemisphere,
        raw=raw,
        weekly=weekly,
        offset=offset,
        y=y,
        ys=ys,
        index=rolling_index(ys),
    )
