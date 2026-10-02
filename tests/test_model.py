from dataclasses import replace

import numpy as np
import pytest

from wastewater.model import (
    CANDIDATES,
    HORIZONS,
    MODEL_SPECS,
    QUANTILES,
    Dataset,
    Forecaster,
    _train_mask,
    apply_calibration,
    apply_fallback,
    compare,
    evaluate,
    site_sample,
)
from wastewater.preprocess import prepare

FAST = replace(CANDIDATES["trend"], max_iter=20)


def _prepared(pathogen: str, offset: float = 0):
    from tests.conftest import wave_series
    from wastewater.sources.base import RawSeries

    out = []
    for c, country in enumerate(["north", "south"]):
        hemi = "N" if c == 0 else "S"
        nat = RawSeries("nat", "Nat", "national", "Nat", wave_series(phase=c + offset, seed=c, noise=0.05), pathogen=pathogen)
        out.append(prepare(nat, country, hemi))
        for i in range(4):
            raw = RawSeries(
                f"r{i}", f"R{i}", "region", "Regions",
                wave_series(phase=c + 0.1 * i + offset, seed=10 * c + i), pathogen=pathogen,
            )
            out.append(prepare(raw, country, hemi))
        site = RawSeries("s0", "S0", "site", "Sites", wave_series(phase=c + offset, seed=99 + c), pathogen=pathogen)
        out.append(prepare(site, country, hemi))
    return out


@pytest.fixture(scope="module")
def dataset():
    return Dataset.from_prepared(_prepared("covid"))


@pytest.fixture(scope="module")
def multi():
    return Dataset.from_prepared(_prepared("covid") + _prepared("flu", offset=1.5))


def test_dataset_has_shared_features(dataset):
    df = dataset.frame
    nat = df[(df.key == "north/covid/nat")].set_index("date")["d2"]
    reg = df[(df.key == "north/covid/r1")].set_index("date")["nat_d2"]
    common = nat.index.intersection(reg.index)
    np.testing.assert_allclose(nat.loc[common].to_numpy(), reg.loc[common].to_numpy())
    assert df["cons_d1"].notna().any()


def test_shared_features_never_mix_viruses(multi):
    df = multi.frame
    flu_nat = df[df.key == "north/flu/nat"].set_index("date")["d2"]
    covid_reg = df[df.key == "north/covid/r1"].set_index("date")["nat_d2"]
    common = flu_nat.index.intersection(covid_reg.index)
    assert not np.allclose(flu_nat.loc[common].to_numpy(), covid_reg.loc[common].to_numpy())


def test_forecaster_predicts_sorted_quantiles(dataset):
    model = Forecaster("covid", FAST).fit(dataset)
    rows = dataset.frame[dataset.usable()].tail(5)
    pred = model.predict(rows)
    assert pred.shape == (5, len(HORIZONS), len(QUANTILES))
    assert (np.diff(pred, axis=2) >= 0).all()


def test_each_virus_trains_only_on_itself_unless_pooled(multi):
    df = multi.frame
    own = _train_mask(multi, "flu", CANDIDATES["trend+season+level"], None, 2)
    assert set(df.loc[own, "pathogen"]) == {"flu"}
    pooled = _train_mask(multi, "flu", CANDIDATES["trend, all viruses"], None, 2)
    assert set(df.loc[pooled, "pathogen"]) == {"covid", "flu"}
    assert "virus_code" in CANDIDATES["trend, all viruses"].columns()
    assert "virus_code" not in CANDIDATES["trend"].columns()
    # Each virus still has its own model; COVID's also learns from the others.
    assert MODEL_SPECS["covid"].pool_viruses and not MODEL_SPECS["flu"].pool_viruses


def test_site_sample_does_not_depend_on_other_viruses(dataset, multi):
    alone = dataset.frame
    together = multi.frame[multi.frame.pathogen == "covid"]
    a = dict(zip(zip(alone.key, alone.date), site_sample(alone)))
    b = dict(zip(zip(together.key, together.date), site_sample(together)))
    assert a == b
    sites = (alone.level_name == "site").to_numpy()
    assert 0.2 < site_sample(alone)[sites].mean() < 0.5
    assert site_sample(alone)[~sites].all()


def test_regular_waves_are_forecast_better_than_no_change(dataset):
    metrics, settings = evaluate(dataset, "covid", replace(FAST, max_iter=60), holdout_weeks=40)
    by_h = {m["horizon_weeks"]: m for m in metrics["by_horizon"]}
    assert by_h[2]["skill_vs_no_change"] > 0.2
    assert set(settings.calibration) == set(HORIZONS)
    for k50, k90 in settings.calibration.values():
        assert 0.5 <= k50 <= 4 and 0.5 <= k90 <= 4


def test_training_respects_the_cutoff(dataset):
    cutoff = dataset.frame["date"].max() - np.timedelta64(30, "W")
    early = Forecaster("covid", replace(FAST, max_iter=5)).fit(dataset, until=cutoff)
    full = Forecaster("covid", replace(FAST, max_iter=5)).fit(dataset)
    # 30 weeks fewer of every non-site series are available before the cutoff
    assert full.n_train_rows - early.n_train_rows >= 30 * 10


def test_compare_scores_every_candidate_on_every_fold(multi):
    few = {name: replace(CANDIDATES[name], max_iter=10) for name in ("trend", "trend+season", "trend, all viruses")}
    rows = compare(multi, "flu", few, folds=2, fold_weeks=40)
    assert {(r["candidate"], r["fold"]) for r in rows} == {(n, k) for n in few for k in (0, 1)}
    for r in rows:
        assert all(f"h{h}" in r for h in HORIZONS)
        assert set(r["by_country"]) == {"north", "south"}
    fold0, fold1 = (next(r for r in rows if r["fold"] == k) for k in (0, 1))
    assert fold1["end"] == fold0["start"]  # consecutive, non-overlapping held-out years


def test_fallback_recentres_on_no_change():
    pred = np.tile(np.array([0.1, 0.2, 0.3, 0.4, 0.5]), (2, len(HORIZONS), 1))
    out = apply_fallback(pred, np.array(["a", "b"]), {"a": [1, 2]})
    np.testing.assert_allclose(out[0, 0], [-0.2, -0.1, 0.0, 0.1, 0.2])
    np.testing.assert_allclose(out[0, 2], pred[0, 2])  # horizon 3 untouched
    np.testing.assert_allclose(out[1], pred[1])  # other country untouched


def test_calibration_scales_spread_around_median():
    pred = np.tile(np.array([-2.0, -1.0, 0.0, 1.0, 2.0]), (1, len(HORIZONS), 1))
    out = apply_calibration(pred, {h: (1.5, 2.0) for h in HORIZONS})
    np.testing.assert_allclose(out[0, 0], [-4.0, -1.5, 0.0, 1.5, 4.0])
    # widening the 50% range past the 90% range must not leave crossed quantiles
    crossed = apply_calibration(pred, {h: (3.0, 0.5) for h in HORIZONS})
    assert (np.diff(crossed, axis=2) >= 0).all()
