"""Netherlands: RIVM national sewage surveillance (rioolwatersurveillance).

* ``COVID-19_rioolwaterdata_landelijk.csv``: national daily average
* ``COVID-19_rioolwaterdata.csv``: per sewage treatment plant (RWZI)

Values are virus particles per 100,000 inhabitants per day.
"""

from __future__ import annotations

import io

import pandas as pd

from ..http import Fetcher
from .base import RawSeries, Signal, Source, SourceInfo, slugify, unique_ids

BASE = "https://data.rivm.nl/covid-19/"
NATIONAL_URL = BASE + "COVID-19_rioolwaterdata_landelijk.csv"
SITES_URL = BASE + "COVID-19_rioolwaterdata.csv"


def _read(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text), sep=";")
    df["date"] = pd.to_datetime(df["Date_measurement"], errors="coerce")
    df["value"] = pd.to_numeric(df["RNA_flow_per_100000"], errors="coerce")
    return df.dropna(subset=["date", "value"])


def parse_national(text: str) -> RawSeries:
    df = _read(text)
    return RawSeries(
        "netherlands", "Netherlands", "national", "Netherlands",
        pd.Series(df["value"].to_numpy(), index=df["date"]),
    )


def parse_sites(text: str) -> list[RawSeries]:
    df = _read(text)
    out = []
    for code, g in df.groupby("RWZI_AWZI_code", sort=True):
        name = str(g["RWZI_AWZI_name"].iloc[-1])
        out.append(
            RawSeries(
                f"rwzi-{slugify(code)}", name, "site", "Treatment plants",
                pd.Series(g["value"].to_numpy(), index=g["date"]),
            )
        )
    return unique_ids(out)


class Netherlands(Source):
    info = SourceInfo(
        id="netherlands",
        name="Netherlands",
        flag="🇳🇱",
        hemisphere="N",
        publisher="RIVM — National Institute for Public Health and the Environment",
        url="https://data.rivm.nl/meta/srv/eng/catalog.search#/metadata/a2960b68-9d3f-4dc3-9485-600570cd52b9",
        license="CC0 1.0",
        signals={
            "covid": Signal(
                'SARS-CoV-2 RNA flow per 100,000 inhabitants per day',
                'particles/100k/day',
                "National series is RIVM's daily national average; treatment-plant series are as measured.",
            ),
        },
    )

    def fetch(self, fetcher: Fetcher) -> list[RawSeries]:
        return [parse_national(fetcher.get_text(NATIONAL_URL)), *parse_sites(fetcher.get_text(SITES_URL))]
