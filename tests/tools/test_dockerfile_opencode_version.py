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


def test_versioned_release_asset_is_downloaded_without_mutable_installer() -> None:
    block = _install_block()
    assert "opencode.ai/install" not in block
    assert "releases/download/v${OPENCODE_VERSION}" in block
    assert "opencode-linux-${asset_arch}.tar.gz" in block


def test_each_supported_platform_has_an_exact_release_checksum() -> None:
    block = _install_block()
    checksums = dict(
        re.findall(
            r"^ARG OPENCODE_LINUX_(AMD64|ARM64)_SHA256=([0-9a-f]{64})$",
            block,
            re.MULTILINE,
        )
    )
    assert checksums == {
        "AMD64": "6b1113da704253fb4da12b41e4236acecb9f2b62949c945f6eeacaa15111b976",
        "ARM64": "41ae3041e91b894e4c0dc06a73a9a2796254bf390ffb99626a43af5e2912d170",
    }
    assert 'echo "${expected_sha256}  ${archive}" | sha256sum -c -' in block


def test_build_fails_if_release_returns_another_version() -> None:
    block = _install_block()
    assert (
        'test "$(/usr/local/bin/opencode --version)" = "${OPENCODE_VERSION}"'
        in block
    )


def test_image_baked_cli_precedes_mutable_npm_global_volume() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    path_line = next(line for line in text.splitlines() if line.startswith("ENV PATH="))
    assert path_line.index("/usr/local/bin") < path_line.index(
        "/home/hermes/.npm-global/bin"
    )
