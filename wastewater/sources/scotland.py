"""Scotland: Public Health Scotland open data (Scottish Water / SEPA sampling).

Published weekly on opendata.nhs.scot as part of the "Viral Respiratory
Diseases (Including Influenza and COVID-19) Data in Scotland" dataset:

* national: daily seven-day average of RNA per person per day
* by NHS Health Board, by council area and by treatment works: weekly means

Values are million gene copies per person per day (Mgc/p/d), normalised for
flow and population.
"""

from __future__ import annotations

import io
import logging

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Source, SourceInfo, slugify, unique_ids

log = logging.getLogger(__name__)

CKAN_API = "https://www.opendata.nhs.scot/api/3/action/package_show"
PACKAGE_ID = "49dc2d88-1cb0-4420-a5ea-d1bbada62fb2"
DATASTORE_DUMP = "https://www.opendata.nhs.scot/datastore/dump/{}?format=csv"

# Resource ids are stable; their download URLs change every week.
RESOURCES = {
    "national": "f5b105ec-17e7-4f36-ab6e-935030651562",
    "health_board": "fc178db3-873b-46f1-bd4a-4333e0c1bbdd",
    "council_area": "c2664d58-7d10-4733-83ba-1fd66ea520d9",
    "treatment_works": "d53421a7-7467-425f-a7f8-22fa7e81add6",
}

VALUE_COL = "Average(Mgc)"


def _date(col: pd.Series) -> pd.Series:
    return pd.to_datetime(col.astype(str).str.slice(0, 8), format="%Y%m%d", errors="coerce")


def parse_national(text: str) -> list[RawSeries]:
    df = pd.read_csv(io.StringIO(text))
    values = pd.Series(
        pd.to_numeric(df["WastewaterRNA"], errors="coerce").to_numpy(),
        index=_date(df["SevenDayEnding"]),
    )
    return [RawSeries("scotland", "Scotland", "national", "Scotland", values[values.index.notna()])]


def parse_weekly(
    text: str,
    *,
    level: str,
    group: str,
    id_prefix: str,
    name_col: str,
    code_col: str | None = None,
    tidy_name=lambda s: s,
) -> list[RawSeries]:
    df = pd.read_csv(io.StringIO(text))
    df["date"] = _date(df["WeekEnding"])
    df["value"] = pd.to_numeric(df[VALUE_COL], errors="coerce")
    df = df.dropna(subset=["date", "value"])
    out = []
    key = code_col or name_col
    for code, g in df.groupby(key, sort=True):
        name = str(g[name_col].iloc[0])
        out.append(
            RawSeries(
                region_id=f"{id_prefix}-{slugify(code)}",
                name=tidy_name(name),
                level=level,
                group=group,
                values=pd.Series(g["value"].to_numpy(), index=g["date"]),
            )
        )
    return unique_ids(out)


class Scotland(Source):
    info = SourceInfo(
        id="scotland",
        name="Scotland",
        flag="🏴󠁧󠁢󠁳󠁣󠁴󠁿",
        hemisphere="N",
        publisher="Public Health Scotland (sampling by Scottish Water and SEPA)",
        url="https://www.opendata.nhs.scot/dataset/viral-respiratory-diseases-including-influenza-and-covid-19-data-in-scotland",
        license="Open Government Licence v3.0",
        metric="SARS-CoV-2 RNA in wastewater, normalised for flow and population",
        unit="Mgc/p/d",
        notes="Million gene copies per person per day. The national series is a 7-day average; "
        "health board, council area and treatment-works series are weekly means.",
    )

    def _resource_urls(self, fetcher: Fetcher) -> dict[str, str]:
        urls: dict[str, str] = {}
        try:
            pkg = fetcher.get_json(CKAN_API, params={"id": PACKAGE_ID})["result"]
            urls = {r["id"]: r["url"] for r in pkg["resources"]}
        except Exception as exc:  # fall back to the datastore dump endpoints
            log.warning("could not resolve PHS resource URLs (%s); using datastore dumps", exc)
        return {k: urls.get(rid) or DATASTORE_DUMP.format(rid) for k, rid in RESOURCES.items()}

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        urls = self._resource_urls(fetcher)
        series = parse_national(fetcher.get_text(urls["national"]))
        series += parse_weekly(
            fetcher.get_text(urls["health_board"]),
            level="region",
            group="Health boards",
            id_prefix="hb",
            name_col="HBName",
            code_col="HBcode",
        )
        series += parse_weekly(
            fetcher.get_text(urls["council_area"]),
            level="local",
            group="Council areas",
            id_prefix="la",
            name_col="LAName",
            code_col="LAcode",
        )
        series += parse_weekly(
            fetcher.get_text(urls["treatment_works"]),
            level="site",
            group="Treatment works",
            id_prefix="wwtw",
            name_col="WastewaterTreatmentWork",
        )
        return series
