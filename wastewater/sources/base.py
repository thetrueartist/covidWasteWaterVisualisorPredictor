"""Common types for wastewater data sources."""

from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

from ..http import Fetcher

# Geographic granularity, coarsest first. The site groups regions by these.
LEVELS = ("national", "region", "local", "site")


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    flag: str
    hemisphere: str  # "N" or "S"; flips seasonality features
    publisher: str
    url: str
    license: str
    metric: str
    unit: str
    notes: str = ""


@dataclass
class RawSeries:
    """One area's measurements as published, before any resampling.

    ``values`` is indexed by measurement date. Zeros are kept (they usually
    mean "below detection"); missing measurements are simply absent.
    """

    region_id: str
    name: str
    level: str
    group: str
    values: pd.Series
    population: float | None = None
    parent: str | None = None
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"unknown level {self.level!r}")
        s = pd.to_numeric(self.values, errors="coerce")
        s.index = pd.DatetimeIndex(pd.to_datetime(s.index)).normalize()
        s = s[s.notna() & (s >= 0)]
        self.values = s.groupby(level=0).mean().sort_index().astype(float)


class Source(ABC):
    info: SourceInfo

    @abstractmethod
    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        """Download and parse every area this source publishes."""


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def week_ending(dates: pd.Series | pd.DatetimeIndex) -> pd.Series:
    """Map dates to the Sunday that ends their (Monday-Sunday) week."""
    d = pd.Series(pd.to_datetime(dates))
    return (d + pd.to_timedelta((6 - d.dt.dayofweek) % 7, unit="D")).dt.normalize()


def weighted_weekly_mean(
    frame: pd.DataFrame, date_col: str, value_col: str, weight_col: str, site_col: str
) -> pd.Series:
    """Population-weighted mean across sites, per week (weeks end on Sunday).

    Each site first contributes one weekly mean so that sites sampled several
    times a week do not get extra weight.
    """
    df = frame[[date_col, value_col, weight_col, site_col]].dropna()
    df = df[df[weight_col] > 0]
    if df.empty:
        return pd.Series(dtype=float)
    df = df.assign(week=week_ending(df[date_col]).to_numpy())
    per_site = df.groupby(["week", site_col]).agg(v=(value_col, "mean"), w=(weight_col, "first"))
    per_site["vw"] = per_site["v"] * per_site["w"]
    weekly = per_site.groupby(level="week")[["vw", "w"]].sum()
    return weekly["vw"] / weekly["w"]


def unique_ids(series: Iterable[RawSeries]) -> list[RawSeries]:
    """Make region ids unique within a source by suffixing duplicates."""
    seen: dict[str, int] = {}
    out = []
    for s in series:
        n = seen.get(s.region_id, 0)
        seen[s.region_id] = n + 1
        if n:
            s.region_id = f"{s.region_id}-{n + 1}"
        out.append(s)
    return out
