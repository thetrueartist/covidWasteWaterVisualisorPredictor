"""United States: CDC National Wastewater Surveillance System (NWSS).

Uses the "CDC Wastewater Viral Activity Level for SARS-CoV-2, Influenza A and
RSV" dataset (data.cdc.gov ``atcp-73re``), which gives a site-level Wastewater
Viral Activity Level (WVAL) per week for each virus. WVAL is already
normalised against each site's own baseline, so sites are comparable. We
aggregate with the median across reporting sites, nationally and per state,
via the Socrata API.
"""

from __future__ import annotations

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Signal, Source, SourceInfo, slugify, unique_ids

API = "https://data.cdc.gov/resource/atcp-73re.json"
PAGE = 50_000
# Each query fits in one page today. The cap stops a misbehaving API from
# paging forever.
MAX_PAGES = 20

# Our virus id -> CDC's pathogen_target value.
TARGETS = {
    "covid": "SARS-CoV-2",
    "flu": "Influenza A virus",
    "rsv": "RSV",
}


def _query(fetcher: Fetcher, target: str, select: str, group: str) -> pd.DataFrame:
    rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "$select": select,
            "$where": f"pathogen_target='{target}' AND site_wval IS NOT NULL",
            "$group": group,
            "$order": group,
            "$limit": PAGE,
            "$offset": offset,
        }
        page = fetcher.get_json(API, params=params)
        if not isinstance(page, list) or not all(isinstance(r, dict) for r in page):
            raise ValueError("unexpected response shape from the CDC API")
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
        if offset >= PAGE * MAX_PAGES:
            raise ValueError(f"CDC API returned more than {MAX_PAGES} pages")
    return pd.DataFrame(rows)


def parse_national(df: pd.DataFrame, pathogen: str = "covid") -> RawSeries:
    return RawSeries(
        region_id="us",
        name="United States",
        level="national",
        group="United States",
        values=pd.Series(df["wval"].to_numpy(), index=df["week_end"].to_numpy()),
        pathogen=pathogen,
    )


def parse_states(df: pd.DataFrame, pathogen: str = "covid") -> list[RawSeries]:
    out = []
    for state, g in df.groupby("state_territory", sort=True):
        out.append(
            RawSeries(
                region_id=slugify(state) or "state",
                name=str(state),
                level="region",
                group="States & territories",
                values=pd.Series(g["wval"].to_numpy(), index=g["week_end"].to_numpy()),
                pathogen=pathogen,
            )
        )
    return unique_ids(out)


_NOTES = (
    "WVAL measures how far each site is above its own baseline. National and state "
    "values here are the median across reporting sites that week."
)


class UnitedStates(Source):
    info = SourceInfo(
        id="usa",
        name="United States",
        flag="🇺🇸",
        hemisphere="N",
        publisher="CDC National Wastewater Surveillance System (NWSS)",
        url="https://data.cdc.gov/Public-Health-Surveillance/CDC-Wastewater-Viral-Activity-Level-for-SARS-CoV-2/atcp-73re",
        license="Public domain (US Government work)",
        signals={
            "covid": Signal("Wastewater Viral Activity Level (WVAL) for SARS-CoV-2, median across sites", "WVAL", _NOTES),
            "flu": Signal("Wastewater Viral Activity Level (WVAL) for influenza A, median across sites", "WVAL", _NOTES),
            "rsv": Signal("Wastewater Viral Activity Level (WVAL) for RSV, median across sites", "WVAL", _NOTES),
        },
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        series: list[RawSeries] = []
        for pathogen, target in TARGETS.items():
            nat = _query(fetcher, target, "week_end, median(site_wval) AS wval, count(site) AS n_sites", "week_end")
            states = _query(
                fetcher,
                target,
                "state_territory, week_end, median(site_wval) AS wval, count(site) AS n_sites",
                "state_territory, week_end",
            )
            if not nat.empty:
                series.append(parse_national(nat, pathogen))
            if not states.empty:
                series.extend(parse_states(states, pathogen))
        return series
