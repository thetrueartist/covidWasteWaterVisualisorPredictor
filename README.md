<div align="center">

<img src="docs/logo.svg" width="64" height="64" alt="">

# Sewer Signal

**How much COVID is going round where you live, where it's heading over the next six weeks,
and whether now's a good time for that gig, flight or visit to your gran.**

Built on public wastewater surveillance data for Scotland, the United States, Canada, Germany,
the Netherlands and New Zealand.

[![CI](https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor/actions/workflows/ci.yml/badge.svg)](https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor/actions/workflows/ci.yml)
[![Daily data](https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor/actions/workflows/pages.yml/badge.svg)](https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor/actions/workflows/pages.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-2a78d6)
![No build step](https://img.shields.io/badge/front--end-no%20build%20step-1c5cab)
[![MIT licence](https://img.shields.io/badge/licence-MIT-104281)](LICENSE)

<img src="docs/screenshots/desktop-light.png" alt="Sewer Signal showing NHS Greater Glasgow and Clyde: level high and rising, a six-week outlook, and the activity planner" width="900">

</div>

## What it does

- **Tells you the level right now.** Each area's wastewater is ranked against its own last two
  years, from very low to very high, with the trend. Published data runs one to two weeks behind,
  so it also estimates this week.
- **Forecasts six weeks ahead** with honest ranges, and gives the chance of each level for every
  week, not just a single line.
- **Answers "should I go?"** Pick an activity and a week, and say whether you're at higher risk. You
  get *go for it*, *go with precautions*, *consider postponing* or *avoid if you can*, with practical
  tips and a better week if there is one.
- **Covers ~500 areas**, from whole countries down to individual treatment works, for example
  NHS health boards, Scottish council areas, US states, German Länder and Dutch treatment plants.
- **Runs anywhere.** It's a static site plus a daily data job. Self-host it with one script, or let
  GitHub Pages host it for free.

<table>
  <tr>
    <td width="62%"><img src="docs/screenshots/chart-dark.png" alt="Two years of wastewater levels for Greater Glasgow with the forecast fan, in dark mode"></td>
    <td width="38%"><img src="docs/screenshots/planner-dark.png" alt="The activity planner recommending postponing a gig this week"></td>
  </tr>
  <tr>
    <td><img src="docs/screenshots/areas-light.png" alt="Table of Scottish health boards sorted by current level"></td>
    <td><img src="docs/screenshots/phone-light.png" alt="The site on a phone"></td>
  </tr>
</table>

## Quick start

On any Linux machine with internet access:

```bash
git clone https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor.git sewer-signal
cd sewer-signal
./install.sh              # install, build the data, start on http://localhost:8000
./install.sh --service    # or keep it running in the background, refreshed daily
```

The installer is careful:
- Everything goes into the folder and a Python virtualenv. Nothing is installed system-wide.
- It only uses `sudo` to add a missing package such as `python3-venv`, and it asks first.
- It listens on `127.0.0.1` only. Add `--lan` to open it to your home network.
- If port 8000 is busy, it takes the next free port. It never stops another program.
- It only changes files and cron lines it created, and `./install.sh uninstall` removes them again.

Other commands: `./install.sh status`, `./install.sh update`, `./install.sh --help`.

<details>
<summary>Manual install (any OS with Python 3.10+)</summary>

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m wastewater build      # download data, train, write site/data (1-2 minutes)
python -m wastewater serve      # http://127.0.0.1:8000
```

`build --countries scotland,usa` builds a subset, but the forecast learns from all countries, so
subsets forecast a little worse. `build --offline` reuses cached downloads. Add `-v` for progress logs.
</details>

## Where to host it

| | |
|---|---|
| **Just for you, or your household** | Your own Linux box: `./install.sh --service`, plus `--lan` for other devices. It needs about 1 GB of disk and 0.5 GB of RAM during the daily refresh. A Raspberry Pi is fine. |
| **Shareable link, zero upkeep** | GitHub Pages, free for public repos. The workflow in `.github/workflows/pages.yml` already rebuilds and publishes daily. Turn on *Settings → Pages → Source: GitHub Actions*. |
| **Your own domain** | Put Caddy or nginx in front of `site/` and keep only the daily refresh timer. |

Don't port-forward the built-in Python server to the internet. For access from outside your home, use
Tailscale or a Cloudflare Tunnel. Details and config snippets are in **[docs/hosting.md](docs/hosting.md)**.

## Data sources

| | Areas | Publisher | Updated |
|---|---|---|---|
| 🏴󠁧󠁢󠁳󠁣󠁴󠁿 Scotland | national · 14 health boards · 32 council areas · ~110 treatment works | [Public Health Scotland](https://www.opendata.nhs.scot/dataset/viral-respiratory-diseases-including-influenza-and-covid-19-data-in-scotland) (Scottish Water and SEPA sampling) | weekly |
| 🇺🇸 United States | national · 50 states, DC and territories | [CDC NWSS](https://data.cdc.gov/Public-Health-Surveillance/CDC-Wastewater-Viral-Activity-Level-for-SARS-CoV-2/atcp-73re), Wastewater Viral Activity Level | weekly |
| 🇨🇦 Canada | national · provinces and territories · cities | [Public Health Agency of Canada](https://health-infobase.canada.ca/wastewater/) | weekly |
| 🇩🇪 Germany | national · 16 Länder · ~70 treatment plants | [Robert Koch Institute, AMELAG](https://github.com/robert-koch-institut/Abwassersurveillance_AMELAG) | weekly |
| 🇳🇱 Netherlands | national · ~130 treatment plants | [RIVM](https://data.rivm.nl/covid-19/) | several times a week |
| 🇳🇿 New Zealand | national · regions · treatment plants | [PHF Science](https://github.com/ESR-NZ/covid_in_wastewater) (formerly ESR) | weekly |

England has had no public wastewater data since UKHSA wound its programme down in 2022.
To suggest another source, [open an issue](../../issues/new/choose). [CONTRIBUTING.md](CONTRIBUTING.md) shows how to add one.

## How it works

```
data portals ─▶ wastewater/sources/*   one adapter per country, cached downloads
                wastewater/preprocess  weekly series, log scale, causal smoothing, 0-100 level index
                wastewater/model       quantile gradient boosting across ~950 series, back-tested
                wastewater/build       ─▶ site/data/*.json
                                                 │
                site/  HTML + CSS + JS ◀─────────┘   static; rebuilt daily
```

**Levels.** Units differ between countries and labs, so each area is compared with its own previous two
years, much like CDC's activity levels. *Very low* is below the 20th percentile of the last two years,
*low* is the 20th–40th, *moderate* the 40th–60th, *high* the 60th–80th, and *very high* is above the 80th.

**Forecast.** One model is trained across every area in all six countries, so it learns from far more
waves than any one place has had. For each of the next six weeks it predicts five quantiles of the change
in the smoothed level. Its inputs are the area's own recent changes, its country's national trend, and the
median trend across that country's areas.

**Keeping it honest.** The most recent year is held out and the model is scored against simply assuming
"no change". Where it doesn't win for a country and horizon, forecasts fall back to no-change. The same
hold-out calibrates the ranges so the 50% and 90% bands cover about 50% and 90% of outcomes.
Back-test results from the 2 Oct 2026 build (the site shows the latest ones):

| Weeks ahead | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| Typical miss | ±37% | ±54% | ±68% | ±84% | ±100% | ±118% |
| …assuming no change | ±39% | ±61% | ±79% | ±96% | ±112% | ±129% |
| Right level | 75% | 67% | 63% | 59% | 57% | 55% |
| …assuming no change | 74% | 65% | 60% | 57% | 55% | 53% |

By country: US +9 to +13% more accurate than no-change, Scotland +5 to +12%, Netherlands +6 to +11%,
Germany 0 to +13%, Canada −1 to +4%. New Zealand is −8 to −15%, so it uses no-change.

These gains are modest, which is normal for weekly wastewater forecasting. Two ideas were tried and
dropped because they didn't hold up in testing:
- level-in-range and seasonality features, which over-predicted rises in one hold-out window;
- per-level sample weights, which were no more accurate and about 6× slower.

**Should I go?** `site/js/advisor.js` adds the expected level (averaged over the forecast's probabilities,
so uncertainty counts), the activity's exposure, and +1.25 if you or someone you'll see is at higher risk.
That total maps to the four verdicts. A gig at very low levels is a go. Visiting a care home while levels
are high says postpone.

## Development

```bash
pytest       # adapters, preprocessing, model, end-to-end build (about 10 s)
npm test     # go/avoid logic (node --test, no dependencies)
```

```
wastewater/   sources/ (one adapter per country) · preprocess · model · risk · build · cli
site/         index.html · styles.css · js/{app,chart,advisor,dates,format}.js
tests/        pytest suite · js/ node tests
install.sh    Linux installer and service manager
docs/         hosting guide, screenshots
```

## Disclaimer

Wastewater shows community trends, not your personal risk, and small treatment works give noisy
readings. Nothing here is medical advice. If you're unwell, stay home. If you're at high risk, follow
your clinician's guidance. The data belongs to the publishers above, under their own licences.

Code: [MIT](LICENSE).
