"""The live track record: score saved forecasts once their outcome is known.

Reads the forecast archive (see wastewater/archive.py) and writes
``track-record.json`` for the site. Everything is scored on the model's log
scale, log(value + offset), so areas measured in very different units can be
pooled, and against the area's own data *as the site later published it*.

Which forecast is scored
    For each virus, country, area, data week D and horizon h, the first
    published one: the earliest issue whose line for that area has
    ``data_as_of == D``. Its target week is T = D + 7h days.

When it counts (no back-dating)
    Only if it was issued before the first issue in which the area's data
    reached T. Otherwise it was made after the outcome was public
    ("issued after target known").

What it is scored against
    The first later issue whose data reaches T + 7 days and that was itself
    issued on or after T + 7 days, so the target week has had a week to fill
    in ("not settled yet" until then; "area stopped reporting" once the
    area's data is more than 6 weeks behind the newest in its folder, the
    same line the site uses to stop showing an area). That issue's saved
    tail is compared with the forecast's own over the weeks both share before
    D (the newest, provisional week D itself is left out; at least 2 weeks
    are needed, else "no overlap"). A publisher can rescale its whole
    history, and the offset (a fraction of the typical level) then scales
    with it, so the change of scale is measured as the median ratio of the
    two copies' values in units, over the shared weeks where both are above
    5% of their offset (or, if fewer than 2 are, as the ratio of the
    offsets, which also drift a little from build to build). The
    later copy is divided by that ratio and put on the forecast's log scale,
    and the median remaining difference over the shared weeks (revisions) is
    taken off too. A total change beyond 25% is still scored but counted as
    "rescaled". Target weeks that were filled in between measurements
    ("interpolated target") or are missing ("target missing") aren't scored;
    weeks measured as zero (below detection) are, and are also counted so
    results can be shown without them.

Levels use the forecast's own cut-offs (the area's levels when the forecast
was made), exactly like the forecast's probabilities and the back-test.

Baselines: "no change" (the starting level), and "no change with the model's
spread" (the forecast's quantiles moved onto the starting level, which is what
the site's fallback publishes) for the interval and probability scores, plus
20% for each level for the probability score.

Results are grouped by virus and horizon for everything the site published
(model and fallback together: that's what people saw), with diagnostics by
method, country and kind of area. Uncertainty comes from resampling runs of
whole data weeks (a moving-block bootstrap), because forecasts made in the
same week share their errors: what counts is weeks, not forecasts.
"""

from __future__ import annotations

import json
import logging
import math
import os
from array import array
from bisect import bisect_left
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import archive

log = logging.getLogger(__name__)

SCHEMA = 1
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)  # the same as model.QUANTILES (a test checks)
TAUS = np.asarray(QUANTILES)
MID = QUANTILES.index(0.5)
N_LEVELS = 5
HORIZONS = archive.HORIZONS
RESCALED = math.log(1.25)  # a scale shift beyond this is counted as a rescaled series
# Weeks used to measure a rescale must be above this fraction of the offset in both copies:
# lower down, the 5-decimal rounding of the saved logs starts to blur the ratio.
RATIO_FLOOR = 0.05
MIN_OVERLAP_WEEKS = 2
MIN_WEEKS = 8  # fewer checked data weeks than this: still "collecting"
ESTABLISHED_WEEKS = 26  # from here on (about half a year): "established"
N_BOOT = 1000
BOOT_SEED = 20261006
CI_LEVEL = 0.9
GAP_DAYS = 10

ISSUED_LATE = "issued after target known"
NOT_SETTLED = "not settled yet"
STOPPED = "area stopped reporting"
TARGET_MISSING = "target missing"
INTERPOLATED = "interpolated target"
NO_OVERLAP = "no overlap"
REASONS = (ISSUED_LATE, NOT_SETTLED, STOPPED, TARGET_MISSING, INTERPOLATED, NO_OVERLAP)
METHOD_CODES = {m: i for i, m in enumerate(archive.METHODS)}
KINDS = ("areas", "sites")  # national, region and local areas vs single treatment works
# Numbers this far out can only be junk; they'd overflow the log arithmetic.
MAX_UNITS = 1e30
MAX_LOG = 100.0
# Real folders have at most a few hundred areas over the years. Past this,
# new ones are ignored (with a warning), so junk on the branch can't exhaust
# memory, and the areas already being scored keep their record.
MAX_REGIONS = 2000


# ---------- scores (plain numpy) ----------
def to_log(x, offset):
    """Values in source units -> the model's log scale.

    Below-zero values (a lower forecast quantile under the model's floor, or
    rounding) count as zero, the bottom of the scale: log(offset).
    """
    return np.log(np.maximum(np.asarray(x, dtype=float), 0.0) + np.asarray(offset, dtype=float))


def level_of(value, cutoffs):
    """Level 0-4: how many of the 4 cut-offs are at or below ``value`` (same scale)."""
    value = np.asarray(value, dtype=float)
    return (np.asarray(cutoffs, dtype=float) <= value[..., None]).sum(axis=-1)


def wis(q, y):
    """Weighted interval score of quantiles ``q`` (..., 5) against outcomes ``y`` (...).

    The 50% and 90% intervals plus the median with the standard weights
    (Bracher et al. 2021), written as the mean of 2 x the quantile (pinball)
    loss over the 5 levels. Lower is better; for a point forecast it's the
    absolute error.
    """
    q = np.asarray(q, dtype=float)
    y = np.asarray(y, dtype=float)[..., None]
    return np.mean(2.0 * ((y <= q) - TAUS) * (q - y), axis=-1)


def rps(probs, outcome):
    """Ranked probability score over the 5 ordered levels, from 0 (perfect) to 1."""
    p = np.asarray(probs, dtype=float)
    k = p.shape[-1]
    cum_p = np.cumsum(p, axis=-1)[..., : k - 1]
    cum_o = (np.asarray(outcome)[..., None] <= np.arange(k - 1)).astype(float)
    return np.sum((cum_p - cum_o) ** 2, axis=-1) / (k - 1)


_KNOT_P = np.array([0.0, *QUANTILES, 1.0])


def quantile_cdf(x, q):
    """P(value <= x) from 5 quantiles: risk.quantile_cdf for many rows at once.

    ``q`` is (n, 5) and ``x`` (n, m). Linear between the quantiles, with
    linear tails, and the same arithmetic as numpy.interp, so the results are
    identical to the site's (a test checks).
    """
    q = np.asarray(q, dtype=float)
    x = np.asarray(x, dtype=float)
    lo = q[:, :1] - np.maximum(q[:, 1:2] - q[:, :1], 1e-6)
    hi = q[:, -1:] + np.maximum(q[:, -1:] - q[:, -2:-1], 1e-6)
    xs = np.concatenate([lo, q, hi], axis=1) + np.arange(7) * 1e-9
    idx = (xs[:, None, :] <= x[:, :, None]).sum(axis=-1)
    j = np.clip(idx - 1, 0, 5)
    x0, x1 = np.take_along_axis(xs, j, axis=1), np.take_along_axis(xs, j + 1, axis=1)
    p0, p1 = _KNOT_P[j], _KNOT_P[j + 1]
    with np.errstate(divide="ignore", invalid="ignore"):
        inner = np.where(x == x0, p0, (p1 - p0) / (x1 - x0) * (x - x0) + p0)
    return np.where(idx == 0, 0.0, np.where(idx >= 7, 1.0, inner))


def level_probabilities(q, cutoffs):
    """Chance of each level: risk.category_probabilities for many rows at once (rounded the same way)."""
    q = np.asarray(q, dtype=float)
    cdf = quantile_cdf(np.asarray(cutoffs, dtype=float), q)
    full = np.concatenate([np.zeros((len(q), 1)), cdf, np.ones((len(q), 1))], axis=1)
    probs = np.clip(np.diff(full, axis=1), 0, 1)
    total = probs.sum(axis=1, keepdims=True)
    with np.errstate(divide="ignore", invalid="ignore"):
        probs = np.where(total > 0, probs / total, probs)
    return np.array([round(v, 3) for v in probs.ravel().tolist()]).reshape(probs.shape)


def block_bootstrap_ci(week, num, den, block: int, n_boot: int = N_BOOT, seed: int = BOOT_SEED, level: float = CI_LEVEL):
    """Interval for sum(num) / sum(den), resampling runs of ``block`` whole weeks.

    ``week`` is each forecast's data week as a whole-week number. Weeks with
    nothing scored still take their place in the calendar, so a block always
    spans ``block`` real weeks. Returns [low, high], or None if there's nothing
    to resample.
    """
    week = np.asarray(week, dtype=np.int64)
    if week.size == 0:
        return None
    idx = week - week.min()
    n_weeks = int(idx.max()) + 1
    w_num = np.bincount(idx, weights=np.asarray(num, dtype=float), minlength=n_weeks)
    w_den = np.bincount(idx, weights=np.asarray(den, dtype=float), minlength=n_weeks)
    length = max(1, min(int(block), n_weeks))
    n_blocks = -(-n_weeks // length)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n_weeks - length + 1, size=(n_boot, n_blocks))
    pick = (starts[:, :, None] + np.arange(length)).reshape(n_boot, -1)[:, :n_weeks]
    with np.errstate(divide="ignore", invalid="ignore"):
        stat = w_num[pick].sum(axis=1) / w_den[pick].sum(axis=1)
    stat = stat[np.isfinite(stat)]
    if stat.size == 0:
        return None
    a = (1 - level) / 2
    lo, hi = np.quantile(stat, [a, 1 - a])
    return [float(lo), float(hi)]


def status(issue_weeks: int) -> str:
    if issue_weeks < MIN_WEEKS:
        return "collecting"
    return "early" if issue_weeks < ESTABLISHED_WEEKS else "established"


# ---------- matching forecasts with outcomes ----------
def _units_ok(line: dict) -> bool:
    nums = [line["latest"], line["offset"], *line["thr"]] + [v for f in line["f"] for v in f["q"]]
    return all(abs(v) <= MAX_UNITS for v in nums) and all(
        abs(v) <= MAX_LOG for v in line["tail"]["log"] if v is not None
    )


def _log_units(x: float, offset: float) -> float:
    return math.log(max(x, 0.0) + offset)


class _Line:
    """The parts of a saved area line that scoring needs, kept small (an archive holds many)."""

    __slots__ = ("offset", "latest", "thr", "site", "start", "logs", "flags")

    def __init__(self, ln: dict):
        self.offset = float(ln["offset"])
        self.latest = float(ln["latest"])
        self.thr = array("d", ln["thr"])
        self.site = ln["level"] == "site"
        self.start = date.fromisoformat(ln["tail"]["start"])
        self.logs = array("d", (math.nan if v is None else v for v in ln["tail"]["log"]))
        self.flags = ln["tail"]["flags"]


class _Area:
    __slots__ = ("highs", "high_days", "first")

    def __init__(self):
        self.highs: list[tuple[str, _Line]] = []  # (issued, line): each time the data reached a new week
        self.high_days: list[date] = []
        self.first: dict = {}  # (data week, h) -> (issued, line, q, probs, method code)


class _Rows:
    """Scored forecasts for one virus, as columns."""

    def __init__(self):
        self.cols: dict[str, list] = {k: [] for k in ("country", "kind", "method", "h", "week", "a", "s", "q", "c", "probs", "rescaled", "below")}

    def add(self, **kw):
        for k, v in kw.items():
            self.cols[k].append(v)

    def extend(self, other: "_Rows"):
        for k, v in other.cols.items():
            self.cols[k].extend(v)

    def arrays(self) -> dict[str, np.ndarray]:
        out = {k: np.asarray(v) for k, v in self.cols.items() if k not in ("q", "c", "probs")}
        out["q"] = np.asarray(self.cols["q"], dtype=float).reshape(-1, 5)
        out["c"] = np.asarray(self.cols["c"], dtype=float).reshape(-1, 4)
        out["probs"] = np.asarray(self.cols["probs"], dtype=float).reshape(-1, N_LEVELS)
        return out


def _outcome(fc: _Line, D: date, T: date, truth: _Line):
    """(actual on the forecast's log scale, rescaled?, below detection?) or the reason it can't be scored."""
    gap = (T - truth.start).days
    k = gap // 7
    if gap % 7 or not 0 <= k < archive.TAIL_WEEKS:
        return TARGET_MISSING
    flag = truth.flags[k]
    if flag == "-":
        return TARGET_MISSING
    if flag == "i":
        return INTERPOLATED
    off_issue, off_truth = fc.offset, truth.offset

    pairs = []  # (truth in units, the forecast copy's log value) for each week both have before D
    for w in range(archive.TAIL_WEEKS):
        week = truth.start + timedelta(weeks=w)
        if week >= D:  # the forecast's own newest week was provisional: leave it out
            break
        gap_own = (week - fc.start).days
        j = gap_own // 7
        if gap_own % 7 or not 0 <= j < archive.TAIL_WEEKS or truth.flags[w] == "-" or fc.flags[j] == "-":
            continue
        if math.isnan(truth.logs[w]) or math.isnan(fc.logs[j]):
            continue
        pairs.append((math.exp(truth.logs[w]) - off_truth, fc.logs[j]))
    if len(pairs) < MIN_OVERLAP_WEEKS:
        return NO_OVERLAP

    # The change of scale, as a ratio of units, from the weeks both copies have clearly above
    # zero. A rescale multiplies every week (and the offset) by the same factor, so weeks below
    # the offset show it too; only the 5-decimal rounding of the saved logs limits how far down.
    # The offset itself drifts from build to build, so it's used only when no week will do.
    ratios = []
    for units, mine in pairs:
        own_units = math.exp(mine) - off_issue
        if units > RATIO_FLOOR * off_truth and own_units > RATIO_FLOOR * off_issue:
            ratios.append(units / own_units)
    unit = float(np.median(ratios)) if len(ratios) >= MIN_OVERLAP_WEEKS else off_truth / off_issue

    def convert(units: float) -> float:  # the later copy, in the forecast's units, on its log scale
        return math.log(max(units / unit, 0.0) + off_issue)

    shift = float(np.median([convert(units) - mine for units, mine in pairs]))  # revisions
    actual = convert(math.exp(truth.logs[k]) - off_truth) - shift
    return actual, abs(math.log(unit) + shift) > RESCALED, flag == "z"


def _issue_day(issued: str) -> date:
    return date.fromisoformat(issued[:10])


def _score_folder(virus: str, country: str, files: list[Path], rows: _Rows, unscored: Counter,
                  stopped_levels: Counter) -> tuple[int, str | None, str | None]:
    """Read one country/virus folder oldest first and add its scored forecasts to ``rows``.

    Returns (issues read, first issue time, newest issue time). Only one
    folder is held in memory at a time, and only what matters, in compact
    form: each area's first forecast per data week and each issue that
    brought new data.
    """
    areas: dict[str, _Area] = {}
    n_issues, first_issued, last_issued, ignored = 0, None, None, 0
    for path in files:
        got = archive.read_issue(path, virus, country)
        if got is None:
            continue
        issue, lines = got
        n_issues += 1
        first_issued = first_issued or issue.issued
        last_issued = issue.issued
        for ln in lines:
            if not _units_ok(ln):
                continue
            area = areas.get(ln["region"])
            if area is None:
                if len(areas) >= MAX_REGIONS:
                    ignored += 1
                    continue
                area = areas[ln["region"]] = _Area()
            d = date.fromisoformat(ln["data_as_of"])
            new_high = not area.high_days or d > area.high_days[-1]
            new = [f for f in ln["f"] if (d, f["h"]) not in area.first]
            if not (new_high or new):
                continue  # a re-issue: nothing in it is scored
            line = _Line(ln)
            if new_high:
                area.highs.append((issue.issued, line))
                area.high_days.append(d)
            for f in new:
                area.first[(d, f["h"])] = (issue.issued, line, array("d", f["q"]), array("d", f["probs"]),
                                           METHOD_CODES[f["method"]])
    if ignored:
        log.warning("%s/%s: ignored %d lines for areas beyond the first %d", virus, country, ignored, MAX_REGIONS)
    if not areas:
        return n_issues, first_issued, last_issued

    # An area whose data has fallen this far behind the rest of its country has stopped
    # reporting (for now): its unsettled forecasts may never be checked.
    newest_data = max(area.high_days[-1] for area in areas.values())
    active_from = newest_data - timedelta(weeks=archive.ACTIVE_WITHIN_WEEKS)
    for area in areas.values():
        stopped = area.high_days[-1] < active_from
        for (D, h), (issued, ln, q, probs, method) in area.first.items():
            T = D + timedelta(weeks=h)
            i = bisect_left(area.high_days, T)
            if i < len(area.highs) and area.highs[i][0] <= issued:
                unscored[ISSUED_LATE] += 1
                continue
            # The settling copy: data reaching T + 7 days, issued once that week was over.
            settled = T + timedelta(weeks=1)
            j = bisect_left(area.high_days, settled)
            while j < len(area.highs) and _issue_day(area.highs[j][0]) < settled:
                j += 1
            off = ln.offset
            if j == len(area.highs):
                if stopped:
                    unscored[STOPPED] += 1
                    start_level = int(level_of(_log_units(ln.latest, off), [_log_units(v, off) for v in ln.thr]))
                    stopped_levels[archive.LEVEL_IDS[start_level]] += 1
                else:
                    unscored[NOT_SETTLED] += 1
                continue
            got = _outcome(ln, D, T, area.highs[j][1])
            if isinstance(got, str):
                unscored[got] += 1
                continue
            actual, rescaled, below = got
            rows.add(
                country=country,
                kind=int(ln.site),
                method=method,
                h=h,
                week=D.toordinal() // 7,
                a=actual,
                s=_log_units(ln.latest, off),
                q=[_log_units(v, off) for v in q],
                c=[_log_units(v, off) for v in ln.thr],
                probs=list(probs),
                rescaled=rescaled,
                below=below,
            )
    return n_issues, first_issued, last_issued


# ---------- summaries ----------
def _r(x, digits: int = 4):
    if x is None:
        return None
    x = float(x)
    return round(x, digits) if math.isfinite(x) else None


def _ratio(num: float, den: float):
    return num / den if den > 0 else None


def _pct(err):
    """A mean log error as a typical percentage miss; None if it's too large to work out."""
    with np.errstate(over="ignore", invalid="ignore"):
        return _r(100 * np.expm1(err), 1)


def _per_forecast(cols: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    a, s, q, c = cols["a"], cols["s"], cols["q"], cols["c"]
    q50 = q[:, MID]
    cat_true, cat_pred, cat_none = level_of(a, c), level_of(q50, c), level_of(s, c)
    base = q - q50[:, None] + s[:, None]  # "no change", with the model's spread
    n = len(a)
    return {
        "err": np.abs(a - q50),
        "err_none": np.abs(a - s),
        "right": (cat_pred == cat_true).astype(float),
        "right_none": (cat_none == cat_true).astype(float),
        "within_one": (np.abs(cat_pred - cat_true) <= 1).astype(float),
        "in50": ((q[:, 1] <= a) & (a <= q[:, 3])).astype(float),
        "in90": ((q[:, 0] <= a) & (a <= q[:, 4])).astype(float),
        "bias": q50 - a,
        "wis": wis(q, a),
        "wis_none": wis(base, a),
        "rps": rps(cols["probs"], cat_true),
        "rps_none": rps(level_probabilities(base, c), cat_true) if n else np.zeros(0),
        "rps_uniform": rps(np.full((n, N_LEVELS), 1 / N_LEVELS), cat_true),
    }


def summarise(cols: dict[str, np.ndarray], m: dict[str, np.ndarray], sel: np.ndarray, h: int) -> dict:
    """Summary of the selected scored forecasts for one horizon."""
    n = int(sel.sum())
    weeks = cols["week"][sel]
    issue_weeks = int(len(np.unique(weeks)))
    out = {"h": h, "n": n, "issue_weeks": issue_weeks, "status": status(issue_weeks)}
    if n == 0:
        return out

    def mean(key, mask=sel):
        return float(m[key][mask].mean())

    err, err_none = mean("err"), mean("err_none")
    excl = sel & ~cols["below"].astype(bool)
    out.update(
        right_level=_r(mean("right")),
        right_level_no_change=_r(mean("right_none")),
        within_one=_r(mean("within_one")),
        coverage_50=_r(mean("in50")),
        coverage_90=_r(mean("in90")),
        typical_error_pct=_pct(err),
        typical_error_pct_no_change=_pct(err_none),
        skill_vs_no_change=_r(None if err_none <= 0 else 1 - err / err_none),
        relative_wis=_r(_ratio(mean("wis"), mean("wis_none"))),
        rps_skill_vs_no_change=_r(None if mean("rps_none") <= 0 else 1 - mean("rps") / mean("rps_none")),
        rps_skill_vs_uniform=_r(None if mean("rps_uniform") <= 0 else 1 - mean("rps") / mean("rps_uniform")),
        bias=_r(mean("bias")),
        n_rescaled=int(cols["rescaled"][sel].sum()),
        n_below_detection=int(n - excl.sum()),
        right_level_excl_below_detection=_r(mean("right", excl)) if excl.any() else None,
        right_level_no_change_excl_below_detection=_r(mean("right_none", excl)) if excl.any() else None,
        relative_wis_excl_below_detection=_r(_ratio(mean("wis", excl), mean("wis_none", excl))) if excl.any() else None,
        right_level_gain_ci90=None,
        relative_wis_ci90=None,
    )
    if issue_weeks >= MIN_WEEKS:
        gain = block_bootstrap_ci(weeks, m["right"][sel] - m["right_none"][sel], np.ones(n), block=h + 1)
        rel = block_bootstrap_ci(weeks, m["wis"][sel], m["wis_none"][sel], block=h + 1)
        out["right_level_gain_ci90"] = None if gain is None else [_r(v) for v in gain]
        out["relative_wis_ci90"] = None if rel is None else [_r(v) for v in rel]
    return out


def _grouped(cols, m, key: str, labels) -> dict:
    out = {}
    for code, label in labels:
        rows = [summarise(cols, m, (cols[key] == code) & (cols["h"] == h), h) for h in HORIZONS]
        rows = [r for r in rows if r["n"]]
        if rows:
            out[label] = rows
    return out


def summarise_virus(cols: dict[str, np.ndarray]) -> dict:
    m = _per_forecast(cols)
    countries = sorted(set(cols["country"].tolist()))
    return {
        "by_horizon": [summarise(cols, m, cols["h"] == h, h) for h in HORIZONS],
        "by_method": _grouped(cols, m, "method", list(enumerate(archive.METHODS))),
        "by_country": _grouped(cols, m, "country", [(c, c) for c in countries]),
        "by_area_kind": _grouped(cols, m, "kind", list(enumerate(KINDS))),
    }


# ---------- the whole archive ----------
def _parse_issued(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def score_archive(root: str | Path, now: datetime | None = None) -> dict:
    """The track record for every forecast in the archive at ``root``.

    A folder (or a virus's summary) that can't be scored is logged, listed
    under ``not_scored`` and left out, so one bad file can't take the whole
    record down.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0)
    per_virus: dict[str, dict] = {}
    newest: dict[tuple[str, str], str] = {}
    not_scored: list[dict] = []
    total, since = 0, None
    for virus, country, files in archive.issue_folders(root):
        v = per_virus.setdefault(virus, {"rows": _Rows(), "unscored": Counter(), "stopped": Counter(), "issues": 0})
        rows, unscored, stopped = _Rows(), Counter(), Counter()
        try:
            n, first, last = _score_folder(virus, country, files, rows, unscored, stopped)
        except Exception:  # merged only on success, so nothing half-scored gets in
            log.exception("couldn't score the %s forecasts for %s; leaving them out", virus, country)
            not_scored.append({"virus": virus, "country": country})
            continue
        if not n:
            continue
        v["rows"].extend(rows)
        v["unscored"].update(unscored)
        v["stopped"].update(stopped)
        v["issues"] += n
        total += n
        since = first if since is None or first < since else since
        newest[(virus, country)] = last

    by_virus = {}
    for virus in archive.VIRUSES:
        v = per_virus.get(virus)
        if not v or not v["issues"]:
            continue
        try:
            summary = summarise_virus(v["rows"].arrays())
        except Exception:
            log.exception("couldn't summarise the %s forecasts", virus)
            not_scored.append({"virus": virus, "country": None})
            by_virus[virus] = {"issues": v["issues"], "available": False}
            continue
        by_virus[virus] = {
            "issues": v["issues"],
            **summary,
            "unscored": {reason: int(v["unscored"][reason]) for reason in REASONS},
            # Forecasts from areas that stopped reporting, by the level the area was at:
            # if dropping out goes with low levels, the record could be biased.
            "stopped_by_level": {level: int(v["stopped"][level]) for level in archive.LEVEL_IDS},
        }

    gaps = []
    if newest:
        latest = max(newest.values())
        for (virus, country), last in sorted(newest.items()):
            behind = _parse_issued(latest) - _parse_issued(last)
            if behind > timedelta(days=GAP_DAYS):
                gaps.append({"virus": virus, "country": country, "newest": last, "days_behind": behind.days})
                log.warning("no new %s forecasts saved for %s since %s", virus, country, last)
    return {
        "schema": SCHEMA,
        "generated_at": archive.issued_text(now),
        "since": since,
        "issues": total,
        "by_virus": by_virus,
        "gaps": gaps,
        "not_scored": not_scored,
    }


def _finite(obj):
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if isinstance(obj, np.generic):
        return _finite(obj.item())
    return obj


def write_track_record(record: dict, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_finite(record), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
