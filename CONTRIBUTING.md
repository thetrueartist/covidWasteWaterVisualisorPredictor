# Contributing

Thanks for helping. The most useful contributions are new data sources and
fixes for adapters when a publisher changes their format.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.txt   # the exact versions CI uses
python -m wastewater -v build     # downloads data into .cache/, writes site/data/
python -m wastewater serve        # http://127.0.0.1:8000
```

After the first build, `python -m wastewater build --offline` reuses the
cached downloads, which is handy while working on the model or the site.

## Checks

```bash
pytest          # Python: adapters, preprocessing, models, downloads, server, installer, build
npm test        # front-end go/avoid logic (plain node, no dependencies)
```

CI runs both on every push.

Dependencies are listed in `pyproject.toml` and pinned with hashes in
`requirements.txt`, for every Python from 3.10 up. To update the pins:

```bash
uv pip compile pyproject.toml --extra dev --generate-hashes --universal --python-version 3.10 -o requirements.txt
```

Dependabot opens weekly pull requests for outdated packages and GitHub Actions.

## Adding a country

1. Create `wastewater/sources/<country>.py` with a `Source` subclass. Its
   `info.signals` says which viruses it publishes (`covid`, `flu`, `rsv`)
   and, for each, the metric, unit and whether it's wastewater or lab tests.
   Its `fetch(fetcher)` returns a list of `RawSeries`, one per area and
   virus. Each has an id, a display name, a level (`national`, `region`,
   `local` or `site`), a group label for the area picker, its `pathogen`, and
   a pandas Series of measurements indexed by date. Any unit works, because
   levels are relative to each area's own history. Give the same place the
   same id for every virus so the site can match it between viruses.
2. Keep parsing separate from downloading (`parse_*(text)` functions) so it
   can be tested without the network. You don't need to clean values or
   dates yourself: `RawSeries` drops anything non-numeric, negative or
   infinite, and dates before 2020 or more than a week ahead.
3. Add the publisher's host to `ALLOWED_HOSTS` in `wastewater/http.py`. The
   build only downloads over HTTPS from hosts on that list, redirects
   included.
4. Register it in `SOURCES` in `wastewater/sources/__init__.py`.
5. Add a test in `tests/test_sources.py` with a few lines in the publisher's
   real format.
6. Run a build and check the back-test numbers it prints. The model is
   trained across all countries, so a new source should not make the others
   worse.

## Changing the model

Each virus has its own model, and `MODEL_SPECS` in `wastewater/model.py`
records which design each one uses. Those choices come from
`python -m wastewater compare`, which scores every candidate design in
`CANDIDATES` on two separate held-out years. Any model change should come
with that comparison before and after. A change that helps one held-out year
but hurts the other isn't an improvement. The README lists ideas that were
dropped for exactly that reason.

## Style

Python follows the existing code: type hints, small functions, docstrings
where the why isn't obvious. The site is plain HTML, CSS and ES modules with
no build step. Please keep it that way, so it stays easy to host anywhere.
