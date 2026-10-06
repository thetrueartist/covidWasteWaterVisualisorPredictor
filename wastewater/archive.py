"""The forecast archive: the published forecasts, each saved before its outcome is known.

The archive is a folder (the ``forecast-archive`` branch on GitHub, or
``forecast-archive/`` in a self-hosted install) of gzipped JSON-lines files::

    v2/<virus>/<country>/<YYYY-MM-DDTHHMMSSZ>.jsonl.gz      (issue time, UTC)

One file is one *issue*: everything the site published for one country and
virus in one build. A new file is written only when the published forecasts
differ from the newest saved ones, and no file is ever replaced, so the
archive only grows. wastewater/scoring.py turns it into the live track record.

Line 1 is the header::

    {"type": "header", "schema": 2, "country", "virus", "issued", "content_hash",
     "commit", "requirements_sha256", "advisor_sha256", "design", "features",
     "trained_through", "holdout_start", "calibration": {"1": [k50, k90], ...},
     "fallback": [h, ...], "level_definition", "inputs": [{"url", "sha256",
     "last_modified", "from_cache"}, ...]}

Every further line is one area that had a forecast::

    {"type": "area", "region", "name", "level", "data_as_of", "source_date",
     "latest", "offset", "index", "category", "thr": [4 cut-offs],
     "tail": {"start", "log": [13 numbers or null], "flags": "13 characters"},
     "f": [{"h", "date", "q": [5 quantiles], "probs": [5], "index", "category",
            "p_lower", "method"}, ...]}

Values (``latest``, ``q``, ``thr``) are in the source's units, as the site
showed them. ``offset`` puts them on the model's log scale, log(value + offset).
``data_as_of`` is the Sunday that ends the area's newest week of data.
``tail`` is the area's smoothed log level for the 13 weeks ending at
``data_as_of``, with one flag per week: ``.`` measured, ``z`` measured as 0
(below detection), ``i`` filled in between measurements, ``-`` missing. It lets
a later issue serve as the "truth" for an earlier one, in the earlier one's
units, even after a publisher revises or rescales its history.

Saved files are permanent, so the format above must not change meaning. A
different format gets a new schema number and a new top folder (``v3/``).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import os
import re
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger(__name__)

SCHEMA = 2
VIRUSES = ("covid", "flu", "rsv")
HORIZONS = (1, 2, 3, 4, 5, 6)
N_QUANTILES = 5
LEVEL_IDS = ("very_low", "low", "moderate", "high", "very_high")
AREA_LEVELS = ("national", "region", "local", "site")
METHODS = ("model", "no_change")
TAIL_WEEKS = 13
TAIL_FLAGS = frozenset(".zi-")
LEVEL_DEFINITION = "own-104-week percentiles, cut-offs 20/40/60/80"

NAME = re.compile(r"^[a-z0-9-]{1,40}$")
ISSUE_FILE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2})(\d{2})(\d{2})Z\.jsonl\.gz$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Like the archive job's check: no markup brackets or control characters.
_REGION = re.compile(r"^[^<>\x00-\x1f\x7f-\x9f]{1,100}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^refs/[A-Za-z0-9._/-]{1,200}$")

# The archive is read back on every build, so one damaged or tampered file
# mustn't be able to exhaust memory. The biggest real issue (the Netherlands,
# 136 areas) is about 250 kB, or 40 kB gzipped. The archive job's check uses
# the same limits, so the writer never hands it a file it would refuse.
MAX_FILE_BYTES = 5_000_000  # decompressed
MAX_GZ_BYTES = 1_000_000
MAX_AREAS = 1000
MAX_HEADER_BYTES = 1024 * 1024
# The archive job refuses areas whose data is older than this at issue time
# (a build could otherwise slip old "forecasts" into a gap in the record), or
# further ahead of it than the end of the current week (one day more for
# New Zealand, whose dates run ahead of UTC). A build that's saving its
# forecasts publishes none for areas with data outside those limits
# (build.withhold_unsavable), so an honest build never trips that check.
MAX_DATA_AGE_DAYS = 35
MAX_DATA_AHEAD_DAYS = 7
# The site shows an area while its newest data is within this many weeks of
# its country's newest. The scorer uses the same line to tell when an area
# has stopped reporting.
ACTIVE_WITHIN_WEEKS = 6
# The smallest offset the model uses (preprocess.MIN_OFFSET; a test checks).
# Anything smaller would put log values far outside what real data can give.
MIN_OFFSET = 1e-9
# Dates outside this range can only be junk, and date arithmetic on them can
# overflow.
FIRST_DATE, LAST_DATE = date(2000, 1, 1), date(2199, 12, 31)
# Probabilities are rounded to 3 decimals, so their sum can be a little off 1.
PROB_SUM_TOLERANCE = 0.02
SUNDAY = 6  # date.weekday()


# ---------- small helpers ----------
def _num(x) -> bool:
    """A finite JSON number (not a bool, not NaN or infinity, not a huge integer)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _iso_date(s) -> date | None:
    """A YYYY-MM-DD date between 2000 and 2199, or None."""
    if not (isinstance(s, str) and _DATE.match(s)):
        return None
    try:
        d = date.fromisoformat(s)
    except ValueError:
        return None
    return d if FIRST_DATE <= d <= LAST_DATE else None


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


def _loads(text: str):
    """json.loads that refuses NaN and Infinity (Python accepts them by default)."""
    return json.loads(text, parse_constant=_reject_constant)


def issued_text(when: datetime) -> str:
    """Issue time as saved in headers: 2026-10-06T05:17:00Z."""
    return f"{when.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%S}Z"


def issue_relpath(virus: str, country: str, when: datetime) -> Path:
    return Path("v2", virus, country, f"{when.astimezone(timezone.utc):%Y-%m-%dT%H%M%S}Z.jsonl.gz")


def issued_from_name(name: str) -> str | None:
    """The issue time a file name stands for, in header form, or None if it isn't an issue file."""
    m = ISSUE_FILE.match(name)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    try:
        datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
    except ValueError:
        return None
    return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}Z"


def sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def git_commit(repo: Path) -> str | None:
    """The commit the code is running from: $GITHUB_SHA, else .git/HEAD, else None."""
    sha = (os.environ.get("GITHUB_SHA") or "").strip().lower()
    if _HEX40.match(sha):
        return sha
    try:
        dot_git = repo / ".git"
        if dot_git.is_file():  # a worktree or submodule: "gitdir: <path>"
            text = dot_git.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir: "):
                return None
            gitdir = (repo / text[len("gitdir: "):]).resolve()
        elif dot_git.is_dir():
            gitdir = dot_git
        else:
            return None
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if _HEX40.match(head):
            return head
        if not head.startswith("ref: "):
            return None
        ref = head[len("ref: "):]
        if not _REF.match(ref) or ".." in ref:
            return None
        common = gitdir
        if (gitdir / "commondir").is_file():
            common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in dict.fromkeys((gitdir, common)):
            loose = base / ref
            if loose.is_file():
                value = loose.read_text(encoding="utf-8").strip()
                return value if _HEX40.match(value) else None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref and _HEX40.match(parts[0]):
                    return parts[0]
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return None


def code_fingerprint(repo: Path) -> dict:
    """Which code made a forecast: commit, pinned packages and the go/avoid rules."""
    req, adv = repo / "requirements.txt", repo / "site" / "js" / "advisor.js"
    return {
        "commit": git_commit(repo),
        "requirements_sha256": sha256_file(req) if req.is_file() else None,
        "advisor_sha256": sha256_file(adv) if adv.is_file() else None,
    }


# ---------- validation (shared by the writer and the reader) ----------
def valid_header(head, virus: str, country: str, issued: str) -> bool:
    return (
        isinstance(head, dict)
        and head.get("type") == "header"
        and head.get("schema") == SCHEMA
        and head.get("virus") == virus
        and head.get("country") == country
        and head.get("issued") == issued
        and isinstance(head.get("content_hash"), str)
    )


def _opt_index(x) -> bool:
    return x is None or (_num(x) and 0 <= x <= 100)


def _quantiles_ok(q) -> bool:
    return (
        isinstance(q, list)
        and len(q) == N_QUANTILES
        and all(_num(v) for v in q)
        and all(a <= b for a, b in zip(q, q[1:]))
    )


def _probs_ok(p) -> bool:
    return (
        isinstance(p, list)
        and len(p) == len(LEVEL_IDS)
        and all(_num(v) and 0 <= v <= 1 for v in p)
        and abs(sum(p) - 1) <= PROB_SUM_TOLERANCE
    )


def _tail_ok(tail, as_of: date) -> bool:
    if not isinstance(tail, dict):
        return False
    start = _iso_date(tail.get("start"))
    values, flags = tail.get("log"), tail.get("flags")
    if start != as_of - timedelta(weeks=TAIL_WEEKS - 1):
        return False
    if not (isinstance(values, list) and len(values) == TAIL_WEEKS and isinstance(flags, str) and len(flags) == TAIL_WEEKS):
        return False
    for v, flag in zip(values, flags):
        if flag not in TAIL_FLAGS:
            return False
        if (flag == "-") != (v is None):  # a missing week has no value, and only a missing week
            return False
        if v is not None and not _num(v):
            return False
    return True


def valid_area(r) -> bool:
    """An area line the scorer can trust: right types, finite and in-range numbers."""
    if not (isinstance(r, dict) and r.get("type") == "area"):
        return False
    if not (isinstance(r.get("region"), str) and _REGION.match(r["region"])):
        return False
    name = r.get("name")
    if not (name is None or (isinstance(name, str) and len(name) <= 200)):
        return False
    if r.get("level") not in AREA_LEVELS:
        return False
    as_of = _iso_date(r.get("data_as_of"))
    if as_of is None or as_of.weekday() != SUNDAY:  # weeks end on Sundays
        return False
    if not (r.get("source_date") is None or _iso_date(r["source_date"]) is not None):
        return False
    if not (_num(r.get("latest")) and _num(r.get("offset")) and r["offset"] >= MIN_OFFSET):
        return False
    if not (_opt_index(r.get("index")) and (r.get("category") is None or r["category"] in LEVEL_IDS)):
        return False
    thr = r.get("thr")
    if not (isinstance(thr, list) and len(thr) == 4 and all(_num(t) for t in thr) and all(a <= b for a, b in zip(thr, thr[1:]))):
        return False
    if not _tail_ok(r.get("tail"), as_of):
        return False
    fs = r.get("f")
    if not (isinstance(fs, list) and 1 <= len(fs) <= len(HORIZONS)):
        return False
    seen = set()
    for f in fs:
        if not isinstance(f, dict):
            return False
        h = f.get("h")
        if isinstance(h, bool) or h not in HORIZONS or h in seen:
            return False
        seen.add(h)
        if f.get("date") != (as_of + timedelta(weeks=h)).isoformat():
            return False
        if not (_quantiles_ok(f.get("q")) and _probs_ok(f.get("probs"))):
            return False
        if not (_opt_index(f.get("index")) and (f.get("category") is None or f["category"] in LEVEL_IDS)):
            return False
        p_lower = f.get("p_lower")
        if not (p_lower is None or (_num(p_lower) and 0 <= p_lower <= 1)):
            return False
        if f.get("method") not in METHODS:
            return False
    return True


def fresh_enough(data_as_of: date, issued_on: date) -> bool:
    """Whether an area with data up to ``data_as_of`` can be saved in an issue made on ``issued_on``."""
    age = (issued_on - data_as_of).days
    return -MAX_DATA_AHEAD_DAYS <= age <= MAX_DATA_AGE_DAYS


def _fresh_enough(line: dict, issued_on: date) -> bool:
    return fresh_enough(date.fromisoformat(line["data_as_of"]), issued_on)


# ---------- writing ----------
def content_hash(lines: Iterable[dict]) -> str:
    """SHA-256 of what was published (area lines in region order), ignoring when."""
    ordered = sorted(lines, key=lambda r: r["region"])
    return hashlib.sha256("\n".join(_canon(r) for r in ordered).encode("utf-8")).hexdigest()


def newest_header(root: Path, virus: str, country: str) -> dict | None:
    """Header of the newest readable issue for one country and virus (only its first line is read)."""
    folder = root / "v2" / virus / country
    if not folder.is_dir() or folder.is_symlink():
        return None
    try:
        names = sorted((p.name for p in folder.iterdir() if issued_from_name(p.name)), reverse=True)
    except OSError:
        return None
    for name in names:
        path = folder / name
        head = read_header(path, virus, country)
        if head is not None:
            return head
    return None


def _write_new(path: Path, data: bytes) -> bool:
    """Create ``path`` with ``data``; never replace an existing file. False if it existed."""
    if path.exists() or path.is_symlink():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        try:
            os.link(tmp, path)  # atomic, and fails if the name is taken
        except FileExistsError:
            return False
        except OSError:  # no hard links on this file system
            try:
                with open(path, "xb") as f:
                    f.write(data)
            except FileExistsError:
                return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def write_issue(
    root: str | Path,
    virus: str,
    country: str,
    header: dict,
    lines: list[dict],
    now: datetime | None = None,
    new_root: str | Path | None = None,
) -> Path | None:
    """Save one issue unless it repeats the newest saved one. Returns the new file, or None.

    ``header`` holds the descriptive fields (commit, design, inputs, ...); the
    identifying ones are added here. Only areas with forecasts are saved. With
    ``new_root`` the new file is also copied there under the same relative
    path (on GitHub, the folder handed to the job that commits it).
    """
    root = Path(root)
    if virus not in VIRUSES or not NAME.match(country):
        log.warning("not archiving %r/%r: unexpected name", virus, country)
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    good = [ln for ln in lines if ln.get("f") and valid_area(ln)]
    if len(good) < sum(1 for ln in lines if ln.get("f")):
        log.warning("%s/%s: %d of %d areas failed validation and weren't archived", virus, country,
                    sum(1 for ln in lines if ln.get("f")) - len(good), sum(1 for ln in lines if ln.get("f")))
    fresh = [ln for ln in good if _fresh_enough(ln, now.date())]
    if len(fresh) < len(good):
        log.info("%s/%s: %d of %d areas weren't archived: their data is more than %d days old or more than %d days ahead",
                 virus, country, len(good) - len(fresh), len(good), MAX_DATA_AGE_DAYS, MAX_DATA_AHEAD_DAYS)
    unique = {}
    for ln in fresh:
        unique.setdefault(ln["region"], ln)
    lines = sorted(unique.values(), key=lambda r: r["region"])
    if not lines:
        return None
    if len(lines) > MAX_AREAS:
        log.error("%s/%s: not archived: %d areas, more than the archive takes (%d)", virus, country, len(lines), MAX_AREAS)
        return None
    digest = content_hash(lines)
    previous = newest_header(root, virus, country)
    if previous is not None and previous.get("content_hash") == digest:
        return None  # nothing new was published
    rel = issue_relpath(virus, country, now)
    targets = [root / rel] + ([Path(new_root) / rel] if new_root is not None else [])
    if any(t.exists() or t.is_symlink() for t in targets):
        return None  # never replace a saved forecast
    head = {
        **{k: v for k, v in header.items() if k not in ("type", "schema", "country", "virus", "issued", "content_hash")},
        "type": "header",
        "schema": SCHEMA,
        "country": country,
        "virus": virus,
        "issued": issued_text(now),
        "content_hash": digest,
    }
    text = "".join(_canon(r) + "\n" for r in [head, *lines]).encode("utf-8")
    data = gzip.compress(text, mtime=0)  # no timestamp inside: same content, same bytes
    if len(text) > MAX_FILE_BYTES or len(data) > MAX_GZ_BYTES:
        log.error("%s/%s: not archived: the issue is too large (%d bytes, %d gzipped)", virus, country, len(text), len(data))
        return None
    if not _write_new(targets[0], data):
        return None
    for extra in targets[1:]:
        if not _write_new(extra, data):
            log.warning("couldn't copy %s to %s: a file is already there", rel, extra.parent)
    return targets[0]


# ---------- reading ----------
@dataclass(frozen=True)
class Issue:
    virus: str
    country: str
    issued: str  # "YYYY-MM-DDTHH:MM:SSZ"
    path: Path
    header: dict


def read_header(path: Path, virus: str, country: str) -> dict | None:
    issued = issued_from_name(path.name)
    if issued is None or path.is_symlink() or not path.is_file():
        return None
    try:
        with gzip.open(path, "rb") as gz:
            first = gz.readline(MAX_HEADER_BYTES)
        head = _loads(first.decode("utf-8"))
    except (OSError, EOFError, ValueError, RecursionError, zlib.error):
        return None
    return head if valid_header(head, virus, country, issued) else None


def read_issue(path: Path, virus: str, country: str) -> tuple[Issue, list[dict]] | None:
    """One issue and its valid area lines, or None if the file can't be trusted at all.

    Odd lines (bad JSON, wrong types, non-finite or out-of-range numbers or
    dates, data dated further ahead of the issue than an honest build can
    have, repeated regions) are skipped; a bad header, bad gzip, an oversized
    file or one with more than MAX_AREAS areas skips the whole file.
    """
    issued = issued_from_name(path.name)
    if issued is None or path.is_symlink() or not path.is_file():
        return None
    try:
        with gzip.open(path, "rb") as gz:
            data = gz.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            log.warning("skipping archive file %s: larger than %d MB decompressed", path.name, MAX_FILE_BYTES // 1_000_000)
            return None
        text = data.decode("utf-8")
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        log.warning("skipping archive file %s: %s", path.name, type(exc).__name__)
        return None
    rows = text.split("\n")
    try:
        head = _loads(rows[0])
    except (ValueError, RecursionError):
        return None
    if not valid_header(head, virus, country, issued):
        log.warning("skipping archive file %s: its header doesn't match its name", path.name)
        return None
    rows = [row for row in rows[1:] if row.strip()]
    if len(rows) > MAX_AREAS:
        log.warning("skipping archive file %s: more than %d areas", path.name, MAX_AREAS)
        return None
    issued_on = _iso_date(issued[:10])
    if issued_on is None:
        log.warning("skipping archive file %s: its date is out of range", path.name)
        return None
    latest_ok = issued_on + timedelta(days=MAX_DATA_AHEAD_DAYS)
    lines, seen, bad = [], set(), 0
    for row in rows:
        try:
            r = _loads(row)
        except (ValueError, RecursionError):
            bad += 1
            continue
        try:  # a slip in the checks skips the line, never the whole run
            ok = valid_area(r) and r["region"] not in seen and date.fromisoformat(r["data_as_of"]) <= latest_ok
        except (ArithmeticError, ValueError, TypeError, KeyError):
            ok = False
        if not ok:
            bad += 1
            continue
        seen.add(r["region"])
        lines.append(r)
    if bad:
        log.warning("archive file %s: skipped %d bad lines", path.name, bad)
    return Issue(virus, country, issued, path, head), lines


def _plain_dirs(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if NAME.match(p.name) and p.is_dir() and not p.is_symlink())
    except OSError:
        return []


def issue_folders(root: str | Path) -> Iterator[tuple[str, str, list[Path]]]:
    """(virus, country, issue files oldest first) for every folder in the archive, in path order."""
    base = Path(root) / "v2"
    if not base.is_dir() or base.is_symlink():
        return
    for vdir in _plain_dirs(base):
        if vdir.name not in VIRUSES:
            continue
        for cdir in _plain_dirs(vdir):
            try:
                files = sorted(p for p in cdir.iterdir() if issued_from_name(p.name) and p.is_file() and not p.is_symlink())
            except OSError:
                continue
            yield vdir.name, cdir.name, files


def iter_issues(root: str | Path) -> Iterator[tuple[Issue, list[dict]]]:
    """Every readable issue with its valid area lines, one file at a time, in path order."""
    for virus, country, files in issue_folders(root):
        for path in files:
            got = read_issue(path, virus, country)
            if got is not None:
                yield got
