"""Static contract for the image-baked OpenCode CLI provenance."""

import re
from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"
EXPECTED_VERSION = "1.18.10"


def _install_block() -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = text.index("# ── Install OpenCode CLI system-wide")
    end = text.index("# ── Copy hermes-agent source", start)
    return text[start:end]


def test_opencode_version_has_one_archive_bound_literal() -> None:
    block = _install_block()
    declarations = re.findall(r"^ARG OPENCODE_VERSION=(\S+)$", block, re.MULTILINE)
    assert declarations == [EXPECTED_VERSION]


def test_installer_receives_the_pinned_version() -> None:
    block = _install_block()
    assert '--version "${OPENCODE_VERSION}"' in block
    assert "bash -s -- --no-modify-path" in block


def test_build_fails_if_installer_returns_another_version() -> None:
    block = _install_block()
    assert (
        'test "$(/usr/local/bin/opencode --version)" = "${OPENCODE_VERSION}"'
        in block
    )


def test_unversioned_installer_invocation_cannot_return() -> None:
    block = _install_block()
    installer_line = next(line for line in block.splitlines() if "opencode.ai/install" in line)
    assert "--version" in installer_line
