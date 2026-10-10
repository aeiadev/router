#!/usr/bin/env python3
"""The shared settings rule beside a fake Harness 0.3 sibling: exact bytes and mode after every
install and uninstall order, the sibling's empty events and folders, and 0.1 and 0.2 starts.

The fake sibling installs the way Harness 0.3.0 (02a2dc2) does: its hook block, a private
.harness-backup-* file and its manifest fields. It uninstalls by the shared rule. No Harness
checkout, network or model is used. The 0.1 and 0.2 starts need ROUTER_V01 and ROUTER_V02 from
old-trees.sh; a test that needs one fails when it is unset, it never skips.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
HOSTS = (("claude", "settings.json"), ("codex", "hooks.json"))
PLAIN = b'{\n\t"z": 2,\n\t"a": 1\n}'
EMPTY_EVENT = b'{\n\t"z": 2,\n\t"hooks": {\n\t\t"SessionStart": []\n\t},\n\t"a": 1\n}'
MODE = 0o640
CODEX_NOTES = ("Roles are installed in the global agents directory: {home}/agents\n"
               "Alternatively, register a role file with -c agents.<name>.config_file=...\n"
               "As of Codex CLI 0.156, project-level .codex/agents/ may not load under codex exec.\n"
               "Trust the new or changed hooks in Codex before using router. Codex prompts for hook trust on startup.\n")
UNINSTALLED = "Router uninstalled. Settings backups, modified files and run state are preserved.\n"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canon(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def mode_of(path):
    return stat.S_IMODE(path.lstat().st_mode)


def write_private(path, data, mode=0o600):
    """Harness write_bytes: a temporary file in the folder, chmod, then replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    os.chmod(name, mode)
    os.replace(name, path)


class FakeHarness:
    """Harness as Router meets it. package=None writes a 0.2 manifest (no package, no record)."""

    FILES = {"harness/hooks/fake.py": b"print('fake harness')\n", "skills/checkpoint/SKILL.md": b"# checkpoint\n"}

    def __init__(self, home, filename, package=VERSION, record_sha=False):
        self.home, self.filename, self.package, self.record_sha = home, filename, package, record_sha
        self.settings = home / filename
        self.manifest_path = home / "harness/install-manifest.json"
        self.block = {"hooks": [{"type": "command", "command": f"python3 {home}/harness/hooks/fake.py"}]}

    def backup(self):
        descriptor, name = tempfile.mkstemp(prefix=f"{self.filename}.harness-backup-", dir=self.home)
        os.close(descriptor)
        os.chmod(name, 0o600)
        shutil.copyfile(self.settings, name)
        os.chmod(name, 0o600)
        return Path(name).name

    def install(self):
        manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {}
        first = not manifest
        original_bytes = self.settings.read_bytes() if self.settings.exists() else None
        original = json.loads(original_bytes) if original_bytes is not None else {}
        settings = copy.deepcopy(original)
        empty = manifest.get("existing_empty_events",
                             [event for event, groups in original.get("hooks", {}).items() if groups == []])
        created = list(manifest.get("created_dirs", []))
        for relative in [*self.FILES, self.filename, "harness/install-manifest.json"]:
            parts = Path(relative).parts
            for depth in range(1, len(parts)):
                folder = str(Path(*parts[:depth]))
                if not (self.home / folder).exists() and folder not in created:
                    created.append(folder)
        owned = copy.deepcopy(manifest.get("hooks", {}))
        hooks = settings.setdefault("hooks", {})
        for event in ("SessionStart", "PreToolUse"):
            current = hooks.setdefault(event, [])
            if self.block not in current:
                current.append(copy.deepcopy(self.block))
                owned.setdefault(event, []).append(copy.deepcopy(self.block))
        router = self.home / "router/install-manifest.json"
        router_value = json.loads(router.read_text()) if router.is_file() else {}
        had_hooks = manifest.get("had_hooks", True) if manifest else (
            "hooks" in original and router_value.get("had_hooks") is not False)
        result = {"version": 1, "files": {relative: digest(data) for relative, data in self.FILES.items()},
                  "hooks": owned, "had_hooks": had_hooks, "existing_empty_events": empty, "statusline": None}
        inherited = False
        if self.package is not None:
            result.update(package=self.package, installed_at=datetime.now(timezone.utc).isoformat(),
                          source="/fake/harness")
            new = canon(settings) if settings != original else None
            # The twin rule (lane 3H11): Router's verified record first, else the current bytes.
            name = router_value.get("settings_original_backup")
            theirs = self.home / name if isinstance(name, str) else None
            inherited = first and "settings_original_sha256" in router_value and (
                router_value.get("settings_original_existed") in (False, None) or theirs is not None and theirs.is_file()
                and digest(theirs.read_bytes()) == router_value["settings_original_sha256"])
            if inherited:
                result["settings_original_existed"] = router_value["settings_original_existed"]
                if theirs is not None and router_value["settings_original_existed"]:  # None: unknown
                    descriptor, copied = tempfile.mkstemp(prefix=f"{self.filename}.harness-backup-", dir=self.home)
                    os.close(descriptor)
                    Path(copied).write_bytes(theirs.read_bytes())
                    os.chmod(copied, 0o600)
                    result.update(settings_original_backup=Path(copied).name,
                                  settings_original_sha256=router_value["settings_original_sha256"],
                                  settings_original_mode=router_value["settings_original_mode"])
            elif first:
                result["settings_original_existed"] = self.settings.exists()
                if self.settings.exists():
                    result["settings_original_mode"] = mode_of(self.settings)
                if self.record_sha and original_bytes is not None:
                    result["settings_original_sha256"] = digest(original_bytes)
            if first and new is not None:
                result["settings_written_sha256"] = digest(new)
            if not first:
                for key in manifest:
                    if key.startswith("settings_"):
                        result[key] = manifest[key]
            result["created_dirs"] = created
        if settings != original:
            if self.settings.exists():
                name = self.backup()
                if first and self.package is not None and not inherited:
                    result["settings_original_backup"] = name
            write_private(self.settings, canon(settings))
        for relative, data in self.FILES.items():
            (self.home / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.home / relative).write_bytes(data)
        write_private(self.manifest_path, canon(result))

    def uninstall(self):
        """The shared rule: the sibling's verified original when it equals what is left, else canon."""
        manifest = json.loads(self.manifest_path.read_text())
        original = json.loads(self.settings.read_bytes()) if self.settings.exists() else {}
        settings = copy.deepcopy(original)
        for event, groups in manifest["hooks"].items():
            current = settings.get("hooks", {}).get(event, [])
            removed = [group for group in groups if group in current]
            for group in removed:
                current.remove(group)
            if removed and not current and event not in manifest.get("existing_empty_events", []):
                settings["hooks"].pop(event)
        if not settings.get("hooks") and not manifest.get("had_hooks", True):
            settings.pop("hooks", None)
        name = manifest.get("settings_original_backup")
        backup = self.home / name if name else None
        recorded = manifest.get("settings_original_sha256")
        verified = (backup is not None and backup.is_file() and
                    (recorded is None or digest(backup.read_bytes()) == recorded))
        if verified and canon(json.loads(backup.read_bytes())) == canon(settings):
            write_private(self.settings, backup.read_bytes(), manifest.get("settings_original_mode", 0o600))
        elif settings in ({}, {"hooks": {}}) and manifest.get("settings_original_existed") is not True:
            self.settings.unlink(missing_ok=True)
        elif settings != original:
            if self.settings.exists():
                self.backup()
            write_private(self.settings, canon(settings))
        for relative, expected in manifest["files"].items():
            path = self.home / relative
            if path.is_file() and digest(path.read_bytes()) == expected:
                path.unlink()
        self.manifest_path.unlink()
        for relative in sorted(set(manifest.get("created_dirs", [])), key=lambda item: len(Path(item).parts),
                               reverse=True):
            try:
                (self.home / relative).rmdir()
            except OSError:
                pass


class RuleBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.path = self.base / "commands"
        self.path.mkdir()
        for command in ("python3", "git", "dirname"):
            (self.path / command).symlink_to(shutil.which(command))
        self.homes = {"claude": self.base / "home/.claude", "codex": self.base / "home/.codex"}
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ROUTER_", "HARNESS_", "CLAUDE_", "CODEX_", "XDG_"))
                    and key != "ROUTES_JSON"}
        self.env.update(HOME=str(self.base / "home"), PATH=str(self.path),
                        CLAUDE_HOME=str(self.homes["claude"]), CODEX_HOME=str(self.homes["codex"]),
                        XDG_CONFIG_HOME=str(self.base / "config"), XDG_STATE_HOME=str(self.base / "state"),
                        ROUTER_LOCAL="off", PYTHONDONTWRITEBYTECODE="1")
        self.harness = {host: FakeHarness(self.homes[host], filename) for host, filename in HOSTS}

    def settings(self, host):
        return self.homes[host] / dict(HOSTS)[host]

    def seed(self, data=PLAIN, mode=MODE):
        for host, _ in HOSTS:
            self.homes[host].mkdir(parents=True, exist_ok=True)
            self.settings(host).write_bytes(data)
            self.settings(host).chmod(mode)

    def backups(self):
        return {host: set(self.homes[host].glob(dict(HOSTS)[host] + ".router-backup-*")) for host, _ in HOSTS}

    def router(self, *options, tree=ROOT):
        result = subprocess.run([BASH, str(tree / "install.sh"), "--host", "both", *options], env=self.env,
                                text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def manifest(self, host):
        return json.loads((self.homes[host] / "router/install-manifest.json").read_text())

    def installed(self, host, before=None, original=None, private=(), removed=()):
        """Router's exact install output for one host; returns it with the new backups, oldest first."""
        new = sorted(self.backups()[host] - (before or set()))
        lines = [f"Removed no-longer-shipped: {self.homes[host] / relative}\n" for relative in removed]
        lines += [f"Backed up original settings: {original}\n"] if original else []
        lines += [f"Backed up settings: {path}\n" for path in new if path != original]
        lines += [f"Made backup private (0600): {path}\n" for path in private]
        home = self.homes[host]
        lines += [f"Router installed for {host} in {home}\n",
                  f"Add {home}/router/bin to PATH. Run router status to inspect routing.\n"]
        if host == "codex":
            lines.append(CODEX_NOTES.format(home=home))
        return "".join(lines), new

    def assert_install(self, stdout, before=None, originals=None, private=None, removed=None):
        expected = "".join(self.installed(host, before and before[host], (originals or {}).get(host),
                                          (private or {}).get(host, ()), (removed or {}).get(host, ()))[0]
                           for host, _ in HOSTS)
        self.assertEqual(stdout, expected)

    def assert_record(self, host, existed, data, mode, backup=None):
        manifest = self.manifest(host)
        record = {key: manifest.get(key) for key in ("settings_original_existed", "settings_original_sha256",
                                                      "settings_original_mode")}
        self.assertEqual(record, {"settings_original_existed": existed,
                                  "settings_original_sha256": None if data is None else digest(data),
                                  "settings_original_mode": mode}, host)
        name = manifest.get("settings_original_backup")
        if data is None:
            self.assertIsNone(name, host)
            return None
        path = self.homes[host] / name
        self.assertEqual((path.read_bytes(), mode_of(path)), (data, 0o600), host)
        if backup is not None:
            self.assertEqual(path, backup, host)
        return path

    def assert_original(self, data, mode):
        for host, _ in HOSTS:
            path = self.settings(host)
            if data is None:
                self.assertFalse(path.exists() or path.is_symlink(), f"{host}: settings left behind")
            else:
                self.assertEqual((path.read_bytes(), oct(mode_of(path))), (data, oct(mode)), host)

    def folders(self):
        return {host: sorted(str(Path(base).relative_to(home)) for base, _, _ in os.walk(home))
                for host, home in self.homes.items()}

    def uninstall(self, order):
        out = None
        for product in order:
            if product == "router":
                out = self.router("--uninstall")
            else:
                for fake in self.harness.values():
                    fake.uninstall()
        return out

    def old_tree(self, name):
        value = os.environ.get(name)
        self.assertTrue(value, f"{name} is unset; source old-trees.sh before running this test")
        tree = Path(value)
        self.assertTrue((tree / "install.sh").is_file(), f"{name} has no install.sh: {tree}")
        return tree


class RecordTheOriginal(RuleBase):
    """Fix A1, record: the first source that exists, on the first 0.3 run only."""

    def test_alone_records_the_current_bytes(self):
        self.seed()
        out = self.router()
        self.assert_install(out)
        for host, _ in HOSTS:
            self.assert_record(host, True, PLAIN, MODE)
        again = self.router()
        self.assert_install(again, before=self.backups())
        self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
        self.assert_original(PLAIN, MODE)

    def test_sibling_record_is_copied_and_restored(self):
        for record_sha in (False, True):
            with self.subTest(record_sha=record_sha):
                self.setUp()
                self.harness = {host: FakeHarness(self.homes[host], name, record_sha=record_sha)
                                for host, name in HOSTS}
                self.seed()
                for fake in self.harness.values():
                    fake.install()
                written = {host: self.settings(host).read_bytes() for host, _ in HOSTS}
                out = self.router()
                expected = []
                for host, _ in HOSTS:
                    new = sorted(self.backups()[host])
                    self.assertEqual(len(new), 2, host)
                    self.assertEqual([path.read_bytes() for path in new], [PLAIN, written[host]], host)
                    self.assert_record(host, True, PLAIN, MODE, backup=new[0])
                    expected.append(self.installed(host, original=new[0])[0])
                self.assertEqual(out, "".join(expected))
                self.uninstall(("harness",))
                self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
                self.assert_original(PLAIN, MODE)

    def test_sibling_backup_failing_its_hash_is_not_taken(self):
        self.harness = {host: FakeHarness(self.homes[host], name, record_sha=True) for host, name in HOSTS}
        self.seed()
        for host, fake in self.harness.items():
            fake.install()
            backup = self.homes[host] / json.loads(fake.manifest_path.read_text())["settings_original_backup"]
            backup.write_bytes(b'{"tampered": 1}')
        current = {host: self.settings(host).read_bytes() for host, _ in HOSTS}
        self.assert_install(self.router())
        for host, _ in HOSTS:
            self.assert_record(host, True, current[host], 0o600)

    def unreadable(self, path):
        """Mode 000, put back in cleanup so the temporary folder can go."""
        if os.geteuid() == 0:
            self.skipTest("root reads mode 000 files")
        self.addCleanup(os.chmod, path, 0o600)
        os.chmod(path, 0)

    def check_current_is_recorded(self, before):
        current = {host: self.settings(host).read_bytes() for host, _ in HOSTS}
        modes = {host: mode_of(self.settings(host)) for host, _ in HOSTS}
        out = self.router()
        expected = "".join(self.installed(host, before=before and before[host])[0] for host, _ in HOSTS)
        self.assertEqual(out, expected)
        for host, _ in HOSTS:
            self.assert_record(host, True, current[host], modes[host])

    def test_unreadable_sibling_backup_is_no_source(self):
        for record_sha in (False, True):
            with self.subTest(record_sha=record_sha):
                self.setUp()
                self.harness = {host: FakeHarness(self.homes[host], name, record_sha=record_sha)
                                for host, name in HOSTS}
                self.seed()
                for host, fake in self.harness.items():
                    fake.install()
                    self.unreadable(self.homes[host] / json.loads(fake.manifest_path.read_text())[
                        "settings_original_backup"])
                self.check_current_is_recorded(None)

    def test_unreadable_router_v02_backup_is_no_source(self):
        self.seed()
        self.router(tree=self.old_tree("ROUTER_V02"))
        old = self.backups()
        for host, _ in HOSTS:
            self.unreadable(next(iter(old[host])))
        current = {host: self.settings(host).read_bytes() for host, _ in HOSTS}
        modes = {host: mode_of(self.settings(host)) for host, _ in HOSTS}
        out = self.router()
        for host, _ in HOSTS:
            self.assertTrue((self.homes[host] / "router/install-manifest.json").is_file(), host)
            self.assertIn(f"Router installed for {host} in {self.homes[host]}\n", out)
            self.assert_record(host, True, current[host], modes[host])

    def test_original_mode_is_restored_without_group_or_other_write(self):
        for recorded, restored in ((0o777, 0o755), (0o600, 0o600), (0o644, 0o644)):
            with self.subTest(recorded=oct(recorded)):
                self.setUp()
                self.seed(mode=recorded)
                for fake in self.harness.values():
                    fake.install()
                out = self.router()
                expected = []
                for host, _ in HOSTS:
                    new = sorted(self.backups()[host])
                    self.assert_record(host, True, PLAIN, restored, backup=new[0])
                    expected.append(self.installed(host, original=new[0])[0])
                self.assertEqual(out, "".join(expected))
                self.uninstall(("harness",))
                self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
                self.assert_original(PLAIN, restored)

    def test_unreadable_backup_at_uninstall_is_not_verified(self):
        self.seed()
        self.assert_install(self.router())
        backups = {}
        for host, _ in HOSTS:
            backups[host] = self.assert_record(host, True, PLAIN, MODE)
            self.unreadable(backups[host])
        before = self.backups()
        installed = {host: self.settings(host).read_bytes() for host, _ in HOSTS}
        out = self.router("--uninstall")
        expected = ""
        for host, _ in HOSTS:
            new = sorted(self.backups()[host] - before[host])
            self.assertEqual(len(new), 1, host)
            self.assertEqual((new[0].read_bytes(), mode_of(new[0])), (installed[host], 0o600), host)
            expected += f"Backed up settings: {new[0]}\n" + UNINSTALLED
            self.assertEqual((self.settings(host).read_bytes(), mode_of(self.settings(host))),
                             (canon(json.loads(PLAIN)), MODE), host)
            self.assertFalse((self.homes[host] / "router/install-manifest.json").exists(), host)
        self.assertEqual(out, expected)

    def test_no_record_at_uninstall_derives_from_what_the_old_release_recorded(self):
        # R-A: a 0.2 install uninstalled by 0.3 with no upgrade run.
        self.seed(b'{"hooks":{}}')
        self.router(tree=self.old_tree("ROUTER_V02"))
        old = self.backups()
        for host, _ in HOSTS:
            self.assertNotIn("settings_original_existed", self.manifest(host))
            self.assertTrue(self.manifest(host)["had_hooks"], host)
        out = self.router("--uninstall")
        for host, _ in HOSTS:
            # The oldest backup holds exactly what is left: its bytes and mode come back.
            self.assertEqual((self.settings(host).read_bytes(), mode_of(self.settings(host))),
                             (b'{"hooks":{}}', MODE), host)
        self.assertEqual(out, UNINSTALLED * 2)
        # Without that backup the file still existed (had_hooks true): kept as canon.
        self.setUp()
        self.seed(b'{"hooks":{}}')
        self.router(tree=self.old_tree("ROUTER_V02"))
        for host, _ in HOSTS:
            for path in self.backups()[host]:
                path.unlink()
        out = self.router("--uninstall")
        for host, _ in HOSTS:
            new = sorted(self.backups()[host])
            self.assertEqual(len(new), 1, host)
            self.assertEqual((self.settings(host).read_bytes(), mode_of(self.settings(host))),
                             (canon({"hooks": {}}), MODE), host)
        self.assertEqual(out, "".join(f"Backed up settings: {sorted(self.backups()[host])[0]}\n" + UNINSTALLED
                                      for host, _ in HOSTS))
        # A fresh home: nothing says the file existed, so it is unknown and goes.
        self.setUp()
        self.router(tree=self.old_tree("ROUTER_V02"))
        self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
        self.assert_original(None, None)

    def test_sibling_unknown_original_is_inherited(self):
        # R-B: Router 0.2 on a fresh home, upgraded to 0.3, Harness 0.3 over it, Router then Harness out.
        self.router(tree=self.old_tree("ROUTER_V02"))
        self.router()
        for host, _ in HOSTS:
            self.assert_record(host, None, None, None)
        for fake in self.harness.values():
            fake.install()
        for host, fake in self.harness.items():
            self.assertIn("settings_original_existed", json.loads(fake.manifest_path.read_text()))
        # Router 0.3 meets a Harness 0.3 that recorded "unknown": it inherits that.
        self.setUp()
        for fake in self.harness.values():
            fake.install()
            record = json.loads(fake.manifest_path.read_text())
            record["settings_original_existed"] = None
            record.pop("settings_original_mode", None)
            record["settings_original_sha256"] = None
            write_private(fake.manifest_path, canon(record))
        self.router()
        for host, _ in HOSTS:
            self.assert_record(host, None, None, None)
        self.assertEqual(self.router("--uninstall"), "".join(
            f"Backed up settings: {sorted(self.backups()[host])[-1]}\n" + UNINSTALLED for host, _ in HOSTS))
        self.uninstall(("harness",))
        self.assert_original(None, None)

    def test_had_hooks_missing_from_a_sibling_manifest_comes_from_its_backup(self):
        # R-C: a sibling manifest without had_hooks, which no released Harness writes (Harness 0.1 does
        # record it). The branch is kept for parity with the rulings; this does not reproduce a real
        # upgrade. The sibling's backup shows the original had no hooks key.
        self.harness = {host: FakeHarness(self.homes[host], name, package=None) for host, name in HOSTS}
        self.seed()
        for host, fake in self.harness.items():
            fake.install()
            record = json.loads(fake.manifest_path.read_text())
            record.pop("had_hooks"), record.pop("existing_empty_events")
            write_private(fake.manifest_path, canon(record))
        self.router()
        for host, _ in HOSTS:
            self.assertIs(self.manifest(host)["had_hooks"], False, host)
        self.uninstall(("harness",))
        for host, _ in HOSTS:  # A user edit, so the exact original bytes are not what comes back.
            value = json.loads(self.settings(host).read_bytes())
            value["extra"] = 3
            self.settings(host).write_bytes(canon(value))
        before = self.backups()
        out = self.router("--uninstall")
        expected = ""
        for host, _ in HOSTS:
            new = sorted(self.backups()[host] - before[host])
            self.assertEqual(len(new), 1, host)
            expected += f"Backed up settings: {new[0]}\n" + UNINSTALLED
            self.assertEqual((self.settings(host).read_bytes(), mode_of(self.settings(host))),
                             (canon({"z": 2, "a": 1, "extra": 3}), 0o600), host)  # The mode Harness left.
        self.assertEqual(out, expected)

    def test_sibling_that_created_the_file_records_it_absent(self):
        for fake in self.harness.values():
            fake.install()
        self.assert_install(self.router())
        for host, _ in HOSTS:
            self.assert_record(host, False, None, None)
        self.uninstall(("harness",))
        self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
        self.assert_original(None, None)

    def check_old_router_start(self, name):
        self.seed()
        self.router(tree=self.old_tree(name))
        old = self.backups()
        old_files = {host: set(self.manifest(host)["files"]) for host, _ in HOSTS}
        for host, _ in HOSTS:
            self.assertEqual(len(old[host]), 1, f"{name} left no settings backup for {host}")
            self.assertEqual(mode_of(next(iter(old[host]))), MODE, f"{name} backup lost the settings mode")
        out = self.router()
        removed = {host: sorted(old_files[host] - set(self.manifest(host)["files"])) for host, _ in HOSTS}
        self.assert_install(out, before=old, private={host: sorted(old[host]) for host, _ in HOSTS}, removed=removed)
        for host, _ in HOSTS:
            self.assert_record(host, True, PLAIN, MODE, backup=next(iter(old[host])))
        self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
        self.assert_original(PLAIN, MODE)

    def test_router_v01_start_records_its_backup(self):
        self.check_old_router_start("ROUTER_V01")

    def test_router_v02_start_records_its_backup(self):
        self.check_old_router_start("ROUTER_V02")

    def test_router_v02_start_without_backup_is_unknown_and_removed(self):
        self.router(tree=self.old_tree("ROUTER_V02"))
        self.assertEqual(self.backups(), {"claude": set(), "codex": set()})
        self.assert_install(self.router(), removed={host: ["router/bin/dispatch" + "-log.py"] for host, _ in HOSTS})
        for host, _ in HOSTS:
            self.assert_record(host, None, None, None)
        self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
        self.assert_original(None, None)

    def test_harness_v02_start_takes_its_earliest_backup_by_mtime(self):
        self.harness = {host: FakeHarness(self.homes[host], name, package=None) for host, name in HOSTS}
        self.seed()
        for host, fake in self.harness.items():
            fake.install()
            ours = sorted(self.homes[host].glob(dict(HOSTS)[host] + ".harness-backup-*"))
            self.assertEqual(len(ours), 1, host)
            early, late = self.homes[host] / f"{fake.filename}.harness-backup-zzzz", \
                self.homes[host] / f"{fake.filename}.harness-backup-aaaa"
            ours[0].rename(early)
            late.write_bytes(b'{"later": 1}')
            late.chmod(0o600)
            os.utime(early, ns=(1_000_000_000, 1_000_000_000))
            os.utime(late, ns=(2_000_000_000, 2_000_000_000))
        out = self.router()
        expected = []
        for host, _ in HOSTS:
            new = sorted(self.backups()[host])
            self.assertEqual(len(new), 2, host)
            self.assert_record(host, True, PLAIN, 0o600, backup=new[0])
            expected.append(self.installed(host, original=new[0])[0])
        self.assertEqual(out, "".join(expected))


class UninstallWithoutRecord(RuleBase):
    """R-A signals and the R-B mirror (lane 3R20, round 2)."""

    def unreadable(self, path):
        """Mode 000, put back in cleanup so the temporary folder can go."""
        if os.geteuid() == 0:
            self.skipTest("root reads mode 000 files")
        self.addCleanup(os.chmod, path, 0o600)
        os.chmod(path, 0)

    def test_router_v01_uninstalled_directly_gives_back_the_original(self):
        # D3 at uninstall: a 0.1 manifest has no had_hooks; its backup shows whether the key was there.
        for original in (b"{}", PLAIN, b'{"a": 1,\n"hooks": {}}'):
            with self.subTest(original=original):
                self.setUp()
                self.seed(original)
                self.router(tree=self.old_tree("ROUTER_V01"))
                for host, _ in HOSTS:
                    self.assertNotIn("had_hooks", self.manifest(host), host)
                self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
                self.assert_original(original, MODE)

    def test_unreadable_legacy_backup_still_means_the_file_existed(self):
        # R-A signal "a legacy backup exists": Router 0.2 over {} recorded no hooks key and no empty events.
        if os.geteuid() == 0:
            self.skipTest("root reads mode 000 files")
        self.seed(b"{}\n")
        self.router(tree=self.old_tree("ROUTER_V02"))
        for host, _ in HOSTS:
            self.assertNotIn("settings_original_existed", self.manifest(host))
            self.assertEqual((self.manifest(host)["had_hooks"], self.manifest(host)["existing_empty_events"]),
                             (False, []), host)
            for path in self.backups()[host]:
                self.unreadable(path)
        before = self.backups()
        out = self.router("--uninstall")
        expected = ""
        for host, _ in HOSTS:
            new = sorted(self.backups()[host] - before[host])
            self.assertEqual(len(new), 1, host)
            expected += f"Backed up settings: {new[0]}\n" + UNINSTALLED
        self.assertEqual(out, expected)
        self.assert_original(b"{}\n", MODE)

    def test_recorded_empty_events_alone_mean_the_file_existed(self):
        # R-A signal existing_empty_events. No released Router writes it without had_hooks true; this
        # covers a hand-edited or damaged manifest, with no backup left.
        self.seed(b"{}\n")
        self.router(tree=self.old_tree("ROUTER_V02"))
        for host, _ in HOSTS:
            for path in self.backups()[host]:
                path.unlink()
            path = self.homes[host] / "router/install-manifest.json"
            record = json.loads(path.read_text())
            record.update(had_hooks=False, existing_empty_events=["Notification"])
            path.write_text(json.dumps(record))
        out = self.router("--uninstall")
        expected = ""
        for host, _ in HOSTS:
            new = sorted(self.backups()[host])
            self.assertEqual(len(new), 1, host)
            expected += f"Backed up settings: {new[0]}\n" + UNINSTALLED
        self.assertEqual(out, expected)
        self.assert_original(b"{}\n", MODE)

    def test_sibling_unknown_original_with_router_uninstalled_last_leaves_no_file(self):
        # The mirror of R-B: the sibling's 0.3 record says unknown, Router installs after it and goes
        # last over {}. Rule R-B plus the unknown rule remove the file. Before lane 3R20 (c9aa25a and
        # 78b09d7) this ended with {}\n left behind.
        for fake in self.harness.values():
            fake.install()
            record = json.loads(fake.manifest_path.read_text())
            record.update(settings_original_existed=None, settings_original_sha256=None)
            for key in ("settings_original_backup", "settings_original_mode"):
                record.pop(key, None)
            write_private(fake.manifest_path, canon(record))
        self.router()
        for host, _ in HOSTS:
            self.assert_record(host, None, None, None)
        self.uninstall(("harness",))
        self.assertEqual(self.router("--uninstall"), UNINSTALLED * 2)
        self.assert_original(None, None)


class UninstallRestore(RuleBase):
    """Fix A1, uninstall: the verified original when it equals what is left, else canon or removal."""

    def check_orders(self, data, mode):
        for first, order in ((first, order) for first in ("router", "harness")
                             for order in (("harness", "router"), ("router", "harness"))):
            with self.subTest(install_first=first, uninstall=order):
                self.setUp()
                if data is not None:
                    self.seed(data, mode)
                if first == "harness":
                    for fake in self.harness.values():
                        fake.install()
                self.router()
                if first == "router":
                    for fake in self.harness.values():
                        fake.install()
                before = self.backups()
                if order[0] == "router":
                    # Harness is still in: Router leaves the sibling's block in standard layout.
                    left = {}
                    for host, _ in HOSTS:
                        value = json.loads(self.settings(host).read_bytes())
                        for event, groups in value["hooks"].items():
                            value["hooks"][event] = [group for group in groups if group == self.harness[host].block]
                        value["hooks"] = {event: groups for event, groups in value["hooks"].items() if groups}
                        left[host] = value
                out = self.uninstall(order[:1])
                if order[0] == "router":
                    lines = ""
                    for host, _ in HOSTS:
                        new = sorted(self.backups()[host] - before[host])
                        self.assertEqual(len(new), 1, host)
                        lines += f"Backed up settings: {new[0]}\n" + UNINSTALLED
                        self.assertEqual(self.settings(host).read_bytes(), canon(left[host]), host)
                    self.assertEqual(out, lines)
                    self.uninstall(order[1:])
                else:
                    self.assertEqual(self.uninstall(order[1:]), UNINSTALLED * 2)
                self.assert_original(data, mode)

    def test_both_uninstall_orders_give_back_bytes_and_mode(self):
        self.check_orders(PLAIN, MODE)

    def test_original_absent_file_is_removed_in_both_orders(self):
        self.check_orders(None, None)

    def test_user_edit_after_install_survives(self):
        self.seed()
        self.router()
        for host, _ in HOSTS:
            value = json.loads(self.settings(host).read_bytes())
            value["later_edit"] = True
            self.settings(host).write_text(json.dumps(value))
        before = self.backups()
        out = self.router("--uninstall")
        lines = ""
        for host, _ in HOSTS:
            new = sorted(self.backups()[host] - before[host])
            self.assertEqual(len(new), 1, host)
            lines += f"Backed up settings: {new[0]}\n" + UNINSTALLED
            path = self.settings(host)
            self.assertEqual((path.read_bytes(), mode_of(path)), (canon({"z": 2, "a": 1, "later_edit": True}), MODE))
        self.assertEqual(out, lines)


class SiblingFacts(RuleBase):
    """Fixes A2 and A3: the sibling's recorded empty events and created folders."""

    def test_user_empty_event_survives_router_after_harness(self):
        for order in (("harness", "router"), ("router", "harness")):
            with self.subTest(order=order):
                self.setUp()
                self.seed(EMPTY_EVENT)
                for fake in self.harness.values():
                    fake.install()
                self.router()
                for host, _ in HOSTS:
                    self.assertIn("SessionStart", self.manifest(host)["existing_empty_events"], host)
                self.uninstall(order)
                self.assert_original(EMPTY_EVENT, MODE)

    def test_folder_the_first_installer_created_goes_with_the_last(self):
        for order in (("harness", "router"), ("router", "harness")):
            for user_file in (False, True):
                with self.subTest(order=order, user_file=user_file):
                    self.setUp()
                    self.seed()
                    before = self.folders()
                    for fake in self.harness.values():
                        fake.install()
                    self.router()
                    for host, _ in HOSTS:
                        self.assertIn("skills", self.manifest(host)["created_dirs"], host)
                        if user_file:
                            (self.homes[host] / "skills/mine.md").write_bytes(b"mine\n")
                    self.uninstall(order)
                    self.assert_original(PLAIN, MODE)
                    after = self.folders()
                    for host, _ in HOSTS:
                        expected = sorted(before[host] + (["skills"] if user_file else []))
                        self.assertEqual(after[host], expected, host)
                        if user_file:
                            self.assertEqual(sorted(os.listdir(self.homes[host] / "skills")), ["mine.md"])


if __name__ == "__main__":
    unittest.main()
