"""The completion checklist is explicit, exact, and available through Router discovery."""
import os
from pathlib import Path
import subprocess
import shutil
from _platform import BASH, posix, home_env
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
EXPECTED = (
    "Inspection: Inspect the actual changed files or diff and confirm the intended edits landed.\n"
    "Acceptance: Check requirements against BAR evidence and record verifier identity and provenance. Inspection or a reported PASS alone is not independent acceptance.\n"
    "External readback: Observe the resulting external state without replaying the mutation; record evidence, not-applicable, or unknown.\n"
)
EXPECTED_BYTES = (EXPECTED.encode(), EXPECTED.replace("\n", "\r\n").encode())


def tree_state(root):
    result = {}
    for path in root.rglob("*"):
        info = path.lstat()
        value = ("link", os.readlink(path)) if path.is_symlink() else (
            ("file", path.read_bytes()) if path.is_file() else ("dir",))
        result[str(path.relative_to(root))] = (value, info.st_mode, info.st_mtime_ns)
    return result


class ChecklistTests(unittest.TestCase):
    def invoke(self, command, env):
        return subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=20, env=env)

    def test_explicit_command_and_invalid_argument(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = dict(os.environ, **home_env(home), CLAUDE_HOME=str(home / ".claude"),
                       CODEX_HOME=str(home / ".codex"), ROUTER_HOME=str(home / "router"),
                       ROUTER_STATE=str(home / "state"), PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, "-B", str(ROOT / "bin/router"), "checklist"]
            good = self.invoke(command, env)
            self.assertEqual((good.returncode, good.stderr), (0, b""))
            self.assertIn(good.stdout, EXPECTED_BYTES)
            bad = self.invoke([*command, "extra"], env)
            self.assertEqual(bad.returncode, 2)
            self.assertEqual(bad.stdout, b"")
            self.assertLessEqual(len(bad.stderr), 200)
            self.assertEqual(list(home.iterdir()), [], "checklist must create no state")

    def test_completion_module_is_standalone(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = dict(os.environ, **home_env(home), PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, "-B", str(ROOT / "hooks/router/completion.py")]
            self.assertIn(self.invoke(command, env).stdout, EXPECTED_BYTES)
            self.assertEqual(self.invoke([*command, "extra"], env).returncode, 2)
            self.assertEqual(list(home.iterdir()), [])

    def test_installed_command_without_harness(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = dict(os.environ, **home_env(home), CLAUDE_HOME=str(home / ".claude"),
                       CODEX_HOME=str(home / ".codex"), XDG_STATE_HOME=str(home / "state"),
                       XDG_CONFIG_HOME=str(home / "config"), ROUTER_LOCAL="off",
                       PYTHONDONTWRITEBYTECODE="1")
            install = self.invoke([BASH, posix(ROOT / "install.sh"), "--host", "claude"], env)
            self.assertEqual(install.returncode, 0, install.stderr.decode(errors="replace")[-400:])
            command = [sys.executable, "-B", str(home / ".claude/router/bin/router"), "checklist"]
            before = tree_state(home)
            self.assertIn(self.invoke(command, env).stdout, EXPECTED_BYTES)
            self.assertEqual(self.invoke([*command, "extra"], env).returncode, 2)
            self.assertEqual(tree_state(home), before)
            self.assertFalse((home / ".claude/harness").exists())

    def test_generated_plugin_is_standalone_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            plugin = home / "plugin"
            shutil.copytree(ROOT / "plugins/router", plugin)
            env = dict(os.environ, **home_env(home), ROUTER_HOME=str(plugin),
                       ROUTER_STATE=str(home / "state"), PYTHONDONTWRITEBYTECODE="1")
            before = tree_state(home)
            command = [sys.executable, "-B", str(plugin / "bin/router"), "checklist"]
            run = self.invoke(command, env)
            self.assertEqual((run.returncode, run.stderr), (0, b""))
            self.assertIn(run.stdout, EXPECTED_BYTES)
            self.assertEqual(self.invoke([*command, "private-invalid"], env).returncode, 2)
            self.assertEqual(tree_state(home), before)

    def test_checklist_bytes_reject_mixed_newlines_and_extra_output(self):
        self.assertNotIn(EXPECTED_BYTES[0].replace(b"\n", b"\r\n", 1), EXPECTED_BYTES)
        self.assertNotIn(EXPECTED_BYTES[0] + b"\n", EXPECTED_BYTES)


if __name__ == "__main__":
    unittest.main()
