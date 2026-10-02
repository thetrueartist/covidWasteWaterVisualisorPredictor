"""Canada: Public Health Agency of Canada National Wastewater Monitoring of Pathogens.

``wastewater_aggregate.csv`` holds weekly population-weighted averages for
Canada, each province/territory, and individual cities, plus per-site rows.
We use the aggregate rows (no ``site``) for SARS-CoV-2 (N2 gene, ``covN2``),
influenza (``fluA`` + ``fluB``) and RSV (``rsv``).
"""

from __future__ import annotations

import io

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Signal, Source, SourceInfo, slugify, unique_ids

URL = "https://health-infobase.canada.ca/src/data/wastewater/wastewater_aggregate.csv"

# Our virus id -> PHAC measure ids, summed when there are several.
MEASURES = {
    "covid": ("covN2",),
    "flu": ("fluA", "fluB"),
    "rsv": ("rsv",),
}


def _series_for(df: pd.DataFrame, pathogen: str, provinces: set) -> list[RawSeries]:
    rows = df[df["measureid"].isin(MEASURES[pathogen])]
    # Flu is reported as A and B separately; a week's value is their sum.
    weekly = rows.groupby(["Location", "date"], as_index=False).agg(
        value=("value", lambda v: v.sum(min_count=1)),
        province=("province", "first"),
    )
    weekly = weekly.dropna(subset=["value"])
    out = []
    for loc, g in weekly.groupby("Location", sort=True):
        values = pd.Series(g["value"].to_numpy(), index=g["date"])
        if loc == "Canada":
            out.append(RawSeries("canada", "Canada", "national", "Canada", values, pathogen=pathogen))
        elif loc in provinces:
            out.append(RawSeries(slugify(loc), loc, "region", "Provinces & territories", values, pathogen=pathogen))
        else:
            prov = g["province"].dropna()
            parent = prov.iloc[0] if len(prov) else None
            out.append(
                RawSeries(f"city-{slugify(loc)}", loc, "local", "Cities", values, parent=parent, pathogen=pathogen)
            )
    return unique_ids(out)


def parse(text: str, pathogens=tuple(MEASURES)) -> list[RawSeries]:
    df = pd.read_csv(io.StringIO(text))
    df = df[df["site"].isna()].copy()
    # weekstart is the Sunday that starts the epi week; label by its Saturday end.
    df["date"] = pd.to_datetime(df["weekstart"], errors="coerce") + pd.Timedelta(days=6)
    df["value"] = pd.to_numeric(df["w_avg"], errors="coerce")
    df = df.dropna(subset=["date", "value"])
    provinces = set(df["province"].dropna())
    out: list[RawSeries] = []
    for pathogen in pathogens:
        out.extend(_series_for(df, pathogen, provinces))
    return out


_NOTES = "National, provincial and city values are PHAC's own population-weighted aggregates."


class Canada(Source):
    info = SourceInfo(
        id="canada",
        name="Canada",
        flag="🇨🇦",
        hemisphere="N",
        publisher="Public Health Agency of Canada — National Wastewater Monitoring of Pathogens",
        url="https://health-infobase.canada.ca/wastewater/",
        license="Open Government Licence – Canada",
        signals={
            "covid": Signal("SARS-CoV-2 (N2 gene) viral load, population-weighted weekly average", "copies/mL", _NOTES),
            "flu": Signal("Influenza A + B viral load, population-weighted weekly average", "copies/mL", _NOTES),
            "rsv": Signal("RSV viral load, population-weighted weekly average", "copies/mL", _NOTES),
        },
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        return parse(fetcher.get_text(URL))
