#!/usr/bin/env bash
# check-new-forecasts.sh NEW_DIR BRANCH_DIR [TODAY [NOW]]
#
# The gatekeeper for the forecast archive. The archive job (the only job that
# can write to the repository) runs it on the files the build job handed over
# in NEW_DIR, against a fresh copy of the forecast-archive branch in
# BRANCH_DIR. The build job runs third-party packages, so its files are
# untrusted: only small, well-formed forecast files from one build, issued
# today (or yesterday, for a run that crosses midnight UTC), not in the
# future, later than everything already saved for their country and virus,
# and at most one per country and virus, may pass. Anything unexpected stops
# the whole save, loudly.
#
# TODAY (YYYY-MM-DD) and NOW (YYYY-MM-DDTHHMMSSZ, the file-name form) default
# to the current UTC time; with only TODAY given, NOW is the end of that day.
#
# Prints the accepted paths (relative to NEW_DIR), one per line. Needs only
# bash, coreutils, gzip and jq, all on GitHub's Ubuntu runners.
set -euo pipefail
export LC_ALL=C

usage="usage: check-new-forecasts.sh NEW_DIR BRANCH_DIR [TODAY [NOW]]"
new=${1:?$usage}
branch=${2:?$usage}
clock=$(date -u +%Y-%m-%dT%H%M%SZ)
today=${3:-${clock:0:10}}
if [ $# -ge 4 ]; then now=$4; elif [ $# -ge 3 ]; then now="${today}T235959Z"; else now=$clock; fi

viruses='covid|flu|rsv'
countries='scotland|usa|canada|germany|netherlands|new-zealand'
max_files=18            # one per virus and country: 3 x 6
max_gz_bytes=1000000    # the biggest real issue is about 40 kB gzipped (archive.MAX_GZ_BYTES)
max_raw_bytes=5000000   # and about 250 kB decompressed (archive.MAX_FILE_BYTES)
max_areas=1000          # and has 136 areas (archive.MAX_AREAS)
max_age_days=35         # older data in a "new" forecast could be back-dated into a gap
max_ahead_days=7        # weeks end on Sundays: at most the end of this week, plus a day for New Zealand
min_offset=1e-9         # the model's smallest offset (archive.MIN_OFFSET)

# Untrusted names never reach the log as they are: they could carry
# workflow commands or terminal escapes.
show() { printf '%s' "${1:0:200}" | tr -c 'A-Za-z0-9._/-' '?'; }
fail() { echo "::error title=Forecast archive check failed::$*" >&2; exit 1; }

[[ "$today" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fail "TODAY must be YYYY-MM-DD"
today_s=$(date -u -d "$today" +%s) || fail "TODAY isn't a date"
[ "$(date -u -d "@$today_s" +%F)" = "$today" ] || fail "TODAY isn't a date"
[[ "$now" =~ ^${today}T[0-9]{6}Z$ ]] || fail "NOW must be YYYY-MM-DDTHHMMSSZ on TODAY"
yesterday=$(date -u -d "@$((today_s - 86400))" +%F)
[ -d "$new" ] && [ ! -L "$new" ] || fail "$(show "$new") is not a folder"
[ -d "$branch" ] || fail "$(show "$branch") is not a folder"

tmp=$(mktemp)
trap 'rm -f -- "$tmp"' EXIT

# Nothing but plain files and folders: no symlinks, devices, sockets or pipes.
odd=$(find "$new" -mindepth 1 ! -type f ! -type d -printf '%P\n' | head -n 1)
[ -z "$odd" ] || fail "not a regular file: $(show "$odd")"

mapfile -d '' -t files < <(cd "$new" && find . -type f -printf '%P\0' | sort -z)
[ "${#files[@]}" -le "$max_files" ] || fail "too many files (${#files[@]}, at most $max_files)"

re="^v2/($viruses)/($countries)/(([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z)\.jsonl\.gz$"
issue_name='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6}Z\.jsonl\.gz$'
declare -A folders=()
build_stamp=
for f in "${files[@]}"; do
  [[ "$f" =~ $re ]] || fail "unexpected file name: $(show "$f")"
  virus=${BASH_REMATCH[1]} country=${BASH_REMATCH[2]} stamp=${BASH_REMATCH[3]} day=${BASH_REMATCH[4]}
  hh=${BASH_REMATCH[5]} mm=${BASH_REMATCH[6]} ss=${BASH_REMATCH[7]}
  issued="${day}T${hh}:${mm}:${ss}Z"
  [ "$day" = "$today" ] || [ "$day" = "$yesterday" ] || fail "$f is not dated today ($today) or yesterday"
  [ "$hh" -le 23 ] && [ "$mm" -le 59 ] && [ "$ss" -le 59 ] || fail "$f has an impossible time"
  if [[ "$stamp" > "$now" ]]; then fail "$f is dated in the future (it's now $now)"; fi
  path="$new/$f"
  [ "$(stat -c %h -- "$path")" -eq 1 ] || fail "$f is a hard link"
  size=$(stat -c %s -- "$path")
  [ "$size" -le "$max_gz_bytes" ] || fail "$f is too large ($size bytes)"

  # One build writes one file per virus and country, all with the build's start time.
  [ -z "${folders[$virus/$country]:-}" ] || fail "more than one new file for $virus/$country"
  folders[$virus/$country]=1
  build_stamp=${build_stamp:-$stamp}
  [ "$stamp" = "$build_stamp" ] || fail "$f has a different time from the other files (one build has one issue time)"

  # Never replace a saved file, or write through a link in the branch.
  for p in v2 "v2/$virus" "v2/$virus/$country"; do
    [ ! -L "$branch/$p" ] || fail "$p is a symlink on the branch"
  done
  if [ -e "$branch/$f" ] || [ -L "$branch/$f" ]; then fail "$f is already saved"; fi
  # Nor slip a file in front of one already saved: the scorer takes the first
  # issue for each week of data, so an earlier name would replace a forecast
  # that was really published.
  newest=$(find "$branch/v2/$virus/$country" -mindepth 1 -maxdepth 1 -printf '%f\n' 2>/dev/null \
    | grep -E "$issue_name" | sort | tail -n 1 || true)
  if [ -n "$newest" ] && ! [[ "$stamp.jsonl.gz" > "$newest" ]]; then
    fail "$f is not later than the newest saved issue for $virus/$country"
  fi

  gzip -t -- "$path" 2>/dev/null || fail "$f is not valid gzip"
  # Decompress once, at most max_raw_bytes + 1 (a gzip bomb stops there).
  { gzip -dc -- "$path" 2>/dev/null || true; } | head -c $((max_raw_bytes + 1)) > "$tmp"
  [ "$(stat -c %s -- "$tmp")" -le "$max_raw_bytes" ] || fail "$f is too large once decompressed"

  # Line 1: the header for this virus and country, issued at the time in the name.
  head -n 1 -- "$tmp" | jq -e -s --arg v "$virus" --arg c "$country" --arg t "$issued" '
    length == 1 and (.[0] | type == "object" and .type == "header" and .schema == 2
      and .virus == $v and .country == $c and .issued == $t and (.content_hash | type == "string"))
  ' >/dev/null 2>&1 || fail "$f: the header is missing or doesn't match the file name"

  # Every other line: one area, with plain ids, recent data and well-formed forecasts.
  tail -n +2 -- "$tmp" | jq -e -s --arg day "$day" --argjson age "$max_age_days" --argjson ahead "$max_ahead_days" \
      --argjson max_areas "$max_areas" --argjson min_offset "$min_offset" '
    def finite: type == "number" and (isnan | not) and (isinfinite | not);
    def isodate: type == "string" and test("^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
      and ((. + "T00:00:00Z" | fromdateiso8601 | todate[0:10]) == .);
    ($day + "T00:00:00Z" | fromdateiso8601) as $issued
    | length > 0 and length <= $max_areas and all(.[];
        type == "object" and .type == "area"
        and (.region | type == "string" and test("^[^<>[:cntrl:]]{1,100}$"))
        and (.data_as_of | isodate)
        and (.latest | finite) and (.offset | finite and . >= $min_offset)
        and (.thr | type == "array" and length == 4 and all(.[]; finite))
        and ((.data_as_of + "T00:00:00Z" | fromdateiso8601) as $as_of
          | ($as_of / 86400) % 7 == 3
          and ($issued - $as_of) <= $age * 86400 and ($as_of - $issued) <= $ahead * 86400
          and (.f | type == "array" and length >= 1 and length <= 6 and all(.[];
                type == "object"
                and (.h | IN(1, 2, 3, 4, 5, 6))
                and .date == ($as_of + .h * 7 * 86400 | todate[0:10])
                and (.q | type == "array" and length == 5 and all(.[]; finite))
                and (.probs | type == "array" and length == 5 and all(.[]; finite and . >= 0 and . <= 1))))))
  ' >/dev/null 2>&1 || fail "$f: an area line is malformed, has old or future data, or a target date that doesn't follow its data, or there are too many"

  echo "$f"
done
echo "${#files[@]} new forecast file(s) passed the checks." >&2
