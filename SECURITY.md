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
- The built-in server serves only the site folder, with security headers, time
  limits and per-client connection caps. It's for your own machine or home network. To publish on the
  internet, use GitHub Pages or put Caddy or nginx in front (see `docs/hosting.md`).
