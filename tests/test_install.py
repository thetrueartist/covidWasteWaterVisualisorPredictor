"""install.sh rejects hostile arguments before it touches anything."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run(*args):
    return subprocess.run(["bash", str(ROOT / "install.sh"), *args, "--no-build", "--no-start"],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(
    "args",
    [
        ["--countries", "scotland\n* * * * * touch /tmp/pwned"],
        ["--countries", "scotland;id"],
        ["--countries", "atlantis"],
        ["--dir", "/tmp/a b"],
        ["--dir", "/tmp/x'$(id)'"],
        ["--dir", "/tmp/x\nExecStartPre=/bin/sh"],
        ["--dir", "/tmp/100%"],
        ["--branch", "--upload-pack=touch /tmp/pwned"],
        ["--branch", "main;id"],
        ["--port", "08000"],
        ["--port", "80"],
        ["--port", "1000"],
        ["--countries", "scotland,atlantis"],
        ["--port", "70000"],
        ["--frobnicate"],
    ],
)
def test_hostile_arguments_are_rejected(args):
    result = run(*args)
    assert result.returncode != 0
    assert "Error:" in result.stderr or "unknown option" in result.stderr


def test_help_works():
    result = subprocess.run(["bash", str(ROOT / "install.sh"), "--help"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "--service" in result.stdout


def test_status_reads_the_saved_state(tmp_path):
    app = tmp_path / "app"
    for d in ("wastewater", "site"):
        (app / d).mkdir(parents=True)
    (app / "pyproject.toml").write_text("")
    # Hand-edited: junk lines are ignored, and the last line has no newline.
    (app / ".sewer-signal.env").write_text(
        "# managed by sewer-signal install.sh\nSAVED_PORT=8123\nSAVED_HOST=$(id)\n"
        "SAVED_COUNTRIES=scotland;id\nSAVED_COUNTRIES=scotland,usa\nSAVED_MODE=cron"
    )
    result = subprocess.run(["bash", str(ROOT / "install.sh"), "status", "--dir", str(app)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "cron" in out and "scotland,usa" in out and "localhost:8123" in out
    assert "id)" not in out


def test_status_shows_the_forecast_record(tmp_path):
    app = tmp_path / "app"
    for d in ("wastewater", "site"):
        (app / d).mkdir(parents=True)
    (app / "pyproject.toml").write_text("")

    def status():
        result = subprocess.run(["bash", str(ROOT / "install.sh"), "status", "--dir", str(app)],
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return next(line for line in result.stdout.splitlines() if line.startswith("Record"))

    assert "no forecasts saved yet" in status()
    for rel in ("v2/covid/scotland/2026-10-06T051700Z.jsonl.gz", "v2/flu/usa/2026-10-07T051800Z.jsonl.gz",
                "v2/flu/usa/notes.jsonl.gz"):
        (app / "forecast-archive" / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / "forecast-archive" / rel).write_bytes(b"x" * 100)
    line = status()
    assert "3 files" in line and "newest saved 2026-10-07 05:18 UTC" in line and "forecast-archive" in line


def test_the_daily_refresh_saves_and_scores():
    text = (ROOT / "install.sh").read_text()
    assert "ExecStart=$PY $build_args\nExecStart=$PY $(score_args)\n" in text
    cron = next(line for line in text.splitlines() if line.lstrip().startswith('cron_replace "15 7'))
    assert "|| exit 1; timeout 3600 $PY $build_args" in cron and "; timeout 600 $PY $(score_args)" in cron
    assert '--archive "$RECORD_DIR"' in text and "-m wastewater score --archive $RECORD_DIR" in text
