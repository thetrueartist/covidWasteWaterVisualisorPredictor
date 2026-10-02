#!/usr/bin/env bash
# Sewer Signal installer for Linux.
#
#   ./install.sh                install, download the data, train the model, start the site
#   ./install.sh --service      ...and keep it running in the background with a daily data refresh
#   ./install.sh update         pull the latest code, reinstall, rebuild, restart
#   ./install.sh status         show what's installed and where it's listening
#   ./install.sh uninstall      stop and remove the background service (keeps the folder)
#
# Careful by default:
#   * everything lives in one folder and a Python virtualenv; nothing is installed system-wide
#   * sudo is only used to install a missing system package, and only after asking
#   * listens on 127.0.0.1 (this machine only) unless you pass --lan
#   * if the port is taken it moves to the next free one; it never stops other programs
#   * it only edits or removes files and cron lines it created (marked "sewer-signal")

set -euo pipefail

APP="sewer-signal"
MARKER="# managed by sewer-signal install.sh"
REPO_URL="https://github.com/thetrueartist/covidWasteWaterVisualisorPredictor.git"
MIN_PY_MINOR=10

# ---------- defaults (overridable by flags) ----------
CMD="install"
APP_DIR=""
BRANCH=""
HOST="127.0.0.1"
PORT="8000"
PORT_SET=0
WANT_SERVICE=0
ASSUME_YES=0
SKIP_BUILD=0
NO_START=0
COUNTRIES=""

# ---------- output ----------
if [ -t 1 ]; then
  B=$'\e[1m'; DIM=$'\e[2m'; RED=$'\e[31m'; GRN=$'\e[32m'; YLW=$'\e[33m'; BLU=$'\e[34m'; RST=$'\e[0m'
else
  B=""; DIM=""; RED=""; GRN=""; YLW=""; BLU=""; RST=""
fi
step() { printf '\n%s==>%s %s%s%s\n' "$BLU" "$RST" "$B" "$*" "$RST"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '  %s!%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '\n%sError:%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

ask() { # ask "question" default(y|n) -> returns 0 for yes
  local prompt="$1" def="${2:-n}" reply
  if [ "$ASSUME_YES" = 1 ]; then return 0; fi
  if [ ! -t 0 ]; then [ "$def" = y ]; return; fi
  local hint="[y/N]"; [ "$def" = y ] && hint="[Y/n]"
  read -r -p "  $prompt $hint " reply || reply=""
  reply="${reply:-$def}"
  [[ "$reply" =~ ^[Yy] ]]
}

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Options:
  --dir PATH          where to install (default: this folder if run from a clone, else ~/$APP)
  --port N            preferred port (default 8000; the next free one is used if it's taken)
  --lan               listen on all interfaces so other devices on your network can open it
  --service           run in the background and refresh the data daily (systemd user units,
                      or cron if systemd isn't available)
  --countries LIST    only build some countries, e.g. scotland,usa (faster, less memory)
  --branch NAME       git branch to check out when cloning
  --no-build          skip the data download/model build
  --no-start          don't start the site at the end
  -y, --yes           don't ask questions (installs missing packages with sudo if needed)
  -h, --help          show this help
EOF
}

# ---------- arguments ----------
while [ $# -gt 0 ]; do
  case "$1" in
    install|update|status|uninstall) CMD="$1" ;;
    --dir) APP_DIR="${2:?--dir needs a path}"; shift ;;
    --port) PORT="${2:?--port needs a number}"; PORT_SET=1; shift ;;
    --lan) HOST="0.0.0.0" ;;
    --service) WANT_SERVICE=1 ;;
    --countries) COUNTRIES="${2:?--countries needs a list}"; shift ;;
    --branch) BRANCH="${2:?--branch needs a name}"; shift ;;
    --no-build) SKIP_BUILD=1 ;;
    --no-start) NO_START=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done
# Strict input checks: these values end up in cron lines and systemd units.
KNOWN_COUNTRIES="scotland usa canada germany netherlands new-zealand"
if ! [[ "$PORT" =~ ^[1-9][0-9]{3,4}$ ]] || [ "$PORT" -gt 65535 ]; then
  die "--port must be a number from 1024 to 65535"
fi
if [ -n "$COUNTRIES" ]; then
  [[ "$COUNTRIES" =~ ^[a-z-]+(,[a-z-]+)*$ ]] || die "--countries must look like scotland,usa"
  IFS=',' read -r -a _wanted <<< "$COUNTRIES"
  for _c in "${_wanted[@]}"; do
    [[ " $KNOWN_COUNTRIES " == *" $_c "* ]] || die "unknown country '$_c' (choose from: $KNOWN_COUNTRIES)"
  done
fi
if [ -n "$BRANCH" ] && ! [[ "$BRANCH" =~ ^[A-Za-z0-9._][A-Za-z0-9._/-]*$ ]]; then
  die "--branch must be a plain git branch name"
fi

[ "$(uname -s)" = "Linux" ] || die "this script is for Linux. On macOS/Windows follow the manual steps in the README."

# ---------- locate the app ----------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
is_app_dir() { [ -f "$1/pyproject.toml" ] && [ -d "$1/wastewater" ] && [ -d "$1/site" ]; }
if [ -z "$APP_DIR" ]; then
  if [ -n "$SCRIPT_DIR" ] && is_app_dir "$SCRIPT_DIR"; then APP_DIR="$SCRIPT_DIR"; else APP_DIR="$HOME/$APP"; fi
fi
case "$APP_DIR" in /*) ;; *) APP_DIR="$PWD/$APP_DIR" ;; esac
APP_DIR="$(realpath -m -- "$APP_DIR" 2>/dev/null || printf '%s' "$APP_DIR")"
# Paths go into cron and systemd lines, so keep them to plain characters.
[[ "$APP_DIR" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || die "install folder '$APP_DIR' contains spaces or special characters; choose another with --dir"
STATE_FILE="$APP_DIR/.sewer-signal.env"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PID_FILE="$APP_DIR/.server.pid"
LOG_FILE="$APP_DIR/.server.log"

load_state() {
  # Only our own KEY=value lines; never source arbitrary content.
  [ -f "$STATE_FILE" ] || return 0
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      SAVED_HOST) if [[ "$value" == "127.0.0.1" || "$value" == "0.0.0.0" ]]; then SAVED_HOST="$value"; fi ;;
      SAVED_PORT) if [[ "$value" =~ ^[1-9][0-9]{3,4}$ ]] && [ "$value" -le 65535 ]; then SAVED_PORT="$value"; fi ;;
      SAVED_MODE) if [[ "$value" =~ ^(manual|systemd|cron)$ ]]; then SAVED_MODE="$value"; fi ;;
    esac
  done < "$STATE_FILE"
  return 0
}
save_state() {
  safe_target "$STATE_FILE"
  (umask 077; printf '%s\nSAVED_HOST=%s\nSAVED_PORT=%s\nSAVED_MODE=%s\n' "$MARKER" "$HOST" "$PORT" "$1" > "$STATE_FILE")
}
SAVED_HOST=""; SAVED_PORT=""; SAVED_MODE=""

# ---------- checks ----------
pkg_manager() {
  for pm in apt-get dnf yum pacman zypper apk; do
    command -v "$pm" >/dev/null 2>&1 && { echo "$pm"; return; }
  done
}

python_ok() {
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "$MIN_PY_MINOR" <<'PY' >/dev/null 2>&1
import sys, ensurepip, venv  # ensurepip is missing when Debian's python3-venv isn't installed
sys.exit(0 if sys.version_info >= (3, int(sys.argv[1])) else 1)
PY
}

install_packages() {
  local pm missing=("$@")
  pm="$(pkg_manager)"
  [ -n "$pm" ] || die "missing: ${missing[*]}. Install them with your package manager and run this again."
  local -a cmd
  case "$pm" in
    apt-get) cmd=(apt-get install -y) ;;
    dnf|yum) cmd=("$pm" install -y) ;;
    pacman) cmd=(pacman -S --needed --noconfirm) ;;
    zypper) cmd=(zypper --non-interactive install) ;;
    apk) cmd=(apk add) ;;
  esac
  local sudo=()
  if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "missing: ${missing[*]} and sudo isn't available. Ask an admin to run: ${cmd[*]} ${missing[*]}"
    sudo=(sudo)
  fi
  warn "Missing system packages: ${missing[*]}"
  info "This needs: ${sudo[*]} ${cmd[*]} ${missing[*]}"
  ask "Install them now?" n || die "cancelled. Install them yourself and run this again."
  if [ "$pm" = apt-get ]; then "${sudo[@]}" apt-get update -qq; fi
  "${sudo[@]}" "${cmd[@]}" "${missing[@]}"
}

check_requirements() {
  step "Checking requirements"
  local pm missing=()
  pm="$(pkg_manager)"
  command -v git >/dev/null 2>&1 || missing+=(git)
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  if ! python_ok; then
    case "$pm" in
      apt-get) missing+=(python3 python3-venv python3-pip) ;;
      dnf|yum|zypper) missing+=(python3 python3-pip) ;;
      pacman) missing+=(python python-pip) ;;
      apk) missing+=(python3 py3-pip) ;;
      *) missing+=(python3) ;;
    esac
  fi
  if [ ${#missing[@]} -gt 0 ]; then install_packages "${missing[@]}"; fi
  python_ok || die "Python 3.$MIN_PY_MINOR or newer with venv support is required (found: $(python3 --version 2>&1 || echo none))."
  ok "$(python3 --version), git, curl"

  local parent free_mb
  parent="$APP_DIR"; while [ ! -d "$parent" ]; do parent="$(dirname "$parent")"; done
  free_mb="$(df -Pm "$parent" | awk 'NR==2 {print $4}')"
  if [ "${free_mb:-0}" -lt 1024 ]; then
    warn "only ${free_mb} MB free under $parent; the install needs about 1 GB"
    ask "Continue anyway?" n || die "cancelled"
  fi
  if [ "$(id -u)" -eq 0 ]; then
    warn "You're running as root. That isn't needed and isn't recommended for a web server."
    [ "$WANT_SERVICE" = 0 ] || die "for --service, run this as a normal user (it installs a per-user service)"
    ask "Continue as root?" n || die "cancelled"
  fi
}

# ---------- code ----------
get_code() {
  step "Getting the code"
  if is_app_dir "$APP_DIR"; then
    ok "using $APP_DIR"
    return
  fi
  if [ -e "$APP_DIR" ] && [ -n "$(ls -A "$APP_DIR" 2>/dev/null)" ]; then
    die "$APP_DIR exists and isn't a Sewer Signal folder. Pick another place with --dir."
  fi
  local args=(clone --depth 1)
  [ -n "$BRANCH" ] && args+=(--branch "$BRANCH")
  git "${args[@]}" "$REPO_URL" "$APP_DIR"
  ok "cloned into $APP_DIR"
}

setup_venv() {
  step "Installing Python packages (in $VENV)"
  if [ -x "$PY" ] && ! "$PY" -c 'import sys' >/dev/null 2>&1; then
    warn "existing virtualenv is broken; recreating it"
    rm -rf -- "$VENV"
  fi
  [ -x "$PY" ] || python3 -m venv "$VENV"
  "$PY" -m pip install --quiet --upgrade pip || warn "couldn't upgrade pip; carrying on with the bundled one"
  # Prefer the exact, hash-checked versions CI tests with. If that fails (an
  # unusual Python or platform with no matching wheels), fall back to the
  # newest compatible releases. The app itself runs from $APP_DIR.
  if [ -f "$APP_DIR/requirements.txt" ] \
    && "$PY" -m pip install --quiet --require-hashes -r "$APP_DIR/requirements.txt"; then
    ok "numpy, pandas, scikit-learn, requests (pinned versions)"
  else
    warn "the pinned versions didn't install; trying the latest compatible ones"
    "$PY" -m pip install --quiet -e "$APP_DIR" \
      || die "pip couldn't install the Python packages (details above). Check your internet connection or proxy, then run this again."
    ok "numpy, pandas, scikit-learn, requests"
  fi
}

build_data() {
  [ "$SKIP_BUILD" = 1 ] && { info "skipping the data build (--no-build)"; return; }
  step "Downloading wastewater data and training the forecasts (5-10 minutes)"
  local args=(-m wastewater build)
  [ -n "$COUNTRIES" ] && args+=(--countries "$COUNTRIES")
  if (cd "$APP_DIR" && "$PY" "${args[@]}"); then
    ok "data written to $APP_DIR/site/data"
  elif [ -f "$APP_DIR/site/data/index.json" ]; then
    warn "the build failed; keeping the previous data"
  else
    die "the data build failed. Check your internet connection and run: $0 update"
  fi
}

# ---------- ports ----------
port_free() { # port_free host port -> 0 if we could listen there
  "$PY" - "$1" "$2" <<'PY' 2>/dev/null
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
probe = "127.0.0.1" if host in ("0.0.0.0", "") else host
try:  # something already answering?
    with socket.create_connection((probe, port), timeout=0.3):
        sys.exit(1)
except OSError:
    pass
try:  # same options the server uses, so a just-closed port counts as free
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
except OSError:
    sys.exit(1)
PY
}

pick_port() {
  local p="$PORT" last=$((PORT + 100))
  while [ "$p" -le "$last" ] && [ "$p" -le 65535 ]; do
    if port_free "$HOST" "$p"; then
      [ "$p" != "$PORT" ] && warn "port $PORT is in use by something else; using $p instead"
      PORT="$p"
      return
    fi
    p=$((p + 1))
  done
  die "no free port between $PORT and $last. Choose another with --port."
}

# ---------- background service ----------
has_systemd_user() {
  [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1
}

# A file is ours if it doesn't exist yet, or is a regular file whose first line is our marker.
ours() {
  [ -L "$1" ] && return 1
  [ ! -e "$1" ] && return 0
  [ -f "$1" ] && [ "$(head -n 1 -- "$1")" = "$MARKER" ]
}

# Never write through a symlink someone else planted.
safe_target() { [ ! -L "$1" ] || die "$1 is a symlink; refusing to write through it"; }

write_unit() { # write_unit name content
  local path="$UNIT_DIR/$1"
  ours "$path" || die "$path already exists and wasn't created by this script; not touching it"
  printf '%s\n%s\n' "$MARKER" "$2" > "$path"
}

install_systemd() {
  mkdir -p "$UNIT_DIR"
  write_unit "$APP.service" "[Unit]
Description=Sewer Signal (COVID-19, flu and RSV wastewater dashboard) on http://$HOST:$PORT
After=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$PY -m wastewater serve --host $HOST --port $PORT
Restart=on-failure
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=default.target"
  local build_args="-m wastewater build"
  [ -n "$COUNTRIES" ] && build_args+=" --countries $COUNTRIES"
  write_unit "$APP-refresh.service" "[Unit]
Description=Refresh Sewer Signal wastewater data and forecasts
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
ExecStart=$PY $build_args
TimeoutStartSec=1h
Nice=10
NoNewPrivileges=true"
  write_unit "$APP-refresh.timer" "[Unit]
Description=Daily Sewer Signal data refresh

[Timer]
OnCalendar=*-*-* 07:00
RandomizedDelaySec=45min
Persistent=true

[Install]
WantedBy=timers.target"
  systemctl --user daemon-reload
  systemctl --user enable --now "$APP.service" "$APP-refresh.timer" >/dev/null
  wait_until_up "$PORT" || die "the service didn't come up; see: journalctl --user -u $APP.service"
  ok "systemd user service '$APP' running; data refreshes daily ($APP-refresh.timer)"
  if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes; then
    if loginctl enable-linger "$USER" >/dev/null 2>&1; then
      ok "enabled lingering so it keeps running after you log out"
    else
      info "To keep it running after you log out: sudo loginctl enable-linger $USER"
    fi
  fi
}

stop_pid() { # stop_pid pid: ask politely, wait up to 5s for it to exit
  kill "$1" 2>/dev/null || return 0
  for _ in $(seq 50); do kill -0 "$1" 2>/dev/null || return 0; sleep 0.1; done
  warn "process $1 is slow to stop"
}

wait_until_up() { # wait_until_up port: poll for up to 15s
  for _ in $(seq 30); do
    curl -fsS -o /dev/null "http://127.0.0.1:$1/" 2>/dev/null && return 0
    sleep 0.5
  done
  return 1
}

server_pid() { # pid of our background server, if it's really ours
  [ -f "$PID_FILE" ] && [ ! -L "$PID_FILE" ] || return 1
  local pid; pid="$(cat "$PID_FILE")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -qF "$PY -m wastewater serve" || return 1
  echo "$pid"
}

CRON_TAG=" # $APP-cron"

cron_replace() { # cron_replace "new lines": drops only lines ending in our tag, keeps everything else as is
  local current
  current="$(crontab -l 2>/dev/null | awk -v tag="$CRON_TAG" '
    length($0) >= length(tag) && substr($0, length($0) - length(tag) + 1) == tag { next }
    { print }' || true)"
  {
    if [ -n "$current" ]; then printf '%s\n' "$current"; fi
    if [ -n "$1" ]; then printf '%s\n' "$1"; fi
  } | crontab -
}

has_our_cron() {
  crontab -l 2>/dev/null | awk -v tag="$CRON_TAG" '
    length($0) >= length(tag) && substr($0, length($0) - length(tag) + 1) == tag { found = 1 }
    END { exit !found }'
}

start_background() {
  local pid
  if pid="$(server_pid)"; then stop_pid "$pid"; fi
  safe_target "$PID_FILE"
  safe_target "$LOG_FILE"
  (cd "$APP_DIR" || exit 1; umask 077; nohup "$PY" -m wastewater serve --host "$HOST" --port "$PORT" >> "$LOG_FILE" 2>&1 & echo $! > "$PID_FILE")
  if ! wait_until_up "$PORT" || ! server_pid >/dev/null; then die "the server didn't start; see $LOG_FILE"; fi
}

install_cron() {
  command -v crontab >/dev/null 2>&1 || die "neither systemd user services nor cron are available. Start it yourself with: $(start_hint)"
  local build_args="-m wastewater build"
  [ -n "$COUNTRIES" ] && build_args+=" --countries $COUNTRIES"
  # Paths and arguments were validated to plain characters above, so these lines can't be bent.
  cron_replace "15 7 * * * cd $APP_DIR && timeout 3600 $PY $build_args >> $APP_DIR/.refresh.log 2>&1$CRON_TAG
@reboot cd $APP_DIR || exit 1; umask 077; $PY -m wastewater serve --host $HOST --port $PORT >> $LOG_FILE 2>&1 & echo \$! > $PID_FILE$CRON_TAG"
  start_background
  ok "running in the background (pid $(server_pid)); cron refreshes data daily and restarts it after a reboot"
}

stop_existing() {
  if has_systemd_user && [ -f "$UNIT_DIR/$APP.service" ] && ours "$UNIT_DIR/$APP.service"; then
    systemctl --user stop "$APP.service" 2>/dev/null || true
  fi
  local pid
  if pid="$(server_pid)"; then stop_pid "$pid"; fi
}

start_hint() { printf 'cd %q && %q -m wastewater serve --host %q --port %q' "$APP_DIR" "$PY" "$HOST" "$PORT"; }

# ---------- commands ----------
show_url() {
  printf '\n%s%sSewer Signal is ready%s\n' "$B" "$GRN" "$RST"
  if [ "$HOST" = "0.0.0.0" ]; then
    local ip; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '  Open %shttp://localhost:%s%s here, or %shttp://%s:%s%s from other devices on your network.\n' "$B" "$PORT" "$RST" "$B" "${ip:-<this-machine-IP>}" "$PORT" "$RST"
    info "${DIM}Anyone on your network can see it. Don't port-forward it to the internet; see the README for hosting options.${RST}"
  else
    printf '  Open %shttp://localhost:%s%s\n' "$B" "$PORT" "$RST"
  fi
}

cmd_install() {
  load_state
  # Re-running keeps the previous port and background mode unless told otherwise.
  if [ "$PORT_SET" = 0 ] && [ -n "$SAVED_PORT" ]; then PORT="$SAVED_PORT"; fi
  if [ "$WANT_SERVICE" = 0 ] && { [ "$SAVED_MODE" = systemd ] || [ "$SAVED_MODE" = cron ]; }; then
    info "Keeping the existing background service (remove it with: $0 uninstall)"
    WANT_SERVICE=1
  fi
  check_requirements
  get_code
  setup_venv
  build_data
  stop_existing
  step "Choosing a port"
  pick_port
  ok "$HOST:$PORT"
  if [ "$WANT_SERVICE" = 1 ]; then
    step "Setting up the background service"
    if has_systemd_user; then install_systemd; save_state systemd; else warn "systemd user services aren't available here; using cron instead"; install_cron; save_state cron; fi
    show_url
    info "Manage it with: $0 status | update | uninstall"
    return
  fi
  save_state manual
  show_url
  info "Later: run ${B}$0 --service${RST} to keep it running and refresh data daily."
  if [ "$NO_START" = 1 ]; then
    info "Start it with: $(start_hint)"
    return
  fi
  info "Starting now. Press Ctrl+C to stop."
  cd "$APP_DIR" && exec "$PY" -m wastewater serve --host "$HOST" --port "$PORT"
}

cmd_update() {
  is_app_dir "$APP_DIR" || die "no install found at $APP_DIR (use --dir)"
  load_state
  HOST="${SAVED_HOST:-$HOST}"; PORT="${SAVED_PORT:-$PORT}"
  step "Updating the code"
  if [ -d "$APP_DIR/.git" ]; then
    if [ -n "$(git -C "$APP_DIR" status --porcelain --untracked-files=no)" ]; then
      warn "you have local changes; skipping git pull"
    else
      git -C "$APP_DIR" pull --ff-only && ok "code is up to date"
    fi
  fi
  setup_venv
  build_data
  case "$SAVED_MODE" in
    systemd) systemctl --user restart "$APP.service" && ok "service restarted" ;;
    cron) start_background && ok "server restarted" ;;
    *) info "Start it with: $(start_hint)" ;;
  esac
}

cmd_status() {
  load_state
  printf '%sFolder%s    %s\n' "$B" "$RST" "$APP_DIR"
  if [ -x "$PY" ]; then printf '%sPython%s    %s\n' "$B" "$RST" "$("$PY" --version)"; else printf '%sPython%s    not installed\n' "$B" "$RST"; fi
  local data="$APP_DIR/site/data/index.json"
  if [ -f "$data" ]; then
    printf '%sData%s      built %s\n' "$B" "$RST" "$(grep -o '"generated_at":"[^"]*"' "$data" | head -1 | cut -d'"' -f4)"
  else
    printf '%sData%s      not built yet\n' "$B" "$RST"
  fi
  printf '%sMode%s      %s\n' "$B" "$RST" "${SAVED_MODE:-not installed}"
  if [ "$SAVED_MODE" = systemd ] && has_systemd_user; then
    printf '%sService%s   %s, refresh timer %s\n' "$B" "$RST" "$(systemctl --user is-active "$APP.service" 2>/dev/null || true)" "$(systemctl --user is-active "$APP-refresh.timer" 2>/dev/null || true)"
  elif [ "$SAVED_MODE" = cron ]; then
    local pid; if pid="$(server_pid)"; then printf '%sServer%s    running (pid %s)\n' "$B" "$RST" "$pid"; else printf '%sServer%s    not running\n' "$B" "$RST"; fi
  fi
  if [ -n "$SAVED_PORT" ]; then
    if curl -fsS -o /dev/null "http://127.0.0.1:$SAVED_PORT/data/index.json" 2>/dev/null; then
      printf '%sURL%s       http://localhost:%s (responding)\n' "$B" "$RST" "$SAVED_PORT"
    else
      printf '%sURL%s       http://localhost:%s (not responding)\n' "$B" "$RST" "$SAVED_PORT"
    fi
  fi
}

cmd_uninstall() {
  load_state
  step "Removing the background service"
  local systemd=0 leftover=0
  has_systemd_user && systemd=1
  for unit in "$APP.service" "$APP-refresh.timer" "$APP-refresh.service"; do
    [ -e "$UNIT_DIR/$unit" ] || [ -L "$UNIT_DIR/$unit" ] || continue
    if ours "$UNIT_DIR/$unit"; then
      [ "$systemd" = 1 ] && systemctl --user disable --now "$unit" >/dev/null 2>&1
      rm -f -- "$UNIT_DIR/$unit"
      ok "removed $unit"
    else
      warn "$UNIT_DIR/$unit wasn't created by this script; left it alone"
      leftover=1
    fi
  done
  [ "$systemd" = 1 ] && systemctl --user daemon-reload 2>/dev/null
  if command -v crontab >/dev/null 2>&1 && has_our_cron; then
    cron_replace ""
    if has_our_cron; then warn "couldn't remove the cron entries; check with: crontab -l"; leftover=1; else ok "removed cron entries"; fi
  fi
  local pid; if pid="$(server_pid)"; then stop_pid "$pid"; ok "stopped server (pid $pid)"; fi
  [ -L "$PID_FILE" ] || rm -f -- "$PID_FILE"
  if ours "$STATE_FILE" && [ -e "$STATE_FILE" ]; then rm -f -- "$STATE_FILE"; fi
  [ "$leftover" = 0 ] || warn "some pieces were left in place (see above)"
  info "The folder is still there. To delete it: rm -rf $(printf '%q' "$APP_DIR")"
}

case "$CMD" in
  install) cmd_install ;;
  update) cmd_update ;;
  status) cmd_status ;;
  uninstall) cmd_uninstall ;;
esac
