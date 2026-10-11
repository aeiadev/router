#!/usr/bin/env python3
"""Backup file modes across install, upgrade and uninstall: every Router backup ends 0600.

The 0.1 release tree comes from old-trees.sh (ROUTER_V01); the test that needs it fails
with a message when it is unset. Under a python3 older than 3.10 it runs with a newer
one from PATH, and skips only when there is none. Homes, state and overlay paths are pinned.
"""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _platform import BASH, can_symlink, home_env, posix, posix_modes  # noqa: E402


import _released_python

RELEASED_PYTHON = []  # one shim folder whose python3 is verified as 3.10 or newer


def released_env(env):
    return _released_python.released_env(env, RELEASED_PYTHON)


class BackupModes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude = self.base / "claude"
        self.claude.mkdir()

    def env(self, claude):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("ROUTER_", "CLAUDE_", "CODEX_", "XDG_", "HARNESS_")) and key != "ROUTES_JSON"}
        env.update(**home_env(self.base / "home"), CLAUDE_HOME=str(claude), CODEX_HOME=str(self.base / "codex"),
                   XDG_CONFIG_HOME=str(self.base / "config"), XDG_STATE_HOME=str(self.base / "state"),
                   ROUTER_LOCAL="off", PYTHONDONTWRITEBYTECODE="1")
        return env

    def run_installer(self, tree, claude, *options):
        env = self.env(claude) if tree == ROOT else released_env(self.env(claude))
        result = subprocess.run([BASH, posix(tree / "install.sh"), "--host", "claude", *options],
                                env=env, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def backups(self, claude, name):
        return sorted(claude.glob(name + ".router-backup-*"))

    def mode(self, path):
        return oct(stat.S_IMODE(path.lstat().st_mode))

    def test_uninstall_defaults_backup_is_private(self):
        # Line kept and tested (not deleted): uninstall makes a defaults backup when the install
        # found the block already in place and so recorded no backup of its own.
        first = self.base / "first"
        first.mkdir()
        self.run_installer(ROOT, first, "--with-defaults")
        block = (first / "CLAUDE.md").read_bytes()
        instructions = self.claude / "CLAUDE.md"
        instructions.write_bytes(block)
        instructions.chmod(0o644)
        self.run_installer(ROOT, self.claude, "--with-defaults")
        self.assertEqual(self.backups(self.claude, "CLAUDE.md"), [], "the install had nothing to back up")
        self.run_installer(ROOT, self.claude, "--uninstall")
        backups = self.backups(self.claude, "CLAUDE.md")
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0].read_bytes(), instructions.read_bytes()), (block, b""))
        if posix_modes(self, "defaults backup and CLAUDE.md modes"):
            self.assertEqual((self.mode(backups[0]), self.mode(instructions)), (oct(0o600), oct(0o644)))

    def test_backup_left_by_v01_becomes_private_on_upgrade(self):
        value = os.environ.get("ROUTER_V01")
        self.assertTrue(value, "ROUTER_V01 is unset; source old-trees.sh before this test")
        old = Path(value)
        settings = self.claude / "settings.json"
        settings.write_text('{"model": "sonnet"}\n')
        settings.chmod(0o644)
        self.run_installer(old, self.claude)
        backups = self.backups(self.claude, "settings.json")
        self.assertEqual(len(backups), 1, "0.1 no longer leaves a settings backup")
        modes = posix_modes(self, "0.1 backup made private on upgrade; NTFS has no 0600 to set")
        if modes:
            self.assertEqual(self.mode(backups[0]), oct(0o644), "0.1 no longer leaves a 0644 backup")
        outside = self.base / "outside.json"
        outside.write_text("{}")
        outside.chmod(0o644)
        if can_symlink():
            (self.claude / "link.router-backup-1").symlink_to(outside)
        else:
            print(f"SKIP {self.id()} symlink backup fixture: this user cannot create symlinks",
                  file=sys.stderr, flush=True)
        dry = self.run_installer(ROOT, self.claude, "--dry-run")
        self.assertNotIn("Made backup private", dry.stdout)
        if modes:
            self.assertEqual(self.mode(backups[0]), oct(0o644), "--dry-run changed a mode")
        result = self.run_installer(ROOT, self.claude)
        self.assertEqual([line for line in result.stdout.splitlines() if line.startswith("Made backup private")],
                         [f"Made backup private (0600): {backups[0]}"] if modes else [])
        if modes:
            self.assertEqual(self.mode(backups[0]), oct(0o600))
            self.assertEqual(self.mode(outside), oct(0o644), "a link named like a backup was followed")
        again = self.run_installer(ROOT, self.claude)
        self.assertNotIn("Made backup private", again.stdout)
        for path in self.backups(self.claude, "settings.json") if modes else ():
            self.assertEqual(self.mode(path), oct(0o600), path)


if __name__ == "__main__":
    unittest.main()
