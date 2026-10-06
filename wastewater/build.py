"""Fetch every source, train the forecaster and write the site's JSON files.

Output (``site/data`` by default):

* ``index.json``: countries and the viruses each publishes, level definitions,
  and each virus model's back-test results
* ``<country>-<virus>.json``: every active area with history, latest status
  and forecast for one virus

With an archive folder, every forecast published is also saved there (see
wastewater/archive.py); ``python -m wastewater score`` turns that archive
into the live track record. Such a build publishes no forecast for an area
whose newest data is too old for the archive to take, so nothing it
publishes goes unsaved.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
import unicodedata
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from . import archive as forecast_archive
from .http import Fetcher
from .model import HOLDOUT_WEEKS, HORIZONS, MODEL_SPECS, QUANTILES, Dataset, evaluate
from .preprocess import WINDOW_WEEKS, Prepared, percentile_rank, prepare
from .risk import CATEGORIES, category, category_probabilities, quantile_cdf, trend
from .sources import DEFAULT_COUNTRY, LEVELS, PATHOGENS, SOURCES, Source
from .sources.base import unique_ids

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# An area is shown if it reported within this many weeks of its country's
# most recent data and has enough history to rank the current level. (The
# scorer uses the same line to tell an area that has stopped reporting.)
ACTIVE_WITHIN_WEEKS = forecast_archive.ACTIVE_WITHIN_WEEKS
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


# Unicode categories of characters that don't display as themselves: controls,
# formatting (bidi marks and overrides, zero-width), line/paragraph separators,
# private use and surrogates. Upstream names could use them to make text
# display differently from what it says.
_HIDDEN_CATEGORIES = {"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"}
MAX_TEXT = 120


def clean_text(value):
    """Upstream names as plain, bounded text: no control, bidi or invisible characters."""
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value))
    text = "".join(ch for ch in text if unicodedata.category(ch) not in _HIDDEN_CATEGORIES)
    return " ".join(text.split())[:MAX_TEXT]


def iso(ts) -> str:
    return pd.Timestamp(ts).date().isoformat()


def _failure(exc: Exception) -> str:
    # Only the error type goes into the public JSON; details (URLs, paths,
    # proxy messages, upstream text) stay in the build log.
    return f"couldn't load the data ({type(exc).__name__})"


def _load(src: Source, fetch, fetcher: Fetcher) -> list[Prepared]:
    out: list[Prepared] = []
    raws = fetch(fetcher)
    for raw in raws:  # published ids are plain and short; shorten first so suffixes stay unique
        raw.region_id = (clean_text(raw.region_id) or "area")[:100]
    # Ids must be unique per virus within a country, whatever the parser did.
    for raw in unique_ids(raws):
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


def _load_with_fallback(src: Source, fetch, fetcher: Fetcher) -> list[Prepared]:
    try:
        with fetcher.transaction() as tx:
            return _load(src, fetch, fetcher)
    except Exception:
        if not tx.restored:
            raise
        # Fresh downloads didn't parse (a maintenance page served as data,
        # say), so use the copies from the last good build.
        log.exception("%s: new data didn't load; using the last good copy", src.info.id)
        with fetcher.cache_only():
            return _load(src, fetch, fetcher)


def collect(
    sources: list[Source], fetcher: Fetcher, inputs: dict[str, list[dict]] | None = None
) -> tuple[list[Prepared], list[dict]]:
    """Load every source. With ``inputs``, also note which upstream files each
    country's data came from: ``inputs[country]`` lists their URL, SHA-256,
    Last-Modified and whether the cached copy was used."""
    prepared: list[Prepared] = []
    errors: list[dict] = []
    for src in sources:
        started = time.time()
        before = dict(getattr(fetcher, "provenance", {}))
        parts = src.parts()
        n = 0
        for viruses, fetch in parts:
            try:  # one broken portal, or one broken file, should not take the site down
                got = _load_with_fallback(src, fetch, fetcher)
            except Exception as exc:
                log.exception("failed to load %s (%s)", src.info.id, ", ".join(viruses))
                if len(parts) == 1:
                    errors.append({"country": src.info.id, "error": _failure(exc)})
                else:
                    errors.extend({"country": src.info.id, "virus": v, "error": _failure(exc)} for v in viruses)
                continue
            prepared.extend(got)
            n += len(got)
        if n:
            log.info("%s: %d series in %.1fs", src.info.id, n, time.time() - started)
        if inputs is not None:
            # Each request records a new entry, so anything new or replaced came from this source.
            after = getattr(fetcher, "provenance", {})
            inputs[src.info.id] = [
                {"url": url, **rec} for url, rec in sorted(after.items()) if before.get(url) is not rec
            ]
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


def area_tail(p: Prepared) -> dict:
    """The area's smoothed log level for the archive's 13 weeks ending at its newest data.

    Each week gets a flag: ``.`` measured, ``z`` measured as 0 (below
    detection), ``i`` filled in between measurements, ``-`` missing.
    """
    last = p.last_date
    weeks = pd.DatetimeIndex([last - pd.Timedelta(weeks=k) for k in range(forecast_archive.TAIL_WEEKS - 1, -1, -1)])
    ys, raw = p.ys.reindex(weeks), p.weekly.reindex(weeks)
    values, flags = [], []
    for v, r in zip(ys.to_numpy(dtype=float), raw.to_numpy(dtype=float)):
        if not np.isfinite(v):
            values.append(None)
            flags.append("-")
            continue
        values.append(round(float(v), 5))
        flags.append("i" if np.isnan(r) else "z" if r == 0 else ".")
    return {"start": iso(weeks[0]), "log": values, "flags": "".join(flags)}


def area_line(region: dict, p: Prepared) -> dict:
    """One archive line: what the site published for an area, plus what's needed to score it."""
    latest = region["latest"]
    return {
        "type": "area",
        "region": region["id"],
        "name": region["name"],
        "level": region["level"],
        "data_as_of": latest["date"],
        "source_date": latest["source_date"],
        "latest": latest["value"],
        "offset": sig(p.offset, 8),
        "index": latest["index"],
        "category": latest["category"],
        "thr": region["thresholds"],
        "tail": area_tail(p),
        "f": [
            {k: f[k] for k in ("h", "date", "q", "probs", "index", "category", "p_lower", "method")}
            for f in region["forecast"]
        ],
    }


def summarise(regions: list[dict]) -> dict:
    counts = {c["id"]: 0 for c in CATEGORIES}
    for r in regions:
        if r["level"] != "national" and r["latest"]["category"]:
            counts[r["latest"]["category"]] += 1
    return counts


def _finite(obj):
    """NaN and infinity become null: browsers can't parse them in JSON."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if isinstance(obj, np.generic):
        return _finite(obj.item())
    return obj


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_finite(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")  # hidden, and unique per build
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def build(
    country_ids: list[str] | None = None,
    out_dir: str | Path = "site/data",
    fetcher: Fetcher | None = None,
    holdout_weeks: int = HOLDOUT_WEEKS,
    max_iter: int | None = None,
    sources: list[Source] | None = None,
    archive: str | Path | None = None,
    archive_new: str | Path | None = None,
) -> dict:
    """Build the site data.

    With ``archive``, each country and virus's published forecasts are also
    saved there as a new issue, unless they're the same as the newest one
    saved (see wastewater/archive.py). New files are copied to
    ``archive_new`` too, if given. A problem saving is logged, never fatal.
    """
    started = time.time()
    out = Path(out_dir)
    issued = _issue_time()
    fetcher = fetcher or Fetcher()
    if sources is None:
        ids = country_ids or list(SOURCES)
        unknown = set(ids) - set(SOURCES)
        if unknown:
            raise ValueError(f"unknown countries: {', '.join(sorted(unknown))}")
        sources = [SOURCES[i]() for i in ids]
    infos = {s.info.id: s.info for s in sources}

    inputs: dict[str, list[dict]] = {}
    prepared, errors = collect(sources, fetcher, inputs)
    if not prepared:
        raise RuntimeError(f"no data could be loaded: {errors}")

    ds = Dataset.from_prepared(prepared)
    frame = ds.frame.set_index(["key", "date"])
    models: dict = {}
    published: dict = {cid: {} for cid in infos}
    code = forecast_archive.code_fingerprint(REPO_ROOT) if archive is not None else {}

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
                if len(rows) != len(active) or list(rows["key"]) != [p.key for p in active]:
                    raise ValueError("feature rows don't line up with the areas")
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
                if archive is not None:
                    withhold_unsavable(regions, issued, f"{cid}/{pathogen}")
                national = next((r for r in regions if r["level"] == "national"), regions[0])

                file = f"{cid}-{pathogen}.json"
                write_json(out / file, {"country": cid, "virus": pathogen, "generated_at": _now(), "regions": regions})
                if archive is not None:
                    header = {
                        **code,
                        "design": spec.name,
                        "features": spec.columns(),
                        "trained_through": iso(forecaster.trained_through) if forecaster.trained_through is not None else None,
                        "holdout_start": metrics["holdout_start"],
                        "calibration": {str(h): [float(k) for k in ks] for h, ks in sorted(forecaster.calibration.items())},
                        "fallback": sorted(fallback),
                        "level_definition": forecast_archive.LEVEL_DEFINITION,
                        "inputs": inputs.get(cid, []),
                    }
                    _save_issue(archive, archive_new, cid, pathogen, regions, active, header, issued)
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
    if archive is None:
        # Not recording: say so, rather than leave the site asking for a file that isn't there.
        # (When recording, `python -m wastewater score` writes the real one.)
        write_json(out / "track-record.json", {"schema": 1, "recording": False, "generated_at": _now()})
    log.info("wrote %d countries to %s in %.0fs", len(countries_out), out, time.time() - started)
    return index


def withhold_unsavable(regions: list[dict], issued: datetime, what: str = "") -> None:
    """Drop the forecasts of areas whose newest data is too old, or dated too far ahead, to save.

    The archive leaves those out (archive.fresh_enough), so a build that
    saves its forecasts doesn't publish them either: everything it publishes
    is on record. Their data is still shown, with a note saying why there's
    no forecast. Data more than 35 days old leaves most of the six weeks
    ahead already past; data more than 7 days ahead can only come from a
    date in the next calendar week (a publisher's typo, say).
    """
    withheld = {"old_data": 0, "future_data": 0}
    for r in regions:
        as_of = date.fromisoformat(r["latest"]["date"])
        if r["forecast"] and not forecast_archive.fresh_enough(as_of, issued.date()):
            why = "old_data" if as_of < issued.date() else "future_data"
            r["forecast"] = []
            r["no_forecast"] = why
            withheld[why] += 1
    if withheld["old_data"]:
        log.info("%s: no forecast for %d areas whose newest data is more than %d days old", what, withheld["old_data"],
                 forecast_archive.MAX_DATA_AGE_DAYS)
    if withheld["future_data"]:
        log.info("%s: no forecast for %d areas whose newest data is more than %d days ahead", what,
                 withheld["future_data"], forecast_archive.MAX_DATA_AHEAD_DAYS)


def _save_issue(archive, archive_new, cid: str, pathogen: str, regions: list[dict], active: list[Prepared],
                header: dict, issued: datetime) -> None:
    """Save what was just published as a new issue, if it changed (never fatal)."""
    try:
        by_id = {clean_text(p.raw.region_id): p for p in active}
        lines = [area_line(r, by_id[r["id"]]) for r in regions if r["forecast"] and r["id"] in by_id]
        path = forecast_archive.write_issue(archive, pathogen, cid, header, lines, now=issued, new_root=archive_new)
        if path is not None:
            log.info("saved %s/%s forecasts to %s", cid, pathogen, path)
        else:
            log.info("%s/%s: nothing new to save", cid, pathogen)
    except Exception:
        log.exception("%s: couldn't save the %s forecasts to the archive", cid, pathogen)


def _issue_time() -> datetime:
    """When this build's forecasts were issued (one time for the whole build)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
