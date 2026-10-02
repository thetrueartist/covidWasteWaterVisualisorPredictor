# Where to host Sewer Signal

The app is a folder of static files (`site/`) plus a daily job that refreshes
the data in `site/data/`. It never needs a database or an always-on backend,
so it can live almost anywhere.

| Option | Cost | Who can see it | Effort | Good for |
|---|---|---|---|---|
| [Your own Linux box](#1-your-own-linux-box) | free | you, or your home network | `./install.sh --service` | a home server, Raspberry Pi or always-on PC |
| [GitHub Pages](#2-github-pages) | free | anyone with the link | one setting | sharing it publicly with zero upkeep |
| [Behind Caddy or nginx](#3-behind-caddy-or-nginx) | a server | anyone | a few lines of config | your own domain on a VPS |
| [Cloudflare Pages / Netlify](#4-cloudflare-pages-or-netlify) | free tier | anyone | a build setting + a deploy hook | if you already use them |

If you only want it for yourself, option 1 is the simplest. If you want to
send the link to friends or family, option 2 is the least work and costs
nothing.

## 1. Your own Linux box

```bash
git clone https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor.git sewer-signal
cd sewer-signal
./install.sh --service
```

This installs everything into the folder (a Python virtualenv, nothing
system-wide), builds the data and starts a small web server. It also adds a
daily refresh: systemd user units where available (`sewer-signal.service`
and `sewer-signal-refresh.timer`), otherwise cron. Open
`http://localhost:8000`, or whichever port it reports if 8000 was taken.

- **Other devices at home:** re-run with `--lan`. It then listens on all
  interfaces, so anything on your network can open
  `http://<the-box's-IP>:<port>`.
- **From outside your home:** don't port-forward it. Python's built-in web
  server is fine on a trusted network but isn't meant to face the internet.
  Use [Tailscale](https://tailscale.com/) (private, nothing exposed) or a
  [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
  or put it behind Caddy (option 3).
- **Hardware:** about 1 GB of disk. The daily build trains three forecast
  models (COVID-19, flu and RSV). It takes about 6 minutes on a 4-core
  machine, longer on a Raspberry Pi, and briefly needs about 600 MB of RAM.
  A Raspberry Pi 4 or 5 on a 64-bit OS copes fine.
- **Manage it:** `./install.sh status`, `./install.sh update`,
  `./install.sh uninstall`.

## 2. GitHub Pages

The repo already has a workflow (`.github/workflows/pages.yml`) that builds
the data every morning and publishes the site.

1. Make sure the code is on the repository's default branch. The workflow
   deploys from whichever branch that is.
2. In the repo, go to **Settings → Pages → Build and deployment → Source** and
   choose **GitHub Actions**.
3. Run the **Build data and deploy site** workflow once from the Actions tab,
   or wait for the next push or the daily schedule.

The site appears at
`https://thetrueartist.github.io/covidWasteWaterVisualisorPredictor/`.
Pages on a private repository needs a paid GitHub plan. On a public
repository it's free. While Pages is off, the workflow skips itself with a
notice instead of failing, so a private repo can use option 1 or 4 instead.

## 3. Behind Caddy or nginx

On a VPS with your own domain, let a real web server handle the files and
HTTPS, and keep only the daily refresh:

```bash
./install.sh --service
systemctl --user disable --now sewer-signal.service      # keep sewer-signal-refresh.timer
```

Caddy (`/etc/caddy/Caddyfile`), which gets HTTPS certificates automatically:

```
signal.example.com {
    root * /home/you/sewer-signal/site
    encode gzip
    file_server
}
```

nginx:

```nginx
server {
    server_name signal.example.com;
    root /home/you/sewer-signal/site;
    gzip on;
    gzip_types application/json text/css application/javascript;
    location / { try_files $uri $uri/ =404; }
}
```

The web server user needs read access to the `site/` folder.

## 4. Cloudflare Pages or Netlify

Point the service at this repo with:

- build command: `pip install . && python -m wastewater build`
- output directory: `site`
- Python 3.10 or newer

These services only rebuild when something triggers them. For fresh data
every day, create a deploy hook and call it on a schedule, for example from a
small GitHub Actions cron job that runs `curl -X POST <hook-url>`.
