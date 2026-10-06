"""Command line: ``python -m wastewater build``, ``score`` and ``serve``."""

from __future__ import annotations

import argparse
import errno
import functools
import http.server
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .http import Fetcher
from .model import HOLDOUT_WEEKS
from .sources import SOURCES

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
serve_log = logging.getLogger("wastewater.serve")
# The server's log is capped at this size, keeping one older file beside it.
LOG_MAX_BYTES = 1_000_000


@contextmanager
def only_one_build(cache_dir: Path) -> Iterator[bool]:
    """Yield False if another build is using this cache already.

    Two builds at once would each try to use every CPU core and slow each
    other to a crawl, for example a manual update during the daily refresh.
    """
    try:
        import fcntl
    except ImportError:  # Windows: no locking, just run
        yield True
        return
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(cache_dir / "build.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True


def cmd_build(args: argparse.Namespace) -> int:
    fetcher = Fetcher(cache_dir=args.cache_dir, offline=args.offline, max_age_hours=args.max_age_hours)
    with only_one_build(fetcher.cache_dir) as ok:
        if not ok:
            print("Another build is already running. Try again when it has finished.", file=sys.stderr)
            return 1
        return _build(args, fetcher)


def _build(args: argparse.Namespace, fetcher: Fetcher) -> int:
    from .build import build

    countries = args.countries.split(",") if args.countries else None
    index = build(
        country_ids=countries,
        out_dir=args.out,
        fetcher=fetcher,
        holdout_weeks=args.holdout_weeks,
        max_iter=args.max_iter,
        archive=args.archive,
        archive_new=args.archive_new,
    )
    labels = {v["id"]: v["label"] for v in index["viruses"]}
    for c in index["countries"]:
        for virus, v in c["viruses"].items():
            nat = v["national"]["latest"]
            print(
                f"{c['flag']} {c['name']:<14} {labels[virus]:<9} {v['n_regions']:>4} areas  data to {v['latest_date']}  "
                f"national: {(nat['category'] or 'n/a').replace('_', ' ')} ({nat['index']}/100)"
            )
    for virus, m in index["models"].items():
        skills = " ".join(f"{100 * h['skill_vs_no_change']:+.0f}%" for h in m["by_horizon"])
        print(f"  {labels[virus]} model ({m['design']}), vs no-change at 1-6 weeks: {skills}")
    for e in index["errors"]:
        where = "/".join(x for x in (e.get("country"), e.get("virus")) if x)
        print(f"  ! {where}: {e['error']}", file=sys.stderr)
    return 1 if not index["countries"] else 0


def cmd_score(args: argparse.Namespace) -> int:
    """Score the forecast archive into the site's track-record.json."""
    from .scoring import score_archive, write_track_record

    archive = Path(args.archive)
    if not archive.is_dir():
        print(f"No forecast archive at {archive}. Build with --archive {archive} first.", file=sys.stderr)
        return 1
    record = score_archive(archive)
    write_track_record(record, args.out)
    # Whatever could be scored is published: parts that couldn't are left out and listed here
    # (with the reason in the log), so they never take the rest of the record down with them.
    print(f"Track record: {record['issues']} saved issues since {record['since'] or 'n/a'}, written to {args.out}")
    for virus, v in record["by_virus"].items():
        if v.get("available") is False:
            print(f"  ! {virus}: couldn't be summarised, so it's left out", file=sys.stderr)
            continue
        checked = sum(r["n"] for r in v["by_horizon"])
        weeks = max((r["issue_weeks"] for r in v["by_horizon"]), default=0)
        print(f"  {virus}: {checked} forecasts checked, from {weeks} data weeks")
    for part in record["not_scored"]:
        if part["country"] is not None:
            print(f"  ! {part['virus']}/{part['country']}: couldn't be scored, so it's left out", file=sys.stderr)
    for gap in record["gaps"]:
        print(f"  ! {gap['country']}/{gap['virus']}: nothing new saved since {gap['newest']}", file=sys.stderr)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """Re-run the model-design comparison that MODEL_SPECS is based on."""
    from .build import collect
    from .model import CANDIDATES, HORIZONS, MODEL_SPECS, Dataset, compare
    from .sources import PATHOGENS

    fetcher = Fetcher(cache_dir=args.cache_dir, offline=args.offline)
    countries = args.countries.split(",") if args.countries else list(SOURCES)
    prepared, errors = collect([SOURCES[c]() for c in countries], fetcher)
    for e in errors:
        print(f"! {e['country']}: {e['error']}", file=sys.stderr)
    ds = Dataset.from_prepared(prepared)
    viruses = args.viruses.split(",") if args.viruses else [v for v in PATHOGENS if (ds.frame["pathogen"] == v).any()]
    for virus in viruses:
        rows = compare(ds, virus, folds=args.folds)
        print(f"\n{PATHOGENS[virus]} (current design: {MODEL_SPECS[virus].name})")
        print(f"  {'design':<34} " + " ".join(f"fold{k} 1-4wk" for k in range(args.folds)) + "   mean 1-6wk")
        for name in CANDIDATES:
            mine = [r for r in rows if r["candidate"] == name]
            per_fold = [sum(r.get(f"h{h}", 0) for h in (1, 2, 3, 4)) / 4 for r in mine]
            overall = sum(r.get(f"h{h}", 0) for r in mine for h in HORIZONS) / max(1, len(mine) * len(HORIZONS))
            print(f"  {name:<34} " + " ".join(f"{100 * v:+10.1f}%" for v in per_fold) + f"   {100 * overall:+8.1f}%")
    return 0


# Sent with every response. The CSP matches the <meta> one in site/index.html.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}


_CONTROL_ESCAPES = {c: f"\\x{c:02x}" for c in (*range(0x20), *range(0x7F, 0xA0))}


class SiteHandler(http.server.SimpleHTTPRequestHandler):
    """Serves the static site folder and nothing else.

    Compared with the standard library handler it refuses anything that
    resolves outside the folder (e.g. through a symlink), hidden files and
    directory listings, hides the Python version, drops clients that stay
    idle, sends security headers and only logs failed requests.
    """

    server_version = "SewerSignal"
    sys_version = ""
    timeout = 15  # seconds a client may stay idle before it is disconnected
    # Seconds to send the whole request line and headers. The idle timeout
    # alone would let a client trickle one byte every few seconds forever.
    header_deadline = 10

    def handle_one_request(self):
        self._deadline = threading.Timer(self.header_deadline, self._drop)
        self._deadline.daemon = True
        self._deadline.start()
        waiting = getattr(self.server, "waiting", None)
        if waiting is not None:
            waiting.add(self.connection)
        try:
            super().handle_one_request()
        finally:
            self._deadline.cancel()
            if waiting is not None:
                waiting.discard(self.connection)

    def parse_request(self):
        ok = super().parse_request()  # reads the request line's headers too
        self._deadline.cancel()  # they're in; from here the idle timeout covers the rest
        waiting = getattr(self.server, "waiting", None)
        if waiting is not None:
            waiting.discard(self.connection)
        return ok

    def _drop(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _inside_root(self, path: str) -> bool:
        root = os.path.realpath(self.directory)
        real = os.path.realpath(path)
        rel = os.path.relpath(real, root)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
            return False
        return not any(part.startswith(".") for part in Path(rel).parts if part != ".")

    def _allowed(self) -> bool:
        try:
            path = self.translate_path(self.path)
            if not self._inside_root(path):
                return False
            if os.path.isdir(path):
                # A folder is served as its index.html, so check that file too.
                for name in ("index.html", "index.htm"):
                    index = os.path.join(path, name)
                    if os.path.lexists(index) and not self._inside_root(index):
                        return False
            return True
        except (ValueError, OSError):  # a NUL byte in the path, for example
            return False

    def send_head(self):
        if not self._allowed():
            self.send_error(404, "File not found")
            return None
        return super().send_head()

    def list_directory(self, path):
        self.send_error(404, "File not found")
        return None

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def log_request(self, code="-", size="-"):
        if isinstance(code, int) and code >= 400:
            super().log_request(code, size)

    def log_message(self, format, *args):
        # One short line per entry, with control characters shown as \xNN so a
        # client can't send terminal escape codes into the log.
        message = (format % args).translate(_CONTROL_ESCAPES)[:200]
        serve_log.warning("%s [%s] %s", self.address_string(), self.log_date_time_string(), message)


class _Waiting:
    """Connections that haven't finished sending a request yet, with when they started."""

    def __init__(self):
        self._lock = threading.Lock()
        self._since: dict = {}

    def add(self, sock) -> None:
        with self._lock:
            self._since[sock] = time.monotonic()

    def discard(self, sock) -> None:
        with self._lock:
            self._since.pop(sock, None)

    def drop_oldest(self, min_age: float) -> bool:
        """Disconnect whoever has been slowest to send a request, if anyone is that slow."""
        with self._lock:
            if not self._since:
                return False
            sock, since = min(self._since.items(), key=lambda item: item[1])
            if time.monotonic() - since < min_age:
                return False
            del self._since[sock]
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        return True


class SiteServer(http.server.ThreadingHTTPServer):
    """Thread-per-connection server with caps on simultaneous connections.

    One client address can hold at most ``max_per_client`` of the
    ``max_connections`` slots. When every slot is busy, a newcomer still gets
    in if some connection has spent over a second without sending a full
    request: the slowest such connection is dropped to make room. So trickling
    bytes, even from several addresses, can't lock everyone else out.
    """

    max_connections = 64
    max_per_client = 16  # browsers open about 6 per site

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._lock = threading.Lock()
        self._open = 0
        self._per_client: dict[str, int] = {}
        self.waiting = _Waiting()

    def _take(self, client: str) -> bool:
        with self._lock:
            if self._per_client.get(client, 0) >= self.max_per_client:
                return False
            if self._open >= self.max_connections:
                # Dropped connections take a moment to wind down, so allow a
                # little overlap, but never more than double the cap.
                if self._open >= 2 * self.max_connections or not self.waiting.drop_oldest(min_age=1.0):
                    return False
            self._open += 1
            self._per_client[client] = self._per_client.get(client, 0) + 1
            return True

    def _give_back(self, client: str) -> None:
        with self._lock:
            self._open -= 1
            left = self._per_client.get(client, 1) - 1
            if left:
                self._per_client[client] = left
            else:
                self._per_client.pop(client, None)

    def process_request(self, request, client_address):
        client = client_address[0]
        if not self._take(client):
            self.shutdown_request(request)  # full: drop the connection rather than queue it
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._give_back(client)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._give_back(client_address[0])

    def handle_error(self, request, client_address):
        # One line, not a full traceback per bad request.
        serve_log.error("%s: error handling a request (%s)", client_address[0], type(sys.exc_info()[1]).__name__)


# Kept for anything importing the old name.
QuietHandler = SiteHandler


def _serve_logging(log_file: str | None) -> None:
    if log_file:
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=1, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    serve_log.handlers[:] = [handler]
    serve_log.setLevel(logging.INFO)
    serve_log.propagate = False


def cmd_serve(args: argparse.Namespace) -> int:
    _serve_logging(args.log_file)
    directory = Path(args.dir).resolve()
    if not (directory / "data" / "index.json").exists():
        serve_log.warning("No data in %s yet - run `python -m wastewater build` first.", directory / "data")
    handler = functools.partial(SiteHandler, directory=str(directory))
    try:
        server = SiteServer((args.host, args.port), handler)
    except OSError as exc:
        hint = (
            f"Something else is using that port; try --port {args.port + 1}."
            if exc.errno == errno.EADDRINUSE
            else "Check the address and that you have permission to use it."
        )
        serve_log.error("Couldn't listen on %s:%s (%s). %s", args.host, args.port, exc.strerror, hint)
        return 1
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    lan = " and to other devices on your network" if args.host in ("0.0.0.0", "::") else ""
    serve_log.info("Serving %s at http://%s:%s/%s (Ctrl+C to stop)", directory, shown, args.port, lan)
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wastewater", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="show progress logs")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="download data, train the model, write site/data/*.json")
    b.add_argument("--countries", help=f"comma-separated subset of: {', '.join(SOURCES)}")
    b.add_argument("--out", default=str(SITE_DIR / "data"), help="output directory")
    b.add_argument("--cache-dir", default=None, help="download cache (default .cache or $WASTEWATER_CACHE)")
    b.add_argument("--offline", action="store_true", help="use cached downloads only")
    b.add_argument("--max-age-hours", type=float, default=6.0, help="re-download cached files older than this")
    b.add_argument("--holdout-weeks", type=int, default=HOLDOUT_WEEKS, help="weeks held out for back-testing")
    b.add_argument("--max-iter", type=int, default=None, help="override boosting rounds per model (lower is faster)")
    b.add_argument("--archive", default=None,
                   help="forecast archive folder: save each published forecast there if it changed (the live track record)")
    b.add_argument("--archive-new", default=None,
                   help="also copy newly saved archive files here, under the same paths (needs --archive)")
    b.set_defaults(func=cmd_build)

    t = sub.add_parser("score", help="score the forecast archive into the live track record")
    t.add_argument("--archive", required=True, help="forecast archive folder (written by build --archive)")
    t.add_argument("--out", default=str(SITE_DIR / "data" / "track-record.json"), help="where to write the track record")
    t.set_defaults(func=cmd_score)

    c = sub.add_parser("compare", help="score the candidate model designs for each virus on held-out years")
    c.add_argument("--countries", help=f"comma-separated subset of: {', '.join(SOURCES)}")
    c.add_argument("--viruses", help="comma-separated subset of: covid, flu, rsv")
    c.add_argument("--folds", type=int, default=2, help="number of held-out years")
    c.add_argument("--cache-dir", default=None)
    c.add_argument("--offline", action="store_true", help="use cached downloads only")
    c.set_defaults(func=cmd_compare)

    s = sub.add_parser("serve", help="serve the web app locally")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--dir", default=str(SITE_DIR))
    s.add_argument("--log-file", help="write the log here, capped at about 1 MB plus one older file")
    s.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    if getattr(args, "archive_new", None) and not getattr(args, "archive", None):
        parser.error("--archive-new needs --archive")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
