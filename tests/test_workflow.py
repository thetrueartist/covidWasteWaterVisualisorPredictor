"""The Pages workflow's archive steps, run as GitHub would run them, against local git repositories."""

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/pages.yml"

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("bash", "git", "base64")), reason="needs bash, git and base64")


def steps():
    """Each step of every job, as its lines (no YAML parser needed: steps start with '      - ')."""
    out, current = [], None
    for line in WORKFLOW.read_text().splitlines():
        if line.startswith("      - "):
            current = [line]
            out.append(current)
        elif current is not None and (line.startswith("        ") or not line.strip()):
            current.append(line)
        else:
            current = None
    return out


def step(key: str) -> dict:
    """The step whose name or id is ``key``: its ``if``, other plain fields and ``run`` script."""
    for lines in steps():
        text = "\n".join(lines)
        if re.search(rf"^\s+(- )?(name|id): {re.escape(key)}$", text, re.M):
            fields = dict(re.findall(r"^ {6}[- ] (\S[\w-]*): (.*)$", text, re.M))
            run = None
            for i, line in enumerate(lines):
                if re.match(r"^ {8}run: \|$", line):
                    run = "\n".join(x[10:] for x in lines[i + 1:])
                elif re.match(r"^ {8}run: \S", line):
                    run = line.split("run: ", 1)[1]
            return {**fields, "run": run}
    raise KeyError(key)


def git(*args, cwd=None, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


@pytest.fixture
def remote(tmp_path):
    """A bare repository standing in for github.com/owner/repo, reached through a private HOME."""
    bare = tmp_path / "remote/owner/repo"
    git("init", "--quiet", "--bare", str(bare))
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "file://{tmp_path}/remote/"]\n\tinsteadOf = https://github.com/\n'
                                     "[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "LANG": "C.UTF-8"}
    return bare, env


def push_branch(tmp_path, env, branch, files):
    work = tmp_path / f"work-{branch.replace('/', '-')}"
    git("init", "--quiet", str(work), env=env)
    for rel, text in files.items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
    git("add", ".", cwd=work, env=env)
    git("commit", "--quiet", "-m", "x", cwd=work, env=env)
    git("push", "--quiet", "https://github.com/owner/repo", f"HEAD:refs/heads/{branch}", cwd=work, env=env)


def run_fetch(tmp_path, env, repo="owner/repo"):
    """The build job's "Get the forecast archive" step in a fresh workspace: (outputs, stdout+stderr, workspace)."""
    ws = tmp_path / "ws"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir()
    out = ws / "github-output"
    out.write_text("")
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step("archive")["run"]], cwd=ws,
                       env={**env, "REPO": repo, "TOKEN": "ghs_test", "GITHUB_OUTPUT": str(out)},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    return outputs, r.stdout + r.stderr, ws


def test_a_branch_merely_ending_in_forecast_archive_is_not_the_archive(tmp_path, remote):
    """ls-remote matches names by their ending. Asked for "forecast-archive", it also found
    fix/forecast-archive, then the clone failed and the archive could never start."""
    _, env = remote
    push_branch(tmp_path, env, "main", {"README.md": "code"})
    push_branch(tmp_path, env, "fix/forecast-archive", {"notes.txt": "work in progress"})
    outputs, log, ws = run_fetch(tmp_path, env)
    assert outputs["ok"] == "true" and "No forecast-archive branch yet" in log
    assert (ws / "archive").is_dir() and not any((ws / "archive").iterdir())


def test_the_archive_branch_is_fetched_without_its_history(tmp_path, remote):
    _, env = remote
    push_branch(tmp_path, env, "forecast-archive", {"v2/covid/usa/2026-10-06T051700Z.jsonl.gz": "x", "README.md": "r"})
    push_branch(tmp_path, env, "fix/forecast-archive", {"notes.txt": "work in progress"})
    outputs, _, ws = run_fetch(tmp_path, env)
    assert outputs["ok"] == "true"
    assert (ws / "archive/v2/covid/usa/2026-10-06T051700Z.jsonl.gz").is_file() and not (ws / "archive/.git").exists()


def test_an_unreachable_repository_means_no_archive_this_run(tmp_path, remote):
    _, env = remote
    outputs, log, ws = run_fetch(tmp_path, env, repo="owner/missing")
    assert outputs["ok"] == "false" and "Forecast archive unavailable" in log and not (ws / "archive").exists()


def test_both_jobs_ask_for_the_branch_by_its_full_name():
    text = WORKFLOW.read_text()
    calls = re.findall(r"git [^\n]*ls-remote[^\n]*", text)
    assert len(calls) == 2 and all("refs/heads/forecast-archive" in c and "--heads" not in c for c in calls)


def test_the_gatekeeper_reads_the_new_files_without_the_write_token(tmp_path, remote):
    """gzip and jq parse the build's files, which aren't trusted, so the token isn't in their
    environment. (Only a separate job would be a hard boundary: this keeps it out of anything
    those tools could print or dump.) The step still saves the files with the token."""
    from tests.archive_helpers import area, put

    bare, env = remote
    ws = tmp_path / "ws"
    (ws / "code/.github/scripts").mkdir(parents=True)
    for name in ("check-new-forecasts.sh", "forecast-archive-README.md"):
        shutil.copy(ROOT / ".github/scripts" / name, ws / "code/.github/scripts" / name)
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    sunday = now.date() - timedelta(days=(now.weekday() + 1) % 7)
    put(ws / "new", now, [area("r1", as_of=sunday.isoformat())], country="germany")
    shims, log = tmp_path / "shims", tmp_path / "tools.log"
    shims.mkdir()
    for tool in ("gzip", "jq"):
        (shims / tool).write_text(f'#!/bin/bash\necho "{tool} TOKEN=${{TOKEN-unset}}" >> {log}\nexec {shutil.which(tool)} "$@"\n')
        (shims / tool).chmod(0o755)
    r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step("Check and save the new forecasts")["run"]],
                       cwd=ws, env={**env, "PATH": f"{shims}:{env['PATH']}", "REPO": "owner/repo", "TOKEN": "ghs_secret",
                                    "RUN_URL": "https://github.com/owner/repo/actions/runs/1"},
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Saved 1 new forecast file(s)." in r.stdout
    calls = log.read_text().splitlines()
    assert {c.split()[0] for c in calls} == {"gzip", "jq"}
    assert all(c.endswith("TOKEN=unset") for c in calls), calls
    assert git("ls-tree", "-r", "--name-only", "forecast-archive", cwd=bare, env=env).stdout.count(".jsonl.gz") == 1


def test_scoring_has_its_own_time_limit_and_never_stops_the_deploy():
    score = step("Score the forecast archive")
    assert score["id"] == "score" and score["continue-on-error"] == "true" and int(score["timeout-minutes"]) <= 15


@pytest.mark.parametrize("ok,reason", [("true", "score"), ("false", "fetch")])
def test_a_track_record_that_couldnt_be_made_says_so(tmp_path, ok, reason):
    """A failed fetch (the build then writes "not recording") or a failed score (no file at all)
    must not leave the official site saying it isn't recording."""
    note = step("Note that the track record couldn't be updated")
    # outcome, not conclusion: continue-on-error makes a failed step's conclusion "success"
    assert note["if"] == "steps.score.outcome != 'success'"
    (tmp_path / "site/data").mkdir(parents=True)
    (tmp_path / "site/data/track-record.json").write_text('{"schema":1,"recording":false}')
    subprocess.run(["bash", "-eo", "pipefail", "-c", note["run"]], cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "ARCHIVE_OK": ok},
                   check=True, timeout=60)
    data = json.loads((tmp_path / "site/data/track-record.json").read_text())
    assert data["schema"] == 1 and data["recording"] is True and data["available"] is False and data["reason"] == reason
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", data["generated_at"])
