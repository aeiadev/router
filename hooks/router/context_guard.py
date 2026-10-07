#!/usr/bin/env python3
"""Delegation guard: keeps the main session's context small.

One script, registered for SubagentStart, SubagentStop and PreToolUse. It
limits what subagents send back (return contract) and notes or flags how much
the main loop reads by itself. Separate from spawn_guard.py: own modes under
routes["context"]["modes"]. Any error allows (exit 0); only a deliberate
block exits 2. Logs carry sizes, counts and type names, never text or paths.
"""
import fcntl
import fnmatch
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

SEAT_CLASSES = ("exec", "judge", "sweep")
FILE_CLASSES = ("exec", "worker")
# Read-only seats: what to keep when trimming; they never write a file for the return limit.
READ_ONLY_KEEP = {"judge": "keep the VERDICT line and the numbered FINDINGS in their shortest form, "
                           "and drop everything else",
                  "sweep": "keep the conclusion and the counts, and drop the rest"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")
CHAIN_TOOLS = ("Bash", "Read", "Grep", "Glob")


def now_ms() -> int:
    value = os.environ.get("ROUTER_TEST_NOW_MS")
    if value:
        try:
            return int(value)
        except ValueError:
            pass
    return int(time.time() * 1000)


def safe_name(value) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(".") if isinstance(value, str) else ""


def ctx_section(routes) -> dict:
    return routes["context"]


def rule_mode(data, routes, key) -> str:
    return common.effective_mode(ctx_section(routes)["modes"].get(key, "shadow"), data, routes)


def base_record(data, event, mode) -> dict:
    session = data.get("session_id")
    return {"event": event, "session": session[:8] if isinstance(session, str) else "",
            "enforced": mode == "enforce"}


def reports_dir(routes) -> str:
    configured = ctx_section(routes).get("reports_dir")
    return str(Path(configured).expanduser() if configured else common.state_dir() / "reports")


def cap_for(c, routes) -> int:
    return ctx_section(routes)["caps"][c["class"]]


def mode_key(c) -> str:
    return "stop_cap_seats" if c["class"] in SEAT_CLASSES else "stop_cap_research"


def start_text(c, cap, routes) -> str:
    if c["class"] == "research":
        return (f"Return contract (router): this agent's report to the main session (its handback "
                f"message or final message) is limited to {cap} characters: conclusions first, then "
                f"path:line pointers, no file dumps.")
    if c["class"] in READ_ONLY_KEEP:
        return (f"Return contract (router): this agent's report to the main session (its handback "
                f"message or final message) is limited to {cap} characters. This seat stays read-only "
                f"and never writes a file for this. If the output is long, trim it to fit: "
                f"{READ_ONLY_KEEP[c['class']]}.")
    return (f"Return contract (router): this agent's report to the main session (its handback "
            f"message or final message) is limited to {cap} characters. Anything longer belongs in a "
            f"file under {reports_dir(routes)}/ when the task allows writing there; "
            f"the final message then gives that path and the conclusion.")


def over_text(c, chars, cap, routes) -> str:
    text = (f"Your final message is {chars} characters; the return contract for this agent is {cap}. "
            f"Send a final message of at most {cap} characters")
    if c["class"] in READ_ONLY_KEEP:
        return text + f". You stay read-only and write no file for this: {READ_ONLY_KEEP[c['class']]}."
    text += ": the conclusion first"
    if c["class"] in FILE_CLASSES:
        return (text + f", and put longer details in a file under {reports_dir(routes)}/ "
                "when the task allows writing there, then give its path. Otherwise trim the message to fit.")
    return text + ", then path:line pointers only."


def on_start(data, routes):
    agent_type = data.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        return
    c = common.classify(agent_type, routes)
    if c is None:
        return
    mode = rule_mode(data, routes, "start_note")
    if mode == "off":
        return
    cap = cap_for(c, routes)
    rec = base_record(data, "start_note", mode)
    rec.update({"type": agent_type, "class": c["class"], "cap": cap})
    common.log("context", rec)
    if mode == "enforce":
        common.emit({"hookSpecificOutput": {"hookEventName": "SubagentStart",
                                            "additionalContext": start_text(c, cap, routes)}})


def on_stop(data, routes):
    agent_type = data.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type:
        return
    message = data.get("last_assistant_message")
    if message is not None and not isinstance(message, str):
        return
    chars = len(message or "")
    c = common.classify(agent_type, routes)
    if c is None:
        rec = base_record(data, "stop_cap", "shadow")
        rec.update({"type": agent_type, "chars": chars, "action": "unlisted"})
        common.log("context", rec)
        return
    mode = rule_mode(data, routes, mode_key(c))
    if mode == "off":
        return
    cap = cap_for(c, routes)
    if chars <= cap:
        action = "ok"
    elif data.get("stop_hook_active"):
        action = "second_pass"
    else:
        action = "block" if mode == "enforce" else "shadow"
    rec = base_record(data, "stop_cap", mode)
    rec.update({"type": agent_type, "class": c["class"], "chars": chars, "cap": cap, "action": action})
    common.log("context", rec)
    if action == "block":
        common.emit({"decision": "block", "reason": over_text(c, chars, cap, routes)})


def handback_agent_type(data):
    agent_type = data.get("agent_type")
    if isinstance(agent_type, str) and agent_type:
        return agent_type
    transcript = data.get("transcript_path")
    session, agent = safe_name(data.get("session_id")), safe_name(data.get("agent_id"))
    if not (isinstance(transcript, str) and transcript and session and agent):
        return None
    meta = Path(transcript).expanduser().parent / session / "subagents" / f"agent-{agent}.meta.json"
    try:
        value = json.loads(meta.read_text(encoding="utf-8")).get("agentType")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def on_handback(data, routes):
    tool_input = data.get("tool_input")
    message = tool_input.get("message") if isinstance(tool_input, dict) else None
    chars = len(message) if isinstance(message, str) else 0
    agent_type = handback_agent_type(data)
    c = common.classify(agent_type, routes) if agent_type else None
    if c is None:
        rec = base_record(data, "handback_cap", "shadow")
        rec.update({"type": agent_type or "", "chars": chars, "action": "unlisted"})
        common.log("context", rec)
        return
    mode = rule_mode(data, routes, mode_key(c))
    if mode == "off":
        return
    cap = cap_for(c, routes)
    if chars <= cap:
        action = "ok"
    else:
        agent = safe_name(data.get("agent_id"))
        counter = common.state_dir() / "handback" / agent if agent else None
        if counter is None:
            action = "unlisted"  # no usable agent id: cannot count, so never block
        elif counter.exists():
            action = "second_pass"
        elif mode != "enforce":
            action = "shadow"
        else:
            counter.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.close(os.open(counter, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                action = "block"
            except FileExistsError:
                action = "second_pass"
    rec = base_record(data, "handback_cap", mode)
    rec.update({"type": agent_type, "class": c["class"], "chars": chars, "cap": cap, "action": action})
    common.log("context", rec)
    if action == "block":
        sys.stderr.write(over_text(c, chars, cap, routes) + "\n")
        sys.exit(2)


def large_read(data, routes, tool_input):
    """Return the log record when this Read is a large unbounded read, else None. The caller logs it once
    the outcome is known, so the logged action always equals what happened."""
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    if tool_input.get("limit") is not None or tool_input.get("pages") is not None:
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        return None
    try:
        size = os.stat(path).st_size if os.path.isfile(path) else None
    except OSError:
        return None
    if size is None or size <= ctx_section(routes).get("large_read_bytes", 24000):
        return None
    absolute = os.path.abspath(os.path.expanduser(path))
    if any(fnmatch.fnmatch(absolute, glob) for glob in ctx_section(routes).get("read_allow_globs", [])):
        return None
    mode = rule_mode(data, routes, "large_read")
    if mode == "off":
        return None
    rec = base_record(data, "large_read", mode)
    rec.update({"bytes": size, "ext": ext[:10], "action": "block" if mode == "enforce" else "shadow"})
    return rec


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_chain_state(state_path):
    """Return (state, count, last_ms, group_calls). A missing file is a fresh state; an unparsable file
    or wrong types are logged once as an error and reset to a fresh state, so counting carries on."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, 0, None, 0
    except (OSError, ValueError) as exc:
        state = exc
    if isinstance(state, dict):
        count, last_ms, group = state.get("count", 0), state.get("last_ms"), state.get("group_calls", 0)
        if is_int(count) and is_int(group) and (last_ms is None or is_int(last_ms)):
            return state, count, last_ms, group
    common.log("errors", {"script": "context_guard",
                          "error": "corrupt chain state reset: " + type(state).__name__})
    return {}, 0, None, 0


def update_chain(data, routes, tool_name, tool_input):
    """Advance the per-session chain counter under a file lock; return the new count if a note is due."""
    session_id = data.get("session_id")
    name = safe_name(session_id)
    if not name:
        return None
    chain_cfg = ctx_section(routes)["chain"]
    folder = common.state_dir() / "chain"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_path = folder / f"{name}.json"
    with open(folder / f"{name}.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state, count, last_ms, group = load_chain_state(state_path)
        now = now_ms()
        spawned = common.last_spawn(session_id)
        if spawned is not None and (last_ms is None or spawned * 1000 > last_ms):
            count = 0
        incremented = False
        if last_ms is not None and now - last_ms > chain_cfg["idle_reset_s"] * 1000:
            count, group = 0, 0
        if last_ms is not None and now - last_ms <= chain_cfg["same_turn_ms"]:
            group += 1
            count = 0
        else:
            group = 1
            if common.read_like(tool_name, tool_input):
                count += 1
                incremented = True
        tmp = folder / f"{name}.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps({"count": count, "last_ms": now, "group_calls": group}), encoding="utf-8")
        os.replace(tmp, state_path)
    first, repeat = chain_cfg["first"], chain_cfg["repeat"]
    if incremented and (count == first or (count >= repeat and count % repeat == 0)):
        return count
    return None


def on_pretool(data, routes):
    tool_name = data.get("tool_name")
    if tool_name == "SubagentHandback":
        return on_handback(data, routes)
    if tool_name not in CHAIN_TOOLS or not common.is_main(data):
        return
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    big = large_read(data, routes, tool_input) if tool_name == "Read" else None
    count = update_chain(data, routes, tool_name, tool_input)
    if big is not None:
        common.log("context", big)
    if big is not None and big["enforced"]:
        size_kb = max(1, os.stat(tool_input["file_path"]).st_size // 1000)
        sys.stderr.write(f"Blocked by the delegation guard: this file is {size_kb} KB and the Read has no "
                         f"limit. Search first (rg), then Read with offset and limit, or send a "
                         f"seat-sweep.\n")
        sys.exit(2)
    if count is None:
        return
    mode = rule_mode(data, routes, "chain_note")
    if mode == "off":
        return
    rec = base_record(data, "chain_note", mode)
    rec.update({"count": count, "action": "note" if mode == "enforce" else "shadow"})
    common.log("context", rec)
    if mode == "enforce":
        text = (f"Context note (delegation guard): {count} consecutive single read-type tool calls in the "
                f"main loop with no spawn in between. Delegate a multi-step investigation to "
                f"seat-sweep or an explore agent to keep detailed results out of the main context.")
        common.emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}})


HANDLERS = {"SubagentStart": on_start, "SubagentStop": on_stop, "PreToolUse": on_pretool}


def main():
    try:
        # The switch is checked before reading stdin or touching routes and state.
        if common.killed():
            return
        data = common.read_event()
        if data is None:
            return
        routes = common.load_routes()
        handler = HANDLERS.get(data.get("hook_event_name"))
        if handler is not None:
            handler(data, routes)
    except Exception as exc:
        common.fail_open("context_guard", exc)


if __name__ == "__main__":
    main()
    sys.exit(0)
