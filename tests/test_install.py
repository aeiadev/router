#!/usr/bin/env python3
"""Host selection and configuration merge checks; never launches a host CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.path = self.home / "commands"
        self.path.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("ROUTER_") and key not in
                    ("CLAUDE_HOME", "CODEX_HOME", "ROUTES_JSON")}
        self.env.update(HOME=str(self.home), PATH=str(self.path),
                        XDG_STATE_HOME=str(self.home / ".local/state"),
                        PYTHONDONTWRITEBYTECODE="1")
        self.shell = shutil.which("bash")
        for command in ("python3", "git", "dirname"):
            (self.path / command).symlink_to(shutil.which(command))

    def install(self, *options, expected=0):
        result = subprocess.run([self.shell, str(ROOT / "install.sh"), *options],
                                env=self.env, text=True, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=40)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def assert_hosts(self, *names):
        for host in ("claude", "codex"):
            self.assertEqual((self.home / ("." + host) / "router/install-manifest.json").is_file(),
                             host in names, f"unexpected installation state for {host}")

    def test_explicit_claude(self):
        self.install("--host", "claude")
        self.assert_hosts("claude")

    def test_explicit_codex(self):
        result = self.install("--host", "codex")
        self.assert_hosts("codex")
        self.assertIn("Trust", result.stdout)
        codex = self.home / ".codex"
        self.assertTrue((codex / "agents/seat-sweep.toml").is_file())
        self.assertFalse((codex / "agents/seat-sweep.md").exists())

    def test_both_merges_and_backs_up_existing_hooks(self):
        user_hook = {"matcher": "Write", "hooks": [{"type": "command", "command": "true"}]}
        original = {"hooks": {"PreToolUse": [user_hook]}, "user_setting": "preserve"}
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = self.home / ("." + host)
            folder.mkdir()
            (folder / filename).write_text(json.dumps(original))
        self.install("--host", "both")
        self.assert_hosts("claude", "codex")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = self.home / ("." + host)
            config = json.loads((folder / filename).read_text())
            self.assertEqual(config["user_setting"], "preserve")
            self.assertIn(user_hook, config["hooks"]["PreToolUse"])
            backups = list(folder.glob(filename + ".router-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), original)
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            self.assertEqual(json.loads((self.home / ("." + host) / filename).read_text()), original)

    def test_default_uses_present_homes(self):
        (self.home / ".codex").mkdir()
        self.install()
        self.assert_hosts("codex")

    def test_default_detects_host_binaries_without_running_them(self):
        for host in ("claude", "codex"):
            command = self.path / host
            command.write_text("#!/bin/sh\nexit 99\n")
            command.chmod(0o755)
        self.install()
        self.assert_hosts("claude", "codex")

    def test_default_fresh_home_retains_claude_install(self):
        self.install()
        self.assert_hosts("claude")

    def test_unknown_host_rejected_without_writes(self):
        result = self.install("--host", "invalid", expected=2)
        self.assertIn("invalid choice", result.stderr)
        self.assert_hosts()

    def test_same_host_homes_rejected_before_writes(self):
        self.env.update(CLAUDE_HOME=str(self.home / "config"), CODEX_HOME=str(self.home / "config"))
        result = self.install("--host", "both", expected=1)
        self.assertIn("must differ", result.stderr)
        self.assertFalse((self.home / "config").exists())


if __name__ == "__main__":
    unittest.main()
