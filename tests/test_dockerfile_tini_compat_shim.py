"""Contract tests for this fork's tini-based container entrypoint.

The hosted stack deliberately uses a small tini PID 1 plus the bootstrap
entrypoint.  Upstream's s6 ``/init`` contract is a different image design and
must not be reintroduced here by stale tests.
"""

from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"


def _text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def test_tini_is_installed() -> None:
    assert "    tini \\\n" in _text()


def test_entrypoint_routes_bootstrap_through_tini() -> None:
    assert (
        'ENTRYPOINT ["/usr/bin/tini", "-g", "--", '
        '"/opt/hermes/docker/entrypoint.sh"]'
    ) in _text()


def test_s6_entrypoint_is_not_part_of_this_image_contract() -> None:
    assert 'ENTRYPOINT [ "/init"' not in _text()
    assert "ln -sf /init /usr/bin/tini" not in _text()
