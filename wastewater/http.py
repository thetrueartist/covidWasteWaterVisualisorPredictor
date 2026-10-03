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
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urljoin, urlsplit

import requests

log = logging.getLogger(__name__)

# Largest download accepted. The biggest real file (RKI's per-plant data) is
# about 55 MB; anything far beyond that is not a normal response.
MAX_BYTES = 400 * 1024 * 1024

# The only hosts the build downloads from. A new data source adds its host here.
ALLOWED_HOSTS = frozenset(
    {
        "www.opendata.nhs.scot",  # Scotland
        "data.cdc.gov",  # United States
        "health-infobase.canada.ca",  # Canada
        "raw.githubusercontent.com",  # Germany, New Zealand
        "data.rivm.nl",  # Netherlands
    }
)
MAX_REDIRECTS = 3

# How old a cached copy may be and still stand in for a failed download.
# Past this, the country shows an error instead of quietly going stale.
STALE_LIMIT_DAYS = 30

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
        self._backups: dict[Path, Path | None] | None = None
        self._cache_only = False

    def _cache_path(self, url: str, params: Mapping[str, Any] | None, tag: str | None = None) -> Path:
        key = url
        if params:
            key += "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
        if tag:  # a separate cached copy of the same URL, e.g. one per source part
            key += "#" + tag
        return self.cache_dir / hashlib.sha256(key.encode()).hexdigest()[:32]

    def get_bytes(self, url: str, params: Mapping[str, Any] | None = None, tag: str | None = None) -> bytes:
        path = self._cache_path(url, params, tag)
        if path.exists():
            age = time.time() - path.stat().st_mtime
            # A modification time in the future isn't trusted as fresh.
            if self.offline or 0 <= age < self.max_age:
                return path.read_bytes()
            if self._cache_only and 0 <= age <= STALE_LIMIT_DAYS * 86400:
                return path.read_bytes()
        if self.offline or self._cache_only:
            raise FileNotFoundError(f"no usable cached copy of {url}")

        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                content = self._download(url, params)
                self._store(path, content)
                return content
            except requests.RequestException as exc:
                last_error = exc
                log.warning("download failed (%s/%s) %s: %s", attempt + 1, self.retries, url, exc)
                if attempt + 1 < self.retries:
                    time.sleep(2 ** (attempt + 1))
            except ValueError as exc:  # refused (not HTTPS, unknown host, too big): retrying won't help
                last_error = exc
                log.warning("download refused %s: %s", url, exc)
                break

        if path.exists():
            age_days = (time.time() - path.stat().st_mtime) / 86400
            if 0 <= age_days <= STALE_LIMIT_DAYS:
                log.warning("using the cached copy of %s from %.1f days ago", url, age_days)
                return path.read_bytes()
            log.warning("cached copy of %s is too old to use (%.0f days)", url, age_days)
        raise RuntimeError(f"could not download {url}") from last_error

    def _store(self, path: Path, content: bytes) -> None:
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._backups is not None and path not in self._backups:
            if path.exists():
                backup = path.with_name(path.name + ".prev")
                os.replace(path, backup)
                self._backups[path] = backup
            else:
                self._backups[path] = None
        # A per-process temporary name, so two builds at once can't trip over each other.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(content)
        tmp.replace(path)

    @contextmanager
    def transaction(self) -> Iterator["Transaction"]:
        """Keep the previous cached copies until the caller has parsed the new ones.

        If the block raises (say a portal answered 200 with an error page),
        the new downloads are dropped and the last good copies put back, so
        ``cache_only()`` can rebuild from them.
        """
        tx = Transaction()
        self._backups = {}
        try:
            yield tx
        except BaseException:
            tx.restored = self._roll_back()
            raise
        else:
            for backup in self._backups.values():
                if backup is not None:
                    backup.unlink(missing_ok=True)
        finally:
            self._backups = None

    def _roll_back(self) -> int:
        restored = 0
        for path, backup in self._backups.items():
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backup, path)
                restored += 1
        return restored

    @contextmanager
    def cache_only(self) -> Iterator[None]:
        """Serve everything from the cache, if it's no older than the stale limit."""
        self._cache_only = True
        try:
            yield
        finally:
            self._cache_only = False

    def _download(self, url: str, params: Mapping[str, Any] | None) -> bytes:
        """GET from an allowed HTTPS host, checking every redirect and capping the size."""
        for _ in range(MAX_REDIRECTS + 1):
            _check_url(url)
            with self._session.get(
                url, params=params, timeout=self.timeout, stream=True, allow_redirects=False
            ) as resp:
                if resp.is_redirect:
                    url = urljoin(resp.url, resp.headers["Location"])
                    params = None  # already part of the redirect target
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError(f"response from {url} is larger than {MAX_BYTES // 2**20} MB")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise ValueError(f"too many redirects fetching {url}")

    def get_text(self, url: str, params: Mapping[str, Any] | None = None, tag: str | None = None) -> str:
        return self.get_bytes(url, params, tag).decode("utf-8-sig")

    def get_json(self, url: str, params: Mapping[str, Any] | None = None, tag: str | None = None) -> Any:
        return json.loads(self.get_bytes(url, params, tag))


class Transaction:
    """Result of ``Fetcher.transaction()``: how many cached copies were put back."""

    restored = 0


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"refusing non-HTTPS URL {url}")
    if parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError(f"refusing URL outside the allowed hosts: {url}")
