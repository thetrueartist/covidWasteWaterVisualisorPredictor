# Sewer Signal forecast archive

This branch holds the forecasts the [Sewer Signal](../../tree/main) site has published, each saved on the
day it was published, before anyone knew how it would turn out. If saving failed on a day, that day's
forecasts are missing (the workflow run for that day says why). It is the evidence behind the site's
"Live track record": each forecast is checked against the data once the week after its target week
has been reported.

## How it works

- The site's daily workflow builds the data and forecasts, then a separate job with write access
  checks the new files (`.github/scripts/check-new-forecasts.sh` on `main`) and commits them here.
  That job runs no project code and installs no packages.
- The workflow only ever adds files: it never changes or deletes one. The commit history shows when
  each forecast was saved, and any later change to a file would show there as a new commit. If the
  repository's owner has set up the branch rulesets in `docs/hosting.md` on `main`, force pushes and
  deletion are blocked too, so that history can't be rewritten.
- The site doesn't publish a forecast for an area whose newest data is more than 35 days old, because
  the check can't tell such a forecast from a back-dated one, or dated more than 7 days ahead. So it
  only publishes forecasts that can be saved here.
- A new file is saved only when what the site published changed since the last saved one.

## Layout

```
v2/<virus>/<country>/<YYYY-MM-DDTHHMMSSZ>.jsonl.gz
```

One gzipped JSON-lines file per country, virus and issue time (UTC). Line 1 describes the issue
(code version, model design, upstream file hashes), and each further line is one area: its latest
level, its level cut-offs, the last 13 weeks of its smoothed level and the forecast for 1 to 6 weeks
ahead. The full format is described in `wastewater/archive.py` on `main`, and the scoring rules in
`wastewater/scoring.py`.

## Data sources and licences

The forecasts are derived from public wastewater and laboratory surveillance data. The saved files
contain smoothed, weekly versions of that data and forecasts made from it. Please credit the
publishers if you reuse them.

| Country | Publisher | Licence |
|---|---|---|
| Scotland | Public Health Scotland | Open Government Licence v3.0. Contains public sector information licensed under the Open Government Licence v3.0. |
| United States | CDC National Wastewater Surveillance System (NWSS) | Public domain (US Government work) |
| Canada | Public Health Agency of Canada | Open Government Licence – Canada. Contains information licensed under the Open Government Licence – Canada. |
| Germany | Robert Koch Institute (AMELAG) | CC BY 4.0. Source: Robert Koch-Institut; the data was aggregated to weeks, smoothed and used for forecasts. |
| Netherlands | RIVM (National Institute for Public Health and the Environment) | CC0 1.0 |
| New Zealand | PHF Science (formerly ESR) | CC BY 4.0. Source: PHF Science; the data was aggregated to weeks, smoothed and used for forecasts. |

The site's code is under the MIT licence. Nothing here is medical advice.
