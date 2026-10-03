"""Common types for wastewater data sources."""

from __future__ import annotations

import io
import math
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from ..http import Fetcher

# Geographic granularity, coarsest first. The site groups regions by these.
LEVELS = ("national", "region", "local", "site")

# Wastewater surveillance started in 2020. Earlier dates, and dates more than a
# week ahead, can only be typos or tampering (2062 for 2026, say). Left in,
# one such row would shift the back-test window for every country.
EARLIEST = pd.Timestamp("2020-01-01")
MAX_DAYS_AHEAD = 7
# Far above any real reading (the largest, Dutch RNA flow per 100,000 people,
# is about 3e15) but small enough that sums and means can't overflow.
MAX_VALUE = 1e20

# Viruses, in the order the site offers them. Each is modelled separately.
PATHOGENS = {
    "covid": "COVID-19",
    "flu": "Flu",
    "rsv": "RSV",
}


@dataclass(frozen=True)
class Signal:
    """What one source publishes for one virus."""

    metric: str
    unit: str
    notes: str = ""
    kind: str = "wastewater"  # or "lab tests" where no wastewater data is published


@dataclass(frozen=True)
class SourceInfo:
    id: str
    name: str
    flag: str
    hemisphere: str  # "N" or "S"; flips seasonality features
    publisher: str
    url: str
    license: str
    signals: dict = field(default_factory=dict)  # pathogen -> Signal


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
    pathogen: str = "covid"
    unit: str | None = None  # overrides the source's unit for this one series
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in LEVELS:
            raise ValueError(f"unknown level {self.level!r}")
        if self.pathogen not in PATHOGENS:
            raise ValueError(f"unknown pathogen {self.pathogen!r}")
        s = pd.to_numeric(self.values, errors="coerce").astype(float)
        dates = pd.DatetimeIndex(pd.to_datetime(s.index, errors="coerce"))
        if dates.tz is not None:
            dates = dates.tz_convert(None)
        s.index = dates.normalize()
        latest = pd.Timestamp.now().normalize() + pd.Timedelta(days=MAX_DAYS_AHEAD)
        v = s.to_numpy()
        ok = np.isfinite(v) & (v >= 0) & (v <= MAX_VALUE) & (s.index >= EARLIEST) & (s.index <= latest)
        merged = s[ok].groupby(level=0).mean().sort_index()
        self.values = merged[np.isfinite(merged.to_numpy())]
        self.population = _positive_or_none(self.population)


def _positive_or_none(value) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


class Source(ABC):
    info: SourceInfo

    @abstractmethod
    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        """Download and parse every area this source publishes."""

    def parts(self) -> list[tuple[tuple[str, ...], Callable[[Fetcher], list[RawSeries]]]]:
        """Pieces of ``fetch`` that can fail on their own, with the viruses each covers.

        A source that publishes each virus in separate files overrides this,
        so a broken flu file can't take its COVID data down too.
        """
        return [(tuple(self.info.signals), self.fetch)]


def read_csv(text: str, columns, **kwargs) -> pd.DataFrame:
    """Parse CSV text, keeping only the columns a parser uses.

    ``columns`` is a collection of names or a predicate. A file padded with
    thousands of extra columns then can't blow up memory.
    """
    keep = columns if callable(columns) else (lambda c, wanted=frozenset(columns): c in wanted)
    return pd.read_csv(io.StringIO(text), usecols=keep, **kwargs)


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
    df = frame[[date_col, value_col, weight_col, site_col]].copy()
    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna()
    df = df[np.isfinite(df[value_col]) & np.isfinite(df[weight_col]) & (df[weight_col] > 0)]
    if df.empty:
        return pd.Series(dtype=float)
    df = df.assign(week=week_ending(df[date_col]).to_numpy())
    per_site = df.groupby(["week", site_col]).agg(v=(value_col, "mean"), w=(weight_col, "first"))
    per_site["vw"] = per_site["v"] * per_site["w"]
    weekly = per_site.groupby(level="week")[["vw", "w"]].sum()
    return weekly["vw"] / weekly["w"]


def unique_ids(series: Iterable[RawSeries]) -> list[RawSeries]:
    """Make region ids unique within a source and virus by suffixing duplicates.

    The same place keeps the same id across viruses, so the site can match an
    area between COVID, flu and RSV.
    """
    used: set[tuple[str, str]] = set()
    out = []
    for s in series:
        base = s.region_id or "area"
        rid, n = base, 1
        while (s.pathogen, rid) in used:  # also skips ids that a suffix would collide with
            n += 1
            rid = f"{base}-{n}"
        used.add((s.pathogen, rid))
        s.region_id = rid
        out.append(s)
    return out
