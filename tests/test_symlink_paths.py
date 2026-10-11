#!/usr/bin/env python3
"""Router state works when HOME, ~/.local or XDG_STATE_HOME sits behind a symlink.

macOS puts temporary folders under /var -> /private/var, and some Linux systems link /home or
~/.local elsewhere. A symlinked ancestor the user made is not Router's to refuse: the switch
files, the ledger and the Harness pressure signal must all be written and read through it, with
nothing logged as an error. Everything lives in a temporary folder; nothing here calls a model
or the network.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks/router"
CLI = ROOT / "bin/router"
BRIEF = "TASK add a text helper\nFILES src/text.py\nBAR run unit tests\nRETURN five lines"
READ_PRESSURE = ("import json, sys; sys.path.insert(0, sys.argv[1]); import pressure; "
                 "print(json.dumps(pressure.read(sys.argv[2], 'claude', now=float(sys.argv[3]))))")


class SymlinkedStatePaths(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-symlink-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.real = self.root / "real"
        self.link = self.root / "link"
        (self.real / "dotlocal").mkdir(parents=True)
        self.link.symlink_to(self.real, target_is_directory=True)
        self.project = self.root / "project"
        (self.project / ".git").mkdir(parents=True)
        # Each layout: environment for state, and the real folder that state must land in.
        dotlocal_home = self.real / "home-dotlocal"
        dotlocal_home.mkdir()
        (dotlocal_home / ".local").symlink_to(self.real / "dotlocal", target_is_directory=True)
        self.layouts = {
            "HOME behind a symlink": ({"HOME": str(self.link / "home")}, self.real / "home/.local/state"),
            "~/.local is a symlink": ({"HOME": str(dotlocal_home)}, self.real / "dotlocal/state"),
            "XDG_STATE_HOME behind a symlink": ({"HOME": str(self.real / "home-xdg"),
                                                 "XDG_STATE_HOME": str(self.link / "xdg")}, self.real / "xdg"),
        }

    def environment(self, layout):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("ROUTER_", "HARNESS_")) and key not in
               ("CLAUDE_HOME", "CODEX_HOME", "ROUTES_JSON", "XDG_STATE_HOME", "CLAUDE_PROJECT_DIR")}
        env.update(CLAUDE_HOME=str(self.root / "claude"), CODEX_HOME=str(self.root / "codex"),
                   XDG_CONFIG_HOME=str(self.root / "config"), ROUTER_LOCAL="off",
                   PYTHONDONTWRITEBYTECODE="1")
        env.update(layout)
        return env

    def run_cli(self, env, *args):
        result = subprocess.run([sys.executable, "-B", str(CLI), *args], env=env, cwd=self.project,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)
        return result.stdout

    def spawn(self, env):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(self.project),
                 "session_id": "symlink-session",
                 "tool_input": {"subagent_type": "builder", "prompt": BRIEF, "description": "Implement the helper"}}
        result = subprocess.run([sys.executable, "-B", str(HOOKS / "spawn_guard.py")], input=json.dumps(event),
                                env=env, cwd=self.project, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout

    def test_switches_are_written_and_read_through_a_symlinked_ancestor(self):
        for name, (layout, state) in self.layouts.items():
            with self.subTest(layout=name):
                env = self.environment(layout)
                self.assertEqual(self.run_cli(env, "auto", "enforce"), "enforce\n")
                self.assertEqual((state / "claude-router/auto").read_text(encoding="utf-8").strip(), "enforce")
                self.assertIn("auto: enforce\n", self.run_cli(env, "status"))
                self.assertEqual(self.run_cli(env, "off").splitlines()[0], "off")
                self.assertTrue((state / "claude-router/OFF").is_file())
                self.assertEqual(self.run_cli(env, "on").splitlines()[0], "on")
                self.assertFalse((state / "claude-router/OFF").exists())
                self.assertFalse((state / "claude-router/errors.jsonl").exists())

    def test_ledger_records_a_spawn_through_a_symlinked_ancestor(self):
        for name, (layout, state) in self.layouts.items():
            with self.subTest(layout=name):
                env = self.environment(layout)
                self.assertEqual(json.loads(self.spawn(env))["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
                ledger = state / "claude-router/ledger.sqlite3"
                self.assertTrue(ledger.is_file(), f"no ledger at {ledger}")
                with contextlib.closing(sqlite3.connect(str(ledger))) as conn:
                    self.assertEqual(conn.execute("SELECT round, role FROM attempts").fetchall(), [(1, "builder")])
                self.assertFalse((state / "claude-router/errors.jsonl").exists())
                self.assertRegex(self.run_cli(env, "ladder", "list"), r"(?m)^c:[0-9a-f]{16} +1/3 +std +0m$")

    def test_harness_pressure_is_read_through_a_symlinked_ancestor(self):
        session = "symlink-pressure"
        now = time.time()
        record = {"schema": 1, "host": "claude", "ts": now, "used": 750, "window": 1000,
                  "window_source": "statusline", "percent": 75.0, "level": "remind"}
        for name, (layout, state) in self.layouts.items():
            with self.subTest(layout=name):
                env = self.environment(layout)
                base = Path(env["XDG_STATE_HOME"]) if "XDG_STATE_HOME" in env else Path(env["HOME"]) / ".local/state"
                path = base / "claude-harness/pressure" / (hashlib.sha256(session.encode()).hexdigest()[:16] + ".json")
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(record), encoding="utf-8")
                self.assertTrue((state / "claude-harness/pressure" / path.name).is_file())
                result = subprocess.run([sys.executable, "-B", "-c", READ_PRESSURE, str(HOOKS), session, str(now)],
                                        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(json.loads(result.stdout), record)
                self.assertFalse((state / "claude-router/errors.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
