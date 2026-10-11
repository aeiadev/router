#!/usr/bin/env python3
"""Windows line endings: an autocrlf clone stays LF, pins verify, a CRLF install.sh refuses, BOM settings load."""

import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _platform import BASH  # noqa: E402
SKIP_DIRS = {".git", ".real031-logs", "__pycache__", "node_modules", ".pytest_cache", ".venv"}
BINARY = (".sqlite3", ".sqlite3-wal", ".png", ".jpg", ".ico", ".woff2", ".mp4")
FIX = "git rm -r --cached -q . && git reset --hard -q"
BOM = b"\xef\xbb\xbf"


def remove(path):
    def retry(func, name, error):  # Git marks objects read-only; Windows refuses to delete those.
        error = error[1] if isinstance(error, tuple) else error
        if isinstance(error, FileNotFoundError):
            return
        # Only Windows deletion failures can be retried by clearing read-only.
        # Other callbacks (such as os.open) cannot be called with just a path.
        if os.name != "nt" or not isinstance(error, PermissionError) or func not in (os.unlink, os.rmdir):
            raise error
        try:
            os.chmod(name, stat.S_IWRITE)
            func(name)
        except FileNotFoundError:
            pass  # Another process removed the file during the retry.
    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


def found(name):
    path = shutil.which(name)
    if path is None:
        raise AssertionError(f"{name} not on PATH")
    return path


def installer_script():
    text = (ROOT / "install.sh").read_bytes().decode("utf-8")
    return text.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0].encode("utf-8")


def installer_function(name, **names):
    """One top-level function of the installer, for a host where the whole installer cannot start yet."""
    tree = ast.parse(installer_script().decode("utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "install.sh", "exec"), names)
    return names[name]


class CrlfTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="router-crlf-"))
        self.addCleanup(remove, self.tmp)

    def test_remove_ignores_file_disappearing_during_rmtree(self):
        root = self.tmp / "cleanup"
        root.mkdir()
        (root / "vanishing.lock").write_bytes(b"")
        unlink = os.unlink

        def vanish(name, *args, **kwargs):
            if Path(name).name == "vanishing.lock":
                unlink(name, *args, **kwargs)
            return unlink(name, *args, **kwargs)

        with mock.patch("os.unlink", side_effect=vanish):
            remove(root)
        self.assertFalse(root.exists())

    def test_remove_propagates_permission_error(self):
        root = self.tmp / "cleanup"
        root.mkdir()
        blocked = root / "blocked.lock"
        blocked.write_bytes(b"")
        modes = [path.stat().st_mode for path in (root, blocked)]
        error = PermissionError("blocked")

        with mock.patch("os.unlink", side_effect=error):
            with self.assertRaises(PermissionError) as raised:
                remove(root)
        self.assertIs(raised.exception, error)
        if os.name != "nt":
            self.assertEqual([path.stat().st_mode for path in (root, blocked)], modes)

    def test_remove_does_not_retry_directory_open(self):
        error = PermissionError("blocked directory")

        def fail(path, **kwargs):
            if "onexc" in kwargs:
                kwargs["onexc"](os.open, str(path), error)
            else:
                kwargs["onerror"](os.open, str(path), (type(error), error, None))

        with mock.patch("shutil.rmtree", side_effect=fail), mock.patch("os.chmod") as chmod:
            with self.assertRaises(PermissionError) as raised:
                remove(self.tmp)
        self.assertIs(raised.exception, error)
        chmod.assert_not_called()

    def test_remove_deletes_readonly_file(self):
        root = self.tmp / "cleanup"
        root.mkdir()
        readonly = root / "readonly"
        readonly.write_bytes(b"")
        readonly.chmod(stat.S_IREAD)
        remove(root)
        self.assertFalse(root.exists())

    def scratch_env(self):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GIT_", "ROUTER_", "HARNESS_", "CLAUDE", "CODEX", "XDG_"))}
        env.update(HOME=str(home), USERPROFILE=str(home), CLAUDE_HOME=str(home / ".claude"),
                   CODEX_HOME=str(home / ".codex"), XDG_STATE_HOME=str(home / ".local/state"),
                   XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
                   XDG_CACHE_HOME=str(home / ".cache"), GIT_CONFIG_GLOBAL=str(self.tmp / "gitconfig"),
                   GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_AUTHOR_NAME="crlf", GIT_AUTHOR_EMAIL="crlf",
                   GIT_COMMITTER_NAME="crlf", GIT_COMMITTER_EMAIL="crlf")
        (self.tmp / "gitconfig").write_bytes(b"")
        return home, env

    def git(self, env, *args):
        result = subprocess.run([found("git"), "-c", "gc.auto=0", "-c", "maintenance.auto=false", *args],
                                env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=300)
        self.assertEqual(result.returncode, 0, f"git {args}: {result.stderr.decode('utf-8', 'replace')[-600:]}")
        return result.stdout

    def test_autocrlf_clone_stays_lf_and_pins_verify(self):
        _, env = self.scratch_env()
        source, clone = self.tmp / "source", self.tmp / "clone"
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS)
            relative = Path(folder).relative_to(ROOT)
            (source / relative).mkdir(parents=True, exist_ok=True)
            for name in files:
                if name != ".git" and not name.startswith(".env") and not name.endswith(".pyc"):
                    shutil.copy2(Path(folder) / name, source / relative / name)
        self.git(env, "init", "-q", str(source))
        self.git(env, "-C", str(source), "-c", "core.autocrlf=false", "add", "-A")
        self.git(env, "-C", str(source), "-c", "commit.gpgsign=false", "commit", "-q", "-m", "snapshot")
        self.git(env, "-c", "core.autocrlf=true", "clone", "-q", str(source), str(clone))
        tracked = [name for name in self.git(env, "-C", str(clone), "ls-files", "-z").decode("utf-8").split("\0") if name]
        self.assertIn(".gitattributes", tracked)
        self.assertIn("install.sh", tracked)
        with_cr, changed_binary = [], []
        for name in tracked:
            data = (clone / name).read_bytes()
            if name.endswith(BINARY):
                if data != (ROOT / name).read_bytes():
                    changed_binary.append(name)
            elif b"\0" not in data[:8000] and b"\r" in data:
                with_cr.append(name)
        self.assertEqual(with_cr, [], "CR in text files of an autocrlf=true clone")
        self.assertEqual(changed_binary, [])
        for line in (clone / "agents/SHARED.sha256").read_bytes().decode("utf-8").splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((clone / "agents" / name).read_bytes()).hexdigest(), digest, name)
        check = subprocess.run([sys.executable, str(clone / "scripts/generate_roles.py"), "--check"], env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_crlf_install_sh_refuses_in_one_line(self):
        home, env = self.scratch_env()
        copy = self.tmp / "checkout"
        copy.mkdir()
        (copy / "install.sh").write_bytes((ROOT / "install.sh").read_bytes().replace(b"\n", b"\r\n"))
        result = subprocess.run([BASH, "install.sh"], cwd=str(copy), env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=120)
        lines = result.stderr.decode("utf-8", "replace").splitlines()
        self.assertEqual((result.returncode, result.stdout, len(lines)), (1, b"", 1), lines)
        self.assertIn(FIX, lines[0])
        self.assertIn("re-download", lines[0])
        self.assertEqual(list(home.iterdir()), [], "a CRLF install.sh must stop before anything runs")

    def test_crlf_role_verifies_against_lf_pin(self):
        spec = importlib.util.spec_from_file_location("generate_roles_crlf", ROOT / "scripts/generate_roles.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        root = self.tmp / "tree"
        for folder in ("agents", "codex/agents"):
            shutil.copytree(ROOT / folder, root / folder)
        for name in ("hooks/router/routes.json", "scripts/budget.py"):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, root / name)
        pins = (root / "agents/SHARED.sha256").read_bytes()
        role = root / "agents" / pins.decode("utf-8").splitlines()[0].split("  ", 1)[1]
        lf = generator.expected(root)
        for path in (role, root / "scripts/budget.py"):
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        self.assertEqual(generator.expected(root), lf)
        generator.expected(root, pin=True)
        self.assertEqual((root / "agents/SHARED.sha256").read_bytes(), pins, "pins hash LF bytes, written LF")
        role.write_bytes(role.read_bytes() + b"changed\r\n")
        with self.assertRaises(ValueError):
            generator.expected(root)

    def test_settings_with_bom_and_crlf_load_and_write_lf(self):
        home, env = self.scratch_env()
        settings = home / ".claude/settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_bytes(BOM + b'{\r\n  "model": "opus"\r\n}\r\n')
        load = installer_function("load_json", copy=copy, json=json, fail=self.fail)
        self.assertEqual(load(settings, {}), {"model": "opus"})
        command = [sys.executable, "-B", "-", str(ROOT), "--host", "claude"]
        dry = subprocess.run(command + ["--dry-run"], input=installer_script(), env=env, capture_output=True, timeout=300)
        self.assertEqual(dry.returncode, 0, dry.stderr.decode("utf-8", "replace")[-800:])
        real = subprocess.run(command, input=installer_script(), env=env, capture_output=True, timeout=300)
        self.assertEqual(real.returncode, 0, real.stderr.decode("utf-8", "replace")[-800:])
        written = settings.read_bytes()
        self.assertFalse(written.startswith(BOM))
        self.assertNotIn(b"\r", written)
        self.assertEqual(json.loads(written)["model"], "opus")


if __name__ == "__main__":
    unittest.main()
