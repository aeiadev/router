#!/usr/bin/env python3
"""Standalone prompt hint probes."""
import json
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hooks/router/prompt_hint.py"


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        routes = json.loads((ROOT / "hooks/router/routes.json").read_text())
        path = root / "routes.json"
        path.write_text(json.dumps(routes))
        (root / "state").mkdir()
        (root / "state/auto").write_text("suggest")
        env = dict(os.environ, HOME=tmp, XDG_STATE_HOME=str(root / "xdg"),
                   XDG_CONFIG_HOME=str(root / "config"), ROUTER_LOCAL="off",
                   ROUTER_STATE=str(root / "state"), ROUTES_JSON=str(path), ROUTER_AUTO_MODE="suggest")
        env.pop("ROUTER_OFF", None)
        def run(prompt, session="hint-test", codex=False, harness_host=None):
            event = {"hook_event_name": "UserPromptSubmit", "session_id": session, "prompt": prompt}
            if codex:
                event["turn_id"] = "turn"
            if harness_host:
                event["harness_host"] = harness_host
            return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(event), text=True,
                                  capture_output=True, env=env, timeout=10)
        def line(result):
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] if result.stdout else ""
        result = run("Please compare options and evaluate the design PRIVATE_SENTINEL")
        assert result.returncode == 0, result.stderr
        value = line(result)
        assert "researcher" in value and "matched: compare, evaluate" in value, value
        assert "PRIVATE_SENTINEL" not in value
        assert "researcher" in line(run("Please compare and design the requested options", session="tie"))
        assert re.fullmatch(r"Router hint: consider researcher \(matched: compare, evaluate\); Agent subagent_type=researcher, send TASK/RETURN\.", value), value
        assert line(run("Please add the requested feature carefully", session="generic")) == ""
        assert "builder" in line(run("Add the requested feature carefully", session="first"))
        assert "docs-writer" in line(run("Please update the docs for this release", session="docs"))
        assert "test-writer" in line(run("Tests for this release need updating", session="tests"))
        assert "builder" in line(run("Please begin refactoring this module", session="refactoring"))
        assert line(run("Please contest the address in this sentence", session="substring")) == ""
        assert re.fullmatch(r"Router hint: consider builder \(matched: implement\); spawn_agent agent_type=builder, send TASK/FILES/BAR/RETURN\.",
                            line(run("Implement the requested feature here", session="codex", codex=True)))
        routes["context"]["prompt_roles"] = {"sweeper": ["count"]}
        path.write_text(json.dumps(routes))
        assert "sweeper" in line(run("Count the relevant files in this folder", session="list"))
        routes = json.loads((ROOT / "hooks/router/routes.json").read_text())
        routes["context"]["hint_every"] = 3
        path.write_text(json.dumps(routes))
        outputs = [line(run("Implement the requested feature here", session="cadence")) for _ in range(4)]
        assert [bool(item) for item in outputs] == [True, False, False, True], outputs
        assert "builder" in line(run("Implement the requested feature here", session="separate"))
        routes["context"]["hint_every"] = 0
        path.write_text(json.dumps(routes))
        marker = root / "state/chain/quiet.spawn"
        marker.parent.mkdir(exist_ok=True)
        marker.touch()
        os.utime(marker, (time.time() - 100, time.time() - 100))
        assert line(run("Implement the requested feature here", session="quiet")) == ""
        os.utime(marker, (time.time() - 700, time.time() - 700))
        assert "builder" in line(run("Implement the requested feature here", session="quiet"))
        routes["context"]["hint_every"] = 3
        path.write_text(json.dumps(routes))
        corrupt = root / "state/hints" / (hashlib.sha256(b"corrupt").hexdigest()[:16] + ".json")
        corrupt.write_text("{")
        pressure_dir = root / "xdg/claude-harness/pressure"
        pressure_dir.mkdir(parents=True)
        def pressure_for(session):
            record = pressure_dir / (hashlib.sha256(session.encode()).hexdigest()[:16] + ".json")
            record.write_text(json.dumps({"schema": 1, "host": "claude", "ts": time.time(), "used": 910,
                                          "window": 1000, "window_source": "statusline",
                                          "percent": 91, "level": "urgent"}))
        pressure_for("corrupt")
        prior = len((root / "state/errors.jsonl").read_text().splitlines()) if (root / "state/errors.jsonl").exists() else 0
        for _ in range(3):
            assert line(run("Implement the requested feature here", session="corrupt")) == ""
        assert len((root / "state/errors.jsonl").read_text().splitlines()) - prior == 1
        assert json.loads((root / "state/errors.jsonl").read_text().splitlines()[-1])["file"] == str(corrupt)
        for name, kind in (("oversized", "oversized"), ("symlink", "symlink"),
                           ("broken-symlink", "broken-symlink"), ("directory", "directory")):
            pressure_for(name)
            target = root / "state/hints" / (hashlib.sha256(name.encode()).hexdigest()[:16] + ".json")
            if kind == "oversized":
                target.write_bytes(b"x" * 5000)
            elif kind == "symlink":
                target.symlink_to(corrupt)
            elif kind == "broken-symlink":
                target.symlink_to(root / "missing-counter.json")
            else:
                target.mkdir()
            for _ in range(3):
                assert line(run("Implement the requested feature here", session=name)) == ""
        stable = root / "state/hints" / (hashlib.sha256(b"stable").hexdigest()[:16] + ".json")
        stable.write_text('{"count":1,"level":null}')
        (root / "state/auto").write_text("nudge")
        before = (stable.stat().st_ino, stable.stat().st_mtime_ns)
        for _ in range(2):
            assert line(run("Implement the requested feature here", session="stable")) == ""
        assert (stable.stat().st_ino, stable.stat().st_mtime_ns) == before
        (root / "state/auto").write_text("suggest")
        pressure_path = root / "xdg/claude-harness/pressure" / (hashlib.sha256(b"pressure").hexdigest()[:16] + ".json")
        pressure_path.parent.mkdir(parents=True, exist_ok=True)
        pressure_path.write_text(json.dumps({"schema": 1, "host": "claude", "ts": time.time(), "used": 700,
                                             "window": 1000, "window_source": "statusline",
                                             "percent": 70, "level": "remind"}))
        assert "Router context pressure: remind (70%)" in run("Please handle this unrelated request", session="pressure").stdout
        assert run("Please handle this unrelated request", session="pressure").stdout == ""
        pressure_record = json.loads(pressure_path.read_text())
        pressure_record["level"] = "urgent"
        pressure_record["percent"] = 91
        pressure_path.write_text(json.dumps(pressure_record))
        assert "Router context pressure: urgent (91%)" in run("Please handle this unrelated request", session="pressure").stdout
        paired = root / "xdg/claude-harness/pressure" / (hashlib.sha256(b"pressure-hint").hexdigest()[:16] + ".json")
        paired.write_text(json.dumps(pressure_record))
        combined = line(run("Implement the requested feature here", session="pressure-hint"))
        assert combined.splitlines()[0].startswith("Router context pressure: urgent (91%)")
        assert combined.splitlines()[1].startswith("Router hint: consider builder")
        for host, legacy_turn, syntax in (("codex", False, "spawn_agent agent_type=builder"),
                                           ("claude", True, "Agent subagent_type=builder")):
            session = "host-" + host
            record = root / "xdg/claude-harness/pressure" / (hashlib.sha256(session.encode()).hexdigest()[:16] + ".json")
            record.write_text(json.dumps({**pressure_record, "host": host}))
            value = line(run("Implement the requested feature here", session=session, codex=legacy_turn, harness_host=host))
            assert value == ("Router context pressure: urgent (91%); delegate reads and sweeps to sweeper or researcher.\n"
                             "Router hint: consider builder (matched: implement); " + syntax
                             + ", send TASK/FILES/BAR/RETURN."), value
        routes["context"]["hint_every"] = 0
        path.write_text(json.dumps(routes))
        zero = root / "state/hints" / (hashlib.sha256(b"zero").hexdigest()[:16] + ".json")
        zero.write_text('{"count":5,"level":null}')
        before = (zero.stat().st_ino, zero.stat().st_mtime_ns)
        for _ in range(3):
            assert line(run("Implement the requested feature here", session="zero")) == (
                "Router hint: consider builder (matched: implement); Agent subagent_type=builder, send TASK/FILES/BAR/RETURN.")
        assert json.loads(zero.read_text()) == {"count": 5, "level": None}
        assert (zero.stat().st_ino, zero.stat().st_mtime_ns) == before
        routes["context"]["hint_every"] = 3
        path.write_text(json.dumps(routes))
        (root / "state/auto").write_text("off")
        assert run("Implement the requested feature here", session="pressure-hint").stdout == ""
        (root / "state/auto").write_text("suggest")
        logs = (root / "state/hints.jsonl").read_text()
        assert "PRIVATE_SENTINEL" not in logs
        print("PASS scored hints, privacy, cadence and spawn quiet")


if __name__ == "__main__":
    main()
