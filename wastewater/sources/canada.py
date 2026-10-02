"""Canada: Public Health Agency of Canada National Wastewater Monitoring of Pathogens.

``wastewater_aggregate.csv`` holds weekly population-weighted averages for
Canada, each province/territory, and individual cities, plus per-site rows.
We use the SARS-CoV-2 N2 measure (``covN2``) aggregate rows (no ``site``).
"""

from __future__ import annotations

import io

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Source, SourceInfo, slugify, unique_ids

URL = "https://health-infobase.canada.ca/src/data/wastewater/wastewater_aggregate.csv"


def parse(text: str) -> list[RawSeries]:
    df = pd.read_csv(io.StringIO(text))
    df = df[(df["measureid"] == "covN2") & df["site"].isna()].copy()
    # weekstart is the Sunday that starts the epi week; label by its Saturday end.
    df["date"] = pd.to_datetime(df["weekstart"], errors="coerce") + pd.Timedelta(days=6)
    df["value"] = pd.to_numeric(df["w_avg"], errors="coerce")
    df = df.dropna(subset=["date", "value"])
    provinces = set(df["province"].dropna())

    out = []
    for loc, g in df.groupby("Location", sort=True):
        values = pd.Series(g["value"].to_numpy(), index=g["date"])
        if loc == "Canada":
            out.append(RawSeries("canada", "Canada", "national", "Canada", values))
        elif loc in provinces:
            out.append(RawSeries(slugify(loc), loc, "region", "Provinces & territories", values))
        else:
            prov = g["province"].dropna()
            parent = prov.iloc[0] if len(prov) else None
            out.append(
                RawSeries(f"city-{slugify(loc)}", loc, "local", "Cities", values, parent=parent)
            )
    return unique_ids(out)


class Canada(Source):
    info = SourceInfo(
        id="canada",
        name="Canada",
        flag="🇨🇦",
        hemisphere="N",
        publisher="Public Health Agency of Canada — National Wastewater Monitoring of Pathogens",
        url="https://health-infobase.canada.ca/wastewater/",
        license="Open Government Licence – Canada",
        metric="SARS-CoV-2 (N2 gene) viral load, population-weighted weekly average",
        unit="copies/mL",
        notes="National, provincial and city values are PHAC's own population-weighted aggregates.",
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        return parse(fetcher.get_text(URL))
