#!/usr/bin/env python3
"""Route Agent/Task calls. Decisions are pure; hook failures always allow the call.

Accepted exec attempts use a normalized TASK/FILES hash or a whole-prompt fallback.
SQLite transactions keep the ladder consistent across concurrent hook processes.
Rejected requests do not advance the round; only brief hashes and tiers are saved.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common

AGENT_TOOLS = ("Agent", "Task")
# Shared routing helpers exposed by this adapter.
decide = common.decide_spawn
load_prior = common.load_prior
mode_keys = common.mode_keys


def main() -> int:
    if common.killed():
        return 0
    data = common.read_event()
    if data is None or data.get("tool_name") not in AGENT_TOOLS:
        return 0
    ti = data.get("tool_input")
    if not isinstance(ti, dict) or not isinstance(ti.get("prompt"), str):
        return 0
    if any(key in ti and not isinstance(ti[key], str) for key in ("subagent_type", "model")):
        return 0
    routes = common.load_routes()
    modes = {key: common.effective_mode(routes["router"]["modes"].get(key)) for key in mode_keys(routes)}
    brief = common.brief_hash(ti["prompt"])
    c = common.classify(ti.get("subagent_type"), routes)
    parent = common.session_model(data.get("transcript_path")) if common.is_fork(ti.get("subagent_type")) else None

    def choose(prior):
        return decide(ti, routes, modes, prior, session_model=parent,
                      agent_pin=lambda name: common.agent_pin(name, data.get("cwd")))

    res = common.record_spawn(brief, c, choose)
    common.log("spawns", {key: res[key] for key in
                         ("decision", "rule", "run_type", "run_model", "class", "tier", "brief", "round", "notes", "shadow")})
    if res["decision"] == "block":
        sys.stderr.write(res["message"] + "\n")
        return 2
    if common.is_main(data):
        common.note_spawn(data.get("session_id"))
    if res["decision"] == "rewrite":
        common.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                            "updatedInput": res["updated"]}})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        common.fail_open("spawn_guard", exc)
