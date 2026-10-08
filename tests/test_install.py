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
        self.assertTrue((codex / "agents/sweeper.toml").is_file())
        self.assertFalse((codex / "agents/sweeper.md").exists())

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

    def test_uninstall_removes_only_events_emptied_by_router(self):
        unrelated = {"matcher": "Write", "hooks": [{"type": "command", "command": "true"}]}
        originals = {}
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = self.home / ("." + host)
            folder.mkdir()
            original = {"user_setting": "keep", "hooks": {"UserPromptSubmit": [],
                        "PreToolUse": [unrelated], "UserEmpty": []}}
            originals[host] = original
            (folder / filename).write_text(json.dumps(original))
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            config = json.loads((self.home / ("." + host) / filename).read_text())
            self.assertEqual(config, originals[host])

    def test_uninstall_removes_event_after_sibling_tool_leaves_it_empty(self):
        sibling = {"matcher": "Write", "hooks": [{"type": "command", "command": "true"}]}
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = self.home / ("." + host)
            folder.mkdir()
            (folder / filename).write_text(json.dumps({"hooks": {"PreToolUse": [sibling]}}))
        self.install("--host", "both")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            path = self.home / ("." + host) / filename
            config = json.loads(path.read_text())
            config["hooks"]["PreToolUse"].remove(sibling)
            path.write_text(json.dumps(config))
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            with self.subTest(host=host):
                config = json.loads((self.home / ("." + host) / filename).read_text())
                self.assertNotIn("PreToolUse", config.get("hooks", {}))

    def test_uninstall_preserves_original_or_shared_top_level_hooks(self):
        for case in ("absent", "empty", "other_tool", "legacy_manifest"):
            folders = {host: self.home / case / host for host in ("claude", "codex")}
            self.env.update(CLAUDE_HOME=str(folders["claude"]),
                            CODEX_HOME=str(folders["codex"]))
            for folder in folders.values():
                folder.mkdir(parents=True)
            if case == "empty":
                for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
                    (folders[host] / filename).write_text(json.dumps({"hooks": {}}))
            self.install("--host", "both")
            for host, folder in folders.items():
                manifest_path = folder / "router/install-manifest.json"
                manifest = json.loads(manifest_path.read_text())
                self.assertEqual(manifest.get("had_hooks"), case == "empty", host)
                if case == "legacy_manifest":
                    manifest.pop("had_hooks")
                    manifest_path.write_text(json.dumps(manifest))
            self.install("--host", "both")
            for host, folder in folders.items():
                manifest = json.loads((folder / "router/install-manifest.json").read_text())
                self.assertEqual(manifest["had_hooks"], case in ("empty", "legacy_manifest"), host)
            if case == "other_tool":
                for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
                    path = folders[host] / filename
                    config = json.loads(path.read_text())
                    config["hooks"]["OtherEvent"] = [{"hooks": [{"type": "command", "command": "true"}]}]
                    path.write_text(json.dumps(config))
            self.install("--host", "both", "--uninstall")
            for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
                with self.subTest(case=case, host=host):
                    config = json.loads((folders[host] / filename).read_text())
                    if case == "absent":
                        self.assertNotIn("hooks", config)
                    elif case == "other_tool":
                        self.assertEqual(config["hooks"], {
                            "OtherEvent": [{"hooks": [{"type": "command", "command": "true"}]}]})
                    else:
                        self.assertEqual(config["hooks"], {})

    def test_legacy_manifest_direct_uninstall_keeps_empty_hooks(self):
        self.install("--host", "both")
        for host in ("claude", "codex"):
            folder = self.home / ("." + host)
            manifest_path = folder / "router/install-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest.pop("had_hooks")
            manifest_path.write_text(json.dumps(manifest))
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            with self.subTest(host=host):
                config = json.loads((self.home / ("." + host) / filename).read_text())
                self.assertEqual(config["hooks"], {})

    def test_first_install_inherits_sibling_had_hooks_false(self):
        cases = (("valid_false", {"version": 1, "files": {}, "hooks": {}, "had_hooks": False}, False),
                 ("valid_true", {"version": 1, "files": {}, "hooks": {}, "had_hooks": True}, True),
                 ("missing_key", {"version": 1, "files": {}, "hooks": {}}, True),
                 ("invalid", {"version": 1, "files": {}, "hooks": "invalid", "had_hooks": False}, True),
                 ("invalid_list", {"version": 1, "files": {}, "hooks": [], "had_hooks": False}, True),
                 ("missing", None, True))
        for case, sibling, expected_with_hooks in cases:
            folders = {host: self.home / case / host for host in ("claude", "codex")}
            self.env.update(CLAUDE_HOME=str(folders["claude"]),
                            CODEX_HOME=str(folders["codex"]))
            for folder in folders.values():
                folder.mkdir(parents=True)
                if sibling is not None:
                    path = folder / "harness/install-manifest.json"
                    path.parent.mkdir()
                    path.write_text(json.dumps(sibling))
                filename = "settings.json" if folder == folders["claude"] else "hooks.json"
                (folder / filename).write_text(json.dumps({"hooks": {}}))
            self.install("--host", "both")
            for host, folder in folders.items():
                with self.subTest(case=case, host=host):
                    manifest = json.loads((folder / "router/install-manifest.json").read_text())
                    self.assertEqual(manifest["had_hooks"], expected_with_hooks)
            self.install("--host", "both", "--uninstall")
            for host, folder in folders.items():
                with self.subTest(case=case, host=host):
                    config = json.loads((folder / ("settings.json" if host == "claude" else "hooks.json")).read_text())
                    self.assertEqual("hooks" in config, expected_with_hooks)

    def test_harness_manifest_with_list_hooks_is_invalid_and_keeps_shared_roles(self):
        self.install("--host", "both")
        roles = ("sweeper", "researcher", "planner", "builder", "builder-in-place",
                 "judge", "worker", "test-writer", "docs-writer")
        shared = {}
        for host, extension in (("claude", ".md"), ("codex", ".toml")):
            folder = self.home / ("." + host)
            manifest = json.loads((folder / "router/install-manifest.json").read_text())
            shared[host] = [f"agents/{role}{extension}" for role in roles]
            self.assertTrue(set(shared[host]) <= set(manifest["files"]), host)
            path = folder / "harness/install-manifest.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"version": 1, "files": {}, "hooks": []}))
        result = self.install("--host", "both", "--uninstall")
        for host in ("claude", "codex"):
            folder = self.home / ("." + host)
            with self.subTest(host=host):
                self.assertFalse((folder / "router/install-manifest.json").exists())
                for name in shared[host]:
                    self.assertTrue((folder / name).is_file(), name)
                    self.assertIn(f"Keeping shared role file: {name} (invalid Harness manifest).",
                                  result.stdout)

    def test_upgrade_replaces_owned_context_registration(self):
        self.install("--host", "claude")
        folder = self.home / ".claude"
        config_path = folder / "settings.json"
        manifest_path = folder / "router/install-manifest.json"
        config = json.loads(config_path.read_text())
        manifest = json.loads(manifest_path.read_text())
        for blocks in (config["hooks"]["PreToolUse"],
                       [entry["block"] for entry in manifest["hooks"] if entry["event"] == "PreToolUse"]):
            for block in blocks:
                if "context_guard.py" in block["hooks"][0]["command"]:
                    block["matcher"] = "Read|Grep|Glob|Bash|SubagentHandback"
        config_path.write_text(json.dumps(config))
        manifest_path.write_text(json.dumps(manifest))
        self.install("--host", "claude")
        config = json.loads(config_path.read_text())
        guards = [block for block in config["hooks"]["PreToolUse"]
                  if "context_guard.py" in block["hooks"][0]["command"]]
        self.assertEqual(len(guards), 1, "upgrade must not double-count each tool call")
        self.assertIn("Edit", guards[0]["matcher"])

    def test_auto_hooks_custom_homes_merge_and_uninstall(self):
        for host in ("claude", "codex"):
            self.env[host.upper() + "_HOME"] = str(self.home / (host + " custom"))
        self.install("--host", "both")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = Path(self.env[host.upper() + "_HOME"])
            config = json.loads((folder / filename).read_text())
            hints = config["hooks"].get("UserPromptSubmit", [])
            self.assertEqual(len(hints), 1, "missing automatic prompt hint")
            command = hints[0]["hooks"][0]["command"]
            self.assertIn("prompt_hint.py", command)
            state = self.home / ".local/state/claude-router"
            state.mkdir(parents=True, exist_ok=True)
            (state / "auto").write_text("suggest\n")
            result = subprocess.run([self.shell, "-c", command], env=self.env,
                                    input=json.dumps(dict(hook_event_name="UserPromptSubmit",
                                                          prompt="find the relevant source files")),
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("sweeper", result.stdout)
            guards = [b for b in config["hooks"]["PreToolUse"]
                      if "context_guard.py" in b["hooks"][0]["command"]]
            self.assertEqual(len(guards), 1)
            for tool in ("Bash", "Edit", "Write"):
                self.assertIn(tool, guards[0]["matcher"])
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = Path(self.env[host.upper() + "_HOME"])
            self.assertNotIn("hooks", json.loads((folder / filename).read_text()))
            self.assertFalse((folder / "hooks/router/prompt_hint.py").exists())

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
