"""Router commands profile lifecycle through the public installer."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from _platform import (BASH, IS_WINDOWS, canonical_temp_root, home_env, posix, python_shim, run,
                       skip_unless_symlinks)

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"


def remove_tree(path):
    """shutil.rmtree that also removes read-only files, such as Git for Windows pack files."""
    def make_writable_and_retry(function, name, _error):
        os.chmod(name, stat.S_IWRITE)
        function(name)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


def link_target(path):
    """os.readlink without the \\\\?\\ prefix Windows reports for an absolute target."""
    target = os.readlink(path)
    if IS_WINDOWS and target.startswith("\\\\?\\UNC\\"):
        return "\\" + target[7:]
    if IS_WINDOWS and target.startswith("\\\\?\\"):
        return target[4:]
    return target


class InstallCommandsOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = canonical_temp_root(self.temp)
        self.home = self.base / "home"
        self.home.mkdir()
        self.install_path = INSTALL
        self.env = {**os.environ, **home_env(self.home),
                    "XDG_STATE_HOME": str(self.home / "state"),
                    "TMPDIR": str(self.base), "PYTHONDONTWRITEBYTECODE": "1"}

    def install(self, *args, ok=True):
        result = run([BASH, posix(self.install_path), "--profile", "commands", *args],
                     timeout=25, env=self.env, cwd=self.base, stdin=subprocess.DEVNULL,
                     capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def apply(self, host):
        plan = self.install("--host", host, "--dry-run")
        saved = self.base / (host + ".json")
        saved.write_text(plan.stdout)
        return self.install("--host", host, "--plan", str(saved))

    def command(self, host, *args, env=None):
        launcher = self.home / ".local/bin" / (host + "-router")
        entry = [sys.executable, "-B", str(launcher)] if IS_WINDOWS else [str(launcher)]
        return run([*entry, *args], timeout=10,
                   env={**self.env, **(env or {})}, cwd=self.base,
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def test_dry_run_both_rejected_and_no_home_writes(self):
        self.install("--host", "both", "--dry-run", ok=False)
        self.assertEqual(list(self.home.iterdir()), [])
        planned = self.install("--host", "claude", "--dry-run")
        plan = json.loads(planned.stdout)
        self.assertEqual(plan["host"], "claude")
        self.assertFalse(Path(plan["payload"]).exists())
        self.assertEqual(list(self.home.iterdir()), [])

    @unittest.skipIf(os.name == "nt", "FIFO input is POSIX only")
    def test_saved_plan_fifo_refuses_before_payload_or_home_writes(self):
        fifo = self.base / "plan.fifo"
        os.mkfifo(fifo)
        self.install("--host", "claude", "--plan", str(fifo), ok=False)
        self.assertEqual(list(self.home.iterdir()), [])

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_both_hosts_standalone_and_state_isolation(self):
        bespoke = {}
        for host in ("claude", "codex"):
            base = self.home / ("." + host)
            for relative in ("CLAUDE.md", "settings.json", "hooks/custom.py",
                             "agents/custom.md", "router/routes.json"):
                path = base / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(host + ":" + relative)
                bespoke[path] = path.read_bytes()
        for host in ("claude", "codex"):
            self.apply(host)
            status = self.command(host, "status")
            self.assertEqual(status.returncode, 0, status.stderr)
            data = json.loads(status.stdout)
            self.assertEqual(data["profile"], "commands-only")
            self.assertEqual(data["host"], host)
            self.assertFalse(data["registered_hooks"])
            self.assertEqual(data["host_availability"], "unknown")
            self.assertEqual(data["state"], str(self.home / "state/router-commands" / host))
            self.assertEqual(self.command(host, "help").returncode, 0)
            for allowed in ("operations", "observe", "receipt", "usage", "evaluate"):
                self.assertNotIn("command unavailable", self.command(host, allowed).stderr)
            for forbidden in ("on", "off", "auto", "ladder", "config", "routing"):
                self.assertEqual(self.command(host, forbidden).returncode, 2)
            runtime = self.home / ("." + host) / "router-commands"
            self.assertFalse(any(path.is_symlink() for path in runtime.rglob("*")))
            self.assertFalse((runtime / "hooks/router/common.py").exists())
        self.assertFalse((self.home / "state/claude-router").exists())
        self.assertEqual({path: path.read_bytes() for path in bespoke}, bespoke)
        self.assertFalse((self.home / ".codex/config.toml").exists())

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_installed_launcher_survives_removed_source_checkout(self):
        checkout = self.base / "source-copy"
        shutil.copytree(ROOT, checkout)
        self.install_path = checkout / "install.sh"
        self.apply("claude")
        remove_tree(checkout)
        self.assertFalse(checkout.exists())
        result = self.command("claude", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["profile"], "commands-only")

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_missing_receipt_context_refuses_ambient_replacement(self):
        self.apply("claude")
        runtime = self.home / ".claude/router-commands"
        (runtime / "hooks/router/receipt_context.py").unlink()
        ambient = self.base / "ambient"
        ambient.mkdir()
        marker = self.base / "ambient-executed"
        (ambient / "receipt_context.py").write_text(
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('yes')\n")
        request = self.base / "receipt.json"
        request.write_text(json.dumps({"alignment_digest": "a" * 64, "requirements": ["R1"]}))
        result = self.command("claude", "receipt", "create", "task_1", "--file", str(request),
                              env={"PYTHONPATH": str(ambient), "ROUTER_STATE": str(self.base / "legacy")})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())
        self.assertFalse((self.base / "legacy").exists())

    def test_archive_without_git_can_plan(self):
        archive = self.base / "archive"
        shutil.copytree(ROOT, archive, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        self.install_path = archive / "install.sh"
        planned = self.install("--host", "claude", "--dry-run")
        plan = json.loads(planned.stdout)
        self.assertTrue(plan["source_revision"].startswith("sha256:"))
        saved = self.base / "archive-plan.json"
        saved.write_text(planned.stdout)
        self.install("--host", "claude", "--plan", str(saved))
        status = json.loads(self.command("claude", "status").stdout)
        self.assertEqual(status["source_revision"], plan["source_revision"])

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_missing_dependency_refuses_all_allowed_commands(self):
        self.apply("claude")
        runtime = self.home / ".claude/router-commands"
        missing = runtime / "hooks/router/observation.py"
        original = missing.read_bytes()
        missing.unlink()
        ambient = self.base / "ambient"
        ambient.mkdir()
        marker = self.base / "ambient-executed"
        (ambient / "observation.py").write_text(
            "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('yes')\n")
        try:
            for command in ("operations", "observe", "receipt", "usage", "evaluate",
                            "status", "doctor", "help", "version"):
                with self.subTest(command=command):
                    result = self.command("claude", command, env={"PYTHONPATH": str(ambient)})
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertFalse(marker.exists())
        finally:
            missing.write_bytes(original)

    @unittest.skipIf(IS_WINDOWS, "POSIX executable launch path")
    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_launcher_with_python_name_only(self):
        from _platform import shim
        self.apply("claude")
        path = self.base / "python-only"
        path.mkdir()
        shim(path, "python", sys.executable)
        result = self.command("claude", "version", env={"PATH": str(path)})
        self.assertEqual(result.returncode, 0, result.stderr)

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_interrupted_apply_recovers_without_adopting_runtime(self):
        plan = self.install("--host", "codex", "--dry-run")
        saved = self.base / "interrupted.json"
        saved.write_text(plan.stdout)
        self.env["COMMANDS_INSTALL_CRASH_AT"] = "after_install"
        self.install("--host", "codex", "--plan", str(saved), ok=False)
        del self.env["COMMANDS_INSTALL_CRASH_AT"]
        recovered = json.loads(self.install("--host", "codex", "--recover").stdout)
        self.assertTrue(recovered["recovered"])
        self.assertFalse((self.home / ".local/bin/codex-router").exists())
        self.assertFalse((self.home / ".codex/router-commands").exists())

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_conflict_drift_and_scoped_uninstall(self):
        launcher = self.home / ".local/bin/claude-router"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("private launcher")
        self.install("--host", "claude", "--dry-run", ok=False)
        self.assertEqual(launcher.read_text(), "private launcher")
        launcher.unlink()
        self.apply("claude")
        data = self.home / "state/router-commands/claude/keep.json"
        data.parent.mkdir(parents=True)
        data.write_text("user data")
        runtime = self.home / ".claude/router-commands"
        (runtime / "user.txt").write_text("drift")
        self.install("--host", "claude", "--uninstall", ok=False)
        self.assertTrue((runtime / "user.txt").exists())
        (runtime / "user.txt").unlink()
        self.install("--host", "claude", "--uninstall")
        self.assertEqual(data.read_text(), "user data")
        self.assertFalse(launcher.exists())

    @skip_unless_symlinks("profile target symlink refusal")
    def test_symlinked_profile_target_is_not_adopted(self):
        outside = self.base / "outside"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_text("owned by user")
        host = self.home / ".claude"
        host.mkdir()
        (host / "router-commands").symlink_to(outside, target_is_directory=True)
        self.install("--host", "claude", "--dry-run", ok=False)
        self.assertEqual(marker.read_text(), "owned by user")
        self.assertTrue((host / "router-commands").is_symlink())

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_receipt_store_and_override_ignore_legacy_routing_vars(self):
        self.apply("claude")
        request = self.base / "receipt.json"
        request.write_text(json.dumps({"alignment_digest": "a" * 64,
                                       "requirements": ["R1"]}))
        legacy = self.base / "legacy"
        override = self.base / "explicit"
        hostile = {"ROUTER_STATE": str(legacy), "ROUTER_HOME": str(self.base / "other"),
                   "ROUTES_JSON": str(self.base / "routes.json")}
        result = self.command("claude", "receipt", "create", "task_1", "--file", str(request), env=hostile)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "state/router-commands/claude/receipts.sqlite3").is_file())
        self.assertFalse(legacy.exists())
        self.assertFalse((self.base / "other").exists())
        result = self.command("claude", "receipt", "create", "task_2", "--file", str(request),
                              env={**hostile, "ROUTER_COMMANDS_STATE": str(override)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((override / "receipts.sqlite3").is_file())
        self.assertFalse(legacy.exists())
        declaration = self.base / "usage.json"
        declaration.write_text(json.dumps({"host": "claude", "expected_event_ids": [],
                                           "outcome": "returned"}))
        result = self.command("claude", "usage", "declare", "usage_1", "--file", str(declaration),
                              env=hostile)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "state/router-commands/claude/usage/usage.sqlite3").is_file())
        self.assertFalse(legacy.exists())

    @skip_unless_symlinks("C1 launcher uses a symbolic link")
    def test_repeat_recover_rollback_and_legacy_uninstall(self):
        self.apply("claude")
        repeated = json.loads(self.apply("claude").stdout)
        self.assertTrue(repeated["idempotent"])
        same = json.loads(self.install("--host", "claude", "--plan", str(self.base / "claude.json")).stdout)
        self.assertTrue(same["idempotent"])
        status = json.loads(self.install("--host", "claude", "--status").stdout)
        self.assertEqual((status["product"], status["host"]), ("router", "claude"))
        self.assertFalse(json.loads(self.install("--host", "claude", "--recover").stdout)["recovered"])
        # The full installer can coexist, then leave the commands profile alone.
        full = run([BASH, posix(INSTALL), "--host", "claude"], timeout=60,
                   env=self.env, cwd=self.base, stdin=subprocess.DEVNULL,
                   capture_output=True, text=True)
        self.assertEqual(full.returncode, 0, full.stderr)
        full = run([BASH, posix(INSTALL), "--host", "claude", "--uninstall"], timeout=60,
                   env=self.env, cwd=self.base, stdin=subprocess.DEVNULL,
                   capture_output=True, text=True)
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(self.command("claude", "version").returncode, 0)
        self.install("--host", "claude", "--rollback")
        self.assertFalse((self.home / ".local/bin/claude-router").exists())


HARNESS_DIR = os.environ.get("HARNESS_DIR", "")
IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc")


def snapshot(top):
    """Relative path -> kind, mode and bytes or link text, without following links."""
    found = {}
    for folder, dirs, names in os.walk(top):
        for name in dirs + names:
            path = Path(folder) / name
            info = path.lstat()
            relative = path.relative_to(top).as_posix()
            if stat.S_ISLNK(info.st_mode):
                found[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[relative] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                found[relative] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
    return found


@unittest.skipUnless(HARNESS_DIR and (Path(HARNESS_DIR) / "install.sh").is_file(),
                     "R3 paired gate incomplete: set HARNESS_DIR to the Harness candidate")
class PairedCommandsProfileTests(unittest.TestCase):
    """Router and Harness commands profiles installed into one disposable home."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="paired-commands-")
        self.addCleanup(self.temp.cleanup)
        self.base = canonical_temp_root(self.temp)
        self.home = self.base / "home"
        self.home.mkdir()
        shims = self.base / "python-shim"
        shims.mkdir()
        python_shim(shims)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("CLAUDE_HOME", "CODEX_HOME", "PYTHONPATH", "COMMANDS_INSTALL_CRASH_AT",
                                   "ROUTER_COMMANDS_STATE", "HARNESS_COMMANDS_STATE")
                    and not key.startswith("ROUTER_")}
        self.env.update(home_env(self.home))
        self.env.update({"XDG_STATE_HOME": str(self.home / "state"),
                         "XDG_CONFIG_HOME": str(self.home / "config"), "TMPDIR": str(self.base),
                         "PYTHONDONTWRITEBYTECODE": "1",
                         "PATH": str(shims) + os.pathsep + os.environ.get("PATH", os.defpath)})
        self.source = {"router": ROOT, "harness": Path(HARNESS_DIR)}

    def bespoke(self):
        """Synthetic unrelated files, links and data that no commands profile may touch."""
        for host in ("claude", "codex"):
            for relative in ("CLAUDE.md", "settings.json", "hooks.json", "hooks/custom.py",
                             "agents/custom.md", "custom/routes.json", "unrelated.txt"):
                path = self.home / ("." + host) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"unrelated_setting": "keep"}' if relative.endswith(".json")
                               and relative != "custom/routes.json" else host + ":" + relative)
        outside = self.base / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "keep.txt").write_text("owned by user")
        (self.home / ".claude/linked").symlink_to(outside, target_is_directory=True)
        (self.home / ".local/bin").mkdir(parents=True, exist_ok=True)
        (self.home / ".local/bin/custom-tool").write_text("#!/bin/sh\necho custom\n")
        (self.home / ".local/bin/custom-link").symlink_to(self.home / ".local/bin/custom-tool")
        (self.home / "state/custom").mkdir(parents=True)
        (self.home / "state/custom/data.json").write_text("{}")
        return snapshot(self.home)

    def preserved(self, before):
        after = snapshot(self.home)
        for relative, value in before.items():
            self.assertEqual(after.get(relative), value, relative)

    def profile(self, product, *args, source=None, ok=True):
        result = run([BASH, posix((source or self.source[product]) / "install.sh"), "--profile", "commands", *args],
                     timeout=90, env=self.env, cwd=self.base, stdin=subprocess.DEVNULL,
                     capture_output=True, text=True)
        if ok:
            self.assertEqual(result.returncode, 0, product + ": " + result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, product + ": " + result.stdout)
        return result

    def apply(self, product, host, source=None):
        plan = self.profile(product, "--host", host, "--dry-run", source=source)
        saved = self.base / (product + "-" + host + ".json")
        saved.write_text(plan.stdout)
        return self.profile(product, "--host", host, "--plan", str(saved), source=source)

    def launch(self, product, host, *args, env=None):
        launcher = self.home / ".local/bin" / (host + "-" + product)
        entry = [sys.executable, "-B", str(launcher)] if IS_WINDOWS else [str(launcher)]
        return run([*entry, *args], timeout=20, env={**self.env, **(env or {})}, cwd=self.base,
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def check_installed(self, product, host):
        data = json.loads(self.launch(product, host, "status").stdout)
        self.assertEqual((data["profile"], data["host"]), ("commands-only", host))
        self.assertEqual(data["state"], str(self.home / "state" / (product + "-commands") / host))
        self.assertIs(data["registered_hooks"], False)
        self.assertEqual(self.launch(product, host, "version").stdout.strip(),
                         (self.source[product] / "VERSION").read_text().strip())
        manifest = json.loads((self.home / ("." + host) / (product + "-commands/manifest.json")).read_text())
        self.assertEqual(manifest["source_revision"], data["source_revision"])
        return data

    def test_shared_helper_is_byte_identical(self):
        self.assertEqual((ROOT / "scripts/commands_install.py").read_bytes(),
                         (Path(HARNESS_DIR) / "scripts/commands_install.py").read_bytes())

    @skip_unless_symlinks("commands launchers are symbolic links")
    def test_both_install_orders_and_hosts_keep_each_other_and_bespoke_files(self):
        for order in (("router", "harness"), ("harness", "router")):
            with self.subTest(order=order):
                shutil.rmtree(self.home)
                self.home.mkdir()
                before = self.bespoke()
                for host in ("claude", "codex"):
                    for product in order:
                        self.apply(product, host)
                        self.preserved(before)
                for host in ("claude", "codex"):
                    states = set()
                    for product in order:
                        states.add(self.check_installed(product, host)["state"])
                        self.assertTrue((self.home / ("." + host) / (product + "-commands")).is_dir())
                    self.assertEqual(len(states), 2)
                # Repeat apply is idempotent for both; reversing one leaves the other working.
                for product in order:
                    saved = self.base / (product + "-claude.json")
                    self.assertTrue(json.loads(self.profile(product, "--host", "claude",
                                                            "--plan", str(saved)).stdout)["idempotent"])
                    self.assertFalse(json.loads(self.profile(product, "--host", "claude",
                                                             "--recover").stdout)["recovered"])
                for product in order:
                    self.profile(product, "--host", "claude", "--uninstall")
                    other = order[1 - order.index(product)]
                    self.assertFalse((self.home / ".local/bin" / ("claude-" + product)).exists())
                    self.assertFalse((self.home / ".claude" / (product + "-commands")).exists())
                    if product == order[0]:
                        self.check_installed(other, "claude")
                    self.check_installed(order[0], "codex")
                self.preserved(before)

    @skip_unless_symlinks("commands launchers are symbolic links")
    def test_launchers_work_with_both_sources_removed_and_state_stays_isolated(self):
        copies = {}
        for product in ("router", "harness"):
            copies[product] = self.base / ("source-" + product)
            shutil.copytree(self.source[product], copies[product], ignore=IGNORE)
        for product in ("router", "harness"):
            self.apply(product, "claude", source=copies[product])
        for copy in copies.values():
            shutil.rmtree(copy)
        for product in ("router", "harness"):
            self.check_installed(product, "claude")
            self.assertEqual(self.launch(product, "claude", "help").returncode, 0)
            self.assertEqual(self.launch(product, "claude", "on").returncode, 2)
        request = self.base / "usage.json"
        request.write_text(json.dumps({"host": "claude", "expected_event_ids": [], "outcome": "returned"}))
        for product in ("router", "harness"):
            result = self.launch(product, "claude", "usage", "declare", "usage_1", "--file", str(request),
                                 env={"ROUTER_STATE": str(self.base / "legacy")})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((self.home / "state" / (product + "-commands/claude")).is_dir())
        self.assertFalse((self.base / "legacy").exists())
        self.assertFalse((self.home / "state/claude-router").exists())

    @skip_unless_symlinks("commands launchers are symbolic links")
    def test_full_installs_and_legacy_uninstall_leave_both_profiles_alone(self):
        before = self.bespoke()
        for product in ("router", "harness"):
            self.apply(product, "claude")
        owned = {product: snapshot(self.home / ".claude" / (product + "-commands"))
                 for product in ("router", "harness")}
        launchers = {product: os.readlink(self.home / ".local/bin" / ("claude-" + product))
                     for product in ("router", "harness")}
        for product in ("router", "harness"):
            full = run([BASH, posix(self.source[product] / "install.sh"), "--host", "claude"], timeout=120,
                       env=self.env, cwd=self.base, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(full.returncode, 0, product + full.stderr)
        for product in ("router", "harness"):
            self.assertEqual(snapshot(self.home / ".claude" / (product + "-commands")), owned[product])
        for product in ("harness", "router"):
            full = run([BASH, posix(self.source[product] / "install.sh"), "--host", "claude", "--uninstall"],
                       timeout=120, env=self.env, cwd=self.base, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True)
            self.assertEqual(full.returncode, 0, product + full.stderr)
            for name in ("router", "harness"):
                self.assertEqual(snapshot(self.home / ".claude" / (name + "-commands")), owned[name])
                self.assertEqual(os.readlink(self.home / ".local/bin" / ("claude-" + name)), launchers[name])
                self.check_installed(name, "claude")
        for product in ("router", "harness"):
            self.profile(product, "--host", "claude", "--uninstall")
        self.assertEqual(list((self.home / ".claude").glob("*-commands")), [])
        self.preserved(before)
        self.assertEqual((self.base / "outside/keep.txt").read_text(), "owned by user")

    def full(self, product, *args):
        result = run([BASH, posix(self.source[product] / "install.sh"), "--host", "claude", *args],
                     timeout=120, env=self.env, cwd=self.base, stdin=subprocess.DEVNULL,
                     capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, product + result.stdout + result.stderr)

    @skip_unless_symlinks("commands launchers are symbolic links")
    def test_profile_after_full_install_for_both_products_in_both_orders(self):
        for order in (("router", "harness"), ("harness", "router")):
            with self.subTest(order=order):
                shutil.rmtree(self.home)
                self.home.mkdir()
                before = self.bespoke()
                for product in order:
                    self.full(product)
                for product in order:
                    self.apply(product, "claude")
                    self.check_installed(product, "claude")
                for product in order:
                    self.assertTrue(json.loads(self.profile(product, "--host", "claude", "--plan",
                                                            str(self.base / (product + "-claude.json"))).stdout)["idempotent"])
                owned = {product: snapshot(self.home / ".claude" / (product + "-commands")) for product in order}
                for product in order:
                    self.full(product, "--uninstall")
                    for name in order:
                        self.assertEqual(snapshot(self.home / ".claude" / (name + "-commands")), owned[name])
                        self.check_installed(name, "claude")
                self.preserved({key: value for key, value in before.items()
                                if not key.startswith(".local/bin/") or key.startswith(".local/bin/custom")})
                for product in order:
                    self.profile(product, "--host", "claude", "--uninstall")
                    self.assertFalse((self.home / ".claude" / (product + "-commands")).exists())
                self.assertEqual(list((self.home / ".claude").glob("*-commands")), [])
                self.preserved(before)

    @skip_unless_symlinks("commands launchers are symbolic links")
    def test_explicit_launcher_conflicts_refuse_only_their_product(self):
        before = self.bespoke()
        bin_dir = self.home / ".local/bin"
        private = self.base / "private-launcher"
        private.write_text("#!/bin/sh\necho private\n")
        (bin_dir / "claude-router").write_text("private launcher")
        (bin_dir / "codex-harness").symlink_to(private)
        guarded = snapshot(self.home)
        self.profile("router", "--host", "claude", "--dry-run", ok=False)
        self.profile("harness", "--host", "codex", "--dry-run", ok=False)
        self.assertEqual(snapshot(self.home), guarded)
        self.apply("harness", "claude")
        self.apply("router", "codex")
        self.check_installed("harness", "claude")
        self.check_installed("router", "codex")
        for product, host in (("router", "claude"), ("harness", "codex")):
            self.profile(product, "--host", host, "--dry-run", ok=False)
        self.assertEqual((bin_dir / "claude-router").read_text(), "private launcher")
        self.assertEqual(link_target(bin_dir / "codex-harness"), str(private))
        self.profile("harness", "--host", "claude", "--uninstall")
        self.profile("router", "--host", "codex", "--uninstall")
        self.assertEqual(private.read_text(), "#!/bin/sh\necho private\n")
        self.assertEqual((bin_dir / "claude-router").read_text(), "private launcher")
        self.preserved({key: value for key, value in before.items()})

    @skip_unless_symlinks("commands launchers are symbolic links")
    def test_upgrade_interrupt_recover_and_reverse_both_products(self):
        before = self.bespoke()
        for product in ("router", "harness"):
            self.apply(product, "codex")
        first = {product: self.check_installed(product, "codex") for product in ("router", "harness")}
        runtimes = {product: snapshot(self.home / ".codex" / (product + "-commands"))
                    for product in ("router", "harness")}
        newer = {}
        for product in ("router", "harness"):
            newer[product] = self.base / ("newer-" + product)
            shutil.copytree(self.source[product], newer[product], ignore=IGNORE)
            version = (newer[product] / "VERSION").read_text().strip() + "-r3paired"
            (newer[product] / "VERSION").write_text(version + "\n")
            plan = self.profile(product, "--host", "codex", "--dry-run", source=newer[product])
            saved = self.base / (product + "-upgrade.json")
            saved.write_text(plan.stdout)
            self.env["COMMANDS_INSTALL_CRASH_AT"] = "after_install"
            self.profile(product, "--host", "codex", "--plan", str(saved), source=newer[product], ok=False)
            del self.env["COMMANDS_INSTALL_CRASH_AT"]
            self.assertTrue(json.loads(self.profile(product, "--host", "codex", "--recover",
                                                    source=newer[product]).stdout)["recovered"])
            self.assertEqual(snapshot(self.home / ".codex" / (product + "-commands")), runtimes[product])
            self.profile(product, "--host", "codex", "--plan", str(saved), source=newer[product])
            self.assertTrue(self.launch(product, "codex", "version").stdout.strip().endswith("-r3paired"))
        for product in ("router", "harness"):
            self.assertNotEqual(json.loads(self.launch(product, "codex", "status").stdout)["source_revision"],
                                first[product]["source_revision"])
            self.profile(product, "--host", "codex", "--rollback")
            self.assertEqual(snapshot(self.home / ".codex" / (product + "-commands")), runtimes[product])
            self.check_installed(product, "codex")
        for product in ("router", "harness"):
            self.profile(product, "--host", "codex", "--rollback")
            self.assertFalse((self.home / ".local/bin" / ("codex-" + product)).exists())
        self.preserved(before)


if __name__ == "__main__":
    unittest.main()
