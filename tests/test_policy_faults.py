"""Governed policy survives unavailable accounting without inventing history."""
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
import test_spawn
import test_codex

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common
import ledger
import spawn_guard
import codex_spawn_guard


class PolicyFaults(unittest.TestCase):
    def test_configured_exec_selection_obeys_modes_with_and_without_accounting(self):
        # The enforced ladder overrides tier model mappings, but untiered roles
        # use inject in rounds 1/2. A tiered flag without levels is also untiered.
        configurations = (
            ("test-writer", False, False, "haiku", "haiku"),
            ("test-writer", True, False, "haiku", "haiku"),
            ("builder", False, True, "haiku", "haiku"),
            ("builder", True, True, "haiku", "sonnet"),
            ("configured-exec", False, False, "custom-lower", "custom-lower"),
        )
        for role, tiered, has_levels, inject, selected in configurations:
            for excluded in (False, True):
                for mode in ("off", "shadow", "enforce"):
                    for fault in (False, True):
                        with self.subTest(role=role, tiered=tiered, levels=has_levels,
                                          excluded=excluded, mode=mode, fault=fault):
                            case = test_spawn.SpawnTests(methodName="runTest")
                            case.setUp()
                            try:
                                routes = copy.deepcopy(test_spawn.ROUTES)
                                entry = copy.deepcopy(routes["router"]["types"]["test-writer"])
                                entry.update(tiered=tiered, inject=inject)
                                routes["router"]["types"][role] = entry
                                if has_levels:
                                    for spec in routes["tiers"][role].values():
                                        spec.update(model="haiku", effort="none")
                                routes["aliases"]["configured-alias"] = role
                                routes["router"]["never"] = [
                                    selected if excluded else ("haiku" if selected == "sonnet" else "sonnet"),
                                    "opus",
                                ]
                                routes["router"]["modes"]["block_model"] = mode
                                self.assertEqual(common.validate_routes(routes), [])
                                case.routes_path.write_text(json.dumps(routes))
                                if fault:
                                    case.state.mkdir()
                                    (case.state / "ledger.sqlite3").write_bytes(b"not sqlite")
                                result = case.run_hook(case.input(agent="configured-alias"))
                                if excluded and mode == "enforce":
                                    case.assert_blocked(result, "excluded")
                                    if not fault and (case.state / "ledger.sqlite3").exists():
                                        with sqlite3.connect(case.state / "ledger.sqlite3") as conn:
                                            self.assertEqual(conn.execute("SELECT count(*) FROM attempts").fetchone(), (0,))
                                elif fault:
                                    case.assert_unchanged(result)
                                    record = json.loads((case.state / "spawns.jsonl").read_text().splitlines()[-1])
                                    self.assertEqual(record["rule"], "accounting-unavailable")
                                    self.assertEqual(record["failure_stage"], "connect")
                                    self.assertFalse(record["history_available"])
                                    self.assertEqual(record["shadow"], ["never-selected"]
                                                     if excluded and mode == "shadow" else [])
                                else:
                                    self.assertEqual(case.updated(result)["model"], selected)
                                    record = json.loads((case.state / "spawns.jsonl").read_text().splitlines()[-1])
                                    self.assertEqual(record["round"], 1)
                                    self.assertEqual(record["shadow"], ["never-selected"]
                                                     if excluded and mode == "shadow" else [])
                                    with sqlite3.connect(case.state / "ledger.sqlite3") as conn:
                                        self.assertEqual(conn.execute("SELECT round FROM attempts").fetchall(), [(1,)])
                            finally:
                                case.doCleanups()

    def test_untiered_injection_with_ladder_off_or_shadow(self):
        for ladder_mode in ("off", "shadow"):
            for block_mode in ("off", "shadow", "enforce"):
                for fault in (False, True):
                    with self.subTest(ladder=ladder_mode, block=block_mode, fault=fault):
                        case = test_spawn.SpawnTests(methodName="runTest")
                        case.setUp()
                        try:
                            routes = copy.deepcopy(test_spawn.ROUTES)
                            routes["router"]["types"]["test-writer"]["inject"] = "haiku"
                            routes["router"]["never"] = ["sonnet", "opus"]
                            routes["router"]["modes"].update(ladder=ladder_mode, block_model=block_mode)
                            case.routes_path.write_text(json.dumps(routes))
                            if fault:
                                case.state.mkdir()
                                (case.state / "ledger.sqlite3").write_bytes(b"not sqlite")
                            result = case.run_hook(case.input(agent="test-writer"))
                            if fault:
                                case.assert_unchanged(result)
                            else:
                                self.assertEqual(case.updated(result)["model"], "haiku")
                        finally:
                            case.doCleanups()

    def test_untiered_unknown_selection_and_round_three(self):
        routes = copy.deepcopy(test_spawn.ROUTES)
        routes["router"]["never"] = ["sonnet", "opus"]
        modes = routes["router"]["modes"]
        request = test_spawn.SpawnTests.input(agent="test-writer")
        for inject in (None, "haiku"):
            with self.subTest(inject=inject):
                routes["router"]["types"]["test-writer"]["inject"] = inject
                self.assertIsNone(common.observable_spawn_policy(request, routes, modes))
                first = common.decide_spawn(request, routes, modes, [])
                self.assertEqual(first["decision"], "allow" if inject is None else "rewrite")
                self.assertEqual(first["run_model"], inject)
                third = common.decide_spawn(dict(request, prompt=request["prompt"] + "\nroute: up ladder"),
                                            routes, modes, [{"tier": "std"}, {"tier": "std"}])
                self.assertEqual((third["decision"], third["rule"], third["round"]),
                                 ("block", "never-selected", 3))

    def test_claude_explicit_exclusion_on_corrupt_ledger(self):
        case = test_spawn.SpawnTests(methodName="runTest")
        case.setUp()
        try:
            routes = copy.deepcopy(test_spawn.ROUTES)
            routes["router"]["never"] = ["excluded-model"]
            case.routes_path.write_text(json.dumps(routes))
            case.state.mkdir()
            (case.state / "ledger.sqlite3").write_bytes(b"not sqlite")
            result = case.run_hook(case.input(model="excluded-model"))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("excluded", result.stderr)
        finally:
            case.doCleanups()

    def test_codex_explicit_exclusion_on_corrupt_ledger(self):
        case = test_codex.CodexSpawnTests(methodName="runTest")
        case.setUp()
        try:
            routes = json.loads(Path(case.environment["ROUTES_JSON"]).read_text())
            routes["router"]["never"] = ["excluded-model"]
            path = case.root / "routes.json"
            path.write_text(json.dumps(routes))
            case.state.mkdir()
            (case.state / "ledger.sqlite3").write_bytes(b"not sqlite")
            result = case.hook(case.spawn(model="excluded-model"), extra={"ROUTES_JSON": str(path)})
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("excluded", result.stderr)
        finally:
            case.doCleanups()

    def test_every_possible_claude_ladder_selection_excluded_on_corrupt_ledger(self):
        case = test_spawn.SpawnTests(methodName="runTest")
        case.setUp()
        try:
            routes = copy.deepcopy(test_spawn.ROUTES)
            routes["router"]["never"] = ["sonnet", "opus"]
            case.routes_path.write_text(json.dumps(routes))
            case.state.mkdir()
            (case.state / "ledger.sqlite3").write_bytes(b"not sqlite")
            result = case.run_hook(case.input())
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("excluded", result.stderr)
        finally:
            case.doCleanups()

    def test_codex_ladder_does_not_guess_editable_role_model_on_corrupt_ledger(self):
        case = test_codex.CodexSpawnTests(methodName="runTest")
        case.setUp()
        try:
            routes = json.loads(Path(case.environment["ROUTES_JSON"]).read_text())
            routes["router"]["never"] = ["sonnet", "opus"]
            path = case.root / "routes.json"
            path.write_text(json.dumps(routes))
            case.state.mkdir()
            (case.state / "ledger.sqlite3").write_bytes(b"not sqlite")
            case.assert_silent(case.hook(extra={"ROUTES_JSON": str(path)}))
        finally:
            case.doCleanups()

    def test_unsupported_locked_and_unwritable_ledgers_keep_exclusion(self):
        for fault in ("unsupported", "locked", "unwritable"):
            with self.subTest(fault=fault):
                case = test_spawn.SpawnTests(methodName="runTest")
                case.setUp()
                blocker = None
                try:
                    routes = copy.deepcopy(test_spawn.ROUTES)
                    routes["router"]["never"] = ["excluded-model"]
                    case.routes_path.write_text(json.dumps(routes))
                    if fault == "unwritable":
                        case.state.write_text("occupied")
                    else:
                        case.state.mkdir()
                        path = case.state / "ledger.sqlite3"
                        if fault == "unsupported":
                            with sqlite3.connect(path) as conn:
                                conn.execute("PRAGMA user_version = 999")
                        else:
                            with contextlib.closing(ledger.connect(path)):
                                pass
                            blocker = sqlite3.connect(path, isolation_level=None)
                            blocker.execute("BEGIN EXCLUSIVE")
                    result = case.run_hook(case.input(model="excluded-model"))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("excluded", result.stderr)
                finally:
                    if blocker:
                        blocker.execute("ROLLBACK")
                        blocker.close()
                    case.doCleanups()

    def test_selected_judge_and_unlisted_exclusions(self):
        routes = copy.deepcopy(test_spawn.ROUTES)
        routes["router"]["never"] = ["opus", "excluded-model"]
        routes["router"]["unlisted"]["default_model"] = "excluded-model"
        modes = routes["router"]["modes"]
        judge = common.decide_spawn({"subagent_type": "judge", "prompt": "TASK inspect"}, routes, modes, [])
        with patch.object(common, "passthrough", return_value=False):
            unlisted = common.decide_spawn({"subagent_type": "unknown-role", "prompt": "TASK inspect"}, routes, modes, [])
        self.assertEqual(judge["decision"], "block")
        self.assertEqual(unlisted["decision"], "block")

    def test_codex_does_not_infer_a_model_from_editable_role_toml(self):
        routes = copy.deepcopy(test_spawn.ROUTES)
        routes["router"]["never"] = ["sonnet", "opus", "custom-lower"]
        routes["router"]["types"]["test-writer"]["inject"] = "custom-lower"
        for role in ("builder", "test-writer"):
            for mode in ("off", "shadow", "enforce"):
                with self.subTest(role=role, mode=mode):
                    modes = dict(routes["router"]["modes"], block_model=mode)
                    result = common.decide_codex_spawn(test_codex.CodexSpawnTests.spawn(role), routes, modes, [])
                    self.assertEqual(result["decision"], "allow")
                    self.assertIsNone(result["run_model"])
                    self.assertNotIn("never-selected", result["shadow"])
                    self.assertIsNone(common.observable_spawn_policy({"subagent_type": role}, routes, modes,
                                                                     selected_model_observable=False))

    def test_commit_failure_retains_computed_block(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.sqlite3"
            conn = ledger.connect(path)
            class CommitFault:
                def __init__(self, wrapped): self.wrapped = wrapped
                def execute(self, sql, *args):
                    if sql == "COMMIT": raise sqlite3.OperationalError("commit fault")
                    return self.wrapped.execute(sql, *args)
                def close(self): self.wrapped.close()
                @property
                def in_transaction(self): return self.wrapped.in_transaction
            with patch.object(ledger, "connect", return_value=CommitFault(conn)):
                outcome = ledger.record_attempt_and_lane("key", lambda prior: {"decision": "block", "message": "excluded"},
                                                         role="builder", path=path, structured=True)
            self.assertEqual(outcome["decision"]["decision"], "block")
            self.assertEqual(outcome["failure_stage"], "commit")
            self.assertFalse(outcome["accounting_committed"])

    def test_read_decide_and_connect_stages_are_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ledger.sqlite3"
            with patch.object(ledger, "connect", side_effect=sqlite3.OperationalError("closed")):
                connect = ledger.record_attempt_and_lane("key", lambda prior: {}, role="builder",
                                                        path=path, structured=True)
            self.assertEqual((connect["history_available"], connect["failure_stage"]), (False, "connect"))
            with contextlib.closing(ledger.connect(path)) as conn:
                conn.execute("DROP TABLE attempts")
            read = ledger.record_attempt_and_lane("key", lambda prior: {}, role="builder",
                                                  path=path, structured=True)
            self.assertEqual((read["history_available"], read["failure_stage"]), (False, "read"))
            path.unlink()
            def fail_decide(prior):
                raise ValueError("decision fault")
            decide = ledger.record_attempt_and_lane("key", fail_decide, role="builder",
                                                    path=path, structured=True)
            self.assertEqual((decide["history_available"], decide["failure_stage"]), (True, "decide"))
            self.assertFalse(decide["accounting_committed"])

    def test_missing_writable_ledger_initializes_and_commits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state/ledger.sqlite3"
            outcome = ledger.record_attempt_and_lane("key", lambda prior: {"decision": "allow", "tier": "std"},
                                                     role="builder", path=path, structured=True)
            self.assertTrue(path.is_file())
            self.assertTrue(outcome["history_available"])
            self.assertTrue(outcome["accounting_committed"])
            self.assertIsNone(outcome["failure_stage"])

    def test_exclusion_modes_and_passthrough(self):
        routes = copy.deepcopy(test_spawn.ROUTES)
        routes["router"]["never"] = ["excluded-model", "opus"]
        for mode in ("off", "shadow", "enforce"):
            modes = dict(routes["router"]["modes"], block_model=mode)
            explicit = common.observable_spawn_policy({"subagent_type": "builder", "model": "excluded-model"},
                                                      routes, modes)
            if mode == "off":
                self.assertIsNone(explicit)
            else:
                self.assertEqual(explicit["decision"], "block" if mode == "enforce" else "allow")
            custom = common.observable_spawn_policy({"subagent_type": "custom-helper", "model": "excluded-model"},
                                                    routes, modes)
            self.assertIsNone(custom)
            selected = common.decide_spawn({"subagent_type": "judge", "prompt": "TASK inspect"}, routes, modes, [])
            self.assertEqual(selected["decision"], "block" if mode == "enforce" else "rewrite")

    def test_adapters_retain_computed_block_after_commit_failure(self):
        cases = (
            (spawn_guard, {"tool_name": "Agent", "tool_input": test_spawn.SpawnTests.input()}),
            (codex_spawn_guard, {"tool_name": "spawn_agent", "tool_input": test_codex.CodexSpawnTests.spawn()}),
        )
        blocked = {"decision": "block", "rule": "ladder-owner", "message": "owner review",
                   "asked_type": "builder", "run_type": "builder", "run_model": None,
                   "class": "exec", "tier": "std", "brief": None, "round": 4,
                   "notes": [], "shadow": []}
        outcome = {"decision": blocked, "history_available": True,
                   "accounting_committed": False, "failure_stage": "commit"}
        for adapter, event in cases:
            with self.subTest(adapter=adapter.__name__), \
                 patch.object(common, "killed", return_value=False), \
                 patch.object(common, "read_event", return_value=event), \
                 patch.object(common, "load_routes", return_value=test_spawn.ROUTES), \
                 patch.object(common, "log"), \
                 patch.object(ledger, "latest_verdict", return_value=None), \
                 patch.object(ledger, "record_attempt_and_lane", return_value=outcome), \
                 contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(adapter.main(), 2)
                self.assertIn("owner review", stderr.getvalue())

    def test_uncommitted_rewrite_does_not_emit_or_count(self):
        event = {"tool_name": "Agent", "tool_input": test_spawn.SpawnTests.input()}
        rewrite = common.decide_spawn(event["tool_input"], test_spawn.ROUTES,
                                      test_spawn.ROUTES["router"]["modes"], [])
        outcome = {"decision": rewrite, "history_available": True,
                   "accounting_committed": False, "failure_stage": "commit"}
        with patch.object(common, "killed", return_value=False), \
             patch.object(common, "read_event", return_value=event), \
             patch.object(common, "load_routes", return_value=test_spawn.ROUTES), \
             patch.object(common, "log"), \
             patch.object(common, "emit") as emitted, \
             patch.object(common, "note_spawn") as counted, \
             patch.object(ledger, "latest_verdict", return_value=None), \
             patch.object(ledger, "record_attempt_and_lane", return_value=outcome):
            self.assertEqual(spawn_guard.main(), 0)
            emitted.assert_not_called()
            counted.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
