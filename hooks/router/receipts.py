"""Explicit, local B1 receipt recording. Coordinator attestations are never host proof."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

import common


compat = common.compat
FILE_NAME = "receipts.sqlite3"
SCHEMA_VERSION = 1
MAX_EVENT_BYTES = 64 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
HEX16_OR_64 = re.compile(r"(?:[0-9a-f]{16}|[0-9a-f]{64})\Z")
HEX40_OR_64 = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
VERIFICATION = {"state": "unverified", "verdict": "UNKNOWN", "provenance": "unavailable"}


class ReceiptError(ValueError):
    """Invalid receipt input or storage, with a bounded, content-free diagnostic."""


def _reject(reason):
    raise ReceiptError(reason)


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        _reject("invalid receipt fields")


def _identifier(value):
    if not isinstance(value, str) or IDENTIFIER.fullmatch(value) is None:
        _reject("invalid receipt identifier")
    return value


def _digest(value, pattern):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _reject("invalid receipt digest")
    return value


def _positive(value):
    if type(value) is not int or value < 1:
        _reject("invalid receipt revision")
    return value


def _requirements(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        _reject("invalid receipt requirements")
    result = [_identifier(item) for item in value]
    if len(set(result)) != len(result):
        _reject("duplicate receipt requirement")
    return result


def _artifact(value):
    if value is None:
        return None
    _keys(value, (), ("revision", "readback"))
    if not value:
        _reject("empty receipt artifact")
    result = {}
    if "revision" in value:
        result["revision"] = _digest(value["revision"], HEX40_OR_64)
    if "readback" in value:
        result["readback"] = _digest(value["readback"], HEX64)
    return result


def _evidence(value):
    if not isinstance(value, dict) or len(value) > 32:
        _reject("invalid receipt evidence")
    result = {}
    for requirement, identifiers in value.items():
        _identifier(requirement)
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 32:
            _reject("invalid receipt evidence")
        checked = [_identifier(item) for item in identifiers]
        if len(set(checked)) != len(checked):
            _reject("duplicate receipt evidence")
        result[requirement] = checked
    return result


def _payload(payload):
    _keys(payload, ("alignment_digest", "requirements"), ("brief_id", "artifact"))
    brief = payload.get("brief_id")
    if "brief_id" in payload:
        _digest(brief, HEX16_OR_64)
    return {"brief_id": brief, "alignment_digest": _digest(payload["alignment_digest"], HEX64),
            "requirements": _requirements(payload["requirements"]), "artifact": _artifact(payload.get("artifact"))}


def _event(event):
    if not isinstance(event, dict):
        _reject("invalid receipt event")
    kind = event.get("kind")
    fields = {
        "revise": ("event_id", "kind", "alignment_digest", "requirements", "artifact"),
        "attempt": ("event_id", "kind", "attempt_id", "actor_id", "revision", "state"),
        "attestation": ("event_id", "kind", "attempt_id", "verifier_id", "revision", "artifact",
                        "verdict", "evidence", "provenance"),
    }
    if not isinstance(kind, str) or kind not in fields:
        _reject("invalid receipt event kind")
    _keys(event, fields[kind])
    try:
        encoded = json.dumps(event, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        _reject("invalid receipt event JSON")
    if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
        _reject("receipt event too large")
    _identifier(event["event_id"])
    if kind == "revise":
        checked = {"event_id": event["event_id"], "kind": kind,
                   "alignment_digest": _digest(event["alignment_digest"], HEX64),
                   "requirements": _requirements(event["requirements"]), "artifact": _artifact(event["artifact"])}
    elif kind == "attempt":
        if event["state"] not in ("running", "returned", "failed", "abandoned"):
            _reject("invalid receipt attempt state")
        checked = {"event_id": event["event_id"], "kind": kind,
                   "attempt_id": _identifier(event["attempt_id"]), "actor_id": _identifier(event["actor_id"]),
                   "revision": _positive(event["revision"]), "state": event["state"]}
    else:
        verifier = event["verifier_id"]
        if verifier is not None:
            _identifier(verifier)
        if event["verdict"] not in ("PASS", "FAIL", "UNKNOWN"):
            _reject("invalid receipt verdict")
        if event["provenance"] not in ("coordinator-recorded", "unavailable"):
            _reject("invalid receipt provenance")
        if event["provenance"] == "unavailable" and (event["verdict"] != "UNKNOWN" or verifier is not None):
            _reject("invalid unavailable attestation")
        if verifier is None and event["verdict"] != "UNKNOWN":
            _reject("invalid receipt verifier")
        checked = {"event_id": event["event_id"], "kind": kind,
                   "attempt_id": _identifier(event["attempt_id"]), "verifier_id": verifier,
                   "revision": _positive(event["revision"]), "artifact": _artifact(event["artifact"]),
                   "verdict": event["verdict"], "evidence": _evidence(event["evidence"]),
                   "provenance": event["provenance"]}
    return checked, encoded


def _location(state_dir):
    if state_dir is None:
        state_dir = common.state_path()
    if not isinstance(state_dir, Path):
        _reject("invalid receipt state directory")
    return state_dir / FILE_NAME


def _safe_path(path, directory=False, private=False):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not compat.owned(info) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        _reject("unsafe receipt storage")
    if private and os.name != "nt" and info.st_mode & 0o077:
        _reject("unsafe receipt storage")
    return True


def _tighten(path, directory=False):
    if not compat.CAN_FCHMOD:
        return
    flags = compat.READ_FLAGS | (compat.DIRECTORY if directory else 0)
    fd = compat.open_fd(path, flags)
    try:
        if not compat.owned(os.fstat(fd)):
            _reject("unsafe receipt storage")
        compat.set_private(fd, 0o700 if directory else 0o600)
    finally:
        os.close(fd)


def _open(path, *, create=False, write=False):
    """Return a checked connection or None for absent read storage."""
    # Inspect lexical ancestors before mkdir or SQLite can follow them. Do not
    # resolve/collapse '..': that would hide a symlink traversed by the OS.
    for ancestor in reversed(path.absolute().parents):
        try:
            info = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            _reject("unsafe receipt storage")
    parent = path.parent
    if not _safe_path(parent, directory=True):
        if not create:
            return None
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _safe_path(parent, directory=True)
    exists = _safe_path(path)
    for suffix in ("-journal", "-wal", "-shm"):
        _safe_path(Path(str(path) + suffix))
    if not exists and not create:
        return None
    if not write:
        _safe_path(parent, directory=True, private=True)
        _safe_path(path, private=True)
        for suffix in ("-journal", "-wal", "-shm"):
            _safe_path(Path(str(path) + suffix), private=True)
    if not exists:
        _tighten(parent, directory=True)
        try:
            fd = compat.open_fd(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            _safe_path(path)
        else:
            os.close(fd)
    if write:
        connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    else:
        connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0 if create else -1, SCHEMA_VERSION):
            _reject("unsupported receipt schema version")
        if write:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and create:
                connection.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                _reject("unsupported receipt schema version")
            _tighten(parent, directory=True)
            _tighten(path)
        return connection
    except BaseException:
        connection.close()
        raise


def _snapshot(state):
    attempts = [{key: item[key] for key in ("attempt_id", "actor_id", "revision", "state", "provenance")}
                for item in state["attempts"]]
    by_id = {item["attempt_id"]: item for item in attempts}
    attestations = []
    for item in state["attestations"]:
        attempt = by_id[item["attempt_id"]]
        if item["provenance"] == "unavailable" or item["verdict"] == "UNKNOWN":
            eligibility = "unknown"
        elif item["revision"] != state["revision"] or attempt["revision"] != state["revision"]:
            eligibility = "stale"
        elif item["verifier_id"] == attempt["actor_id"]:
            eligibility = "self-reported"
        elif (attempt["state"] != "returned" or item["artifact"] is None or
              item["artifact"] != state["artifact"] or
              not set(state["requirements"]) <= item["evidence"].keys()):
            eligibility = "incomplete"
        else:
            eligibility = "current"
        attestations.append({**item, "eligibility": eligibility})
    latest = attestations[-1] if attestations else None
    summary = {"verdict": latest["verdict"] if latest and latest["eligibility"] == "current" else "UNKNOWN",
               "eligibility": latest["eligibility"] if latest else "missing",
               "provenance": latest["provenance"] if latest else "unavailable"}
    return {"schema_version": SCHEMA_VERSION, "task_id": state["task_id"], "brief_id": state["brief_id"],
            "alignment_digest": state["alignment_digest"], "requirements": state["requirements"],
            "artifact": state["artifact"], "revision": state["revision"], "attempts": attempts,
            "attestations": attestations, "verification": dict(VERIFICATION), "attestation_summary": summary}


def _load(connection, task_id):
    row = connection.execute("SELECT state_json FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _apply(state, event, encoded):
    prior = state["events"].get(event["event_id"])
    if prior is not None:
        if prior != encoded:
            _reject("conflicting receipt event")
        return False
    kind = event["kind"]
    if kind == "revise":
        state["revision"] += 1
        for key in ("alignment_digest", "requirements", "artifact"):
            state[key] = event[key]
        state["known_requirements"].update({item: True for item in event["requirements"]})
    elif kind == "attempt":
        previous = next((item for item in state["attempts"] if item["attempt_id"] == event["attempt_id"]), None)
        if previous is None:
            if event["state"] != "running" or event["revision"] != state["revision"]:
                _reject("invalid first receipt attempt")
            state["attempts"].append({key: event[key] for key in ("attempt_id", "actor_id", "revision", "state")}
                                     | {"provenance": "coordinator-recorded"})
        else:
            if (previous["state"] != "running" or event["state"] == "running" or
                previous["actor_id"] != event["actor_id"] or previous["revision"] != event["revision"]):
                _reject("invalid receipt attempt transition")
            previous["state"] = event["state"]
    else:
        if not any(item["attempt_id"] == event["attempt_id"] for item in state["attempts"]):
            _reject("unknown receipt attempt")
        if not set(event["evidence"]) <= set(state["known_requirements"]):
            _reject("unknown receipt requirement")
        state["attestations"].append(event)
    state["events"][event["event_id"]] = encoded
    return True


def create_task(task_id, payload, *, state_dir=None) -> dict:
    """Create one opt-in receipt; an existing task always refuses."""
    _identifier(task_id)
    checked = _payload(payload)
    path = _location(state_dir)
    try:
        connection = _open(path, create=True, write=True)
        try:
            state = {"task_id": task_id, **checked, "revision": 1, "attempts": [], "attestations": [],
                     "events": {}, "known_requirements": {item: True for item in checked["requirements"]}}
            connection.execute("INSERT INTO tasks (task_id, state_json) VALUES (?, ?)",
                               (task_id, json.dumps(state, sort_keys=True, separators=(",", ":"))))
            connection.execute("COMMIT")
            return _snapshot(state)
        finally:
            connection.close()
    except ReceiptError:
        raise
    except sqlite3.IntegrityError:
        _reject("receipt task already exists")
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ReceiptError("receipt storage unavailable") from exc


def record_event(task_id, event, *, state_dir=None) -> dict:
    """Record a validated event atomically; duplicate IDs are idempotent."""
    _identifier(task_id)
    checked, encoded = _event(event)
    path = _location(state_dir)
    try:
        connection = _open(path, write=True)
        if connection is None:
            _reject("receipt task absent")
        try:
            state = _load(connection, task_id)
            if state is None:
                _reject("receipt task absent")
            if _apply(state, checked, encoded):
                connection.execute("UPDATE tasks SET state_json = ? WHERE task_id = ?",
                                   (json.dumps(state, sort_keys=True, separators=(",", ":")), task_id))
            connection.execute("COMMIT")
            return _snapshot(state)
        finally:
            connection.close()
    except ReceiptError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ReceiptError("receipt storage unavailable") from exc


def get_task(task_id, *, state_dir=None) -> dict | None:
    """Read without creating a state directory or database."""
    _identifier(task_id)
    path = _location(state_dir)
    try:
        connection = _open(path)
        if connection is None:
            return None
        try:
            state = _load(connection, task_id)
            return _snapshot(state) if state is not None else None
        finally:
            connection.close()
    except ReceiptError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise ReceiptError("receipt storage unavailable") from exc
