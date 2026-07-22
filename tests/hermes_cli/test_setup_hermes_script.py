import os
from pathlib import Path
import shutil
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "setup-hermes.sh"


def test_setup_hermes_script_is_valid_shell():
    result = subprocess.run(["bash", "-n", str(SETUP_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_setup_hermes_script_has_termux_path():
    content = SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "is_termux()" in content
    assert ".[termux]" in content
    assert "constraints-termux.txt" in content
    assert "$PREFIX/bin" in content


def _run_setup_with_fake_uv(tmp_path, *, docker_build, include_lockfile):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    shutil.copy2(SETUP_SCRIPT, project_dir / "setup-hermes.sh")
    if include_lockfile:
        (project_dir / "uv.lock").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv_log = tmp_path / "uv.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$UV_LOG"
if [ "$1" = "--version" ]; then
    echo "uv 0.test"
elif [ "$1" = "python" ] && [ "$2" = "find" ]; then
    command -v python3
elif [ "$1" = "venv" ]; then
    mkdir -p "$2/bin"
elif [ "$1" = "sync" ]; then
    exit 23
elif [ "$1" = "pip" ]; then
    exit 0
fi
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "CI": "true",
            "DEBIAN_FRONTEND": "noninteractive",
            "HOME": str(home),
            "HERMES_HOME": str(home / ".hermes"),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SHELL": "/bin/sh",
            "UV_LOG": str(uv_log),
            "UV_PYTHON_PREFERENCE": "only-system",
        }
    )
    if docker_build:
        env["DOCKER_BUILD"] = "true"
    else:
        env.pop("DOCKER_BUILD", None)

    result = subprocess.run(
        ["/bin/bash", str(project_dir / "setup-hermes.sh")],
        capture_output=True,
        env=env,
        text=True,
    )
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    return result, calls


def test_docker_setup_fails_closed_when_locked_sync_fails(tmp_path):
    result, calls = _run_setup_with_fake_uv(
        tmp_path, docker_build=True, include_lockfile=True
    )

    assert result.returncode != 0
    assert "Docker build requires a successful uv sync --locked" in result.stdout
    assert "sync --extra all --locked" in calls
    assert not any(call.startswith("pip install") for call in calls)


def test_docker_setup_fails_closed_without_lockfile(tmp_path):
    result, calls = _run_setup_with_fake_uv(
        tmp_path, docker_build=True, include_lockfile=False
    )

    assert result.returncode != 0
    assert "Docker build requires uv.lock" in result.stdout
    assert not any(call.startswith("sync ") for call in calls)
    assert not any(call.startswith("pip install") for call in calls)


def test_non_docker_setup_retains_unlocked_fallback(tmp_path):
    result, calls = _run_setup_with_fake_uv(
        tmp_path, docker_build=False, include_lockfile=True
    )

    assert result.returncode == 0, result.stderr
    assert "sync --extra all --locked" in calls
    assert any(call.startswith("pip install") for call in calls)
