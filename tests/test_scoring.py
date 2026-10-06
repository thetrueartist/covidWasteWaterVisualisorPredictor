"""The live track record: every scoring rule, checked against numbers worked out by hand."""

import json
import logging
import math
from datetime import date, timedelta
from statistics import median

import numpy as np
import pytest

from tests.archive_helpers import PROBS, T0, area, header_for, lg, put, raw_issue, tail
from wastewater import archive, cli, model, preprocess, risk, scoring
from wastewater.sources import LEVELS

D, T, SETTLED = "2026-09-27", "2026-10-04", "2026-10-11"  # data week, its 1-week target, the week after
WEEK = timedelta(days=7)
log = math.log


def record(root):
    return scoring.score_archive(root, now=T0 + timedelta(days=30))


def h1(rec, virus="covid"):
    return rec["by_virus"][virus]["by_horizon"][0]


def three_issues(root, truth_tail=None, truth_offset=1.0, issue_tail=None, virus="covid", country="alpha", **kw):
    """A forecast made from week D, a later issue whose data reaches its target week T,
    and one whose data reaches the week after T, which settles it."""
    where = dict(virus=virus, country=country)
    put(root, T0, [area(as_of=D, tail_=issue_tail, **kw)], **where)
    put(root, T0 + WEEK, [area(as_of=T, latest=27.0, tail_=tail(T, [15.0] * 12 + [27.0]))], **where)
    settled = truth_tail if truth_tail is not None else tail(SETTLED, [15.0] * 11 + [27.0, 30.0])
    put(root, T0 + 2 * WEEK, [area(as_of=SETTLED, latest=30.0, offset=truth_offset, tail_=settled)], **where)


def wis_by_hand(q, y):
    """Bracher et al. (2021) eq. 3: median and the 50% and 90% intervals, standard weights."""
    total = 0.5 * abs(y - q[2])
    for alpha, lo, hi in ((0.1, q[0], q[4]), (0.5, q[1], q[3])):
        total += alpha / 2 * ((hi - lo) + 2 / alpha * max(lo - y, 0) + 2 / alpha * max(y - hi, 0))
    return total / 2.5


def rps_by_hand(probs, outcome):
    cum, total = 0.0, 0.0
    for k in range(4):
        cum += probs[k]
        total += (cum - (1.0 if outcome <= k else 0.0)) ** 2
    return total / 4


# ---------- the scores themselves ----------
@pytest.mark.parametrize("y", [-3.0, 0.1, 0.5, 0.9, 1.5, 2.2, 3.0, 9.0])
def test_wis_is_the_interval_score(y):
    q = [0.0, 0.5, 1.0, 2.0, 3.0]
    assert scoring.wis(np.array(q), y) == pytest.approx(wis_by_hand(q, y))


def test_wis_of_a_point_forecast_is_the_absolute_error_and_it_vectorises():
    assert scoring.wis(np.full(5, 2.0), 3.5) == pytest.approx(1.5)
    assert scoring.wis(np.array([[0, 1, 2, 3, 4], [0, 0, 0, 0, 0]], float), np.array([2.0, 1.0])).shape == (2,)


def test_rps_worked_examples():
    assert scoring.rps(PROBS, 2) == pytest.approx((0.05**2 + 0.2**2 + 0.3**2 + 0.1**2) / 4)
    assert scoring.rps([0.2] * 5, 2) == pytest.approx(0.1)
    assert scoring.rps([1, 0, 0, 0, 0], 0) == 0
    assert scoring.rps([1, 0, 0, 0, 0], 4) == pytest.approx(1.0)
    # probability on a neighbouring level beats a confident miss two levels away
    assert scoring.rps([0, 0.25, 0.5, 0.25, 0], 3) < scoring.rps([0, 1, 0, 0, 0], 3)


def test_level_counts_cut_offs_at_or_below_the_value():
    c = [10, 20, 30, 40]
    assert list(scoring.level_of(np.array([5, 10, 19.9, 20, 45]), np.array([c] * 5))) == [0, 1, 1, 2, 4]


def test_level_probabilities_are_exactly_the_sites():
    rng = np.random.default_rng(3)
    q = np.sort(rng.normal(0, 1, (400, 5)), axis=1)
    q[::7, 1] = q[::7, 0]  # tied quantiles
    q[::11] = 1.5  # a forecast with no spread at all
    c = np.sort(rng.normal(0, 1.5, (400, 4)), axis=1)
    c[::5, 0] = q[::5, 2]  # cut-offs exactly on a quantile
    c[::13] = [-9, -8, 8, 9]  # beyond both tails
    expected = np.array([risk.category_probabilities(qq, cc) for qq, cc in zip(q, c)])
    assert (scoring.level_probabilities(q, c) == expected).all()


def test_constants_match_the_model_and_the_site():
    assert scoring.QUANTILES == model.QUANTILES
    assert archive.HORIZONS == model.HORIZONS
    assert archive.LEVEL_IDS == tuple(risk.CATEGORY_IDS)
    assert archive.AREA_LEVELS == LEVELS
    assert archive.MIN_OFFSET == preprocess.MIN_OFFSET


def test_to_log_treats_below_zero_as_zero():
    assert scoring.to_log([-0.5, 0.0, 9.0], 1.0) == pytest.approx([0.0, 0.0, log(10)])


# ---------- matching forecasts with outcomes ----------
def test_a_forecast_is_scored_against_the_settled_target_week(tmp_path):
    three_issues(tmp_path)
    rec = record(tmp_path)
    r = h1(rec)
    a, s = lg(27.0), log(16)
    q = [log(v + 1) for v in (5, 10, 25, 30, 50)]
    base = [v - q[2] + s for v in q]
    c = [log(v + 1) for v in (10, 20, 30, 40)]
    assert (r["n"], r["issue_weeks"], r["status"]) == (1, 1, "collecting")
    assert r["right_level"] == 1.0  # 28 and 26 are both "moderate" (between 21 and 31)
    assert r["right_level_no_change"] == 0.0  # 16 was "low"
    assert r["within_one"] == 1.0 and r["coverage_50"] == 1.0 and r["coverage_90"] == 1.0
    assert r["typical_error_pct"] == pytest.approx(100 * (math.exp(abs(a - q[2])) - 1), abs=0.05)
    assert r["typical_error_pct_no_change"] == pytest.approx(100 * (math.exp(abs(a - s)) - 1), abs=0.05)
    assert r["skill_vs_no_change"] == pytest.approx(1 - abs(a - q[2]) / abs(a - s), abs=1e-4)
    assert r["bias"] == pytest.approx(q[2] - a, abs=1e-4)
    assert r["relative_wis"] == pytest.approx(wis_by_hand(q, a) / wis_by_hand(base, a), abs=1e-4)
    rps_model = rps_by_hand(PROBS, 2)
    rps_none = rps_by_hand(risk.category_probabilities(np.array(base), np.array(c)), 2)
    assert r["rps_skill_vs_no_change"] == pytest.approx(1 - rps_model / rps_none, abs=1e-4)
    assert r["rps_skill_vs_uniform"] == pytest.approx(1 - rps_model / 0.1, abs=1e-4)
    assert (r["n_rescaled"], r["n_below_detection"]) == (0, 0)
    assert r["right_level_gain_ci90"] is None  # fewer than 8 weeks: no interval yet
    cv = rec["by_virus"]["covid"]
    assert cv["issues"] == 3 and rec["issues"] == 3 and rec["since"] == "2026-10-06T05:17:00Z"
    # the forecasts from the 2nd and 3rd issues are waiting for their own target weeks
    assert cv["unscored"] == {"issued after target known": 0, "not settled yet": 2, "area stopped reporting": 0,
                              "target missing": 0, "interpolated target": 0, "no overlap": 0}
    assert cv["stopped_by_level"] == dict.fromkeys(archive.LEVEL_IDS, 0)
    assert rec["not_scored"] == []
    assert cv["by_country"]["alpha"][0]["n"] == 1 and cv["by_area_kind"]["areas"][0]["n"] == 1


def test_nothing_is_scored_until_the_week_after_the_target_is_in(tmp_path):
    put(tmp_path, T0, [area(as_of=D)])
    put(tmp_path, T0 + WEEK, [area(as_of=T, tail_=tail(T, [15.0] * 12 + [27.0]))])
    rec = record(tmp_path)
    assert h1(rec)["n"] == 0
    assert rec["by_virus"]["covid"]["unscored"]["not settled yet"] == 2


LOW = [1.0] * 11  # a quiet summer: levels at the offset
VARIED = [0.5, 3.0, 1.0, 2.0, 0.2, 4.0, 1.5, 0.0, 2.5, 1.0, 6.0]  # some weeks above it, some below


@pytest.mark.parametrize("history", [LOW, VARIED], ids=["at the offset", "around the offset"])
@pytest.mark.parametrize("k", [0.7, 1.1, 1.44, 2.0])
def test_a_rescaled_history_is_scored_in_the_forecasts_units(tmp_path, k, history):
    """A publisher multiplies its whole history by k. The offset is a fraction of the typical
    level, so it's multiplied by k too. A forecast that was exactly right must still score as
    exactly right, even at low levels near the offset, where the rescale isn't a constant shift
    on the log scale."""
    units = history + [8.0, 20.0]  # the rise: week D, then the target week T (the outcome is 20)
    forecast_copy = tail(D, [0.0] + history + [8.0])
    settled = tail(SETTLED, [k * u for u in history[1:] + [8.0, 20.0, 22.0]], offset=k)
    assert len(units) == 13 and settled["log"][-2] == lg(20.0 * k, k)
    put(tmp_path, T0, [area(as_of=D, latest=8.0, q=(10.0, 15.0, 20.0, 26.0, 35.0), thr=(5.0, 12.0, 30.0, 40.0),
                            tail_=forecast_copy)])
    put(tmp_path, T0 + WEEK, [area(as_of=T, latest=20.0, tail_=tail(T, history + [8.0, 20.0]))])
    put(tmp_path, T0 + 2 * WEEK, [area(as_of=SETTLED, latest=22.0 * k, offset=k, tail_=settled)])
    r = h1(record(tmp_path))
    assert r["n"] == 1
    assert r["typical_error_pct"] == 0.0 and r["bias"] == pytest.approx(0.0, abs=1e-4)
    assert r["right_level"] == 1.0 and r["coverage_50"] == 1.0
    assert r["n_rescaled"] == int(abs(log(k)) > log(1.25))  # 1.1 is within the 25% line


def test_a_drifting_offset_alone_changes_nothing(tmp_path):
    # No rescale, but the offset grew 10% as history built up (it's a fraction of the median level).
    before = [100.0, 140.0, 180.0, 150.0, 120.0, 160.0, 200.0, 240.0, 210.0, 190.0, 170.0, 230.0]  # D-12 .. D-1
    put(tmp_path, T0, [area(as_of=D, latest=250.0, tail_=tail(D, before + [250.0]))])
    put(tmp_path, T0 + WEEK, [area(as_of=T, latest=260.0, tail_=tail(T, before[1:] + [250.0, 260.0]))])
    settled = tail(SETTLED, before[2:] + [250.0, 260.0, 280.0], offset=1.1)
    put(tmp_path, T0 + 2 * WEEK, [area(as_of=SETTLED, latest=280.0, offset=1.1, tail_=settled)])
    r = h1(record(tmp_path))
    assert r["n_rescaled"] == 0
    assert r["bias"] == pytest.approx(log(26) - log(261), abs=0.005)


QUIET = [0.5, 0.6, 0.4, 0.5, 0.7, 0.5, 0.6, 0.4, 0.5, 0.6, 0.5]  # a quiet summer: every week below the offset of 1


@pytest.mark.parametrize("drift", [0.85, 1.1, 1.2])
def test_a_drifting_offset_alone_changes_nothing_below_the_offset(tmp_path, drift):
    """No rescale, the shared weeks are all below the offset, and the offset drifts (it's a fraction
    of the median level, so it moves from build to build). Those weeks still show the scale hasn't
    changed, so a forecast that was exactly right must score as exactly right."""
    thr = (2.0, 5.0, 30.0, 60.0)
    put(tmp_path, T0, [area(as_of=D, latest=2.0, q=(10 / 3, 10 / 1.5, 10.0, 15.0, 30.0), thr=thr,
                            tail_=tail(D, [0.5] + QUIET + [2.0]))])
    put(tmp_path, T0 + WEEK, [area(as_of=T, latest=10.0, tail_=tail(T, QUIET + [2.0, 10.0]))])
    settled = tail(SETTLED, QUIET[1:] + [2.0, 10.0, 12.0], offset=drift)
    put(tmp_path, T0 + 2 * WEEK, [area(as_of=SETTLED, latest=12.0, offset=drift, thr=thr, tail_=settled)])
    r = h1(record(tmp_path))
    assert r["n"] == 1 and r["n_rescaled"] == 0
    assert r["bias"] == pytest.approx(0.0, abs=1e-3) and r["typical_error_pct"] <= 0.1


def test_the_provisional_start_week_is_left_out_of_the_rescaling(tmp_path):
    # The forecast saw only weeks D-2, D-1 and D, all at 15. The later copy moved them by 0, +0.2
    # and +0.6 on the log scale (week D was only partly reported). Only D-2 and D-1 are compared.
    issue = tail(D, [None] * 10 + [15.0, 15.0, 15.0])
    revised = 16 * math.exp(0.2) - 1
    later = [15.0] * 8 + [15.0, revised, 16 * math.exp(0.6) - 1, 27.0, 30.0]
    three_issues(tmp_path, issue_tail=issue, truth_tail=tail(SETTLED, later))
    r = h1(record(tmp_path))
    # Worked by hand: the scale is the median ratio of the two copies over those weeks, and the
    # median of what's left (the revision) is taken off too.
    unit = median([15 / 15, revised / 15])
    convert = lambda u: log(u / unit + 1)  # noqa: E731
    shift = median([convert(15) - log(16), convert(revised) - log(16)])
    a = convert(27) - shift
    assert r["bias"] == pytest.approx(log(26) - a, abs=1e-4)
    assert r["typical_error_pct_no_change"] == pytest.approx(100 * (math.exp(a - log(16)) - 1), abs=0.05)
    assert r["n_rescaled"] == 0
    # comparing week D as well would have given a different answer
    unit_d = median([1.0, revised / 15, (16 * math.exp(0.6) - 1) / 15])
    assert abs(log(27 / unit_d + 1) - a) > 0.01


def test_a_forecast_issued_after_its_target_was_known_does_not_count(tmp_path):
    put(tmp_path, T0, [area(as_of=T, tail_=tail(T, [15.0] * 12 + [27.0]))])
    # a later issue with older data, as a tampered or broken build might produce
    put(tmp_path, T0 + timedelta(days=1), [area(as_of=D)])
    unscored = record(tmp_path)["by_virus"]["covid"]["unscored"]
    assert unscored["issued after target known"] == 1 and unscored["not settled yet"] == 1


def test_the_settling_copy_must_be_issued_after_the_week_that_settles_it(tmp_path):
    """Data for the week after the target can appear before that week has ended (a line can be
    dated up to 7 days ahead). Such a copy isn't used: the next one, issued once it's over, is."""
    put(tmp_path, T0 - timedelta(days=7), [area(as_of=D)])  # issued 2026-09-29
    # issued 2026-10-06, before SETTLED (10-11) has ended, with data dated 10-11: 25 at T
    early = tail(SETTLED, [15.0] * 11 + [25.0, 26.0])
    put(tmp_path, T0, [area(as_of=SETTLED, latest=26.0, tail_=early)])
    # issued 2026-10-20 with data to 10-18: the target week was really 45
    put(tmp_path, T0 + 2 * WEEK, [area(as_of="2026-10-18", latest=40.0, tail_=tail("2026-10-18", [15.0] * 10 + [45.0, 42.0, 40.0]))])
    r = h1(record(tmp_path))
    assert r["n"] == 1
    assert r["bias"] == pytest.approx(log(26) - lg(45.0), abs=1e-4)  # scored against 45, not 25


def test_lines_dated_too_far_ahead_of_their_issue_are_ignored(tmp_path):
    """A line whose data runs more than 7 days past its issue date can't be honest: it is neither
    the truth for earlier forecasts nor a reason to call later ones late."""
    put(tmp_path, T0 - timedelta(days=7), [area(as_of=D)])  # the forecast, issued 2026-09-29
    # a hostile hand-over on 2026-10-06: data "to" 2026-10-18 (12 days ahead), matching the forecast
    fake = area(as_of="2026-10-18", latest=25.0, tail_=tail("2026-10-18", [15.0] * 11 + [25.0, 25.0]))
    raw_issue(tmp_path / "v2/covid/alpha/2026-10-06T235959Z.jsonl.gz",
              header_for("covid", "alpha", T0.replace(hour=23, minute=59, second=59)), [fake])
    assert archive.read_issue(tmp_path / "v2/covid/alpha/2026-10-06T235959Z.jsonl.gz", "covid", "alpha")[1] == []
    # the honest data: 45 at the target week
    put(tmp_path, T0 + WEEK, [area(as_of=T, latest=45.0, tail_=tail(T, [15.0] * 12 + [45.0]))])
    put(tmp_path, T0 + 2 * WEEK, [area(as_of=SETTLED, latest=40.0, tail_=tail(SETTLED, [15.0] * 11 + [45.0, 40.0]))])
    rec = record(tmp_path)
    r = h1(rec)
    assert r["n"] == 1 and rec["by_virus"]["covid"]["unscored"]["issued after target known"] == 0
    assert r["typical_error_pct"] > 50  # the forecast of 25 is scored against 45


def test_only_the_first_published_forecast_for_a_week_is_scored(tmp_path):
    put(tmp_path, T0 - timedelta(days=1), [area(as_of=D)])  # first
    three_issues(tmp_path, q=(1.0, 2.0, 3.0, 4.0, 5.0))  # re-issued from the same week: ignored
    rec = record(tmp_path)
    r = h1(rec)
    assert r["n"] == 1 and r["right_level"] == 1.0 and r["coverage_90"] == 1.0
    assert rec["by_virus"]["covid"]["unscored"]["issued after target known"] == 0


@pytest.mark.parametrize(
    "flags,reason",
    [("." * 11 + "i.", "interpolated target"), ("." * 11 + "-.", "target missing")],
)
def test_filled_in_or_missing_target_weeks_are_not_scored(tmp_path, flags, reason):
    later = tail(SETTLED, [15.0] * 11 + [27.0, 30.0])
    later["flags"] = flags
    later["log"] = [None if f == "-" else v for v, f in zip(later["log"], flags)]
    three_issues(tmp_path, truth_tail=later)
    rec = record(tmp_path)
    assert h1(rec)["n"] == 0
    assert rec["by_virus"]["covid"]["unscored"][reason] == 1


def test_a_target_week_outside_the_settling_copy_is_missing(tmp_path):
    put(tmp_path, T0, [area(as_of=D)])
    far = date.fromisoformat(D) + timedelta(weeks=15)  # data next arrives 15 weeks later
    put(tmp_path, T0 + timedelta(weeks=15), [area(as_of=far.isoformat())])
    rec = record(tmp_path)
    assert rec["by_virus"]["covid"]["unscored"]["target missing"] == 1


def test_no_overlap_means_no_score(tmp_path):
    three_issues(tmp_path, issue_tail=tail(D, [None] * 12 + [15.0]))
    rec = record(tmp_path)
    assert h1(rec)["n"] == 0 and rec["by_virus"]["covid"]["unscored"]["no overlap"] == 1


def test_below_detection_targets_are_scored_and_also_left_out(tmp_path):
    zero = tail(SETTLED, [15.0] * 11 + [0.0, 30.0])
    zero["flags"] = "." * 11 + "z."
    put(tmp_path, T0, [area("r1", as_of=D), area("r2", as_of=D)])
    put(tmp_path, T0 + WEEK, [area("r1", as_of=T, latest=0.0, tail_=tail(T, [15.0] * 12 + [0.0])),
                              area("r2", as_of=T, latest=27.0, tail_=tail(T, [15.0] * 12 + [27.0]))])
    put(tmp_path, T0 + 2 * WEEK, [area("r1", as_of=SETTLED, tail_=zero),
                                  area("r2", as_of=SETTLED, tail_=tail(SETTLED, [15.0] * 11 + [27.0, 30.0]))])
    r = h1(record(tmp_path))
    assert (r["n"], r["n_below_detection"]) == (2, 1)
    assert r["right_level"] == 0.5  # r1 fell to zero: "very low", forecast "moderate"
    assert r["right_level_excl_below_detection"] == 1.0
    assert r["right_level_no_change_excl_below_detection"] == 0.0


def test_fallback_forecasts_count_in_the_public_numbers_and_are_shown_apart(tmp_path):
    fallback = dict(q=(6.0, 10.0, 15.0, 22.0, 40.0), method="no_change")  # centred on the start, 15
    put(tmp_path, T0, [area("r1", as_of=D), area("r2", as_of=D, level="site", **fallback)])
    for i, (as_of, units) in enumerate([(T, [15.0] * 12 + [27.0]), (SETTLED, [15.0] * 11 + [27.0, 30.0])], start=1):
        put(tmp_path, T0 + i * WEEK, [area(r, as_of=as_of, tail_=tail(as_of, units)) for r in ("r1", "r2")])
    cv = record(tmp_path)["by_virus"]["covid"]
    assert cv["by_horizon"][0]["n"] == 2
    assert cv["by_method"]["model"][0]["n"] == 1 and cv["by_method"]["no_change"][0]["n"] == 1
    # the fallback *is* the "no change with the model's spread" baseline
    assert cv["by_method"]["no_change"][0]["relative_wis"] == 1.0
    assert cv["by_method"]["no_change"][0]["skill_vs_no_change"] == 0.0
    assert set(cv["by_area_kind"]) == {"areas", "sites"}


def test_other_viruses_and_countries_are_kept_apart(tmp_path):
    three_issues(tmp_path)
    put(tmp_path, T0, [area(as_of=D)], virus="flu", country="beta")
    rec = record(tmp_path)
    assert set(rec["by_virus"]) == {"covid", "flu"}
    assert h1(rec, "flu")["n"] == 0 and rec["by_virus"]["flu"]["issues"] == 1
    assert list(rec["by_virus"]["covid"]["by_country"]) == ["alpha"]


def test_broken_files_in_the_archive_do_not_stop_scoring(tmp_path):
    three_issues(tmp_path)
    folder = tmp_path / "v2/covid/alpha"
    (folder / "2026-10-07T000000Z.jsonl.gz").write_bytes(b"garbage")
    (folder / "README").write_text("hi")
    assert h1(record(tmp_path))["n"] == 1


# ---------- areas that stop reporting ----------
def weekly(root, weeks, r2_weeks, r2_back=range(0)):
    """``weeks`` weekly issues with 1-6-week forecasts: r1 in every one, r2 in the first
    ``r2_weeks`` and again in the weeks ``r2_back``."""
    d0 = date.fromisoformat(D)
    for w in range(weeks):
        as_of = (d0 + timedelta(weeks=w)).isoformat()
        lines = [area("r1", as_of=as_of, hs=archive.HORIZONS)]
        if w < r2_weeks or w in r2_back:
            lines.append(area("r2", as_of=as_of, hs=archive.HORIZONS))
        put(root, T0 + w * WEEK, lines)


def test_forecasts_from_an_area_that_stopped_reporting_are_counted_apart(tmp_path):
    # r2 reported for 3 weeks, then stopped: 17 of its 18 forecasts can never be checked.
    weekly(tmp_path, 40, 3)
    cv = record(tmp_path)["by_virus"]["covid"]
    assert cv["unscored"]["area stopped reporting"] == 17
    # r1's newest forecasts are the normal backlog: 2 + 3 + ... + 7
    assert cv["unscored"]["not settled yet"] == 27
    assert cv["stopped_by_level"] == {"very_low": 0, "low": 17, "moderate": 0, "high": 0, "very_high": 0}  # 15 is "low"


@pytest.mark.parametrize("weeks,r2_weeks,waiting", [(40, 40, 54), (40, 35, 54), (10, 10, 54)],
                         ids=["both to the end", "r2 stopped within 6 weeks", "the whole folder stops"])
def test_areas_within_6_weeks_of_the_newest_data_are_still_waiting(tmp_path, weeks, r2_weeks, waiting):
    weekly(tmp_path, weeks, r2_weeks)
    unscored = record(tmp_path)["by_virus"]["covid"]["unscored"]
    assert unscored["area stopped reporting"] == 0
    assert unscored["not settled yet"] == waiting  # each area's newest 27 forecasts


def test_an_area_that_reports_again_is_no_longer_counted_as_stopped(tmp_path):
    weekly(tmp_path, 40, 3, r2_back=range(30, 40))
    unscored = record(tmp_path)["by_virus"]["covid"]["unscored"]
    assert unscored["area stopped reporting"] == 0
    # its early targets fell in the gap, so the later data doesn't have them
    assert unscored["target missing"] == 17


def test_gaps_list_countries_that_stopped_saving(tmp_path):
    put(tmp_path, T0 + timedelta(days=14), [area(as_of=SETTLED)], country="alpha")
    put(tmp_path, T0, [area(as_of=D)], country="beta")  # 14 days behind
    put(tmp_path, T0 + timedelta(days=6), [area(as_of=D)], country="gamma")  # 8 days: fine
    gaps = record(tmp_path)["gaps"]
    assert gaps == [{"virus": "covid", "country": "beta", "newest": "2026-10-06T05:17:00Z", "days_behind": 14}]


def test_an_empty_archive(tmp_path):
    rec = record(tmp_path)
    assert rec["issues"] == 0 and rec["since"] is None and rec["by_virus"] == {} and rec["gaps"] == []


# ---------- hostile or broken files ----------
def test_a_tiny_offset_cannot_crash_the_scorer(tmp_path):
    """Two well-formed files with an absurdly small offset once overflowed the typical-error sum,
    which stopped scoring every day after. Now the line is refused, and the honest folder next to
    it is scored."""
    three_issues(tmp_path)  # covid/alpha, honest
    tiny = dict(latest=0.0, offset=1e-300, q=(0.0,) * 5, method="no_change")
    assert archive.write_issue(tmp_path / "w", "covid", "usa", {}, [area(as_of=D, **tiny)], now=T0) is None
    folder = tmp_path / "v2/covid/usa"
    when = T0.replace(hour=1)
    raw_issue(folder / "2026-10-06T011700Z.jsonl.gz", header_for("covid", "usa", when),
              [area(as_of=D, tail_=tail(D, logs=[3.0] * 13), **tiny)])
    later = when + timedelta(seconds=1)
    raw_issue(folder / "2026-10-06T011701Z.jsonl.gz", header_for("covid", "usa", later),
              [area(as_of=SETTLED, tail_=tail(SETTLED, logs=[3.0] * 11 + [90.0, 3.0]), **tiny)])
    assert archive.read_issue(folder / "2026-10-06T011700Z.jsonl.gz", "covid", "usa")[1] == []
    rec = record(tmp_path)
    assert h1(rec)["n"] == 1 and list(rec["by_virus"]["covid"]["by_country"]) == ["alpha"]


def test_a_typical_error_too_large_to_work_out_is_null():
    assert scoring._pct(790.0) is None
    assert scoring._pct(math.log(1.25)) == 25.0


def _far_out(as_of: str, h: int, start: str, target: str) -> dict:
    line = area("odd", hs=(h,))
    line["data_as_of"] = line["source_date"] = as_of
    line["tail"]["start"] = start
    line["f"][0]["date"] = target
    return line


@pytest.mark.parametrize(
    "line",
    [
        _far_out("9999-12-26", 1, "9999-10-03", "10000-01-02"),  # its target can't be a date
        _far_out("0001-01-07", 1, "0001-01-01", "0001-01-14"),  # its tail would start before year 1
        _far_out("9999-11-14", 6, "9999-08-22", "9999-12-26"),  # fine on its own, but not the week after
    ],
    ids=["year 9999", "year 1", "year 9999, 6 weeks ahead"],
)
def test_a_line_with_an_extreme_date_is_skipped_not_fatal(tmp_path, line):
    three_issues(tmp_path)
    raw_issue(tmp_path / "v2/covid/alpha/2026-10-07T000000Z.jsonl.gz",
              header_for("covid", "alpha", T0.replace(day=7, hour=0, minute=0)), [line, area("ok", as_of=T)])
    _, lines = archive.read_issue(tmp_path / "v2/covid/alpha/2026-10-07T000000Z.jsonl.gz", "covid", "alpha")
    assert [a["region"] for a in lines] == ["ok"]
    out = tmp_path / "out/track-record.json"
    assert cli.main(["score", "--archive", str(tmp_path), "--out", str(out)]) == 0
    assert json.loads(out.read_text())["by_virus"]["covid"]["by_horizon"][0]["n"] == 1


def test_dates_outside_2000_to_2199_are_not_dates():
    assert [archive._iso_date(d) for d in ("1999-12-26", "2000-01-02", "2199-12-29", "2200-01-04")] == [
        None, date(2000, 1, 2), date(2199, 12, 29), None]


def test_a_folder_that_cant_be_scored_is_left_out_and_listed(tmp_path, monkeypatch, caplog):
    three_issues(tmp_path)
    three_issues(tmp_path, country="beta")
    real = scoring._score_folder

    def flaky(virus, country, *rest):
        if country == "beta":
            raise RuntimeError("something nobody thought of")
        return real(virus, country, *rest)

    monkeypatch.setattr(scoring, "_score_folder", flaky)
    with caplog.at_level(logging.ERROR, logger="wastewater.scoring"):
        rec = record(tmp_path)
    cv = rec["by_virus"]["covid"]
    assert list(cv["by_country"]) == ["alpha"] and cv["by_horizon"][0]["n"] == 1 and cv["issues"] == 3
    assert rec["not_scored"] == [{"virus": "covid", "country": "beta"}]
    assert "couldn't score the covid forecasts for beta" in caplog.text


def test_a_virus_that_cant_be_summarised_is_marked_unavailable(tmp_path, monkeypatch):
    three_issues(tmp_path)
    three_issues(tmp_path, virus="flu")
    real, calls = scoring.summarise_virus, []

    def flaky(cols):
        calls.append(1)
        if len(calls) == 2:  # covid is summarised first, then flu
            raise OverflowError("math range error")
        return real(cols)

    monkeypatch.setattr(scoring, "summarise_virus", flaky)
    rec = record(tmp_path)
    assert rec["by_virus"]["covid"]["by_horizon"][0]["n"] == 1
    assert rec["by_virus"]["flu"] == {"issues": 3, "available": False}
    assert rec["not_scored"] == [{"virus": "flu", "country": None}]


def test_areas_beyond_the_regions_cap_are_ignored_not_fatal(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(scoring, "MAX_REGIONS", 1)
    put(tmp_path, T0, [area("r1", as_of=D), area("r2", as_of=D)])
    put(tmp_path, T0 + WEEK, [area(r, as_of=T, tail_=tail(T, [15.0] * 12 + [27.0])) for r in ("r1", "r2")])
    put(tmp_path, T0 + 2 * WEEK, [area(r, as_of=SETTLED, tail_=tail(SETTLED, [15.0] * 11 + [27.0, 30.0])) for r in ("r1", "r2")])
    with caplog.at_level(logging.WARNING, logger="wastewater.scoring"):
        r = h1(record(tmp_path))
    assert r["n"] == 1  # r1 only
    assert "ignored 3 lines for areas beyond the first 1" in caplog.text


# ---------- summaries, status and uncertainty ----------
def columns(n_weeks, per_week=20, h=1, seed=0):
    """Scored forecasts for ``n_weeks`` data weeks, as the scorer holds them."""
    rng = np.random.default_rng(seed)
    n = n_weeks * per_week
    a = rng.normal(3, 1, n)
    q50 = a + rng.normal(0, 0.3, n)
    q = np.stack([q50 - 1, q50 - 0.4, q50, q50 + 0.4, q50 + 1], axis=1)
    c = np.tile([2.0, 2.7, 3.3, 4.0], (n, 1))
    return {
        "country": np.array(["alpha"] * n), "kind": np.zeros(n, int), "method": np.zeros(n, int),
        "h": np.full(n, h), "week": np.repeat(np.arange(n_weeks) + 2900, per_week), "a": a,
        "s": a + rng.normal(0, 0.5, n), "q": q, "c": c, "probs": np.tile(PROBS, (n, 1)),
        "rescaled": np.zeros(n, bool), "below": np.zeros(n, bool),
    }


def test_status_follows_the_number_of_weeks_checked():
    assert [scoring.status(w) for w in (0, 7, 8, 25, 26, 80)] == ["collecting"] * 2 + ["early"] * 2 + ["established"] * 2


@pytest.mark.parametrize("weeks,state,has_ci", [(5, "collecting", False), (10, "early", True), (30, "established", True)])
def test_intervals_appear_from_8_weeks(weeks, state, has_ci):
    r = scoring.summarise_virus(columns(weeks))["by_horizon"][0]
    assert (r["issue_weeks"], r["status"]) == (weeks, state)
    assert (r["right_level_gain_ci90"] is not None) == has_ci and (r["relative_wis_ci90"] is not None) == has_ci
    if has_ci:
        lo, hi = r["right_level_gain_ci90"]
        assert lo <= r["right_level"] - r["right_level_no_change"] <= hi
        lo, hi = r["relative_wis_ci90"]
        assert lo <= r["relative_wis"] <= hi


def test_the_bootstrap_is_repeatable():
    cols = columns(12)
    assert scoring.summarise_virus(cols) == scoring.summarise_virus(cols)
    weeks = cols["week"]
    num = cols["a"]
    assert scoring.block_bootstrap_ci(weeks, num, np.ones(len(num)), 2) == scoring.block_bootstrap_ci(weeks, num, np.ones(len(num)), 2)
    assert scoring.block_bootstrap_ci(weeks, num, np.ones(len(num)), 2) != scoring.block_bootstrap_ci(weeks, num, np.ones(len(num)), 2, seed=1)


def test_the_bootstrap_resamples_weeks_not_forecasts():
    # one week with 1,000 forecasts that all went right, nine weeks with one forecast each that didn't
    weeks = np.array([0] * 1000 + list(range(1, 10)))
    gain = np.array([1.0] * 1000 + [0.0] * 9)
    lo, hi = scoring.block_bootstrap_ci(weeks, gain, np.ones(len(gain)), block=1)
    assert lo == 0.0 and hi > 0.99  # without that one week there is nothing to show
    # identical weeks: no uncertainty at all
    assert scoring.block_bootstrap_ci(np.repeat(np.arange(10), 3), np.full(30, 0.25), np.ones(30), block=3) == [0.25, 0.25]


def test_blocks_span_calendar_weeks_including_empty_ones():
    weeks = np.array([0, 0, 5, 5])  # weeks 1-4 had nothing scored
    lo, hi = scoring.block_bootstrap_ci(weeks, np.array([1.0, 1.0, 0.0, 0.0]), np.ones(4), block=2)
    assert 0.0 <= lo <= hi <= 1.0


def test_non_finite_numbers_become_null(tmp_path):
    out = tmp_path / "data/track-record.json"
    scoring.write_track_record({"a": float("nan"), "b": [np.float64("inf"), 1.5], "c": np.int64(3)}, out)
    assert json.loads(out.read_text()) == {"a": None, "b": [None, 1.5], "c": 3}


# ---------- the command ----------
def test_score_command_writes_the_track_record(tmp_path, capsys):
    three_issues(tmp_path / "archive")
    out = tmp_path / "site/data/track-record.json"
    assert cli.main(["score", "--archive", str(tmp_path / "archive"), "--out", str(out)]) == 0
    text = out.read_text()
    saved = json.loads(text, parse_constant=lambda c: pytest.fail(f"{c} in the output"))
    assert saved["schema"] == 1 and saved["issues"] == 3
    assert saved["by_virus"]["covid"]["by_horizon"][0]["n"] == 1
    assert "1 forecasts checked" in capsys.readouterr().out


def test_score_command_publishes_a_partial_record(tmp_path, capsys, monkeypatch):
    """A virus or folder that can't be scored is left out, and the command still succeeds, so CI
    publishes what could be scored rather than replacing it all with "couldn't be updated"."""
    three_issues(tmp_path / "archive")
    three_issues(tmp_path / "archive", virus="flu")
    three_issues(tmp_path / "archive", virus="rsv")
    real_summary, real_folder, calls = scoring.summarise_virus, scoring._score_folder, []

    def flaky_summary(cols):
        calls.append(1)
        if len(calls) == 2:  # covid, flu, rsv in that order
            raise OverflowError("math range error")
        return real_summary(cols)

    def flaky_folder(virus, country, *rest):
        if virus == "rsv":
            raise RuntimeError("something nobody thought of")
        return real_folder(virus, country, *rest)

    monkeypatch.setattr(scoring, "summarise_virus", flaky_summary)
    monkeypatch.setattr(scoring, "_score_folder", flaky_folder)
    out = tmp_path / "track-record.json"
    assert cli.main(["score", "--archive", str(tmp_path / "archive"), "--out", str(out)]) == 0
    saved = json.loads(out.read_text())
    assert saved["by_virus"]["covid"]["by_horizon"][0]["n"] == 1
    assert saved["by_virus"]["flu"] == {"issues": 3, "available": False}
    printed = capsys.readouterr()
    assert "covid: 1 forecasts checked" in printed.out
    assert "flu: couldn't be summarised" in printed.err
    assert "rsv/alpha: couldn't be scored" in printed.err


def test_score_command_needs_an_archive(tmp_path, capsys):
    assert cli.main(["score", "--archive", str(tmp_path / "missing"), "--out", str(tmp_path / "x.json")]) == 1
    assert not (tmp_path / "x.json").exists()
    with pytest.raises(SystemExit):
        cli.main(["build", "--archive-new", str(tmp_path / "new")])
