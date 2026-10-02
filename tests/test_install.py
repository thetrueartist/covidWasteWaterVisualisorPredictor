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
