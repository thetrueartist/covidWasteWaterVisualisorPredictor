"""Command line: ``python -m wastewater build`` and ``python -m wastewater serve``."""

from __future__ import annotations

import argparse
import functools
import http.server
import logging
import sys
from pathlib import Path

from .http import Fetcher
from .model import HOLDOUT_WEEKS
from .sources import SOURCES

SITE_DIR = Path(__file__).resolve().parent.parent / "site"


def cmd_build(args: argparse.Namespace) -> int:
    from .build import build

    fetcher = Fetcher(cache_dir=args.cache_dir, offline=args.offline, max_age_hours=args.max_age_hours)
    countries = args.countries.split(",") if args.countries else None
    index = build(
        country_ids=countries,
        out_dir=args.out,
        fetcher=fetcher,
        holdout_weeks=args.holdout_weeks,
        max_iter=args.max_iter,
    )
    for c in index["countries"]:
        nat = c["national"]["latest"]
        print(
            f"{c['flag']} {c['name']:<14} {c['n_regions']:>4} areas  data to {c['latest_date']}  "
            f"national: {(nat['category'] or 'n/a').replace('_', ' ')} ({nat['index']}/100)"
        )
    for h in index["model"]["by_horizon"]:
        print(
            f"  {h['horizon_weeks']}-week forecast: {100 * h['skill_vs_no_change']:+.0f}% vs no-change, "
            f"level right {100 * h['category_accuracy']:.0f}% of the time"
        )
    for e in index["errors"]:
        print(f"  ! {e['country']}: {e['error']}", file=sys.stderr)
    return 1 if not index["countries"] else 0


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Static file handler that only logs failed requests."""

    def log_request(self, code="-", size="-"):
        if isinstance(code, int) and code >= 400:
            super().log_request(code, size)


def cmd_serve(args: argparse.Namespace) -> int:
    directory = Path(args.dir).resolve()
    if not (directory / "data" / "index.json").exists():
        print(f"No data in {directory / 'data'} yet - run `python -m wastewater build` first.", file=sys.stderr)
    handler = functools.partial(QuietHandler, directory=str(directory))
    try:
        server = http.server.ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        print(
            f"Couldn't listen on {args.host}:{args.port} ({exc.strerror}). "
            f"Something else may be using that port; try --port {args.port + 1}.",
            file=sys.stderr,
        )
        return 1
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"Serving {directory} at http://{shown}:{args.port}/ (Ctrl+C to stop)", flush=True)
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
    b.add_argument("--max-iter", type=int, default=100, help="boosting rounds per model (lower is faster)")
    b.set_defaults(func=cmd_build)

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
