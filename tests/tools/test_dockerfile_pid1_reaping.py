"""Container runtime contracts for the hosted Hermes fork.

These checks intentionally follow the build delegation into setup-hermes.sh.
The Dockerfile copies the complete archived source and invokes that script;
requiring duplicate npm/uv commands in the Dockerfile would create a second,
drift-prone installation policy.
"""

from pathlib import Path
import tomllib


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
SETUP = REPO_ROOT / "setup-hermes.sh"


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _setup() -> str:
    return SETUP.read_text(encoding="utf-8")


def test_dockerfile_installs_tini_for_zombie_reaping() -> None:
    assert "    tini \\\n" in _dockerfile()


def test_dockerfile_entrypoint_routes_through_tini() -> None:
    assert 'ENTRYPOINT ["/usr/bin/tini", "-g", "--"' in _dockerfile()


def test_dockerfile_delegates_one_install_policy_to_setup_script() -> None:
    text = _dockerfile()
    assert "ENV DOCKER_BUILD=true" in text
    assert "RUN bash /opt/hermes/setup-hermes.sh" in text


def test_docker_build_requires_locked_curated_dependencies() -> None:
    setup = _setup()
    assert "$UV_CMD sync --extra all --locked" in setup
    assert "Docker build requires a successful uv sync --locked" in setup
    assert "Docker build requires uv.lock" in setup


def test_opt_in_backends_are_not_eagerly_baked() -> None:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        optional = tomllib.load(handle)["project"]["optional-dependencies"]
    all_specs = set(optional["all"])
    for extra in ("messaging", "matrix", "hindsight"):
        assert f"hermes-agent[{extra}]" not in all_specs


def test_setup_builds_tui_and_image_fails_closed_if_bundle_missing() -> None:
    assert "(cd ui-tui && npm run build" in _setup()
    assert "test -f /opt/hermes/ui-tui/dist/entry.js" in _dockerfile()


def test_local_tui_workspace_source_is_in_build_context() -> None:
    assert (REPO_ROOT / "ui-tui" / "packages" / "hermes-ink").is_dir()
    ignored = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "ui-tui/packages/hermes-ink/" not in ignored


def test_dockerignore_excludes_generated_dependency_dirs() -> None:
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "**/node_modules" in text
    assert "**/.venv" in text
