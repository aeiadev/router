#!/usr/bin/env python3
"""Entry points must disable bytecode before importing local modules."""
import ast
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
from _platform import BASH, IS_WINDOWS, home_env, posix
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _platform import BASH, posix  # noqa: E402


def entry_command(path, *arguments):
    """Windows needs Git Bash explicitly; POSIX executes the installed shebang."""
    return [BASH, posix(path), *arguments] if IS_WINDOWS else [str(path), *arguments]


VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
ROUTES = json.loads((ROOT / "hooks/router/routes.json").read_text(encoding="utf-8"))
HOOK_SCRIPTS = {"codex_spawn_guard.py", "context_guard.py", "judge_reminder.py", "prompt_hint.py",
                "session_note.py", "spawn_guard.py"}
DIRECT_REPORTS = {"cmd_observe.py", "cmd_evaluate.py"}
PYTHON_ROW = r"\[ok\] python: 3\.\d+\.\d+ \(.+\)"
CAPABILITY_IDS = ("role_discovery", "model_pins_inheritance", "hook_payload_fields",
                  "trusted_hook_lifecycle", "tool_rewrites", "completion_events",
                  "tool_visibility", "read_only_enforcement", "usage_formats")


def capability_rows(host):
    reference = "2.1.296" if host == "claude" else "0.156.1"
    return [re.escape(f"[info] capability version: unknown (unavailable); reference {reference}; match=false; host unverified"),
            *(rf"\[info\] capability {name}: [^\n]+; host unverified; [^\n]+" for name in CAPABILITY_IDS)]


def status_rows(state, budget):
    """Every line of router status with the shipped modes, ROUTER_LOCAL=off and the given budget rows."""
    modes = [f"{section}.{rule}: {mode}" for section in ("router", "context")
             for rule, mode in sorted(ROUTES[section]["modes"].items())]
    return [re.escape(row) for row in ("on", f"switch: {state / 'OFF'}", "auto: nudge", *modes,
                                       "overlay: off (ROUTER_LOCAL=off)",
                                       "global: on (router off or ROUTER_OFF=1 disables every hook)",
                                       "auto guidance: off/suggest/nudge/enforce changes automatic suggestions; rule modes above govern hooks",
                                       "legacy unlisted fallback: inject_unlisted=enforce for unresolved types; named native/custom agents pass through")] + budget


def budget_row(name):
    return (rf"budget {name}: ~\d+ tokens always-on \(\d+ B names and descriptions, estimate; "
            r"ceiling \d+\)")


def installed_doctor_rows(host, home, running):
    """Every row of a healthy doctor report for HOST on a fresh install with no sibling; the routes
    row names the table beside the RUNNING installed router."""
    e = re.escape
    rows = [e(f"Router doctor ({host}): {home}"), PYTHON_ROW,
            e(f"[warn] path: {home / 'router' / 'bin'} is not on PATH"),
            rf"\[ok\] hooks: (?P<hooks_{host}>\d+) of (?P=hooks_{host}) registered"]
    if host == "codex":
        rows.append(e("[warn] hooks trust: unreadable on Codex (hook trust state is not visible to doctor)"))
    rows += [rf"\[ok\] agents: (?P<agents_{host}>[1-9]\d*) of (?P=agents_{host}) role files present",
             e(f"[ok] version: installed {VERSION}, checkout {VERSION} (current)"),
             r"\[ok\] files: [1-9]\d* current", e("[ok] leftovers: none"),
             e("[warn] sibling: Harness not installed"), e("[ok] skew: none")]
    if host == "claude":
        rows += [e("[ok] plugin: router not enabled, not cached; stands down: script install present"),
                 e("[ok] plugin: shared-roles not enabled, not cached")]
    rows += [e(f"[ok] routes: shipped table valid ({(running / 'hooks' / 'router').resolve() / 'routes.json'})"),
             e("[ok] overlay: off (ROUTER_LOCAL=off)"), e("[warn] last hook write: none yet"),
             e("[ok] errors: none"), e("[ok] spawn: dry run allowed a sweeper brief"),
             r"\[ok\] budget: router \d+ tokens always-on \(estimate, ceiling \d+\)",
             *capability_rows(host)]
    return rows


def doctor_summary(rows):
    """The final doctor line, counted from the expected row patterns."""
    count = lambda level: sum(row.startswith(rf"\[{level}\]") for row in rows)
    return re.escape(f"doctor: {count('ok')} ok, {count('warn')} warn, {count('FAIL')} fail")


def interpreter_prefix(env, cwd):
    """Ask the child interpreter where it mirrors bytecode, if anywhere."""
    result = subprocess.run([sys.executable, "-c", "import sys; print(sys.pycache_prefix or '')"],
                            env=env, cwd=cwd, capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=10, check=True)
    return Path(result.stdout.strip()) if result.stdout.strip() else None


def mirrored_cache_root(prefix, tree):
    """CPython drops the drive and mirrors the absolute source path."""
    relative = ntpath.splitdrive(str(tree))[1].replace("\\", "/").lstrip("/")
    return prefix / relative


def written_bytecode(base, prefix):
    """Find copied application bytecode, locally or in CPython's mirrored prefix."""
    found = {path for path in base.rglob("*.pyc")
             if prefix is None or not path.is_relative_to(prefix)}
    if prefix is not None:
        for tree in {base, base.resolve()}:
            found.update(mirrored_cache_root(prefix, tree).rglob("*.pyc"))
    return found


class BytecodeTests(unittest.TestCase):
    def test_real_install_entries_and_doctor_import_write_no_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.clean_env(root)
            env.update(CLAUDE_HOME=str(root / "claude"), CODEX_HOME=str(root / "codex"))
            done = []
            for host in ("claude", "codex"):
                result = subprocess.run([BASH, posix(ROOT / "install.sh"), "--host", host],
                                        env=env, capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = Path(env["CLAUDE_HOME" if host == "claude" else "CODEX_HOME"])
                done.append((host, installed))
                # status and doctor report every home with Router installed so far.
                status = status_rows(Path(env["ROUTER_STATE"]), [budget_row(name) for name, _ in done])
                doctor = [row for name, home in done for row in installed_doctor_rows(name, home, installed)]
                for command, rows in (("status", status), ("doctor", doctor + [doctor_summary(doctor)])):
                    result = subprocess.run(entry_command(installed / "router/bin/router", command),
                                            env=env, capture_output=True, text=True,
                                            stdin=subprocess.DEVNULL, timeout=30)
                    self.assertEqual((result.returncode, result.stderr), (0, ""), f"{host} {command}: {result.stdout}")
                    self.assertRegex(result.stdout, "\\A" + "".join(row + "\n" for row in rows) + "\\Z")
                result = subprocess.run([sys.executable, str(installed / "hooks/router/cmd_doctor.py")],
                                        env=env, capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, timeout=30)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                prefix = interpreter_prefix(env, installed)
                for app in (installed / "router/bin", installed / "hooks/router"):
                    self.assertEqual(written_bytecode(app, prefix), set())

    def test_prelude_precedes_other_imports(self):
        files = [ROOT / "bin/router"] + list((ROOT / "hooks/router").glob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text())
            if path.name != "router" and not any(
                isinstance(node, ast.If) and "__main__" in ast.unparse(node.test) for node in tree.body):
                continue
            body = tree.body
            first_import = next((i for i, node in enumerate(body)
                                if isinstance(node, (ast.Import, ast.ImportFrom)) and
                                not (isinstance(node, ast.Import) and len(node.names) == 1 and node.names[0].name == "sys")), len(body))
            assignment = next((i for i, node in enumerate(body) if isinstance(node, ast.Assign) and
                               any(isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and
                                   target.value.id == "sys" and target.attr == "dont_write_bytecode"
                                   for target in node.targets)), None)
            self.assertIsNotNone(assignment, str(path))
            self.assertLess(assignment, first_import, str(path))

    def test_installed_entries_write_no_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            shutil.copy2(ROOT / "bin/router", root / "bin/router")
            shutil.copytree(ROOT / "hooks/router", root / "hooks/router", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            env = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONDONTWRITEBYTECODE", "ROUTER_OFF", "ROUTER_LOCAL") and
                   not key.startswith(("ROUTER_", "CLAUDE_", "CODEX_", "XDG_"))}
            env.update(home_env(root))
            env.update(XDG_CONFIG_HOME=str(root / "config"),
                       XDG_STATE_HOME=str(root / "state"), ROUTER_LOCAL="off",
                       ROUTER_STATE=str(root / "state/router"), ROUTER_PYTHON=sys.executable)
            prefix = interpreter_prefix(env, root)
            app_roots = (root / "bin", root / "hooks/router")
            before_bytecode = {app: written_bytecode(app, prefix) for app in app_roots}
            event = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "probe",
                                "prompt": "Implement the requested feature here", "tool_name": "Read",
                                "tool_input": {"file_path": "source.py"}})
            # A bare copy: no manifest, roles or scripts/budget.py, so doctor fails on purpose.
            home, e = root / ".claude", re.escape
            doctor = [e(f"Router doctor (claude): {home}"), PYTHON_ROW,
                      e(f"[warn] path: {home / 'router' / 'bin'} is not on PATH"),
                      e(f"[FAIL] install: no manifest at {home / 'router' / 'install-manifest.json'}; run install.sh"),
                      e("[FAIL] hooks: settings.json is not readable"),
                      e("[FAIL] agents: 0 of 0 role files present"),
                      e("[warn] version: not installed, checkout unknown"), e("[warn] files: none installed"),
                      e("[ok] leftovers: none"), e("[warn] sibling: Harness not installed"),
                      e("[warn] skew: unknown (checkout not found)"),
                      e("[ok] plugin: router not enabled, not cached"),
                      e("[ok] plugin: shared-roles not enabled, not cached"),
                      e(f"[ok] routes: shipped table valid ({(root / 'hooks' / 'router').resolve() / 'routes.json'})"),
                      e("[ok] overlay: off (ROUTER_LOCAL=off)"), e("[warn] last hook write: none yet"),
                      e("[ok] errors: none"), e("[ok] spawn: dry run allowed a sweeper brief"),
                      e("[warn] budget: unavailable (scripts/budget.py is missing)"),
                      *capability_rows("claude")]
            status = status_rows(root / "state/router", [e("budget: unavailable (scripts/budget.py is missing)")])
            for command, code, rows in (("status", 0, status), ("doctor", 1, doctor + [doctor_summary(doctor)])):
                result = subprocess.run(entry_command(root / "bin/router", command), input=event, text=True,
                                        capture_output=True, env=env, timeout=30)
                self.assertEqual((result.returncode, result.stderr), (code, ""), f"{command}: {result.stdout}")
                self.assertRegex(result.stdout, "\\A" + "".join(row + "\n" for row in rows) + "\\Z")
            scripts = {script.name for script in (root / "hooks/router").glob("*.py")
                       if 'if __name__ == "__main__"' in script.read_text()}
            self.assertEqual(scripts, HOOK_SCRIPTS | DIRECT_REPORTS | {"completion.py"})
            for name in sorted(HOOK_SCRIPTS):
                # As installed: through pyrun.sh, which keeps Apple Python from caching the stdlib.
                result = subprocess.run([BASH, posix(root / "hooks/router/pyrun.sh"), "router", "warn", name],
                                        input=event, text=True, capture_output=True, env=env, timeout=30)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""), name)
            checklist = subprocess.run([sys.executable, str(root / "hooks/router/completion.py")],
                                       stdin=subprocess.DEVNULL, text=True, capture_output=True,
                                       env=env, timeout=30)
            self.assertEqual((checklist.returncode, checklist.stderr), (0, ""))
            self.assertTrue(checklist.stdout.startswith("Inspection: "))
            self.assertIn("External readback: ", checklist.stdout)
            state = root / "state"
            before_reports = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
            for name in sorted(DIRECT_REPORTS):
                report = subprocess.run([sys.executable, str(root / "hooks/router" / name), "--file", "-"],
                                        stdin=subprocess.DEVNULL, text=True, capture_output=True,
                                        env=env, timeout=30)
                self.assertEqual(report.returncode, 1, name)
                self.assertEqual(report.stdout, "", name)
                self.assertLess(len(report.stderr), 256, name)
            after_reports = {path.relative_to(state): path.read_bytes() for path in state.rglob("*") if path.is_file()}
            self.assertEqual(after_reports, before_reports, "direct reports changed state")
            self.assertEqual({app: written_bytecode(app, prefix) for app in app_roots}, before_bytecode)
            self.assertEqual(before_bytecode, {app: set() for app in app_roots})

    def test_copied_application_cache_is_found_locally_and_in_mirrored_prefix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / "hooks/router"
            app.mkdir(parents=True)
            (app / "helper.py").write_text("value = 1\n")
            prefix = root / "home/Library/Caches/com.apple.python"
            env = {**os.environ, **home_env(root / "home")}
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            env.pop("PYTHONPYCACHEPREFIX", None)
            for chosen_prefix in (None, prefix):
                before = written_bytecode(app, chosen_prefix)
                script = "import sys; sys.pycache_prefix = " + repr(
                    str(chosen_prefix) if chosen_prefix is not None else None) + "; import helper"
                result = subprocess.run([sys.executable, "-c", script], env=env, cwd=app,
                                        capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(written_bytecode(app, chosen_prefix) - before)
            self.assertEqual(mirrored_cache_root(prefix, r"C:\Users\BG\router\hooks"),
                             prefix / "Users/BG/router/hooks")
            stdlib = prefix / "usr/lib/python3.9/site.cpython-39.pyc"
            stdlib.parent.mkdir(parents=True, exist_ok=True)
            stdlib.touch()
            self.assertNotIn(stdlib, written_bytecode(root, prefix))

    def clean_env(self, root):
        env = {key: value for key, value in os.environ.items()
               if key not in ("PYTHONDONTWRITEBYTECODE", "ROUTER_OFF", "ROUTER_LOCAL", "ROUTES_JSON") and
               not key.startswith(("ROUTER_", "CLAUDE_", "CODEX_", "XDG_"))}
        env.update(home_env(root))
        env.update(XDG_CONFIG_HOME=str(root / "config"),
                   XDG_STATE_HOME=str(root / "state"), ROUTER_LOCAL="off",
                   ROUTER_STATE=str(root / "state/router"))
        return env

    def test_bin_router_probes_common_file_not_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good/bin").mkdir(parents=True)
            shutil.copy2(ROOT / "bin/router", root / "good/bin/router")
            shutil.copytree(ROOT / "hooks/router", root / "good/hooks/router",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (root / "bad/bin").mkdir(parents=True)
            shutil.copy2(ROOT / "bin/router", root / "bad/bin/router")
            (root / "bad/hooks/router").mkdir(parents=True)  # a directory without common.py
            env = self.clean_env(root)
            direct = subprocess.run([sys.executable, str(root / "good/bin/router"), "status"], text=True,
                                    capture_output=True, env=env, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual(direct.returncode, 0, direct.stderr)
            env["ROUTER_HOME"] = str(root / "good/hooks/router")
            result = subprocess.run([sys.executable, str(root / "bad/bin/router"), "status"], text=True,
                                    capture_output=True, env=env, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, direct.stdout, ""))
            self.assertTrue(direct.stdout)

    def test_bin_router_names_first_existing_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            shutil.copy2(ROOT / "bin/router", root / "bin/router")
            (root / "hooks/router").mkdir(parents=True)
            env = self.clean_env(root)
            env["ROUTER_HOME"] = str(root / "nowhere")
            result = subprocess.run([sys.executable, str(root / "bin/router"), "status"], text=True,
                                    capture_output=True, env=env, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (
                1, "", f"router: cannot load the router hooks from {(root / 'hooks/router').resolve()}: ModuleNotFoundError\n"))

    def test_hook_scripts_print_one_line_without_common(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hooks = root / "hooks/router"
            shutil.copytree(ROOT / "hooks/router", hooks, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            (hooks / "common.py").unlink()
            env = dict(self.clean_env(root), ROUTER_PYTHON=sys.executable)
            prefix = interpreter_prefix(env, root)
            before_bytecode = written_bytecode(hooks, prefix)
            event = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "probe",
                                "prompt": "Implement the requested feature here", "tool_name": "Read",
                                "tool_input": {"file_path": "source.py"}})
            names = ("spawn_guard", "codex_spawn_guard", "judge_reminder", "session_note", "prompt_hint", "context_guard")
            for name in names:
                script = hooks / (name + ".py")
                result = subprocess.run([BASH, posix(hooks / "pyrun.sh"), "router", "warn", script.name],  # as installed
                                        input=event, text=True, capture_output=True, env=env, timeout=30)
                expected = f"{name}.py: cannot load the router hooks from {script.resolve().parent}: ModuleNotFoundError\n"
                self.assertEqual(result.stderr, expected, name)
                self.assertEqual((result.returncode, result.stdout), (1, ""), name)
            self.assertEqual(written_bytecode(hooks, prefix), before_bytecode)
            self.assertEqual(before_bytecode, set())


if __name__ == "__main__":
    unittest.main()
