"""Cached HTTP downloads shared by every data source.

Responses are cached on disk so repeated builds are fast and so a build can
fall back to the last good copy when an upstream portal is briefly down.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "covid-wastewater-visualiser/0.1 "
    "(+https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor)"
)


class Fetcher:
    """Downloads URLs with retries and a time-limited on-disk cache.

    ``offline=True`` serves only from the cache, which is handy for working on
    the model or the site without hammering public data portals.
    """

    def __init__(
        self,
        cache_dir: str | os.PathLike = None,
        max_age_hours: float = 6.0,
        offline: bool = False,
        timeout: float = 180.0,
        retries: int = 3,
    ) -> None:
        self.cache_dir = Path(cache_dir or os.environ.get("WASTEWATER_CACHE", ".cache"))
        self.max_age = max_age_hours * 3600
        self.offline = offline
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def _cache_path(self, url: str, params: Mapping[str, Any] | None) -> Path:
        key = url
        if params:
            key += "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        return self.cache_dir / hashlib.sha256(key.encode()).hexdigest()[:32]

    def get_bytes(self, url: str, params: Mapping[str, Any] | None = None) -> bytes:
        path = self._cache_path(url, params)
        if path.exists():
            age = time.time() - path.stat().st_mtime
            if self.offline or age < self.max_age:
                return path.read_bytes()
        if self.offline:
            raise FileNotFoundError(f"offline mode and no cached copy of {url}")

        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(resp.content)
                tmp.replace(path)
                return resp.content
            except requests.RequestException as exc:
                last_error = exc
                log.warning("download failed (%s/%s) %s: %s", attempt + 1, self.retries, url, exc)
                if attempt + 1 < self.retries:
                    time.sleep(2 ** (attempt + 1))

        if path.exists():
            log.warning("using stale cached copy of %s", url)
            return path.read_bytes()
        raise RuntimeError(f"could not download {url}") from last_error

    def get_text(self, url: str, params: Mapping[str, Any] | None = None) -> str:
        return self.get_bytes(url, params).decode("utf-8-sig")

    def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> Any:
        return json.loads(self.get_bytes(url, params))
