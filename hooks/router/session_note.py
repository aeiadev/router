#!/usr/bin/env python3
"""Restore after compaction: this session's Router lanes as SessionStart context (both hosts).

Silent unless source is "compact", when the kill switch is on, and when the session has no lanes.
Otherwise one additionalContext of at most 800 UTF-8 bytes: a line with the auto mode, up to 6 lanes
newest first ("role-tier label rN/3 status verdict", missing parts left out) and one action line.
The oldest lanes are dropped first, then labels are shortened, until the text fits. It never
carries STATE.md text or git facts. Any error prints nothing and exits 0.

Codex lanes always show as running: no SubagentStop is registered there, so none return.
"""
import sys
sys.dont_write_bytecode = True

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import common  # noqa: E402
except (OSError, ImportError, SyntaxError) as exc:
    print(f"session_note.py: cannot load the router hooks from {Path(__file__).resolve().parent}: {type(exc).__name__}",
          file=sys.stderr)
    sys.exit(1)

MAX_BYTES = 800
MAX_LANES = 6
ROUNDS = 3  # two standard rounds, then one upper-tier round
LABEL_STEPS = (None, 40, 20, 10, 0)  # label lengths tried once only one lane is left
ACTIONS = (("returned", "Spawn judge with the same TASK and FILES before closing."),
           ("running", "Running lanes finish on their own; see `router lanes`."))
ALL_JUDGED = "All lanes judged."


def lane_line(lane, label_chars=None) -> str:
    label = lane.get("label") or ""
    if label_chars is not None:
        label = label[:label_chars].rstrip()
    parts = ("-".join(part for part in (lane.get("role"), lane.get("tier")) if part), label,
             f"r{lane['round']}/{ROUNDS}" if lane.get("round") else "", lane.get("status") or "",
             lane.get("verdict") or "")
    return " ".join(part for part in parts if part)


def action_line(lanes) -> str:
    statuses = {lane.get("status") for lane in lanes}
    return next((text for status, text in ACTIONS if status in statuses), ALL_JUDGED)


def size(text) -> int:
    return len(text.encode("utf-8", "replace"))


def render(header, lanes) -> str:
    """The note: header, the newest lanes that fit, and the action line for every lane."""
    shown = list(lanes[:MAX_LANES])
    last = action_line(lanes)

    def text(label_chars=None):
        return "\n".join((header, *(lane_line(lane, label_chars) for lane in shown), last))

    while len(shown) > 1 and size(text()) > MAX_BYTES:
        shown.pop()  # newest first, so the last one is the oldest
    for label_chars in LABEL_STEPS:
        candidate = text(label_chars)
        if size(candidate) <= MAX_BYTES:
            return candidate
    return candidate.encode("utf-8", "replace")[:MAX_BYTES].decode("utf-8", "ignore")


def main() -> None:
    if common.killed():
        return
    data = common.read_event()
    if data is None or data.get("source") != "compact":
        return
    if data.get("hook_event_name", "SessionStart") != "SessionStart":
        return
    session = common.session_hash(data.get("session_id"))
    if session is None:
        return
    import ledger
    lanes = ledger.lanes_report(session=session)
    if not lanes:
        return
    header = f"Router lanes after compaction (auto mode: {common.auto_mode()}), newest first:"
    common.emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": render(header, lanes)}})


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        common.fail_open("session_note", exc)
    sys.exit(0)
