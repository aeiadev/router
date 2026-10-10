#!/usr/bin/env python3
"""Adapt Codex spawn_agent events to the shared role and attempt policy.

The message is encrypted on this host. Never inspect, decrypt, or log it.
Model selection belongs to the installed role TOML, not updatedInput.
"""
import sys
sys.dont_write_bytecode = True

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import common
    import ledger
except (OSError, ImportError, SyntaxError) as exc:
    print(f"codex_spawn_guard.py: cannot load the router hooks from {Path(__file__).resolve().parent}: {type(exc).__name__}",
          file=sys.stderr)
    sys.exit(1)


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
    brief = common.codex_brief(ti.get("task_name"), classified)

    def choose(prior):
        return common.decide_codex_spawn(ti, routes, modes, prior)

    if brief and classified and classified["class"] == "exec":
        project = common.project_key(data.get("cwd"))
        result = ledger.record_attempt_and_lane(common.codex_key(project, ti.get("task_name"), classified), choose,
                                                role=classified["base"], project=project,
                                                session=common.session_hash(data.get("session_id")), brief=brief,
                                                label=common.lane_label(ti.get("task_name"), routes),
                                                ttl_hours=common.ladder_ttl_hours(routes))
        if result is None:
            return 0
    else:
        result = choose([])
    common.log("spawns", {key: result[key] for key in
                         ("decision", "rule", "asked_type", "run_type", "run_model", "class", "tier", "brief", "round", "notes", "shadow")})
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
