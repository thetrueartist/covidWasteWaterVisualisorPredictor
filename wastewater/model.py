"""Probabilistic forecaster for wastewater levels.

One "global" model is trained across every area in every country at once,
which gives it far more waves to learn from than any single area has. For
each forecast horizon (1-6 weeks ahead) and each quantile, a gradient-boosted
tree model predicts how much an area's smoothed log level will change.
Features are all relative (recent changes in the area, in its country's
national series and across all of the country's areas), so areas measured in
completely different units can share one model.

Honesty checks, in ``evaluate``:

* the most recent year is held out; the model is trained only on data that
  was available before it and scored against a "no change" baseline;
* per country and horizon, if the model did not beat "no change" in that
  hold-out, forecasts for that country fall back to "no change" (keeping the
  model's uncertainty band);
* the hold-out also widens or narrows the 50% and 90% ranges so that they
  cover about 50% and 90% of outcomes.

Earlier versions also used "where is the level within its two-year range"
and seasonality features. They looked fine on one hold-out window and badly
over-predicted rises on another, so they were dropped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from .preprocess import MIN_WINDOW, WINDOW_WEEKS, Prepared

log = logging.getLogger(__name__)

HORIZONS = (1, 2, 3, 4, 5, 6)
QUANTILES = (0.05, 0.25, 0.5, 0.75, 0.95)
TRAIN_START = pd.Timestamp("2022-01-01")
HOLDOUT_WEEKS = 52
# Treatment-plant rows outnumber everything else. Training on a sample of
# them is as accurate, much faster, and stops noisy single plants from
# dominating the aggregated series people mostly look at. (Explicit sample
# weights did the same job no better and made fitting about 6x slower.)
SITE_SAMPLE = 0.35
LEVEL_CODES = {"national": 0, "region": 1, "local": 2, "site": 3}
CATEGORY_EDGES = ("thr20", "thr40", "thr60", "thr80")

OWN_FEATURES = ["d1", "d2", "d4", "d8", "accel", "raw_dev", "vol", "level"]
NATIONAL_FEATURES = ["nat_d1", "nat_d2", "nat_d4"]
CONSENSUS_FEATURES = ["cons_d1", "cons_d2", "cons_d4"]
FEATURES = OWN_FEATURES + NATIONAL_FEATURES + CONSENSUS_FEATURES
MID = QUANTILES.index(0.5)


def series_frame(p: Prepared) -> pd.DataFrame:
    """Own features, targets and bookkeeping columns for every week of one series."""
    ys, y = p.ys, p.y
    f = pd.DataFrame(index=ys.index)
    for k in (1, 2, 4, 8):
        f[f"d{k}"] = ys - ys.shift(k)
    f["accel"] = f["d1"] - f["d1"].shift(1)
    f["raw_dev"] = y - ys
    f["vol"] = ys.diff().rolling(12, min_periods=4).std()
    f["level"] = LEVEL_CODES[p.raw.level]

    roll = ys.rolling(WINDOW_WEEKS, min_periods=MIN_WINDOW)
    for q, col in zip((0.2, 0.4, 0.6, 0.8), CATEGORY_EDGES):
        f[col] = roll.quantile(q)
    f["ys"] = ys
    f["pct"] = p.index
    for h in HORIZONS:
        f[f"target_h{h}"] = ys.shift(-h) - ys
    f["key"] = p.key
    f["country"] = p.country
    f["level_name"] = p.raw.level
    f["date"] = f.index
    return f.reset_index(drop=True)


def add_shared_features(df: pd.DataFrame) -> pd.DataFrame:
    """Attach each country's national trend and the median trend of its areas."""
    trend = ["d1", "d2", "d4"]
    nat = df.loc[df["level_name"] == "national", ["country", "date", *trend]]
    nat = nat.drop_duplicates(["country", "date"]).rename(columns={c: f"nat_{c}" for c in trend})
    cons = df.groupby(["country", "date"], as_index=False)[trend].median()
    cons = cons.rename(columns={c: f"cons_{c}" for c in trend})
    return df.merge(nat, on=["country", "date"], how="left").merge(cons, on=["country", "date"], how="left")


@dataclass
class Dataset:
    frame: pd.DataFrame

    @classmethod
    def from_prepared(cls, prepared: list[Prepared]) -> "Dataset":
        frames = [series_frame(p) for p in prepared]
        if not frames:
            raise ValueError("no series to build a dataset from")
        return cls(add_shared_features(pd.concat(frames, ignore_index=True)))

    def usable(self) -> pd.Series:
        df = self.frame
        return (df["date"] >= TRAIN_START) & df["ys"].notna() & df["d1"].notna()


def _new_model(q: float, max_iter: int, seed: int) -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss="quantile",
        quantile=q,
        learning_rate=0.1,
        max_iter=max_iter,
        max_leaf_nodes=31,
        min_samples_leaf=80,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=seed,
    )


@dataclass
class Forecaster:
    max_iter: int = 100
    seed: int = 0
    models: dict = field(default_factory=dict)
    # Per-horizon multipliers for the 50% and 90% interval half-widths.
    calibration: dict = field(default_factory=lambda: {h: (1.0, 1.0) for h in HORIZONS})
    # country -> horizons where "no change" beat the model in back-testing.
    fallback: dict = field(default_factory=dict)
    n_train_rows: int = 0

    def fit(self, ds: Dataset, until: pd.Timestamp | None = None) -> "Forecaster":
        """Train on rows whose target was observed on or before ``until``."""
        df = ds.frame
        rng = np.random.default_rng(self.seed)
        base = ds.usable() & ((df["level_name"] != "site") | (rng.random(len(df)) < SITE_SAMPLE))
        for h in HORIZONS:
            target = f"target_h{h}"
            mask = base & df[target].notna()
            if until is not None:
                mask &= df["date"] + pd.Timedelta(weeks=h) <= until
            X, y = df.loc[mask, FEATURES], df.loc[mask, target]
            self.n_train_rows = max(self.n_train_rows, int(mask.sum()))
            log.info("fitting %d-week horizon on %d rows", h, len(X))
            for q in QUANTILES:
                self.models[(h, q)] = _new_model(q, self.max_iter, self.seed).fit(X, y)
        return self

    def predict_raw(self, X: pd.DataFrame) -> np.ndarray:
        """Predicted change in log level, shape (rows, horizons, quantiles)."""
        out = np.empty((len(X), len(HORIZONS), len(QUANTILES)))
        for i, h in enumerate(HORIZONS):
            for j, q in enumerate(QUANTILES):
                out[:, i, j] = self.models[(h, q)].predict(X[FEATURES])
        return smooth_horizons(np.sort(out, axis=2))

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """Final forecasts: fallback applied, then intervals calibrated."""
        pred = apply_fallback(self.predict_raw(X), X["country"].to_numpy(), self.fallback)
        return apply_calibration(pred, self.calibration)


def smooth_horizons(pred: np.ndarray) -> np.ndarray:
    """Blend each horizon's weekly rate of change with its neighbours.

    Each horizon has its own models, so on their own the forecast path zigzags
    from week to week. Averaging the per-week rate with the adjacent horizons
    (the first horizon is left alone) removed about 70% of that zigzag and
    slightly improved back-test accuracy at every horizon.
    """
    h = np.asarray(HORIZONS, dtype=float)[None, :, None]
    rate = pred / h
    out = rate.copy()
    out[:, 1:-1] = (rate[:, :-2] + rate[:, 1:-1] + rate[:, 2:]) / 3
    out[:, -1] = (rate[:, -2] + rate[:, -1]) / 2
    return np.sort(out * h, axis=2)


def apply_fallback(pred: np.ndarray, countries: np.ndarray, fallback: dict) -> np.ndarray:
    """Re-centre forecasts on "no change" where the model lost to it."""
    out = pred.copy()
    for country, horizons in fallback.items():
        rows = countries == country
        for h in horizons:
            i = HORIZONS.index(h)
            out[rows, i, :] -= out[rows, i, MID : MID + 1]
    return out


def apply_calibration(pred: np.ndarray, calibration: dict) -> np.ndarray:
    out = pred.copy()
    for i, h in enumerate(HORIZONS):
        k50, k90 = calibration.get(h, (1.0, 1.0))
        med = pred[:, i, MID]
        for j, q in enumerate(QUANTILES):
            k = k50 if q in (0.25, 0.75) else k90 if q in (0.05, 0.95) else 1.0
            out[:, i, j] = med + k * (pred[:, i, j] - med)
    return np.sort(out, axis=2)


def _coverage(pred_h: np.ndarray, actual: np.ndarray, lo: int, hi: int, k: float = 1.0) -> float:
    med = pred_h[:, MID]
    low = med + k * (pred_h[:, lo] - med)
    high = med + k * (pred_h[:, hi] - med)
    return float(np.mean((actual >= low) & (actual <= high)))


def _fit_scale(pred_h: np.ndarray, actual: np.ndarray, lo: int, hi: int, nominal: float) -> float:
    grid = np.round(np.arange(0.5, 4.01, 0.05), 2)
    for k in grid:
        if _coverage(pred_h, actual, lo, hi, k) >= nominal:
            return float(k)
    return float(grid[-1])


def _categories(levels: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return (levels[:, None] >= edges).sum(axis=1)


def _skill(err: np.ndarray, baseline: np.ndarray) -> float:
    return round(float(1 - err.mean() / baseline.mean()), 3) if baseline.mean() > 0 else 0.0


def evaluate(ds: Dataset, holdout_weeks: int = HOLDOUT_WEEKS, max_iter: int = 100) -> tuple[dict, Forecaster]:
    """Back-test on the most recent ``holdout_weeks``.

    Returns the metrics and an *unfitted* Forecaster carrying the fallback and
    calibration settings learned from the hold-out, ready to be fitted on all
    data. Headline skill scores are for the raw model, before any fallback.
    """
    df = ds.frame
    cutoff = df["date"].max() - pd.Timedelta(weeks=holdout_weeks)
    model = Forecaster(max_iter=max_iter).fit(ds, until=cutoff)
    lo90, lo50, _, hi50, hi90 = range(len(QUANTILES))

    settings = Forecaster(max_iter=max_iter)
    settings.fallback = {}
    by_horizon, by_country = [], {}
    base = ds.usable() & (df["date"] >= cutoff)
    for i, h in enumerate(HORIZONS):
        target = f"target_h{h}"
        test = df[base & df[target].notna()]
        if test.empty:
            continue
        actual = test[target].to_numpy()
        countries = test["country"].to_numpy()
        raw = model.predict_raw(test)[:, i, :]
        err_model = np.abs(actual - raw[:, MID])
        err_none = np.abs(actual)

        for country in np.unique(countries):
            rows = countries == country
            skill = _skill(err_model[rows], err_none[rows])
            uses_model = skill > 0
            if not uses_model:
                settings.fallback.setdefault(country, []).append(h)
            by_country.setdefault(country, []).append(
                {"horizon_weeks": h, "n": int(rows.sum()), "skill_vs_no_change": skill, "uses_model": uses_model}
            )

        pred = raw.copy()
        fell_back = np.isin(countries, [c for c, hs in settings.fallback.items() if h in hs])
        pred[fell_back] -= pred[fell_back, MID : MID + 1]
        err_final = np.abs(actual - pred[:, MID])
        k50 = _fit_scale(pred, actual, lo50, hi50, 0.5)
        k90 = _fit_scale(pred, actual, lo90, hi90, 0.9)
        settings.calibration[h] = (k50, k90)

        edges = test[list(CATEGORY_EDGES)].to_numpy()
        ys = test["ys"].to_numpy()
        cat_true = _categories(ys + actual, edges)
        cat_pred = _categories(ys + pred[:, MID], edges)
        cat_none = _categories(ys, edges)
        areas = (test["level_name"] != "site").to_numpy()
        by_horizon.append(
            {
                "horizon_weeks": h,
                "n": int(len(test)),
                "typical_error_pct": round(float(100 * (np.exp(err_final.mean()) - 1)), 1),
                "typical_error_pct_no_change": round(float(100 * (np.exp(err_none.mean()) - 1)), 1),
                "skill_vs_no_change": _skill(err_model, err_none),
                "skill_vs_no_change_areas": _skill(err_model[areas], err_none[areas]),
                "skill_vs_no_change_final": _skill(err_final, err_none),
                "category_accuracy": round(float(np.mean(cat_true == cat_pred)), 3),
                "category_accuracy_no_change": round(float(np.mean(cat_true == cat_none)), 3),
                "within_one_category": round(float(np.mean(np.abs(cat_true - cat_pred) <= 1)), 3),
                "coverage_50_raw": round(_coverage(pred, actual, lo50, hi50), 3),
                "coverage_90_raw": round(_coverage(pred, actual, lo90, hi90), 3),
                "calibration_50": k50,
                "calibration_90": k90,
            }
        )
    metrics = {
        "holdout_start": cutoff.date().isoformat(),
        "holdout_weeks": holdout_weeks,
        "train_rows_holdout_model": model.n_train_rows,
        "by_horizon": by_horizon,
        "by_country": by_country,
        "fallback": {c: sorted(hs) for c, hs in settings.fallback.items()},
    }
    return metrics, settings
