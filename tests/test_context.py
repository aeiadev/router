#!/usr/bin/env python3
"""Standalone tests for hooks/router/context_guard.py.

Every case runs the script as a subprocess with stdin JSON. State, kill switch
and routes table are temp paths; time is driven by ROUTER_TEST_NOW_MS.
Exit 0 only when all cases pass.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "hooks" / "router" / "context_guard.py"
ROUTES_SRC = ROOT / "hooks" / "router" / "routes.json"

SID = "test-context-session"
T0 = 1_800_000_000_000  # fake clock origin, ms
MAX_ELAPSED = [0.0]
TMPS = []


class Env:
    """A fresh temp world: state dir, kill-switch path, routes copy, transcripts."""

    def __init__(self, mod=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctxguard-"))
        TMPS.append(self.tmp)
        self.state = self.tmp / "state"
        self.off = self.tmp / "guard.off"
        self.routes_path = self.tmp / "routes.json"
        routes = json.loads(ROUTES_SRC.read_text())
        if mod:
            mod(routes)
        self.routes_path.write_text(json.dumps(routes))
        self.transcript = self.tmp / "transcripts" / f"{SID}.jsonl"
        self.transcript.parent.mkdir()
        self.transcript.write_text("")

    def env(self, now_ms=None):
        env = dict(os.environ)
        env["HOME"] = str(self.tmp / "home")
        env["XDG_STATE_HOME"] = str(self.tmp / "xdg-state")
        env["CLAUDE_HOME"] = str(self.tmp / "claude")
        env["ROUTER_HOME"] = str(self.tmp / "router-home")
        env["ROUTER_STATE"] = str(self.state)
        env["ROUTER_OFF_FILE"] = str(self.off)
        env.pop("ROUTER_OFF", None)
        env.pop("ROUTER_TEST_NOW_MS", None)
        env["ROUTES_JSON"] = str(self.routes_path)
        if now_ms is not None:
            env["ROUTER_TEST_NOW_MS"] = str(now_ms)
        return env

    def run(self, event, now_ms=None, raw=None):
        stdin = raw if raw is not None else json.dumps(event)
        started = time.monotonic()
        proc = subprocess.run([sys.executable, str(SCRIPT)], input=stdin, capture_output=True,
                              text=True, env=self.env(now_ms), timeout=20)
        MAX_ELAPSED[0] = max(MAX_ELAPSED[0], time.monotonic() - started)
        return proc

    def log(self, name="context"):
        path = self.state / f"{name}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def all_logs(self):
        return sorted(self.state.glob("*.jsonl")) if self.state.exists() else []

    def chain(self):
        path = self.state / "chain" / f"{SID}.json"
        return json.loads(path.read_text()) if path.exists() else None


def event(name, env, **fields):
    data = {"hook_event_name": name, "session_id": SID, "transcript_path": str(env.transcript)}
    data.update(fields)
    return data


def start_ev(env, agent_type, agent_id="ag1"):
    return event("SubagentStart", env, agent_type=agent_type, agent_id=agent_id)


def stop_ev(env, agent_type, text, agent_id="ag1", active=None):
    data = event("SubagentStop", env, agent_type=agent_type, agent_id=agent_id, last_assistant_message=text)
    if active is not None:
        data["stop_hook_active"] = active
    return data


def pre_ev(env, tool, tool_input, **extra):
    return event("PreToolUse", env, tool_name=tool, tool_input=tool_input, **extra)


def bash_ev(env, cmd="cat a"):
    return pre_ev(env, "Bash", {"command": cmd})


def handback_ev(env, text, agent_id="agA", agent_type="seat-exec-here-std"):
    extra = {"agent_id": agent_id}
    if agent_type is not None:
        extra["agent_type"] = agent_type
    return pre_ev(env, "SubagentHandback", {"message": text}, **extra)


def ctx(proc):
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def set_modes(**modes):
    def mod(routes):
        routes["context"]["modes"].update(modes)
    return mod


def drive(env, calls, step=2000, start=T0, cmd="cat a"):
    """Run `calls` read-type Bash calls `step` ms apart; return the list of procs."""
    return [env.run(bash_ev(env, cmd), now_ms=start + i * step) for i in range(1, calls + 1)]


# 1 to 8: start and stop events

def case_01_start_note():
    env = Env()
    proc = env.run(start_ev(env, "seat-exec-here-std"))
    check(proc.returncode == 0, "exit")
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    check(out["hookEventName"] == "SubagentStart", "event name")
    check("1500" in out["additionalContext"], "exec cap in text")
    check(str(env.state / "reports") in out["additionalContext"] and "~" not in out["additionalContext"],
          "reports dir expanded")
    proc = env.run(start_ev(env, "Explore"))
    text = ctx(proc)
    check("5000" in text and "reports" not in text, "research text has cap and no reports dir")
    for agent_type in ("totally-unknown", ""):
        proc = env.run(start_ev(env, agent_type))
        check(proc.returncode == 0 and proc.stdout == "", f"no output for {agent_type!r}")
    check(len(env.log()) == 2, "unknown and empty types are not logged")
    check(env.log()[0]["event"] == "start_note" and env.log()[0]["cap"] == 1500, "start_note log")


def case_02_start_note_off_and_shadow():
    env = Env(set_modes(start_note="off"))
    proc = env.run(start_ev(env, "seat-exec-here-std"))
    check(proc.returncode == 0 and proc.stdout == "", "off: no output")
    env = Env(set_modes(start_note="shadow", stop_cap_seats="shadow"))
    proc = env.run(start_ev(env, "seat-exec-here-std"))
    check(proc.returncode == 0 and proc.stdout == "", "shadow: no output")
    rec = env.log()[-1]
    check(rec["event"] == "start_note" and rec["enforced"] is False and rec["session"] == SID[:8], f"log {rec}")


def case_03_stop_under_cap():
    env = Env()
    proc = env.run(stop_ev(env, "seat-exec-here", "x" * 1500))
    check(proc.returncode == 0 and proc.stdout == "", "no output under cap")
    rec = env.log()[-1]
    check(rec["event"] == "stop_cap" and rec["action"] == "ok" and rec["chars"] == 1500, f"log {rec}")


def case_04_stop_over_cap_blocks():
    env = Env()
    proc = env.run(stop_ev(env, "seat-exec-here", "x" * 2200))
    check(proc.returncode == 0, "exit 0 with block JSON")
    out = json.loads(proc.stdout)
    check(out["decision"] == "block", "decision block")
    check("2200" in out["reason"] and "1500" in out["reason"], "reason has both numbers")
    check("reports" in out["reason"], "seat reason points at reports dir")
    rec = env.log()[-1]
    check(rec["action"] == "block" and rec["enforced"] is True and rec["class"] == "exec", f"log {rec}")


def case_05_stop_hook_active():
    env = Env()
    proc = env.run(stop_ev(env, "seat-exec-here", "x" * 2200, active=True))
    check(proc.returncode == 0 and proc.stdout == "", "no output when stop_hook_active")
    check(env.log()[-1]["action"] == "second_pass", "second_pass")


def case_06_research_shadow_then_enforce():
    env = Env()
    proc = env.run(stop_ev(env, "Explore", "x" * 6000))
    check(proc.returncode == 0 and proc.stdout == "", "shadow: no output")
    check(env.log()[-1]["action"] == "shadow", "shadow logged")
    env = Env(set_modes(stop_cap_research="enforce"))
    proc = env.run(stop_ev(env, "Explore", "x" * 6000))
    out = json.loads(proc.stdout)
    check(out["decision"] == "block" and "path:line" in out["reason"], "research enforce blocks")


def case_07_stop_shadow():
    env = Env(set_modes(start_note="shadow", stop_cap_seats="shadow"))
    proc = env.run(stop_ev(env, "seat-exec-here", "x" * 2200))
    check(proc.returncode == 0 and proc.stdout == "", "shadow: no output")
    rec = env.log()[-1]
    check(rec["enforced"] is False and rec["action"] == "shadow", f"log {rec}")


def case_08_class_caps():
    def mod(routes):
        routes["context"]["caps"].update({"judge": 1200, "worker": 4000})
    env = Env(mod)
    proc = env.run(stop_ev(env, "seat-judge-up", "x" * 1300))
    check(json.loads(proc.stdout)["decision"] == "block", "judge over its own cap blocks")
    check("1200" in json.loads(proc.stdout)["reason"], "judge cap in reason")
    proc = env.run(stop_ev(env, "seat-exec-here", "x" * 1300))
    check(proc.stdout == "", "exec cap 1500 not exceeded by 1300")
    env2 = Env(lambda r: (mod(r), set_modes(stop_cap_research="enforce")(r)))
    proc = env2.run(stop_ev(env2, "general-purpose", "x" * 4100))
    out = json.loads(proc.stdout)
    check("4000" in out["reason"], "worker cap in reason")
    check(env2.log()[-1]["class"] == "worker", "worker class")
    proc = env2.run(stop_ev(env2, "general-purpose", "x" * 3900))
    check(proc.stdout == "", "worker under cap")


# 9 to 16: chain

def case_09_subagent_pretool_ignored():
    env = Env()
    proc = env.run(pre_ev(env, "Bash", {"command": "cat a"}, agent_id="agX", agent_type="Explore"), now_ms=T0)
    check(proc.returncode == 0 and proc.stdout == "", "no output")
    check(not (env.state / "chain").exists(), "no chain dir")
    check(not env.all_logs(), "no log")


def case_10_chain_notes():
    env = Env()
    procs = drive(env, 20)
    for index, proc in enumerate(procs, 1):
        check(proc.returncode == 0, f"call {index} exit")
        if index in (5, 10, 20):
            text = ctx(proc)
            check(f"{index} consecutive" in text and "seat-sweep" in text, f"note at {index}")
        else:
            check(proc.stdout == "", f"no note at {index}")
    notes = [r for r in env.log() if r["event"] == "chain_note"]
    check([r["count"] for r in notes] == [5, 10, 20] and notes[0]["action"] == "note", f"log {notes}")


def case_11_spawn_resets():
    env = Env()
    drive(env, 3)
    last_ms = T0 + 3 * 2000
    spawn = env.state / "chain" / f"{SID}.spawn"
    spawn.touch()
    os.utime(spawn, (last_ms / 1000 + 0.5, last_ms / 1000 + 0.5))
    proc = env.run(bash_ev(env), now_ms=last_ms + 2000)
    check(proc.stdout == "" and env.chain()["count"] == 1, f"count after spawn {env.chain()}")
    procs = drive(env, 4, start=last_ms + 2000)
    check(procs[-1].stdout != "" and "5 consecutive" in ctx(procs[-1]), "note on the fifth call after the spawn")


def case_12_same_turn_not_chain():
    env = Env()
    drive(env, 3)
    last = T0 + 3 * 2000
    proc = env.run(bash_ev(env), now_ms=last + 2000)  # fourth, spaced
    proc = env.run(bash_ev(env), now_ms=last + 2200)  # fifth, 200 ms later: same turn
    check(proc.stdout == "", "no note for a multi-call turn")
    state = env.chain()
    check(state["count"] == 0 and state["group_calls"] == 2, f"state {state}")
    proc = env.run(bash_ev(env), now_ms=last + 4200)
    check(proc.stdout == "" and env.chain()["count"] == 1, "next spaced call is number 1")


def case_13_idle_reset():
    env = Env()
    drive(env, 4)
    proc = env.run(bash_ev(env), now_ms=T0 + 4 * 2000 + 200_000)
    check(proc.stdout == "" and env.chain()["count"] == 1, f"state {env.chain()}")


def case_14_git_is_neutral():
    env = Env()
    drive(env, 4)
    proc = env.run(bash_ev(env, "git status"), now_ms=T0 + 5 * 2000)
    check(proc.stdout == "" and env.chain()["count"] == 4, "git leaves the count alone")
    proc = env.run(bash_ev(env, "rg x"), now_ms=T0 + 6 * 2000)
    check("5 consecutive" in ctx(proc), "note on the read after git")


def case_15_read_grep_glob_count():
    env = Env()
    small = env.tmp / "small.txt"
    small.write_text("hi")
    seq = [("Read", {"file_path": str(small)}), ("Grep", {"pattern": "x"}), ("Glob", {"pattern": "*.py"}),
           ("Read", {"file_path": str(small)}), ("Grep", {"pattern": "y"})]
    procs = []
    for index, (tool, tool_input) in enumerate(seq, 1):
        procs.append(env.run(pre_ev(env, tool, tool_input), now_ms=T0 + index * 2000))
        check(env.chain()["count"] == index, f"{tool} counted as read-type (count {env.chain()['count']})")
    check("5 consecutive" in ctx(procs[-1]), "note on fifth")


def case_16_chain_shadow():
    env = Env(set_modes(chain_note="shadow"))
    procs = drive(env, 5)
    check(procs[-1].stdout == "", "no output in shadow")
    rec = [r for r in env.log() if r["event"] == "chain_note"]
    check(len(rec) == 1 and rec[0]["action"] == "shadow" and rec[0]["count"] == 5, f"log {rec}")


# 17: large read

def case_17_large_read():
    env = Env()
    big = env.tmp / "big.txt"
    big.write_bytes(b"a" * 30_000)
    proc = env.run(pre_ev(env, "Read", {"file_path": str(big)}), now_ms=T0)
    check(proc.returncode == 0 and proc.stdout == "", "shadow exits 0 quietly")
    rec = [r for r in env.log() if r["event"] == "large_read"]
    check(len(rec) == 1 and rec[0]["action"] == "shadow" and rec[0]["bytes"] == 30_000 and rec[0]["ext"] == ".txt",
          f"log {rec}")

    def custom_read_allowlist(routes):
        set_modes(large_read="enforce")(routes)
        routes["context"]["read_allow_globs"] = ["*/review-notes.txt"]

    env = Env(custom_read_allowlist)
    big = env.tmp / "big.txt"
    big.write_bytes(b"a" * 30_000)
    proc = env.run(pre_ev(env, "Read", {"file_path": str(big)}), now_ms=T0)
    check(proc.returncode == 2 and "offset and limit" in proc.stderr, f"enforce blocks ({proc.returncode})")
    check(env.chain()["count"] == 1, "chain state updated before the block")
    check([r["action"] for r in env.log() if r["event"] == "large_read"] == ["block"], "block logged")

    def lr():
        return len([r for r in env.log() if r["event"] == "large_read"])

    n = lr()
    proc = env.run(pre_ev(env, "Read", {"file_path": str(big), "limit": 100}), now_ms=T0 + 5000)
    check(proc.returncode == 0 and proc.stdout == "" and lr() == n, "limit: nothing")
    proc = env.run(pre_ev(env, "Read", {"file_path": str(big), "pages": "1-2"}), now_ms=T0 + 10_000)
    check(proc.returncode == 0 and lr() == n, "pages: nothing")
    allowed = env.tmp / "review-notes.txt"
    allowed.write_bytes(b"a" * 30_000)
    proc = env.run(pre_ev(env, "Read", {"file_path": str(allowed)}), now_ms=T0 + 15_000)
    check(proc.returncode == 0 and lr() == n, "allow glob: nothing")
    png = env.tmp / "pic.png"
    png.write_bytes(b"a" * 30_000)
    proc = env.run(pre_ev(env, "Read", {"file_path": str(png)}), now_ms=T0 + 20_000)
    check(proc.returncode == 0 and lr() == n, "png: nothing")
    proc = env.run(pre_ev(env, "Read", {"file_path": str(env.tmp / "missing.txt")}), now_ms=T0 + 25_000)
    check(proc.returncode == 0 and lr() == n, "missing file: nothing")


# 18 to 21: kill switch, errors, privacy, speed

def case_18_kill_switch():
    env = Env()
    env.off.write_text("")
    big = env.tmp / "big.txt"
    big.write_bytes(b"a" * 30_000)
    events = [start_ev(env, "seat-exec-here-std"), stop_ev(env, "seat-exec-here", "x" * 2200),
              handback_ev(env, "x" * 4000), bash_ev(env), pre_ev(env, "Read", {"file_path": str(big)})]
    for ev in events:
        proc = env.run(ev, now_ms=T0)
        check(proc.returncode == 0 and proc.stdout == "" and proc.stderr == "", f"killed: {ev['hook_event_name']}")
    check(not env.all_logs(), "no log while killed")
    check(not (env.state / "chain").exists(), "no chain state while killed")


def case_19_errors_allow():
    env = Env()
    for raw in ("not json at all", "", "[1,2]"):
        proc = env.run(None, raw=raw)
        check(proc.returncode == 0 and proc.stdout == "", f"malformed stdin {raw!r}")
    proc = env.run({"hook_event_name": "SomethingElse", "session_id": SID})
    check(proc.returncode == 0 and proc.stdout == "", "unknown event")
    proc = env.run(pre_ev(env, "Write", {"file_path": "x"}), now_ms=T0)
    check(proc.returncode == 0 and proc.stdout == "", "other tool")
    env.routes_path.write_text("{ broken")
    proc = env.run(start_ev(env, "seat-exec-here-std"))
    check(proc.returncode == 0 and proc.stdout == "", "malformed routes exits 0")
    errors = env.log("errors")
    check(len(errors) == 1 and errors[0]["script"] == "context_guard", f"errors log {errors}")


def case_20_privacy():
    marker = "ZXQMARKER9917"
    env = Env(set_modes(large_read="enforce"))
    big = env.tmp / f"{marker}.txt"
    big.write_bytes(b"a" * 30_000)
    env.run(stop_ev(env, "seat-exec-here", marker + "x" * 2200))
    env.run(stop_ev(env, "Explore", marker + "x" * 6000))
    for index in range(1, 6):  # five spaced calls so the chain note record is written
        env.run(pre_ev(env, "Bash", {"command": f"cat {marker}"}), now_ms=T0 + index * 2000)
    env.run(pre_ev(env, "Read", {"file_path": str(big)}), now_ms=T0 + 5000)
    env.run(handback_ev(env, marker + "x" * 4000), now_ms=T0)
    env.run(handback_ev(env, marker + "x" * 4000, agent_id="agB"), now_ms=T0)
    files = [p for p in env.state.rglob("*") if p.is_file()]
    check(files, "some state written")
    for path in files:
        check(marker not in path.read_text(errors="replace"), f"marker leaked into {path.name}")
        check(marker not in path.name, f"marker in file name {path.name}")


def case_21_speed():
    env = Env()
    started = time.monotonic()
    env.run(bash_ev(env), now_ms=T0)
    check(time.monotonic() - started < 5.0, "fresh invocation under 5 s")


# 22 to 24: SubagentHandback

def case_22_handback():
    env = Env()
    proc = env.run(handback_ev(env, "x" * 1000))
    check(proc.returncode == 0 and env.log()[-1]["action"] == "ok" and env.log()[-1]["chars"] == 1000, "ok")
    proc = env.run(handback_ev(env, "x" * 4000))
    check(proc.returncode == 2, f"first oversize blocks ({proc.returncode})")
    check("4000" in proc.stderr and "1500" in proc.stderr, "stderr has both numbers")
    rec = env.log()[-1]
    check(rec["event"] == "handback_cap" and rec["action"] == "block" and rec["class"] == "exec", f"log {rec}")
    proc = env.run(handback_ev(env, "x" * 4000))
    check(proc.returncode == 0 and env.log()[-1]["action"] == "second_pass", "second pass allowed")
    proc = env.run(handback_ev(env, "x" * 4000, agent_id="agB"))
    check(proc.returncode == 2 and env.log()[-1]["action"] == "block", "different agent blocked again")
    check(not (env.state / "chain").exists(), "handback does not touch the chain")


def case_23_handback_meta_lookup():
    env = Env()
    meta = env.transcript.parent / SID / "subagents" / "agent-agM.meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"agentType": "seat-exec-here-std"}))
    proc = env.run(handback_ev(env, "x" * 4000, agent_id="agM", agent_type=None))
    check(proc.returncode == 2 and "1500" in proc.stderr, f"type read from meta ({proc.returncode})")
    proc = env.run(handback_ev(env, "x" * 4000, agent_id="agNone", agent_type=None))
    check(proc.returncode == 0 and proc.stderr == "", "no meta: allowed")
    check(env.log()[-1]["action"] == "unlisted", f"log {env.log()[-1]}")


def case_24_handback_research_and_shadow():
    env = Env()
    proc = env.run(handback_ev(env, "x" * 6000, agent_type="Explore"))
    check(proc.returncode == 0 and env.log()[-1]["action"] == "shadow", "research shadow")
    check(not (env.state / "handback").exists() or not list((env.state / "handback").iterdir()),
          "shadow leaves no counter file")
    env = Env(set_modes(start_note="shadow", stop_cap_seats="shadow"))
    proc = env.run(handback_ev(env, "x" * 4000))
    rec = env.log()[-1]
    check(proc.returncode == 0 and rec["enforced"] is False and rec["action"] == "shadow", f"shadow {rec}")


# 25 to 29: errors allow, concurrency, handback edge cases

def case_25_corrupt_chain_state_heals():
    bad_states = ['{"count":"abc","last_ms":1}', "not json at all", "[1, 2]", '{"count":1,"last_ms":"x"}',
                  '{"count":1,"last_ms":1,"group_calls":null}', '{"count":true,"last_ms":1}']
    for bad in bad_states:
        env = Env()
        (env.state / "chain").mkdir(parents=True)
        (env.state / "chain" / f"{SID}.json").write_text(bad)
        procs = drive(env, 5)
        check(all(p.returncode == 0 for p in procs), f"{bad}: exit codes")
        errors = env.log("errors")
        check(len(errors) == 1 and errors[0]["script"] == "context_guard", f"{bad}: errors {errors}")
        check(all(p.stdout == "" for p in procs[:4]), f"{bad}: no early note")
        check("5" in ctx(procs[4]), f"{bad}: counting resumed, fifth call notes ({procs[4].stdout!r})")
        state = env.chain()
        check(state == {"count": 5, "last_ms": T0 + 10000, "group_calls": 1}, f"{bad}: healed state {state}")
        check(any(r["event"] == "chain_note" and r["count"] == 5 for r in env.log()), f"{bad}: note logged")


def case_26_odd_event_name_allows():
    env = Env()
    for name in (["PreToolUse"], {"a": 1}, 7, None):
        proc = env.run({"hook_event_name": name, "session_id": SID})
        check(proc.returncode == 0 and proc.stdout == "", f"event name {name!r}: exit {proc.returncode}")


def case_27_handler_exception_exits_zero():
    env = Env()
    env.state.mkdir(parents=True)
    (env.state / "handback").write_text("not a directory")  # makes the counter creation raise
    proc = env.run(handback_ev(env, "x" * 4000))
    check(proc.returncode == 0 and proc.stdout == "", f"exit {proc.returncode} (want 0, not 1 or 2)")
    check(len(env.log("errors")) == 1, "one errors line")
    check(all(r.get("action") != "block" for r in env.log()), "no block logged when the counter failed")


def case_28_concurrent_chain_updates():
    env = Env()
    procs = [subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=env.env(T0)) for _ in range(20)]
    for proc in procs:
        proc.stdin.write(json.dumps(bash_ev(env)))
        proc.stdin.close()
    codes = [proc.wait(timeout=30) for proc in procs]
    check(codes == [0] * 20, f"exit codes {codes}")
    state = env.chain()
    check(state["group_calls"] == 20, f"lost updates: group_calls {state['group_calls']}")
    check(not list((env.state / "chain").glob("*.tmp")), "no stray tmp files")


def case_29_handback_edge_cases():
    env = Env()
    for _ in range(3):  # no usable agent id: cannot count, so never block
        proc = env.run(pre_ev(env, "SubagentHandback", {"message": "x" * 4000}, agent_type="seat-exec-here-std"))
        check(proc.returncode == 0, "no agent_id: allowed")
    check(env.log()[-1]["action"] == "unlisted", f"log {env.log()[-1]}")
    env = Env(set_modes(start_note="shadow", stop_cap_seats="shadow"))  # counter present wins over mode: second_pass in any mode
    (env.state / "handback").mkdir(parents=True)
    (env.state / "handback" / "agA").write_text("")
    proc = env.run(handback_ev(env, "x" * 4000))
    check(proc.returncode == 0 and env.log()[-1]["action"] == "second_pass", f"log {env.log()[-1]}")


def case_30_read_log_matches_outcome():
    mod = set_modes(large_read="enforce")
    env = Env(mod)
    big = env.tmp / "big.txt"
    big.write_text("x" * 30000)
    (env.state / "chain").mkdir(parents=True)
    (env.state / "chain" / f"{SID}.json").write_text("not json")  # heals, so the block still happens
    proc = env.run(pre_ev(env, "Read", {"file_path": str(big)}), now_ms=T0)
    check(proc.returncode == 2 and "offset and limit" in proc.stderr, f"blocked after healing ({proc.returncode})")
    check([r["action"] for r in env.log() if r["event"] == "large_read"] == ["block"], "block logged once")
    env = Env(mod)  # the chain folder cannot be made: the hook fails open and must not claim a block
    big = env.tmp / "big.txt"
    big.write_text("x" * 30000)
    env.state.mkdir(parents=True)
    (env.state / "chain").write_text("not a directory")
    proc = env.run(pre_ev(env, "Read", {"file_path": str(big)}), now_ms=T0)
    check(proc.returncode == 0, f"allowed on internal error ({proc.returncode})")
    check(len(env.log("errors")) == 1, "one errors line")
    check(all(r.get("action") != "block" for r in env.log()), f"a block was logged for an allowed Read: {env.log()}")


def case_31_missing_mode_key_is_shadow():
    def mod(routes):
        routes["context"]["modes"].pop("chain_note", None)
    env = Env(mod)
    procs = drive(env, 5)
    check(procs[-1].stdout == "", "missing key prints no note")
    rec = [r for r in env.log() if r["event"] == "chain_note"]
    check(len(rec) == 1 and rec[0]["action"] == "shadow" and rec[0]["count"] == 5, f"log {rec}")


def case_32_kill_switch_before_input_and_state():
    for switch in ("file", "environment"):
        env = Env()
        overrides = env.env()
        if switch == "file":
            env.off.touch()
        else:
            overrides["ROUTER_OFF"] = "1"
        overrides["ROUTES_JSON"] = str(env.tmp / "missing-routes.json")
        proc = subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=overrides)
        try:
            # Leave stdin open and empty. A disabled hook must exit without reading it.
            code = proc.wait(timeout=3)
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise AssertionError(f"{switch} kill switch read stdin instead of returning")
        check(code == 0 and stdout == stderr == "", f"{switch} switch is a silent no-op")
        check(not env.state.exists(), f"{switch} switch touched state")


def case_33_read_only_return_contracts():
    env = Env()
    for agent_type, cap in (("seat-sweep", 3000), ("seat-judge", 1500)):
        proc = env.run(start_ev(env, agent_type))
        text = ctx(proc)
        check("read-only" in text and "never writes a file" in text, f"{agent_type} read-only note")
        check(str(cap) in text, f"{agent_type} cap")
        proc = env.run(stop_ev(env, agent_type, "x" * (cap + 1)))
        reason = json.loads(proc.stdout)["reason"]
        check("write no file" in reason, f"{agent_type} stays read-only after overflow")


def case_34_no_transcript_required():
    env = Env()
    env.transcript.unlink()
    proc = env.run(start_ev(env, "seat-exec"))
    check("1500" in ctx(proc), "configured enforcement requires no transcript")
    proc = env.run(stop_ev(env, "seat-exec", "x" * 1501))
    check(json.loads(proc.stdout)["decision"] == "block", "cap enforced with no transcript")


def case_35_reports_override_and_bad_message():
    custom = "~/custom-reports"
    env = Env(lambda r: r["context"].update({"reports_dir": custom}))
    proc = env.run(start_ev(env, "seat-exec"))
    check(str(env.tmp / "home" / "custom-reports") in ctx(proc), "reports override expands HOME")
    proc = env.run(stop_ev(env, "seat-exec", ["not a string"] * 2000))
    check(proc.returncode == 0 and proc.stdout == "", "wrong message type fails open")


def case_36_clock_override():
    env = Env()
    overrides = env.env()
    overrides["ROUTER_TEST_NOW_MS"] = str(T0)
    proc = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(bash_ev(env)),
                          capture_output=True, text=True, env=overrides, timeout=20)
    check(proc.returncode == 0, "public clock override permits call")
    check(env.chain()["last_ms"] == T0, "public clock override controls chain timestamp")


def case_37_malformed_agent_types_allow():
    env = Env()
    for agent_type in (["seat-exec"], {"type": "seat-exec"}, 7, True):
        for ev in (start_ev(env, agent_type), stop_ev(env, agent_type, "x" * 6000)):
            proc = env.run(ev)
            check(proc.returncode == 0 and proc.stdout == "", f"invalid agent type {agent_type!r}")


def case_38_reports_do_not_expand_write_scope():
    env = Env(set_modes(stop_cap_research="enforce"))
    for agent_type, cap in (("seat-exec", 1500), ("general-purpose", 5000)):
        proc = env.run(start_ev(env, agent_type))
        check("when the task allows writing there" in ctx(proc), f"{agent_type}: start respects write scope")
        proc = env.run(stop_ev(env, agent_type, "x" * (cap + 1)))
        reason = json.loads(proc.stdout)["reason"]
        check("when the task allows writing there" in reason, f"{agent_type}: overflow respects write scope")
        check("Otherwise trim the message to fit" in reason, f"{agent_type}: overflow permits no-file recovery")


def main():
    cases = sorted((name, fn) for name, fn in globals().items() if name.startswith("case_") and callable(fn))
    failed = 0
    for name, fn in cases:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failed += 1
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            print(f"FAIL {name}: {detail}")
    for tmp in TMPS:
        shutil.rmtree(tmp, ignore_errors=True)
    if MAX_ELAPSED[0] >= 5.0:  # timed over every case above, handback cases included
        failed += 1
        print(f"FAIL speed: slowest invocation {MAX_ELAPSED[0]:.2f}s")
    else:
        print(f"PASS speed: slowest of all invocations {MAX_ELAPSED[0]:.2f}s")
    print(f"{len(cases)} cases checked; {failed} failures")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
