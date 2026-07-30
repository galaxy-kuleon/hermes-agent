"""Read-only export surface for committed platform-skill governance evidence.

The isolated shared-skill writer stores one target-only post-state archive in
the existing transaction journal before a mutation can commit.  This module
lets a host reconciler list and export those immutable events without mounting
the governance repository or Git credentials into the writer container.

It deliberately has no acknowledgement or deletion command.  Reconciliation
state belongs in Git; the writer journal remains an append-only source fact.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from tools.platform_skill_store import (
    PlatformSkillStoreError,
    TRANSACTION_ID_PATTERN,
    TRANSACTION_RECEIPT_FILENAME,
    default_transactions_dir,
)


EXIT_OK = 0
EXIT_CANNOT_DETERMINE = 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _event(
    receipt: dict[str, Any],
    *,
    transaction_id: str,
    rollback: bool,
) -> dict[str, Any] | None:
    if rollback:
        metadata = receipt.get("rollback_governance_outbox")
        generation = receipt.get("rollback_generation")
        event_id = f"rollback:{transaction_id}"
        operation = "rollback"
        result = receipt.get("result")
        before_hash = (
            result.get("after_hash") if isinstance(result, dict) else None
        )
        after_hash = (
            result.get("before_hash") if isinstance(result, dict) else None
        )
    else:
        result = receipt.get("result")
        metadata = (
            result.get("governance_outbox") if isinstance(result, dict) else None
        )
        generation = receipt.get("after_generation")
        event_id = transaction_id
        operation = str(receipt.get("operation") or "")
        before_hash = (
            result.get("before_hash") if isinstance(result, dict) else None
        )
        after_hash = result.get("after_hash") if isinstance(result, dict) else None
    if not isinstance(metadata, dict) or not isinstance(generation, int):
        return None
    request = receipt.get("request")
    target = str(request.get("target") or "") if isinstance(request, dict) else ""
    if not target:
        return None
    return {
        "schema_version": 1,
        "event_id": event_id,
        "transaction_id": transaction_id,
        "generation": generation,
        "operation": operation,
        "target": target,
        "state": metadata.get("state"),
        "destination": metadata.get("destination"),
        "tree_hash": metadata.get("tree_hash"),
        "before_hash": before_hash,
        "after_hash": after_hash,
        "archive": metadata.get("archive"),
        "archive_sha256": metadata.get("archive_sha256"),
    }


def collect_events(transactions_dir: Path) -> list[dict[str, Any]]:
    """Return committed mutation and rollback events in generation order."""

    transactions_dir = Path(transactions_dir)
    if not transactions_dir.is_dir():
        return []
    events: list[dict[str, Any]] = []
    for transaction_dir in sorted(transactions_dir.iterdir()):
        if (
            not transaction_dir.is_dir()
            or not TRANSACTION_ID_PATTERN.fullmatch(transaction_dir.name)
        ):
            continue
        receipt_path = transaction_dir / TRANSACTION_RECEIPT_FILENAME
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if receipt.get("transaction_id") != transaction_dir.name:
            continue
        if receipt.get("status") not in {"committed", "rolled_back"}:
            continue
        original = _event(
            receipt,
            transaction_id=transaction_dir.name,
            rollback=False,
        )
        if original is not None:
            events.append(original)
        if receipt.get("status") == "rolled_back":
            rollback = _event(
                receipt,
                transaction_id=transaction_dir.name,
                rollback=True,
            )
            if rollback is not None:
                events.append(rollback)
    return sorted(events, key=lambda event: (event["generation"], event["event_id"]))


def export_event(
    transactions_dir: Path, event_id: str
) -> dict[str, Any]:
    """Return one event plus base64 archive bytes, never journal paths."""

    events = {event["event_id"]: event for event in collect_events(transactions_dir)}
    event = events.get(event_id)
    if event is None:
        raise PlatformSkillStoreError("governance outbox event was not found")
    payload = dict(event)
    payload["archive_b64"] = None
    if event["state"] == "deleted":
        if event["archive"] is not None or event["archive_sha256"] is not None:
            raise PlatformSkillStoreError("invalid deleted governance outbox event")
        return payload
    if (
        event["state"] != "present"
        or not isinstance(event["archive"], str)
        or not isinstance(event["archive_sha256"], str)
    ):
        raise PlatformSkillStoreError("invalid present governance outbox event")
    transaction_id = str(event["transaction_id"])
    archive = Path(transactions_dir) / transaction_id / event["archive"]
    if archive.is_symlink() or not archive.is_file():
        raise PlatformSkillStoreError("governance outbox archive is unavailable")
    if archive.parent != Path(transactions_dir) / transaction_id:
        raise PlatformSkillStoreError("unsafe governance outbox archive path")
    if _sha256(archive) != event["archive_sha256"]:
        raise PlatformSkillStoreError("governance outbox archive digest mismatch")
    payload["archive_b64"] = base64.b64encode(archive.read_bytes()).decode("ascii")
    return payload


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transactions-dir",
        type=Path,
        default=default_transactions_dir(),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("event_id")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.command == "list":
            payload: dict[str, Any] = {
                "schema_version": 1,
                "events": collect_events(args.transactions_dir),
            }
        else:
            payload = export_event(args.transactions_dir, args.event_id)
        print(json.dumps(payload, sort_keys=True))
        return EXIT_OK
    except (OSError, PlatformSkillStoreError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "error": str(exc),
                },
                sort_keys=True,
            )
        )
        return EXIT_CANNOT_DETERMINE


if __name__ == "__main__":
    raise SystemExit(main())
