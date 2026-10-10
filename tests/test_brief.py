#!/usr/bin/env python3
"""C1 and C3 spawn contract regressions."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common
import ledger
import spawn_guard
from test_spawn import TIMEOUT

ROUTES = common.load_routes()
FULL = "TASK build helper\nFILES src/helper.py\nBAR run tests\nRETURN five lines"
READ = "TASK find helper\nRETURN paths"
# The whole C3 enforce message, time as YYYY-MM-DD HH:MM UTC, and nothing else on stderr.
RERUN_DENY = re.compile(r"already passed at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) UTC; change TASK or FILES to rerun\n")


class BriefTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="router-brief-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.routes = self.root / "routes.json"
        self.routes.write_text(json.dumps(ROUTES), encoding="utf-8")
        self.env = dict(os.environ, ROUTER_STATE=str(self.state), ROUTES_JSON=str(self.routes),
                        ROUTER_OFF_FILE=str(self.root / "OFF"), PYTHONDONTWRITEBYTECODE="1",
                        HOME=str(self.root / "home"), XDG_CONFIG_HOME=str(self.root / "config"),
                        ROUTER_LOCAL="off")
        self.env.pop("ROUTER_OFF", None)

    def hook(self, prompt, role="builder", session="one", guard="spawn_guard.py", extra=None):
        ti = {"subagent_type": role, "prompt": prompt}
        if guard == "codex_spawn_guard.py":
            ti = {"agent_type": role, "task_name": "task", "message": prompt}
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent" if guard == "spawn_guard.py" else "spawn_agent",
                 "tool_input": ti, "session_id": session, "cwd": str(self.root)}
        return subprocess.run([sys.executable, "-B", str(ROOT / "hooks/router" / guard)],
                              input=json.dumps(event), text=True, capture_output=True,
                              timeout=TIMEOUT, cwd=self.root, env={**os.environ, **self.env, **(extra or {})})

    def test_hook_ignores_caller_overlay(self):
        decoy_config = self.root / "caller-config"
        decoy = decoy_config / "router/routes.local.json"
        decoy.parent.mkdir(parents=True)
        decoy.write_text(json.dumps({"context": {"caps": {"exec": 1}},
                                     "router": {"modes": {"brief": "off"}}}))
        original = decoy.read_bytes()
        caller = {"HOME": str(self.root / "caller-home"),
                  "XDG_CONFIG_HOME": str(decoy_config), "ROUTER_LOCAL": str(decoy)}
        self.assertTrue(all(self.env.get(name) != caller[name] for name in caller))
        prompt = "TASK build\nFILES a.py\nRETURN lines"
        denied = (2, "brief: BAR missing (builder needs TASK, FILES, BAR, RETURN in that order)\n")
        # Positive control: the same decoy, handed to the hook's own environment, turns the brief check off.
        control = self.hook(prompt, extra=caller)
        self.assertEqual((control.returncode, control.stderr), (0, ""))
        self.assertEqual(self.env["ROUTER_LOCAL"], "off")
        # Pinned run with the decoy in the caller's environment gives the shipped-table result.
        with patch.dict(os.environ, caller):
            result = self.hook(prompt)
        self.assertEqual((result.returncode, result.stderr), denied)
        self.assertEqual(decoy.read_bytes(), original)
        self.assertFalse(list(self.state.rglob("*overlay*")))

    def test_problem_order_and_role_sets(self):
        for role, good, cases in (
            ("builder", FULL, [("TASK x\nFILES x\nRETURN x", "BAR missing"),
                               (FULL + "\nFILES again", "FILES appears more than once"),
                               ("TASK: \nFILES x\nBAR x\nRETURN x", "TASK is empty"),
                               ("TASK x\nBAR x\nFILES x\nRETURN x", "BAR is out of order")]),
            ("researcher", READ, [("TASK x", "RETURN missing"),
                                  (READ + "\nTASK again", "TASK appears more than once"),
                                  ("TASK: \nRETURN x", "TASK is empty"),
                                  ("RETURN x\nTASK x", "RETURN is out of order")])):
            self.assertIsNone(common.brief_problem(good, role, ROUTES))
            for prompt, problem in cases:
                with self.subTest(role=role, problem=problem):
                    self.assertIn(problem, common.brief_problem(prompt, role, ROUTES))

    def test_denial_is_free_and_read_role_passes(self):
        result = self.hook("TASK build\nFILES a.py\nRETURN lines")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stderr.strip(), "brief: BAR missing (builder needs TASK, FILES, BAR, RETURN in that order)")
        ledger_path = self.state / "ledger.sqlite3"
        if ledger_path.exists():
            with sqlite3.connect(ledger_path) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0,
                                 "a denied brief spent a ladder attempt")
                self.assertEqual(db.execute("SELECT count(*) FROM lanes").fetchone()[0], 0,
                                 "a denied brief wrote a lane")
        self.assertFalse(list((self.state / "chain").glob("*.spawn")) if (self.state / "chain").exists() else False)
        self.assertEqual(self.hook(READ, "researcher").returncode, 0)

    def test_field_prose_and_notes(self):
        for line, expected in (("Task x", []), ("TASK x", ["TASK"]), ("TASK: x", ["TASK"]),
                               ("task: x", ["TASK"]), ("  FILES: x", ["FILES"]),
                               ("Files are listed below", [])):
            with self.subTest(line=line):
                self.assertEqual([name for name, _ in common.brief_fields(line)], expected)
        self.assertIsNone(common.brief_problem(FULL + "\nNotes are allowed.\nroute: light", "builder", ROUTES))
        self.assertIsNone(common.brief_problem("Task x\n" + FULL, "builder", ROUTES))

    def test_roles_contain_exact_contract_sentence(self):
        sentences = {
            "full": "Before any work, check the brief has exactly one nonempty TASK, FILES, BAR, and RETURN, in that order. A field is a line starting with its name in capitals followed by a colon or a space, or its name in any case followed by a colon. If any field is missing, duplicated, empty, or out of order, return NOT DONE naming the problem and do nothing else.",
            "read": "Before any work, check the brief has exactly one nonempty TASK and RETURN. FILES and BAR are optional, at most once each. Fields keep the order TASK, FILES, BAR, RETURN. A field is a line starting with its name in capitals followed by a colon or a space, or its name in any case followed by a colon. If the brief breaks this, return NOT DONE naming the problem and do nothing else.",
        }
        for group in ("full", "read"):
            sentence = sentences[group]
            for role in ROUTES["brief"][group]["roles"]:
                for path in (ROOT / "agents" / f"{role}.md", ROOT / "codex/agents" / f"{role}.toml"):
                    with self.subTest(path=path):
                        self.assertIn(sentence, path.read_text(encoding="utf-8"))

    def assert_rerun_denied(self, result, seeded):
        """Exit 2 with exactly the documented message, naming the seeded PASS to the minute."""
        self.assertEqual(result.returncode, 2, result.stderr)
        match = RERUN_DENY.fullmatch(result.stderr)
        self.assertIsNotNone(match, result.stderr)
        self.assertEqual(match.group(1), datetime.fromtimestamp(seeded, timezone.utc).strftime("%Y-%m-%d %H:%M"))

    def log_rows(self, name="spawns"):
        path = Path(self.env["ROUTER_STATE"]) / f"{name}.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        return [{k: v for k, v in row.items() if k != "ts"} for row in rows]

    def test_modes_unlisted_and_corrupt_config(self):
        missing = "TASK build\nFILES a.py\nRETURN lines"
        self.assertEqual(self.hook(missing, "custom-role").returncode, 0)
        for mode in ("shadow", "off"):
            routes = copy.deepcopy(ROUTES)
            routes["router"]["modes"]["brief"] = mode
            self.routes.write_text(json.dumps(routes), encoding="utf-8")
            self.assertEqual(self.hook(missing).returncode, 0, mode)
            self.assertEqual([r for r in self.log_rows() if r.get("rule") == "brief"],
                             [{"decision": "allow", "rule": "brief", "shadow": True}] if mode == "shadow" else [],
                             mode)
            (self.state / "spawns.jsonl").unlink(missing_ok=True)
        routes["brief"] = {"full": "broken"}
        self.routes.write_text(json.dumps(routes), encoding="utf-8")
        self.assertEqual(self.hook(missing).returncode, 0)
        self.assertIn("spawn_guard", (self.state / "errors.jsonl").read_text(encoding="utf-8"))

    # Codex spawn_agent messages are encrypted; only the pinned role text carries C1 there.
    # Codex has no verdict path, so the rerun rule cannot fire on that host.
    def test_codex_encrypted_message_is_allowed(self):
        result = self.hook("encrypted:opaque", "sweeper", guard="codex_spawn_guard.py")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fixture_replay(self):
        rows = [json.loads(line) for line in (ROOT / "tests/fixtures/spawn.jsonl").read_text(encoding="utf-8").splitlines()]
        replay = [row for row in rows if "brief" in row]
        self.assertGreaterEqual(len(replay), 14)
        for index, row in enumerate(replay):
            with self.subTest(row=row["name"]):
                self.env["ROUTER_STATE"] = str(self.state / str(index))
                ti = row["tool_input"]
                result = self.hook(ti["prompt"], ti.get("subagent_type", "worker"), session=row["name"])
                if row["brief"] == "ok":
                    self.assertNotEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn("brief:", result.stderr)
                else:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(row["brief"].removeprefix("deny: "), result.stderr)

    def test_recent_pass_blocks_once_without_spending_round(self):
        project = common.project_key(str(self.root))
        key = common.claude_key(project, common.brief_hash(FULL))
        seeded = time.time()
        self.assertTrue(ledger.record_verdict(key, "PASS", project=project, session=common.session_hash("one"),
                                              now=seeded, path=self.state / "ledger.sqlite3"))
        first = self.hook(FULL)
        self.assert_rerun_denied(first, seeded)
        rerun_file = self.state / "rerun" / "one.json"
        self.assertEqual(json.loads(rerun_file.read_text(encoding="utf-8")), [key])
        self.assertEqual(stat.S_IMODE(rerun_file.stat().st_mode), 0o600)
        with sqlite3.connect(self.state / "ledger.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM lanes").fetchone()[0], 0)
        second = self.hook(FULL)
        self.assertEqual(second.returncode, 0, second.stderr)
        another = self.hook(FULL, session="two")
        self.assertEqual(another.returncode, 0, another.stderr)
        self.assertNotIn("already passed", another.stderr)
        changed = self.hook(FULL.replace("src/helper.py", "src/other.py"))
        self.assertNotIn("already passed", changed.stderr)

    def test_rerun_verdict_and_mode_edges(self):
        project = common.project_key(str(self.root))
        key = common.claude_key(project, common.brief_hash(FULL))
        self.assertIsNone(common.rerun_denied(key, "one", None, 12))
        self.assertIsNone(common.rerun_denied(key, "one", {"verdict": "FAIL", "ts": time.time()}, 12))
        self.assertIsNone(common.rerun_denied(key, "one", {"verdict": "PASS", "ts": time.time() - 13 * 3600}, 12))
        for mode in ("shadow", "off"):
            routes = copy.deepcopy(ROUTES)
            routes["router"]["modes"]["rerun"] = mode
            self.routes.write_text(json.dumps(routes), encoding="utf-8")
            self.assertTrue(ledger.record_verdict(key, "PASS", now=time.time(), path=self.state / "ledger.sqlite3"))
            result = self.hook(FULL, session=mode)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("already passed", result.stderr)

    def test_rerun_modes_same_session(self):
        project = common.project_key(str(self.root))
        key = common.claude_key(project, common.brief_hash(FULL))
        for mode in ("enforce", "shadow", "off"):
            with self.subTest(mode=mode):
                state = self.state / mode
                state.mkdir(parents=True)
                self.env["ROUTER_STATE"] = str(state)
                routes = copy.deepcopy(ROUTES)
                routes["router"]["modes"]["rerun"] = mode
                self.routes.write_text(json.dumps(routes), encoding="utf-8")
                seeded = time.time()
                self.assertTrue(ledger.record_verdict(key, "PASS", project=project, session=common.session_hash("one"),
                                                      now=seeded, path=state / "ledger.sqlite3"))
                result = self.hook(FULL)
                rerun = [r for r in self.log_rows() if r.get("rule") == "rerun"]
                if mode == "enforce":
                    self.assert_rerun_denied(result, seeded)
                    self.assertEqual(rerun, [{"decision": "block", "rule": "rerun", "shadow": False}])
                    self.assertEqual(self.hook(FULL).returncode, 0)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("already passed", result.stderr)
                    self.assertEqual(rerun, [{"decision": "allow", "rule": "rerun", "shadow": True}] if mode == "shadow" else [])

    def test_route_brief_schema(self):
        for mutate in (lambda r: r["brief"]["full"].update(roles=["builder", "sweeper"]),
                       lambda r: r["brief"]["read"].update(fields=["TASK", "UNKNOWN"]),
                       lambda r: r["brief"]["read"].update(roles=["unknown-role"])):
            routes = copy.deepcopy(ROUTES)
            mutate(routes)
            self.assertTrue(common.validate_routes(routes))

    def test_parser_error_allows_without_logging_prompt(self):
        event = {"tool_name": "Agent", "tool_input": {"subagent_type": "researcher", "prompt": READ}}
        with patch.object(common, "killed", return_value=False), patch.object(common, "read_event", return_value=event), \
             patch.object(common, "load_routes", return_value=ROUTES), \
             patch.object(common, "brief_problem", side_effect=ValueError("bad parser")), \
             patch.object(common, "emit"), \
             patch.dict(os.environ, {"ROUTER_STATE": str(self.state), "HOME": self.env["HOME"],
                                     "XDG_CONFIG_HOME": self.env["XDG_CONFIG_HOME"],
                                     "ROUTER_LOCAL": "off"}), \
             patch.object(common, "log", wraps=common.log) as logged:
            self.assertEqual(spawn_guard.main(), 0)
        self.assertIn(("errors", {"script": "spawn_guard", "error": "bad parser"}),
                      [call.args for call in logged.call_args_list])
        errors = self.log_rows("errors")
        self.assertIn({"script": "spawn_guard", "error": "bad parser"}, errors)
        for name in ("errors", "spawns"):
            path = self.state / f"{name}.jsonl"
            self.assertNotIn("find helper", path.read_text(encoding="utf-8") if path.exists() else "", name)


if __name__ == "__main__":
    unittest.main()
