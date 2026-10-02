"""New Zealand: PHF Science (formerly ESR) wastewater surveillance programme.

Published on GitHub (ESR-NZ/covid_in_wastewater) as weekly national,
regional and per-site SARS-CoV-2 genome copies per person per day.
"""

from __future__ import annotations

import io
import re

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Source, SourceInfo, slugify, unique_ids

BASE = "https://raw.githubusercontent.com/ESR-NZ/covid_in_wastewater/main/data/"

# Two-letter prefixes used in SampleLocation codes, e.g. "AU_Rosedale".
REGION_CODES = {
    "AU": "Auckland",
    "BP": "Bay of Plenty",
    "CA": "Canterbury",
    "GS": "Gisborne",
    "HB": "Hawke's Bay",
    "MB": "Marlborough",
    "MW": "Manawatu-Whanganui",
    "NL": "Northland",
    "NS": "Nelson",
    "OT": "Otago",
    "SL": "Southland",
    "TK": "Taranaki",
    "TS": "Tasman",
    "WC": "West Coast",
    "WG": "Wellington",
    "WK": "Waikato",
}


def _value_col(df: pd.DataFrame) -> str:
    return next(c for c in df.columns if c.startswith("copies"))


def _site_name(code: str) -> str:
    _, _, rest = code.partition("_")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", rest or code)


def parse_national(text: str) -> RawSeries:
    df = pd.read_csv(io.StringIO(text))
    return RawSeries(
        "new-zealand", "New Zealand", "national", "New Zealand",
        pd.Series(df[_value_col(df)].to_numpy(), index=pd.to_datetime(df["week_end_date"])),
    )


def parse_regions(text: str) -> list[RawSeries]:
    df = pd.read_csv(io.StringIO(text))
    col = _value_col(df)
    return [
        RawSeries(
            slugify(region), str(region), "region", "Regions",
            pd.Series(g[col].to_numpy(), index=pd.to_datetime(g["week_end_date"])),
        )
        for region, g in df.groupby("Region", sort=True)
    ]


def parse_sites(text: str, sites_text: str | None = None) -> list[RawSeries]:
    df = pd.read_csv(io.StringIO(text))
    col = _value_col(df)
    meta = {}
    if sites_text:
        sites = pd.read_csv(io.StringIO(sites_text))
        meta = sites.set_index("SampleLocation").to_dict("index")
    out = []
    for code, g in df.groupby("SampleLocation", sort=True):
        m = meta.get(code, {})
        region = m.get("Region") or REGION_CODES.get(str(code)[:2])
        out.append(
            RawSeries(
                f"site-{slugify(code)}",
                m.get("DisplayName") or _site_name(str(code)),
                "site",
                "Treatment plants",
                pd.Series(g[col].to_numpy(), index=pd.to_datetime(g["week_end_date"])),
                population=m.get("Population"),
                parent=region,
            )
        )
    return unique_ids(out)


class NewZealand(Source):
    info = SourceInfo(
        id="new-zealand",
        name="New Zealand",
        flag="🇳🇿",
        hemisphere="S",
        publisher="PHF Science (formerly ESR) — Wastewater surveillance programme",
        url="https://github.com/ESR-NZ/covid_in_wastewater",
        license="CC BY 4.0",
        metric="SARS-CoV-2 genome copies per person per day",
        unit="copies/person/day",
        notes="Regional and national values are PHF Science's population-weighted aggregates.",
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        try:
            sites_meta = fetcher.get_text(BASE + "sites.csv")
        except Exception:
            sites_meta = None
        return [
            parse_national(fetcher.get_text(BASE + "ww_national.csv")),
            *parse_regions(fetcher.get_text(BASE + "ww_regional.csv")),
            *parse_sites(fetcher.get_text(BASE + "ww_site.csv"), sites_meta),
        ]
