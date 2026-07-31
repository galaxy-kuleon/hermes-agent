"""Static contracts for the hosted Docker install/state boundary."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _seal_block() -> str:
    text = _text()
    start = text.index("# ── Seal the runtime install")
    end = text.index("# ── Runtime config", start)
    return text[start:end]


def test_source_tree_is_root_owned_and_non_writable_at_runtime() -> None:
    block = _seal_block()
    assert "chown -R root:root /opt/hermes" in block
    assert "chmod -R a+rX,a-w /opt/hermes" in block


def test_only_venv_is_carved_back_out_for_allowlisted_lazy_dependencies() -> None:
    block = _seal_block()
    assert "chown -R hermes:hermes /opt/hermes/venv" in block
    assert "chmod -R u+w /opt/hermes/venv" in block
    for path in ("gateway", "tools", "skills", "ui-tui", "node_modules"):
        assert f"chown -R hermes:hermes /opt/hermes/{path}" not in block


def test_mutable_state_stays_under_explicit_home_mounts_without_volume() -> None:
    text = _text()
    assert "ENV HERMES_HOME=/home/hermes" in text
    assert "\nVOLUME " not in text


def test_runtime_avoids_source_and_tui_mutation_without_disabling_lazy_policy() -> None:
    text = _text()
    assert "ENV PYTHONDONTWRITEBYTECODE=1" in text
    assert "ENV HERMES_TUI_DIR=/opt/hermes/ui-tui" in text
    assert "ENV HERMES_DISABLE_LAZY_INSTALLS=1" not in text


def test_image_bakes_code_scoped_install_method_stamp_before_sealing() -> None:
    block = _seal_block()
    assert "printf 'docker\\n' > /opt/hermes/.install_method" in block
    assert block.index(".install_method") < block.index("chmod -R a+rX,a-w")
