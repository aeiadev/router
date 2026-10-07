#!/usr/bin/env python3
"""Offline Codex PreToolUse regressions using encrypted-message-shaped payloads."""
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "hooks/router/codex_spawn_guard.py"
CLAUDE_GUARD = ROOT / "hooks/router/spawn_guard.py"
ROUTES = ROOT / "hooks/router/routes.json"
ROLES = ROOT / "codex/agents"


class CodexSpawnTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-codex-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.switch = self.root / "OFF"
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("ROUTER_") and key != "ROUTES_JSON"}
        self.environment.update({
            "HOME": str(self.root / "home"),
            "CLAUDE_HOME": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "ROUTER_HOME": str(self.root / "config"),
            "ROUTER_STATE": str(self.state),
            "ROUTER_OFF_FILE": str(self.switch),
            "ROUTES_JSON": str(ROUTES),
            "XDG_STATE_HOME": str(self.root / "xdg-state"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    @staticmethod
    def spawn(role="seat-exec", task="exec-helper", **fields):
        return dict(agent_type=role, task_name=task,
                    message="encrypted:opaque-message-envelope", **fields)

    def hook(self, tool_input=None, *, tool="collaborationspawn_agent", extra=None,
             event_fields=None, raw=None, guard=GUARD):
        if raw is None:
            event = {"hook_event_name": "PreToolUse", "tool_name": tool,
                     "tool_input": self.spawn() if tool_input is None else tool_input,
                     "cwd": str(self.root)}
            event.update(event_fields or {})
            raw = json.dumps(event)
        return subprocess.run([sys.executable, "-B", str(guard)], input=raw,
                              text=True, capture_output=True, timeout=20,
                              cwd=self.root, env=dict(self.environment, **(extra or {})))

    def assert_allowed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        if result.stdout.strip():
            output = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(output["hookEventName"], "PreToolUse")
            self.assertEqual(output.get("permissionDecision", "allow"), "allow")
            self.assertNotIn("updatedInput", output,
                             "Pinned Codex roles must not rely on model rewrites")

    def assert_denied(self, result, *parts):
        if result.returncode == 2:
            explanation = result.stderr
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(result.stdout.strip(), "Expected denial, got silent allow")
            output = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(output["hookEventName"], "PreToolUse")
            self.assertEqual(output["permissionDecision"], "deny")
            explanation = output.get("permissionDecisionReason", "")
        self.assertTrue(explanation.strip(), "Denied spawn must explain the correction")
        for part in parts:
            self.assertIn(part.lower(), explanation.lower())

    def assert_silent(self, result):
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_codex_tool_names_allow_pinned_roles_without_rewrites(self):
        for tool in ("collaborationspawn_agent", "spawn_agent"):
            for role in ("seat-sweep", "seat-exec", "seat-exec-here", "seat-judge"):
                with self.subTest(tool=tool, role=role):
                    self.assert_allowed(self.hook(self.spawn(role, task=role + tool), tool=tool))

    def test_non_exec_upper_roles_require_up_code_on_both_hosts(self):
        for role in ("worker-up", "seat-sweep-up", "explore-up", "plan-up"):
            with self.subTest(role=role):
                codex = self.hook(self.spawn(role))
                claude = self.hook({"subagent_type": role, "prompt": "Find the helper"},
                                   tool="Agent", guard=CLAUDE_GUARD)
                self.assert_denied(claude, "known `route: up <code>`")
                self.assert_denied(codex, "known `route: up <code>`")
                self.assertEqual(codex.stderr, claude.stderr)

    def test_top_tier_models_require_visible_up_code_on_both_hosts(self):
        for model in ("opus", "claude-opus-5-5", "gpt-6-astra", " GPT-6-ASTRA "):
            for role in ("unlisted-role", "seat-sweep"):
                for directive in ("", "\nroute: up novel", "\nroute: up invalid"):
                    for host in ("claude", "codex"):
                        with self.subTest(model=model, role=role, directive=directive, host=host):
                            if host == "claude":
                                request = dict(subagent_type=role, model=model,
                                               prompt="Find the helper" + directive)
                                result = self.hook(request, tool="Agent", guard=CLAUDE_GUARD)
                            else:
                                request = self.spawn(role, model=model)
                                request["message"] += directive
                                result = self.hook(request)
                            # A code inside an encrypted message is never visible.
                            if host == "claude" and directive == "\nroute: up novel":
                                self.assertEqual((result.returncode, result.stderr), (0, ""))
                            else:
                                self.assert_denied(result, "known `route: up <code>`")

    def test_configured_top_tier_models_obey_modes_on_both_hosts(self):
        routes = json.loads(ROUTES.read_text(encoding="utf-8"))
        routes["router"]["top_tier_models"] = ["custom-upper-model"]
        # Native upper models must also come from the route table.
        routes["tiers"]["seat-sweep"]["up"]["model"] = "sonnet"
        routes_path = self.root / "top-tier-routes.json"
        for mode in ("enforce", "shadow", "off"):
            routes["router"]["modes"]["block_model"] = mode
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            for model in ("custom-upper-model", "sonnet", "gpt-6-astra", "gpt-6-sol", "haiku"):
                for host in ("claude", "codex"):
                    with self.subTest(mode=mode, model=model, host=host):
                        if host == "claude":
                            request = dict(subagent_type="unlisted-role", model=model,
                                           prompt="Find the helper")
                            result = self.hook(request, tool="Agent", guard=CLAUDE_GUARD,
                                               extra={"ROUTES_JSON": str(routes_path)})
                        else:
                            result = self.hook(self.spawn("unlisted-role", model=model),
                                               extra={"ROUTES_JSON": str(routes_path)})
                        if mode == "enforce" and model in ("custom-upper-model", "sonnet"):
                            self.assert_denied(result, "known `route: up <code>`")
                        else:
                            self.assert_silent(result)

    def test_rule_modes_have_same_allow_deny_outcomes_on_both_hosts(self):
        routes = json.loads(ROUTES.read_text(encoding="utf-8"))
        defaults = routes["router"]["modes"]
        profiles = {"default": defaults,
                    "all-off": dict.fromkeys(defaults, "off"),
                    "all-shadow": dict.fromkeys(defaults, "shadow"),
                    "all-enforce": dict.fromkeys(defaults, "enforce")}
        for key in defaults:
            for mode in ("off", "shadow"):
                profiles[key + "-" + mode] = dict(defaults, **{key: mode})
        cases = [
            ("sweep", {"role": "seat-sweep"}),
            ("exec", {"role": "seat-exec"}),
            ("judge", {"role": "seat-judge"}),
            ("unknown-role", {"role": "unknown-role"}),
            ("missing-role", {}),
            ("empty-role", {"role": ""}),
            ("null-role", {"role": None}),
            ("invalid-role", {"role": []}),
            ("standard-model", {"role": "seat-sweep", "model": "sonnet"}),
            ("upper-model", {"role": "seat-sweep", "model": "opus"}),
            ("upper-astra", {"role": "seat-sweep", "model": "gpt-6-astra"}),
            ("unlisted-astra", {"role": "unknown-role", "model": "gpt-6-astra"}),
            ("unlisted-opus-id", {"role": "unknown-role", "model": "claude-opus-5-5"}),
            ("pinned-model", {"role": "seat-exec", "model": "gpt-6-astra"}),
            ("unknown-model", {"role": "seat-exec", "model": "unknown-model"}),
            ("excluded-model", {"role": "seat-exec", "model": "excluded-model"}),
            ("null-model", {"role": "seat-exec", "model": None}),
            ("effort", {"role": "seat-exec", "reasoning_effort": "high"}),
            ("model-effort", {"role": "seat-exec", "model_reasoning_effort": "high"}),
        ] + [(role, {"role": role}) for role in
             ("worker-up", "seat-sweep-up", "explore-up", "plan-up")]
        routes["router"]["never"] = ["excluded-model"]
        routes_path = self.root / "parity-routes.json"

        def outcome(result):
            self.assertIn(result.returncode, (0, 2), result.stderr)
            if result.returncode == 2:
                self.assertTrue(result.stderr.strip(), "Denial needs an explanation")
                return "deny"
            self.assertEqual(result.stderr, "", "Hook errors must not masquerade as parity")
            return (json.loads(result.stdout)["hookSpecificOutput"].get("permissionDecision", "allow")
                    if result.stdout.strip() else "allow")

        for profile, modes in profiles.items():
            routes["router"]["modes"] = modes
            routes_path.write_text(json.dumps(routes), encoding="utf-8")
            for name, fields in cases:
                with self.subTest(profile=profile, case=name):
                    identity = profile + "-" + name
                    claude = {"prompt": "TASK " + identity + "\nFILES helper.py"}
                    codex = {"task_name": identity, "message": "encrypted:opaque"}
                    for key, value in fields.items():
                        claude["subagent_type" if key == "role" else key] = value
                        codex["agent_type" if key == "role" else key] = value
                    extra = {"ROUTES_JSON": str(routes_path)}
                    expected = outcome(self.hook(claude, tool="Agent", guard=CLAUDE_GUARD, extra=extra))
                    actual = outcome(self.hook(codex, extra=extra))
                    self.assertEqual(actual, expected, f"{profile}: {name} host mismatch")
                    if profile in ("all-off", "all-shadow"):
                        self.assertEqual(actual, "allow")

    def test_unknown_missing_and_malformed_roles_follow_claude_allow_behavior(self):
        requests = [self.spawn("not-a-router-role"), self.spawn(""), self.spawn(None), self.spawn([])]
        missing = self.spawn()
        del missing["agent_type"]
        requests.append(missing)
        for request in requests:
            with self.subTest(request=request):
                self.assert_allowed(self.hook(request))
        self.assert_allowed(self.hook())
        self.assert_allowed(self.hook())
        self.assert_denied(self.hook(), "3")

    def test_missing_task_identity_obeys_ladder_mode(self):
        requests = [self.spawn(task=""), self.spawn(task="   "),
                    self.spawn(task=None), self.spawn(task=[])]
        missing = self.spawn()
        del missing["task_name"]
        requests.append(missing)
        routes = json.loads(ROUTES.read_text(encoding="utf-8"))
        path = self.root / "task-modes.json"
        for mode in ("enforce", "shadow", "off"):
            routes["router"]["modes"]["ladder"] = mode
            path.write_text(json.dumps(routes), encoding="utf-8")
            for request in requests:
                with self.subTest(mode=mode, request=request):
                    result = self.hook(request, extra={"ROUTES_JSON": str(path)})
                    if mode == "enforce":
                        self.assert_denied(result, "task_name")
                    else:
                        self.assert_allowed(result)

    def test_ladder_off_and_shadow_do_not_enforce_round_limits(self):
        routes = json.loads(ROUTES.read_text(encoding="utf-8"))
        path = self.root / "ladder-modes.json"
        for mode in ("off", "shadow"):
            routes["router"]["modes"]["ladder"] = mode
            path.write_text(json.dumps(routes), encoding="utf-8")
            extra = {"ROUTES_JSON": str(path)}
            for attempt in range(4):
                with self.subTest(mode=mode, attempt=attempt + 1):
                    self.assert_allowed(self.hook(self.spawn(task=mode), extra=extra))
                    claude = self.hook({"subagent_type": "seat-exec", "prompt": mode},
                                       tool="Agent", guard=CLAUDE_GUARD, extra=extra)
                    self.assertEqual(claude.returncode, 0, claude.stderr)
                    records = [json.loads(line) for line in
                               (self.state / "spawns.jsonl").read_text(encoding="utf-8").splitlines()]
                    for record in records[-2:]:
                        expected = ({2: "ladder-needs-up", 3: "ladder-owner"}.get(attempt)
                                    if mode == "shadow" else None)
                        self.assertEqual(record["shadow"], [expected] if expected else [])
            # Disabling the ladder must not manufacture an up code and bypass
            # block_model. Both hosts still reject this independent violation.
            self.assert_denied(self.hook(self.spawn("seat-exec-up", task=mode), extra=extra),
                               "known `route: up <code>`")
            self.assert_denied(self.hook({"subagent_type": "seat-exec-up", "prompt": mode},
                                         tool="Agent", guard=CLAUDE_GUARD, extra=extra),
                               "known `route: up <code>`")

    def test_unrelated_tools_are_ignored_without_state(self):
        for tool in ("Agent", "Task", "exec_command", "collaborationwait_agent"):
            with self.subTest(tool=tool):
                self.assert_silent(self.hook(self.spawn("unlisted"), tool=tool))
        self.assertFalse(self.state.exists(), "Ignored tools must not create routing state")

    def test_ladder_requires_two_standard_rounds_then_explicit_up_role(self):
        self.assert_denied(self.hook(self.spawn("seat-exec-up")))
        self.assert_allowed(self.hook(self.spawn("seat-exec-light")))
        self.assert_denied(self.hook(self.spawn("seat-exec-up")))
        self.assert_allowed(self.hook(self.spawn("seat-exec-std")))
        self.assert_denied(self.hook(), "3", "seat-exec-up")
        self.assert_denied(self.hook(self.spawn("seat-exec-light")), "3")
        self.assert_allowed(self.hook(self.spawn("seat-exec-up")))
        self.assert_denied(self.hook(self.spawn("seat-exec-up")), "owner")
        self.assert_denied(self.hook(), "owner")

    def test_task_name_changes_start_a_new_ladder(self):
        self.assert_allowed(self.hook())
        self.assert_allowed(self.hook())
        self.assert_denied(self.hook(), "3")
        self.assert_allowed(self.hook(self.spawn(task="exec-other-helper")))
        self.assert_denied(self.hook(self.spawn("seat-exec-up", "exec-third-helper")))

    def test_role_family_is_part_of_ladder_identity(self):
        self.assert_allowed(self.hook())
        self.assert_allowed(self.hook())
        self.assert_denied(self.hook(), "3")
        self.assert_allowed(self.hook(self.spawn("seat-exec-here")))
        self.assert_allowed(self.hook(self.spawn("seat-exec-here-light")))
        self.assert_denied(self.hook(self.spawn("seat-exec-here-std")), "3")
        self.assert_allowed(self.hook(self.spawn("seat-exec-here-up")))
        self.assert_denied(self.hook(self.spawn("seat-exec-here-up")), "owner")

    def test_ladder_survives_event_session_and_nested_agent_changes(self):
        self.assert_allowed(self.hook(event_fields={"session_id": "first-event"}))
        self.assert_allowed(self.hook(event_fields={"session_id": "second-event",
                                                   "agent_id": "nested-worker"}))
        self.assert_denied(self.hook(event_fields={"session_id": "third-event"}), "3")

    def test_non_exec_roles_do_not_advance_exec_ladder(self):
        for role in ("seat-sweep", "seat-judge", "Explore", "Plan", "general-purpose"):
            self.assert_allowed(self.hook(self.spawn(role)))
        self.assert_allowed(self.hook())
        self.assert_allowed(self.hook())
        self.assert_denied(self.hook(), "3")

    def test_concurrent_requests_cannot_skip_or_exceed_ladder_rounds(self):
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.hook(), range(6)))
        allowed = [result for result in results if result.returncode == 0 and
                   (not result.stdout.strip() or '"deny"' not in result.stdout)]
        self.assertEqual(len(allowed), 2, "Concurrent standard spawns bypassed round three")
        for result in results:
            if result in allowed:
                self.assert_allowed(result)
            else:
                self.assert_denied(result, "3")
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.hook(self.spawn("seat-exec-up")), range(6)))
        allowed = [result for result in results if result.returncode == 0 and
                   (not result.stdout.strip() or '"deny"' not in result.stdout)]
        self.assertEqual(len(allowed), 1, "Concurrent escalations bypassed the owner round")
        for result in results:
            if result in allowed:
                self.assert_allowed(result)
            else:
                self.assert_denied(result, "owner")

    def test_explicit_model_or_effort_does_not_add_a_host_specific_ban(self):
        for fields in ({"model": "gpt-6-astra"}, {"model": "unknown-model"},
                       {"model": "gpt-6-sol"}, {"model_reasoning_effort": "high"},
                       {"reasoning_effort": "high"}, {"reasoning_effort": "medium"}):
            with self.subTest(fields=fields):
                self.assert_allowed(self.hook(self.spawn(task="override-" + str(fields), **fields)))
        self.assert_allowed(self.hook())
        self.assert_allowed(self.hook())
        self.assert_denied(self.hook(), "3")

    def test_missing_message_does_not_block_role_routing(self):
        request = self.spawn()
        del request["message"]
        self.assert_allowed(self.hook(request))

    def test_encrypted_message_is_not_parsed_or_persisted(self):
        marker = "OPAQUE_MESSAGE_MUST_NEVER_BE_SAVED"
        task = "exec-task-name-must-be-hashed"
        first = self.spawn(task=task)
        first["message"] = marker + " route: up ladder TASK bypass FILES anything"
        self.assert_allowed(self.hook(first))
        second = self.spawn(task=task)
        second["message"] = {"encrypted": marker}
        self.assert_allowed(self.hook(second))
        self.assert_denied(self.hook(self.spawn(task=task)), "3")
        files = [path for path in self.state.rglob("*") if path.is_file()]
        self.assertTrue(files, "Persistence assertion requires actual state writes")
        for path in files:
            with self.subTest(path=path.name):
                contents = path.read_bytes()
                self.assertNotIn(marker.encode(), contents)
                self.assertNotIn(task.encode(), contents)

    def test_environment_and_file_switch_disable_both_hosts(self):
        claude = {"subagent_type": "seat-exec", "prompt": "TASK helper\nFILES src/helper.py"}
        for via_file in (False, True):
            with self.subTest(via_file=via_file):
                if via_file:
                    self.switch.touch()
                extra = {} if via_file else {"ROUTER_OFF": "1"}
                self.assert_silent(self.hook(self.spawn("unknown"), extra=extra))
                self.assert_silent(self.hook(raw="not JSON", extra=extra))
                self.assert_silent(self.hook(claude, tool="Agent", guard=CLAUDE_GUARD, extra=extra))
                self.assertFalse(self.state.exists(), "Disabled hooks must not write routing state")
                self.switch.unlink(missing_ok=True)
        self.assert_allowed(self.hook())

    def test_cli_switch_preserves_rounds_and_controls_both_hosts(self):
        self.assert_allowed(self.hook())
        self.assert_allowed(self.hook())
        self.assert_denied(self.hook(), "3")
        for action in ("off", "on"):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), action],
                                    env=self.environment, stdin=subprocess.DEVNULL,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.switch.exists(), action == "off")
            if action == "off":
                self.assert_silent(self.hook())
                self.assert_silent(self.hook({"subagent_type": "seat-exec", "prompt": "helper"},
                                             tool="Agent", guard=CLAUDE_GUARD))
        self.assert_denied(self.hook(), "3")
        self.assert_allowed(self.hook(self.spawn("seat-exec-up")))


class CodexRoleTests(unittest.TestCase):
    @staticmethod
    def scalar(source, name):
        match = re.search(r"(?m)^" + re.escape(name) + r"\s*=\s*(\"[^\"\n]*\")\s*$", source)
        if not match:
            raise AssertionError(f"Missing quoted TOML field {name}")
        return ast.literal_eval(match.group(1))

    def test_top_tier_configuration_covers_upper_and_judge_pins(self):
        routes = json.loads(ROUTES.read_text(encoding="utf-8"))
        models = {levels["up"]["model"] for levels in routes["tiers"].values() if "up" in levels}
        models.update(routes["router"].get("top_tier_models", []))
        for base, levels in routes["tiers"].items():
            specs = list(levels.values()) if routes["router"]["types"][base]["class"] == "judge" else [levels["up"]]
            for spec in specs:
                with self.subTest(role=spec["agent"]):
                    source = (ROLES / (spec["agent"] + ".toml")).read_text(encoding="utf-8")
                    self.assertIn(self.scalar(source, "model"), models,
                                  "Top-tier role pin is missing from the shared model check configuration")

    def test_every_claude_seat_and_tier_has_a_pinned_codex_role(self):
        models = {"haiku": ("gpt-6-luna", "low"), "sonnet": ("gpt-6-sol", "medium"),
                  "opus": ("gpt-6-astra", "high")}
        for agent in sorted((ROOT / "agents").glob("*.md")):
            with self.subTest(role=agent.stem):
                role = ROLES / (agent.stem + ".toml")
                self.assertTrue(role.is_file(), f"Missing Codex role: {role.name}")
                source = role.read_text(encoding="utf-8")
                self.assertEqual(self.scalar(source, "name"), agent.stem)
                claude_model = re.search(r"(?m)^model:\s*(\S+)", agent.read_text(encoding="utf-8"))
                self.assertIsNotNone(claude_model, "Claude role must declare its model")
                expected_model, expected_effort = models[claude_model.group(1)]
                self.assertEqual(self.scalar(source, "model"), expected_model)
                self.assertEqual(self.scalar(source, "model_reasoning_effort"), expected_effort)
                self.assertIn("developer_instructions", source)
                for field in ("TASK", "FILES", "BAR", "RETURN"):
                    self.assertIn(field, source, f"{role.name} omits the brief field {field}")

    def test_hooks_template_matches_codex_spawn_tool(self):
        hooks = json.loads((ROOT / "codex/hooks.json").read_text(encoding="utf-8"))
        entries = hooks.get("hooks", hooks).get("PreToolUse", [])
        self.assertTrue(entries, "Codex template needs a PreToolUse entry")
        matching = [entry for entry in entries if
                    re.search(entry.get("matcher", "^$"), "collaborationspawn_agent")]
        self.assertTrue(matching, "Hook matcher does not match the observed Codex spawn tool")
        self.assertTrue(any("codex_spawn_guard.py" in hook.get("command", "")
                            for entry in matching for hook in entry.get("hooks", [])),
                        "Spawn hook must call the thin Codex adapter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
