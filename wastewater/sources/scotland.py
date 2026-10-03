"""Scotland: Public Health Scotland open data (Scottish Water / SEPA sampling).

Published weekly on opendata.nhs.scot as part of the "Viral Respiratory
Diseases (Including Influenza and COVID-19) Data in Scotland" dataset:

* national: daily seven-day average of RNA per person per day
* by NHS Health Board, by council area and by treatment works: weekly means

Values are million gene copies per person per day (Mgc/p/d), normalised for
flow and population.

PHS publishes no flu or RSV wastewater data, so those come from laboratory
surveillance in the same dataset and are labelled as such on the site:
national weekly test positivity, and confirmed cases per 100,000 people per
week for each health board.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Signal, Source, SourceInfo, read_csv, slugify, unique_ids

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
    "positivity": "573f3110-1693-4e09-816c-a0a74685c8ce",
    "cases_by_board": "212412ba-cff2-43b9-bd40-f8d80688d8bf",
}

# Our virus id -> PHS "Pathogen" value in the laboratory datasets.
LAB_PATHOGENS = {"flu": "Influenza (All)", "rsv": "RSV"}
SCOTLAND_CODE = "S92000003"

VALUE_COL = "Average(Mgc)"


def _date(col: pd.Series) -> pd.Series:
    return pd.to_datetime(col.astype(str).str.slice(0, 8), format="%Y%m%d", errors="coerce")


def parse_national(text: str) -> list[RawSeries]:
    df = read_csv(text, ["WastewaterRNA", "SevenDayEnding"])
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
    df = read_csv(text, ["WeekEnding", VALUE_COL, name_col, code_col])
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


def parse_positivity(text: str) -> list[RawSeries]:
    """National weekly test positivity (%) for flu and RSV."""
    df = read_csv(text, ["WeekEnding", "PositivityPercentage", "Pathogen"])
    df["date"] = _date(df["WeekEnding"])
    df["value"] = pd.to_numeric(df["PositivityPercentage"], errors="coerce")
    df = df.dropna(subset=["date", "value"])
    out = []
    for pathogen, label in LAB_PATHOGENS.items():
        g = df[df["Pathogen"] == label]
        if not g.empty:
            out.append(
                RawSeries(
                    "scotland", "Scotland", "national", "Scotland",
                    pd.Series(g["value"].to_numpy(), index=g["date"]),
                    pathogen=pathogen, unit="% positive",
                )
            )
    return out


def parse_cases_by_board(text: str) -> list[RawSeries]:
    """Weekly confirmed flu and RSV cases per 100,000 people, per health board."""
    df = read_csv(text, ["HBcode", "HBName", "WeekEnding", "RateCasesPerWeek", "Pathogen", "Population"])
    df = df[df["HBcode"] != SCOTLAND_CODE]
    df["date"] = _date(df["WeekEnding"])
    df["value"] = pd.to_numeric(df["RateCasesPerWeek"], errors="coerce")
    df = df.dropna(subset=["date", "value"])
    out = []
    for pathogen, label in LAB_PATHOGENS.items():
        for code, g in df[df["Pathogen"] == label].groupby("HBcode", sort=True):
            out.append(
                RawSeries(
                    f"hb-{slugify(code)}", str(g["HBName"].iloc[0]), "region", "Health boards",
                    pd.Series(g["value"].to_numpy(), index=g["date"]),
                    population=g["Population"].iloc[-1] if "Population" in g else None,
                    pathogen=pathogen, unit="cases/100k",
                )
            )
    return out


_LAB_NOTES = (
    "Public Health Scotland doesn't publish {virus} wastewater data, so this uses laboratory "
    "surveillance: Scotland-wide test positivity, and confirmed cases per 100,000 people for "
    "each health board. Lab data depends on who gets tested and lags wastewater by a few days."
)


def _phs_url(url) -> str | None:
    """A download link from the CKAN API, if it really points at PHS over HTTPS."""
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.netloc == "www.opendata.nhs.scot" else None


class Scotland(Source):
    info = SourceInfo(
        id="scotland",
        name="Scotland",
        flag="🏴󠁧󠁢󠁳󠁣󠁴󠁿",
        hemisphere="N",
        publisher="Public Health Scotland (sampling by Scottish Water and SEPA)",
        url="https://www.opendata.nhs.scot/dataset/viral-respiratory-diseases-including-influenza-and-covid-19-data-in-scotland",
        license="Open Government Licence v3.0",
        signals={
            "covid": Signal(
                "SARS-CoV-2 RNA in wastewater, normalised for flow and population",
                "Mgc/p/d",
                "Million gene copies per person per day. The national series is a 7-day average; "
                "health board, council area and treatment-works series are weekly means.",
            ),
            "flu": Signal(
                "Influenza laboratory surveillance (test positivity and confirmed cases)",
                "% positive",
                _LAB_NOTES.format(virus="flu"),
                kind="lab tests",
            ),
            "rsv": Signal(
                "RSV laboratory surveillance (test positivity and confirmed cases)",
                "% positive",
                _LAB_NOTES.format(virus="RSV"),
                kind="lab tests",
            ),
        },
    )

    def _resource_urls(self, fetcher: Fetcher) -> dict[str, str]:
        urls: dict[str, str] = {}
        try:
            pkg = fetcher.get_json(CKAN_API, params={"id": PACKAGE_ID})["result"]
            urls = {r["id"]: r["url"] for r in pkg["resources"]}
        except Exception as exc:  # fall back to the datastore dump endpoints
            log.warning("could not resolve PHS resource URLs (%s); using datastore dumps", exc)
        return {k: _phs_url(urls.get(rid)) or DATASTORE_DUMP.format(rid) for k, rid in RESOURCES.items()}

    def parts(self):
        return [(("covid",), self.fetch_wastewater), (("flu", "rsv"), self.fetch_lab_tests)]

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        return self.fetch_wastewater(fetcher) + self.fetch_lab_tests(fetcher)

    def fetch_wastewater(self, fetcher: Fetcher) -> list[RawSeries]:
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

    def fetch_lab_tests(self, fetcher: Fetcher) -> list[RawSeries]:
        urls = self._resource_urls(fetcher)
        series = parse_positivity(fetcher.get_text(urls["positivity"]))
        series += parse_cases_by_board(fetcher.get_text(urls["cases_by_board"]))
        return series
