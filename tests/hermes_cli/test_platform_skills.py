import argparse
import json
from pathlib import Path

from hermes_cli.subcommands.platform_skills import (
    build_platform_skills_parser,
    cmd_platform_skills,
)


def _parser():
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")
    build_platform_skills_parser(subs)
    return parser


def test_parser_exposes_operator_commands():
    parser = _parser()
    for command in ("plan", "sync-bundled", "put", "delete", "verify", "rollback"):
        argv = ["platform-skills", command]
        if command == "put":
            argv.append("/tmp/source")
        elif command == "delete":
            argv.append("skill-name")
        elif command == "rollback":
            argv.append("transaction-id")
        args = parser.parse_args(argv)
        assert args.platform_skills_command == command
        assert args.func is cmd_platform_skills


def test_plan_command_is_read_only_and_machine_readable(tmp_path, capsys):
    source = tmp_path / "source"
    platform = tmp_path / "skills"
    source.mkdir()
    platform.mkdir()
    skill = source / "canary"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: canary\ndescription: Canary.\n---\n\n# Canary\n"
    )
    args = _parser().parse_args(
        [
            "platform-skills",
            "plan",
            "--source",
            str(source),
            "--platform-root",
            str(platform),
        ]
    )

    rc = args.func(args)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["add"] == ["canary"]
    assert list(platform.iterdir()) == []

