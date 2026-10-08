#!/usr/bin/env python3
"""Automatic routing regressions, isolated from host settings and model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AutoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = dict(os.environ)
        for name in list(self.env):
            if name.startswith("ROUTER_") or name in ("ROUTES_JSON", "CLAUDE_HOME", "CODEX_HOME"):
                del self.env[name]
        self.env.update(HOME=str(self.home), ROUTER_STATE=str(self.home / "state"),
                        ROUTER_OFF_FILE=str(self.home / "state/OFF"), PYTHONDONTWRITEBYTECODE="1")
        self.routes = json.loads((ROOT / "hooks/router/routes.json").read_text())
        self.routes_path = self.home / "routes.json"
        self.env["ROUTES_JSON"] = str(self.routes_path)
        self.tick = 0

    def cli(self, *args):
        self.routes_path.write_text(json.dumps(self.routes))
        return subprocess.run([sys.executable, str(ROOT / "bin/router"), *args], env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10)

    def mode(self, mode):
        # Direct state setup keeps guard regressions independent of the CLI fix.
        path = self.home / "state/auto"
        path.parent.mkdir(exist_ok=True)
        path.write_text(mode + "\n")

    def hook(self, script, event):
        self.routes_path.write_text(json.dumps(self.routes))
        self.tick += 2000
        env = dict(self.env, ROUTER_TEST_NOW_MS=str(1800000000000 + self.tick))
        return subprocess.run([sys.executable, str(ROOT / "hooks/router" / script)],
                              input=json.dumps(event), env=env, capture_output=True, text=True, timeout=10)

    def tool(self, tool="Read", inputs=None, **extra):
        return self.hook("context_guard.py", dict(hook_event_name="PreToolUse", session_id="auto-test",
                         tool_name=tool, tool_input=inputs or {"file_path": "source.py"}, **extra))

    def test_mode_cli(self):
        self.assertEqual(self.cli("auto").stdout.strip(), "nudge")
        self.assertFalse((self.home / "state/auto").exists())
        for mode in ("off", "suggest", "nudge", "enforce"):
            self.assertEqual(self.cli("auto", mode).returncode, 0)
            self.assertEqual(self.cli("auto").stdout.strip(), mode)
            self.assertIn("auto: " + mode, self.cli("status").stdout)
        self.assertEqual(self.cli("auto", "invalid").returncode, 2)
        self.assertEqual(self.cli("auto").stdout.strip(), "enforce")
        self.assertEqual(self.cli("on", "suggest").returncode, 2)
        self.assertNotIn("auto", json.loads(self.routes_path.read_text())["context"])
        self.cli("off")
        self.assertIn("off", self.cli("status").stdout)
        self.cli("on")
        self.assertEqual(self.cli("auto").stdout.strip(), "enforce")

    def test_prompt_hint_both_payloads(self):
        self.mode("suggest")
        for host in ("claude", "codex"):
            for keyword, role in (("find", "sweeper"), ("list", "sweeper"), ("count", "sweeper"),
                                  ("where", "sweeper"), ("compare", "researcher"), ("evaluate", "researcher"),
                                  ("should we", "researcher"), ("why", "researcher"), ("plan", "planner"),
                                  ("design", "planner"), ("approach", "planner"), ("fix", "builder"),
                                  ("add", "builder"), ("implement", "builder"), ("change", "builder"),
                                  ("refactor", "builder"), ("test", "test-writer"), ("readme", "docs-writer"),
                                  ("docs", "docs-writer")):
                prompt = keyword + " the requested behavior here PRIVATE_PROMPT"
                event = dict(hook_event_name="UserPromptSubmit", session_id="auto-test", prompt=prompt)
                if host == "codex":
                    event.update(turn_id="turn-test", model="example-model")
                result = self.hook("prompt_hint.py", event)
                self.assertEqual(result.returncode, 0, result.stderr)
                context = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(context["hookEventName"], "UserPromptSubmit")
                self.assertIn(role, context["additionalContext"])
                self.assertEqual(len(context["additionalContext"].splitlines()), 1)
        for log in (self.home / "state").glob("*.jsonl"):
            self.assertNotIn("PRIVATE_PROMPT", log.read_text())

    def test_hint_skips_and_kill_switch(self):
        for mode in ("off", "nudge", "enforce", "suggest"):
            self.mode(mode)
            prompts = ["fix it", "/dispatch fix the whole parser", "unmatched vocabulary sentence"]
            if mode != "suggest":
                prompts.append("fix the parser for empty strings")
            for prompt in prompts:
                p = self.hook("prompt_hint.py", dict(hook_event_name="UserPromptSubmit", prompt=prompt))
                self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)
        (self.home / "state/OFF").touch()
        p = self.hook("prompt_hint.py", dict(hook_event_name="UserPromptSubmit", prompt="fix the parser for empty strings"))
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_enforce_read_threshold_and_relax(self):
        self.mode("enforce")
        for _ in range(self.routes["context"]["chain"]["first"]):
            self.assertEqual(self.tool().returncode, 0)
        for tool in ("Read", "Grep", "Glob", "Bash", "Edit", "Write"):
            p = self.tool(tool)
            self.assertEqual(p.returncode, 2, (tool, p.stdout, p.stderr))
            self.assertIn("sweeper", p.stderr)
            self.assertIn("router auto nudge", p.stderr)
        self.mode("nudge")
        self.assertEqual(self.tool().returncode, 0)
        self.mode("off")
        for _ in range(10):
            p = self.tool()
            self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_enforce_distinct_files_and_spawn_resets_both(self):
        self.mode("enforce")
        self.assertEqual(self.routes["context"].get("files_threshold"), 3)
        for _ in range(6):
            self.assertEqual(self.tool("Edit", {"file_path": "same.py"}).returncode, 0)
        for path in ("second.py", "third.py", "fourth.py"):
            self.assertEqual(self.tool("Write", {"file_path": path}).returncode, 0)
        self.assertEqual(self.tool().returncode, 2)
        self.assertIn("builder", self.tool().stderr)
        # Exercise accepted spawns through each host's actual guard.
        for script, name, inputs in (
            ("spawn_guard.py", "Agent", {"subagent_type": "sweeper", "prompt": "find the relevant files"}),
            ("codex_spawn_guard.py", "collaborationspawn_agent", {"agent_type": "sweeper", "task_name": "scan", "message": "encrypted"}),
        ):
            p = self.hook(script, dict(hook_event_name="PreToolUse", session_id="auto-test", tool_name=name, tool_input=inputs))
            self.assertEqual(p.returncode, 0, p.stderr)
            for _ in range(self.routes["context"]["chain"]["first"]):
                self.assertEqual(self.tool().returncode, 0)
            self.assertEqual(self.tool().returncode, 2)

    def test_allow_globs_subagents_and_nonread_reset(self):
        self.mode("enforce")
        self.routes["context"]["chain"]["first"] = 2
        self.routes["context"]["read_allow_globs"] = ["*/allowed.md"]
        self.tool()
        self.tool("Bash", {"command": "git status"})
        self.assertEqual(self.tool().returncode, 0)
        self.assertEqual(self.tool().returncode, 0)
        self.assertEqual(self.tool().returncode, 2)
        self.assertEqual(self.tool(inputs={"file_path": str(self.home / "allowed.md")}).returncode, 0)
        self.assertEqual(self.tool(agent_id="child", agent_type="worker").returncode, 0)
        self.assertEqual(self.tool().returncode, 2)
        (self.home / "state/OFF").touch()
        self.assertEqual(self.tool().returncode, 0)

    def test_codex_shell_and_assumed_edit_file_path_payload(self):
        # The spike did not verify that Codex edits emit Edit with file_path.
        self.mode("enforce")
        self.routes["context"]["chain"]["first"] = 2
        for _ in range(2):
            self.assertEqual(self.tool("Bash", {"command": "rg needle src"}, turn_id="turn-test").returncode, 0)
        self.assertEqual(self.tool("Edit", {"file_path": "src/main.py"}, turn_id="turn-test").returncode, 2)

    def test_missing_codex_role_logs_null(self):
        self.routes["router"]["modes"] = dict.fromkeys(self.routes["router"]["modes"], "off")
        result = self.hook("codex_spawn_guard.py", dict(hook_event_name="PreToolUse",
                           tool_name="collaborationspawn_agent", tool_input={}))
        self.assertEqual(result.returncode, 0)
        record = json.loads((self.home / "state/spawns.jsonl").read_text().splitlines()[-1])
        self.assertIsNone(record["run_type"])

    def test_off_keeps_return_caps_and_spawn_rules(self):
        for mode in ("nudge", "off"):
            self.mode(mode)
            with self.subTest(auto=mode):
                p = self.hook("context_guard.py", dict(hook_event_name="SubagentStop", agent_type="builder",
                                                     last_assistant_message="x" * 2000))
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout)["decision"], "block")
                for script, tool, inputs in (
                    ("spawn_guard.py", "Agent", {"subagent_type": "worker-up", "prompt": "find files"}),
                    ("codex_spawn_guard.py", "collaborationspawn_agent", {"agent_type": "worker-up"}),
                ):
                    p = self.hook(script, dict(hook_event_name="PreToolUse", tool_name=tool, tool_input=inputs))
                    self.assertEqual(p.returncode, 2, (script, p.stdout, p.stderr))

    def test_off_keeps_large_read_own_mode(self):
        self.assertEqual(self.cli("auto", "off").returncode, 0)
        path = self.home / "large.txt"
        path.write_text("x" * (self.routes["context"]["large_read_bytes"] + 1))
        for mode, expected in (("enforce", 2), ("shadow", 0), ("off", 0)):
            with self.subTest(large_read=mode):
                self.routes["context"]["modes"]["large_read"] = mode
                p = self.tool(inputs={"file_path": str(path)})
                self.assertEqual(p.returncode, expected, p.stderr)
                self.assertEqual(p.stdout, "")
                if mode == "enforce":
                    self.assertIn("Read has no limit", p.stderr)
        self.routes["context"]["modes"]["large_read"] = "enforce"
        self.assertEqual(self.tool(inputs={"file_path": str(path), "limit": 10}).returncode, 0)
        self.routes["context"]["read_allow_globs"] = [str(path)]
        self.assertEqual(self.tool(inputs={"file_path": str(path)}).returncode, 0)

    def test_suggest_keeps_default_nudge_notes(self):
        for mode in ("nudge", "suggest"):
            self.mode(mode)
            # Distinct sessions start with a fresh nudge counter.
            for index in range(self.routes["context"]["chain"]["first"]):
                p = self.hook("context_guard.py", dict(hook_event_name="PreToolUse", session_id=mode,
                              tool_name="Read", tool_input={"file_path": "source.py"}))
                self.assertEqual(p.returncode, 0)
            self.assertIn("Context note", p.stdout)

    def test_tier_provenance_preserves_bases(self):
        for base, tiers in self.routes["tiers"].items():
            self.assertTrue((ROOT / f"codex/agents/{base}.toml").read_text().startswith("# Generated by codex/generate_agents.py;"))
            for spec in tiers.values():
                self.assertTrue((ROOT / f"codex/agents/{spec['agent']}.toml").read_text().startswith("# Generated by scripts/generate_roles.py;"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
