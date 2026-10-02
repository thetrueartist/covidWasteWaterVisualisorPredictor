import numpy as np
import pandas as pd
import pytest

from wastewater.sources.base import RawSeries


def wave_series(
    weeks: int = 200,
    start: str = "2021-01-03",
    period: float = 26.0,
    phase: float = 0.0,
    noise: float = 0.15,
    seed: int = 0,
    scale: float = 100.0,
) -> pd.Series:
    """Weekly synthetic wastewater curve: regular waves with multiplicative noise."""
    rng = np.random.default_rng(seed)
    t = np.arange(weeks)
    log_level = 1.5 * np.sin(2 * np.pi * t / period + phase)
    values = scale * np.exp(log_level + rng.normal(0, noise, weeks))
    return pd.Series(values, index=pd.date_range(start, periods=weeks, freq="W-SUN"))


@pytest.fixture
def make_raw():
    def _make(region_id="r1", level="region", group="Regions", **kw):
        return RawSeries(region_id, region_id.upper(), level, group, wave_series(**kw))

    return _make
