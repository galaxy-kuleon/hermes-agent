"""Executable offline contract for image-baked OpenCode CLI provenance."""

import hashlib
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
INSTALLER = ROOT / "scripts" / "install_opencode.sh"
EXPECTED_VERSION = "1.18.10"
EXPECTED = {
    "amd64": (
        "x64",
        "6b1113da704253fb4da12b41e4236acecb9f2b62949c945f6eeacaa15111b976",
    ),
    "arm64": (
        "arm64",
        "41ae3041e91b894e4c0dc06a73a9a2796254bf390ffb99626a43af5e2912d170",
    ),
}


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), *args], capture_output=True, text=True, check=False
    )


def _fake_archive(root: Path, reported_version: str) -> Path:
    binary = root / "opencode"
    binary.write_text(
        f"#!/usr/bin/env sh\nprintf '%s\\n' {reported_version!r}\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    archive = root / "opencode.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(binary, arcname="opencode")
    return archive


def test_dockerfile_has_no_overrideable_release_fact_build_args() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG TARGETARCH" in text
    assert "ARG OPENCODE_VERSION" not in text
    assert "ARG OPENCODE_LINUX_" not in text
    assert 'bash /tmp/install_opencode.sh --targetarch "${TARGETARCH}"' in text
    assert "opencode.ai/install" not in text


def test_supported_architectures_select_exact_official_assets_offline() -> None:
    for target, (asset, checksum) in EXPECTED.items():
        result = _run(INSTALLER, "--targetarch", target, "--print-selection")
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{EXPECTED_VERSION} {asset} {checksum}"


def test_unknown_architecture_fails_closed_offline() -> None:
    result = _run(INSTALLER, "--targetarch", "riscv64", "--print-selection")
    assert result.returncode != 0
    assert "unsupported OpenCode target architecture" in result.stderr


def test_wrong_checksum_fails_before_install_offline() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = _fake_archive(root, EXPECTED_VERSION)
        install_dir = root / "installed"
        result = _run(
            INSTALLER,
            "--targetarch",
            "arm64",
            "--archive",
            str(archive),
            "--install-dir",
            str(install_dir),
        )
        assert result.returncode != 0
        assert not (install_dir / "opencode").exists()


def test_wrong_reported_version_fails_after_verified_archive_offline() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = _fake_archive(root, "0.0.0-wrong")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        script = root / "install_opencode.sh"
        source = INSTALLER.read_text(encoding="utf-8")
        source = source.replace(EXPECTED["arm64"][1], digest)
        script.write_text(source, encoding="utf-8")
        result = _run(
            script,
            "--targetarch",
            "arm64",
            "--archive",
            str(archive),
            "--install-dir",
            str(root / "installed"),
        )
        assert result.returncode != 0


def test_image_baked_cli_precedes_mutable_npm_global_volume() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    path_line = next(line for line in text.splitlines() if line.startswith("ENV PATH="))
    assert path_line.index("/usr/local/bin") < path_line.index(
        "/home/hermes/.npm-global/bin"
    )
