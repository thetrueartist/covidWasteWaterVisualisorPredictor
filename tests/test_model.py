import numpy as np
import pytest

from wastewater.model import (
    HORIZONS,
    QUANTILES,
    Dataset,
    Forecaster,
    apply_calibration,
    apply_fallback,
    evaluate,
)
from wastewater.preprocess import prepare


@pytest.fixture(scope="module")
def dataset():
    from tests.conftest import wave_series
    from wastewater.sources.base import RawSeries

    prepared = []
    for c, country in enumerate(["north", "south"]):
        hemi = "N" if c == 0 else "S"
        prepared.append(prepare(RawSeries("nat", "Nat", "national", "Nat", wave_series(phase=c, seed=c, noise=0.05)), country, hemi))
        for i in range(4):
            raw = RawSeries(f"r{i}", f"R{i}", "region", "Regions", wave_series(phase=c + 0.1 * i, seed=10 * c + i))
            prepared.append(prepare(raw, country, hemi))
    return Dataset.from_prepared(prepared)


def test_dataset_has_shared_features(dataset):
    df = dataset.frame
    nat = df[(df.key == "north/nat")].set_index("date")["d2"]
    reg = df[(df.key == "north/r1")].set_index("date")["nat_d2"]
    common = nat.index.intersection(reg.index)
    np.testing.assert_allclose(nat.loc[common].to_numpy(), reg.loc[common].to_numpy())
    assert df["cons_d1"].notna().any()


def test_forecaster_predicts_sorted_quantiles(dataset):
    model = Forecaster(max_iter=20).fit(dataset)
    rows = dataset.frame[dataset.usable()].tail(5)
    pred = model.predict(rows)
    assert pred.shape == (5, len(HORIZONS), len(QUANTILES))
    assert (np.diff(pred, axis=2) >= 0).all()


def test_regular_waves_are_forecast_better_than_no_change(dataset):
    metrics, settings = evaluate(dataset, holdout_weeks=40, max_iter=60)
    by_h = {m["horizon_weeks"]: m for m in metrics["by_horizon"]}
    assert by_h[2]["skill_vs_no_change"] > 0.2
    assert set(settings.calibration) == set(HORIZONS)
    for k50, k90 in settings.calibration.values():
        assert 0.5 <= k50 <= 4 and 0.5 <= k90 <= 4


def test_training_respects_the_cutoff(dataset):
    cutoff = dataset.frame["date"].max() - np.timedelta64(30, "W")
    early = Forecaster(max_iter=5).fit(dataset, until=cutoff)
    full = Forecaster(max_iter=5).fit(dataset)
    # 30 weeks fewer of every series (plus the horizon) are available before the cutoff
    assert full.n_train_rows - early.n_train_rows >= 30 * dataset.frame["key"].nunique()


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
