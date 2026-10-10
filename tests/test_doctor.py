#!/usr/bin/env python3
"""Standalone health-report checks with isolated host homes."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
CHECK_ROW = re.compile(r"\[(?:ok|warn|FAIL)\] [a-z ]+: .+|  (?:edited|missing): .+|  .+ \(retired by an earlier release\)")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude = self.base / "claude"
        self.codex = self.base / "codex"
        self.state = self.base / "state" / "claude-router"
        self.config = self.base / "config"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ROUTER_", "CLAUDE_", "CODEX_", "XDG_"))}
        self.env.update(HOME=str(self.base), CLAUDE_HOME=str(self.claude), CODEX_HOME=str(self.codex),
                        XDG_CONFIG_HOME=str(self.config), XDG_STATE_HOME=str(self.base / "state"),
                        ROUTER_LOCAL="off")

    def run_doctor(self, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / "bin/router"), "doctor", *args],
                              env=env or self.env, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=30)

    def install(self, host="claude", tree=ROOT):
        result = subprocess.run(["bash", str(tree / "install.sh"), "--host", host],
                                env=self.env, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=50)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def old_tree(self, name):
        value = os.environ.get(name)
        self.assertTrue(value, f"{name} is unset; source old-trees.sh before this test")
        tree = Path(value)
        self.assertTrue((tree / "install.sh").is_file(), f"{name} has no installer")
        return tree

    def lines(self, result):
        self.assertEqual(result.stderr, "")
        rows = result.stdout.splitlines()
        self.assertRegex(rows[-1], r"^doctor: \d+ ok, \d+ warn, \d+ fail$")
        failures = sum(row.startswith("[FAIL]") for row in rows)
        self.assertEqual(result.returncode, bool(failures), result.stdout)
        return rows

    def expect(self, rows, text):
        self.assertTrue(any(re.fullmatch(re.escape(text), row) for row in rows), (text, rows))

    def expect_re(self, rows, pattern):
        self.assertTrue(any(re.fullmatch(pattern, row) for row in rows), (pattern, rows))

    def inproc(self, *args, env=None, patches=()):
        import cmd_doctor
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ, env or self.env, clear=True))
            for target, name, value in patches:
                stack.enter_context(mock.patch.object(target, name, value))
            with contextlib.redirect_stdout(out):
                code = cmd_doctor.main(list(args))
        rows = out.getvalue().splitlines()
        self.assertRegex(rows[-1], r"^doctor: \d+ ok, \d+ warn, \d+ fail$")
        self.assertEqual(code, int(any(row.startswith("[FAIL]") for row in rows)), out.getvalue())
        return rows

    def manifest_names(self, prefix):
        manifest = json.loads((self.claude / "router/install-manifest.json").read_text())
        return [name for name in manifest["files"] if name.startswith(prefix)]

    def locked(self, path):
        if os.geteuid() == 0:
            self.skipTest("running as root: chmod 000 does not deny access")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o755)

    def snapshot(self):
        result = {}
        for root in (self.state, self.claude, self.codex):
            if root.exists():
                for path in [root, *root.rglob("*")]:
                    info = path.lstat()
                    payload = (path.read_bytes() if path.is_file() and not path.is_symlink() else
                               os.readlink(path).encode() if path.is_symlink() else b"")
                    result[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns,
                                         hashlib.sha256(payload).hexdigest())
        return result

    def test_absent_manifest_and_exit_rule(self):
        rows = self.lines(self.run_doctor())
        self.assertIn(f"Router doctor (claude): {self.claude}", rows)
        self.assertIn(f"[FAIL] install: no manifest at {self.claude / 'router/install-manifest.json'}; run install.sh", rows)
        self.assertIn("[warn] version: not installed, checkout " + (ROOT / "VERSION").read_text().strip(), rows)

    def test_healthy_and_read_only(self):
        self.install()
        before = self.snapshot()
        rows = self.lines(self.run_doctor())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(rows[0], f"Router doctor (claude): {self.claude}")
        self.assertIn("[ok] version: installed 0.3.0, checkout 0.3.0 (current)", rows)
        self.assertIn("[ok] skew: none", rows)
        self.assertIn("[ok] overlay: off (ROUTER_LOCAL=off)", rows)
        self.assertIn("[ok] spawn: dry run allowed a sweeper brief", rows)
        self.assertEqual(self.run_doctor().returncode, 0)

    def test_path_and_budget_match_direct_script(self):
        self.install()
        env = dict(self.env, PATH=str(self.claude / "router/bin") + os.pathsep + self.env.get("PATH", ""))
        rows = self.lines(self.run_doctor(env=env))
        self.assertIn(f"[ok] path: router found at {self.claude / 'router/bin/router'}", rows)
        budget = subprocess.run([sys.executable, str(ROOT / "scripts/budget.py"), "--home", str(self.claude),
                                 "--host", "claude", "--json"], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(budget.returncode, 0, budget.stderr)
        row = json.loads(budget.stdout)["trees"][0]
        self.assertIn(f"[ok] budget: {row['label']} {row['tokens']} tokens always-on (estimate, ceiling 650)", rows)

    def test_missing_hook_fails(self):
        self.install()
        (self.claude / "hooks/router/spawn_guard.py").unlink()
        rows = self.lines(self.run_doctor())
        self.assertIn("[FAIL] hooks: spawn_guard.py is registered but missing", rows)

    def test_retired_files_and_old_version(self):
        self.install(tree=self.old_tree("ROUTER_V02"))
        rows = self.lines(self.run_doctor())
        self.assertIn("[warn] version: installed 0.2.0 or earlier, checkout 0.3.0", rows)
        self.install()
        retired = self.claude / "agents/Explore.md"
        retired.write_text("retired")
        rows = self.lines(self.run_doctor())
        self.assertIn("[warn] leftovers: 1", rows)
        self.assertIn(f"  {retired} (retired by an earlier release)", rows)

    def test_sibling_skew(self):
        self.install(tree=self.old_tree("HARNESS_V02"))
        self.install()
        rows = self.lines(self.run_doctor())
        self.assertIn("[warn] skew: shared roles come from Harness 0.2.0 or earlier; upgrade it for the 0.3 roles", rows)
        sibling = self.claude / "harness/install-manifest.json"
        sibling.write_text("{")
        rows = self.lines(self.run_doctor())
        self.assertIn("[warn] sibling: Harness unknown", rows)

    def test_installed_doctor_finds_checkout_skew_or_says_unknown(self):
        self.install("claude")
        self.install("codex")
        checkout = self.base / "checkout"
        for host, home, name in (("claude", self.claude, "agents/sweeper.md"),
                                 ("codex", self.codex, "agents/sweeper.toml")):
            (checkout / "codex/agents").mkdir(parents=True)
            (checkout / "agents").mkdir(exist_ok=True)
            (checkout / "VERSION").write_text("0.3.0\n")
            (checkout / "install.sh").write_text("# checkout marker\n")
            source = checkout / (name if host == "claude" else "codex/" + name)
            source.write_bytes((home / name).read_bytes() + b"\ncheckout edit\n")
            manifest = home / "router/install-manifest.json"
            value = json.loads(manifest.read_text())
            value["source"] = str(checkout)
            manifest.write_text(json.dumps(value))
            sibling = home / "harness/install-manifest.json"
            sibling.parent.mkdir(exist_ok=True)
            sibling.write_text(json.dumps({"version": 1, "package": "0.2.5",
                                           "files": {name: "0" * 64}, "hooks": {}}))
            installed = home / "router/bin/router"
            run = lambda: subprocess.run([sys.executable, str(installed), "doctor", "--host", host],
                                         env=self.env, capture_output=True, text=True,
                                         stdin=subprocess.DEVNULL, timeout=30)
            self.expect(self.lines(run()),
                        "[warn] skew: shared roles come from Harness 0.2.5; upgrade it for the 0.3 roles")
            shutil.rmtree(checkout)
            self.expect(self.lines(run()), "[warn] skew: unknown (checkout not found)")

    def test_edited_missing_and_last_write(self):
        self.install()
        manifest = json.loads((self.claude / "router/install-manifest.json").read_text())
        victim = self.claude / next(name for name in manifest["files"] if name.startswith("hooks/router/"))
        victim.write_text("edited")
        missing = self.claude / next(name for name in manifest["files"] if name.startswith("agents/"))
        missing.unlink()
        self.state.mkdir(parents=True)
        record = self.state / "events.jsonl"
        record.write_text("{}\n")
        rows = self.lines(self.run_doctor())
        current = len(manifest["files"]) - 2
        self.assertIn(f"[FAIL] files: 1 edited, 1 missing, {current} current", rows)
        self.assertIn(f"  edited: {victim}", rows)
        self.assertIn(f"  missing: {missing}", rows)
        self.assertTrue(any(re.fullmatch(r"\[ok\] last hook write: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \(" + re.escape(str(record)) + r"\)", row) for row in rows))

    def test_bad_settings_and_manifest(self):
        self.install()
        settings = self.claude / "settings.json"
        settings.write_text("{")
        rows = self.lines(self.run_doctor())
        self.assertIn("[FAIL] hooks: settings.json is not valid JSON", rows)
        manifest = self.claude / "router/install-manifest.json"
        manifest.unlink()
        manifest.mkdir()
        rows = self.lines(self.run_doctor())
        self.assertIn("[warn] version: installed unknown, checkout 0.3.0", rows)

    def test_overlay_bad_shapes_and_no_marker(self):
        self.install()
        local = self.config / "router/routes.local.json"
        local.parent.mkdir(parents=True)
        env = dict(self.env, ROUTER_LOCAL=str(local))
        try:
            json.loads("{")
        except ValueError as exc:
            bad_json = str(exc)
        for payload, detail in ((b"{", f"not valid JSON: {bad_json}"), (b"[]", "must be a JSON object"),
                                (b'{"version":2}', "version: cannot change"),
                                (b'{"context":{"caps":{"exec":-1}}}', "context.caps.exec: cap must be a positive int, got -1"),
                                (b"[" * 20000 + b"]" * 20000, "nested too deeply"),
                                (b"x" * 65537, "larger than 65536 bytes")):
            local.write_bytes(payload)
            before = self.snapshot()
            rows = self.lines(self.run_doctor(env=env))
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(sum(row.startswith("[FAIL] overlay:") for row in rows), 1)
            self.expect(rows, f"[FAIL] overlay: invalid {local}: {detail}")
            self.assertFalse((self.state / "overlay-warned").exists())
        local.unlink()
        local.mkdir()
        rows = self.lines(self.run_doctor(env=env))
        self.expect(rows, f"[FAIL] overlay: invalid {local}: not a regular file")

    def test_overlay_ok_and_missing(self):
        self.install()
        local = self.config / "router/routes.local.json"
        local.parent.mkdir(parents=True)
        env = dict(self.env, ROUTER_LOCAL=str(local))
        rows = self.lines(self.run_doctor(env=env))
        self.assertIn(f"[FAIL] overlay: invalid {local}: overlay file not found", rows)
        local.write_text('{"router":{"modes":{"inject":"off"}}}')
        rows = self.lines(self.run_doctor(env=env))
        self.assertIn(f"[ok] overlay: ok {local} (1 keys changed)", rows)

    def test_invalid_shipped_table_names_file_once(self):
        self.install()
        routes = self.base / "broken-routes.json"
        routes.write_text("{}")
        env = dict(self.env, ROUTES_JSON=str(routes))
        rows = self.lines(self.run_doctor(env=env))
        report = [row for row in rows if row.startswith("[FAIL] routes:")]
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0], f"[FAIL] routes: {routes}: is invalid: missing top-level key 'version'; "
                         "missing top-level key 'tiers'; missing top-level key 'router'; missing top-level key 'context'")
        self.assertIn("[FAIL] spawn: dry run failed (RoutesError)", rows)

    def test_errors_and_codex_trust(self):
        self.install("codex")
        self.state.mkdir(parents=True)
        (self.state / "errors.jsonl").write_text('{"ts":"2026-01-01T00:00:00Z","script":"hook","class":"ValueError","message":"secret"}\nnope\n{"ts":"2026-01-02T00:00:00Z","script":"guard","class":"OSError","message":"secret"}\n')
        rows = self.lines(self.run_doctor("--host", "codex"))
        self.assertIn("[warn] hooks trust: unreadable on Codex (hook trust state is not visible to doctor)", rows)
        self.assertIn("[warn] errors: 2 in errors.jsonl, last: 2026-01-02T00:00:00Z guard OSError", rows)
        self.assertNotIn("secret", "\n".join(rows))

    def test_both_hosts_and_argparse(self):
        self.install("both")
        rows = self.lines(self.run_doctor())
        self.assertIn(f"Router doctor (claude): {self.claude}", rows)
        self.assertIn(f"Router doctor (codex): {self.codex}", rows)
        self.assertEqual(sum(row.startswith("Router doctor (") for row in rows), 2)
        bad = self.run_doctor("--host", "other")
        self.assertEqual(bad.returncode, 2)

    def test_bad_file_matrix_has_no_traceback(self):
        self.install()
        version = (ROOT / "VERSION").read_text().strip()
        settings, manifest, errors = (self.claude / "settings.json", self.claude / "router/install-manifest.json",
                                      self.state / "errors.jsonl")
        bad_manifest = [f"[FAIL] install: manifest at {manifest} is not readable",
                        "[FAIL] agents: 0 of 0 role files present",
                        f"[warn] version: installed unknown, checkout {version}",
                        "[warn] files: unknown (install manifest unreadable)"]
        unreadable_errors = f"[warn] errors: unavailable (errors.jsonl is not readable: {errors}, PermissionError)"
        expected = {
            (settings, "directory"): ["[FAIL] hooks: settings.json is not readable"],
            (settings, "deep"): ["[FAIL] hooks: settings.json is nested too deeply"],
            (settings, "binary"): ["[FAIL] hooks: settings.json is not valid JSON"],
            (settings, "unreadable"): ["[FAIL] hooks: settings.json is not readable"],
            (manifest, "directory"): bad_manifest, (manifest, "deep"): bad_manifest,
            (manifest, "binary"): bad_manifest, (manifest, "unreadable"): bad_manifest,
            (errors, "directory"): [f"[warn] errors: unavailable (errors.jsonl is not readable: {errors}, IsADirectoryError)"],
            (errors, "deep"): ["[ok] errors: none"], (errors, "binary"): ["[ok] errors: none"],
            (errors, "unreadable"): [unreadable_errors],
        }
        self.state.mkdir(parents=True, exist_ok=True)
        for path in (settings, manifest, errors):
            original = path.read_bytes() if path.is_file() else None
            try:
                for kind in ("directory", "deep", "binary", "unreadable"):
                    if kind == "unreadable" and os.geteuid() == 0:
                        continue
                    if path.is_dir():
                        path.rmdir()
                    elif path.exists():
                        path.chmod(0o600)
                        path.unlink()
                    if kind == "directory":
                        path.mkdir()
                    elif kind == "deep":
                        path.write_bytes(b"[" * 100000 + b"]" * 100000)
                    elif kind == "binary":
                        path.write_bytes(b"\xff\xfe\x00")
                    else:
                        path.write_text("{}")
                        path.chmod(0)
                    result = self.run_doctor()
                    self.assertEqual(result.stderr, "", (path, kind, result.stderr))
                    self.assertNotIn("Traceback", result.stdout)
                    rows = self.lines(result)
                    for row in rows[1:-1]:
                        self.assertTrue(re.fullmatch(CHECK_ROW, row), row)
                    for line in expected[(path, kind)]:
                        self.expect(rows, line)
                    if path.exists() and not path.is_dir():
                        path.chmod(0o600)
            finally:
                if path.is_dir():
                    path.rmdir()
                elif path.exists():
                    path.chmod(0o600)
                    path.unlink()
                if original is not None:
                    path.write_bytes(original)

    def test_budget_missing_is_only_a_warning(self):
        self.install()
        installed = self.claude / "router/bin/budget.py"
        installed.unlink()
        copy = self.base / "checkout"
        (copy / "hooks/router").mkdir(parents=True)
        (copy / "bin").mkdir()
        shutil.copy2(ROOT / "bin/router", copy / "bin/router")
        for name in ("cmd_doctor.py", "common.py", "overlay.py", "routes.json"):
            shutil.copy2(ROOT / "hooks/router" / name, copy / "hooks/router" / name)
        (copy / "VERSION").write_text("0.3.0\n")
        result = subprocess.run([sys.executable, str(copy / "bin/router"), "doctor"], env=self.env,
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15)
        rows = self.lines(result)
        self.assertIn("[warn] budget: unavailable (scripts/budget.py is missing)", rows)

    def test_budget_failure(self):
        self.install()
        installed_budget = self.claude / "router/bin/budget.py"
        if installed_budget.exists():
            installed_budget.unlink()
        copy = self.base / "checkout"
        (copy / "hooks/router").mkdir(parents=True)
        (copy / "scripts").mkdir()
        shutil.copy2(ROOT / "hooks/router/cmd_doctor.py", copy / "hooks/router/cmd_doctor.py")
        for name in ("common.py", "overlay.py", "routes.json"):
            shutil.copy2(ROOT / "hooks/router" / name, copy / "hooks/router" / name)
        (copy / "VERSION").write_text("0.3.0\n")
        (copy / "bin").mkdir()
        shutil.copy2(ROOT / "bin/router", copy / "bin/router")
        for stub in ("raise SystemExit(1)", "import sys; sys.exit(0)", "print('not json')", "import os; os._exit(0)",
                     "import time; time.sleep(3)"):
            (copy / "scripts/budget.py").write_text(stub)
            env = dict(self.env, ROUTER_DOCTOR_BUDGET_TIMEOUT="1")
            result = subprocess.run([sys.executable, str(copy / "bin/router"), "doctor", "--host", "claude"],
                                    env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10)
            rows = self.lines(result)
            self.assertIn("[warn] budget: unavailable (scripts/budget.py failed)", rows)



    # Fix 1: unreadable directories give one check line each and the run reaches the closing line.
    def test_unreadable_agents_directory(self):
        self.install()
        names = self.manifest_names("agents/")
        self.locked(self.claude / "agents")
        rows = self.lines(self.run_doctor())
        self.expect(rows, f"[FAIL] agents: 0 of {len(names)} role files present")
        self.expect(rows, f"[FAIL] agents: {self.claude / names[0]} is not readable (PermissionError)")
        self.assertEqual(self.run_doctor().returncode, 1)

    def test_unreadable_hooks_directory(self):
        self.install()
        settings = (self.claude / "settings.json").read_text()
        scripts = re.findall(r"/hooks/router/([A-Za-z_][A-Za-z_0-9]*\.py)", settings)
        folder = self.claude / "hooks/router"
        self.locked(folder)
        for mode in (0, 0o444):  # Neither lets a hook file be opened: one line names the directory.
            with self.subTest(mode=oct(mode)):
                folder.chmod(mode)
                result = self.run_doctor()
                rows = self.lines(result)
                self.assertEqual(result.returncode, 1)
                self.assertEqual([row for row in rows if row.startswith("[FAIL] hooks: ")],
                                 [f"[FAIL] hooks: 0 of {len(scripts)} registered",
                                  f"[FAIL] hooks: {folder} is not readable (PermissionError)"])

    def test_unreadable_state_directory(self):
        self.install()
        self.state.mkdir(parents=True)
        self.locked(self.state)
        result = self.run_doctor()
        rows = self.lines(result)
        self.assertEqual(result.returncode, 0)
        self.expect(rows, f"[warn] last hook write: unknown (state directory not readable: {self.state}, PermissionError)")
        self.expect(rows, f"[warn] errors: unavailable (errors.jsonl is not readable: {self.state / 'errors.jsonl'}, PermissionError)")

    # Plugin state (3P1 fix 4): one line per Router plugin, doubled roles, malformed input; read-only.
    def base_hash(self):
        digest = hashlib.sha256()
        for path in sorted(self.base.rglob("*")):
            info = path.lstat()
            digest.update(f"{path}\0{info.st_mode}\0{info.st_mtime_ns}\0".encode())
            if path.is_file() and not path.is_symlink() and os.access(path, os.R_OK):
                digest.update(path.read_bytes())
        return digest.hexdigest()

    def plugin_rows(self, rows=None):
        before = self.base_hash()
        rows = rows or self.lines(self.run_doctor())
        self.assertEqual(self.base_hash(), before, "doctor wrote below the scratch home")
        return [row for row in rows if re.fullmatch(r"\[(?:ok|warn|FAIL)\] plugin: .+", row)]

    def enable(self, *plugins):
        settings = self.claude / "settings.json"
        value = json.loads(settings.read_text()) if settings.exists() else {}
        value["enabledPlugins"] = {f"{name}@router": True for name in plugins}
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps(value))

    def cache(self, plugin, version):
        folder = self.claude / "plugins/cache/router" / plugin / version
        folder.mkdir(parents=True)
        return folder

    def test_plugin_not_enabled(self):
        self.assertEqual(self.plugin_rows(), ["[ok] plugin: router not enabled, not cached",
                                              "[ok] plugin: shared-roles not enabled, not cached"])
        self.assertFalse(self.claude.exists())

    def test_plugin_enabled_cached_and_stale(self):
        version = (ROOT / "VERSION").read_text().strip()
        self.enable("router", "shared-roles")
        self.cache("router", version)
        self.cache("shared-roles", "0.2.0")
        self.assertEqual(self.plugin_rows(), [
            f"[ok] plugin: router enabled, cached {version} (current)",
            f"[warn] plugin: shared-roles enabled, cached 0.2.0 (stale, checkout {version})"])
        self.cache("shared-roles", version)
        self.assertEqual(self.plugin_rows()[1], f"[ok] plugin: shared-roles enabled, cached 0.2.0, {version} (current)")

    def test_plugin_script_install_and_doubled_roles(self):
        self.install()
        self.enable("router", "shared-roles")
        roles = "builder, builder-in-place, docs-writer, judge, planner, researcher, sweeper, test-writer, worker"
        self.assertEqual(self.plugin_rows(), [
            "[ok] plugin: router enabled, not cached; stands down: script install present",
            "[ok] plugin: shared-roles enabled, not cached",
            f"[warn] plugin: roles in both {self.claude / 'agents'} and shared-roles: {roles} "
            "(doubled descriptions cost tokens every turn)"])
        (self.claude / "agents/judge.md").unlink()
        self.enable("router")
        self.cache("shared-roles", "0.2.0")
        self.assertEqual(self.plugin_rows()[2], f"[warn] plugin: roles in both {self.claude / 'agents'} and "
                         "shared-roles: " + roles.replace(" judge,", "") + " (doubled descriptions cost tokens every turn)")
        self.enable()
        (self.claude / "plugins").chmod(0o755)
        shutil.rmtree(self.claude / "plugins")
        self.assertEqual(len(self.plugin_rows()), 2, "no doubled-roles line without the shared-roles plugin")

    def test_plugin_malformed_shapes(self):
        settings = self.claude / "settings.json"
        settings.parent.mkdir(parents=True)
        unknown = ["[ok] plugin: router enabled unknown, not cached", "[ok] plugin: shared-roles enabled unknown, not cached"]
        for text, error in (("{", "is not valid JSON"), ("[]", "is not a JSON object"), ("7", "is not a JSON object"),
                            ('{"enabledPlugins": "router@router"}', "enabledPlugins is not an object"),
                            ('{"enabledPlugins": {"shared-roles@router": 1}}',
                             'enabledPlugins["shared-roles@router"] is not true or false'),
                            (b"\xff\xfe", "is not valid JSON"),
                            ("[" * 100000 + "]" * 100000, "is nested too deeply")):
            with self.subTest(text=text[:20]):
                settings.write_bytes(text if isinstance(text, bytes) else text.encode())
                self.assertEqual(self.plugin_rows(), [f"[warn] plugin: {settings} {error}", *unknown])
        settings.write_text("{}")
        self.locked(settings)
        self.assertEqual(self.plugin_rows(), [f"[warn] plugin: {settings} is not readable (PermissionError)", *unknown])

    def test_plugin_unreadable_cache_directories(self):
        self.enable("router", "shared-roles")
        market = self.claude / "plugins/cache/router"
        self.cache("router", "0.3.0")
        self.locked(market)
        self.assertEqual(self.plugin_rows(), [f"[warn] plugin: {market} is not readable (PermissionError)",
                                              "[ok] plugin: router enabled, cached unknown",
                                              "[ok] plugin: shared-roles enabled, cached unknown"])
        market.chmod(0o755)
        self.locked(market / "router")
        self.assertEqual(self.plugin_rows(), [f"[warn] plugin: {market / 'router'} is not readable (PermissionError)",
                                              "[ok] plugin: router enabled, cached unknown",
                                              "[ok] plugin: shared-roles enabled, not cached"])

    # Fix 2: the Harness manifest is cleaned and validated the way install.sh does.
    def harness_then_router(self):
        self.install(tree=self.old_tree("HARNESS_V02"))
        self.install()
        return self.claude / "harness/install-manifest.json"

    def test_harness_package_is_cleaned(self):
        sibling = self.harness_then_router()
        value = json.loads(sibling.read_text())
        for package in ("0.2.0\n[ok] injected: yes", "9" * 5000, "1.0\x1b[31m\x07", "", 7):
            value["package"] = package
            sibling.write_text(json.dumps(value))
            rows = self.lines(self.run_doctor())
            self.assertFalse(any(row.startswith("[ok] injected") for row in rows), rows)
            self.assertLess(max(map(len, rows)), 400)
            self.expect(rows, "[warn] sibling: Harness unknown")
        value["package"] = "0.2.5"
        sibling.write_text(json.dumps(value))
        rows = self.lines(self.run_doctor())
        self.expect(rows, "[ok] sibling: Harness 0.2.5 installed")
        self.expect_re(rows, r"\[warn\] skew: shared roles come from Harness 0\.2\.5; upgrade it for the 0\.3 roles")

    def test_harness_manifest_rejected_like_install(self):
        sibling = self.harness_then_router()
        good = json.loads(sibling.read_text())
        bad_digest = dict(good, files=dict(good["files"], **{next(iter(good["files"])): "not-a-digest"}), package="0.2.5")
        for value in (bad_digest, dict(good, version=2, package="0.2.5"), dict(good, hooks=[], package="0.2.5"),
                      dict(good, files={"../x": "0" * 64}, package="0.2.5")):
            sibling.write_text(json.dumps(value))
            rows = self.lines(self.run_doctor())
            self.assertEqual(sum(row.startswith("[warn] sibling:") for row in rows), 1)
            self.expect(rows, "[warn] sibling: Harness unknown")
            self.expect(rows, "[ok] skew: none")

    def test_own_manifest_package_is_cleaned(self):
        self.install()
        path = self.claude / "router/install-manifest.json"
        value = json.loads(path.read_text())
        value["package"] = "0.3.0\n[ok] injected: yes"
        path.write_text(json.dumps(value))
        rows = self.lines(self.run_doctor())
        self.assertFalse(any(row.startswith("[ok] injected") for row in rows), rows)
        self.expect(rows, f"[warn] version: installed unknown, checkout {(ROOT / 'VERSION').read_text().strip()} (run install.sh to update)")

    # Fix 3: one fullmatch test per message the first round left unpinned.
    def test_python_too_old(self):
        self.install()
        import cmd_doctor
        rows = self.inproc(patches=((cmd_doctor, "python_version", lambda: (3, 9, 0)),))
        self.expect(rows, f"[FAIL] python: 3.9.0 at {sys.executable}, need 3.10 or newer")

    def test_path_warning(self):
        self.install()
        rows = self.lines(self.run_doctor())
        self.expect(rows, f"[warn] path: {self.claude / 'router/bin'} is not on PATH")

    def test_no_hooks_registered(self):
        self.install()
        (self.claude / "settings.json").write_text("{}")
        rows = self.lines(self.run_doctor())
        self.expect(rows, "[FAIL] hooks: none registered in settings.json")

    def test_settings_nested_too_deeply(self):
        self.install()
        (self.claude / "settings.json").write_bytes(b"[" * 100000 + b"]" * 100000)
        rows = self.lines(self.run_doctor())
        self.expect(rows, "[FAIL] hooks: settings.json is nested too deeply")

    def test_hook_walk_nested_too_deeply(self):
        self.install()
        import cmd_doctor

        def deep(value):
            raise RecursionError
        rows = self.inproc(patches=((cmd_doctor, "_commands", deep),))
        self.expect(rows, "[FAIL] hooks: settings.json is nested too deeply")

    def test_overlay_none_and_last_write_none_yet(self):
        self.install()
        env = {key: value for key, value in self.env.items() if key != "ROUTER_LOCAL"}
        rows = self.lines(self.run_doctor(env=env))
        self.expect(rows, f"[ok] overlay: none ({self.config / 'router/routes.local.json'})")
        self.expect(rows, "[warn] last hook write: none yet")

    def test_last_write_present_empty_and_subdirectory(self):
        self.install()
        self.state.mkdir(parents=True)
        self.expect(self.lines(self.run_doctor()), "[warn] last hook write: none yet")
        (self.state / "subdir").mkdir()
        self.expect(self.lines(self.run_doctor()), "[warn] last hook write: none yet")
        record = self.state / "record.jsonl"
        record.write_text("entry\n")
        rows = self.lines(self.run_doctor())
        self.expect_re(rows, rf"\[ok\] last hook write: \d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}}Z \({re.escape(str(record))}\)")

    def test_installed_unreadable_hooks_reports_one_line(self):
        self.install()
        hooks = self.claude / "hooks/router"
        self.locked(hooks)
        result = subprocess.run([sys.executable, str(self.claude / "router/bin/router"), "doctor"],
                                env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr, rf"\Arouter: cannot load the router hooks from {re.escape(str(hooks))}: (?:PermissionError|ModuleNotFoundError|ImportError)\n\Z")

    def test_installed_missing_common_reports_one_line(self):
        self.install()
        hooks = self.claude / "hooks/router"
        (hooks / "common.py").unlink()
        result = subprocess.run([sys.executable, str(self.claude / "router/bin/router"), "doctor"],
                                env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr, rf"\Arouter: cannot load the router hooks from {re.escape(str(hooks))}: (?:ModuleNotFoundError|ImportError)\n\Z")

    def test_spawn_dry_run_exception(self):
        self.install()
        import common

        def broken(*args):
            raise ValueError("boom")
        rows = self.inproc(patches=((common, "decide_spawn", broken),))
        self.expect(rows, "[FAIL] spawn: dry run failed (ValueError)")

    def budget_copy(self, ceilings):
        script = self.claude / "router/bin/budget.py"
        text = (ROOT / "scripts/budget.py").read_text()
        changed = re.sub(r"^CEILINGS = .*$", "CEILINGS = " + repr(ceilings), text, count=1, flags=re.M)
        self.assertNotEqual(changed, text)
        script.unlink()
        script.write_text(changed)
        direct = subprocess.run([sys.executable, str(script), "--home", str(self.claude), "--host", "claude", "--json"],
                                env=self.env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        return json.loads(direct.stdout)["trees"][0]["tokens"]

    def test_budget_over_ceiling_warns(self):
        self.install()
        tokens = self.budget_copy({"router": 5, "harness": 600, "together": 900})
        self.assertGreater(tokens, 5)
        rows = self.lines(self.run_doctor())
        self.expect(rows, f"[ok] budget: router {tokens} tokens always-on (estimate, ceiling 5)")
        self.expect(rows, f"[warn] budget: {tokens} tokens is over the ceiling 5")

    # Fix 5: the ceilings come from budget.py, not from a copy in doctor.
    def test_budget_ceiling_is_read_from_budget_script(self):
        self.install()
        tokens = self.budget_copy({"router": 99999, "harness": 600, "together": 900})
        rows = self.lines(self.run_doctor())
        self.expect(rows, f"[ok] budget: router {tokens} tokens always-on (estimate, ceiling 99999)")
        self.assertFalse(any("over the ceiling" in row for row in rows))

    # Fix 4: read-only runs on a used home and on a home whose state directory does not exist.
    def test_used_home_is_untouched(self):
        self.install()
        self.state.mkdir(parents=True)
        (self.state / "events.jsonl").write_text('{"ts":"2026-01-01T00:00:00Z"}\n')
        (self.state / "errors.jsonl").write_text('{"ts":"2026-01-01T00:00:00Z","script":"hook","class":"OSError"}\n')
        import ledger
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertTrue(ledger.record_verdict("lane-key", "PASS", findings=1))
            self.assertTrue(ledger.ledger_path().is_file())
        settings = self.claude / "settings.json"
        before = (self.snapshot(), settings.stat().st_mtime_ns)
        rows = self.lines(self.run_doctor())
        self.assertEqual((self.snapshot(), settings.stat().st_mtime_ns), before)
        self.expect(rows, "[warn] errors: 1 in errors.jsonl, last: 2026-01-01T00:00:00Z hook OSError")

    def test_absent_state_directory_stays_absent(self):
        self.install()
        self.assertFalse(self.state.exists())
        self.lines(self.run_doctor())
        self.assertFalse(self.state.exists())
        self.assertFalse((self.base / "state").exists())

    # Bytecode: nothing doctor imports itself writes .pyc; the entry script is another lane's.
    def test_importing_doctor_turns_bytecode_off(self):
        self.install()
        env = {key: value for key, value in self.env.items() if key != "PYTHONDONTWRITEBYTECODE"}
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import cmd_doctor; print(sys.dont_write_bytecode)")
        result = subprocess.run([sys.executable, "-c", code, str(self.claude / "hooks/router")], env=env,
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.assertEqual(result.stdout, "True\n", result.stderr)
        written = {path.name.split(".")[0] for path in (self.claude / "hooks/router").rglob("*.pyc")}
        self.assertEqual(written - {"cmd_doctor"}, set())

    def test_installed_run_writes_no_new_file(self):
        self.install()
        env = {key: value for key, value in self.env.items() if key != "PYTHONDONTWRITEBYTECODE"}
        before = {path for path in self.base.rglob("*")}
        result = subprocess.run([sys.executable, str(self.claude / "router/bin/router"), "doctor"], env=env,
                                capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
        self.lines(result)
        new = sorted(path for path in self.base.rglob("*") if path not in before)
        self.assertEqual(new, [])


if __name__ == "__main__":
    unittest.main()
