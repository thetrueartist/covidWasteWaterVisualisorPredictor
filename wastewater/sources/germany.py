"""Germany: Robert Koch Institute AMELAG wastewater surveillance.

Published on GitHub (robert-koch-institut/Abwassersurveillance_AMELAG):

* ``amelag_aggregierte_kurve.tsv``: national population-weighted curve
* ``amelag_einzelstandorte.tsv``: per treatment plant, with state (Land)

State curves are built here as population-weighted weekly means of plants.

RSV caveat: in 2026 RKI moved from separate RSV A / RSV B assays (and their
"RSV A+B" sum) to a combined "RSV A/B" assay. The two differ by a factor of
0.2-1.5 where they overlap, so they are not spliced: the national RSV series
uses "RSV A/B" only, each plant uses one assay, and there are no state RSV
curves because plants in one state may use different assays.
"""

from __future__ import annotations

import io

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Signal, Source, SourceInfo, slugify, unique_ids, weighted_weekly_mean

BASE = "https://raw.githubusercontent.com/robert-koch-institut/Abwassersurveillance_AMELAG/main/"
AGG_URL = BASE + "amelag_aggregierte_kurve.tsv"
SITES_URL = BASE + "amelag_einzelstandorte.tsv"

# Our virus id -> RKI "typ" values a plant may use, in order of preference.
TYPES = {
    "covid": ("SARS-CoV-2",),
    "flu": ("Influenza A+B",),
    "rsv": ("RSV A/B", "RSV A+B"),
}
NATIONAL_TYPE = {"covid": "SARS-CoV-2", "flu": "Influenza A+B", "rsv": "RSV A/B"}
STATE_CURVES = ("covid", "flu")

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


def parse_national(text: str, pathogens=tuple(TYPES)) -> list[RawSeries]:
    df = pd.read_csv(io.StringIO(text), sep="\t")
    out = []
    for pathogen in pathogens:
        sub = df[df["typ"] == NATIONAL_TYPE[pathogen]]
        if sub.empty:
            continue
        values = pd.Series(_value_column(sub).to_numpy(), index=pd.to_datetime(sub["datum"], errors="coerce"))
        out.append(RawSeries("germany", "Germany", "national", "Germany", values, pathogen=pathogen))
    return out


def _pick_assay(g: pd.DataFrame, preferred: tuple) -> pd.DataFrame:
    """One assay per plant: whichever allowed assay has the most recent data."""
    best = None
    for typ in preferred:
        sub = g[g["typ"] == typ]
        if not sub.empty and (best is None or sub["datum"].max() > best["datum"].max()):
            best = sub
    return best if best is not None else g.iloc[0:0]


def parse_sites(text: str, pathogens=tuple(TYPES)) -> list[RawSeries]:
    df = pd.read_csv(
        io.StringIO(text),
        sep="\t",
        usecols=["standort", "bundesland", "datum", "viruslast", "viruslast_normalisiert", "einwohner", "typ"],
    )
    df["value"] = _value_column(df)
    df["einwohner"] = pd.to_numeric(df["einwohner"], errors="coerce")
    df = df.dropna(subset=["value"])

    out: list[RawSeries] = []
    for pathogen in pathogens:
        sub = df[df["typ"].isin(TYPES[pathogen])]
        if sub.empty:
            continue
        per_site = pd.concat([_pick_assay(g, TYPES[pathogen]) for _, g in sub.groupby("standort", sort=True)])
        if pathogen in STATE_CURVES:
            for land, g in per_site.groupby("bundesland", sort=True):
                weekly = weighted_weekly_mean(g, "datum", "value", "einwohner", "standort")
                out.append(
                    RawSeries(
                        f"land-{slugify(land) or 'unknown'}", LAENDER.get(land, str(land)), "region", "States (Länder)", weekly,
                        population=float(g.groupby("standort")["einwohner"].first().sum()),
                        pathogen=pathogen,
                    )
                )
        for site, g in per_site.groupby("standort", sort=True):
            land = g["bundesland"].iloc[0]
            out.append(
                RawSeries(
                    f"site-{slugify(site)}",
                    str(site),
                    "site",
                    "Treatment plants",
                    pd.Series(g["value"].to_numpy(), index=pd.to_datetime(g["datum"], errors="coerce")),
                    population=float(g["einwohner"].iloc[-1]) if g["einwohner"].notna().any() else None,
                    parent=LAENDER.get(land, str(land)),
                    pathogen=pathogen,
                )
            )
    return unique_ids(out)


_NOTES = (
    "National curve is RKI's population-weighted aggregate. State curves are "
    "population-weighted weekly means of the state's treatment plants."
)


class Germany(Source):
    info = SourceInfo(
        id="germany",
        name="Germany",
        flag="🇩🇪",
        hemisphere="N",
        publisher="Robert Koch Institute — AMELAG wastewater surveillance",
        url="https://github.com/robert-koch-institut/Abwassersurveillance_AMELAG",
        license="CC BY 4.0",
        signals={
            "covid": Signal("SARS-CoV-2 viral load in wastewater (flow-normalised where available)", "gc/L", _NOTES),
            "flu": Signal("Influenza A + B viral load in wastewater (flow-normalised where available)", "gc/L", _NOTES),
            "rsv": Signal(
                "RSV viral load in wastewater (flow-normalised where available)",
                "gc/L",
                "National curve is RKI's aggregate for the combined RSV A/B assay (from August 2024). "
                "RKI changed RSV assays in 2026, so there are no state curves for RSV.",
            ),
        },
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        return [*parse_national(fetcher.get_text(AGG_URL)), *parse_sites(fetcher.get_text(SITES_URL))]
