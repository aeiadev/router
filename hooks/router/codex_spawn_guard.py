#!/usr/bin/env python3
"""Adapt Codex spawn_agent events to the shared role and attempt policy.

The message is encrypted on this host. Never inspect, decrypt, or log it.
Model selection belongs to the installed role TOML, not updatedInput.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common


def main() -> int:
    if common.killed():
        return 0
    data = common.read_event()
    if data is None or data.get("tool_name") not in ("spawn_agent", "collaborationspawn_agent"):
        return 0
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        return 0
    if any(key in ti and not isinstance(ti[key], str) for key in ("agent_type", "model")):
        return 0
    routes = common.load_routes()
    modes = {key: common.effective_mode(routes["router"]["modes"].get(key))
             for key in common.mode_keys(routes)}
    classified = common.classify(ti.get("agent_type"), routes)
    brief = common.codex_task_hash(ti.get("task_name"), classified)
    result = common.record_spawn(brief, classified if brief else None,
                                 lambda prior: common.decide_codex_spawn(ti, routes, modes, prior))
    common.log("spawns", {key: result[key] for key in
                         ("decision", "rule", "run_type", "run_model", "class", "tier", "brief", "round", "notes", "shadow")})
    if result["decision"] == "block":
        sys.stderr.write(result["message"] + "\n")
        return 2
    if common.is_main(data):
        common.note_spawn(data.get("session_id"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        common.fail_open("codex_spawn_guard", exc)
