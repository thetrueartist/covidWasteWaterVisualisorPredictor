"""The forecast archive: saving issues once, never replacing them, and reading them back safely."""

import gzip
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from tests.archive_helpers import T0, area, header_for, lg, put, raw_issue, tail
from tests.conftest import wave_series
from wastewater import archive, scoring
from wastewater import build as build_mod
from wastewater.build import area_tail, build
from wastewater.model import HORIZONS
from wastewater.preprocess import prepare
from wastewater.sources.base import RawSeries, Signal, Source, SourceInfo


def files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.jsonl.gz"))


def lines_of(path):
    return [json.loads(x) for x in gzip.decompress(path.read_bytes()).decode().splitlines()]


# ---------- writing ----------
def test_an_issue_is_saved_with_its_header_and_area_lines(tmp_path):
    header = {"commit": "a" * 40, "design": "trend", "inputs": [{"url": "https://x", "sha256": "0" * 64}]}
    path = put(tmp_path, T0, [area("r2"), area("r1")], header=header)
    assert path == tmp_path / "v2/covid/alpha/2026-10-06T051700Z.jsonl.gz"
    head, *areas = lines_of(path)
    assert head["type"] == "header" and head["schema"] == 2
    assert (head["virus"], head["country"], head["issued"]) == ("covid", "alpha", "2026-10-06T05:17:00Z")
    assert head["commit"] == "a" * 40 and head["design"] == "trend" and head["inputs"][0]["url"] == "https://x"
    assert [a["region"] for a in areas] == ["r1", "r2"]  # stable order, by region id
    canon = "\n".join(json.dumps(a, sort_keys=True, separators=(",", ":")) for a in areas)
    assert head["content_hash"] == hashlib.sha256(canon.encode()).hexdigest()
    # gzip without a timestamp: the same issue always has the same bytes
    again = put(tmp_path / "other", T0, [area("r1"), area("r2")], header=header)
    assert again.read_bytes() == path.read_bytes()


def test_unchanged_forecasts_are_not_saved_again(tmp_path):
    put(tmp_path, T0, [area()])
    assert archive.write_issue(tmp_path, "covid", "alpha", {"commit": "b" * 40}, [area()], now=T0 + timedelta(days=1)) is None
    put(tmp_path, T0 + timedelta(days=2), [area(latest=16.0)])
    # back to the first version: different from the newest saved one, so it is saved
    put(tmp_path, T0 + timedelta(days=3), [area()])
    assert len(files(tmp_path)) == 3


def test_dedupe_reads_only_the_newest_readable_header(tmp_path):
    first = put(tmp_path, T0, [area()])
    # a newer file that can't be read doesn't count as the newest version
    (first.parent / "2026-10-07T000000Z.jsonl.gz").write_bytes(b"not gzip")
    assert archive.write_issue(tmp_path, "covid", "alpha", {}, [area()], now=T0 + timedelta(days=2)) is None
    assert archive.newest_header(tmp_path, "covid", "alpha")["issued"] == "2026-10-06T05:17:00Z"


def test_a_saved_file_is_never_replaced(tmp_path):
    path = put(tmp_path, T0, [area()])
    before = path.read_bytes()
    assert archive.write_issue(tmp_path, "covid", "alpha", {}, [area(latest=99.0)], now=T0) is None
    assert path.read_bytes() == before
    # nor written through a planted symlink
    os.symlink(tmp_path / "elsewhere", path.parent / "2026-10-08T051700Z.jsonl.gz")
    assert archive.write_issue(tmp_path, "covid", "alpha", {}, [area(latest=98.0)], now=T0 + timedelta(days=2)) is None
    assert not (tmp_path / "elsewhere").exists()


def test_new_files_are_also_copied_to_archive_new(tmp_path):
    root, new = tmp_path / "archive", tmp_path / "new"
    put(root, T0, [area()])
    assert archive.write_issue(root, "covid", "alpha", {}, [area()], now=T0 + timedelta(days=1), new_root=new) is None
    path = put(root, T0 + timedelta(days=1), [area(latest=17.0)], new_root=new)
    assert files(new) == ["v2/covid/alpha/2026-10-07T051700Z.jsonl.gz"]
    assert (new / files(new)[0]).read_bytes() == path.read_bytes()
    assert os.stat(path).st_nlink == 1  # a copy, not a hard link the archive job would refuse


def test_only_areas_with_forecasts_and_recent_data_are_saved(tmp_path):
    no_forecast = area("r9") | {"f": []}
    stale = area("old", as_of="2026-08-30")  # 37 days before the issue: the archive job would refuse it
    ahead = area("ahead", as_of="2026-10-18")  # 12 days after it: so would this
    path = put(tmp_path, T0, [area("r1"), no_forecast, stale, ahead, area("week", as_of="2026-10-11")])
    assert [a["region"] for a in lines_of(path)[1:]] == ["r1", "week"]  # 10-11 ends the issue's own week
    assert archive.write_issue(tmp_path / "x", "covid", "alpha", {}, [no_forecast, stale, ahead], now=T0) is None


def test_issues_too_large_for_the_archive_job_are_not_written(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(archive, "MAX_AREAS", 2)
    with caplog.at_level(logging.ERROR, logger="wastewater.archive"):
        assert archive.write_issue(tmp_path, "covid", "alpha", {}, [area("a"), area("b"), area("c")], now=T0) is None
    assert "3 areas" in caplog.text
    monkeypatch.setattr(archive, "MAX_AREAS", 1000)
    monkeypatch.setattr(archive, "MAX_GZ_BYTES", 200)
    assert archive.write_issue(tmp_path, "covid", "alpha", {}, [area("a"), area("b")], now=T0) is None
    monkeypatch.setattr(archive, "MAX_GZ_BYTES", 1_000_000)
    monkeypatch.setattr(archive, "MAX_FILE_BYTES", 1000)
    assert archive.write_issue(tmp_path, "covid", "alpha", {}, [area("a"), area("b")], now=T0) is None
    assert not list(tmp_path.rglob("*.gz"))


def test_the_limits_fit_a_real_issue_many_times_over():
    # The biggest real issue: the Netherlands, 136 areas, about 250 kB (40 kB gzipped).
    assert archive.MAX_AREAS >= 5 * 136 and archive.MAX_FILE_BYTES >= 10 * 250_000 and archive.MAX_GZ_BYTES >= 10 * 40_000


@pytest.mark.parametrize("virus,country", [("ebola", "alpha"), ("covid", "../etc"), ("covid", "Alpha"), ("covid", "a" * 41), ("covid", "")])
def test_unexpected_names_are_not_written(tmp_path, virus, country):
    assert archive.write_issue(tmp_path, virus, country, {}, [area()], now=T0) is None
    assert not list(tmp_path.rglob("*"))


def test_lines_that_fail_validation_are_left_out(tmp_path):
    bad = area("r2")
    bad["f"][0]["probs"] = [0.9, 0.9, 0, 0, 0]
    path = put(tmp_path, T0, [area("r1"), bad])
    assert [a["region"] for a in lines_of(path)[1:]] == ["r1"]


# ---------- the code fingerprint ----------
def _git(tmp_path, head, **files_):
    git = tmp_path / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head)
    for rel, text in files_.items():
        p = git / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_commit_comes_from_github_sha_then_git_head(tmp_path, monkeypatch):
    sha, other = "1" * 40, "2" * 40
    monkeypatch.setenv("GITHUB_SHA", sha)
    assert archive.git_commit(tmp_path) == sha
    monkeypatch.setenv("GITHUB_SHA", "not-a-sha")
    assert archive.git_commit(tmp_path) is None  # and no .git either
    monkeypatch.delenv("GITHUB_SHA")
    assert archive.git_commit(_git(tmp_path / "a", "ref: refs/heads/main\n", **{"refs/heads/main": other + "\n"})) == other
    assert archive.git_commit(_git(tmp_path / "b", other)) == other  # detached HEAD
    packed = f"# pack-refs with: peeled\n{sha} refs/heads/feature/x\n"
    assert archive.git_commit(_git(tmp_path / "c", "ref: refs/heads/feature/x", **{"packed-refs": packed})) == sha
    assert archive.git_commit(_git(tmp_path / "d", "ref: refs/heads/../../../../etc/passwd")) is None
    assert archive.git_commit(_git(tmp_path / "e", "garbage")) is None


def test_commit_of_a_git_worktree(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    common = tmp_path / "main/.git"
    wt = common / "worktrees/w"
    wt.mkdir(parents=True)
    (wt / "HEAD").write_text("ref: refs/heads/feature\n")
    (wt / "commondir").write_text("../..\n")
    (common / "refs/heads").mkdir(parents=True)
    (common / "refs/heads/feature").write_text("3" * 40 + "\n")
    repo = tmp_path / "w"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {wt}\n")
    assert archive.git_commit(repo) == "3" * 40


def test_code_fingerprint_hashes_requirements_and_advisor(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    (tmp_path / "site/js").mkdir(parents=True)
    (tmp_path / "requirements.txt").write_text("numpy==1\n")
    (tmp_path / "site/js/advisor.js").write_text("export {}\n")
    fp = archive.code_fingerprint(tmp_path)
    assert fp == {"commit": None, "requirements_sha256": hashlib.sha256(b"numpy==1\n").hexdigest(),
                  "advisor_sha256": hashlib.sha256(b"export {}\n").hexdigest()}
    assert archive.code_fingerprint(tmp_path / "nothing") == {"commit": None, "requirements_sha256": None, "advisor_sha256": None}


# ---------- reading ----------
def test_reader_skips_odd_files_and_folders(tmp_path, monkeypatch):
    good = put(tmp_path, T0, [area()])
    folder = good.parent
    (folder / "notes.txt").write_text("hello")
    (folder / "2026-10-07T000000Z.jsonl.gz").write_bytes(b"not gzip")
    (folder / "2026-13-40T000000Z.jsonl.gz").write_bytes(good.read_bytes())  # not a real date
    os.symlink(good, folder / "2026-10-08T000000Z.jsonl.gz")
    os.mkfifo(folder / "2026-10-09T000000Z.jsonl.gz")
    (folder / "2026-10-10T000000Z.jsonl.gz").mkdir()
    # a whole folder that's a symlink, and folders with names that can't be a virus or country
    os.symlink(folder, tmp_path / "v2/covid/beta")
    for odd in ("v2/ebola/alpha", "v2/covid/Bad Name", "v2/covid/.hidden"):
        (tmp_path / odd).mkdir(parents=True)
        (tmp_path / odd / good.name).write_bytes(good.read_bytes())
    # too big once decompressed
    monkeypatch.setattr(archive, "MAX_FILE_BYTES", 2000)
    big = area("big")
    big["name"] = "x" * 150
    raw_issue(folder / "2026-10-11T000000Z.jsonl.gz", header_for("covid", "alpha", T0.replace(day=11, hour=0, minute=0)),
              [big] * 20)
    got = [(i.virus, i.country, i.issued, [a["region"] for a in lines]) for i, lines in archive.iter_issues(tmp_path)]
    assert got == [("covid", "alpha", "2026-10-06T05:17:00Z", ["r1"])]


@pytest.mark.parametrize(
    "change",
    [
        lambda h: h | {"schema": 1},
        lambda h: h | {"virus": "flu"},  # filed under the wrong virus
        lambda h: h | {"country": "beta"},
        lambda h: h | {"issued": "2026-09-01T00:00:00Z"},  # header back-dated against its name
        lambda h: h | {"type": "area"},
        lambda h: [h],
    ],
)
def test_files_whose_header_disagrees_are_skipped(tmp_path, change):
    when = T0.replace(minute=0)
    raw_issue(tmp_path / "v2/covid/alpha/2026-10-06T050000Z.jsonl.gz", change(header_for("covid", "alpha", when)), [area()])
    assert list(archive.iter_issues(tmp_path)) == []


def _break(field, value, where="area"):
    def change(line):
        target = line if where == "area" else line["f"][0] if where == "f" else line["tail"]
        target[field] = value
        return line
    return change


@pytest.mark.parametrize(
    "change",
    [
        _break("probs", [0.5, 0.5, 0.5, 0.0, 0.0], "f"),  # doesn't sum to 1
        _break("probs", [1.2, -0.2, 0.0, 0.0, 0.0], "f"),  # outside [0, 1]
        _break("q", [5.0, 30.0, 25.0, 31.0, 50.0], "f"),  # quantiles out of order
        _break("q", [5.0, 10.0, 25.0, 30.0], "f"),
        _break("q", ["5", 10.0, 25.0, 30.0, 50.0], "f"),
        _break("h", 9, "f"),
        _break("h", True, "f"),
        _break("date", "2026-10-05", "f"),  # not data_as_of + h weeks
        _break("method", "magic", "f"),
        _break("p_lower", 1.5, "f"),
        _break("thr", [10.0, 20.0, 1e400, 40.0]),  # becomes infinity
        _break("thr", [10.0, 20.0]),
        _break("offset", 0),
        _break("offset", 1e-300),  # far below any offset the model uses
        _break("latest", None),
        _break("latest", 10**400),  # an integer too big for a float
        _break("region", "<b>\n</b>"),
        _break("region", ""),
        _break("level", "planet"),
        _break("data_as_of", "2026-02-30"),
        lambda line: area("bad", as_of="2026-09-28"),  # a Monday: weeks end on Sundays
        lambda line: area("bad", as_of="2026-10-18"),  # 12 days after the file's issue date
        _break("category", "apocalyptic"),
        _break("index", 101),
        _break("log", [1.0] * 12, "tail"),
        _break("flags", "." * 12 + "?", "tail"),
        _break("flags", "-" * 13, "tail"),  # says missing, but has values
        _break("start", "2026-07-01", "tail"),
        _break("f", []),
    ],
)
def test_bad_area_lines_are_skipped(tmp_path, change):
    when = T0.replace(minute=0)
    raw_issue(tmp_path / "v2/covid/alpha/2026-10-06T050000Z.jsonl.gz", header_for("covid", "alpha", when),
              [change(area("bad")), area("good")])
    ((issue, lines),) = archive.iter_issues(tmp_path)
    assert [a["region"] for a in lines] == ["good"]


def test_files_with_more_areas_than_the_limit_are_skipped(tmp_path, monkeypatch):
    when = T0.replace(minute=0)
    raw_issue(tmp_path / "v2/covid/alpha/2026-10-06T050000Z.jsonl.gz", header_for("covid", "alpha", when),
              [area(f"r{i}") for i in range(3)])
    assert len(next(archive.iter_issues(tmp_path))[1]) == 3
    monkeypatch.setattr(archive, "MAX_AREAS", 2)
    assert list(archive.iter_issues(tmp_path)) == []


def test_a_file_dated_out_of_range_is_skipped(tmp_path):
    late = datetime(9999, 12, 31, 0, 0, tzinfo=timezone.utc)
    raw_issue(tmp_path / "v2/covid/alpha/9999-12-31T000000Z.jsonl.gz", header_for("covid", "alpha", late), [area()])
    assert list(archive.iter_issues(tmp_path)) == []


def test_non_json_and_non_finite_lines_are_skipped(tmp_path):
    when = T0.replace(minute=0)
    nan_line = json.dumps(area("nan")).replace('"latest": 15.0', '"latest": NaN')
    inf_line = json.dumps(area("inf")).replace('"offset": 1.0', '"offset": Infinity')
    deep = "[" * 100000 + "]" * 100000
    raw_issue(tmp_path / "v2/covid/alpha/2026-10-06T050000Z.jsonl.gz", header_for("covid", "alpha", when),
              [area("a"), area("a")], text_lines=["not json", nan_line, inf_line, deep, json.dumps(area("b"))])
    ((issue, lines),) = archive.iter_issues(tmp_path)
    assert [a["region"] for a in lines] == ["a", "b"]  # and the repeated "a" only once


def test_issues_are_read_in_path_order(tmp_path):
    for virus, country, days in [("rsv", "alpha", 0), ("covid", "beta", 1), ("covid", "alpha", 2), ("covid", "alpha", 0)]:
        put(tmp_path, T0 + timedelta(days=days), [area(latest=10.0 + days)], virus=virus, country=country)
    order = [(i.virus, i.country, i.issued[:10]) for i, _ in archive.iter_issues(tmp_path)]
    assert order == [("covid", "alpha", "2026-10-06"), ("covid", "alpha", "2026-10-08"),
                     ("covid", "beta", "2026-10-07"), ("rsv", "alpha", "2026-10-06")]


# ---------- the tail ----------
def test_tail_flags_measured_zero_filled_and_missing_weeks():
    weeks = pd.date_range("2025-01-05", periods=40, freq="W-SUN")
    values = pd.Series(np.linspace(10, 50, 40), index=weeks)
    values.iloc[30] = 0.0  # below detection
    values = values.drop(weeks[[32, 33]])  # two missing weeks: filled in
    values = values.drop(weeks[[35, 36, 37, 38]])  # four: too long a gap, left missing
    p = prepare(RawSeries("x", "X", "region", "G", values), "alpha", "N")
    t = area_tail(p)
    assert p.last_date == weeks[39]
    assert t["start"] == weeks[27].date().isoformat()
    assert t["flags"] == "...z.ii.----."
    assert [v is None for v in t["log"]] == [f == "-" for f in t["flags"]]
    assert t["log"][-1] == round(float(p.ys.iloc[-1]), 5)
    assert t["log"][3] == round(float(np.log(0 + p.offset) * 0.5 + p.ys.iloc[29] * 0.5), 5)


def test_tail_of_a_short_series_marks_weeks_before_it_started():
    weeks = pd.date_range("2025-01-05", periods=10, freq="W-SUN")
    p = prepare(RawSeries("x", "X", "region", "G", pd.Series(np.arange(1.0, 11.0), index=weeks)), "alpha", "N")
    assert area_tail(p)["flags"] == "---.........."


# ---------- the build ----------
def growing_source(weeks: int):
    class Growing(Source):
        info = SourceInfo(id="alpha", name="Alpha", flag="", hemisphere="N", publisher="", url="", license="",
                          signals={"covid": Signal("Test metric", "gc/L")})

        def fetch(self, fetcher):
            series = [RawSeries("alpha", "Alpha", "national", "Alpha", wave_series(seed=1, noise=0.05)[:weeks])]
            for i in range(3):
                series.append(RawSeries(f"r{i}", f"R{i}", "region", "Regions", wave_series(seed=i + 2, phase=i / 3)[:weeks]))
            return series

    return Growing()


def test_builds_save_issues_that_score_once_settled(tmp_path, monkeypatch):
    archive_dir, new_dir = tmp_path / "archive", tmp_path / "new"
    last_week = wave_series().index
    monkeypatch.setenv("GITHUB_SHA", "f" * 40)

    def run(weeks, out, later=0, **kw):
        # issued ten days after the newest data, like a real build
        when = (last_week[weeks - 1] + pd.Timedelta(days=10 + later, hours=5)).to_pydatetime().replace(tzinfo=timezone.utc)
        monkeypatch.setattr(build_mod, "_issue_time", lambda: when)
        return build(out_dir=tmp_path / out, sources=[growing_source(weeks)], holdout_weeks=30, max_iter=15,
                     archive=archive_dir, **kw)

    index = run(190, "a", archive_new=new_dir)
    assert "track_record" not in index  # scoring is its own step now
    (first,) = files(archive_dir)
    assert files(new_dir) == [first]
    head, *areas = lines_of(archive_dir / first)
    assert head["commit"] == "f" * 40 and head["design"] and head["features"]
    assert head["level_definition"] == archive.LEVEL_DEFINITION
    assert set(head["calibration"]) == {str(h) for h in HORIZONS} and isinstance(head["fallback"], list)
    assert head["trained_through"] <= areas[0]["data_as_of"] and head["holdout_start"] < head["trained_through"]
    assert head["inputs"] == []  # the test source downloads nothing
    assert len(areas) == 4 and all(len(a["f"]) == len(HORIZONS) for a in areas)
    assert all(a["tail"]["flags"] == "." * 13 for a in areas)

    # the same data again: nothing new to save
    run(190, "a2", later=1)
    assert len(files(archive_dir)) == 1
    # one more week, then another: the 1-week forecasts from the first build settle
    run(191, "b")
    record = scoring.score_archive(archive_dir)
    assert record["by_virus"]["covid"]["by_horizon"][0]["n"] == 0  # target week known, not yet settled
    run(192, "c")
    assert len(files(archive_dir)) == 3
    record = scoring.score_archive(archive_dir)
    cv = record["by_virus"]["covid"]
    assert record["issues"] == 3 and cv["issues"] == 3
    h1 = cv["by_horizon"][0]
    assert (h1["n"], h1["issue_weeks"], h1["status"]) == (4, 1, "collecting")
    assert all(r["n"] == 0 for r in cv["by_horizon"][1:])
    assert 0 <= h1["right_level"] <= 1 and h1["n_rescaled"] == 0
    assert cv["unscored"]["not settled yet"] == 4 * 6 * 3 - 4
    assert sum(cv["unscored"].values()) == 4 * 6 * 3 - 4


def test_a_recording_build_publishes_no_forecast_it_could_not_save(tmp_path, monkeypatch):
    """The archive won't take forecasts from data more than 35 days old, so a build that saves its
    forecasts doesn't publish them either: everything the site shows is on record."""
    weeks = wave_series().index

    class Late(Source):
        info = SourceInfo(id="alpha", name="Alpha", flag="", hemisphere="N", publisher="", url="", license="",
                          signals={"covid": Signal("Test metric", "gc/L")})

        def fetch(self, fetcher):
            return [RawSeries("alpha", "Alpha", "national", "Alpha", wave_series(seed=1)[:190]),
                    RawSeries("r1", "R1", "region", "Regions", wave_series(seed=2)[:190]),
                    RawSeries("late", "Late", "region", "Regions", wave_series(seed=3)[:185])]  # 5 weeks behind

    when = (weeks[189] + pd.Timedelta(days=10, hours=5)).to_pydatetime().replace(tzinfo=timezone.utc)
    monkeypatch.setattr(build_mod, "_issue_time", lambda: when)
    kw = dict(sources=[Late()], holdout_weeks=30, max_iter=15)
    build(out_dir=tmp_path / "rec", archive=tmp_path / "archive", **kw)
    regions = {r["id"]: r for r in json.loads((tmp_path / "rec/alpha-covid.json").read_text())["regions"]}
    assert regions["r1"]["forecast"] and "no_forecast" not in regions["r1"]
    assert regions["late"]["forecast"] == [] and regions["late"]["no_forecast"] == "old_data"  # 45 days old
    assert regions["late"]["raw"]  # its data is still shown
    (saved,) = files(tmp_path / "archive")
    assert sorted(a["region"] for a in lines_of(tmp_path / "archive" / saved)[1:]) == ["alpha", "r1"]
    # a build that isn't saving anything publishes the forecast as before
    build(out_dir=tmp_path / "plain", **kw)
    plain = {r["id"]: r for r in json.loads((tmp_path / "plain/alpha-covid.json").read_text())["regions"]}
    assert plain["late"]["forecast"] and "no_forecast" not in plain["late"]


def test_a_recording_build_publishes_no_forecast_from_data_dated_ahead(tmp_path, monkeypatch, caplog):
    """A raw date in the next calendar week (a publisher's typo, say) labels an area's newest week
    up to 13 days after the build. The archive won't take data more than 7 days ahead, so a build
    that saves its forecasts doesn't publish that one either."""
    weeks = wave_series().index

    class Ahead(Source):
        info = SourceInfo(id="alpha", name="Alpha", flag="", hemisphere="N", publisher="", url="", license="",
                          signals={"covid": Signal("Test metric", "gc/L")})

        def fetch(self, fetcher):
            return [RawSeries("alpha", "Alpha", "national", "Alpha", wave_series(seed=1)[:189]),
                    RawSeries("r1", "R1", "region", "Regions", wave_series(seed=2)[:189]),
                    RawSeries("ahead", "Ahead", "region", "Regions", wave_series(seed=3)[:190])]  # a week further on

    # The Saturday before the others' newest week: theirs ends tomorrow, the odd one's in 8 days.
    when = (weeks[188] - pd.Timedelta(days=1) + pd.Timedelta(hours=5)).to_pydatetime().replace(tzinfo=timezone.utc)
    monkeypatch.setattr(build_mod, "_issue_time", lambda: when)
    with caplog.at_level(logging.INFO, logger="wastewater"):
        build(out_dir=tmp_path / "rec", archive=tmp_path / "archive", sources=[Ahead()], holdout_weeks=30, max_iter=15)
    regions = {r["id"]: r for r in json.loads((tmp_path / "rec/alpha-covid.json").read_text())["regions"]}
    assert regions["r1"]["forecast"] and "no_forecast" not in regions["r1"]
    assert regions["ahead"]["forecast"] == [] and regions["ahead"]["no_forecast"] == "future_data"
    assert regions["ahead"]["raw"]
    (saved,) = files(tmp_path / "archive")
    assert sorted(a["region"] for a in lines_of(tmp_path / "archive" / saved)[1:]) == ["alpha", "r1"]
    assert "more than 7 days ahead" in caplog.text and "weren't archived" not in caplog.text


def test_forecasts_are_withheld_only_outside_the_archives_date_limits():
    issued = datetime(2026, 10, 6, 5, 17, tzinfo=timezone.utc)
    dates = ("2026-09-01", "2026-08-31", "2026-09-27", "2026-10-13", "2026-10-14")
    regions = [{"latest": {"date": d}, "forecast": [{"h": 1}]} for d in dates]
    regions.append({"latest": {"date": "2026-01-04"}, "forecast": []})
    build_mod.withhold_unsavable(regions, issued)
    # 35 days old and 7 days ahead are still fine
    assert [bool(r["forecast"]) for r in regions] == [True, False, True, True, False, False]
    assert [r.get("no_forecast") for r in regions] == [None, "old_data", None, None, "future_data", None]


def test_the_writer_gives_the_real_reason_for_leaving_areas_out(tmp_path, caplog):
    lines = [area("r1", as_of="2026-10-04"), area("old", as_of="2026-08-30"), area("ahead", as_of="2026-10-18")]
    with caplog.at_level(logging.INFO, logger="wastewater.archive"):
        put(tmp_path, datetime(2026, 10, 10, 5, 17, tzinfo=timezone.utc), lines)
    assert "2 of 3 areas weren't archived: their data is more than 35 days old or more than 7 days ahead" in caplog.text


def test_a_build_without_an_archive_says_it_is_not_recording(tmp_path):
    build(out_dir=tmp_path, sources=[growing_source(200)], holdout_weeks=30, max_iter=15)
    assert json.loads((tmp_path / "track-record.json").read_text())["recording"] is False
    # with an archive the build leaves the track record to the score command
    build(out_dir=tmp_path / "rec", sources=[growing_source(200)], holdout_weeks=30, max_iter=15, archive=tmp_path / "a")
    assert not (tmp_path / "rec/track-record.json").exists()


def test_an_archive_problem_never_stops_the_build(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(archive, "write_issue", boom)
    index = build(out_dir=tmp_path / "out", sources=[growing_source(200)], holdout_weeks=30, max_iter=15,
                  archive=tmp_path / "archive")
    assert index["countries"] and index["errors"] == []


def test_collect_notes_which_upstream_files_each_country_used(tmp_path):
    from wastewater.build import collect
    from wastewater.http import Fetcher

    class Src(Source):
        def __init__(self, sid, url):
            self.info = SourceInfo(id=sid, name=sid, flag="", hemisphere="N", publisher="", url="", license="",
                                   signals={"covid": Signal("m", "u")})
            self.url = url

        def fetch(self, fetcher):
            fetcher.provenance[self.url] = {"sha256": self.url[-5:], "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT",
                                            "from_cache": False}
            return [RawSeries(self.info.id, "X", "national", "X", wave_series(weeks=60))]

    inputs = {}
    fetcher = Fetcher(cache_dir=tmp_path)
    fetcher.provenance["https://old.example/a.csv"] = {"sha256": "0", "last_modified": None, "from_cache": True}
    collect([Src("alpha", "https://a.example/1.csv"), Src("beta", "https://b.example/2.csv")], fetcher, inputs)
    assert inputs == {
        "alpha": [{"url": "https://a.example/1.csv", "sha256": "1.csv", "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT",
                   "from_cache": False}],
        "beta": [{"url": "https://b.example/2.csv", "sha256": "2.csv", "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT",
                  "from_cache": False}],
    }


def test_written_tail_values_match_the_helper(tmp_path):
    # guards the helpers the scoring tests rely on
    t = tail("2026-09-27", [15.0] * 12 + [None])
    assert t["flags"] == "." * 12 + "-" and t["log"][0] == lg(15.0)
    assert archive.valid_area(area(tail_=t))
