"""Restricted entry point must reject controls before loading stateful modules."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from _platform import home_env

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "bin/router-commands"


class CommandsProfileTests(unittest.TestCase):
    def test_rejected_controls_do_not_import_or_create_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {**os.environ, **home_env(home), "XDG_STATE_HOME": str(home / "state"),
                   "ROUTER_STATE": str(home / "legacy"), "PYTHONDONTWRITEBYTECODE": "1"}
            for command in ("on", "off", "auto", "ladder", "config", "routing"):
                with self.subTest(command=command):
                    result = subprocess.run([sys.executable, "-B", str(ENTRY), command],
                                            env=env, stdin=subprocess.DEVNULL,
                                            capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse((home / "state").exists())
                    self.assertFalse((home / "legacy").exists())
            probe = ("import sys; from pathlib import Path; "
                     "sys.path.insert(0, sys.argv[1]); import command_runtime; "
                     "code=command_runtime.main(['auto'], root=Path(sys.argv[2])); "
                     "print(code, any(x in sys.modules for x in "
                     "('common','receipts','usage','operations','cmd_receipt')))" )
            result = subprocess.run([sys.executable, "-B", "-c", probe,
                                     str(ROOT / "hooks/router"), str(ROOT)], env=env,
                                    stdin=subprocess.DEVNULL, capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.stdout.strip(), "2 False")

    def test_status_state_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {**os.environ, **home_env(home), "XDG_STATE_HOME": str(home / "state"),
                   "ROUTER_STATE": str(home / "legacy"), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-B", str(ENTRY), "status"],
                                    env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("commands-only", result.stdout)
            self.assertNotIn(str(home / "legacy"), result.stdout)

    def test_missing_runtime_never_loads_ambient_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            shutil.copytree(ROOT, source)
            (source / "hooks/router/command_runtime.py").unlink()
            ambient = base / "ambient"
            ambient.mkdir()
            (ambient / "command_runtime.py").write_text("raise SystemExit(42)\n")
            env = {**os.environ, **home_env(base / "home"), "PYTHONPATH": str(ambient),
                   "XDG_STATE_HOME": str(base / "state"), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, "-B", str(source / "bin/router-commands"), "auto"],
                                    env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse((base / "state").exists())


if __name__ == "__main__":
    unittest.main()
