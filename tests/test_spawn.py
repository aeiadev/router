#!/usr/bin/env python3
"""Standalone routing regressions. All hook state lives in temporary directories."""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
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
TIMEOUT = 60


class SpawnTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-spawn-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.switch = self.root / "OFF"
        self.routes_path = self.root / "routes.json"
        self.routes_path.write_text(json.dumps(ROUTES), encoding="utf-8")
        (self.root / "routes.local.json").write_text("{}", encoding="utf-8")
        self.environment = self.hook_environment(os.environ)
        isolated = patch.dict(os.environ, self.environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)

    def hook_environment(self, outer):
        """The caller's environment with every Router input pinned inside self.root."""
        environment = {key: value for key, value in outer.items()
                       if not key.startswith("ROUTER_") and key != "ROUTES_JSON"}
        environment.update({
            "HOME": str(self.root / "home"),
            "CLAUDE_HOME": str(self.root / "claude"),
            "ROUTER_HOME": str(self.root / "config"),
            "ROUTER_STATE": str(self.state),
            "ROUTER_OFF_FILE": str(self.switch),
            "ROUTES_JSON": str(self.routes_path),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),  # no caller routes.local.json overlay
            "ROUTER_LOCAL": str(self.root / "routes.local.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        return environment

    def run_hook(self, tool_input=None, *, raw=None, tool="Agent", extra=None, event_fields=None):
        if raw is None:
            event = {"hook_event_name": "PreToolUse", "tool_name": tool,
                     "tool_input": tool_input if tool_input is not None else self.input(),
                     "cwd": str(self.root)}
            event.update(event_fields or {})
            raw = json.dumps(event)
        environment = dict(self.environment, **(extra or {}))
        return subprocess.run([sys.executable, str(GUARD)], input=raw,
                              text=True, capture_output=True, timeout=TIMEOUT,
                              cwd=self.root, env=environment)

    @staticmethod
    def input(prompt=BRIEF, agent="builder", **fields):
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
        details = [f"stderr={result.stderr!r}"]
        if result.returncode != 2:
            for name in ("errors.jsonl", "spawns.jsonl"):
                path = self.state / name
                details.append(f"{name}=" + (path.read_text() if path.exists() else "<absent>"))
        self.assertEqual(result.returncode, 2, "\n".join(details))
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
                    prompt = (BRIEF.replace("add a text helper", f"add a text helper for {spec['agent']}")
                              if ROUTES["router"]["types"][base]["class"] not in ("sweep", "research")
                              else f"TASK Find the relevant files for {spec['agent']}.\nRETURN file paths")
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
                    if base == "builder":
                        self.assertEqual(got["isolation"], "worktree")
                    else:
                        self.assertNotIn("isolation", got)

    def test_default_model_per_class(self):
        expected = {"sweeper": "haiku", "builder": "sonnet", "builder-in-place": "sonnet",
                    "judge": "opus", "researcher": "sonnet", "planner": "sonnet", "worker": "sonnet"}
        for agent, model in expected.items():
            with self.subTest(agent=agent):
                ti = self.input(BRIEF if agent not in ("sweeper", "researcher", "planner") else
                                "TASK Find the relevant files.\nRETURN file paths", agent)
                self.assertEqual(self.updated(self.run_hook(ti))["model"], model)

    def test_judge_is_opus_and_fresh_for_every_tier(self):
        for agent in ("judge", "judge-light", "judge-std", "judge-up"):
            for model in ("haiku", "sonnet", "opus"):
                with self.subTest(agent=agent, model=model):
                    ti = self.input(agent=agent, model=model, resume="previous-review")
                    got = self.updated(self.run_hook(ti))
                    self.assertEqual(got["model"], "opus")
                    self.assertNotIn("resume", got, "judge must review in a fresh context")

    def test_exec_requires_its_own_worktree_and_preserves_other_fields(self):
        ti = self.input(BRIEF, isolation="shared",
                        run_in_background=True, name="helper", max_turns=8)
        updated = self.updated(self.run_hook(ti))
        self.assertEqual(updated, dict(ti, subagent_type="builder-std",
                                       model="sonnet", isolation="worktree"))

    def test_exec_here_does_not_add_worktree_isolation(self):
        for suffix in ("", "-light", "-std", "-up"):
            for fields in ({}, {"isolation": "shared"}, {"isolation": "worktree"}):
                with self.subTest(suffix=suffix, fields=fields):
                    ti = self.input(agent="builder-in-place" + suffix, **fields)
                    result = spawn_guard.decide(ti, ROUTES, ROUTES["router"]["modes"], [])
                    updated = result["updated"]
                    self.assertEqual("isolation" in updated, "isolation" in ti)
                    self.assertEqual(updated.get("isolation"), ti.get("isolation"))

    def test_every_exec_prompt_obeys_the_ladder_in_decide(self):
        for prompt in ("fix it", "task: fix it", ""):
            for agent in ("builder", "builder-in-place-up"):
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
        routes = copy.deepcopy(ROUTES)
        routes["router"]["modes"]["brief"] = "off"  # "fix it" is unstructured, so the brief check would deny it
        self.routes_path.write_text(json.dumps(routes), encoding="utf-8")
        prompt = "fix it"
        self.assertEqual(self.updated(self.run_hook(self.input("route: up ladder\n" + prompt)))["model"], "sonnet")
        self.assertEqual(self.updated(self.run_hook(self.input(" fix  it \n")))["model"], "sonnet")
        self.assert_blocked(self.run_hook(self.input(prompt)), "Round 3", "route: up ladder")
        self.assertEqual(self.updated(self.run_hook(self.input(prompt + "\nroute: up ladder")))["model"], "opus")
        self.assert_blocked(self.run_hook(self.input(prompt + "\nroute: up ladder")), "planning", "owner")

    def test_lowercase_task_persists_all_ladder_rounds(self):
        prompt = "task: fix it\nfiles: src/helper.py\nbar: run tests\nreturn: five lines"
        self.assertEqual(self.updated(self.run_hook(self.input("route: up ladder\n" + prompt)))["model"], "sonnet")
        uppercase = "TASK: fix it\nFILES: src/helper.py\nBAR: run tests\nRETURN: five lines"
        self.assertEqual(self.updated(self.run_hook(self.input(uppercase)))["model"], "sonnet")
        self.assert_blocked(self.run_hook(self.input(prompt)), "Round 3", "route: up ladder")
        self.assertEqual(self.updated(self.run_hook(self.input(prompt + "\nroute: up ladder")))["model"], "opus")
        self.assert_blocked(self.run_hook(self.input(uppercase + "\nroute: up ladder")), "planning", "owner")

    def test_locked_ledger_fail_open_names_connect_error_then_unlocked_blocks(self):
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        blocker = sqlite3.connect(self.state / "ledger.sqlite3", isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            allowed = self.run_hook()
            self.assert_unchanged(allowed)
            errors = (self.state / "errors.jsonl").read_text()
            self.assertIn('"action":"connect"', errors)
            self.assertIn(str(self.state / "ledger.sqlite3"), errors)
            self.assertIn("locked", errors)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        self.assert_blocked(self.run_hook(), "Round 3")

    def test_ladder_rounds_one_to_four(self):
        for round_number in (1, 2):
            with self.subTest(round=round_number):
                got = self.updated(self.run_hook())
                self.assertEqual(got["model"], "sonnet")
        self.assert_blocked(self.run_hook(), "Round 3", "route: up ladder")
        got = self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))
        self.assertEqual(got["model"], "opus")
        self.assertEqual(got["subagent_type"], "builder-up")
        self.assert_blocked(self.run_hook(self.input(BRIEF + "\nroute: up ladder")), "planning", "owner")

    def test_callers_routes_overlay_never_reaches_the_hook(self):
        # A caller whose config holds routes.local.json must not change these ladder results.
        loose = json.dumps({"router": {"modes": {"ladder": "shadow"}}})
        caller_home, caller_config = self.root / "caller-home", self.root / "caller-config"
        for path in (caller_home / ".config/router/routes.local.json",
                     caller_config / "router/routes.local.json", self.root / "caller-local.json"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(loose, encoding="utf-8")
        self.environment = self.hook_environment(dict(
            os.environ, HOME=str(caller_home), XDG_CONFIG_HOME=str(caller_config),
            ROUTER_LOCAL=str(self.root / "caller-local.json")))
        for _round in (1, 2):
            self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.assert_blocked(self.run_hook(), "Round 3", "route: up ladder")
        self.assertEqual(self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))["model"], "opus")
        self.assert_blocked(self.run_hook(self.input(BRIEF + "\nroute: up ladder")), "planning", "owner")
        self.assertFalse((self.state / "errors.jsonl").exists())

    def test_ladder_requires_exact_known_escalation_code(self):
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        for route in ("route: up novel", "route: up unknown", "route: light", "route: up", "route: invalid"):
            with self.subTest(route=route):
                self.assert_blocked(self.run_hook(self.input(BRIEF + "\n" + route)), "route: up ladder")
        self.assertEqual(self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))["model"], "opus")

    def test_early_model_and_tier_requests_cannot_skip_ladder(self):
        for suffix in ("\nroute: up ladder", "\nroute: up security"):
            got = self.updated(self.run_hook(self.input(BRIEF + suffix, "builder-up", model="opus")))
            self.assertEqual(got["model"], "sonnet")
            self.assertEqual(got["subagent_type"], "builder-std")
        self.assertEqual(self.updated(self.run_hook(self.input(BRIEF + "\nroute: up ladder")))["model"], "opus")

    def test_light_attempt_consumes_a_ladder_round(self):
        first = self.updated(self.run_hook(self.input(BRIEF + "\nroute: light")))
        self.assertEqual(first["model"], "sonnet")
        self.assertEqual(first["subagent_type"], "builder-light")
        second = self.updated(self.run_hook(self.input(BRIEF + "\nroute: light")))
        self.assertEqual(second["model"], "sonnet")
        self.assertEqual(second["subagent_type"], "builder-std")
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
        self.updated(self.run_hook(self.input(agent="builder-in-place")))
        self.updated(self.run_hook(self.input(agent="builder-std"), event_fields={"agent_id": "nested-worker"}))
        self.assert_blocked(self.run_hook(self.input(agent="builder-in-place-light")), "Round 3")

    def test_non_exec_calls_do_not_consume_exec_attempts(self):
        for agent in ("sweeper", "judge", "researcher", "planner", "worker"):
            self.updated(self.run_hook(self.input(agent=agent)))
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.assert_blocked(self.run_hook(), "Round 3")

    def test_already_correct_calls_still_consume_ladder_attempts(self):
        ti = self.input(agent="builder-std", model="sonnet", isolation="worktree")
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
        self.assertEqual(len(accepted), 3, ("concurrent submissions bypassed the three-round limit: " +
                         (self.state / "errors.jsonl").read_text() if (self.state / "errors.jsonl").exists()
                         else "concurrent submissions bypassed the three-round limit"))
        self.assertEqual(len(rejected), 5)
        models = sorted(self.updated(result)["model"] for result in accepted)
        self.assertEqual(models, ["opus", "sonnet", "sonnet"])
        for result in rejected:
            self.assert_blocked(result, "planning", "owner")

    def project(self, name):
        path = self.root / name
        (path / ".git").mkdir(parents=True)
        return path

    def ledger_rows(self, sql, *args):
        with contextlib.closing(sqlite3.connect(self.state / "ledger.sqlite3")) as conn:
            rows = conn.execute(sql, args).fetchall()
            conn.commit()
            return rows

    def age_attempts(self, seconds):
        self.ledger_rows("UPDATE attempts SET ts = ts - ?", seconds)

    def test_same_brief_in_two_projects_starts_at_round_one_in_each(self):
        first, second = self.project("first"), self.project("second")
        for _ in range(2):
            self.assertEqual(self.updated(self.run_hook(event_fields={"cwd": str(first)}))["model"], "sonnet")
        self.assert_blocked(self.run_hook(event_fields={"cwd": str(first)}), "Round 3")
        self.assertEqual(self.updated(self.run_hook(event_fields={"cwd": str(second)}))["model"], "sonnet")
        rows = self.ledger_rows("SELECT project, round FROM attempts ORDER BY ts")
        self.assertEqual([row[1] for row in rows], [1, 2, 1])
        self.assertEqual(len({row[0] for row in rows}), 2, "each project needs its own ladder")

    def test_attempts_older_than_the_ladder_ttl_do_not_count(self):
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        self.age_attempts(13 * 3600)
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.assertEqual(self.ledger_rows("SELECT round FROM attempts WHERE round > 0 ORDER BY round"), [(1,)])
        self.assertEqual(self.ledger_rows("SELECT COUNT(*) FROM attempts WHERE round < 0"), [(2,)],
                         "expired attempts must stay until the daily prune")
        # A ladder that spans the TTL: the expired round drops out and numbering stays unique.
        self.age_attempts(11 * 3600)
        self.updated(self.run_hook())
        self.age_attempts(2 * 3600)
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.assertEqual(self.ledger_rows("SELECT round FROM attempts WHERE round > 0 ORDER BY round"), [(1,), (2,)])
        self.assertEqual(self.ledger_rows("SELECT COUNT(*) FROM attempts WHERE round < 0"), [(3,)])
        self.assert_blocked(self.run_hook(), "Round 3")

    def test_ladder_ttl_hours_comes_from_the_route_table(self):
        routes = copy.deepcopy(ROUTES)
        routes["router"]["ladder_ttl_hours"] = 1
        self.routes_path.write_text(json.dumps(routes), encoding="utf-8")
        self.updated(self.run_hook())
        self.updated(self.run_hook())
        self.age_attempts(2 * 3600)
        self.assertEqual(self.updated(self.run_hook())["model"], "sonnet")
        self.updated(self.run_hook())
        self.assert_blocked(self.run_hook(), "Round 3")

    def test_worktree_under_the_project_root_keeps_the_ladder(self):
        root = self.project("repo")
        (root / ".git/worktrees/lane").mkdir(parents=True)
        (root / ".git/worktrees/lane/commondir").write_text("../..\n", encoding="utf-8")
        worktree = root / ".claude/worktrees/lane"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {root / '.git/worktrees/lane'}\n", encoding="utf-8")
        (root / "src").mkdir()
        self.updated(self.run_hook(event_fields={"cwd": str(root)}))
        self.updated(self.run_hook(event_fields={"cwd": str(worktree)}))
        self.assert_blocked(self.run_hook(event_fields={"cwd": str(root / "src")}), "Round 3")

    def lanes(self):
        return self.ledger_rows("SELECT session, brief, key, project, role, tier, label, status, started, ended"
                                " FROM lanes ORDER BY started")

    def test_each_accepted_spawn_writes_one_running_lane_and_a_denial_writes_none(self):
        session = {"session_id": "session-one"}
        for agent in ("sweeper", "judge", "researcher", "planner", "worker"):
            self.updated(self.run_hook(self.input(agent=agent), event_fields=session))
        self.assertFalse((self.state / "ledger.sqlite3").exists(), "non-exec spawns must not open the ledger")
        self.updated(self.run_hook(event_fields=session))
        self.updated(self.run_hook(event_fields=session))
        self.assert_blocked(self.run_hook(event_fields=session), "Round 3")
        self.assertEqual(len(self.lanes()), 2, "a denial writes no lane")
        ladder = self.input(BRIEF + "\nroute: up ladder")
        self.assertEqual(self.updated(self.run_hook(ladder, event_fields=session))["model"], "opus")
        self.assert_blocked(self.run_hook(ladder, event_fields=session), "owner")
        lanes = self.lanes()
        attempts = self.ledger_rows("SELECT key, round, tier, role, project, session, ts FROM attempts ORDER BY round")
        self.assertEqual(len(lanes), 3, "one lane per accepted spawn, none for a denial")
        self.assertEqual([attempt[1:4] for attempt in attempts], [(1, "std", "builder"), (2, "std", "builder"),
                                                                  (3, "up", "builder")])
        expected_session = hashlib.sha256(b"session-one").hexdigest()[:16]
        for lane, attempt in zip(lanes, attempts):
            self.assertEqual(lane[0], expected_session)
            self.assertRegex(lane[1], r"^[0-9a-f]{16}$")
            self.assertEqual(lane[2:6], (attempt[0], attempt[4], attempt[3], attempt[2]))
            self.assertEqual(lane[6:8], ("Implement the helper", "running"))
            self.assertEqual((lane[8], lane[9]), (attempt[6], None))
            self.assertEqual(attempt[5], expected_session)

    def test_two_briefs_in_one_session_make_two_lanes(self):
        session = {"session_id": "session-one"}
        self.updated(self.run_hook(event_fields=session))
        self.updated(self.run_hook(self.input(BRIEF.replace("src/text.py", "src/number.py")), event_fields=session))
        lanes = self.lanes()
        self.assertEqual(len(lanes), 2)
        self.assertEqual(len({lane[0] for lane in lanes}), 1)
        self.assertEqual(len({lane[1] for lane in lanes}), 2, "each brief needs its own lane")
        self.assertEqual(len({lane[2] for lane in lanes}), 2)

    def test_lane_labels_are_scrubbed_and_can_be_switched_off(self):
        home = os.environ.get("HOME", "")
        description = f"  {home}/repo/fix\nthe\tparser\x00 " + "x" * 80
        ti = self.input()
        ti["description"] = description
        self.updated(self.run_hook(ti, event_fields={"session_id": "s"}))
        label = self.lanes()[0][6]
        self.assertTrue(label.startswith("~/repo/fix the parser x"), label)
        self.assertEqual(len(label), 60)
        ti = self.input(BRIEF.replace("text", "list"))
        ti["description"] = ["not", "text"]
        self.updated(self.run_hook(ti, event_fields={"session_id": "s"}))
        self.assertIsNone(self.lanes()[1][6], "a non-string description has no label")
        routes = copy.deepcopy(ROUTES)
        routes["router"]["lane_labels"] = False
        self.routes_path.write_text(json.dumps(routes), encoding="utf-8")
        self.updated(self.run_hook(self.input(BRIEF.replace("text", "date")), event_fields={"session_id": "s"}))
        self.assertIsNone(self.lanes()[2][6], "router.lane_labels false stores no label")

    def test_unknown_up_code_is_ignored(self):
        ti = self.input("TASK Find the relevant files.\nRETURN file paths\nroute: up unknown", "sweeper")
        got = self.updated(self.run_hook(ti))
        self.assertEqual(got["subagent_type"], "sweeper-light")
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
        # C3 stores the scrubbed description as the lane label, and only there; with
        # router.lane_labels false nothing of the description is stored at all.
        marker = "PRIVATE_PROMPT_CONTENT_MUST_NOT_BE_STORED"
        label = "PRIVATE_DESCRIPTION_ONLY_IN_THE_LANE_LABEL"
        routes = copy.deepcopy(ROUTES)
        for labels in (True, False):
            with self.subTest(lane_labels=labels):
                routes["router"]["lane_labels"] = labels
                self.routes_path.write_text(json.dumps(routes), encoding="utf-8")
                ti = self.input(BRIEF + "\n" + marker)
                ti["description"] = label
                self.updated(self.run_hook(ti))
                paths = [path for path in self.state.rglob("*") if path.is_file()]
                self.assertTrue(paths, "test needs actual state writes")
                for path in paths:
                    self.assertNotIn(marker.encode(), path.read_bytes(), str(path))
                    if path.name != "ledger.sqlite3" or not labels:
                        self.assertNotIn(label.encode(), path.read_bytes(), str(path))
                stored = [row[0] for row in self.ledger_rows("SELECT label FROM lanes")]
                self.assertEqual(stored, [label] if labels else [None])
                with contextlib.closing(sqlite3.connect(self.state / "ledger.sqlite3")) as conn:
                    for table in ("attempts", "lanes", "verdicts"):
                        names = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
                        for row in conn.execute(f"SELECT * FROM {table}"):
                            for name, cell in zip(names, row):
                                if table == "lanes" and name == "label":
                                    continue
                                self.assertNotIn(label, str(cell), f"label leaked into {table}.{name}")
                (self.state / "ledger.sqlite3").unlink()


if __name__ == "__main__":
    unittest.main(verbosity=2)
