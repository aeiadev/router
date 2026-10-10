#!/usr/bin/env python3
"""Judge reminder: the main session's Stop hook (Claude only).

When builder lanes of this session have returned and no judge has looked at them (a judge return,
UNKNOWN verdict included, marks a lane judged), block the stop once:
{"decision":"block","reason":"returned without a judge: <labels>; spawn judge with the same TASK and FILES"}.
The same set of lanes never blocks twice; a changed set blocks again. Silent under the kill switch,
when stop_hook_active is set, under `router auto off`, in mode off, and when the ledger is missing
or unreadable (the ledger logs its path). Shadow mode only logs.

Mode: routes["router"]["modes"]["judge_reminder"], enforce when the key is absent.
Codex has no registration: its lanes stay running (no SubagentStop there), so nothing returns.
State: <state>/judge-reminder/<session>.json, 0600, holding a hash of the lane set only.
"""
import sys
sys.dont_write_bytecode = True

import hashlib
import json
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import common  # noqa: E402
except (OSError, ImportError, SyntaxError) as exc:
    print(f"judge_reminder.py: cannot load the router hooks from {Path(__file__).resolve().parent}: {type(exc).__name__}",
          file=sys.stderr)
    sys.exit(1)

DEFAULT_MODE = "enforce"
REASON = "returned without a judge: {labels}; spawn judge with the same TASK and FILES"
MAX_LABELS = 5
MAX_REASON_BYTES = 600


def reason_text(lanes) -> str:
    """The block reason: at most 5 labels (newest first), then ", +N more"; at most 600 bytes UTF-8."""
    names = [lane_name(lane) for lane in lanes]
    shown = names[:MAX_LABELS]
    while True:
        parts = shown + ([f"+{len(names) - len(shown)} more"] if len(shown) < len(names) else [])
        text = REASON.format(labels=", ".join(parts))
        if len(text.encode("utf-8")) <= MAX_REASON_BYTES or not shown:
            return text
        shown = shown[:-1]


def lane_name(lane) -> str:
    """The lane's label, else role-tier (for example builder-std)."""
    if lane.get("label"):
        return lane["label"]
    return "-".join(part for part in (lane.get("role"), lane.get("tier")) if part) or "lane"


def set_hash(lanes) -> str:
    pairs = sorted([lane["brief"] or "", lane["started"]] for lane in lanes)
    return hashlib.sha256(json.dumps(pairs).encode("utf-8")).hexdigest()


def seen(path, digest) -> bool:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("set") == digest
    except (OSError, ValueError, AttributeError):
        return False


def remember(path, digest) -> None:
    """Write {"set": digest} atomically as 0600."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"set": digest}))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    if common.killed():
        return
    data = common.read_event()
    if data is None or data.get("hook_event_name") != "Stop" or data.get("stop_hook_active"):
        return
    if common.auto_mode() == "off":
        return
    routes = common.load_routes()
    mode = common.effective_mode(routes["router"]["modes"].get("judge_reminder", DEFAULT_MODE))
    session = common.session_hash(data.get("session_id"))
    if mode == "off" or session is None:
        return
    import ledger
    lanes = [lane for lane in ledger.lanes_for_session(session) if lane["status"] == "returned"]
    if not lanes:
        return
    digest = set_hash(lanes)
    state = common._state_file("judge-reminder", data.get("session_id"), ".json")
    if state is None or seen(state, digest):
        return
    remember(state, digest)
    action = "block" if mode == "enforce" else "shadow"
    common.log("context", {"event": "judge_reminder", "lanes": len(lanes), "action": action})
    if action == "block":
        common.emit({"decision": "block", "reason": reason_text(lanes)})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        common.fail_open("judge_reminder", exc)
    sys.exit(0)
