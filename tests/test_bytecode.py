#!/usr/bin/env python3
"""Entry points must disable bytecode before importing local modules."""
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


class BytecodeTests(unittest.TestCase):
    def test_real_install_entries_and_doctor_import_write_no_bytecode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = self.clean_env(root)
            env.update(CLAUDE_HOME=str(root / "claude"), CODEX_HOME=str(root / "codex"))
            for host in ("claude", "codex"):
                result = subprocess.run(["bash", str(ROOT / "install.sh"), "--host", host],
                                        env=env, capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                installed = Path(env["CLAUDE_HOME" if host == "claude" else "CODEX_HOME"])
                for command in ("status", "doctor"):
                    result = subprocess.run([sys.executable, str(installed / "router/bin/router"), command],
                                            env=env, capture_output=True, text=True,
                                            stdin=subprocess.DEVNULL, timeout=30)
                    self.assertNotIn("Traceback", result.stderr)
                result = subprocess.run([sys.executable, str(installed / "hooks/router/cmd_doctor.py")],
                                        env=env, capture_output=True, text=True,
                                        stdin=subprocess.DEVNULL, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(list(installed.rglob("__pycache__")), [])
                self.assertEqual(list(installed.rglob("*.pyc")), [])

    def test_prelude_precedes_other_imports(self):
        files = [ROOT / "bin/router"] + list((ROOT / "hooks/router").glob("*.py"))
        for path in files:
            tree = ast.parse(path.read_text())
            if path.name != "router" and not any(
                isinstance(node, ast.If) and "__main__" in ast.unparse(node.test) for node in tree.body):
                continue
            body = tree.body
            first_import = next(i for i, node in enumerate(body)
                                if isinstance(node, (ast.Import, ast.ImportFrom)) and
                                not (isinstance(node, ast.Import) and len(node.names) == 1 and node.names[0].name == "sys"))
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
            env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / "config"),
                       XDG_STATE_HOME=str(root / "state"), ROUTER_LOCAL="off",
                       ROUTER_STATE=str(root / "state/router"))
            event = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "probe",
                                "prompt": "Implement the requested feature here", "tool_name": "Read",
                                "tool_input": {"file_path": "source.py"}})
            for args in ([root / "bin/router", "status"], [root / "bin/router", "doctor"]):
                result = subprocess.run([sys.executable, *map(str, args)], input=event, text=True,
                                        capture_output=True, env=env, timeout=30)
                self.assertNotIn("Traceback", result.stderr)
            for script in (root / "hooks/router").glob("*.py"):
                if 'if __name__ == "__main__"' not in script.read_text():
                    continue
                result = subprocess.run([sys.executable, str(script)], input=event, text=True,
                                        capture_output=True, env=env, timeout=30)
                self.assertNotIn("Traceback", result.stderr, str(script))
            self.assertEqual(list(root.rglob("*.pyc")), [])
            self.assertEqual(list(root.rglob("__pycache__")), [])

    def clean_env(self, root):
        env = {key: value for key, value in os.environ.items()
               if key not in ("PYTHONDONTWRITEBYTECODE", "ROUTER_OFF", "ROUTER_LOCAL", "ROUTES_JSON") and
               not key.startswith(("ROUTER_", "CLAUDE_", "CODEX_", "XDG_"))}
        env.update(HOME=str(root), XDG_CONFIG_HOME=str(root / "config"),
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
            env = self.clean_env(root)
            event = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "probe",
                                "prompt": "Implement the requested feature here", "tool_name": "Read",
                                "tool_input": {"file_path": "source.py"}})
            names = ("spawn_guard", "codex_spawn_guard", "judge_reminder", "session_note", "prompt_hint", "context_guard")
            for name in names:
                script = hooks / (name + ".py")
                result = subprocess.run([sys.executable, str(script)], input=event, text=True,
                                        capture_output=True, env=env, timeout=30)
                expected = f"{name}.py: cannot load the router hooks from {script.resolve().parent}: ModuleNotFoundError\n"
                self.assertEqual(result.stderr, expected, name)
                self.assertEqual((result.returncode, result.stdout), (1, ""), name)
            self.assertEqual(list(root.rglob("*.pyc")), [])


if __name__ == "__main__":
    unittest.main()
