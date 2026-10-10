#!/usr/bin/env python3
"""Upgrade cleanup, shared-role skew, --purge and backup modes, checked against the released trees.

The released trees come from old-trees.sh (ROUTER_V01, ROUTER_V02, HARNESS_V01, HARNESS_V02).
A test that needs one fails with a message when it is unset; it never skips.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
# host, settings file, agents source in a checkout, role extension
HOSTS = (("claude", "settings.json", "agents", ".md"), ("codex", "hooks.json", "codex/agents", ".toml"))
ROLES = ("sweeper", "researcher", "planner", "builder", "builder-in-place",
         "judge", "worker", "test-writer", "docs-writer")


def tree_hash(path):
    """Hash every entry below path: relative name, type and mode, bytes or link target."""
    digest = hashlib.sha256()
    for base, folders, files in os.walk(path):
        folders.sort()
        for name in sorted(folders + files):
            item = Path(base) / name
            info = item.lstat()
            digest.update(str(item.relative_to(path)).encode() + b"\0" + oct(info.st_mode).encode() + b"\0")
            if item.is_symlink():
                digest.update(os.readlink(item).encode())
            elif item.is_file():
                digest.update(item.read_bytes())
            digest.update(b"\1")
    return digest.hexdigest()


def directories(path):
    return {str(Path(base).relative_to(path)) for base, _, _ in os.walk(path)}


class UpgradeBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.path = self.home / "commands"
        self.path.mkdir()
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ROUTER_", "HARNESS_")) and key not in
                    ("CLAUDE_HOME", "CODEX_HOME", "ROUTES_JSON")}
        self.env.update(HOME=str(self.home), PATH=str(self.path),
                        XDG_STATE_HOME=str(self.home / ".local/state"),
                        XDG_CONFIG_HOME=str(self.home / ".config"), ROUTER_LOCAL="off",
                        PYTHONDONTWRITEBYTECODE="1")
        self.assertTrue(all(name in self.env for name in ("HOME", "XDG_CONFIG_HOME", "ROUTER_LOCAL")))
        self.shell = shutil.which("bash")
        for command in ("python3", "git", "dirname"):
            (self.path / command).symlink_to(shutil.which(command))

    def old_tree(self, name):
        value = os.environ.get(name)
        self.assertTrue(value, f"{name} is unset; source old-trees.sh before running this test")
        tree = Path(value)
        self.assertTrue((tree / "install.sh").is_file(), f"{name} has no install.sh: {tree}")
        return tree

    def run_installer(self, tree, *options, expected=0):
        result = subprocess.run([self.shell, str(tree / "install.sh"), *options],
                                env=self.env, text=True, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, expected,
                         f"{tree.name} install.sh {' '.join(options)}:\n{result.stdout}{result.stderr}")
        return result

    def install(self, *options, expected=0):
        return self.run_installer(ROOT, *options, expected=expected)

    def host_home(self, host):
        return self.home / ("." + host)

    def manifest(self, host):
        return json.loads((self.host_home(host) / "router/install-manifest.json").read_text())


class UpgradeCleanup(UpgradeBase):
    def old_only(self, old, agents):
        """Agent files the old tree ships and this checkout does not."""
        names = lambda folder: {path.name for path in folder.iterdir() if path.is_file()}
        return names(old / agents) - names(ROOT / agents)

    def check_cleanup(self, name):
        old = self.old_tree(name)
        self.run_installer(old, "--host", "both")
        before = {host: self.manifest(host)["files"] for host, *_ in HOSTS}
        result = self.install("--host", "both")
        self.assertNotIn("Keeping modified old file", result.stdout)
        for host, _, agents, _ in HOSTS:
            home = self.host_home(host)
            after = self.manifest(host)["files"]
            gone = {f"agents/{entry}" for entry in self.old_only(old, agents)}
            dropped = set(before[host]) - set(after)
            with self.subTest(version=name, host=host):
                self.assertLessEqual(gone, dropped, "old-only agents still recorded")
                for relative in sorted(dropped):
                    self.assertFalse((home / relative).exists() or (home / relative).is_symlink(),
                                     f"no-longer-shipped file remains: {relative}")
                    self.assertEqual(result.stdout.count(f"Removed no-longer-shipped: {home / relative}\n"), 1,
                                     f"removal of {relative} not reported once:\n{result.stdout}")
        return old, result

    def test_upgrade_from_v01_removes_no_longer_shipped_files(self):
        old, _ = self.check_cleanup("ROUTER_V01")
        claude = self.old_only(old, "agents")
        codex = self.old_only(old, "codex/agents")
        for expected in ("Explore.md", "Plan.md", "general-purpose.md", "seat-exec.md", "explore-std.md", "plan-up.md"):
            self.assertIn(expected, claude)
        for expected in ("seat-exec.toml", "seat-judge-up.toml", "Explore.toml", "general-purpose.toml"):
            self.assertIn(expected, codex)
        for host, names in (("claude", claude), ("codex", codex)):
            left = sorted(name for name in names if (self.host_home(host) / "agents" / name).exists())
            self.assertEqual(left, [], f"{host} keeps 0.1 agents")

    def test_upgrade_from_v02_removes_no_longer_shipped_files(self):
        self.check_cleanup("ROUTER_V02")

    def test_edited_old_file_survives_and_is_reported(self):
        old = self.old_tree("ROUTER_V01")
        self.run_installer(old, "--host", "both")
        edited = {"claude": self.host_home("claude") / "agents/Explore.md",
                  "codex": self.host_home("codex") / "agents/seat-exec.toml"}
        for path in edited.values():
            path.chmod(0o644)  # The released trees are read-only, so the 0.1 copy is too.
            path.write_text(path.read_text() + "\nmy note\n")
        result = self.install("--host", "both")
        for host, path in edited.items():
            with self.subTest(host=host):
                self.assertTrue(path.is_file(), f"edited old file was deleted: {path}")
                self.assertTrue(path.read_text().endswith("\nmy note\n"), "edited old file changed")
                self.assertIn(f"Keeping modified old file: {path}\n", result.stdout)
                self.assertNotIn(f"Removed no-longer-shipped: {path}\n", result.stdout)
                self.assertNotIn(str(path.relative_to(self.host_home(host))), self.manifest(host)["files"],
                                 "a kept old file must leave Router's manifest")
        self.install("--host", "both", "--uninstall")
        for path in edited.values():
            self.assertTrue(path.is_file() and path.read_text().endswith("\nmy note\n"),
                            f"uninstall removed the user's old file: {path}")

    def test_upgrade_cleanup_removes_no_directory(self):
        self.run_installer(self.old_tree("ROUTER_V01"), "--host", "both")
        for host, *_ in HOSTS:
            home = self.host_home(host)
            extra = home / "old-dir/old.md"
            extra.parent.mkdir()
            extra.write_text("shipped by an old version\n")
            manifest_path = home / "router/install-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["old-dir/old.md"] = {"sha256": hashlib.sha256(extra.read_bytes()).hexdigest()}
            manifest_path.write_text(json.dumps(manifest))
        result = self.install("--host", "both")
        for host, *_ in HOSTS:
            home = self.host_home(host)
            self.assertIn(f"Removed no-longer-shipped: {home / 'old-dir/old.md'}\n", result.stdout)
            self.assertFalse((home / "old-dir/old.md").exists())
            self.assertTrue((home / "old-dir").is_dir(), "upgrade cleanup removed a directory")

    def test_settings_hold_no_router_hook_after_v01_upgrade_and_uninstall(self):
        user_hook = {"matcher": "Write", "hooks": [{"type": "command", "command": "true"}]}
        for host, filename, *_ in HOSTS:
            self.host_home(host).mkdir()
            (self.host_home(host) / filename).write_text(json.dumps({"keep": "me", "hooks": {"PreToolUse": [user_hook]}}))
        self.run_installer(self.old_tree("ROUTER_V01"), "--host", "both")
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        for host, filename, *_ in HOSTS:
            with self.subTest(host=host):
                text = (self.host_home(host) / filename).read_text()
                config = json.loads(text)
                self.assertEqual(config["keep"], "me")
                self.assertEqual(config["hooks"]["PreToolUse"], [user_hook])
                self.assertNotIn("hooks/router", text)
                self.assertNotIn("router", json.dumps(config["hooks"]))

    def test_dry_run_lists_cleanup_and_writes_nothing(self):
        old = self.old_tree("ROUTER_V01")
        self.run_installer(old, "--host", "both")
        edited = self.host_home("claude") / "agents/Plan.md"
        edited.chmod(0o644)
        edited.write_text(edited.read_text() + "\nmine\n")
        before = tree_hash(self.home)
        result = self.install("--host", "both", "--dry-run")
        self.assertEqual(tree_hash(self.home), before, "dry-run changed the host homes")
        self.assertEqual(result.stdout.count(f"Would keep modified old file: {edited}\n"), 1, result.stdout)
        for host, _, agents, _ in HOSTS:
            for name in self.old_only(old, agents):
                path = self.host_home(host) / "agents" / name
                if path != edited:
                    self.assertEqual(result.stdout.count(f"Would remove no-longer-shipped: {path}\n"), 1,
                                     f"{path} not listed once:\n{result.stdout}")
        self.assertNotIn("Removed no-longer-shipped", result.stdout)


class SharedRoleSkew(UpgradeBase):
    SKEW = "shared roles come from Harness 0.2.0 or earlier; upgrade it for the 0.3 roles\n"

    def roles(self, host):
        return [f"agents/{role}{'.md' if host == 'claude' else '.toml'}" for role in ROLES]

    def checkout_role(self, host, relative):
        return (ROOT / ("agents" if host == "claude" else "codex/agents") / Path(relative).name).read_bytes()

    def role_bytes(self, host):
        return {relative: (self.host_home(host) / relative).read_bytes() for relative in self.roles(host)}

    def check_skew(self, name):
        self.run_installer(self.old_tree(name), "--host", "both")
        for host in ("claude", "codex"):
            home = self.host_home(host)
            with self.subTest(harness=name, host=host):
                missing = self.roles(host)[5]  # judge: a claimed role that is absent is written normally
                (home / missing).unlink()
                theirs = {relative: (home / relative).read_bytes() for relative in self.roles(host) if relative != missing}
                self.assertTrue(any(data != self.checkout_role(host, relative) for relative, data in theirs.items()),
                                f"{name} roles equal this checkout's; the skew fixture proves nothing")
                preview = self.install("--host", host, "--dry-run")
                self.assertEqual(preview.stdout.count(self.SKEW), 1, preview.stdout)
                result = self.install("--host", host)
                self.assertEqual(result.stdout.count(self.SKEW), 1, result.stdout)
                expected = dict(theirs, **{missing: self.checkout_role(host, missing)})
                self.assertEqual(self.role_bytes(host), expected, "Router rewrote a role Harness claims")
                claimed = self.manifest(host)["files"]
                for relative, data in expected.items():
                    self.assertEqual(claimed.get(relative), {"sha256": hashlib.sha256(data).hexdigest()}, relative)
                result = self.install("--host", host, "--uninstall")
                self.assertEqual(self.role_bytes(host), expected, "uninstall removed a role Harness claims")
                self.install("--host", host)
                sibling_path = home / "harness/install-manifest.json"
                sibling = json.loads(sibling_path.read_text())
                for relative in self.roles(host):
                    sibling["files"].pop(relative)
                sibling_path.write_text(json.dumps(sibling))
                result = self.install("--host", host)
                self.assertNotIn("shared roles come from", result.stdout)
                for relative in self.roles(host):
                    self.assertEqual((home / relative).read_bytes(), self.checkout_role(host, relative),
                                     f"unclaimed role was not updated: {relative}")
                edited = home / self.roles(host)[0]
                edited.write_bytes(edited.read_bytes() + b"\n# mine\n")
                refused = self.install("--host", host, expected=1)
                self.assertIn("refusing to overwrite", refused.stderr)

    def test_harness_v02_first_keeps_its_roles(self):
        self.check_skew("HARNESS_V02")

    def test_harness_v01_first_keeps_its_roles(self):
        self.check_skew("HARNESS_V01")

    def test_invalid_harness_manifest_still_refuses_differing_roles(self):
        self.run_installer(self.old_tree("HARNESS_V02"), "--host", "both")
        for host in ("claude", "codex"):
            home = self.host_home(host)
            sibling_path = home / "harness/install-manifest.json"
            sibling = json.loads(sibling_path.read_text())
            sibling["hooks"] = []
            sibling_path.write_text(json.dumps(sibling))
            before = tree_hash(home)
            refused = self.install("--host", host, expected=1)
            self.assertIn("refusing to overwrite", refused.stderr)
            self.assertEqual(tree_hash(home), before)


class SharedRoleDeadlock(UpgradeBase):
    """Decision 4: a same-version sibling's claim on unchanged roles no longer blocks the update."""

    roles, checkout_role, role_bytes = SharedRoleSkew.roles, SharedRoleSkew.checkout_role, SharedRoleSkew.role_bytes

    def setUp(self):
        super().setUp()
        self.run_installer(self.old_tree("HARNESS_V02"), "--host", "both")
        self.install("--host", "both")  # Router claims the 0.2-era roles Harness 0.2 wrote.
        self.old_roles = {host: self.role_bytes(host) for host, *_ in HOSTS}
        for host, *_ in HOSTS:
            self.assertTrue(all(data != self.checkout_role(host, relative)
                                for relative, data in self.old_roles[host].items()), "fixture roles are 0.3 already")

    def sibling(self, host, **changes):
        path = self.host_home(host) / "harness/install-manifest.json"
        value = json.loads(path.read_text())
        value.update(changes)
        path.write_text(json.dumps(value))
        return value

    def installed(self, notices=None):
        lines = ""
        for host, *_ in HOSTS:
            home = self.host_home(host)
            lines += (notices or {}).get(host, "")
            lines += (f"Router installed for {host} in {home}\n"
                      f"Add {home / 'router/bin'} to PATH. Run router status to inspect routing.\n")
        codex = self.host_home("codex")
        return lines + (f"Roles are installed in the global agents directory: {codex / 'agents'}\n"
                        "Alternatively, register a role file with -c agents.<name>.config_file=...\n"
                        "As of Codex CLI 0.156, project-level .codex/agents/ may not load under codex exec.\n"
                        "Trust the new or changed hooks in Codex before using router. Codex prompts for hook trust on startup.\n")

    def test_same_version_sibling_lets_the_rerun_write_the_roles(self):
        for host, *_ in HOSTS:
            self.sibling(host, package=(ROOT / "VERSION").read_text().strip())
        result = self.install("--host", "both")
        self.assertEqual(result.stdout, self.installed())
        for host, *_ in HOSTS:
            claimed = self.manifest(host)["files"]
            for relative, data in self.role_bytes(host).items():
                wanted = self.checkout_role(host, relative)
                self.assertEqual(data, wanted, relative)
                self.assertEqual(claimed[relative], {"sha256": hashlib.sha256(wanted).hexdigest()}, relative)

    def test_older_sibling_or_edited_role_keeps_the_notice(self):
        old = "shared roles come from Harness 0.2.0 or earlier; upgrade it for the 0.3 roles\n"
        result = self.install("--host", "both")
        self.assertEqual(result.stdout, self.installed({host: old for host, *_ in HOSTS}))
        for host, *_ in HOSTS:
            self.assertEqual(self.role_bytes(host), self.old_roles[host])
        edited = {}
        for host, *_ in HOSTS:
            self.sibling(host, package="0.3.0")
            edited[host] = self.roles(host)[0]
            path = self.host_home(host) / edited[host]
            path.chmod(0o644)  # Harness 0.2 copied the read-only mode of its release tree.
            path.write_bytes(path.read_bytes() + b"\n# mine\n")
        result = self.install("--host", "both")
        notice = "shared roles come from Harness 0.3.0; upgrade it for the 0.3 roles\n"
        self.assertEqual(result.stdout, self.installed({host: notice for host, *_ in HOSTS}))
        for host, *_ in HOSTS:
            for relative, data in self.role_bytes(host).items():
                expected = (self.old_roles[host][relative] + b"\n# mine\n" if relative == edited[host] else
                            self.checkout_role(host, relative))
                self.assertEqual(data, expected, relative)

    def test_stale_record_leaves_no_stray_once_the_sibling_is_gone(self):
        for host, *_ in HOSTS:
            # The sibling took the update under the same rule, then left: Router's record is stale.
            for relative in self.roles(host):
                (self.host_home(host) / relative).chmod(0o644)
                (self.host_home(host) / relative).write_bytes(self.checkout_role(host, relative))
            (self.host_home(host) / "harness/install-manifest.json").unlink()
        before = {host: set(self.host_home(host).glob(filename + ".router-backup-*")) for host, filename, *_ in HOSTS}
        result = self.install("--host", "both", "--uninstall")
        expected = ""
        for host, filename, *_ in HOSTS:
            new = sorted(set(self.host_home(host).glob(filename + ".router-backup-*")) - before[host])
            expected += "".join(f"Backed up settings: {path}\n" for path in new)
            expected += "Router uninstalled. Settings backups, modified files and run state are preserved.\n"
            for relative in self.roles(host):
                self.assertFalse((self.host_home(host) / relative).exists(), f"stray shared role: {relative}")
        self.assertEqual(result.stdout, expected)


def entries(path, skip=()):
    """Every entry below path with its mode and bytes or link target, minus the skipped paths."""
    found = {}
    for base, folders, files in os.walk(path):
        for name in folders + files:
            item = Path(base) / name
            if item in skip:
                continue
            info = item.lstat()
            found[str(item.relative_to(path))] = (info.st_mode, os.readlink(item) if item.is_symlink() else
                                                  item.read_bytes() if item.is_file() else None)
    return found


class Purge(UpgradeBase):
    RETIRED = ("attempts.sqlite3", "attempts.sqlite3-wal", "attempts.sqlite3-shm", "attempts.sqlite3-journal")

    def seed(self):
        """Install over user files so settings, hooks and defaults backups exist, plus 0.2 state."""
        for host, filename, *_ in HOSTS:
            home = self.host_home(host)
            home.mkdir()
            (home / filename).write_text(json.dumps({"keep": "me"}))
            (home / ("CLAUDE.md" if host == "claude" else "AGENTS.md")).write_text("# mine\n")
        self.install("--host", "both", "--with-defaults")
        backups = sorted(path for host, *_ in HOSTS for path in self.host_home(host).glob("*.router-backup-*"))
        names = sorted(path.name.split(".router-backup-")[0] for path in backups)
        self.assertEqual(names, ["AGENTS.md", "CLAUDE.md", "hooks.json", "settings.json"], "fixture lacks a backup")
        self.state = self.home / ".local/state/claude-router"
        self.state.mkdir(parents=True, exist_ok=True)
        retired = [self.state / name for name in self.RETIRED[:3]]
        for path in retired + [self.state / "ledger.sqlite3"]:
            path.write_bytes(b"state " + path.name.encode())
        return backups, retired

    def decoys(self):
        claude = self.host_home("claude")
        outside = self.home / "outside.txt"
        outside.write_text("not a backup\n")
        (claude / "settings.json.router-backup-1").symlink_to(outside)
        (claude / "folder.router-backup-2").mkdir()
        (claude / "folder.router-backup-2/settings.json.router-backup-3").write_text("{}")
        (claude / "sub").mkdir()
        (claude / "sub/settings.json.router-backup-4").write_text("{}")
        (claude / "settings.json.harness-backup-5").write_text("{}")
        (self.state / self.RETIRED[3]).symlink_to(outside)

    def test_purge_alone_removes_only_backups_and_retired_state(self):
        backups, retired = self.seed()
        self.decoys()
        before = entries(self.home, skip=set(backups + retired))
        result = self.install("--host", "both", "--purge")
        for path in backups:
            self.assertFalse(path.exists(), f"backup survived: {path}")
            self.assertEqual(result.stdout.count(f"Removed backup: {path}\n"), 1, result.stdout)
        for path in retired:
            self.assertFalse(path.exists(), f"retired state survived: {path}")
            self.assertEqual(result.stdout.count(f"Removed retired state: {path}\n"), 1, result.stdout)
        self.assertTrue(result.stdout.endswith("Purged 4 backup(s)\n"), result.stdout)
        self.assertNotIn("Router installed", result.stdout)
        self.assertEqual(entries(self.home), before, "purge changed something besides backups and retired state")
        self.assertEqual((self.state / "ledger.sqlite3").read_bytes(), b"state ledger.sqlite3")

    def test_uninstall_purge_leaves_no_backup(self):
        backups, retired = self.seed()
        result = self.install("--host", "both", "--uninstall", "--purge")
        self.assertLess(result.stdout.rindex("Router uninstalled"), result.stdout.index("Removed backup:"),
                        "purge must run after the uninstall")
        for host, filename, *_ in HOSTS:
            home = self.host_home(host)
            with self.subTest(host=host):
                self.assertEqual(sorted(home.glob("*.router-backup-*")), [])
                self.assertEqual((home / filename).read_text(), json.dumps({"keep": "me"}),
                                 "exact restore must run before its backup is purged")
                self.assertEqual((home / ("CLAUDE.md" if host == "claude" else "AGENTS.md")).read_text(), "# mine\n")
        self.assertFalse(any(path.exists() for path in retired))
        self.assertTrue((self.state / "ledger.sqlite3").is_file(), "purge removed the ledger")

    def test_dry_run_purge_removes_nothing(self):
        backups, retired = self.seed()
        before = tree_hash(self.home)
        for options in (("--purge",), ("--purge", "--uninstall")):
            result = self.install("--host", "both", "--dry-run", *options)
            self.assertEqual(tree_hash(self.home), before, f"dry-run {options} changed files")
            for path in backups:
                self.assertEqual(result.stdout.count(f"Would remove backup: {path}\n"), 1, result.stdout)
            for path in retired:
                self.assertEqual(result.stdout.count(f"Would remove retired state: {path}\n"), 1, result.stdout)
            self.assertNotIn("Removed", result.stdout)

    def test_purge_then_uninstall_falls_back_to_surgical_removal(self):
        self.seed()
        self.install("--host", "both", "--purge")
        result = self.install("--host", "both", "--uninstall")
        self.assertIn("Router uninstalled", result.stdout)
        for host, filename, *_ in HOSTS:
            config = json.loads((self.host_home(host) / filename).read_text())
            self.assertEqual(config.get("keep"), "me")
            self.assertNotIn("hooks/router", json.dumps(config))


class BackupModes(UpgradeBase):
    def seed(self):
        originals = {}
        for host, filename, *_ in HOSTS:
            home = self.host_home(host)
            home.mkdir()
            for name, data in ((filename, b'{\n\t"z": 2,\n\t"a": 1\n}'),
                               ("CLAUDE.md" if host == "claude" else "AGENTS.md", b"# mine\n")):
                (home / name).write_bytes(data)
                (home / name).chmod(0o644)
            originals[host] = (home / filename).read_bytes()
        return originals

    def backups(self):
        return [path for host, *_ in HOSTS for path in self.host_home(host).glob("*.router-backup-*")]

    def assert_private(self, paths):
        for path in paths:
            self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o600), f"backup is not 0600: {path}")

    def test_every_backup_is_0600_and_exact_restore_keeps_the_original_mode(self):
        originals = self.seed()
        self.install("--host", "both", "--with-defaults")
        self.assertEqual(len(self.backups()), 4, "expected settings, hooks and two defaults backups")
        self.assert_private(self.backups())
        self.install("--host", "both", "--uninstall")
        for host, filename, *_ in HOSTS:
            path = self.host_home(host) / filename
            with self.subTest(host=host):
                self.assertEqual(path.read_bytes(), originals[host], "exact-bytes restore failed")
                self.assertEqual(oct(path.stat().st_mode & 0o777), oct(0o644), "restore took the backup's mode")

    def test_uninstall_backup_of_an_edited_file_is_0600(self):
        self.seed()
        self.install("--host", "both")
        first = set(self.backups())
        for host, filename, *_ in HOSTS:
            path = self.host_home(host) / filename
            config = json.loads(path.read_text())
            config["later"] = True
            path.write_text(json.dumps(config))
        self.install("--host", "both", "--uninstall")
        later = set(self.backups()) - first
        self.assertEqual(len(later), 2, "surgical uninstall should back up each edited file")
        self.assert_private(later)

    def check_old_original(self, name):
        original = json.dumps({"keep": "me"}).encode()
        for host, filename, *_ in HOSTS:
            self.host_home(host).mkdir()
            (self.host_home(host) / filename).write_bytes(original)
            (self.host_home(host) / filename).chmod(0o644)
        self.run_installer(self.old_tree(name), "--host", "both")
        old = {host: sorted(self.host_home(host).glob(filename + ".router-backup-*")) for host, filename, *_ in HOSTS}
        for _ in range(2):  # Recorded on the first 0.3 run only; the second run keeps it.
            self.install("--host", "both")
            for host, *_ in HOSTS:
                manifest = self.manifest(host)
                self.assertEqual(len(old[host]), 1, f"{name} {host}")
                self.assertEqual([manifest.get(key) for key in ("settings_original_existed", "settings_original_backup",
                                                                 "settings_original_sha256", "settings_original_mode")],
                                 [True, old[host][0].name, hashlib.sha256(original).hexdigest(), 0o644], f"{name} {host}")
        self.install("--host", "both", "--uninstall")
        for host, filename, *_ in HOSTS:
            path = self.host_home(host) / filename
            self.assertEqual((path.read_bytes(), oct(path.stat().st_mode & 0o777)), (original, oct(0o644)), f"{name} {host}")

    def test_upgrade_over_v01_manifest_records_its_backup(self):
        self.check_old_original("ROUTER_V01")

    def test_upgrade_over_v02_manifest_records_its_backup(self):
        self.check_old_original("ROUTER_V02")

    def test_uninstall_removes_no_directory_outside_created_dirs(self):
        for host, *_ in HOSTS:
            for folder in ("agents", "hooks", "skills", "router", "user/empty"):
                (self.host_home(host) / folder).mkdir(parents=True)
        before = {host: directories(self.host_home(host)) for host, *_ in HOSTS}
        self.install("--host", "both")
        during = {host: directories(self.host_home(host)) for host, *_ in HOSTS}
        created = {host: set(self.manifest(host)["created_dirs"]) for host, *_ in HOSTS}
        self.install("--host", "both", "--uninstall")
        for host, *_ in HOSTS:
            after = directories(self.host_home(host))
            with self.subTest(host=host):
                self.assertLessEqual(before[host], after, "uninstall removed a directory that existed before install")
                self.assertLessEqual(during[host] - after, created[host], "uninstall removed an unrecorded directory")
                self.assertIn("hooks/router", created[host])
                self.assertNotIn("agents", created[host])


class OldInstallFolders(UpgradeBase):
    """D-P1: a manifest with no created_dirs (0.1 or 0.2) hands on the folders its files lived in.

    Known limit: a user folder of the same name that existed empty before the old install is removed
    when both tools leave.
    """

    def test_fresh_router_beside_a_harness_v02_leaves_no_folder(self):
        before = directories(self.home)
        self.run_installer(self.old_tree("HARNESS_V02"), "--host", "both")
        self.install("--host", "both")
        self.run_installer(self.old_tree("HARNESS_V02"), "--host", "both", "--uninstall")
        self.install("--host", "both", "--uninstall")
        # Only the host homes stay, holding the preserved settings backups; no skills/ or other folder.
        self.assertEqual(directories(self.home), before | {".claude", ".codex"})

    def test_router_upgraded_from_v02_leaves_no_folder(self):
        before = directories(self.home)
        self.run_installer(self.old_tree("ROUTER_V02"), "--host", "both")
        self.install("--host", "both")
        self.install("--host", "both", "--uninstall")
        # Only the host homes stay, holding the preserved settings backups; no skills/ or other folder.
        self.assertEqual(directories(self.home), before | {".claude", ".codex"})


if __name__ == "__main__":
    unittest.main()
