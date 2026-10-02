# Contributing

Thanks for helping. The most useful contributions are new data sources and
fixes for adapters when a publisher changes their format.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m wastewater -v build     # downloads data into .cache/, writes site/data/
python -m wastewater serve        # http://127.0.0.1:8000
```

After the first build, `python -m wastewater build --offline` reuses the
cached downloads, which is handy while working on the model or the site.

## Checks

```bash
pytest          # Python: adapters, preprocessing, model, build
npm test        # front-end go/avoid logic (plain node, no dependencies)
```

CI runs both on every push.

## Adding a country

1. Create `wastewater/sources/<country>.py` with a `Source` subclass. Its
   `fetch(fetcher)` returns a list of `RawSeries`, one per area. Each has an
   id, a display name, a level (`national`, `region`, `local` or `site`), a
   group label for the area picker, and a pandas Series of measurements
   indexed by date. Any unit works, because levels are relative to each
   area's own history.
2. Keep parsing separate from downloading (`parse_*(text)` functions) so it
   can be tested without the network.
3. Register it in `SOURCES` in `wastewater/sources/__init__.py`.
4. Add a test in `tests/test_sources.py` with a few lines in the publisher's
   real format.
5. Run a build and check the back-test numbers it prints. The model is
   trained across all countries, so a new source should not make the others
   worse.

## Changing the model

Any change to `wastewater/model.py` should come with back-test numbers
before and after (`python -m wastewater build` prints them). A change that
helps one hold-out window but hurts another isn't an improvement. The README
lists two ideas that were dropped for exactly that reason.

## Style

Python follows the existing code: type hints, small functions, docstrings
where the why isn't obvious. The site is plain HTML, CSS and ES modules with
no build step. Please keep it that way, so it stays easy to host anywhere.
