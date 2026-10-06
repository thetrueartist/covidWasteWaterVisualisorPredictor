"""The archive job's gatekeeper (.github/scripts/check-new-forecasts.sh), run on good and hostile hand-overs."""

import gzip
import json
import os
import re
import shutil
import subprocess
from datetime import timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from tests.archive_helpers import T0, area, header_for, put, raw_issue
from tests.conftest import wave_series
from wastewater import archive
from wastewater.sources import SOURCES
from wastewater.sources.base import RawSeries, Signal, Source, SourceInfo

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github/scripts/check-new-forecasts.sh"
TODAY = "2026-10-06"
NOW = "2026-10-06T120000Z"  # the archive job runs after the build that started at 05:17
NAME = "v2/covid/germany/2026-10-06T051700Z.jsonl.gz"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "gzip", "jq")), reason="needs bash, gzip and jq"
)


def run(new, branch=None, today=TODAY, now=None):
    """The gatekeeper on ``new`` against ``branch``. With only ``today`` the time is the end of that day."""
    if branch is None:
        branch = new.parent / "branch"
        branch.mkdir(exist_ok=True)
    when = [now] if now else ([NOW] if today == TODAY else [])
    return subprocess.run(["bash", str(SCRIPT), str(new), str(branch), today, *when], capture_output=True, text=True, timeout=120)


def saved(branch, name, data=b"saved"):
    """A file already on the forecast-archive branch."""
    p = branch / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def good(new, **kw):
    return put(new, T0, [area("r1", **kw), area("r2", **kw)], country="germany")


def hand_made(new, header=None, lines=None, text_lines=None, name=NAME):
    head = header if header is not None else header_for("covid", "germany", T0)
    return raw_issue(new / name, head, [area()] if lines is None else lines, text_lines)


@pytest.fixture
def new(tmp_path):
    d = tmp_path / "new"
    d.mkdir()
    return d


# ---------- accepted ----------
def test_accepts_todays_files(new):
    p = good(new)
    r = run(new)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split("\n") == [p.relative_to(new).as_posix(), ""]
    assert run(new, today="2026-10-07", now="2026-10-07T001500Z").returncode == 0  # a run that crossed midnight


def test_accepts_a_file_later_than_those_already_saved(tmp_path, new):
    branch = tmp_path / "branch"
    saved(branch, "v2/covid/germany/2026-10-05T051700Z.jsonl.gz")
    saved(branch, "v2/covid/germany/README.txt")  # not an issue: ignored
    saved(branch, "v2/covid/usa/2026-10-06T090000Z.jsonl.gz")  # another country: no matter
    good(new)
    r = run(new, branch)
    assert r.returncode == 0, r.stderr


def test_accepts_a_build_that_crossed_midnight(tmp_path):
    late = T0.replace(hour=23, minute=50)
    new = tmp_path / "new"
    put(new, late, [area()], country="germany")
    put(new, late, [area()], virus="flu", country="germany")
    r = run(new, today="2026-10-07", now="2026-10-07T001500Z")
    assert r.returncode == 0, r.stderr


def test_accepts_files_for_every_country_and_virus(new):
    for virus in archive.VIRUSES:
        for country in SOURCES:
            put(new, T0, [area()], virus=virus, country=country)
    r = run(new)
    assert r.returncode == 0, r.stderr
    assert len(r.stdout.split()) == 18


def test_nothing_to_check_is_fine(new):
    r = run(new)
    assert r.returncode == 0 and r.stdout == ""


def test_accepts_a_full_size_issue(new):
    """Real issues decompress to far more than a pipe buffer (64 kB): no SIGPIPE trouble."""
    lines = [area(f"site-{i:04d}", hs=(1, 2, 3, 4, 5, 6)) | {"name": "Treatment works " * 4} for i in range(400)]
    p = put(new, T0, lines, country="netherlands")
    assert len(gzip.decompress(p.read_bytes())) > 500_000
    r = run(new)
    assert r.returncode == 0, r.stderr


def test_a_real_builds_files_pass(tmp_path):
    """Whatever the build writes, the gatekeeper accepts: the two must never drift apart."""
    from wastewater import build as build_mod

    class Germany(Source):
        info = SourceInfo(id="germany", name="Germany", flag="", hemisphere="N", publisher="", url="", license="",
                          signals={"covid": Signal("m", "u"), "flu": Signal("m", "u")})

        def fetch(self, fetcher):
            out = [RawSeries("de", "Deutschland", "national", "Deutschland", wave_series(seed=1))]
            out += [RawSeries(f"Klärwerk <{i}>", f"K{i}", "site", "Sites", wave_series(seed=i + 2)) for i in range(4)]
            zeros = wave_series(seed=9)
            zeros.iloc[-3:-1] = 0.0
            out.append(RawSeries("zero", "Zero", "site", "Sites", zeros.drop(zeros.index[-6])))
            out.append(RawSeries("de", "Deutschland", "national", "Deutschland", wave_series(seed=5), pathogen="flu"))
            return out

    issued = (wave_series().index[-1] + pd.Timedelta(days=9, hours=5)).to_pydatetime().replace(tzinfo=timezone.utc)
    mp = pytest.MonkeyPatch()
    mp.setattr(build_mod, "_issue_time", lambda: issued)
    try:
        build_mod.build(out_dir=tmp_path / "site", sources=[Germany()], holdout_weeks=30, max_iter=15,
                        archive=tmp_path / "archive", archive_new=tmp_path / "new")
    finally:
        mp.undo()
    written = sorted(p.relative_to(tmp_path / "new").as_posix() for p in (tmp_path / "new").rglob("*.gz"))
    assert len(written) == 2
    r = run(tmp_path / "new", today=issued.date().isoformat())
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == written
    areas = [json.loads(x) for x in gzip.decompress((tmp_path / "new" / written[0]).read_bytes()).splitlines()[1:]]
    # ids with "<" or ">" would be refused, so those areas aren't saved; the rest are
    assert sorted(a["region"] for a in areas) == ["de", "zero"]
    assert {a["region"]: a["tail"]["flags"] for a in areas}["zero"] == ".......i..zz."


# ---------- refused: names and dates ----------
def test_refuses_backdated_and_future_files(new):
    good(new)
    r = run(new, today="2026-10-20")
    assert r.returncode != 0 and "not dated today" in r.stderr
    assert run(new, today="2026-10-05").returncode != 0  # dated tomorrow


def test_refuses_a_time_later_today(new):
    good(new)  # 05:17
    r = run(new, now="2026-10-06T051659Z")
    assert r.returncode != 0 and "in the future" in r.stderr
    assert run(new, now="2026-10-06T051700Z").returncode == 0


@pytest.mark.parametrize("on_branch", ["2026-10-06T051701Z", "2026-10-06T090000Z", "2026-10-07T000000Z"])
def test_refuses_a_file_slipped_in_front_of_one_already_saved(tmp_path, new, on_branch):
    """The scorer takes the first issue for each week of data, so a file dated before one already
    saved could replace a forecast that was really published."""
    branch = tmp_path / "branch"
    saved(branch, f"v2/covid/germany/{on_branch}.jsonl.gz")
    good(new)
    r = run(new, branch, now="2026-10-06T235959Z")
    assert r.returncode != 0 and "not later than the newest saved issue" in r.stderr


def test_refuses_two_files_for_one_country_and_virus(new):
    put(new, T0, [area()], country="germany")
    put(new, T0 + timedelta(seconds=1), [area(latest=16.0)], country="germany")
    r = run(new)
    assert r.returncode != 0 and "more than one new file for covid/germany" in r.stderr


def test_refuses_files_from_more_than_one_build(new):
    put(new, T0, [area()], country="germany")
    put(new, T0 + timedelta(seconds=1), [area()], country="usa")
    r = run(new)
    assert r.returncode != 0 and "different time" in r.stderr


@pytest.mark.parametrize("today,now", [("2026-10-06", "2026-10-07T000000Z"), ("2026-10-06", "05:17"), ("2026-10-06", "")])
def test_refuses_a_bad_now(new, today, now):
    good(new)
    r = subprocess.run(["bash", str(SCRIPT), str(new), str(new.parent), today, now], capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "NOW must be" in r.stderr


@pytest.mark.parametrize(
    "name",
    [
        "v2/covid/germany/../../x.jsonl.gz",
        "v2/ebola/germany/2026-10-06T000000Z.jsonl.gz",
        "v2/covid/atlantis/2026-10-06T000000Z.jsonl.gz",
        "v2/covid/germany/2026-10-06T000000Z.jsonl",
        "v2/covid/germany/2026-10-06T000000Z.sh",
        "v2/covid/germany/extra/2026-10-06T000000Z.jsonl.gz",
        "v2/Covid/germany/2026-10-06T000000Z.jsonl.gz",
        "v3/covid/germany/2026-10-06T000000Z.jsonl.gz",
        "v2/covid/germany/2026-10-06T246000Z.jsonl.gz",
        "v2/covid/germany/2026-10-06t000000z.jsonl.gz",
        "v2/covid/germany/2026-10-06T000000Z.jsonl.gz\n::add-mask::oops",
        ".github/workflows/pages.yml",
        "README.md",
    ],
)
def test_refuses_unexpected_paths(new, name):
    good(new)
    p = new / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(b"x"))
    r = run(new)
    assert r.returncode != 0
    assert "\n::add-mask::" not in r.stderr and "\x1b" not in r.stderr  # hostile names aren't echoed raw


def test_refuses_symlinks_hard_links_and_pipes(tmp_path):
    for i, make in enumerate([
        lambda p: os.symlink("/etc/passwd", p.parent / "2026-10-06T000000Z.jsonl.gz"),
        lambda p: os.symlink(p.parent, p.parent.parent / "usa"),
        lambda p: os.link(p, p.parent / "2026-10-06T000001Z.jsonl.gz"),
        lambda p: os.mkfifo(p.parent / "2026-10-06T000002Z.jsonl.gz"),
    ]):
        new = tmp_path / f"new{i}"
        make(good(new))
        r = run(new)
        assert r.returncode != 0, i
        assert ("not a regular file" in r.stderr) or ("hard link" in r.stderr)


def test_refuses_too_many_files(new):
    for i in range(19):
        put(new, T0 + timedelta(seconds=i), [area(latest=10.0 + i)], country="germany")
    assert "too many files (19, at most 18)" in run(new).stderr


def test_refuses_too_many_areas(new):
    lines = [area(f"r{i}") for i in range(archive.MAX_AREAS + 1)]
    hand_made(new, lines=lines)
    r = run(new)
    assert r.returncode != 0 and "too many" in r.stderr
    hand_made(new, lines=lines[:-1])
    assert run(new).returncode == 0


def test_refuses_large_files_and_gzip_bombs(new):
    big = new / NAME
    big.parent.mkdir(parents=True)
    big.write_bytes(gzip.compress(os.urandom(2_100_000)))
    assert "too large" in run(new).stderr
    with gzip.open(big, "wb") as f:
        f.write(b" " * 25_000_000)
    assert "decompressed" in run(new).stderr


def test_refuses_invalid_gzip(new):
    (new / NAME).parent.mkdir(parents=True)
    (new / NAME).write_bytes(b"\x1f\x8b not really gzip")
    assert "not valid gzip" in run(new).stderr
    (new / NAME).write_bytes(gzip.compress(json.dumps(header_for("covid", "germany", T0)).encode()) + b"trailing junk")
    assert run(new).returncode != 0


# ---------- refused: contents ----------
@pytest.mark.parametrize(
    "header",
    [
        header_for("flu", "germany", T0),  # filed under the wrong virus
        header_for("covid", "usa", T0),
        header_for("covid", "germany", T0, schema=1),
        header_for("covid", "germany", T0, type="area"),
        header_for("covid", "germany", T0 - timedelta(days=30)),  # back-dated inside
        header_for("covid", "germany", T0 + timedelta(seconds=1)),
        {k: v for k, v in header_for("covid", "germany", T0).items() if k != "content_hash"},
        [header_for("covid", "germany", T0)],
    ],
)
def test_refuses_headers_that_disagree_with_the_name(new, header):
    hand_made(new, header=header)
    r = run(new)
    assert r.returncode != 0 and "header" in r.stderr


def test_refuses_a_missing_or_doubled_header(new):
    hand_made(new, header=area(), lines=[area()])
    assert run(new).returncode != 0
    two = json.dumps(header_for("covid", "germany", T0)) + " " + json.dumps(header_for("covid", "germany", T0))
    (new / NAME).write_bytes(gzip.compress((two + "\n" + json.dumps(area()) + "\n").encode()))
    assert run(new).returncode != 0


def _set(path, value):
    def change(line):
        *parents, key = path
        target = line
        for k in parents:
            target = target[k]
        target[key] = value
        return line
    return change


@pytest.mark.parametrize(
    "change",
    [
        lambda line: area("bad", as_of="2026-08-30"),  # 37 days before the issue: could fill a gap in the record
        lambda line: area("bad", as_of="2026-10-25"),  # weeks in the future
        lambda line: area("bad", as_of="2026-10-18"),  # 12 days ahead: past the end of this week
        lambda line: area("bad", as_of="2026-09-28"),  # a Monday: weeks end on Sundays
        _set(["data_as_of"], "2026-02-30"),
        _set(["f", 0, "date"], "2026-09-27"),  # a target that isn't after the data
        _set(["f", 0, "date"], "2026-10-11"),  # not data_as_of + h weeks
        _set(["f", 0, "h"], 9),
        _set(["f", 0, "h"], True),
        _set(["f", 0, "q"], [1, 2, 3, 4]),
        _set(["f", 0, "q"], ["1", 2, 3, 4, 5]),
        _set(["f", 0, "probs"], [1.5, 0, 0, 0, -0.5]),
        _set(["f"], []),
        _set(["f"], [area()["f"][0]] * 7),
        _set(["thr"], [1, 2, 3]),
        _set(["offset"], 0),
        _set(["offset"], 1e-300),  # far below any offset the model uses: would overflow the scorer
        _set(["region"], "<script>alert(1)</script>"),
        _set(["region"], "a\u0085b"),
        _set(["region"], "x" * 101),
        _set(["type"], "fc"),
    ],
)
def test_refuses_malformed_area_lines(new, change):
    hand_made(new, lines=[area("ok"), change(area("bad"))])
    r = run(new)
    assert r.returncode != 0 and "area line" in r.stderr


def test_data_age_limit_is_inclusive(new):
    # issued on Sunday 2026-10-04: data from 35 days before and to 7 days after (the next Sunday)
    when = T0.replace(day=4)
    hand_made(new, header=header_for("covid", "germany", when), name="v2/covid/germany/2026-10-04T051700Z.jsonl.gz",
              lines=[area(as_of="2026-08-30"), area("b", as_of="2026-10-11")])
    r = run(new, today="2026-10-04")
    assert r.returncode == 0, r.stderr


def test_refuses_non_finite_numbers_and_junk_lines(new):
    nan = json.dumps(area()).replace('"q": [5.0', '"q": [NaN')
    hand_made(new, lines=[area()], text_lines=[nan])
    assert run(new).returncode != 0
    hand_made(new, lines=[area()], text_lines=["not json"])
    assert run(new).returncode != 0
    hand_made(new, lines=[])
    assert run(new).returncode != 0  # a header and nothing else


def test_refuses_files_already_on_the_branch(tmp_path, new):
    p = good(new)
    branch = tmp_path / "branch"
    (branch / p.relative_to(new)).parent.mkdir(parents=True)
    (branch / p.relative_to(new)).write_bytes(b"the saved one")
    r = run(new, branch)
    assert r.returncode != 0 and "already saved" in r.stderr
    assert (branch / p.relative_to(new)).read_bytes() == b"the saved one"


def test_refuses_a_branch_with_a_symlinked_folder(tmp_path, new):
    good(new)
    branch = tmp_path / "branch"
    (branch / "v2/covid").mkdir(parents=True)
    os.symlink("/tmp", branch / "v2/covid/germany")
    assert "symlink" in run(new, branch).stderr


def test_country_list_and_limits_match_the_archive():
    text = SCRIPT.read_text()
    countries = re.search(r"countries='([^']+)'", text).group(1).split("|")
    viruses = re.search(r"viruses='([^']+)'", text).group(1).split("|")
    assert countries == list(SOURCES) and tuple(viruses) == archive.VIRUSES
    limits = dict(re.findall(r"^(max_\w+|min_\w+)=(\S+)", text, re.M))
    assert int(limits["max_age_days"]) == archive.MAX_DATA_AGE_DAYS
    assert int(limits["max_ahead_days"]) == archive.MAX_DATA_AHEAD_DAYS
    assert int(limits["max_gz_bytes"]) == archive.MAX_GZ_BYTES
    assert int(limits["max_raw_bytes"]) == archive.MAX_FILE_BYTES
    assert int(limits["max_areas"]) == archive.MAX_AREAS
    assert float(limits["min_offset"]) == archive.MIN_OFFSET
    assert int(limits["max_files"]) == len(archive.VIRUSES) * len(SOURCES)


def test_archive_readme_credits_every_source():
    readme = (ROOT / ".github/scripts/forecast-archive-README.md").read_text()
    for cls in SOURCES.values():
        assert cls.info.license in readme, cls.info.id
        assert cls.info.name in readme, cls.info.id
