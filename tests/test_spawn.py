#!/usr/bin/env python3
"""Standalone routing regressions. All hook state lives in temporary directories."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "hooks" / "router"
sys.path.insert(0, str(ROUTER))
import spawn_guard

GUARD = ROUTER / "spawn_guard.py"
ROUTES = json.loads((ROUTER / "routes.json").read_text(encoding="utf-8"))
BRIEF = "TASK add a text helper\nFILES src/text.py\nBAR run unit tests\nRETURN five lines"


class SpawnTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-spawn-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.switch = self.root / "OFF"
        self.routes_path = self.root / "routes.json"
        self.routes_path.write_text(json.dumps(ROUTES), encoding="utf-8")
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("ROUTER_") and key != "ROUTES_JSON"}
        self.environment.update({
            "CLAUDE_HOME": str(self.root / "claude"),
            "ROUTER_HOME": str(self.root / "config"),
            "ROUTER_STATE": str(self.state),
            "ROUTER_OFF_FILE": str(self.switch),
            "ROUTES_JSON": str(self.routes_path),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        isolated = patch.dict(os.environ, self.environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)

    def run_hook(self, tool_input=None, *, raw=None, tool="Agent", extra=None, event_fields=None):
        if raw is None:
            event = {"hook_event_name": "PreToolUse", "tool_name": tool,
                     "tool_input": tool_input if tool_input is not None else self.input(),
                     "cwd": str(self.root)}
            event.update(event_fields or {})
            raw = json.dumps(event)
        environment = dict(self.environment, **(extra or {}))
        return subprocess.run([sys.executable, str(GUARD)], input=raw,
                              text=True, capture_output=True, timeout=20,
                              cwd=self.root, env=environment)

    @staticmethod
    def input(prompt=BRIEF, agent="seat-exec", **fields):
        return dict(subagent_type=agent, prompt=prompt, description="Implement the helper", **fields)

    def updated(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        output = json.loads(result.stdout)
        specific = output["hookSpecificOutput"]
        self.assertEqual(specific["hookEventName"], "PreToolUse")
        self.assertEqual(specific["permissionDecision"], "allow")
        return specific["updatedInput"]

    def assert_blocked(self, result, *message_parts):
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        for part in message_parts:
            self.assertIn(part, result.stderr)

    def assert_unchanged(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_decision_fixtures(self):
        fixtures = ROOT / "tests" / "fixtures" / "spawn.jsonl"
        rows = [json.loads(line) for line in fixtures.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(rows), 30, "routing fixture coverage is incomplete")
        for row in rows:
            with self.subTest(row=row["name"]):
                tool_input = copy.deepcopy(row["tool_input"])
                modes = dict(ROUTES["router"]["modes"], **row.get("modes", {}))
                got = spawn_guard.decide(tool_input, ROUTES, modes, row.get("prior", []))
                self.assertEqual(tool_input, row["tool_input"], "decide mutated its input")
                for key, value in row["expect"].items():
                    self.assertEqual(got.get(key), value, f"fixture field {key}")
                if got["decision"] == "rewrite":
                    updated = got["updated"]
                    self.assertEqual(updated["subagent_type"], got["run_type"])
                    self.assertEqual(updated["model"], got["run_model"])
                    for key in tool_input.keys() - {"subagent_type", "model", "resume", "isolation"}:
                        self.assertEqual(updated[key], tool_input[key], f"rewrite changed {key}")
                elif got["decision"] == "block":
                    self.assertTrue(got["message"], "a blocked call must explain the next action")

    def test_every_declared_agent_tier_routes(self):
        for base, levels in ROUTES["tiers"].items():
            for tier, spec in levels.items():
                with self.subTest(base=base, tier=tier):
                    prompt = f"Find the relevant files for {spec['agent']}."
                    ti = self.input(prompt, spec["agent"])
                    if tier == "up":
                        # Exec up tiers require two accepted rounds for this brief.
                        if ROUTES["router"]["types"][base]["class"] == "exec":
                            self.updated(self.run_hook(ti))
                            self.updated(self.run_hook(ti))
                        ti["prompt"] += "\nroute: up ladder"
                    got = self.updated(self.run_hook(ti))
                    self.assertEqual(got["subagent_type"], spec["agent"])
                    self.assertEqual(got["model"], spec["model"])
                    if base == "seat-exec":
                        self.assertEqual(got["isolation"], "worktree")
                    else:
                        self.assertNotIn("isolation", got)

    def test_default_model_per_class(self):
        expected = {"seat-sweep": "haiku", "seat-exec": "sonnet", "seat-exec-here": "sonnet",
                    "seat-judge": "opus", "Explore": "sonnet", "Plan": "sonnet", "general-purpose": "sonnet"}
        for agent, model in expected.items():
            with self.subTest(agent=agent):
                ti = self.input("Find the relevant files.", agent)
                self.assertEqual(self.updated(self.run_hook(ti))["model"], model)

    def test_judge_is_opus_and_fresh_for_every_tier(self):
        for agent in ("seat-judge", "seat-judge-light", "seat-judge-std", "seat-judge-up"):
            for model in ("haiku", "sonnet", "opus"):
                with self.subTest(agent=agent, model=model):
                    ti = self.input(agent=agent, model=model, resume="previous-review")
                    got = self.updated(self.run_hook(ti))
                    self.assertEqual(got["model"], "opus")
                    self.assertNotIn("resume", got, "judge must review in a fresh context")

    def test_exec_requires_its_own_worktree_and_preserves_other_fields(self):
        ti = self.input("Implement the helper.", isolation="shared",
                        run_in_background=True, name="helper", max_turns=8)
        updated = self.updated(self.run_hook(ti))
        self.assertEqual(updated, dict(ti, subagent_type="seat-exec-std",
                                       model="sonnet", isolation="worktree"))

    def test_exec_here_does_not_add_worktree_isolation(self):
        for suffix in ("", "-light", "-std", "-up"):
            for fields in ({}, {"isolation": "shared"}, {"isolation": "worktree"}):
                with self.subTest(suffix=suffix, fields=fields):
                    ti = self.input(agent="seat-exec-here" + suffix, **fields)
                    result = spawn_guard.decide(ti, ROUTES, ROUTES["router"]["modes"], [])
                    updated = result["updated"]
                    self.assertEqual("isolation" in updated, "isolation" in ti)
                    self.assertEqual(updated.get("isolation"), ti.get("isolation"))

    def test_every_exec_prompt_obeys_the_ladder_in_decide(self):
        for prompt in ("fix it", "task: fix it", ""):
            for agent in ("seat-exec", "seat-exec-here-up"):
                with self.subTest(prompt=prompt, agent=agent):
                    ti = self.input("route: up ladder\n" + prompt, agent, model="opus")
                    prior = []
                    for round_number, model in ((1, "sonnet"), (2, "sonnet"), (3, "opus")):
                        result = spawn_guard.decide(ti, ROUTES, ROUTES["router"]["modes"], prior)
                        self.assertEqual(result["run_model"], model)
                        self.assertEqual(result["round"], round_number)
                        self.assertRegex(result["brief"], r"^[0-9a-f]{16}$")
                        prior.append({"tier": result["tier"]})
                    result = spawn_guard.decide(ti, ROUTES, ROUTES["router"]["modes"], prior)
                    self.assertEqual(result["decision"], "block")
                    self.assertEqual(result["round"], 4)
                    self.assertIn("planning", result["message"])
                    self.assertIn("owner", result["message"])

    def test_unstructured_prompt_persists_all_ladder_rounds(self):
        prompt = "fix it"
        self.assertEqual(self.updated(self.run_hook(self.input("route: up ladder\n" + prompt)))["model"], "sonnet")
        self.assertEqual(self.updated(self.run_hook(self.input(" fix  it \n")))["model"], "sonnet")
        self.assert_blocked(self.run_hook(self.input(prompt)), "Round 3", "route: up ladder")
        self.assertEqual(self.updated(self.run_hook(self.input(prompt + "\nroute: up ladder")))["model"], "opus")
        self.assert_blocked(self.run_hook(self.input(prompt + "\nroute: up ladder")), "planning", "owner")

    def test_lowercase_task_persists_all_ladder_rounds(self):
        prompt = "task: fix it\nfiles: src/helper.py"
        self.assertEqual(self.updated(self.run_hook(self.input("route: up ladder\n" + prompt)))["model"], "sonnet")
        uppercase = "TASK: fix it\nFILES: src/helper.py"
        self.assertEqual(self.updated(self.run_hook(self.input(uppercase)))["model"], "sonnet")
        self.assert_blocked(self.run_hook(self.input(prompt)), "Round 3", "route: up ladder")
        self.assertEqual(self.updated(self.run_hook(self.input(prompt + "\nroute: up ladder")))["model"], "opus")
        self.assert_blocked(self.run_hook(self.input(uppercase + "\nroute: up ladder")), "planning", "owner")

    def test_ladder_rounds_one_to_four(self):
        for round_number in (1, 2):
            with self.subTest(round=round_number):
                got = self.updated(self.run_hook())
                self.assertEqual(got["model"], "sonnet")
        self.assert_blocked(self.run_hook(), "Round 3", "route: up ladder")
        got = self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))
        self.assertEqual(got["model"], "opus")
        self.assertEqual(got["subagent_type"], "seat-exec-up")
        self.assert_blocked(self.run_hook(self.input(BRIEF + "\nroute: up ladder")), "planning", "owner")

    def test_ladder_requires_exact_known_escalation_code(self):
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        for route in ("route: up novel", "route: up unknown", "route: light", "route: up", "route: invalid"):
            with self.subTest(route=route):
                self.assert_blocked(self.run_hook(self.input(BRIEF + "\n" + route)), "route: up ladder")
        self.assertEqual(self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))["model"], "opus")

    def test_early_model_and_tier_requests_cannot_skip_ladder(self):
        for suffix in ("\nroute: up ladder", "\nroute: up security"):
            got = self.updated(self.run_hook(self.input(BRIEF + suffix, "seat-exec-up", model="opus")))
            self.assertEqual(got["model"], "sonnet")
            self.assertEqual(got["subagent_type"], "seat-exec-std")
        self.assertEqual(self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))["model"], "opus")

    def test_light_attempt_consumes_a_ladder_round(self):
        first = self.updated(self.run_hook(self.input(BRIEF + "\nroute: light")))
        self.assertEqual(first["model"], "sonnet")
        self.assertEqual(first["subagent_type"], "seat-exec-light")
        second = self.updated(self.run_hook(self.input(BRIEF + "\nroute: light")))
        self.assertEqual(second["model"], "sonnet")
        self.assertEqual(second["subagent_type"], "seat-exec-std")
        self.assert_blocked(self.run_hook(), "Round 3")

    def test_ladder_hash_normalizes_whitespace_and_ignores_other_sections(self):
        self.updated(self.run_hook())
        changed = "  TASK  add   a text helper  \n FILES  src/text.py \nBAR a different check\nRETURN a report"
        self.assertEqual(self.updated(self.run_hook(self.input(changed)))["model"], "sonnet")
        self.assert_blocked(self.run_hook(self.input(changed + "\nExtra instructions.")), "Round 3")

    def test_changed_task_or_files_starts_a_new_brief(self):
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        self.assert_blocked(self.run_hook(), "Round 3")
        for brief in (BRIEF.replace("text helper", "number helper"), BRIEF.replace("src/text.py", "src/number.py")):
            with self.subTest(brief=brief):
                self.assertEqual(self.updated(self.run_hook(self.input(brief)))["model"], "sonnet")

    def test_ladder_is_shared_across_exec_agents_and_event_sessions(self):
        self.updated(self.run_hook(self.input(agent="seat-exec-here")))
        self.updated(self.run_hook(self.input(agent="seat-exec-std"), event_fields={"agent_id": "nested-worker"}))
        self.assert_blocked(self.run_hook(self.input(agent="seat-exec-here-light")), "Round 3")

    def test_non_exec_calls_do_not_consume_exec_attempts(self):
        for agent in ("seat-sweep", "seat-judge", "Explore", "Plan", "general-purpose"):
            self.updated(self.run_hook(self.input(agent=agent)))
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.assert_blocked(self.run_hook(), "Round 3")

    def test_already_correct_calls_still_consume_ladder_attempts(self):
        ti = self.input(agent="seat-exec-std", model="sonnet", isolation="worktree")
        self.assert_unchanged(self.run_hook(ti))
        self.assert_unchanged(self.run_hook(ti))
        self.assert_blocked(self.run_hook(ti), "Round 3")

    def test_ladder_survives_spawn_log_removal(self):
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        for path in self.state.glob("*.jsonl"):
            path.unlink()
        self.assert_blocked(self.run_hook(), "Round 3")

    def test_concurrent_attempts_cannot_run_more_than_three_rounds(self):
        ti = self.input(BRIEF + "\nroute: up ladder")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: self.run_hook(ti), range(8)))
        accepted = [result for result in results if result.returncode == 0]
        rejected = [result for result in results if result.returncode == 2]
        self.assertEqual(len(accepted), 3, "concurrent submissions bypassed the three-round limit")
        self.assertEqual(len(rejected), 5)
        models = sorted(self.updated(result)["model"] for result in accepted)
        self.assertEqual(models, ["opus", "sonnet", "sonnet"])
        for result in rejected:
            self.assert_blocked(result, "planning", "owner")

    def test_unknown_up_code_is_ignored(self):
        ti = self.input("Find the relevant files.\nroute: up unknown", "seat-sweep")
        got = self.updated(self.run_hook(ti))
        self.assertEqual(got["subagent_type"], "seat-sweep-light")
        self.assertEqual(got["model"], "haiku")

    def test_switch_file_and_environment_make_hook_silent(self):
        for use_file in (False, True):
            with self.subTest(file=use_file):
                if use_file:
                    self.switch.touch()
                extra = {} if use_file else {"ROUTER_OFF": "1"}
                self.assert_unchanged(self.run_hook(extra=extra))
                self.assert_unchanged(self.run_hook(raw="not JSON", extra=extra))
                self.assertFalse(self.state.exists(), "disabled hook wrote state")
                self.switch.unlink(missing_ok=True)
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")

    def test_switch_runs_before_configuration_is_loaded(self):
        self.routes_path.write_text("not JSON", encoding="utf-8")
        self.assert_unchanged(self.run_hook(extra={"ROUTER_OFF": "1"}))
        self.switch.touch()
        self.assert_unchanged(self.run_hook())
        self.assertFalse(self.state.exists(), "disabled hook read configuration or logged")

    def test_switch_bypasses_a_blocked_brief_without_consuming_rounds(self):
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        self.assert_blocked(self.run_hook(), "Round 3")
        self.assert_unchanged(self.run_hook(extra={"ROUTER_OFF": "1"}))
        self.switch.touch()
        self.assert_unchanged(self.run_hook())
        self.switch.unlink()
        self.assertEqual(self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))["model"], "opus")

    def test_false_environment_switch_keeps_routing_enabled(self):
        got = self.updated(self.run_hook(extra={"ROUTER_OFF": "0"}))
        self.assertEqual(got["model"], "sonnet")

    def test_malformed_input_fails_open(self):
        for raw in ("", "{broken", "[]", "null", '"text"', "42"):
            with self.subTest(raw=raw):
                self.assert_unchanged(self.run_hook(raw=raw))
        for ti in ([], "text", {}, {"prompt": None}, {"prompt": BRIEF, "model": []},
                   {"prompt": BRIEF, "subagent_type": 42}):
            with self.subTest(tool_input=ti):
                self.assert_unchanged(self.run_hook(ti))
        self.assertFalse(self.state.exists(), "invalid events should not create state")

    def test_malformed_or_missing_routes_fail_open(self):
        for contents in ("{broken", "{}", "[]"):
            with self.subTest(contents=contents):
                self.routes_path.write_text(contents, encoding="utf-8")
                self.assert_unchanged(self.run_hook())
        self.routes_path.unlink()
        self.assert_unchanged(self.run_hook())

    def test_unwritable_state_fails_open(self):
        # A regular file makes the state path unusable even when tests run as root.
        self.state.write_text("occupied", encoding="utf-8")
        self.assert_unchanged(self.run_hook())

    def test_non_agent_tools_are_silent(self):
        for tool in ("Read", "Bash", "Unknown"):
            with self.subTest(tool=tool):
                self.assert_unchanged(self.run_hook(tool=tool))
        self.assertFalse(self.state.exists())

    def test_task_tool_uses_the_same_ladder(self):
        self.assertEqual(self.updated(self.run_hook(tool="Task"))["model"], "sonnet")
        self.assertEqual(self.updated(self.run_hook(tool="Agent"))["model"], "sonnet")
        self.assert_blocked(self.run_hook(tool="Task"), "Round 3")

    def test_logs_do_not_store_prompt_or_description(self):
        marker = "PRIVATE_PROMPT_CONTENT_MUST_NOT_BE_STORED"
        ti = self.input(BRIEF + "\n" + marker)
        ti["description"] = marker
        self.updated(self.run_hook(ti))
        paths = [path for path in self.state.rglob("*") if path.is_file()]
        self.assertTrue(paths, "test needs actual state writes")
        for path in paths:
            self.assertNotIn(marker.encode(), path.read_bytes(), str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
