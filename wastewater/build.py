"""Fetch every source, train the forecaster and write the site's JSON files.

Output (``site/data`` by default):

* ``index.json``: countries and the viruses each publishes, level definitions,
  and each virus model's back-test results
* ``<country>-<virus>.json``: every active area with history, latest status
  and forecast for one virus
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .http import Fetcher
from .model import HOLDOUT_WEEKS, HORIZONS, MODEL_SPECS, QUANTILES, Dataset, evaluate
from .preprocess import WINDOW_WEEKS, Prepared, percentile_rank, prepare
from .risk import CATEGORIES, category, category_probabilities, quantile_cdf, trend
from .sources import DEFAULT_COUNTRY, LEVELS, PATHOGENS, SOURCES, Source

log = logging.getLogger(__name__)

# An area is shown if it reported within this many weeks of its country's
# most recent data and has enough history to rank the current level.
ACTIVE_WITHIN_WEEKS = 6
# Single treatment plants are numerous; their pandemic-era history adds a lot
# of bytes and nothing to the two-year comparison, so it is trimmed.
SITE_HISTORY_START = pd.Timestamp("2022-01-02")


def sig(x, digits: int = 4):
    """Round to significant figures for compact JSON; NaN becomes null."""
    if x is None:
        return None
    x = float(x)
    if math.isnan(x) or math.isinf(x):
        return None
    return float(f"{x:.{digits}g}")


# Invisible formatting characters (bidi overrides, zero-width) that upstream
# names could use to make text display differently from what it says.
_INVISIBLE = re.compile("[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
MAX_TEXT = 120


def clean_text(value):
    """Upstream names as plain, bounded text: no control or bidi characters."""
    if value is None:
        return None
    text = _INVISIBLE.sub("", str(value)).strip()
    return text[:MAX_TEXT]


def iso(ts) -> str:
    return pd.Timestamp(ts).date().isoformat()


def _failure(exc: Exception) -> str:
    # Only the error type goes into the public JSON; details (URLs, paths,
    # proxy messages, upstream text) stay in the build log.
    return f"couldn't load the data ({type(exc).__name__})"


def _load(src: Source, fetcher: Fetcher) -> list[Prepared]:
    out: list[Prepared] = []
    for raw in src.fetch(fetcher):
        signal = src.info.signals.get(raw.pathogen)
        if signal is None:
            continue
        try:
            p = prepare(raw, src.info.id, src.info.hemisphere, lab=signal.kind == "lab tests")
        except Exception:  # one odd area shouldn't cost the whole country
            log.exception("%s: skipping %s/%s", src.info.id, raw.pathogen, raw.region_id)
            continue
        if p is not None:
            out.append(p)
    if not out:
        raise ValueError("no usable series")
    return out


def collect(sources: list[Source], fetcher: Fetcher) -> tuple[list[Prepared], list[dict]]:
    prepared: list[Prepared] = []
    errors: list[dict] = []
    for src in sources:
        started = time.time()
        try:  # one broken portal should not take the site down
            try:
                with fetcher.transaction() as tx:
                    got = _load(src, fetcher)
            except Exception:
                if not tx.restored:
                    raise
                # Fresh downloads didn't parse (a maintenance page served
                # as data, say), so use the copies from the last good build.
                log.exception("%s: new data didn't load; using the last good copy", src.info.id)
                with fetcher.cache_only():
                    got = _load(src, fetcher)
        except Exception as exc:
            log.exception("failed to load %s", src.info.id)
            errors.append({"country": src.info.id, "error": _failure(exc)})
            continue
        prepared.extend(got)
        log.info("%s: %d series in %.1fs", src.info.id, len(got), time.time() - started)
    return prepared, errors


def is_active(p: Prepared, country_latest: pd.Timestamp) -> bool:
    last = p.last_date
    if last is None or last < country_latest - pd.Timedelta(weeks=ACTIVE_WITHIN_WEEKS):
        return False
    return not math.isnan(p.index.loc[last])


def region_payload(p: Prepared, pred: np.ndarray | None, fallback: list[int]) -> dict:
    ys = p.ys
    last = p.last_date
    window = ys.loc[last - pd.Timedelta(weeks=WINDOW_WEEKS - 1) : last].to_numpy()
    window = window[~np.isnan(window)]
    edges_log = np.percentile(window, [20, 40, 60, 80])
    idx = float(p.index.loc[last])
    first = ys.index[0]
    if p.raw.level == "site":
        first = max(first, SITE_HISTORY_START)
    weekly, smooth, index = p.weekly.loc[first:], ys.loc[first:], p.index.loc[first:]

    payload = {
        "id": clean_text(p.raw.region_id),
        "name": clean_text(p.raw.name),
        "level": p.raw.level,
        "group": clean_text(p.raw.group),
        "parent": clean_text(p.raw.parent),
        "population": sig(p.raw.population, 6),
        "unit": p.raw.unit,
        "start": iso(first),
        "raw": [sig(v) for v in weekly.to_numpy()],
        "smooth": [sig(v) for v in p.to_units(smooth.to_numpy())],
        "index": [None if math.isnan(v) else round(v) for v in index.to_numpy()],
        "thresholds": [sig(v) for v in p.to_units(edges_log)],
        "window": {"min": sig(p.to_units(window.min())), "max": sig(p.to_units(window.max()))},
        "latest": {
            "date": iso(last),
            "source_date": iso(p.raw.values.index.max()),
            "value": sig(p.to_units(ys.loc[last])),
            "index": round(idx),
            "category": category(idx),
            "trend": trend(ys),
        },
        "forecast": [],
    }
    if pred is None:
        return payload

    current = float(ys.loc[last])
    for i, h in enumerate(HORIZONS):
        qlog = current + pred[i]
        med_idx = percentile_rank(qlog[QUANTILES.index(0.5)], window)
        payload["forecast"].append(
            {
                "date": iso(last + pd.Timedelta(weeks=h)),
                "h": h,
                "q": [sig(v) for v in p.to_units(qlog)],
                "index": round(med_idx),
                "category": category(med_idx),
                "probs": category_probabilities(qlog, edges_log),
                "p_lower": round(quantile_cdf(current, qlog), 3),
                "method": "no_change" if h in fallback else "model",
            }
        )
    return payload


def summarise(regions: list[dict]) -> dict:
    counts = {c["id"]: 0 for c in CATEGORIES}
    for r in regions:
        if r["level"] != "national" and r["latest"]["category"]:
            counts[r["latest"]["category"]] += 1
    return counts


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # hidden, and unique per build
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def build(
    country_ids: list[str] | None = None,
    out_dir: str | Path = "site/data",
    fetcher: Fetcher | None = None,
    holdout_weeks: int = HOLDOUT_WEEKS,
    max_iter: int | None = None,
    sources: list[Source] | None = None,
) -> dict:
    started = time.time()
    out = Path(out_dir)
    fetcher = fetcher or Fetcher()
    if sources is None:
        ids = country_ids or list(SOURCES)
        unknown = set(ids) - set(SOURCES)
        if unknown:
            raise ValueError(f"unknown countries: {', '.join(sorted(unknown))}")
        sources = [SOURCES[i]() for i in ids]
    infos = {s.info.id: s.info for s in sources}

    prepared, errors = collect(sources, fetcher)
    if not prepared:
        raise RuntimeError(f"no data could be loaded: {errors}")

    ds = Dataset.from_prepared(prepared)
    frame = ds.frame.set_index(["key", "date"])
    models: dict = {}
    published: dict = {cid: {} for cid in infos}

    for pathogen in PATHOGENS:
        of_virus = [p for p in prepared if p.pathogen == pathogen]
        if not of_virus:
            continue
        spec = MODEL_SPECS[pathogen]
        if max_iter is not None:
            spec = replace(spec, max_iter=max_iter)
        try:
            log.info("%s: back-testing on the last %d weeks", pathogen, holdout_weeks)
            metrics, forecaster = evaluate(ds, pathogen, spec, holdout_weeks=holdout_weeks)
            log.info("%s: training the final model on all data", pathogen)
            forecaster.fit(ds)
        except Exception as exc:  # keep the other viruses
            log.exception("%s: model failed", pathogen)
            errors.append({"virus": pathogen, "error": f"couldn't train the forecast ({type(exc).__name__})"})
            continue
        models[pathogen] = {
            **metrics,
            "design": spec.name,
            "training": {"rounds": spec.max_iter, "learning_rate": spec.learning_rate},
            "features": spec.columns(),
            "learns_from_all_viruses": spec.pool_viruses,
            "n_series": len(of_virus),
            "train_rows": forecaster.n_train_rows,
        }

        for cid, info in infos.items():
            series = [p for p in of_virus if p.country == cid]
            if not series:
                continue
            latest = max(p.last_date for p in series)
            active = [p for p in series if is_active(p, latest)]
            if not active:
                errors.append({"country": cid, "virus": pathogen, "error": "no area has recent data"})
                continue

            try:  # a problem in one country's data shouldn't stop the others
                rows = frame.loc[[(p.key, p.last_date) for p in active]].reset_index()
                has_features = rows["d1"].notna().to_numpy()
                preds = np.full((len(rows), len(HORIZONS), len(QUANTILES)), np.nan)
                if has_features.any():
                    preds[has_features] = forecaster.predict(rows[has_features])
                fallback = forecaster.fallback.get(cid, [])
                regions = [
                    region_payload(p, preds[i] if has_features[i] else None, fallback)
                    for i, p in enumerate(active)
                ]
                regions.sort(key=lambda r: (LEVELS.index(r["level"]), r["name"].lower()))
                national = next((r for r in regions if r["level"] == "national"), regions[0])

                file = f"{cid}-{pathogen}.json"
                write_json(out / file, {"country": cid, "virus": pathogen, "generated_at": _now(), "regions": regions})
                signal = info.signals[pathogen]
                published[cid][pathogen] = {
                    "file": file,
                    "latest_date": iso(latest),
                    "default_region": national["id"],
                    "n_regions": len(regions),
                    "summary": summarise(regions),
                    "national": {k: national[k] for k in ("id", "name")} | {"latest": national["latest"]},
                    "signal": {
                        "kind": signal.kind,
                        "metric": signal.metric,
                        "unit": signal.unit,
                        "notes": signal.notes,
                    },
                }
            except Exception as exc:
                log.exception("%s: couldn't publish %s", cid, pathogen)
                errors.append({"country": cid, "virus": pathogen, "error": _failure(exc)})

    countries_out = [
        {
            "id": cid,
            "name": info.name,
            "flag": info.flag,
            "source": {"publisher": info.publisher, "url": info.url, "license": info.license},
            "viruses": published[cid],
        }
        for cid, info in infos.items()
        if published[cid]
    ]
    index = {
        "generated_at": _now(),
        "version": __version__,
        "default_country": DEFAULT_COUNTRY if any(c["id"] == DEFAULT_COUNTRY for c in countries_out)
        else (countries_out[0]["id"] if countries_out else None),
        "viruses": [{"id": v, "label": PATHOGENS[v]} for v in PATHOGENS if v in models],
        "categories": CATEGORIES,
        "horizons": list(HORIZONS),
        "quantiles": list(QUANTILES),
        "countries": countries_out,
        "models": models,
        "errors": errors,
    }
    write_json(out / "index.json", index)
    log.info("wrote %d countries to %s in %.0fs", len(countries_out), out, time.time() - started)
    return index


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
