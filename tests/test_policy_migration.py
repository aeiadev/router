#!/usr/bin/env python3
"""Effective policy inspection and explicit legacy inheritance cleanup."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "hooks/router"))
import common  # noqa: E402
from _platform import BASH, IS_WINDOWS, posix  # noqa: E402

CLI = [BASH, posix(ROOT / "bin/router")] if IS_WINDOWS else [sys.executable, "-B", str(ROOT / "bin/router")]


class PolicyMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.local = self.home / "config/routes.json"
        self.env = dict(os.environ, HOME=str(self.home), ROUTER_LOCAL=str(self.local),
                        ROUTER_STATE=str(self.home / "state"), CLAUDE_HOME=str(self.home / "claude"),
                        CODEX_HOME=str(self.home / "codex"))

    def run_cli(self, *args, **environment):
        return subprocess.run([*CLI, *args], env=dict(self.env, **environment), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20)

    def write(self, patch):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_text(json.dumps(patch, indent=2) + "\n", encoding="utf-8")

    def test_policy_precedence_and_named_agents(self):
        for name, kind in (("seat-exec", "governed"), ("builder", "governed"),
                           ("my-custom", "passthrough"), ("explore", "passthrough"),
                           ("fork", "fork")):
            with self.subTest(name=name):
                result = self.run_cli("status", "--agent", name)
                self.assertEqual(result.returncode, 1 if "overlay file not found" in result.stdout else 0, result.stdout + result.stderr)
                self.assertIn(f"agent requested: {name}", result.stdout)
                self.assertIn(f"agent behavior: {kind}", result.stdout)
        self.write({"router": {"modes": {"redirect": "off", "ladder": "enforce"}}})
        result = self.run_cli("status", "--agent", "builder")
        self.assertIn("redirect: off", result.stdout)
        self.assertIn("ladder", result.stdout)
        self.assertIn("forced", result.stdout)
        self.assertIn("global: off", self.run_cli("status", "--agent", "builder", ROUTER_OFF="1").stdout)

    def test_named_custom_models_and_rule_modes_are_unchanged(self):
        routes = common.load_routes(overlay=False)
        modes = dict(routes["router"]["modes"])
        for redirect in ("off", "shadow", "enforce"):
            modes["redirect"] = redirect
            for requested in (None, "inherit", "haiku", "claude-sonnet-4-5"):
                with self.subTest(redirect=redirect, requested=requested):
                    tool_input = {"subagent_type": "my-custom", "prompt": "custom task"}
                    if requested is not None:
                        tool_input["model"] = requested
                    result = common.decide_spawn(tool_input, routes, modes, [], agent_pin=lambda _: "opus")
                    self.assertEqual((result["decision"], result["rule"]), ("allow", "passthrough"))
                    self.assertEqual(result["run_model"], requested)
                    self.assertIsNone(result["updated"])

    def test_migration_preview_backup_and_repeat(self):
        patch = {"router": {"modes": {"risk": "shadow", "inject_unlisted": "enforce"},
                            "unlisted": {"models": {"my-custom": "opus"}}},
                 "aliases": {"old": "builder"}, "owner": {"note": "keep"}}
        self.write(patch)
        original = self.local.read_bytes()
        preview = self.run_cli("config", "migrate", "inheritance")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertIn("legacy", preview.stdout.lower())
        self.assertEqual(self.local.read_bytes(), original)
        self.assertEqual(list(self.local.parent.glob("*.bak*")), [])
        applied = self.run_cli("config", "migrate", "inheritance", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        changed = json.loads(self.local.read_text())
        self.assertEqual(changed["router"]["modes"]["inject_unlisted"], "off")
        self.assertEqual(changed["router"]["modes"]["risk"], "shadow")
        self.assertEqual(changed["router"]["unlisted"], patch["router"]["unlisted"])
        self.assertEqual(changed["aliases"], patch["aliases"])
        self.assertEqual(changed["owner"], patch["owner"])
        backups = list(self.local.parent.glob("routes.json.bak*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        if not IS_WINDOWS:
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        after = self.local.read_bytes()
        repeat = self.run_cli("config", "migrate", "inheritance", "--apply")
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        self.assertEqual(self.local.read_bytes(), after)
        self.assertEqual(list(self.local.parent.glob("routes.json.bak*")), backups)

    def test_invalid_and_custom_mode_key(self):
        self.write({"version": 2})
        before = self.local.read_bytes()
        result = self.run_cli("config", "migrate", "inheritance", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.local.read_bytes(), before)
        self.assertFalse(list(self.local.parent.glob("routes.json.bak*")))
        for key in ("legacy_custom", "legacy.custom"):
            with self.subTest(key=key):
                self.write({"router": {"unlisted": {"mode_key": key}, "modes": {key: "enforce"}}})
                result = self.run_cli("config", "migrate", "inheritance", "--apply")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.local.read_text())["router"]["modes"][key], "off")

    @unittest.skipIf(IS_WINDOWS, "symlink and POSIX file modes are host dependent")
    def test_backup_collisions_and_symlink_refusal(self):
        self.write({"router": {"modes": {"inject_unlisted": "enforce"}}})
        original = self.local.read_bytes()
        collision = self.local.with_name(self.local.name + ".bak")
        collision.write_bytes(b"owner backup")
        result = self.run_cli("config", "migrate", "inheritance", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(collision.read_bytes(), b"owner backup")
        self.assertEqual(self.local.with_name(self.local.name + ".bak.1").read_bytes(), original)
        target = self.home / "target.json"
        target.write_bytes(original)
        self.local.unlink()
        self.local.symlink_to(target)
        result = self.run_cli("config", "migrate", "inheritance", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(self.local.is_symlink())


if __name__ == "__main__":
    unittest.main()
