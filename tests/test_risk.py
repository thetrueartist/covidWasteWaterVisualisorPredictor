import math

import numpy as np
import pandas as pd
import pytest

from wastewater.risk import category, category_probabilities, quantile_cdf, trend


@pytest.mark.parametrize(
    "index,expected",
    [(0, "very_low"), (19.9, "very_low"), (20, "low"), (40, "moderate"), (59, "moderate"), (60, "high"), (80, "very_high"), (100, "very_high")],
)
def test_category_edges(index, expected):
    assert category(index) == expected


def test_category_missing():
    assert category(None) is None
    assert category(float("nan")) is None


def weekly(values):
    return pd.Series(values, index=pd.date_range("2024-01-07", periods=len(values), freq="W-SUN"))


@pytest.mark.parametrize(
    "weekly_change,label",
    [(1.5, "rising_fast"), (1.15, "rising"), (1.0, "steady"), (0.88, "falling"), (0.6, "falling_fast")],
)
def test_trend_labels(weekly_change, label):
    ys = weekly([0.0, math.log(weekly_change), 2 * math.log(weekly_change)])
    t = trend(ys)
    assert t["label"] == label
    assert t["pct_per_week"] == pytest.approx(100 * (weekly_change - 1), abs=0.1)


def test_trend_needs_recent_contiguous_data():
    assert trend(weekly([1.0, 2.0])) is None
    gappy = pd.Series([1.0, 2.0, 3.0], index=pd.to_datetime(["2024-01-07", "2024-02-04", "2024-02-11"]))
    assert trend(gappy) is None


def test_quantile_cdf_is_monotone_and_hits_the_quantiles():
    q = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    assert quantile_cdf(2.0, q) == pytest.approx(0.5)
    assert quantile_cdf(1.0, q) == pytest.approx(0.25)
    assert quantile_cdf(-10, q) == 0 and quantile_cdf(10, q) == 1
    xs = np.linspace(-3, 7, 50)
    cdf = [quantile_cdf(x, q) for x in xs]
    assert all(b >= a for a, b in zip(cdf, cdf[1:]))


def test_quantile_cdf_handles_tied_quantiles():
    q = np.array([1.0, 1.0, 1.0, 1.0, 1.0])
    assert 0 <= quantile_cdf(1.0, q) <= 1


def test_category_probabilities():
    edges = np.array([1.0, 2.0, 3.0, 4.0])
    probs = category_probabilities(np.array([2.2, 2.4, 2.5, 2.6, 2.8]), edges)
    assert sum(probs) == pytest.approx(1.0, abs=1e-3)
    assert probs[2] == pytest.approx(1.0)  # everything inside the moderate band
    wide = category_probabilities(np.array([0.0, 1.5, 2.5, 3.5, 5.0]), edges)
    assert all(p > 0 for p in wide)
