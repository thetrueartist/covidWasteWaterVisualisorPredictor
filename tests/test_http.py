"""Downloads: allowed HTTPS hosts only, size-capped, cached, and falling back to the last good copy."""

import os
import time

import pytest
import requests

from wastewater import http as wh
from wastewater.http import Fetcher

OK = "https://data.rivm.nl/a.csv"


class FakeResponse:
    def __init__(self, url, body=b"ok", status=200, location=None):
        self.url, self._body, self.status_code = url, body, status
        self.headers = {"Location": location} if location else {}
        self.is_redirect = location is not None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


def fetcher(tmp_path, monkeypatch, response, **kw):
    f = Fetcher(cache_dir=tmp_path, retries=1, **kw)
    calls = []

    def get(url, **_):
        calls.append(url)
        return response(url)

    monkeypatch.setattr(f._session, "get", get)
    f.calls = calls
    return f


@pytest.mark.parametrize(
    "url",
    [
        "http://data.rivm.nl/a.csv",  # not HTTPS
        "https://example.org/a.csv",  # not a data portal
        "https://data.rivm.nl.evil.example/a.csv",
        "https://user@data.rivm.nl/a.csv",
        "https://data.rivm.nl:8443/a.csv",
    ],
)
def test_refuses_urls_outside_the_allowed_hosts(tmp_path, monkeypatch, url):
    f = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u))
    with pytest.raises(RuntimeError):
        f.get_bytes(url)
    assert f.calls == []  # refused before any request


@pytest.mark.parametrize(
    "target",
    ["http://data.rivm.nl/b.csv", "https://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data/"],
)
def test_refuses_redirects_off_the_allowed_hosts(tmp_path, monkeypatch, target):
    f = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, status=302, location=target) if u == OK else FakeResponse(u))
    with pytest.raises(RuntimeError):
        f.get_bytes(OK)
    assert f.calls == [OK]  # the redirect target was never requested


def test_follows_redirects_within_the_allowed_hosts(tmp_path, monkeypatch):
    f = fetcher(
        tmp_path, monkeypatch,
        lambda u: FakeResponse(u, status=301, location="/b.csv") if u == OK else FakeResponse(u, body=b"moved"),
    )
    assert f.get_bytes(OK) == b"moved"
    assert f.calls == [OK, "https://data.rivm.nl/b.csv"]


def test_stops_redirect_loops(tmp_path, monkeypatch):
    f = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, status=302, location=OK))
    with pytest.raises(RuntimeError):
        f.get_bytes(OK)
    assert len(f.calls) == wh.MAX_REDIRECTS + 1


def test_refuses_oversized_bodies(tmp_path, monkeypatch):
    monkeypatch.setattr(wh, "MAX_BYTES", 10)
    f = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"x" * 50))
    with pytest.raises(RuntimeError):
        f.get_bytes(OK)


def test_caches_and_falls_back_to_last_good_copy(tmp_path, monkeypatch):
    good = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"v1"))
    assert good.get_bytes(OK) == b"v1"
    broken = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, status=503), max_age_hours=0)
    assert broken.get_bytes(OK) == b"v1"


def test_too_old_or_future_dated_cache_is_not_trusted(tmp_path, monkeypatch):
    fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"v1")).get_bytes(OK)
    (path,) = list(tmp_path.iterdir())
    # dated in the future: refetched rather than served as fresh
    future = time.time() + 365 * 86400
    os.utime(path, (future, future))
    fresh = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"v2"))
    assert fresh.get_bytes(OK) == b"v2" and fresh.calls == [OK]
    # older than the stale limit: an error, not silently old data
    old = time.time() - (wh.STALE_LIMIT_DAYS + 1) * 86400
    os.utime(path, (old, old))
    broken = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, status=503))
    with pytest.raises(RuntimeError):
        broken.get_bytes(OK)


def test_a_failed_parse_puts_the_last_good_copy_back(tmp_path, monkeypatch):
    fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"good,data")).get_bytes(OK)
    f = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"<html>maintenance</html>"), max_age_hours=0)
    with pytest.raises(ValueError):
        with f.transaction() as tx:
            if f.get_bytes(OK).startswith(b"<html>"):
                raise ValueError("not CSV")
    assert tx.restored == 1
    with f.cache_only():
        assert f.get_bytes(OK) == b"good,data"
        assert f.calls == [OK]  # nothing downloaded again
    # but not a copy past the stale limit
    path = f._cache_path(OK, None)
    old = time.time() - (wh.STALE_LIMIT_DAYS + 1) * 86400
    os.utime(path, (old, old))
    with f.cache_only(), pytest.raises(FileNotFoundError):
        f.get_bytes(OK)
    assert [p.name for p in tmp_path.iterdir()] == [f._cache_path(OK, None).name]  # no leftovers


def test_a_good_parse_keeps_the_new_copy(tmp_path, monkeypatch):
    fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"v1")).get_bytes(OK)
    f = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"v2"), max_age_hours=0)
    with f.transaction() as tx:
        f.get_bytes(OK)
    assert tx.restored == 0
    with f.cache_only():
        assert f.get_bytes(OK) == b"v2"
    assert len(list(tmp_path.iterdir())) == 1


def test_provenance_records_each_body_used(tmp_path, monkeypatch):
    import hashlib

    def with_date(u, body=b"v1"):
        r = FakeResponse(u, body=body)
        r.headers["Last-Modified"] = "Mon, 05 Oct 2026 10:00:00 GMT"
        return r

    f = fetcher(tmp_path, monkeypatch, with_date)
    f.get_bytes(OK)
    f.get_bytes("https://data.rivm.nl/b.csv", params={"z": 1, "a": "x y"})
    assert f.provenance == {
        OK: {"sha256": hashlib.sha256(b"v1").hexdigest(), "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT", "from_cache": False},
        "https://data.rivm.nl/b.csv?a=x+y&z=1": {"sha256": hashlib.sha256(b"v1").hexdigest(),
                                                 "last_modified": "Mon, 05 Oct 2026 10:00:00 GMT", "from_cache": False},
    }
    # served from the cache: no header to report
    f.get_bytes(OK)
    assert f.provenance[OK] == {"sha256": hashlib.sha256(b"v1").hexdigest(), "last_modified": None, "from_cache": True}
    # a failed download that falls back to the last good copy says so
    broken = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, status=503), max_age_hours=0)
    broken.get_bytes(OK)
    assert broken.provenance[OK]["from_cache"] is True


def test_provenance_describes_the_copy_used_after_a_rollback(tmp_path, monkeypatch):
    import hashlib

    fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"good")).get_bytes(OK)
    f = fetcher(tmp_path, monkeypatch, lambda u: FakeResponse(u, body=b"<html>"), max_age_hours=0)
    with pytest.raises(ValueError):
        with f.transaction():
            f.get_bytes(OK)
            raise ValueError("didn't parse")
    assert f.provenance[OK]["sha256"] == hashlib.sha256(b"<html>").hexdigest()
    with f.cache_only():
        f.get_bytes(OK)
    assert f.provenance[OK] == {"sha256": hashlib.sha256(b"good").hexdigest(), "last_modified": None, "from_cache": True}


def test_odd_last_modified_headers_are_dropped(tmp_path, monkeypatch):
    def odd(u):
        r = FakeResponse(u)
        r.headers["Last-Modified"] = "x" * 500
        return r

    f = fetcher(tmp_path, monkeypatch, odd)
    f.get_bytes(OK)
    assert f.provenance[OK]["last_modified"] is None
