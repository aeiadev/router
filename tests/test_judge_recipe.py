#!/usr/bin/env python3
"""Router-only Codex judge recipe contract; never changes a real profile."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from _platform import BASH, home_env, posix

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10 remain supported for installation.
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]


class JudgeRecipeTests(unittest.TestCase):
    @unittest.skipUnless(tomllib, "TOML parser is available on Python 3.11+")
    def test_standalone_router_install_has_a_usable_judge_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "router"
            shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            home = root / "home"
            env = dict(os.environ, **home_env(home), CODEX_HOME=str(home / ".codex"),
                       PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([BASH, posix(source / "install.sh"), "--host", "codex"],
                                    cwd=source, env=env, stdin=subprocess.DEVNULL,
                                    text=True, capture_output=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)

            recipe = source / "codex/judge-permissions.toml"
            config = tomllib.loads(recipe.read_text(encoding="utf-8"))
            self.assertNotIn("default_permissions", config)
            profile = config["permissions"]["harness-judge"]
            self.assertEqual(profile["filesystem"][":workspace_roots"], "read")
            self.assertEqual(profile["filesystem"][":tmpdir"], "write")
            self.assertFalse(profile["network"]["enabled"])

            installed = home / ".codex/agents/judge.toml"
            instructions = tomllib.loads(installed.read_text(encoding="utf-8"))["developer_instructions"]
            self.assertIn("codex/judge-permissions.toml", instructions)
            self.assertIn("codex -c default_permissions=harness-judge", instructions)
            self.assertIn("not enforced", instructions)
            readme = (source / "README.md").read_text(encoding="utf-8")
            self.assertIn("codex/judge-permissions.toml", readme)
            self.assertIn("default_permissions=harness-judge", readme)
            self.assertIn("does not edit", readme)


if __name__ == "__main__":
    unittest.main()
