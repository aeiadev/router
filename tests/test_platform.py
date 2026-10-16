#!/usr/bin/env python3
"""tests/_platform.py: bash by full path, POSIX paths for bash, a python3 shim, a tree-killing timeout and
skips that print their reason. Runs on Linux, macOS and native Windows."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _platform  # noqa: E402
from _platform import BASH, home_env, posix, python_shim, run, skip_unless_posix_modes  # noqa: E402

GIT = r"C:\Program Files\Git"
WINDOWS_ENV = {"SystemRoot": r"C:\Windows", "LOCALAPPDATA": r"C:\Users\x\AppData\Local",
               "PATH": r"C:\Windows\system32;C:\Users\x\AppData\Local\Microsoft\WindowsApps;" + GIT + r"\cmd"}
WSL_BASH = r"C:\Windows\System32\bash.exe"
STORE_BASH = r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe"


class FindBash(unittest.TestCase):
    def test_windows_derives_git_bash_and_never_the_wsl_one(self):
        files = {WSL_BASH, STORE_BASH, GIT + r"\bin\bash.exe"}
        found = _platform.find_bash(WINDOWS_ENV, windows=True, isfile=files.__contains__)
        self.assertEqual(found, GIT + r"\bin\bash.exe")
        given = dict(WINDOWS_ENV, BASH=WSL_BASH)
        self.assertEqual(_platform.find_bash(given, windows=True, isfile=files.__contains__), GIT + r"\bin\bash.exe")
        self.assertIsNone(_platform.find_bash(WINDOWS_ENV, windows=True, isfile={WSL_BASH, STORE_BASH}.__contains__))

    def test_windows_takes_bash_from_the_environment(self):
        given = dict(WINDOWS_ENV, BASH=r"D:\Tools\Git\usr\bin\bash.exe")
        files = {given["BASH"], GIT + r"\bin\bash.exe"}
        self.assertEqual(_platform.find_bash(given, windows=True, isfile=files.__contains__), given["BASH"])

    def test_windows_finds_git_from_program_files_when_path_lacks_it(self):
        env = dict(WINDOWS_ENV, PATH=r"C:\Windows\system32", ProgramFiles=r"C:\Program Files")
        files = {WSL_BASH, GIT + r"\usr\bin\bash.exe"}
        self.assertEqual(_platform.find_bash(env, windows=True, isfile=files.__contains__), GIT + r"\usr\bin\bash.exe")

    def test_bash_is_a_full_path_that_runs(self):
        self.assertTrue(os.path.isabs(BASH), BASH)
        result = run([BASH, "-c", "printf %s \"$BASH_VERSION\""], timeout=30, capture_output=True, text=True,
                     stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"^\d+\.\d+")


class Posix(unittest.TestCase):
    def test_windows_paths_become_posix(self):
        self.assertEqual(posix(r"C:\Work\a b\install.sh", windows=True), "/c/Work/a b/install.sh")
        self.assertEqual(posix("D:/work/router", windows=True), "/d/work/router")
        self.assertEqual(posix(r"\\?\C:\long\path", windows=True), "/c/long/path")
        self.assertEqual(posix("/c/already", windows=True), "/c/already")

    def test_posix_paths_are_unchanged_elsewhere(self):
        self.assertEqual(posix("/tmp/a b/x.sh", windows=False), "/tmp/a b/x.sh")
        self.assertEqual(posix(r"odd\name", windows=False), r"odd\name")

    def test_bash_reads_a_script_by_its_posix_path(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "it's here.sh"
            script.write_bytes(b"printf ok\n")
            result = run([BASH, posix(script)], timeout=30, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual((result.returncode, result.stdout), (0, "ok"), result.stderr)


class PythonShim(unittest.TestCase):
    def test_shim_is_a_script_that_runs_this_interpreter(self):
        with tempfile.TemporaryDirectory() as folder:
            path = python_shim(folder)
            self.assertFalse(path.is_symlink())
            self.assertTrue(path.read_bytes().startswith(b"#!/bin/sh\n"))
            self.assertNotIn(b"\r", path.read_bytes())
            env = dict(os.environ, PATH=folder + os.pathsep + os.environ.get("PATH", os.defpath))
            code = "import sys; print(sys.executable); print(sys.argv[1:])"
            result = run([BASH, "-c", 'python3 -c "$1" "a b" "it\'s"', "_", code], timeout=60, env=env,
                         capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        executable, arguments = result.stdout.splitlines()
        self.assertEqual(os.path.normcase(os.path.realpath(executable)), os.path.normcase(os.path.realpath(sys.executable)))
        self.assertEqual(arguments, repr(["a b", "it's"]))

    def test_shim_quotes_an_odd_target(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "it's $HOME"
            target.write_bytes(b"#!/bin/sh\nprintf 'ran %s' \"$1\"\n")
            target.chmod(0o755)
            _platform.shim(folder, "tool", target)
            result = run([BASH, posix(Path(folder) / "tool"), "x"], timeout=30, capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
        self.assertEqual((result.returncode, result.stdout), (0, "ran x"), result.stderr)


class HomeEnv(unittest.TestCase):
    def test_python_home_follows_home_env_on_every_os(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(home_env(folder), {"HOME": folder, "USERPROFILE": folder})
            result = run([sys.executable, "-c", "from pathlib import Path; print(Path.home())"], timeout=30,
                         env=dict(os.environ, **home_env(folder)), capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
            self.assertEqual(result.stdout.strip(), folder)


class CanonicalTempRoot(unittest.TestCase):
    @_platform.skip_unless_symlinks("a TMPDIR reached through a symlinked ancestor, as macOS /var is")
    def test_root_has_no_symlink_component_when_tmpdir_is_an_alias(self):
        with tempfile.TemporaryDirectory() as parent:
            real = Path(os.path.realpath(parent)) / "real"
            real.mkdir()
            alias = Path(parent) / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with patch.dict(os.environ, {"TMPDIR": str(alias)}), patch.object(tempfile, "tempdir", None):
                temporary = tempfile.TemporaryDirectory(prefix="canonical-")
            with temporary:
                self.assertTrue(alias.is_symlink())
                self.assertEqual(Path(temporary.name).parent, alias)
                for given in (temporary, temporary.name):
                    root = _platform.canonical_temp_root(given)
                    self.assertEqual(str(root), os.path.realpath(str(root)))
                    self.assertEqual(str(root), os.path.realpath(temporary.name))
                    self.assertTrue(root.is_dir())


class Timeout(unittest.TestCase):
    def test_run_kills_the_whole_tree_on_timeout(self):
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run([BASH, "-c", "(sleep 40) & wait"], timeout=1, capture_output=True, stdin=subprocess.DEVNULL)
        self.assertLess(time.monotonic() - start, 20)

    def test_run_matches_subprocess_run(self):
        result = run([BASH, "-c", "read line; printf '%s' \"$line\"; exit 3"], timeout=30, input=b"hi\n",
                     capture_output=True)
        self.assertEqual((result.returncode, result.stdout), (3, b"hi"))

    def test_timeout_command_exits_124(self):
        script = str(Path(_platform.__file__))
        start = time.monotonic()
        late = subprocess.run([sys.executable, script, "timeout", "1", BASH, "-c", "(sleep 40) & wait"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(late.returncode, 124)
        self.assertLess(time.monotonic() - start, 20)
        done = subprocess.run([sys.executable, script, "timeout", "30", BASH, "-c", "exit 3"],
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 3)


class Skips(unittest.TestCase):
    def test_mode_skip_prints_its_reason(self):
        ran = []
        skipped = skip_unless_posix_modes("0o600 settings", posix_modes=False)(lambda: ran.append(1))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(unittest.SkipTest) as caught:
            skipped()
        self.assertEqual(ran, [])
        self.assertIn("no POSIX file modes", str(caught.exception))
        self.assertIn("SKIP", stderr.getvalue())
        self.assertIn("0o600 settings", stderr.getvalue())
        kept = skip_unless_posix_modes("x", posix_modes=True)(lambda: ran.append(1))
        kept()
        self.assertEqual(ran, [1])

    def test_inline_mode_check_prints_its_reason(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertFalse(_platform.posix_modes(self, "0o600 backup", has_modes=False))
            self.assertTrue(_platform.posix_modes(self, "unused", has_modes=True))
        self.assertIn("SKIP", stderr.getvalue())
        self.assertIn(self.id(), stderr.getvalue())
        self.assertIn("0o600 backup", stderr.getvalue())
        self.assertNotIn("unused", stderr.getvalue())

    def test_skips_only_where_modes_are_missing(self):
        self.assertEqual(_platform.POSIX_MODES, os.name != "nt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
