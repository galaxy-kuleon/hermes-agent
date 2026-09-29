import fcntl
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from utils import atomic_json_write


logger = logging.getLogger(__name__)


def pending_path(home, sid):
    return Path(home) / "openviking" / "pending_sessions" / (quote(sid, safe="") + ".json")


def client_identity(client):
    return {"endpoint": client._endpoint, "account": client._account,
            "user_id": client._user, "actor_peer_id": client._agent}


def read_pending(home, sid):
    path = pending_path(home, sid)
    return json.loads(path.read_text()) if path.exists() else {}


def trusted_owner(home, sid):
    try:
        activity = json.loads((Path(home) / "session_activity.json").read_text())
        return str(activity.get(sid, {}).get("user_id") or "")
    except (OSError, ValueError, AttributeError):
        return ""


@contextmanager
def locked_record(home, sid, *, nonblocking=False):
    path = pending_path(home, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            record = read_pending(home, sid)
            yield record
            atomic_json_write(path, record, mode=0o600)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def bind_owner(record, identity, home, sid):
    existing = record.get("identity")
    if existing and existing != identity:
        raise PermissionError("OpenViking commit owner mismatch")
    if record and not existing and trusted_owner(home, sid) != identity["user_id"]:
        raise PermissionError("Legacy OpenViking recovery owner is unresolved")
    if record and not existing:
        record["legacy_recovery"] = True
    record.update(session_id=sid, identity=identity)


def mark_pending(home, sid, identity, owner_run_id):
    with locked_record(home, sid) as record:
        bind_owner(record, identity, home, sid)
        generation = int(record.get("generation", 0)) + 1
        record.update(generation=generation, owner_run_id=owner_run_id)
        record.setdefault("uploads", {})[str(generation)] = "pending"
        if record.get("status") in {None, "completed"}:
            record["status"] = "pending"
        return generation


def finish_upload(home, sid, identity, generation, success):
    with locked_record(home, sid) as record:
        bind_owner(record, identity, home, sid)
        record.setdefault("uploads", {})[str(generation)] = "ready" if success else "failed"


def _result(response):
    if response.get("status") == "error":
        raise RuntimeError(str(response.get("error") or "OpenViking request failed"))
    return response.get("result", response)


def _complete(record, generation):
    record.update(completed_generation=generation, completed_at=time.time(), last_error="",
                  next_attempt_at=0)
    if record.get("task_id"):
        record.setdefault("completed_tasks", []).append({
            "task_id": record["task_id"], "archive_uri": record.get("archive_uri"),
            "generation": generation})
    record.pop("task_id", None)
    record.pop("archive_uri", None)
    record["uploads"] = {k: v for k, v in record.get("uploads", {}).items() if int(k) > generation}
    record["status"] = "completed" if record.get("generation", 0) == generation else "pending"
    return record["status"] == "completed"


def commit_step(home, client, sid, payload):
    try:
        with locked_record(home, sid, nonblocking=True) as record:
            bind_owner(record, client_identity(client), home, sid)
            now = time.time()
            if record.get("status") == "completed":
                return True
            if now < float(record.get("next_attempt_at", 0)):
                return False
            posting = False
            try:
                task_id = record.get("task_id")
                if task_id:
                    task = _result(client.get(f"/api/v1/tasks/{quote(task_id, safe='')}", params={"include_events": True}, timeout=10))
                    if task.get("resource_id") != sid or task.get("task_type") != "session_commit":
                        raise PermissionError("OpenViking extraction task ownership mismatch")
                    state = task.get("status")
                    record["status"] = state
                    if state in {"completed", "failed", "cancelled"}:
                        record.setdefault("terminal_receipts", {})[task_id] = task
                    if state == "completed":
                        record.setdefault("terminal_receipts", {})[task_id] = task
                        return _complete(record, record.get("submitted_generation", 0))
                    if state == "failed":
                        record.setdefault("terminal_receipts", {})[task_id] = task
                        record["last_error"] = str(task.get("error") or "Extraction failed")
                        if record.get("retry_parent") != task_id:
                            count = int(record.get("retry_count", 0)) + 1
                            record.update(retry_parent=task_id, retry_count=count,
                                          next_attempt_at=now + min(3600, 30 * (2 ** min(count - 1, 7))))
                            return False
                        result = _result(client.post(f"/api/v1/sessions/{quote(sid, safe='')}/commit/retry",
                                                     {"task_id": task_id, "archive_uri": record.get("archive_uri")}, timeout=60))
                        record.setdefault("failed_tasks", []).append({"task_id": task_id, "error": record["last_error"]})
                    elif state == "cancelled":
                        record["last_error"] = "Extraction was cancelled; explicit retry required"
                        record["next_attempt_at"] = now + 3600
                        return False
                    else:
                        record["next_attempt_at"] = now + 15
                        return False
                else:
                    tasks = _result(client.get("/api/v1/tasks", params={
                        "task_type": "session_commit", "resource_id": sid, "limit": 200}, timeout=10))
                    if not isinstance(tasks, list):
                        raise RuntimeError("Invalid OpenViking task list")
                    if record.get("status") == "submitting":
                        known = set(record.get("known_tasks", []))
                        discovered = [t for t in tasks if t.get("task_id") not in known]
                        if len(discovered) == 1:
                            task = discovered[0]
                            record.update(task_id=task["task_id"], status="submitted",
                                          archive_uri=(task.get("result") or {}).get("archive_uri"), next_attempt_at=0)
                            return False
                        if discovered:
                            record["unresolved_task_ids"] = [t["task_id"] for t in discovered]
                        record["last_error"] = "Commit submission outcome unknown; recovery required"
                        record["next_attempt_at"] = now + 60
                        return False
                    unfinished = [t for t in tasks if t.get("status") in {"pending", "running", "cancelling", "failed"}
                                  and t["task_id"] not in record.get("terminal_receipts", {})]
                    if unfinished:
                        task = min(unfinished, key=lambda t: t.get("created_at", 0))
                        record.update(task_id=task["task_id"], status="submitted", submitted_generation=0,
                                      archive_uri=(task.get("result") or {}).get("archive_uri"), next_attempt_at=0)
                        return False
                    if any(state != "ready" for state in record.get("uploads", {}).values()):
                        record.update(status="upload_pending", last_error="Conversation upload is not confirmed complete",
                                      next_attempt_at=now + 30)
                        return False
                    record.update(status="submitting", submitted_generation=record.get("generation", 0))
                    record["known_tasks"] = [t["task_id"] for t in tasks]
                    atomic_json_write(pending_path(home, sid), record, mode=0o600)
                    posting = True
                    result = _result(client.post(f"/api/v1/sessions/{quote(sid, safe='')}/commit", payload, timeout=60))
                if result.get("task_id"):
                    record.update(task_id=result["task_id"], archive_uri=result.get("archive_uri"),
                                  status="submitted", next_attempt_at=now + 15, last_error="")
                    logger.info("OpenViking extraction submitted session=%s user=%s task=%s",
                                sid, client._user, result["task_id"])
                    return False
                if result.get("status") == "skipped" and not task_id:
                    if record.get("legacy_recovery") and not record.get("terminal_receipts"):
                        record.update(status="unresolved", last_error="Legacy archive outcome is not verified", next_attempt_at=now + 3600)
                        return False
                    return _complete(record, record.get("submitted_generation", 0))
                raise RuntimeError("OpenViking commit returned no extraction task")
            except Exception as exc:
                if posting and type(exc).__name__ in {"ConnectError", "ConnectTimeout", "PoolTimeout"} and not record.get("task_id"):
                    record["status"] = "pending"
                attempts = int(record.get("attempts", 0)) + 1
                record.update(attempts=attempts, last_error=str(exc),
                              next_attempt_at=now + min(3600, 15 * (2 ** min(attempts, 8))))
                logger.warning("OpenViking commit pending session=%s user=%s state=%s error=%s",
                               sid, client._user, record.get("status"), exc)
                return False
    except BlockingIOError:
        return False
