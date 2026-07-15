from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_entrypoint_uses_read_only_safe_startup_sync_and_external_state():
    script = (REPO_ROOT / "docker" / "entrypoint.sh").read_text()

    assert 'skills_sync.py" --startup' in script
    assert '"$HERMES_HOME/skill-state/platform"' in script
    assert '"$HERMES_HOME/user-skills"' in script
    assert 'chown -R hermes:hermes "$HERMES_HOME"' not in script
    assert '"$HERMES_HOME"/{cron,sessions,logs,hooks,memories,skills,' not in script


def test_stage2_does_not_chown_or_seed_platform_content():
    script = (REPO_ROOT / "docker" / "stage2-hook.sh").read_text()
    chown_loop = script.split("for sub in ", 1)[1].split("; do", 1)[0].split()
    seed_block = script.split("as_hermes mkdir -p \\\n", 1)[1].split(
        "# --- Install-method stamp", 1
    )[0]

    assert "skills" not in chown_loop
    assert '"$HERMES_HOME/skills"' not in seed_block
    assert '"$HERMES_HOME/skill-state/platform"' in seed_block
    assert 'skills_sync.py" --startup' in script
    assert "explicit startup skill sync failed" in script
