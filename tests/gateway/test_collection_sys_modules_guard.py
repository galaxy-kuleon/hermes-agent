"""Behavioural contract for the collection-time sys.modules guard."""

import shutil
import subprocess
import sys
from pathlib import Path

from tests.gateway.conftest import _preexisting_module_identity_changes


def test_guard_names_deleted_and_replaced_preexisting_modules():
    retained = object()
    deleted = object()
    replaced = object()
    replacement = object()

    before = {
        "retained": retained,
        "deleted": deleted,
        "replaced": replaced,
    }
    after = {
        "retained": retained,
        "replaced": replacement,
        "ordinary.new.import": object(),
    }

    assert _preexisting_module_identity_changes(before, after) == [
        "deleted:deleted",
        "replaced:replaced",
    ]


def test_guard_allows_normal_import_additions_and_identity_preservation():
    retained = object()

    assert _preexisting_module_identity_changes(
        {"retained": retained},
        {"retained": retained, "ordinary.new.import": object()},
    ) == []


def test_real_collection_boundary_rejects_and_names_a_polluter(tmp_path):
    """Prove the hook, not only its comparison helper, observes the import."""
    shutil.copy2(Path(__file__).with_name("conftest.py"), tmp_path / "conftest.py")
    (tmp_path / "test_polluter.py").write_text(
        "import sys\n"
        "from types import ModuleType\n"
        "sys.modules['telegram.constants'] = ModuleType('leaked.constants')\n"
        "def test_body_never_licenses_the_leak():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "test_polluter.py",
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    observed = result.stdout + result.stderr

    assert result.returncode == 2, observed
    assert "test_polluter.py polluted pre-existing sys.modules entries" in observed
    assert "replaced:telegram.constants" in observed
