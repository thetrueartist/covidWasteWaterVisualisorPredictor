import numpy as np
import pandas as pd
import pytest

from wastewater.preprocess import (
    causal_smooth,
    fill_short_gaps,
    percentile_rank,
    prepare,
    rolling_index,
    to_weekly,
)
from wastewater.sources.base import RawSeries


def test_to_weekly_averages_and_keeps_gaps():
    s = pd.Series(
        [1.0, 3.0, 10.0],
        index=pd.to_datetime(["2024-01-01", "2024-01-03", "2024-01-22"]),  # Mon, Wed, Mon
    )
    weekly = to_weekly(s)
    assert list(weekly.index.strftime("%Y-%m-%d")) == ["2024-01-07", "2024-01-14", "2024-01-21", "2024-01-28"]
    assert weekly.iloc[0] == 2.0
    assert weekly.iloc[1:3].isna().all()
    assert weekly.iloc[3] == 10.0


def test_fill_short_gaps_only_fills_short_runs():
    y = pd.Series([1.0, np.nan, np.nan, 4.0, np.nan, np.nan, np.nan, np.nan, 9.0])
    out = fill_short_gaps(y, max_gap=3)
    assert out.iloc[1] == pytest.approx(2.0) and out.iloc[2] == pytest.approx(3.0)
    assert out.iloc[4:8].isna().all()


def test_causal_smooth_never_looks_ahead_and_restarts_after_gaps():
    y = pd.Series([0.0, 2.0, np.nan, 10.0, 10.0])
    s = causal_smooth(y, alpha=0.5)
    assert s.iloc[1] == pytest.approx(1.0)
    assert np.isnan(s.iloc[2])
    assert s.iloc[3] == 10.0  # restarted
    changed = y.copy()
    changed.iloc[4] = -100
    assert causal_smooth(changed, alpha=0.5).iloc[:4].equals(s.iloc[:4])


def test_percentile_rank_and_rolling_index():
    window = np.array([1.0, 2.0, 3.0, 4.0])
    assert percentile_rank(5.0, window) == 100
    assert percentile_rank(0.0, window) == 0
    assert percentile_rank(2.0, window) == pytest.approx(37.5)
    ys = pd.Series(np.arange(60, dtype=float))
    idx = rolling_index(ys, window=52, min_periods=26)
    assert idx.iloc[:25].isna().all()
    assert idx.iloc[-1] == pytest.approx(100 * 51.5 / 52)


def test_prepare(make_raw):
    p = prepare(make_raw(weeks=120), "testland", "N")
    assert p is not None
    assert p.offset > 0
    assert p.ys.notna().sum() == 120
    assert p.index.dropna().between(0, 100).all()
    # units round-trip
    assert p.to_units(np.log(p.weekly.iloc[5] + p.offset)) == pytest.approx(p.weekly.iloc[5])


def test_prepare_rejects_tiny_series():
    s = pd.Series([1.0, 2.0], index=pd.to_datetime(["2024-01-01", "2024-01-08"]))
    assert prepare(RawSeries("x", "X", "site", "S", s), "c", "N") is None
