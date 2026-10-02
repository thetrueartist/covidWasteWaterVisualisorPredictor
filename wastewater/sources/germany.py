"""Germany: Robert Koch Institute AMELAG wastewater surveillance.

Published on GitHub (robert-koch-institut/Abwassersurveillance_AMELAG):

* ``amelag_aggregierte_kurve.tsv``: national population-weighted curve
* ``amelag_einzelstandorte.tsv``: per treatment plant, with state (Land)

State curves are built here as population-weighted weekly means of plants.
"""

from __future__ import annotations

import io

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Source, SourceInfo, slugify, unique_ids, weighted_weekly_mean

BASE = "https://raw.githubusercontent.com/robert-koch-institut/Abwassersurveillance_AMELAG/main/"
AGG_URL = BASE + "amelag_aggregierte_kurve.tsv"
SITES_URL = BASE + "amelag_einzelstandorte.tsv"
TYPE = "SARS-CoV-2"

LAENDER = {
    "BW": "Baden-Württemberg",
    "BY": "Bavaria (Bayern)",
    "BE": "Berlin",
    "BB": "Brandenburg",
    "HB": "Bremen",
    "HH": "Hamburg",
    "HE": "Hesse (Hessen)",
    "MV": "Mecklenburg-Vorpommern",
    "NI": "Lower Saxony (Niedersachsen)",
    "NW": "North Rhine-Westphalia",
    "RP": "Rhineland-Palatinate",
    "SL": "Saarland",
    "SN": "Saxony (Sachsen)",
    "ST": "Saxony-Anhalt",
    "SH": "Schleswig-Holstein",
    "TH": "Thuringia (Thüringen)",
}


def _value_column(df: pd.DataFrame) -> pd.Series:
    """Prefer the flow-normalised load; older files only have the raw load."""
    col = "viruslast_normalisiert" if "viruslast_normalisiert" in df else "viruslast"
    return pd.to_numeric(df[col], errors="coerce")


def parse_national(text: str) -> RawSeries:
    df = pd.read_csv(io.StringIO(text), sep="\t")
    df = df[df["typ"] == TYPE]
    values = pd.Series(_value_column(df).to_numpy(), index=pd.to_datetime(df["datum"]))
    return RawSeries("germany", "Germany", "national", "Germany", values)


def parse_sites(text: str) -> list[RawSeries]:
    df = pd.read_csv(
        io.StringIO(text),
        sep="\t",
        usecols=["standort", "bundesland", "datum", "viruslast", "viruslast_normalisiert", "einwohner", "typ"],
    )
    df = df[df["typ"] == TYPE].copy()
    df["value"] = _value_column(df)
    df["einwohner"] = pd.to_numeric(df["einwohner"], errors="coerce")
    df = df.dropna(subset=["value"])

    out: list[RawSeries] = []
    for land, g in df.groupby("bundesland", sort=True):
        weekly = weighted_weekly_mean(g, "datum", "value", "einwohner", "standort")
        name = LAENDER.get(land, land)
        out.append(
            RawSeries(
                f"land-{land.lower()}", name, "region", "States (Länder)", weekly,
                population=float(g.groupby("standort")["einwohner"].first().sum()),
            )
        )
    for site, g in df.groupby("standort", sort=True):
        land = g["bundesland"].iloc[0]
        out.append(
            RawSeries(
                f"site-{slugify(site)}",
                str(site),
                "site",
                "Treatment plants",
                pd.Series(g["value"].to_numpy(), index=pd.to_datetime(g["datum"])),
                population=float(g["einwohner"].iloc[-1]) if g["einwohner"].notna().any() else None,
                parent=LAENDER.get(land, land),
            )
        )
    return unique_ids(out)


class Germany(Source):
    info = SourceInfo(
        id="germany",
        name="Germany",
        flag="🇩🇪",
        hemisphere="N",
        publisher="Robert Koch Institute — AMELAG wastewater surveillance",
        url="https://github.com/robert-koch-institut/Abwassersurveillance_AMELAG",
        license="CC BY 4.0",
        metric="SARS-CoV-2 viral load in wastewater (flow-normalised where available)",
        unit="gc/L",
        notes="National curve is RKI's population-weighted aggregate. State curves are "
        "population-weighted weekly means of the state's treatment plants.",
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        return [parse_national(fetcher.get_text(AGG_URL)), *parse_sites(fetcher.get_text(SITES_URL))]
