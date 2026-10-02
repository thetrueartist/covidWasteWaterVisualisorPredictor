"""End-to-end build with synthetic sources, no network."""

import json

import pandas as pd
import pytest

from tests.conftest import wave_series
from wastewater.build import build
from wastewater.model import HORIZONS
from wastewater.sources.base import RawSeries, Source, SourceInfo


def make_source(sid: str, hemisphere: str, stale_region: bool = False):
    class Fake(Source):
        info = SourceInfo(
            id=sid, name=sid.title(), flag="🏳️", hemisphere=hemisphere, publisher="Test lab",
            url="https://example.org", license="CC0", metric="Test metric", unit="gc/L",
        )

        def fetch(self, fetcher):
            series = [RawSeries(sid, sid.title(), "national", sid.title(), wave_series(seed=1, noise=0.05))]
            for i in range(3):
                series.append(RawSeries(f"r{i}", f"Region {i}", "region", "Regions", wave_series(seed=i + 2, phase=i / 3)))
            if stale_region:
                old = wave_series(weeks=100, seed=9)
                series.append(RawSeries("old", "Old site", "site", "Sites", old))
            return series

    return Fake()


class Broken(Source):
    info = SourceInfo(id="broken", name="Broken", flag="", hemisphere="N", publisher="", url="", license="", metric="", unit="")

    def fetch(self, fetcher):
        raise RuntimeError("portal down")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    sources = [make_source("alpha", "N", stale_region=True), make_source("beta", "S"), Broken()]
    index = build(out_dir=out, sources=sources, holdout_weeks=30, max_iter=15)
    return out, index


def test_index(built):
    out, index = built
    on_disk = json.loads((out / "index.json").read_text())
    assert on_disk == index
    assert [c["id"] for c in index["countries"]] == ["alpha", "beta"]
    assert index["errors"] == [{"country": "broken", "error": "RuntimeError: portal down"}]
    assert len(index["model"]["by_horizon"]) == len(HORIZONS)
    assert index["default_country"] == "alpha"  # scotland isn't in this build


def test_country_file(built):
    out, index = built
    data = json.loads((out / "alpha.json").read_text())
    ids = [r["id"] for r in data["regions"]]
    assert ids[0] == "alpha"  # national first
    assert "old" not in ids  # stopped reporting long ago
    region = data["regions"][1]
    n = len(region["smooth"])
    assert len(region["raw"]) == n and len(region["index"]) == n
    expected_last = pd.Timestamp(region["start"]) + pd.Timedelta(weeks=n - 1)
    assert region["latest"]["date"] == expected_last.date().isoformat()
    assert len(region["thresholds"]) == 4
    assert region["thresholds"] == sorted(region["thresholds"])
    fc = region["forecast"]
    assert [f["h"] for f in fc] == list(HORIZONS)
    for f in fc:
        assert f["q"] == sorted(f["q"])
        assert sum(f["probs"]) == pytest.approx(1, abs=0.01)
        assert 0 <= f["p_lower"] <= 1
        assert f["method"] in ("model", "no_change")
