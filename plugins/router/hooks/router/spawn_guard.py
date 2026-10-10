#!/usr/bin/env python3
"""Route Agent/Task calls. Decisions are pure; hook failures always allow the call.

Accepted exec attempts use a normalized TASK/FILES hash or a whole-prompt fallback.
One ledger transaction (ledger.py) keeps the ladder consistent across concurrent hook
processes. Rejected requests do not advance the round; only hashes and tiers are saved.
A broken ledger is logged with its path and the call is allowed unchanged.
"""
import sys
sys.dont_write_bytecode = True

import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import common
    import ledger
except (OSError, ImportError, SyntaxError) as exc:
    print(f"spawn_guard.py: cannot load the router hooks from {Path(__file__).resolve().parent}: {type(exc).__name__}",
          file=sys.stderr)
    sys.exit(1)

AGENT_TOOLS = ("Agent", "Task")
# Shared routing helpers exposed by this adapter.
decide = common.decide_spawn
load_prior = ledger.load_prior
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
    modes = {key: common.effective_mode(routes["router"]["modes"].get(key, "enforce" if key in ("brief", "rerun") else None))
             for key in mode_keys(routes)}
    brief = common.brief_hash(ti["prompt"])
    c = common.classify(ti.get("subagent_type"), routes)
    if modes["brief"] != "off":
        try:
            problem = common.brief_problem(ti["prompt"], ti.get("subagent_type"), routes)
        except Exception as exc:
            common.log("errors", {"script": "spawn_guard", "error": str(exc)})
            problem = None
        if problem:
            common.log("spawns", {"decision": "block" if modes["brief"] == "enforce" else "allow",
                                  "rule": "brief", "shadow": modes["brief"] == "shadow"})
            if modes["brief"] == "enforce":
                sys.stderr.write(problem + "\n")
                return 2
    if c and c["class"] == "exec" and modes["rerun"] != "off":
        project = common.project_key(data.get("cwd"))
        key = common.claude_key(project, brief)
        try:
            verdict = ledger.latest_verdict(key)
            if modes["rerun"] == "shadow":
                stamp = verdict.get("ts") if verdict and verdict.get("verdict") == "PASS" else None
                if (isinstance(stamp, (int, float)) and verdict.get("session") == common.session_hash(data.get("session_id"))
                        and stamp > time.time() - common.ladder_ttl_hours(routes) * 3600):
                    common.log("spawns", {"decision": "allow", "rule": "rerun", "shadow": True})
            else:
                problem = common.rerun_denied(key, data.get("session_id"), verdict,
                                              common.ladder_ttl_hours(routes))
                if problem:
                    common.log("spawns", {"decision": "block", "rule": "rerun", "shadow": False})
                    sys.stderr.write(problem + "\n")
                    return 2
        except Exception as exc:
            common.log("errors", {"script": "spawn_guard", "error": str(exc)})
    parent = common.session_model(data.get("transcript_path")) if common.is_fork(ti.get("subagent_type")) else None

    def choose(prior):
        return decide(ti, routes, modes, prior, session_model=parent,
                      agent_pin=lambda name: common.agent_pin(name, data.get("cwd")),
                      cwd=data["cwd"] if isinstance(data.get("cwd"), str) and data["cwd"] else None)

    if c and c["class"] == "exec":
        project = common.project_key(data.get("cwd"))
        res = ledger.record_attempt_and_lane(common.claude_key(project, brief), choose, role=c["base"],
                                             project=project, session=common.session_hash(data.get("session_id")),
                                             brief=brief, label=common.lane_label(ti.get("description"), routes),
                                             ttl_hours=common.ladder_ttl_hours(routes))
        if res is None:
            return 0
    else:
        res = choose([])
    common.log("spawns", {key: res[key] for key in
                         ("decision", "rule", "asked_type", "run_type", "run_model", "class", "tier", "brief", "round", "notes", "shadow")})
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
