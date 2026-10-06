"""Hand-made archive lines and issues for the archive, scoring and gatekeeper tests."""

from __future__ import annotations

import gzip
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from wastewater import archive

T0 = datetime(2026, 10, 6, 5, 17, 0, tzinfo=timezone.utc)
PROBS = [0.05, 0.15, 0.5, 0.2, 0.1]


def lg(units: float, offset: float = 1.0) -> float:
    """A tail value: units on the log scale, rounded as the archive stores them."""
    return round(math.log(units + offset), 5)


def tail(as_of: str, units=None, offset: float = 1.0, flags: str | None = None, logs=None) -> dict:
    """13 weeks ending at ``as_of``; ``units`` is one value per week (None = missing)."""
    start = date.fromisoformat(as_of) - timedelta(weeks=archive.TAIL_WEEKS - 1)
    if logs is None:
        units = list(units) if units is not None else [15.0] * archive.TAIL_WEEKS
        logs = [None if u is None else lg(u, offset) for u in units]
    if flags is None:
        flags = "".join("-" if v is None else "." for v in logs)
    return {"start": start.isoformat(), "log": list(logs), "flags": flags}


def area(
    region: str = "r1",
    as_of: str = "2026-09-27",
    latest: float = 15.0,
    offset: float = 1.0,
    q=(5.0, 10.0, 25.0, 30.0, 50.0),
    probs=PROBS,
    hs=(1,),
    method: str = "model",
    thr=(10.0, 20.0, 30.0, 40.0),
    level: str = "region",
    tail_: dict | None = None,
) -> dict:
    d = date.fromisoformat(as_of)
    return {
        "type": "area",
        "region": region,
        "name": region.upper(),
        "level": level,
        "data_as_of": as_of,
        "source_date": as_of,
        "latest": latest,
        "offset": offset,
        "index": 30,
        "category": "low",
        "thr": list(thr),
        "tail": tail_ if tail_ is not None else tail(as_of, offset=offset),
        "f": [
            {"h": h, "date": (d + timedelta(weeks=h)).isoformat(), "q": list(q), "probs": list(probs),
             "index": 50, "category": "moderate", "p_lower": 0.2, "method": method}
            for h in hs
        ],
    }


def put(root: Path, when: datetime, lines: list[dict], virus: str = "covid", country: str = "alpha", **kw) -> Path:
    """Save an issue through the real writer; fails the test if nothing was written."""
    path = archive.write_issue(root, virus, country, kw.pop("header", {}), lines, now=when, **kw)
    assert path is not None, "nothing was written"
    return path


def raw_issue(path: Path, header: dict, lines: list, text_lines: list[str] | None = None) -> Path:
    """Write an issue file by hand, bypassing the writer's checks (for hostile or broken files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [json.dumps(header)] + [json.dumps(x) for x in lines] + list(text_lines or [])
    path.write_bytes(gzip.compress(("\n".join(rows) + "\n").encode(), mtime=0))
    return path


def header_for(virus: str, country: str, when: datetime, **over) -> dict:
    return {"type": "header", "schema": 2, "country": country, "virus": virus,
            "issued": archive.issued_text(when), "content_hash": "x", **over}
