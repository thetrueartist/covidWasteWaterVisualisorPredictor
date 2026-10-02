"""The local web server (python -m wastewater serve) only serves the site folder."""

import functools
import http.client
import os
import socket
import threading
import time

import pytest

from wastewater.cli import SECURITY_HEADERS, SiteHandler, SiteServer


@pytest.fixture
def server(tmp_path):
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    (site / "index.html").write_text("<p>hello</p>")
    (site / "data" / "index.json").write_text("{}")
    (site / ".hidden").write_text("secret")
    (tmp_path / "outside.txt").write_text("outside")
    os.symlink(tmp_path / "outside.txt", site / "link.txt")

    class FastTimeout(SiteHandler):
        timeout = 1

    handler = functools.partial(FastTimeout, directory=str(site))
    srv = SiteServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def get(srv, path, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.request(method, path)
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def test_serves_site_files_with_security_headers(server):
    resp, body = get(server, "/index.html")
    assert resp.status == 200 and body == b"<p>hello</p>"
    for name, value in SECURITY_HEADERS.items():
        assert resp.getheader(name) == value
    assert "Python" not in resp.getheader("Server")


@pytest.mark.parametrize(
    "path",
    [
        "/link.txt",  # symlink pointing outside the folder
        "/.hidden",
        "/data/",  # directory listing
        "/../outside.txt",
        "/%2e%2e/outside.txt",
        "/data/%2e%2e/%2e%2e/outside.txt",
        "//etc/passwd",
    ],
)
def test_refuses_anything_outside_or_hidden(server, path):
    resp, body = get(server, path)
    assert resp.status == 404
    assert b"outside" not in body and b"secret" not in body


def test_other_methods_are_refused(server):
    resp, _ = get(server, "/index.html", method="POST")
    assert resp.status == 501


def test_idle_connections_are_dropped(server):
    sock = socket.create_connection(server.server_address, timeout=5)
    sock.sendall(b"GET /index.html HTTP/1.1\r\n")  # never finish the request
    start = time.time()
    assert sock.recv(1024) == b""  # server closes the idle connection
    assert time.time() - start < 4
    sock.close()


def test_connection_cap_drops_extra_clients(tmp_path):
    site = tmp_path / "s"
    site.mkdir()
    (site / "index.html").write_text("ok")

    class Slow(SiteHandler):
        timeout = 2

    class Small(SiteServer):
        max_connections = 3

    srv = Small(("127.0.0.1", 0), functools.partial(Slow, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        idle = [socket.create_connection(srv.server_address) for _ in range(3)]
        time.sleep(0.3)
        extra = socket.create_connection(srv.server_address, timeout=3)
        extra.sendall(b"GET /index.html HTTP/1.0\r\n\r\n")
        assert extra.recv(1024) == b""  # dropped while all slots are busy
        for s in idle:
            s.close()
        time.sleep(0.5)
        resp, body = get(srv, "/index.html")  # slots free again
        assert resp.status == 200 and body == b"ok"
    finally:
        srv.shutdown()
        srv.server_close()
