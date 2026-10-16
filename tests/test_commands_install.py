"""Synthetic, isolated commands installer lifecycle checks."""
import json
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _platform import canonical_temp_root  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/commands_install.py"


class CommandsInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.normcase(str(Path(self.tmp.name).resolve())))
        self.home = self.base / "home"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home))
        for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "CLAUDE_HOME", "CODEX_HOME"):
            self.env.pop(name, None)
        if os.name == "nt":
            local = self.base / "local"
            local.mkdir()
            self.env["LOCALAPPDATA"] = str(local)
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.payload = self.base / "payload"
        self.payload.mkdir(mode=0o700)
        (self.payload / "bin").mkdir(mode=0o700)
        self.write_payload("one")

    def write_payload(self, content):
        entry = self.payload / "bin/router-commands"
        entry.write_text(content)
        entry.chmod(0o700)
        import hashlib
        (self.payload / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "source_revision": content,
            "files": {"bin/router-commands": {"sha256": hashlib.sha256(content.encode()).hexdigest(), "mode": 0o700}}
        }))
        (self.payload / "manifest.json").chmod(0o600)

    def cli(self, action, *extra, ok=True, env=None, product="router", host="claude", payload=None):
        argv = [sys.executable, "-B", str(HELPER), action, "--product", product,
                "--host", host, "--home", str(self.home), "--payload", str(payload or self.payload), *map(str, extra)]
        result = subprocess.run(argv, text=True, capture_output=True, env={**self.env, **(env or {})}, timeout=10, stdin=subprocess.DEVNULL)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def apply(self):
        plan = self.cli("plan")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        return self.cli("apply", "--plan", saved)

    @unittest.skipIf(os.name == "nt", "POSIX creation mask")
    def test_restrictive_umasks_refuse_before_lifecycle_or_lock_mutation(self):
        module = self.module()
        for upgrade in (False, True):
            if upgrade:
                self.apply()
                self.write_payload("two")
            saved = self.base / "mask-plan.json"
            saved.write_text(json.dumps(self.cli("plan")))
            # Include both missing and already valid coordination locks.
            if upgrade:
                self.cli("status")
            before = self.home_snapshot()
            locks = [Path("/tmp") / ("commands-installer-" +
                     module.digest(module.canonical_path(p).encode()) + ".lock")
                     for p in (*module.paths(self.home), module.bootstrap_path(self.home))]
            lock_before = {str(p): (p.read_bytes(), p.stat().st_mode, p.stat().st_ino)
                           if p.exists() else None for p in locks}
            for mask in (0o100, 0o200, 0o400, 0o777):
                for action in ("plan", "apply", "status", "recover", "rollback", "uninstall"):
                    with self.subTest(upgrade=upgrade, mask=oct(mask), action=action):
                        argv = [sys.executable, "-B", str(HELPER), action,
                                "--product", "router", "--host", "claude",
                                "--home", str(self.home), "--payload", str(self.payload)]
                        if action == "apply":
                            argv += ["--plan", str(saved)]
                        result = subprocess.run(argv, text=True, capture_output=True,
                                                stdin=subprocess.DEVNULL, timeout=10, umask=mask)
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("umask", result.stderr)
                        self.assertEqual(result.stdout, "")
                        self.assertEqual(self.home_snapshot(), before)
                        self.assertEqual({str(p): (p.read_bytes(), p.stat().st_mode, p.stat().st_ino)
                                          if p.exists() else None for p in locks}, lock_before)
            self.assertIs(self.cli("recover")["recovered"], False)

    @unittest.skipIf(os.name == "nt", "POSIX creation mask")
    def test_compatible_umasks_keep_private_creation_modes(self):
        for mask in (0o000, 0o022, 0o077):
            with self.subTest(mask=oct(mask)):
                saved = self.base / "mask-plan.json"
                saved.write_text(json.dumps(self.cli("plan")))
                argv = [sys.executable, "-B", str(HELPER), "apply",
                        "--product", "router", "--host", "claude",
                        "--home", str(self.home), "--payload", str(self.payload),
                        "--plan", str(saved)]
                result = subprocess.run(argv, text=True, capture_output=True,
                                        stdin=subprocess.DEVNULL, timeout=10, umask=mask)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.cli("status")
                self.cli("uninstall")

    @unittest.skipIf(os.name == "nt", "POSIX filesystem byte limits")
    def test_staging_filename_boundary_and_legacy_plan_refusal(self):
        from unittest.mock import patch
        import hashlib
        module = self.module()
        original_manifest = (self.payload / "manifest.json").read_bytes()
        limit = os.pathconf(self.payload, "PC_NAME_MAX")
        for name, accepted in (("x" * (limit - 41), True),
                               ("x" * (limit - 40), False), ("é" * (limit // 2), False)):
            entry = self.payload / name
            entry.write_bytes(b"payload")
            entry.chmod(0o600)
            manifest = json.loads(original_manifest)
            manifest["files"][name] = {"sha256": hashlib.sha256(b"payload").hexdigest(), "mode": 0o600}
            (self.payload / "manifest.json").write_text(json.dumps(manifest))
            with self.subTest(bytes=len(os.fsencode(name))):
                if accepted:
                    self.apply()
                    self.cli("uninstall")
                else:
                    # A self-consistent plan from before the path preflight.
                    with patch.object(module, "path_preflight", create=True):
                        legacy = module.plan(self.home, self.payload)
                    saved = self.base / "legacy-path-plan.json"
                    saved.write_text(json.dumps(legacy))
                    before = self.home_snapshot()
                    for action, args in (("plan", ()), ("apply", ("--plan", saved))):
                        result = self.cli(action, *args, ok=False)
                        self.assertIn("path", result.stderr)
                        self.assertEqual(self.home_snapshot(), before)
            entry.unlink()
            (self.payload / "manifest.json").write_bytes(original_manifest)

    @unittest.skipIf(os.name == "nt", "POSIX filesystem byte limits")
    def test_long_state_envelope_is_checked_before_bootstrap(self):
        from unittest.mock import patch
        module = self.module()
        limit = os.pathconf(self.home, "PC_PATH_MAX")
        # Longest envelope: /transaction/recovery_stage/bin/router-commands.writing-UUID.
        suffix = 1 + 49 + len("/recovery_stage/bin/router-commands.writing-") + 32
        for state_size in (limit - suffix - 1, limit - suffix, limit - 64, limit - 46):
            state_suffix = "/router-commands-installer/claude"
            base_size = state_size - len(state_suffix)
            raw = str(self.home / "long-state")
            while len(os.fsencode(raw)) < base_size:
                remaining = base_size - len(os.fsencode(raw))
                if remaining == 1:
                    raw += "x"
                else:
                    raw += "/" + "x" * min(180, remaining - 1)
            env = {"XDG_STATE_HOME": raw}
            with patch.dict(os.environ, env), patch.object(module, "path_preflight", create=True):
                legacy = module.plan(self.home, self.payload)
            saved = self.base / "legacy-long-plan.json"
            saved.write_text(json.dumps(legacy))
            before = self.home_snapshot()
            with self.subTest(state_bytes=state_size):
                if state_size == limit - suffix - 1:
                    plan = self.cli("plan", env=env)
                    saved.write_text(json.dumps(plan))
                    self.cli("apply", "--plan", saved, env=env)
                    self.cli("uninstall", env=env)
                else:
                    for action, args in (("plan", ()), ("apply", ("--plan", saved))):
                        result = self.cli(action, *args, ok=False, env=env)
                        self.assertIn("path", result.stderr)
                        self.assertEqual(self.home_snapshot(), before)

    @unittest.skipIf(os.name == "nt", "POSIX filesystem byte limits")
    def test_atomic_json_path_envelope_preserves_destination(self):
        module = self.module()
        name_max = os.pathconf(self.base, "PC_NAME_MAX")
        # The atomic JSON temporary suffix is dot, UUID, dot-tmp (37 bytes).
        target = self.base / ("j" * (name_max - 36))
        target.write_bytes(b"original")
        before = sorted(self.base.iterdir())
        with self.assertRaises(module.Refusal):
            module.atomic_json(target, {"new": True})
        self.assertEqual(target.read_bytes(), b"original")
        self.assertEqual(sorted(self.base.iterdir()), before)

    @unittest.skipIf(os.name == "nt", "POSIX filesystem byte limits")
    def test_legacy_journal_path_envelope_refuses_before_recovery_or_reversal(self):
        import hashlib
        module = self.module()
        installed = self.apply()
        target, _, state = module.paths(self.home)
        journal_path = state / installed["transaction"] / "journal.json"
        journal = json.loads(journal_path.read_bytes())
        name = "x" * (os.pathconf(target, "PC_NAME_MAX") - 40)
        (target / name).write_bytes(b"legacy")
        (target / name).chmod(0o600)
        manifest = json.loads((target / "manifest.json").read_bytes())
        manifest["files"][name] = {"sha256": hashlib.sha256(b"legacy").hexdigest(), "mode": 0o600}
        (target / "manifest.json").write_text(json.dumps(manifest))
        journal["after"] = module.snapshot(target)
        for phase in ("pending", "committed"):
            journal["phase"] = phase
            journal["staging"] = False
            journal_path.write_bytes(module.encoded(journal))
            before = self.home_snapshot()
            for action in ("status", "recover", "rollback", "uninstall"):
                with self.subTest(phase=phase, action=action):
                    result = self.cli(action, ok=False)
                    self.assertIn("path component", result.stderr)
                    self.assertEqual(self.home_snapshot(), before)

    def test_exhausted_transaction_sequence_refuses_before_bootstrap(self):
        module = self.module()
        result = self.apply()
        state = module.paths(self.home)[2]
        entry = state / result["transaction"]
        entry.rename(state / ("f" * 16 + result["transaction"][16:]))
        self.write_payload("two")
        plan = self.cli("plan")
        saved = self.base / "max-sequence-plan.json"
        saved.write_text(json.dumps(plan))
        before = self.home_snapshot()
        for crash in ("", "after_install"):
            with self.subTest(crash=crash):
                result = self.cli("apply", "--plan", saved, ok=False,
                                  env={"COMMANDS_INSTALL_CRASH_AT": crash})
                self.assertEqual(result.returncode, 1)
                self.assertIn("sequence", result.stderr)
                self.assertEqual(self.home_snapshot(), before)
                self.cli("status")
                self.assertIs(self.cli("recover")["recovered"], False)

    @unittest.skipIf(os.name == "nt", "POSIX legacy path-boundary recovery")
    def test_legacy_overlong_bootstrap_cleans_only_created_namespace(self):
        from unittest.mock import patch
        module = self.module()
        limit = os.pathconf(self.home, "PC_PATH_MAX")
        for state_size in (limit - 64, limit - 46):
            desired = state_size - len("/router-commands-installer/claude")
            raw = str(self.home / "legacy-long-state")
            while len(os.fsencode(raw)) < desired:
                remaining = desired - len(os.fsencode(raw))
                raw += "x" if remaining == 1 else "/" + "x" * min(180, remaining - 1)
            env = {"XDG_STATE_HOME": raw}
            with patch.dict(os.environ, env), patch.object(module, "path_preflight"):
                saved_plan = module.plan(self.home, self.payload)
            saved = self.base / "legacy-bootstrap-plan.json"
            saved.write_text(json.dumps(saved_plan))
            before = self.home_snapshot()
            # Exercise the pre-fix write sequence with only new path checks disabled.
            child = ("import importlib.util,sys; "
                     "s=importlib.util.spec_from_file_location('old',sys.argv[1]); "
                     "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
                     "m.path_preflight=lambda *a:None;m.path_bounds=lambda *a:None; "
                     "sys.argv=sys.argv[1:];sys.exit(m.main())")
            argv = [sys.executable, "-B", "-c", child, str(HELPER), "apply",
                    "--home", str(self.home), "--host", "claude", "--product", "router",
                    "--payload", str(self.payload), "--plan", str(saved)]
            old = subprocess.run(argv, env={**self.env, **env}, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=10)
            with self.subTest(state_bytes=state_size):
                self.assertEqual(old.returncode, 1)
                self.assertTrue(module.bootstrap_path(self.home).exists())
                self.assertTrue(self.cli("recover", env=env)["recovered"])
                self.assertEqual(self.home_snapshot(), before)

    def test_large_legacy_plan_refuses_before_any_install_mutation(self):
        module = self.module()
        plan = self.cli("plan")
        limit = 1024 * 1024
        value = {"type": "dir", "mode": 0o777 if os.name == "nt" else 0o700}
        # Keep the physical legacy inventory within ordinary Win32 paths.
        suffix_length = 57 if os.name == "nt" else 193
        name = "d00000_" + "x" * suffix_length
        addition = len(module.encoded({name: value})) - 1
        count = (limit - 128 - len(module.encoded(plan))) // addition
        for index in range(count):
            name = "d%05d_" % index + "x" * suffix_length
            (self.payload / name).mkdir(mode=0o700)
            plan["payload_inventory"][name] = value
        plan.pop("plan_sha256")
        plan["plan_sha256"] = module.digest(module.encoded(plan))
        raw = module.encoded(plan)
        self.assertLessEqual(len(raw), limit)
        self.assertGreater(len(raw), limit - 500)
        saved = self.base / "legacy-compact-plan.json"
        saved.write_bytes(raw)
        before = self.home_snapshot()
        for action, args in (("plan", ()), ("apply", ("--plan", saved))):
            with self.subTest(action=action):
                result = self.cli(action, *args, ok=False,
                                  env={"COMMANDS_INSTALL_CRASH_AT": "after_install"})
                self.assertEqual(result.returncode, 1)
                self.assertIn("size limit", result.stderr)
                self.assertEqual(self.home_snapshot(), before)

    def test_atomic_json_size_boundary_preserves_destination_and_no_temp(self):
        module = self.module()
        destination = self.base / "journal.json"
        limit = 1024 * 1024
        value = {"padding": "x" * (limit - len(module.encoded({"padding": ""})))}
        module.atomic_json(destination, value)
        self.assertEqual(destination.stat().st_size, limit)
        self.assertEqual(module.read_json(destination), value)
        before = destination.read_bytes()
        value["padding"] += "x"
        with self.assertRaises(module.Refusal):
            module.atomic_json(destination, value)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(self.base.glob("journal.json*")), [destination])

    def test_readable_legacy_journal_reserves_recovery_growth(self):
        module = self.module()
        result = self.apply()
        target, _, state = module.paths(self.home)
        journal_path = state / result["transaction"] / "journal.json"
        journal = json.loads(journal_path.read_bytes())
        desired = 1024 * 1024 - 32
        value = {"type": "dir", "mode": 0o777 if os.name == "nt" else 0o700}
        # Keep the physical legacy inventory within ordinary Win32 paths.
        suffix_length = 57 if os.name == "nt" else 193
        addition = len(module.encoded({"d00000_" + "x" * suffix_length: value})) - 1
        count = (desired - len(module.encoded(journal))) // addition
        names = ["d%05d_" % index + "x" * suffix_length for index in range(count)]
        for name in names:
            journal["after"][name] = value
        remaining = desired - len(module.encoded(journal))
        smallest = len(module.encoded({"z": value})) - 1
        if remaining < smallest:
            journal["after"].pop(names.pop())
            remaining += addition
        final = "z" + "y" * (remaining - smallest)
        self.assertLessEqual(len(final), 255)
        names.append(final)
        journal["after"][final] = value
        for name in names:
            (target / name).mkdir(mode=0o700)
        self.assertEqual(len(module.encoded(journal)), desired)
        for action in ("rollback", "uninstall", "recover"):
            journal["phase"] = "pending" if action == "recover" else "committed"
            journal_path.write_bytes(module.encoded(journal))
            before = self.home_snapshot()
            with self.subTest(action=action):
                failure = self.cli(action, ok=False)
                self.assertIn("size limit", failure.stderr)
                self.assertEqual(self.home_snapshot(), before)

    @unittest.skipIf(os.name == "nt", "POSIX literal names and special mode bits")
    def test_payload_inventory_must_be_recoverable_before_plan_or_apply(self):
        module = self.module()
        saved = self.base / "valid-plan.json"
        for upgrade in (False, True):
            if upgrade:
                self.apply()
                self.write_payload("two")
            valid = self.cli("plan")
            for kind in ("colon", "backslash", "directory-setuid", "directory-sticky",
                         "manifest-setuid", "manifest-setgid"):
                if kind in ("colon", "backslash"):
                    changed = self.payload / ("bad:name" if kind == "colon" else "bad\\name")
                    changed.mkdir(mode=0o700)
                else:
                    if kind.startswith("directory"):
                        changed = self.payload / "bin"
                        requested = 0o4700 if kind.endswith("setuid") else 0o1700
                    else:
                        changed = self.payload / "manifest.json"
                        requested = 0o4600 if kind.endswith("setuid") else 0o2600
                        # BSD gives a new file its folder's group (wheel under macOS /private/tmp),
                        # and chmod silently drops setgid for a group the caller is not in.
                        os.chown(changed, -1, os.getegid())
                    changed.chmod(requested)
                    self.assertEqual(stat.S_IMODE(changed.stat().st_mode), requested,
                                     "fixture: the OS did not keep the special mode bit")
                try:
                    # A consistent saved plan must still validate its payload;
                    # refusal cannot depend merely on stale inventory/digest.
                    inventory = module.snapshot(self.payload)
                    forged = dict(valid, payload_inventory=inventory)
                    forged.pop("plan_sha256")
                    forged["plan_sha256"] = module.digest(module.encoded(forged))
                    saved.write_text(json.dumps(forged))
                    before = self.home_snapshot()
                    with self.subTest(kind=kind, upgrade=upgrade):
                        self.cli("plan", ok=False)
                        result = self.cli("apply", "--plan", saved, ok=False,
                                          env={"COMMANDS_INSTALL_CRASH_AT": "after_install"})
                        self.assertEqual(result.returncode, 1)
                        self.assertIn("invalid payload inventory", result.stderr)
                        self.assertEqual(self.home_snapshot(), before)
                        self.assertEqual(module.snapshot(self.payload), inventory)
                finally:
                    if kind in ("colon", "backslash"):
                        changed.rmdir()
                    else:
                        changed.chmod(0o700 if kind.startswith("directory") else 0o600)

    def test_inventory_rejects_nonportable_names_and_special_mode_metadata(self):
        module = self.module()
        for name, mode in (("bad:name", 0o700), ("bad\\name", 0o700),
                           ("/absolute", 0o700), ("../escape", 0o700),
                           ("dir", 0o1700), ("dir", 0o4700)):
            with self.subTest(name=name, mode=mode), self.assertRaises(module.Refusal):
                module.validate_inventory({name: {"type": "dir", "mode": mode}}, "invalid inventory")

    def test_lock_uses_fixed_physical_system_directory(self):
        module = self.module()
        expected = (Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else
                    Path("/private/tmp" if sys.platform == "darwin" else "/tmp"))
        name = "commands-installer-" + module.digest(module.canonical_path(self.home).encode()) + ".lock"
        alternate = self.base / "alternate-temp"
        alternate.mkdir()
        with patch.dict(os.environ, {"TMPDIR": str(alternate), "TEMP": str(alternate), "TMP": str(alternate)}):
            with module.lock_path(self.home):
                self.assertTrue((expected / name).is_file())
                self.assertEqual(list(alternate.iterdir()), [])

    def test_readonly_payload_has_reversible_platform_contract(self):
        import hashlib
        module = self.module()
        extra = self.payload / "read-only.txt"
        manifest_path = self.payload / "manifest.json"
        for upgrade in (False, True):
            if upgrade:
                self.apply()
                self.write_payload("two")
            valid = self.cli("plan")
            original = manifest_path.read_bytes()
            for mode in (0o400, 0o500):
                extra.write_bytes(b"read-only fixture")
                extra.chmod(mode)
                manifest = json.loads(original)
                manifest["files"][extra.name] = {
                    "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(), "mode": mode}
                manifest_path.write_text(json.dumps(manifest))
                try:
                    if os.name == "nt":
                        inventory = module.snapshot(self.payload)
                        forged = dict(valid, payload_inventory=inventory)
                        forged.pop("plan_sha256")
                        forged["plan_sha256"] = module.digest(module.encoded(forged))
                        saved = self.base / "readonly-plan.json"
                        saved.write_text(json.dumps(forged))
                        before = self.home_snapshot()
                        for action, args in (("plan", ()), ("apply", ("--plan", saved))):
                            result = self.cli(action, *args, ok=False)
                            self.assertIn("read-only payload files are unsupported on Windows", result.stderr)
                            self.assertEqual(self.home_snapshot(), before)
                            self.assertEqual(module.snapshot(self.payload), inventory)
                    else:
                        self.apply()
                        self.cli("uninstall")
                finally:
                    extra.chmod(0o600)
                    extra.unlink()
                    manifest_path.write_bytes(original)

    @unittest.skipUnless(os.name == "nt", "native NTFS directory junction")
    def test_junctions_are_not_traversed_or_adopted(self):
        module = self.module()
        outside = self.base / "junction-outside"
        outside.mkdir()
        victim = outside / "synthetic.txt"
        victim.write_bytes(b"unchanged synthetic outside")
        for target in (self.payload / "junction", self.home / ".claude"):
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(outside)],
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                    timeout=10, env=self.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                before = self.home_snapshot()
                self.cli("plan", ok=False)
                with self.assertRaises(module.Refusal):
                    module.snapshot(target if target.parent == self.home else self.payload)
                self.assertEqual(self.home_snapshot(), before)
                self.assertEqual(victim.read_bytes(), b"unchanged synthetic outside")
            finally:
                target.rmdir()  # Removes the junction itself, never its target.

    def test_valid_empty_payload_directory_roundtrips_lifecycle(self):
        (self.payload / "empty directory").mkdir(mode=0o700)
        (self.payload / "empty directory/nested").mkdir(mode=0o700)
        self.apply()
        self.cli("status")
        self.cli("recover")
        self.cli("uninstall")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(self.cli("plan")))
        result = self.cli("apply", "--plan", saved, ok=False,
                          env={"COMMANDS_INSTALL_CRASH_AT": "after_install"})
        self.assertEqual(result.returncode, 91)
        self.cli("recover")
        self.cli("status")

    def test_dry_run_is_write_free_and_saved_plan_is_exact(self):
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        plan = self.cli("plan")
        self.assertEqual(before, sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*")))
        self.assertIn("plan_sha256", plan)
        self.write_payload("changed")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, ok=False)
        self.assertFalse((self.home / ".claude/router-commands").exists())

    def test_apply_upgrade_rollback_uninstall_and_data(self):
        self.apply()
        target = self.home / ".claude/router-commands"
        launcher = self.home / ".local/bin/claude-router"
        self.assertEqual((target / "bin/router-commands").read_text(), "one")
        self.assertTrue(launcher.is_symlink())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o777 if os.name == "nt" else 0o700)
        data = self.home / ".local/state/router-commands/claude/data.json"
        data.parent.mkdir(parents=True)
        data.write_text("user")
        self.write_payload("two")
        self.apply()
        self.assertEqual((target / "bin/router-commands").read_text(), "two")
        self.cli("rollback")
        self.assertEqual((target / "bin/router-commands").read_text(), "one")
        self.cli("uninstall")
        self.assertFalse(target.exists())
        self.assertFalse(launcher.is_symlink())
        self.assertEqual(data.read_text(), "user")

    def test_launcher_conflict_and_unknown_drift_are_preserved(self):
        launcher = self.home / ".local/bin/claude-router"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("private")
        self.cli("plan", ok=False)
        self.assertEqual(launcher.read_text(), "private")
        launcher.unlink()
        self.apply()
        target = self.home / ".claude/router-commands"
        (target / "user.txt").write_text("mine")
        self.cli("uninstall", ok=False)
        self.assertEqual((target / "user.txt").read_text(), "mine")

    def test_recover_interrupted_apply(self):
        plan = self.cli("plan")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        self.cli("recover")
        self.assertFalse((self.home / ".claude/router-commands").exists())

    def test_host_both_rejected(self):
        result = subprocess.run([sys.executable, "-B", str(HELPER), "plan", "--product", "router",
                                 "--host", "both", "--home", str(self.home), "--payload", str(self.payload)],
                                capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        self.assertNotEqual(result.returncode, 0)

    def test_interrupted_apply_and_recovery_at_each_boundary(self):
        for boundary in ("during_staging", "after_backup", "after_install", "after_link"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                original = self.home
                self.home = canonical_temp_root(root) / "home"
                self.home.mkdir()
                try:
                    plan = self.cli("plan")
                    saved = self.base / "plan.json"
                    saved.write_text(json.dumps(plan))
                    self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": boundary}, ok=False)
                    self.cli("recover")
                    self.assertFalse((self.home / ".claude/router-commands").exists())
                    self.assertFalse((self.home / ".local/bin/claude-router").is_symlink())
                finally:
                    self.home = original

    def test_recovery_refuses_user_addition_and_temp_symlink(self):
        plan = self.cli("plan")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        target = self.home / ".claude/router-commands"
        (target / "user.txt").write_text("mine")
        self.cli("recover", ok=False)
        self.assertEqual((target / "user.txt").read_text(), "mine")
        (target / "user.txt").unlink()
        state = self.home / ".local/state/router-commands-installer/claude"
        entry = next(state.iterdir())
        outside = self.base / "outside"
        outside.write_text("keep")
        (entry / "journal.json.tmp").symlink_to(outside)
        self.cli("recover", ok=False)
        self.assertEqual(outside.read_text(), "keep")

    def test_upgrade_uninstall_and_namespace_isolation(self):
        self.apply()
        self.write_payload("two")
        self.apply()
        (self.home / ".codex").mkdir()
        (self.home / ".codex/other").write_text("keep")
        self.cli("uninstall")
        self.assertFalse((self.home / ".claude/router-commands").exists())
        self.assertEqual((self.home / ".codex/other").read_text(), "keep")

    def test_symlink_parent_and_manifest_traversal_refuse(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.home / ".claude").symlink_to(outside, target_is_directory=True)
        self.cli("plan", ok=False)
        self.assertEqual(list(outside.iterdir()), [])
        (self.home / ".claude").unlink()
        manifest = self.payload / "manifest.json"
        data = json.loads(manifest.read_text())
        data["files"]["../escape"] = data["files"].pop("bin/router-commands")
        manifest.write_text(json.dumps(data))
        self.cli("plan", ok=False)

    def test_saved_plan_preimage_and_idempotence(self):
        stale = self.cli("plan")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(stale))
        self.cli("apply", "--plan", saved)
        self.assertTrue(self.cli("apply", "--plan", saved)["idempotent"])
        self.write_payload("two")
        changed = self.cli("plan")
        saved.write_text(json.dumps(changed))
        (self.home / ".local/bin/claude-router").unlink()
        self.cli("apply", "--plan", saved, ok=False)
        self.assertEqual((self.home / ".claude/router-commands/bin/router-commands").read_text(), "one")

    def test_interrupted_upgrade_and_recovery_resumes(self):
        for boundary in ("after_backup", "after_install", "after_link"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                original = self.home
                self.home = canonical_temp_root(root) / "home"
                self.home.mkdir()
                try:
                    self.write_payload("one")
                    self.apply()
                    self.write_payload("two")
                    plan = self.cli("plan")
                    saved = self.base / "plan.json"
                    saved.write_text(json.dumps(plan))
                    self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": boundary}, ok=False)
                    self.cli("recover", env={"COMMANDS_INSTALL_CRASH_AT": "after_recover_stage"}, ok=False)
                    self.cli("recover")
                    self.assertEqual((self.home / ".claude/router-commands/bin/router-commands").read_text(), "one")
                finally:
                    self.home = original

    def test_interrupted_rollback_recovers(self):
        for boundary in ("after_rollback_stage", "after_rollback_unlink", "after_rollback_restore",
                         "after_rollback_link", "after_rollback_cleanup_intent", "during_recover_cleanup"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                original = self.home
                self.home = canonical_temp_root(root) / "home"
                self.home.mkdir()
                try:
                    self.write_payload("one")
                    self.apply()
                    self.write_payload("two")
                    self.apply()
                    self.cli("rollback", env={"COMMANDS_INSTALL_CRASH_AT": boundary}, ok=False)
                    self.cli("recover")
                    self.assertEqual((self.home / ".claude/router-commands/bin/router-commands").read_text(), "one")
                finally:
                    self.home = original

    def test_recovery_and_rollback_cleanup_boundaries(self):
        for boundary in ("after_recover_intent", "after_recover_stage", "after_recover_unlink",
                         "after_recover_restore", "after_recover_link", "after_recover_cleanup_intent",
                         "during_recover_cleanup", "after_recover_cleanup"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                original = self.home
                self.home = canonical_temp_root(root) / "home"
                self.home.mkdir()
                try:
                    plan = self.cli("plan")
                    saved = self.base / "plan.json"
                    saved.write_text(json.dumps(plan))
                    self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
                    self.cli("recover", env={"COMMANDS_INSTALL_CRASH_AT": boundary}, ok=False)
                    self.cli("recover")
                    self.assertFalse((self.home / ".claude/router-commands").exists())
                finally:
                    self.home = original

    def test_canonical_lock_serializes_alias_and_tmpdir(self):
        if os.name == "nt":
            self.skipTest("POSIX flock probe")
        spec = importlib.util.spec_from_file_location("commands_install", HELPER)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        (self.home / "alias").mkdir()
        alias = self.home / "alias/.."
        child = r'''
import importlib.util, pathlib, sys, fcntl
spec=importlib.util.spec_from_file_location("commands_install",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=fcntl.flock
def probe(fd, operation):
    try: original(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: print("blocked", flush=True)
    return original(fd, operation)
fcntl.flock=probe
with m.lock(pathlib.Path(sys.argv[2])): print("acquired", flush=True)
'''
        with installer.lock(self.home):
            process = subprocess.Popen([sys.executable, "-B", "-c", child, str(HELPER), str(alias)],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL, env={**self.env, "TMPDIR": str(self.base)})
            try:
                self.assertEqual(process.stdout.readline().strip(), "blocked")
            finally:
                pass
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("acquired", out)

    def test_input_fifo_and_payload_symlink_refuse(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO unavailable")
        fifo = self.base / "plan.fifo"
        os.mkfifo(fifo)
        self.cli("apply", "--plan", fifo, ok=False)
        entry = self.payload / "bin/router-commands"
        entry.unlink()
        outside = self.base / "outside"
        outside.write_text("one")
        entry.symlink_to(outside)
        self.cli("plan", ok=False)
        self.assertEqual(outside.read_text(), "one")

    def test_product_and_host_namespaces_are_independent(self):
        self.apply()
        second = self.base / "harness-payload"
        second.mkdir(mode=0o700)
        (second / "bin").mkdir(mode=0o700)
        command = second / "bin/harness-commands"
        command.write_text("harness")
        command.chmod(0o700)
        import hashlib
        (second / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "source_revision": "h1",
            "files": {"bin/harness-commands": {"sha256": hashlib.sha256(b"harness").hexdigest(), "mode": 0o700}}
        }))
        (second / "manifest.json").chmod(0o600)
        plan = self.cli("plan", product="harness", host="codex", payload=second)
        saved = self.base / "harness-plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, product="harness", host="codex", payload=second)
        self.cli("uninstall")
        self.assertTrue((self.home / ".codex/harness-commands/bin/harness-commands").exists())
        self.assertTrue((self.home / ".local/bin/codex-harness").is_symlink())

    def test_uninstall_preflights_every_upgrade_backup(self):
        self.apply()
        self.write_payload("two")
        self.apply()
        self.write_payload("three")
        self.apply()
        state = self.home / ".local/state/router-commands-installer/claude"
        upgrades = sorted(p for p in state.iterdir() if (p / "backup").exists())
        (upgrades[0] / "backup/user.txt").write_text("mine")
        self.cli("uninstall", ok=False)
        self.assertEqual((self.home / ".claude/router-commands/bin/router-commands").read_text(), "three")
        self.assertEqual((upgrades[0] / "backup/user.txt").read_text(), "mine")

    def test_forged_cleanup_path_cannot_remove_unrelated_directory(self):
        plan = self.cli("plan")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        outside = self.base / "empty-unrelated"
        outside.mkdir()
        entry = next((self.home / ".local/state/router-commands-installer/claude").iterdir())
        journal_path = entry / "journal.json"
        journal = json.loads(journal_path.read_text())
        journal["made"].append(str(outside))
        journal_path.write_text(json.dumps(journal))
        self.cli("recover", ok=False)
        self.assertTrue(outside.is_dir())
        self.assertTrue((self.home / ".claude/router-commands").exists())

    def test_shared_state_recovery_is_bound_to_original_home(self):
        shared = self.base / "shared-state"
        env = {"XDG_STATE_HOME": str(shared)}
        plan = self.cli("plan", env=env)
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, env={**env, "COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        original = self.home
        other = self.base / "other-home"
        other.mkdir()
        self.home = other
        try:
            self.cli("recover", env=env, ok=False)
        finally:
            self.home = original
        self.assertTrue((original / ".claude/router-commands").exists())
        self.assertTrue(self.cli("recover", env=env)["recovered"])

    def test_partial_stage_write_recovers_and_edited_partial_refuses(self):
        for upgrade in (False, True):
          for boundary in ("after_stage_file_create", "during_stage_write",
                           "after_stage_file_chmod", "after_stage_file_rename"):
            with self.subTest(upgrade=upgrade, boundary=boundary), tempfile.TemporaryDirectory() as root:
                original = self.home
                self.home = canonical_temp_root(root) / "home"
                self.home.mkdir()
                try:
                    if upgrade:
                        self.write_payload("one")
                        self.apply()
                        self.write_payload("two")
                    plan = self.cli("plan")
                    saved = self.base / "plan.json"
                    saved.write_text(json.dumps(plan))
                    self.cli("apply", "--plan", saved,
                             env={"COMMANDS_INSTALL_CRASH_AT": boundary}, ok=False)
                    state = self.home / ".local/state/router-commands-installer/claude"
                    entry = sorted(state.iterdir())[-1]
                    partials = list((entry / "stage").rglob("*.writing-*"))
                    if partials:
                        partial = partials[0]
                        partial.write_text("user-edited")
                        self.cli("recover", ok=False)
                        self.assertEqual(partial.read_text(), "user-edited")
                        partial.write_bytes(b"")
                        partial.chmod(0o600)
                    self.cli("recover", env={"COMMANDS_INSTALL_CRASH_AT": "after_recover_cleanup_intent"}, ok=False)
                    self.cli("recover")
                    target = self.home / ".claude/router-commands/bin/router-commands"
                    self.assertEqual(target.read_text() if upgrade else None, "one" if upgrade else None)
                finally:
                    self.home = original

    def test_private_transaction_and_temp_journal_attack(self):
        plan = self.cli("plan")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        state = self.home / ".local/state/router-commands-installer/claude"
        entry = next(state.iterdir())
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o777 if os.name == "nt" else 0o700)
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o777 if os.name == "nt" else 0o700)
        self.assertEqual(stat.S_IMODE((entry / "journal.json").stat().st_mode), 0o666 if os.name == "nt" else 0o600)
        outside = self.base / "outside"
        outside.write_text("keep")
        (entry / "journal.json.0123456789abcdef0123456789abcdef.tmp").symlink_to(outside)
        self.cli("recover", ok=False)
        self.assertEqual(outside.read_text(), "keep")

    def test_recovery_refuses_unexpected_transaction_material(self):
        plan = self.cli("plan")
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(plan))
        self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        entry = next((self.home / ".local/state/router-commands-installer/claude").iterdir())
        backup = entry / "backup"
        backup.mkdir()
        self.cli("recover", ok=False)
        self.assertTrue(backup.exists())
        self.assertTrue((self.home / ".claude/router-commands").exists())

    def test_atomic_journal_falls_back_when_nofollow_is_unavailable(self):
        spec = importlib.util.spec_from_file_location("commands_install", HELPER)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        original = installer.os
        class OsWithoutNofollow:
            def __getattr__(self, name):
                if name == "O_NOFOLLOW":
                    raise AttributeError(name)
                return getattr(original, name)
        installer.os = OsWithoutNofollow()
        path = self.base / "journal.json"
        installer.atomic_json(path, {"phase": "pending"})
        self.assertEqual(json.loads(path.read_text())["phase"], "pending")

    def module(self):
        spec = importlib.util.spec_from_file_location("commands_install", HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_windows_canonical_paths_preserve_drive_and_unc(self):
        import ntpath
        module = self.module()
        for raw, expected in ((r"C:\Users\probe\..\probe", r"c:\users\probe"),
                              (r"\\server\share\User", r"\\server\share\user")):
            actual = module.canonical_path(raw, path_module=ntpath)
            self.assertEqual(actual, expected)
            self.assertTrue(ntpath.isabs(actual))

    def test_shared_destinations_serialize_across_home_and_host_home(self):
        if os.name == "nt":
            self.skipTest("POSIX lock contention probe")
        module = self.module()
        other = self.base / "other"
        other.mkdir()
        shared = self.base / "host"
        shared.mkdir()
        module.HOST_HOME = shared
        child = r'''
import importlib.util, pathlib, sys, fcntl
spec=importlib.util.spec_from_file_location("installer",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.HOST_HOME=pathlib.Path(sys.argv[3])
original=fcntl.flock
def probe(fd, operation):
    if operation == fcntl.LOCK_EX:
        try: original(fd, operation | fcntl.LOCK_NB)
        except BlockingIOError: print("blocked", flush=True)
    return original(fd, operation)
fcntl.flock=probe
with m.lock(pathlib.Path(sys.argv[2])): print("acquired", flush=True)
'''
        for home, host_home in ((other, shared), (self.home, other)):
            with self.subTest(home=home, host_home=host_home):
                with module.lock(self.home):
                    process = subprocess.Popen([sys.executable, "-B", "-c", child,
                        str(HELPER), str(home), str(host_home)], stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    first = process.stdout.readline().strip()
                out, err = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, err)
                self.assertEqual(first, "blocked")
                self.assertIn("acquired", out)

    def test_recovery_and_uninstall_preserve_root_mode_drift(self):
        for action, material in (("recover", "target"), ("recover", "backup"),
                                 ("rollback", "backup"), ("uninstall", "backup"),
                                 ("rollback_recover", "rollback_stage")):
            with self.subTest(action=action, material=material), tempfile.TemporaryDirectory() as root:
                original = self.home
                self.home = canonical_temp_root(root) / "home"
                self.home.mkdir()
                changed = None
                try:
                    self.write_payload("one")
                    self.apply()
                    self.write_payload("two")
                    saved = self.base / "plan.json"
                    saved.write_text(json.dumps(self.cli("plan")))
                    if action == "recover":
                        self.cli("apply", "--plan", saved,
                                 env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
                    else:
                        self.cli("apply", "--plan", saved)
                    if action == "rollback_recover":
                        self.cli("rollback", env={"COMMANDS_INSTALL_CRASH_AT": "after_rollback_stage"}, ok=False)
                    entry = sorted((self.home / ".local/state/router-commands-installer/claude").iterdir())[-1]
                    changed = self.home / ".claude/router-commands" if material == "target" else entry / material
                    changed.chmod(0o500 if os.name == "nt" else 0o755)
                    before = sorted(str(p) for p in self.home.rglob("*"))
                    self.cli("recover" if action == "rollback_recover" else action, ok=False)
                    self.assertEqual(stat.S_IMODE(changed.stat().st_mode), 0o555 if os.name == "nt" else 0o755)
                    self.assertEqual(before, sorted(str(p) for p in self.home.rglob("*")))
                finally:
                    if os.name == "nt" and changed is not None and changed.exists():
                        changed.chmod(0o700)
                    self.home = original

    def mutation_run(self, action, saved, cut=0, edge="after", bootstrap=False):
        # Patch real OS boundaries, not the installer's named checkpoints.
        child = r'''
import importlib.util, os, sys
spec=importlib.util.spec_from_file_location("installer",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
cut=int(sys.argv[2]); edge=sys.argv[3]; bootstrap=sys.argv[4] == "True"
count=0; active=True
original_copy=m.copy_payload
def copy(*args, **kw):
    global active
    if bootstrap: active=False
    return original_copy(*args, **kw)
m.copy_payload=copy
def wrap(name):
    original=getattr(os,name)
    def call(*args, **kw):
        global count
        mutation = name != "open" or args[1] & (os.O_CREAT | os.O_WRONLY | os.O_RDWR)
        # Locks are persistent coordination files, not install mutations.
        mutation = mutation and "commands-installer-" not in str(args[0])
        if active and mutation:
            count+=1
            if count == cut and edge == "before": os._exit(91)
        result=original(*args, **kw)
        if active and mutation and count == cut and edge == "after": os._exit(91)
        return result
    setattr(os,name,call)
    # Python 3.9/3.10 pathlib caches these functions on an accessor.
    # Trace the same actual OS boundary regardless of interpreter version.
    accessor = getattr(m.Path(), "_accessor", None)
    if accessor is not None and getattr(accessor, name, None) is original:
        setattr(accessor, name, call)
for name in ("mkdir", "open", "replace", "unlink", "rmdir", "chmod", "symlink"):
    wrap(name)
sys.argv=[sys.argv[1]]+sys.argv[5:]
result=m.main()
print("MUTATIONS="+str(count),file=sys.stderr)
sys.exit(result)
'''
        args = [sys.executable, "-B", "-c", child, str(HELPER), str(cut), edge, str(bootstrap),
                action, "--product", "router", "--host", "claude", "--home", str(self.home)]
        if action == "apply":
            args += ["--payload", str(self.payload), "--plan", str(saved)]
        return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)

    def test_bootstrap_real_mutation_boundaries_and_recovery(self):
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(self.cli("plan")))
        baseline = self.mutation_run("apply", saved, bootstrap=True)
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        count = int(baseline.stderr.split("MUTATIONS=")[-1])
        self.assertGreater(count, 10)
        print("Bootstrap syscall cuts checked:", count * 2, file=sys.stderr)
        for edge in ("before", "after"):
            for cut in range(1, count + 1):
                with self.subTest(edge=edge, cut=cut), tempfile.TemporaryDirectory() as root:
                    original = self.home
                    self.home = canonical_temp_root(root) / "home"
                    self.home.mkdir()
                    try:
                        saved.write_text(json.dumps(self.cli("plan")))
                        result = self.mutation_run("apply", saved, cut, edge, bootstrap=True)
                        if result.returncode == 0:  # e.g. unlink of an already renamed temp
                            continue
                        self.assertEqual(result.returncode, 91, result.stderr)
                        bootstrap = list(self.home.glob(".*-bootstrap.json"))
                        journals = list(self.home.rglob("journal.json"))
                        recovery = self.cli("recover")
                        self.assertFalse(self.cli("status")["installed"])
                        if bootstrap or not journals:
                            # No state directories survive an incomplete bootstrap.
                            self.cli("apply", "--plan", saved)
                        else:
                            # A fully published transaction retains recovery history.
                            self.assertTrue(recovery["recovered"])
                            self.apply()
                    finally:
                        self.home = original

    def test_bootstrap_recovery_real_mutation_boundaries(self):
        # Crash before removing the bootstrap journal: all parents, the entry,
        # journal and ownership markers exist, but runtime staging has not begun.
        child = r'''
import importlib.util, os, sys
spec=importlib.util.spec_from_file_location("installer",sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=os.unlink
def unlink(path, *a, **kw):
    if str(path).endswith("-bootstrap.json"): os._exit(91)
    return original(path, *a, **kw)
os.unlink=unlink
accessor = getattr(m.Path(), "_accessor", None)
if accessor is not None and getattr(accessor, "unlink", None) is original:
    accessor.unlink = unlink
sys.argv=[sys.argv[1]]+sys.argv[2:]
sys.exit(m.main())
'''
        def prepare():
            saved = self.base / "plan.json"
            saved.write_text(json.dumps(self.cli("plan")))
            result = subprocess.run([sys.executable, "-B", "-c", child, str(HELPER),
                "apply", "--product", "router", "--host", "claude", "--home", str(self.home),
                "--payload", str(self.payload), "--plan", str(saved)],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 91, result.stderr)
            entry = next((self.home / ".local/state/router-commands-installer/claude").iterdir())
            temp = entry / ("journal.json." + "a" * 32 + ".tmp")
            temp.write_bytes((entry / "journal.json").read_bytes()[:30])
            temp.chmod(0o600)
            return saved
        saved = prepare()
        result = self.mutation_run("recover", saved)
        self.assertEqual(result.returncode, 0, result.stderr)
        count = int(result.stderr.split("MUTATIONS=")[-1])
        print("Bootstrap recovery syscall cuts checked:", count * 2, file=sys.stderr)
        for edge in ("before", "after"):
            for cut in range(1, count + 1):
                with self.subTest(edge=edge, cut=cut), tempfile.TemporaryDirectory() as root:
                    original = self.home
                    self.home = canonical_temp_root(root) / "home"
                    self.home.mkdir()
                    try:
                        saved = prepare()
                        result = self.mutation_run("recover", saved, cut, edge)
                        self.assertIn(result.returncode, (0, 91), result.stderr)
                        self.cli("recover")
                        self.cli("apply", "--plan", saved)
                    finally:
                        self.home = original

    def test_bootstrap_preserves_unknown_content_and_private_modes(self):
        # Leave a published bootstrap intent, before transaction creation.
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(self.cli("plan")))
        # Discover mkdir's index from a genuine boundary trace, not a checkpoint.
        module = self.module()
        journal = {"phase": "pending", "plan_sha256": "a" * 64,
            "before": {"tree": None, "link": None},
            "after": module.validate_manifest(self.payload)[0],
            "parent_before": module.parent_preimages(self.home),
            "staging": True, "payload": str(self.payload),
            "identity": module.identity(self.home), "owners": module.owner_preimages(self.home),
            **module.identity(self.home)}
        journal.pop("state")
        journal["made"] = [p for p, mode in journal["parent_before"].items() if mode is None]
        boot = module.bootstrap_path(self.home)
        module.atomic_json(boot, {"transaction": "a" * 16 + "-" + "b" * 32, "journal": journal})
        module.ensure_parents(self.home)
        target_parent = self.home / ".claude"
        extra = target_parent / "personal"
        extra.write_text("keep")
        self.cli("recover", ok=False)
        self.assertEqual(extra.read_text(), "keep")
        extra.unlink()
        target_parent.chmod(0o500 if os.name == "nt" else 0o755)
        self.cli("recover", ok=False)
        self.assertEqual(stat.S_IMODE(target_parent.stat().st_mode), 0o555 if os.name == "nt" else 0o755)
        target_parent.chmod(0o700)
        self.cli("recover")
        self.cli("apply", "--plan", saved)

    def test_concurrent_cross_home_apply_preserves_winner(self):
        module = self.module()
        other = self.base / "other-home"
        other.mkdir()
        shared = self.base / "shared-host"
        shared.mkdir()
        module.HOST_HOME = shared
        homes = [self.home, other]
        plans = []
        for index, home in enumerate(homes):
            saved = self.base / ("shared-plan-" + str(index) + ".json")
            result = self.cli("plan", "--home", home, "--host-home", shared)
            saved.write_text(json.dumps(result))
            plans.append(saved)
        with module.lock(self.home):
            processes = [subprocess.Popen([sys.executable, "-B", str(HELPER), "apply",
                "--product", "router", "--host", "claude", "--home", str(home),
                "--host-home", str(shared), "--payload", str(self.payload), "--plan", str(saved)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for home, saved in zip(homes, plans)]
        results = [p.communicate(timeout=10) for p in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1], results)
        winner = next(home for home, p in zip(homes, processes) if p.returncode == 0)
        loser = next(home for home, p in zip(homes, processes) if p.returncode == 1)
        self.assertFalse(self.cli("recover", "--home", loser, "--host-home", shared)["recovered"])
        self.assertTrue(self.cli("status", "--home", winner, "--host-home", shared)["installed"])
        self.assertEqual((shared / "router-commands/bin/router-commands").read_text(), "one")

    def test_real_lifecycle_and_recovery_mutation_boundaries(self):
        total = 0
        for scenario in ("upgrade", "rollback", "recover_apply", "recover_upgrade", "uninstall"):
            def prepare():
                self.write_payload("one")
                if scenario != "recover_apply":
                    self.apply()
                    self.write_payload("two")
                saved = self.base / "plan.json"
                saved.write_text(json.dumps(self.cli("plan")))
                if scenario.startswith("recover_"):
                    self.cli("apply", "--plan", saved,
                             env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
                    return "recover", saved
                if scenario in ("rollback", "uninstall"):
                    self.cli("apply", "--plan", saved)
                    return scenario, saved
                return "apply", saved
            with tempfile.TemporaryDirectory() as root:
                original = self.home
                self.home = canonical_temp_root(root) / "home"
                self.home.mkdir()
                try:
                    action, saved = prepare()
                    baseline = self.mutation_run(action, saved)
                    self.assertEqual(baseline.returncode, 0, baseline.stderr)
                    count = int(baseline.stderr.split("MUTATIONS=")[-1])
                finally:
                    self.home = original
            for edge in ("before", "after"):
                for cut in range(1, count + 1):
                    with self.subTest(scenario=scenario, edge=edge, cut=cut), tempfile.TemporaryDirectory() as root:
                        original = self.home
                        self.home = canonical_temp_root(root) / "home"
                        self.home.mkdir()
                        try:
                            action, saved = prepare()
                            result = self.mutation_run(action, saved, cut, edge)
                            self.assertIn(result.returncode, (0, 91), result.stderr)
                            self.cli("recover")
                            status = self.cli("status")
                            if status["installed"]:
                                value = (self.home / ".claude/router-commands/bin/router-commands").read_text()
                                self.assertIn(value, ("one", "two"))
                            else:
                                self.assertIn(scenario, ("recover_apply", "uninstall"))
                            if scenario == "uninstall":
                                self.cli("uninstall")
                                self.assertFalse(self.cli("status")["installed"])
                            total += 1
                        finally:
                            self.home = original
        print("Filesystem mutation cuts checked:", total, file=sys.stderr)

    def test_symlink_state_alias_and_bootstrap_temp_attacks(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.home / "alias").symlink_to(outside, target_is_directory=True)
        self.cli("plan", env={"XDG_STATE_HOME": str(self.home / "alias/../state")}, ok=False)
        module = self.module()
        boot = module.bootstrap_path(self.home)
        victim = self.base / "victim"
        victim.write_text("keep")
        temp = boot.with_name(boot.name + "." + "a" * 32 + ".tmp")
        temp.symlink_to(victim)
        self.cli("recover", ok=False)
        self.assertEqual(victim.read_text(), "keep")
        temp.unlink()
        temp.write_text("unknown")
        temp.chmod(0o400 if os.name == "nt" else 0o644)
        self.cli("recover", ok=False)
        self.assertEqual(temp.read_text(), "unknown")

    def test_interrupted_shared_runtime_cannot_be_claimed_by_other_home(self):
        other = self.base / "other-home"
        other.mkdir()
        shared = self.base / "shared-host"
        shared.mkdir()
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(self.cli("plan", "--host-home", shared)))
        self.cli("apply", "--plan", saved, "--host-home", shared,
                 env={"COMMANDS_INSTALL_CRASH_AT": "after_backup"}, ok=False)
        self.cli("plan", "--home", other, "--host-home", shared, ok=False)
        self.cli("recover", "--host-home", shared)
        self.assertFalse((shared / "router-commands").exists())
        self.cli("plan", "--home", other, "--host-home", shared)

    def test_owner_marker_drift_is_preserved(self):
        saved = self.base / "plan.json"
        saved.write_text(json.dumps(self.cli("plan")))
        self.cli("apply", "--plan", saved,
                 env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        module = self.module()
        marker = module.owner_paths(self.home)[0]
        marker.unlink()
        marker.symlink_to("personal")
        self.cli("recover", ok=False)
        self.assertEqual(os.readlink(marker), "personal")
        self.assertTrue((self.home / ".claude/router-commands").exists())

    def test_history_order_survives_backward_wall_clock(self):
        from unittest.mock import patch
        module = self.module()
        with module.lock(self.home):
            for content, timestamp in (("one", 2), ("two", 1)):
                self.write_payload(content)
                saved = module.plan(self.home, self.payload)
                with patch.object(module.time, "time_ns", return_value=timestamp):
                    module.do_apply(self.home, self.payload, saved)
            self.assertTrue(module.do_uninstall(self.home)["uninstalled"])

    def test_committed_transaction_drift_refuses_before_journal_mutation(self):
        self.apply()
        entry = next((self.home / ".local/state/router-commands-installer/claude").iterdir())
        journal = entry / "journal.json"
        before = journal.read_bytes()
        stage = entry / "rollback_stage"
        stage.mkdir(mode=0o700)
        (stage / "personal").write_text("keep")
        self.cli("rollback", ok=False)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual((stage / "personal").read_text(), "keep")


    def home_snapshot(self):
        result = {}
        for path in self.home.rglob("*"):
            mode = path.lstat().st_mode
            content = (os.readlink(path) if stat.S_ISLNK(mode)
                       else path.read_bytes() if stat.S_ISREG(mode) else None)
            result[str(path.relative_to(self.home))] = (mode, content)
        return result

    def test_overlapping_install_namespaces_refuse_before_mutation(self):
        for location in ("runtime", "inside_runtime", "launcher", "runtime_owner",
                         "launcher_temporary", "bootstrap", "state_ancestor", "normalized_runtime",
                         "data_in_runtime", "bootstrap_legacy_temp", "bootstrap_random_temp"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                original = self.home
                self.home = canonical_temp_root(temporary) / "home"
                self.home.mkdir()
                try:
                    target = self.home / ".claude/router-commands"
                    link = self.home / ".local/bin/claude-router"
                    locations = {"runtime": target, "inside_runtime": target / "metadata",
                                 "launcher": link, "runtime_owner": target.with_name(".router-commands.owner"),
                                 "launcher_temporary": link.with_name(link.name + ".installer-tmp"),
                                 "bootstrap": self.home / ".router-commands-claude-bootstrap.json",
                                 "bootstrap_legacy_temp": self.home / ".router-commands-claude-bootstrap.json.tmp",
                                 "bootstrap_random_temp": self.home / (".router-commands-claude-bootstrap.json." + "a" * 32 + ".tmp"),
                                 "normalized_runtime": target / "metadata/..",
                                 "data_in_runtime": self.home / ".claude",
                                 "state_ancestor": self.home / "state"}
                    env = {"XDG_STATE_HOME": str(locations[location])}
                    extra = (["--host-home", self.home / "state/router-commands-installer/claude/host"]
                             if location == "state_ancestor" else [])
                    before = self.home_snapshot()
                    refused = self.cli("plan", *extra, env=env, ok=False)
                    self.assertIn("overlap", refused.stderr)
                    self.assertEqual(self.home_snapshot(), before)
                finally:
                    self.home = original

    def test_disappeared_install_cannot_start_new_history_or_release_owners(self):
        for remove_markers in (False, True):
            with self.subTest(remove_markers=remove_markers), tempfile.TemporaryDirectory() as temporary:
                original = self.home
                self.home = canonical_temp_root(temporary) / "home"
                self.home.mkdir()
                try:
                    self.apply()
                    target = self.home / ".claude/router-commands"
                    target.rename(Path(temporary) / "retained-runtime")
                    (self.home / ".local/bin/claude-router").unlink()
                    if remove_markers:
                        for marker in self.module().owner_paths(self.home):
                            marker.unlink()
                    self.write_payload("replacement")
                    before = self.home_snapshot()
                    for action in ("plan", "status", "uninstall"):
                        refused = self.cli(action, ok=False)
                        self.assertIn("history", refused.stderr)
                        self.assertEqual(self.home_snapshot(), before)
                finally:
                    self.home = original

    def test_saved_absent_plan_cannot_bypass_outstanding_committed_history(self):
        self.apply()
        saved = self.base / "plan.json"
        proposal = json.loads(saved.read_text())
        (self.home / ".claude/router-commands").rename(self.base / "retained-runtime")
        (self.home / ".local/bin/claude-router").unlink()
        # A saved plan is untrusted JSON with a consistency digest, not an
        # authorization token. It can describe the absent filesystem accurately
        # and must still fail against the independent installation history.
        module = self.module()
        proposal["parents"] = module.parent_preimages(self.home)
        proposal["owners"] = module.owner_preimages(self.home)
        proposal.pop("plan_sha256")
        proposal["plan_sha256"] = module.digest(module.encoded(proposal))
        saved.write_text(json.dumps(proposal))
        before = self.home_snapshot()
        refused = self.cli("apply", "--plan", saved, ok=False)
        self.assertIn("history", refused.stderr)
        self.assertEqual(self.home_snapshot(), before)

    def test_disjoint_state_can_share_host_ancestor_or_unused_default_runtime(self):
        for state_in_default in (False, True):
            with self.subTest(state_in_default=state_in_default), tempfile.TemporaryDirectory() as temporary:
                original = self.home
                self.home = canonical_temp_root(temporary) / "home"
                self.home.mkdir()
                try:
                    host = self.home / "custom-host"
                    state = self.home / ".claude/router-commands" if state_in_default else host / "state"
                    env = {"XDG_STATE_HOME": str(state)}
                    saved = self.base / "shared-ancestor.json"
                    saved.write_text(json.dumps(self.cli("plan", "--host-home", host, env=env)))
                    self.cli("apply", "--plan", saved, "--host-home", host, env=env)
                    self.assertTrue(self.cli("status", "--host-home", host, env=env)["installed"])
                    self.assertTrue(self.cli("uninstall", "--host-home", host, env=env)["uninstalled"])
                finally:
                    self.home = original

    def test_reinstall_after_owned_uninstall_or_recovery_remains_supported(self):
        self.apply()
        self.cli("uninstall")
        self.write_payload("two")
        self.apply()
        self.cli("rollback")
        self.write_payload("three")
        saved = self.base / "reinstall.json"
        saved.write_text(json.dumps(self.cli("plan")))
        self.cli("apply", "--plan", saved, env={"COMMANDS_INSTALL_CRASH_AT": "after_install"}, ok=False)
        self.cli("recover")
        self.apply()
        self.assertTrue(self.cli("uninstall")["uninstalled"])


if __name__ == "__main__":
    unittest.main()
