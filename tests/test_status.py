#!/usr/bin/env python3
"""Status reports overlay health without changing state."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.overlay = self.home / "config/router/routes.local.json"
        self.state = self.home / "state"
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / "config"),
                        ROUTER_LOCAL="", ROUTER_STATE=str(self.state),
                        CLAUDE_HOME=str(self.home / "claude"), CODEX_HOME=str(self.home / "codex"))

    def status(self, **env):
        return subprocess.run([sys.executable, str(ROOT / "bin/router"), "status"],
                              env=dict(self.env, **env), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=15)

    def snapshot(self):
        return {str(p.relative_to(self.home)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.home.rglob("*") if p.is_file()}

    def overlay_lines(self, result):
        return [line for line in result.stdout.splitlines() if line.startswith("overlay:")]

    def assert_overlay(self, result, expected, code):
        self.assertEqual(result.returncode, code, result.stderr)
        found = self.overlay_lines(result)
        self.assertEqual(len(found), 1, result.stdout)
        self.assertIsNotNone(re.fullmatch(re.escape(expected), found[0]), found[0])

    def test_overlay_lines_and_read_only(self):
        for value, expected, code in (
            (None, f"overlay: none ({self.overlay})", 0),
            ({}, f"overlay: ok {self.overlay} (keys changed: none)", 0),
            ({"router": {"modes": {"risk": "enforce"}}, "context": {"files_threshold": 99}},
             f"overlay: ok {self.overlay} (keys changed: context.files_threshold, router.modes.risk)", 0),
            ({"version": 2}, f"overlay: invalid {self.overlay}: version: cannot change", 1),
        ):
            with self.subTest(value=value):
                self.overlay.parent.mkdir(parents=True, exist_ok=True)
                if value is None:
                    self.overlay.unlink(missing_ok=True)
                else:
                    self.overlay.write_text(json.dumps(value))
                before = self.snapshot()
                result = self.status()
                self.assert_overlay(result, expected, code)
                self.assertEqual(result.stdout.splitlines()[0], "on")
                self.assertEqual(result.stdout.splitlines()[1], f"switch: {self.state / 'OFF'}")
                self.assertEqual(result.stdout.splitlines()[2], "auto: nudge")
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.state / "overlay-warned").exists())
        self.assert_overlay(self.status(ROUTER_LOCAL="off"), "overlay: off (ROUTER_LOCAL=off)", 0)

    def test_two_keys_and_null_delete_lines(self):
        self.overlay.parent.mkdir(parents=True)
        self.overlay.write_text(json.dumps({"context": {"files_threshold": 5}, "unused": None}))
        self.assert_overlay(self.status(),
                            f"overlay: ok {self.overlay} (keys changed: context.files_threshold, unused)", 0)

    def test_invalid_shapes_and_decoy(self):
        self.overlay.parent.mkdir(parents=True)
        cases = (("directory", "not a regular file"),
                 ("x" * 65537, "larger than 65536 bytes"),
                 ("bad json", "not valid JSON: Expecting value: line 1 column 1 (char 0)"),
                 ("[]", "must be a JSON object"),
                 ('{"version":2}', "version: cannot change"),
                 ('{"context":{"caps":{"exec":-1}}}', "context.caps.exec: cap must be a positive int, got -1"))
        for value, reason in cases:
            with self.subTest(value=value[:25]):
                if self.overlay.is_dir():
                    self.overlay.rmdir()
                if value == "directory":
                    self.overlay.unlink(missing_ok=True)
                    self.overlay.mkdir()
                else:
                    self.overlay.write_text(value)
                result = self.status()
                self.assert_overlay(result, f"overlay: invalid {self.overlay}: {reason}", 1)
                self.assertIn("auto: nudge", result.stdout.splitlines())
        self.overlay.rmdir() if self.overlay.is_dir() else self.overlay.unlink()
        decoy = self.home / "decoy.json"
        decoy.write_text('{"context":{"files_threshold":99}}')
        with patch.dict(os.environ, {"ROUTER_LOCAL": str(decoy)}):
            result = self.status()
        self.assert_overlay(result, f"overlay: none ({self.overlay})", 0)

    def test_many_keys_shows_eight_and_exact_remainder(self):
        self.overlay.parent.mkdir(parents=True)
        names = ("inject", "redirect", "block_model", "risk", "ladder", "brief", "rerun", "judge_reminder", "inject_unlisted")
        patch_value = {"router": {"modes": {key: "enforce" for key in names}}, "unused": None}
        self.overlay.write_text(json.dumps(patch_value))
        shown = sorted(f"router.modes.{key}" for key in names)[:8]
        self.assert_overlay(self.status(),
                            f"overlay: ok {self.overlay} (keys changed: {', '.join(shown)}, +2 more)", 0)
        patch_value = {"router": {"modes": {key: "enforce" for key in names[:8]}}}
        self.overlay.write_text(json.dumps(patch_value))
        shown = sorted(f"router.modes.{key}" for key in names[:8])
        self.assert_overlay(self.status(), f"overlay: ok {self.overlay} (keys changed: {', '.join(shown)})", 0)


if __name__ == "__main__":
    unittest.main()
