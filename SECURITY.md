# Security

Please report vulnerabilities privately through GitHub's **Report a
vulnerability** button on the repository's Security tab, rather than in a
public issue. I'll reply as soon as I can.

## What's in scope

- The static site in `site/` (for example script injection through the data files)
- `python -m wastewater serve`, the small web server `install.sh` runs
- `install.sh`
- The data pipeline in `wastewater/` and the GitHub Actions workflows

## Design notes

- The site has no accounts, cookies, analytics or third-party requests. Fonts are
  self-hosted, and a strict Content-Security-Policy allows only the site's own files.
- Upstream data is treated as untrusted. Downloads only come over HTTPS from the
  known data portals, implausible values and dates are dropped, one bad file only
  affects its own country and virus, and names are stripped of control and bidi
  characters and only reach the page as text.
- `install.sh` listens on localhost unless you pass `--lan`. It installs nothing
  system-wide, uses `sudo` only to add missing packages after asking, installs
  hash-checked Python packages, and refuses unusual paths and arguments rather
  than escaping them.
- The daily Pages workflow keeps write access away from anything that installs packages. The build
  job, which runs third-party packages, has a read-only token and only hands over new forecast files.
  A separate job, after the site is deployed, runs no project code and installs nothing: it checks
  those files with `.github/scripts/check-new-forecasts.sh` (bash, gzip and jq only) and adds them to
  the `forecast-archive` branch with plain git. The check refuses anything unexpected: other paths,
  symlinks or hard links, more than one file per country and virus, files from more than one build,
  files over 1 MB (5 MB decompressed) or with more than 1,000 areas, invalid gzip, headers that don't
  match their file name, files not dated today or yesterday, dated in the future or no later than a
  file already saved for their country and virus, data more than 35 days old or more than 7 days ahead
  or not ending on a Sunday, malformed forecasts, and any file that's already saved. The token is passed
  to git per command and never stored. The checks run without it in their environment, but in the same
  job, so this only keeps it out of anything gzip or jq might print.
- Branch rulesets (see `docs/hosting.md`) stop the `forecast-archive` branch being deleted or its
  history rewritten (any later change to a saved file shows as a new commit) and keep the workflow's
  token off `main`. The archive records what the build reported. The checks above stop saved files
  being replaced and new ones being dated before them, and the scorer ignores a forecast that its file
  says was published after the data for its target week. A build that had been tampered with could
  still hold back or misreport data, including the weeks forecasts are checked against, and it builds
  the published site: the public commit history and the publishers' own data are the check on that.
  The scorer reads the archive defensively (size and area caps, no symlinks, every number and date
  checked) and leaves out a folder it can't score rather than stopping.
- The built-in server serves only the site folder, with security headers, time
  limits and per-client connection caps. It's for your own machine or home network. To publish on the
  internet, use GitHub Pages or put Caddy or nginx in front (see `docs/hosting.md`).
