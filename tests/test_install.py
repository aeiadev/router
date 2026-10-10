#!/usr/bin/env python3
"""Host selection and configuration merge checks; never launches a host CLI."""
import json
import hashlib
from datetime import datetime
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
                        XDG_CONFIG_HOME=str(self.home / ".config"), ROUTER_LOCAL="off",
                        PYTHONDONTWRITEBYTECODE="1")
        self.assertTrue(all(name in self.env for name in ("HOME", "XDG_CONFIG_HOME", "ROUTER_LOCAL")))
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

    def test_manifest_package_stamp(self):
        self.install("--host", "both")
        for host in ("claude", "codex"):
            path = self.home / ("." + host) / "router/install-manifest.json"
            first = json.loads(path.read_text())
            self.assertEqual(first["version"], 1)
            self.assertEqual(first["package"], (ROOT / "VERSION").read_text().strip())
            self.assertEqual(first["source"], str(ROOT))
            self.assertIsInstance(first["installed_at"], str)
            self.assertIsNotNone(datetime.fromisoformat(first["installed_at"].replace("Z", "+00:00")).tzinfo)
        self.install("--host", "both")
        for host in ("claude", "codex"):
            value = json.loads((self.home / ("." + host) / "router/install-manifest.json").read_text())
            self.assertEqual(value["package"], "0.3.0")

    def test_both_preflights_codex_before_claude_writes(self):
        claude = self.home / ".claude"
        claude.mkdir()
        (claude / "settings.json").write_bytes(b'{"user_setting":true}\n')
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "hooks.json").write_text('{"hooks": []}')
        def snapshot(folder):
            return hashlib.sha256(b"".join(
                str(path.relative_to(folder)).encode() + b"\0" + path.read_bytes()
                for path in sorted(folder.rglob("*")) if path.is_file())).digest()
        before = snapshot(claude)
        result = self.install("--host", "both", expected=1)
        self.assertIn("hooks.json", result.stderr)
        self.assertEqual(snapshot(claude), before)
        self.assertEqual(sorted(p.name for p in claude.iterdir()), ["settings.json"])

    def test_uninstall_restores_exact_original_bytes(self):
        originals = {}
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = self.home / ("." + host)
            folder.mkdir()
            originals[host] = b'{\n\t"z": 2,\n\t"a": 1\n}' + (b'\n' if host == "claude" else b'')
            (folder / filename).write_bytes(originals[host])
        self.install("--host", "both")
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            self.assertEqual((self.home / ("." + host) / filename).read_bytes(), originals[host])

    def test_uninstall_preserves_intervening_user_edit(self):
        path = self.home / ".claude/settings.json"
        path.parent.mkdir()
        path.write_bytes(b'{"user":1}\n')
        self.install("--host", "claude")
        config = json.loads(path.read_text())
        config["later_edit"] = True
        path.write_text(json.dumps(config))
        self.install("--host", "claude", "--uninstall")
        self.assertTrue(json.loads(path.read_text())["later_edit"])

    def test_uninstall_removes_settings_created_by_router(self):
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            self.assertFalse((self.home / ("." + host) / filename).exists())
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_uninstall_preserves_preexisting_empty_object(self):
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = self.home / ("." + host)
            folder.mkdir()
            (folder / filename).write_bytes(b'{}')
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            self.assertEqual((self.home / ("." + host) / filename).read_bytes(), b'{}')

    def test_install_file_errors_name_paths(self):
        cases = (("claude", "settings.json", b"{bad"),
                 ("claude", "router/install-manifest.json", b'{"version": 1, "files": []}'),
                 ("codex", "hooks.json", b'{"hooks": []}'))
        for index, (host, relative, payload) in enumerate(cases):
            folder = self.home / str(index) / host
            folder.mkdir(parents=True)
            self.env[host.upper() + "_HOME"] = str(folder)
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            result = self.install("--host", host, expected=1)
            self.assertEqual(len(result.stderr.splitlines()), 1)
            self.assertIn(str(path), result.stderr)
            self.assertNotIn("Traceback", result.stderr)

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
                    path = folders[host] / filename
                    if case in ("absent", "legacy_manifest"):
                        self.assertFalse(path.exists())
                        continue
                    config = json.loads(path.read_text())
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
                self.assertFalse((self.home / ("." + host) / filename).exists())

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
                    # had_hooks decides what is left; the original bytes come back only when they hold it.
                    path = folder / ("settings.json" if host == "claude" else "hooks.json")
                    self.assertEqual(path.read_text(), json.dumps({"hooks": {}}) if expected_with_hooks else "{}\n")
            # A file edited since install takes the surgical path, where had_hooks decides.
            self.install("--host", "both")
            for host, folder in folders.items():
                path = folder / ("settings.json" if host == "claude" else "hooks.json")
                config = json.loads(path.read_text())
                config["extra"] = 1
                path.write_text(json.dumps(config))
            self.install("--host", "both", "--uninstall")
            for host, folder in folders.items():
                with self.subTest(case=case, host=host, edited=True):
                    path = folder / ("settings.json" if host == "claude" else "hooks.json")
                    expected = {"extra": 1} if not expected_with_hooks else {"extra": 1, "hooks": {}}
                    self.assertEqual(json.loads(path.read_text()), expected)

    def v02_install(self, *options):
        tree = os.environ.get("ROUTER_V02")
        self.assertTrue(tree, "ROUTER_V02 is unset; source old-trees.sh")
        result = subprocess.run([self.shell, str(Path(tree) / "install.sh"), *options],
                                env=self.env, text=True, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_upgrade_from_v02_manifest_records_the_v02_backup(self):
        names = (("claude", "settings.json"), ("codex", "hooks.json"))
        original = json.dumps({"keep": "me"}).encode()
        for host, filename in names:
            folder = self.home / ("." + host)
            folder.mkdir()
            (folder / filename).write_bytes(original)
            (folder / filename).chmod(0o644)
        self.v02_install("--host", "both")
        old = {host: sorted((self.home / ("." + host)).glob(filename + ".router-backup-*")) for host, filename in names}
        self.install("--host", "both")
        for host, _ in names:
            manifest = json.loads((self.home / ("." + host) / "router/install-manifest.json").read_text())
            self.assertEqual(len(old[host]), 1, host)
            self.assertEqual([manifest.get(key) for key in ("settings_original_existed", "settings_original_backup",
                                                             "settings_original_sha256", "settings_original_mode")],
                             [True, old[host][0].name, hashlib.sha256(original).hexdigest(), 0o644], host)
        self.install("--host", "both", "--uninstall")
        for host, filename in names:
            with self.subTest(host=host):
                path = self.home / ("." + host) / filename
                self.assertEqual((path.read_bytes(), oct(path.stat().st_mode & 0o777)), (original, oct(0o644)))

    def test_rerun_after_a_sibling_settings_write_changes_nothing(self):
        names = (("claude", "settings.json"), ("codex", "hooks.json"))
        for host, filename in names:
            folder = self.home / ("." + host)
            folder.mkdir()
            (folder / filename).write_bytes(b'{"user": 1}\n')
        self.install("--host", "both")
        snapshot = {}
        for host, filename in names:
            folder = self.home / ("." + host)
            # A Harness 0.3 sibling installs after Router: its block, its 0600 write, its manifest.
            block = {"hooks": [{"type": "command", "command": f"python3 {folder}/harness/hooks/fake.py"}]}
            path = folder / filename
            value = json.loads(path.read_text())
            value["hooks"]["SessionStart"].append(block)
            path.write_bytes((json.dumps(value, indent=2) + "\n").encode())
            path.chmod(0o600)
            sibling = folder / "harness/install-manifest.json"
            sibling.parent.mkdir()
            sibling.write_text(json.dumps({"version": 1, "package": "0.3.0", "files": {},
                                           "hooks": {"SessionStart": [block]}, "had_hooks": True,
                                           "existing_empty_events": [], "statusline": None,
                                           "settings_original_existed": True, "created_dirs": ["harness"]}))
            manifest_path = folder / "router/install-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["installed_at"] = "2026-01-01T00:00:00+00:00"
            manifest_path.write_text(json.dumps(manifest))
            snapshot[host] = (path.read_bytes(), manifest_path.read_bytes())
        expected = "".join(f"Router installed for {host} in {self.home / ('.' + host)}\n"
                           f"Add {self.home / ('.' + host) / 'router/bin'} to PATH. Run router status to inspect routing.\n"
                           for host, _ in names[:1])
        codex = self.home / ".codex"
        expected += (f"Router installed for codex in {codex}\n"
                     f"Add {codex / 'router/bin'} to PATH. Run router status to inspect routing.\n"
                     f"Roles are installed in the global agents directory: {codex / 'agents'}\n"
                     "Alternatively, register a role file with -c agents.<name>.config_file=...\n"
                     "As of Codex CLI 0.156, project-level .codex/agents/ may not load under codex exec.\n"
                     "Trust the new or changed hooks in Codex before using router. Codex prompts for hook trust on startup.\n")
        for run in ("second", "third"):
            result = self.install("--host", "both")
            self.assertEqual(result.stdout, expected, run)
            for host, filename in names:
                folder = self.home / ("." + host)
                with self.subTest(run=run, host=host):
                    self.assertEqual(((folder / filename).read_bytes(),
                                      (folder / "router/install-manifest.json").read_bytes()), snapshot[host])

    def test_uninstall_removes_only_directories_router_created(self):
        (self.home / ".claude").mkdir()
        self.install("--host", "both")
        manifest = json.loads((self.home / ".claude/router/install-manifest.json").read_text())
        self.assertNotIn(".", manifest["created_dirs"])
        self.assertIn(".", json.loads((self.home / ".codex/router/install-manifest.json").read_text())["created_dirs"])
        for entry in manifest["created_dirs"]:
            self.assertFalse(Path(entry).is_absolute(), entry)
        self.install("--host", "both", "--uninstall")
        self.assertTrue((self.home / ".claude").is_dir(), "pre-existing empty home was removed")
        self.assertEqual(list((self.home / ".claude").iterdir()), [])
        self.assertFalse((self.home / ".codex").exists(), "home Router created was left behind")

    def test_uninstall_never_removes_directories_through_a_symlink(self):
        self.install("--host", "both")
        outside = self.home / "outside"
        for host in ("claude", "codex"):
            folder = self.home / ("." + host)
            (outside / host / "sub").mkdir(parents=True)
            (folder / "link").symlink_to(outside / host)
            (folder / "real/sub").mkdir(parents=True)
            (folder / "inner").symlink_to(folder / "real")
            manifest_path = folder / "router/install-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["created_dirs"] += ["link", "link/sub", "inner/sub"]
            manifest_path.write_text(json.dumps(manifest))
        result = self.install("--host", "both", "--uninstall")
        self.assertIn("Router uninstalled", result.stdout)
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                folder = self.home / ("." + host)
                self.assertTrue((outside / host / "sub").is_dir(), "removed a directory outside the home")
                self.assertTrue((folder / "real/sub").is_dir(), "removed a directory through an inner symlink")
                self.assertTrue((folder / "link").is_symlink())
                self.assertFalse((folder / "router").exists())
                for entry in ("link/sub", "inner/sub"):
                    self.assertEqual(sum(str(folder / entry) in line for line in result.stdout.splitlines()), 1,
                                     result.stdout)

    def test_uninstall_after_v02_upgrade_prunes_router_directories(self):
        pruned = ("router/bin", "router/templates", "router", "hooks/router", "agents", "skills/dispatch")
        for keep_user_files in (False, True):
            with tempfile.TemporaryDirectory() as scratch:
                self.env["HOME"] = scratch
                home = Path(scratch)
                for host in ("claude", "codex"):
                    (home / ("." + host)).mkdir()
                self.v02_install("--host", "both")
                self.install("--host", "both")
                for host in ("claude", "codex"):
                    manifest = json.loads((home / ("." + host) / "router/install-manifest.json").read_text())
                    self.assertFalse("created_dirs" in manifest, host)
                    if keep_user_files:
                        for folder in ("router", "agents", "skills/dispatch"):
                            (home / ("." + host) / folder / "mine.txt").write_text("user\n")
                self.install("--host", "both", "--uninstall")
                for host in ("claude", "codex"):
                    folder = home / ("." + host)
                    with self.subTest(host=host, keep_user_files=keep_user_files):
                        self.assertTrue(folder.is_dir(), "removed the host home")
                        self.assertFalse((folder / "hooks/router").exists())
                        self.assertFalse((folder / "router/bin").exists())
                        if keep_user_files:
                            for kept in ("router", "agents", "skills/dispatch"):
                                self.assertEqual((folder / kept / "mine.txt").read_text(), "user\n")
                        else:
                            for gone in pruned:
                                self.assertFalse((folder / gone).exists(), gone)

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
            self.assertFalse((folder / filename).exists())
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

    def test_reinstall_over_read_only_install_replaces_files(self):
        work = self.home / "src"
        work.mkdir()
        ignore = shutil.ignore_patterns(".git", "__pycache__")
        locked = work / "locked"
        shutil.copytree(ROOT, locked, ignore=ignore)
        for base, folders, files in os.walk(locked):
            for name in folders + files:
                os.chmod(Path(base) / name, (Path(base) / name).stat().st_mode & ~0o222)
        self.addCleanup(lambda: [os.chmod(Path(b), 0o755) for b, _, _ in os.walk(locked)])
        env = dict(self.env)
        run = lambda tree: subprocess.run([self.shell, str(tree / "install.sh"), "--host", "claude"],
                                          env=env, text=True, capture_output=True,
                                          stdin=subprocess.DEVNULL, timeout=40)
        self.assertEqual(run(locked).returncode, 0)
        fresh = work / "fresh"
        shutil.copytree(ROOT, fresh, ignore=ignore)
        guard = fresh / "hooks/router/codex_spawn_guard.py"
        guard.write_text(guard.read_text() + "\n# changed\n")
        script = fresh / "scripts" / sorted(p.name for p in (fresh / "scripts").iterdir() if p.is_file())[0]
        script.write_text(script.read_text() + "\n# changed\n")
        result = run(fresh)
        self.assertEqual(result.returncode, 0, result.stderr)
        home = self.home / ".claude"
        installed = home / "hooks/router/codex_spawn_guard.py"
        self.assertTrue(installed.read_text().endswith("# changed\n"))
        self.assertEqual(installed.stat().st_mode & 0o777, 0o644)
        tool = home / "router/bin" / script.name
        self.assertTrue(tool.read_text().endswith("# changed\n"))
        self.assertEqual(tool.stat().st_mode & 0o777, 0o755)

    def test_backups_and_retired_state_kept_without_purge(self):
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = self.home / ("." + host)
            folder.mkdir()
            (folder / filename).write_text(json.dumps({"keep": "me"}))
        retired = self.home / ".local/state/claude-router/attempts.sqlite3"
        retired.parent.mkdir(parents=True)
        retired.write_bytes(b"0.2 state")
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            with self.subTest(host=host):
                self.assertEqual(len(list((self.home / ("." + host)).glob(filename + ".router-backup-*"))), 1,
                                 "uninstall without --purge must keep backups")
        self.assertTrue(retired.is_file() and retired.read_bytes() == b"0.2 state",
                        "uninstall without --purge removed retired state")

    def test_purge_alone_installs_nothing(self):
        result = self.install("--host", "both", "--purge")
        self.assertEqual(result.stdout, "Purged 0 backup(s)\n")
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["commands"])

    def test_purge_with_defaults_is_one_line_error(self):
        result = self.install("--host", "both", "--purge", "--with-defaults", expected=1)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("--purge", result.stderr)
        self.assertIn("--with-defaults", result.stderr)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["commands"])


if __name__ == "__main__":
    unittest.main()
