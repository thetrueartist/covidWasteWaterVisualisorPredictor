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
