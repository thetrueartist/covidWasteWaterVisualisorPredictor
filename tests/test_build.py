"""End-to-end build with synthetic sources, no network."""

import json

import pandas as pd
import pytest

from tests.conftest import wave_series
from wastewater.build import build
from wastewater.model import HORIZONS
from wastewater.sources.base import RawSeries, Signal, Source, SourceInfo


def make_source(sid: str, hemisphere: str, stale_region: bool = False, flu: bool = False):
    signals = {"covid": Signal("Test metric", "gc/L", "Test notes")}
    if flu:
        signals["flu"] = Signal("Flu tests", "% positive", "Lab data", kind="lab tests")

    class Fake(Source):
        info = SourceInfo(
            id=sid, name=sid.title(), flag="🏳️", hemisphere=hemisphere, publisher="Test lab",
            url="https://example.org", license="CC0", signals=signals,
        )

        def fetch(self, fetcher):
            series = [RawSeries(sid, sid.title(), "national", sid.title(), wave_series(seed=1, noise=0.05))]
            for i in range(3):
                series.append(RawSeries(f"r{i}", f"Region {i}", "region", "Regions", wave_series(seed=i + 2, phase=i / 3)))
            if stale_region:
                old = wave_series(weeks=100, seed=9)
                series.append(RawSeries("old", "Old site", "site", "Sites", old))
            if flu:
                series.append(RawSeries(sid, sid.title(), "national", sid.title(), wave_series(seed=5, phase=2), pathogen="flu", unit="% positive"))
                series.append(RawSeries("r0", "Region 0", "region", "Regions", wave_series(seed=6, phase=2.1), pathogen="flu"))
            # A virus the source doesn't declare is ignored rather than published unlabelled.
            series.append(RawSeries(sid, sid.title(), "national", sid.title(), wave_series(seed=7), pathogen="rsv"))
            return series

    return Fake()


class Broken(Source):
    info = SourceInfo(id="broken", name="Broken", flag="", hemisphere="N", publisher="", url="", license="")

    def fetch(self, fetcher):
        raise RuntimeError("portal down")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    sources = [make_source("alpha", "N", stale_region=True, flu=True), make_source("beta", "S"), Broken()]
    index = build(out_dir=out, sources=sources, holdout_weeks=30, max_iter=15)
    return out, index


def test_index(built):
    out, index = built
    on_disk = json.loads((out / "index.json").read_text())
    assert on_disk == index
    assert [c["id"] for c in index["countries"]] == ["alpha", "beta"]
    # The public JSON names the failure type only, never the message text.
    assert index["errors"] == [{"country": "broken", "error": "couldn't load the data (RuntimeError)"}]
    assert [v["id"] for v in index["viruses"]] == ["covid", "flu"]  # rsv was never declared
    assert set(index["models"]) == {"covid", "flu"}
    for model in index["models"].values():
        assert len(model["by_horizon"]) == len(HORIZONS)
        assert model["design"] and model["features"]
    assert index["default_country"] == "alpha"  # scotland isn't in this build
    alpha, beta = index["countries"]
    assert set(alpha["viruses"]) == {"covid", "flu"} and set(beta["viruses"]) == {"covid"}
    assert alpha["viruses"]["flu"]["signal"]["kind"] == "lab tests"
    assert alpha["viruses"]["covid"]["file"] == "alpha-covid.json"


def test_country_file(built):
    out, index = built
    data = json.loads((out / "alpha-covid.json").read_text())
    assert data["virus"] == "covid"
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


def test_flu_file_keeps_ids_and_units(built):
    out, _ = built
    flu = json.loads((out / "alpha-flu.json").read_text())
    ids = {r["id"]: r for r in flu["regions"]}
    assert set(ids) == {"alpha", "r0"}  # same ids as the COVID areas, so the site can match them
    assert ids["alpha"]["unit"] == "% positive"
    assert ids["r0"]["unit"] is None  # falls back to the signal's unit
    assert not (out / "alpha-rsv.json").exists()


class Poisoned(Source):
    """Hostile or glitchy upstream data: none of it may leak into other countries."""

    info = SourceInfo(
        id="poisoned", name="Poisoned", flag="", hemisphere="N", publisher="", url="", license="",
        signals={"covid": Signal("Test metric", "gc/L")},
    )

    def fetch(self, fetcher):
        nat = wave_series(seed=11).astype(object)
        nat.iloc[-3] = float("inf")
        nat.iloc[-4] = "n/a"
        nat.iloc[-5] = -50
        nat[pd.Timestamp("2099-12-27")] = 1e6  # a typo'd year
        nat[pd.Timestamp("1999-01-03")] = 1e6
        region = RawSeries("r0", "Region 0", "region", "Regions", wave_series(seed=12), population="lots")
        return [RawSeries("poisoned", "Poisoned", "national", "Poisoned", nat), region]


def test_bad_upstream_values_stay_contained(tmp_path):
    index = build(out_dir=tmp_path, sources=[make_source("alpha", "N"), Poisoned()], holdout_weeks=30, max_iter=15)
    assert [c["id"] for c in index["countries"]] == ["alpha", "poisoned"]
    assert index["errors"] == []
    model = index["models"]["covid"]
    assert len(model["by_horizon"]) == len(HORIZONS)
    assert model["holdout_start"] < "2025-01-01"  # not pushed out by the 2099 row
    data = json.loads((tmp_path / "poisoned-covid.json").read_text())
    nat = data["regions"][0]
    assert nat["latest"]["date"] < "2030-01-01" and nat["start"] > "2000-01-01"
    assert all(v is None or 0 <= v < 1e5 for v in nat["raw"])


class FromPortal(Source):
    info = SourceInfo(
        id="portal", name="Portal", flag="", hemisphere="N", publisher="", url="", license="",
        signals={"covid": Signal("Test metric", "gc/L")},
    )
    URL = "https://data.rivm.nl/test.json"

    def fetch(self, fetcher):
        values = json.loads(fetcher.get_text(self.URL))  # raises on an HTML error page
        s = pd.Series(values, index=pd.date_range("2021-01-03", periods=len(values), freq="W-SUN"))
        return [RawSeries("portal", "Portal", "national", "Portal", s)]


def test_a_garbage_download_falls_back_to_the_last_good_copy(tmp_path, monkeypatch):
    from wastewater.build import collect
    from wastewater.http import Fetcher

    class Resp:
        def __init__(self, body):
            self.body, self.url, self.status_code, self.is_redirect, self.headers = body, FromPortal.URL, 200, False, {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield self.body

    good = json.dumps(list(wave_series(seed=3).round(3))).encode()
    for body in (good, b"<html>Down for maintenance</html>"):
        fetcher = Fetcher(cache_dir=tmp_path / "cache", max_age_hours=0, retries=1)
        monkeypatch.setattr(fetcher._session, "get", lambda url, body=body, **kw: Resp(body))
        prepared, errors = collect([FromPortal()], fetcher)
        assert errors == [] and len(prepared) == 1  # second time round: served from the last good copy
    # and the good copy is still there for next time
    assert len(list((tmp_path / "cache").iterdir())) == 1


def test_only_one_build_at_a_time(tmp_path):
    from wastewater.cli import only_one_build

    with only_one_build(tmp_path) as first:
        with only_one_build(tmp_path) as second:
            assert first and not second
    with only_one_build(tmp_path) as again:
        assert again
