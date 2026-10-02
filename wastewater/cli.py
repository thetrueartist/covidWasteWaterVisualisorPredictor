"""Command line: ``python -m wastewater build`` and ``python -m wastewater serve``."""

from __future__ import annotations

import argparse
import errno
import functools
import http.server
import logging
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .http import Fetcher
from .model import HOLDOUT_WEEKS
from .sources import SOURCES

SITE_DIR = Path(__file__).resolve().parent.parent / "site"


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

    def _inside_root(self, path: str) -> bool:
        root = os.path.realpath(self.directory)
        real = os.path.realpath(path)
        rel = os.path.relpath(real, root)
        if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
            return False
        return not any(part.startswith(".") for part in Path(rel).parts if part != ".")

    def send_head(self):
        if not self._inside_root(self.translate_path(self.path)):
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


class SiteServer(http.server.ThreadingHTTPServer):
    """Thread-per-connection server with a cap on simultaneous connections."""

    max_connections = 64

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # full: drop the connection rather than queue it
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


# Kept for anything importing the old name.
QuietHandler = SiteHandler


def cmd_serve(args: argparse.Namespace) -> int:
    directory = Path(args.dir).resolve()
    if not (directory / "data" / "index.json").exists():
        print(f"No data in {directory / 'data'} yet - run `python -m wastewater build` first.", file=sys.stderr)
    handler = functools.partial(SiteHandler, directory=str(directory))
    try:
        server = SiteServer((args.host, args.port), handler)
    except OSError as exc:
        hint = (
            f"Something else is using that port; try --port {args.port + 1}."
            if exc.errno == errno.EADDRINUSE
            else "Check the address and that you have permission to use it."
        )
        print(f"Couldn't listen on {args.host}:{args.port} ({exc.strerror}). {hint}", file=sys.stderr)
        return 1
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    lan = " and to other devices on your network" if args.host in ("0.0.0.0", "::") else ""
    print(f"Serving {directory} at http://{shown}:{args.port}/{lan} (Ctrl+C to stop)", flush=True)
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
    b.set_defaults(func=cmd_build)

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
    s.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
