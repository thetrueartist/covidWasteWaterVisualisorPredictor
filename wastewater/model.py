"""Probabilistic forecasters for wastewater levels, one per virus.

Each virus (COVID-19, flu, RSV) gets its own set of models. Within a virus,
one "global" model is trained across every area in every country at once,
which gives it far more waves to learn from than any single area has. For
each forecast horizon (1-6 weeks ahead) and each quantile, a gradient-boosted
tree model predicts how much an area's smoothed log level will change.
Features are all relative (recent changes in the area, in its country's
national series and across all of the country's areas), so areas measured in
completely different units can share one model.

Which inputs each virus's model uses is chosen by back-testing, not
assumption: ``compare`` scores every candidate in ``CANDIDATES`` on two
separate held-out years and ``MODEL_SPECS`` records the winners (run
``python -m wastewater compare`` to repeat it).

Honesty checks, in ``evaluate``:

* the most recent year is held out; the model is trained only on data that
  was available before it and scored against a "no change" baseline;
* per country and horizon, if the model did not beat "no change" in that
  hold-out, forecasts for that country fall back to "no change" (keeping the
  model's uncertainty band);
* the hold-out also widens or narrows the 50% and 90% ranges so that they
  cover about 50% and 90% of outcomes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

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
VIRUS_CODES = {"covid": 0, "flu": 1, "rsv": 2}
CATEGORY_EDGES = ("thr20", "thr40", "thr60", "thr80")
MID = QUANTILES.index(0.5)

OWN_FEATURES = ["d1", "d2", "d4", "d8", "accel", "raw_dev", "vol", "level", "lab"]
NATIONAL_FEATURES = ["nat_d1", "nat_d2", "nat_d4"]
CONSENSUS_FEATURES = ["cons_d1", "cons_d2", "cons_d4"]
TREND_FEATURES = OWN_FEATURES + NATIONAL_FEATURES + CONSENSUS_FEATURES
SEASON_FEATURES = ["season_sin", "season_cos"]
LEVEL_FEATURES = ["rank", "z", "from_max", "from_min", "range_pos", "weeks_since_max"]


@dataclass(frozen=True)
class ModelSpec:
    """Which inputs a virus's model uses and how it is trained."""

    name: str
    features: tuple
    pool_viruses: bool = False  # also learn from the other viruses' series
    max_iter: int = 100
    learning_rate: float = 0.1
    min_samples_leaf: int = 80

    def columns(self) -> list[str]:
        return [*self.features, "virus_code"] if self.pool_viruses else list(self.features)


def _candidate(name: str, features: list[str], pool: bool = False) -> ModelSpec:
    return ModelSpec(name, tuple(features), pool_viruses=pool)


CANDIDATES = {
    spec.name: spec
    for spec in (
        _candidate("trend", TREND_FEATURES),
        _candidate("trend+season", TREND_FEATURES + SEASON_FEATURES),
        _candidate("trend+level", TREND_FEATURES + LEVEL_FEATURES),
        _candidate("trend+season+level", TREND_FEATURES + SEASON_FEATURES + LEVEL_FEATURES),
        _candidate("trend, all viruses", TREND_FEATURES, pool=True),
        _candidate("trend+season, all viruses", TREND_FEATURES + SEASON_FEATURES, pool=True),
        _candidate("trend+level, all viruses", TREND_FEATURES + LEVEL_FEATURES, pool=True),
        _candidate("trend+season+level, all viruses", TREND_FEATURES + SEASON_FEATURES + LEVEL_FEATURES, pool=True),
    )
}

# Chosen with ``compare`` on two consecutive held-out years (Sep 2024-Sep 2025
# and Sep 2025-Sep 2026), average skill vs "no change" at 1-6 weeks:
#   COVID  trend +5.3%   ->  trend+level, all viruses +8.8% (better in both years;
#          seasonality made COVID worse in the earlier year, down to -7.6%)
#   flu    trend +15.1%  ->  trend+season+level +21.6% (best in both years)
#   RSV    trend +15.6%  ->  trend+level +23.1% (season helped one year, hurt the other)
# Then 200 rounds at learning rate 0.05 were equal or slightly better than
# 100 at 0.1 for all three, in both years.
_TUNED = {"max_iter": 200, "learning_rate": 0.05}
MODEL_SPECS = {
    "covid": replace(CANDIDATES["trend+level, all viruses"], **_TUNED),
    "flu": replace(CANDIDATES["trend+season+level"], **_TUNED),
    "rsv": replace(CANDIDATES["trend+level"], **_TUNED),
}


def _weeks_since_max(window: np.ndarray) -> float:
    return float(len(window) - 1 - np.nanargmax(window))


def series_frame(p: Prepared) -> pd.DataFrame:
    """Features, targets and bookkeeping columns for every week of one series."""
    ys, y = p.ys, p.y
    f = pd.DataFrame(index=ys.index)
    for k in (1, 2, 4, 8):
        f[f"d{k}"] = ys - ys.shift(k)
    f["accel"] = f["d1"] - f["d1"].shift(1)
    f["raw_dev"] = y - ys
    f["vol"] = ys.diff().rolling(12, min_periods=4).std()
    f["level"] = LEVEL_CODES[p.raw.level]
    f["lab"] = 1 if p.lab else 0
    f["virus_code"] = VIRUS_CODES[p.pathogen]

    week = ys.index.isocalendar().week.to_numpy(dtype=float)
    if p.hemisphere == "S":
        week = (week + 26) % 52
    f["season_sin"] = np.sin(2 * np.pi * week / 52.18)
    f["season_cos"] = np.cos(2 * np.pi * week / 52.18)

    roll = ys.rolling(WINDOW_WEEKS, min_periods=MIN_WINDOW)
    rmax, rmin = roll.max(), roll.min()
    f["rank"] = p.index / 100.0
    f["z"] = (ys - roll.median()) / roll.std().replace(0, np.nan)
    f["from_max"] = ys - rmax
    f["from_min"] = ys - rmin
    f["range_pos"] = f["from_min"] / (rmax - rmin).replace(0, np.nan)
    f["weeks_since_max"] = ys.rolling(52, min_periods=8).apply(_weeks_since_max, raw=True)

    for q, col in zip((0.2, 0.4, 0.6, 0.8), CATEGORY_EDGES):
        f[col] = roll.quantile(q)
    f["ys"] = ys
    f["pct"] = p.index
    for h in HORIZONS:
        f[f"target_h{h}"] = ys.shift(-h) - ys
    f["key"] = p.key
    f["country"] = p.country
    f["pathogen"] = p.pathogen
    f["level_name"] = p.raw.level
    f["date"] = f.index
    return f.reset_index(drop=True)


def add_shared_features(df: pd.DataFrame) -> pd.DataFrame:
    """Attach the national trend and the median trend of the country's areas, per virus."""
    trend = ["d1", "d2", "d4"]
    keys = ["country", "pathogen", "date"]
    nat = df.loc[df["level_name"] == "national", [*keys, *trend]]
    nat = nat.drop_duplicates(keys).rename(columns={c: f"nat_{c}" for c in trend})
    cons = df.groupby(keys, as_index=False)[trend].median()
    cons = cons.rename(columns={c: f"cons_{c}" for c in trend})
    return df.merge(nat, on=keys, how="left").merge(cons, on=keys, how="left")


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

    def latest(self, pathogen: str) -> pd.Timestamp:
        return self.frame.loc[self.frame["pathogen"] == pathogen, "date"].max()


def site_sample(df: pd.DataFrame) -> np.ndarray:
    """Keep every non-plant row and a fixed ~35% of plant rows.

    The sample depends only on each row's series and date, so a virus's
    training set doesn't change when other viruses are added.
    """
    h = pd.util.hash_pandas_object(df[["key", "date"]], index=False).to_numpy()
    return (df["level_name"] != "site").to_numpy() | ((h % 10_000) < SITE_SAMPLE * 10_000)


def _new_model(spec: ModelSpec, q: float, seed: int) -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        loss="quantile",
        quantile=q,
        learning_rate=spec.learning_rate,
        max_iter=spec.max_iter,
        max_leaf_nodes=31,
        min_samples_leaf=spec.min_samples_leaf,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=seed,
    )


def _train_mask(ds: Dataset, pathogen: str, spec: ModelSpec, until: pd.Timestamp | None, h: int) -> pd.Series:
    df = ds.frame
    mask = ds.usable() & pd.Series(site_sample(df), index=df.index) & df[f"target_h{h}"].notna()
    if not spec.pool_viruses:
        mask &= df["pathogen"] == pathogen
    if until is not None:
        mask &= df["date"] + pd.Timedelta(weeks=h) <= until
    return mask


@dataclass
class Forecaster:
    pathogen: str = "covid"
    spec: ModelSpec = field(default_factory=lambda: MODEL_SPECS["covid"])
    quantiles: tuple = QUANTILES
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
        cols = self.spec.columns()
        for h in HORIZONS:
            mask = _train_mask(ds, self.pathogen, self.spec, until, h)
            X, y = df.loc[mask, cols], df.loc[mask, f"target_h{h}"]
            self.n_train_rows = max(self.n_train_rows, int(mask.sum()))
            log.info("%s: fitting %d-week horizon on %d rows", self.pathogen, h, len(X))
            for q in self.quantiles:
                self.models[(h, q)] = _new_model(self.spec, q, self.seed).fit(X, y)
        return self

    def predict_raw(self, X: pd.DataFrame) -> np.ndarray:
        """Predicted change in log level, shape (rows, horizons, quantiles)."""
        cols = self.spec.columns()
        out = np.empty((len(X), len(HORIZONS), len(self.quantiles)))
        for i, h in enumerate(HORIZONS):
            for j, q in enumerate(self.quantiles):
                out[:, i, j] = self.models[(h, q)].predict(X[cols])
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


def _test_rows(ds: Dataset, pathogen: str, start: pd.Timestamp, end: pd.Timestamp | None, h: int) -> pd.DataFrame:
    """Forecast origins from ``start`` on whose target week is no later than ``end``."""
    df = ds.frame
    mask = ds.usable() & (df["pathogen"] == pathogen) & (df["date"] >= start) & df[f"target_h{h}"].notna()
    if end is not None:
        mask &= df["date"] + pd.Timedelta(weeks=h) <= end
    return df[mask]


def evaluate(
    ds: Dataset,
    pathogen: str = "covid",
    spec: ModelSpec | None = None,
    holdout_weeks: int = HOLDOUT_WEEKS,
) -> tuple[dict, Forecaster]:
    """Back-test one virus's model on its most recent ``holdout_weeks``.

    Returns the metrics and an *unfitted* Forecaster carrying the fallback and
    calibration settings learned from the hold-out, ready to be fitted on all
    data. Headline skill scores are for the raw model, before any fallback.
    """
    spec = spec or MODEL_SPECS[pathogen]
    cutoff = ds.latest(pathogen) - pd.Timedelta(weeks=holdout_weeks)
    model = Forecaster(pathogen, spec).fit(ds, until=cutoff)
    lo90, lo50, _, hi50, hi90 = range(len(QUANTILES))

    settings = Forecaster(pathogen, spec)
    settings.fallback = {}
    by_horizon, by_country = [], {}
    for i, h in enumerate(HORIZONS):
        target = f"target_h{h}"
        test = _test_rows(ds, pathogen, cutoff, None, h)
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
                "skill_vs_no_change_areas": _skill(err_model[areas], err_none[areas]) if areas.any() else None,
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


def compare(
    ds: Dataset,
    pathogen: str,
    candidates: dict | None = None,
    folds: int = 2,
    fold_weeks: int = 52,
) -> list[dict]:
    """Score candidate model designs for one virus on separate held-out years.

    Fold k holds out the year ending k years before the latest data, trains
    only on what came before it, and scores the median forecast against "no
    change" at every horizon. Folds are consecutive, so each contains a full
    winter season.
    """
    candidates = candidates or CANDIDATES
    latest = ds.latest(pathogen)
    results = []
    for k in range(folds):
        end = latest - pd.Timedelta(weeks=fold_weeks * k)
        start = end - pd.Timedelta(weeks=fold_weeks)
        tests = {h: _test_rows(ds, pathogen, start, end, h) for h in HORIZONS}
        if all(t.empty for t in tests.values()):
            continue
        for name, spec in candidates.items():
            model = Forecaster(pathogen, spec, quantiles=(0.5,)).fit(ds, until=start)
            row = {"pathogen": pathogen, "candidate": name, "fold": k, "start": start.date().isoformat(),
                   "end": end.date().isoformat()}
            by_country: dict = {}
            for i, h in enumerate(HORIZONS):
                test = tests[h]
                if test.empty:
                    continue
                actual = test[f"target_h{h}"].to_numpy()
                pred = model.predict_raw(test)[:, i, 0]
                err, none = np.abs(actual - pred), np.abs(actual)
                row[f"h{h}"] = _skill(err, none)
                row[f"n{h}"] = int(len(test))
                for country in test["country"].unique():
                    rows = (test["country"] == country).to_numpy()
                    by_country.setdefault(country, []).append(_skill(err[rows], none[rows]))
            row["by_country"] = {c: round(float(np.mean(v)), 3) for c, v in by_country.items()}
            results.append(row)
            log.info("%s fold %d %-34s %s", pathogen, k, name,
                     " ".join(f"{row.get(f'h{h}', float('nan')):+.3f}" for h in HORIZONS))
    return results
