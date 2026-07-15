"""Out-of-band platform-skill administration CLI.

The command is intentionally a thin adapter over ``tools.platform_skill_store``.
Chat and gateway processes do not receive the writer environment marker or a
read-write platform mount, so they cannot activate this path.
"""

from __future__ import annotations

import json
from pathlib import Path


def _common_paths(parser) -> None:
    parser.add_argument("--platform-root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--transactions-dir", type=Path)


def _expected_generation(parser) -> None:
    parser.add_argument(
        "--expected-generation",
        type=int,
        help="Refuse the write unless the current platform generation matches.",
    )


def build_platform_skills_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "platform-skills",
        help="Transactional out-of-band platform-skill administration",
    )
    commands = parser.add_subparsers(dest="platform_skills_command", required=True)

    plan = commands.add_parser("plan", help="Compare a source tree without writing")
    plan.add_argument("--source", type=Path)
    plan.add_argument("--platform-root", type=Path)

    sync = commands.add_parser("sync-bundled", help="Transactionally sync bundled skills")
    _common_paths(sync)
    _expected_generation(sync)

    put = commands.add_parser("put", help="Transactionally add or replace one skill")
    put.add_argument("source", type=Path)
    put.add_argument("--destination")
    _common_paths(put)
    _expected_generation(put)

    delete = commands.add_parser("delete", help="Transactionally delete one named skill")
    delete.add_argument("name")
    _common_paths(delete)
    _expected_generation(delete)

    verify = commands.add_parser("verify", help="Validate the platform content tree")
    verify.add_argument("--platform-root", type=Path)

    rollback = commands.add_parser("rollback", help="Rollback the latest eligible transaction")
    rollback.add_argument("transaction_id")
    _common_paths(rollback)

    for command in commands.choices.values():
        command.set_defaults(func=cmd_platform_skills)


def cmd_platform_skills(args) -> int:
    from tools.platform_skill_store import PlatformSkillStoreError

    try:
        return _cmd_platform_skills(args)
    except (PlatformSkillStoreError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2


def _cmd_platform_skills(args) -> int:
    from tools import platform_skill_store as store

    command = args.platform_skills_command
    platform_root = getattr(args, "platform_root", None)
    state_dir = getattr(args, "state_dir", None)
    transactions_dir = getattr(args, "transactions_dir", None)

    if command == "plan":
        source = args.source or store.bundled_source_dir()
        target = platform_root or store.platform_skills_dir()
        result = store.build_plan(source, target)
    elif command == "sync-bundled":
        result = store.sync_bundled(
            target_root=platform_root,
            state_dir=state_dir,
            transactions_dir=transactions_dir,
            expected_generation=args.expected_generation,
        )
    elif command == "put":
        result = store.put_skill(
            args.source,
            destination=args.destination,
            target_root=platform_root,
            state_dir=state_dir,
            transactions_dir=transactions_dir,
            expected_generation=args.expected_generation,
        )
    elif command == "delete":
        result = store.delete_skill(
            args.name,
            target_root=platform_root,
            state_dir=state_dir,
            transactions_dir=transactions_dir,
            expected_generation=args.expected_generation,
        )
    elif command == "verify":
        result = store.verify_store(platform_root or store.platform_skills_dir())
    elif command == "rollback":
        result = store.rollback_transaction(
            args.transaction_id,
            target_root=platform_root,
            state_dir=state_dir,
            transactions_dir=transactions_dir,
        )
    else:  # argparse requires one of the registered values
        raise ValueError(f"unsupported platform-skills command: {command}")

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", True) else 1
