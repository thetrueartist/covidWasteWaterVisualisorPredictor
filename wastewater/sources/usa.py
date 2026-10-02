"""United States: CDC National Wastewater Surveillance System (NWSS).

Uses the "CDC Wastewater Viral Activity Level for SARS-CoV-2, Influenza A and
RSV" dataset (data.cdc.gov ``atcp-73re``), which gives a site-level Wastewater
Viral Activity Level (WVAL) per week. WVAL is already normalised against each
site's own baseline, so sites are comparable. We aggregate with the median
across reporting sites, nationally and per state, via the Socrata API.
"""

from __future__ import annotations

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Source, SourceInfo, slugify

API = "https://data.cdc.gov/resource/atcp-73re.json"
WHERE = "pathogen_target='SARS-CoV-2' AND site_wval IS NOT NULL"
PAGE = 50_000


def _query(fetcher: Fetcher, select: str, group: str) -> pd.DataFrame:
    rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "$select": select,
            "$where": WHERE,
            "$group": group,
            "$order": group,
            "$limit": PAGE,
            "$offset": offset,
        }
        page = fetcher.get_json(API, params=params)
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    return pd.DataFrame(rows)


def parse_national(df: pd.DataFrame) -> RawSeries:
    return RawSeries(
        region_id="us",
        name="United States",
        level="national",
        group="United States",
        values=pd.Series(pd.to_numeric(df["wval"]).to_numpy(), index=pd.to_datetime(df["week_end"])),
    )


def parse_states(df: pd.DataFrame) -> list[RawSeries]:
    out = []
    for state, g in df.groupby("state_territory", sort=True):
        out.append(
            RawSeries(
                region_id=slugify(state),
                name=str(state),
                level="region",
                group="States & territories",
                values=pd.Series(pd.to_numeric(g["wval"]).to_numpy(), index=pd.to_datetime(g["week_end"])),
            )
        )
    return out


class UnitedStates(Source):
    info = SourceInfo(
        id="usa",
        name="United States",
        flag="🇺🇸",
        hemisphere="N",
        publisher="CDC National Wastewater Surveillance System (NWSS)",
        url="https://data.cdc.gov/Public-Health-Surveillance/CDC-Wastewater-Viral-Activity-Level-for-SARS-CoV-2/atcp-73re",
        license="Public domain (US Government work)",
        metric="Wastewater Viral Activity Level (WVAL), median across sites",
        unit="WVAL",
        notes="WVAL measures how far each site is above its own baseline. National and state "
        "values here are the median across reporting sites that week.",
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        nat = _query(
            fetcher,
            "week_end, median(site_wval) AS wval, count(site) AS n_sites",
            "week_end",
        )
        states = _query(
            fetcher,
            "state_territory, week_end, median(site_wval) AS wval, count(site) AS n_sites",
            "state_territory, week_end",
        )
        return [parse_national(nat), *parse_states(states)]
