#!/usr/bin/env python3
"""pyrun.sh, the shared POSIX sh launcher: finds a real Python 3.9+, sets UTF-8, fails loudly when none exists."""
import ast
import getpass
import json
import os
from pathlib import Path
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import types
import unittest

from _platform import BASH

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
TOOL = "router" if (ROOT / "hooks/router").is_dir() else "harness"  # same file in both repositories
LAUNCHER = ROOT / ("hooks/router/pyrun.sh" if TOOL == "router" else "hooks/pyrun.sh")
VARIABLE = TOOL.upper() + "_PYTHON"
SH = shutil.which("sh") or "/bin/sh"
WINDOWS = os.name == "nt"
PROBE = ("import json, os, sys\n"
         "print(json.dumps({'argv': sys.argv[1:], 'utf8': os.environ.get('PYTHONUTF8'),\n"
         "                  'nopyc': os.environ.get('PYTHONDONTWRITEBYTECODE'), 'stdin': sys.stdin.read()}))\n")


def posix(path):
    return str(path).replace("\\", "/")


class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pyrun "))  # a space in every path
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home dir"
        self.home.mkdir()
        self.state = self.home / ".local/state"
        self.dir = self.tmp / "hooks dir"
        self.dir.mkdir()
        shutil.copy2(LAUNCHER, self.dir / "pyrun.sh")
        (self.dir / "probe.py").write_text(PROBE, encoding="utf-8")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.empty = self.tmp / "empty"
        self.empty.mkdir()

    def shim(self, name, body):
        path = self.bin / name
        path.write_bytes(("#!/bin/sh\n" + body + "\n").encode("utf-8"))  # LF on Windows too; 3.9 has no newline=
        path.chmod(0o755)
        return path

    def real_python(self, name="python3"):
        return self.shim(name, f"exec '{posix(sys.executable)}' \"$@\"")

    def env(self, path, **extra):
        env = {key: value for key, value in os.environ.items()
               if key not in (VARIABLE, "ROUTER_STATE", "XDG_STATE_HOME", "MSYSTEM") and not key.startswith("PYTHON")}
        env.update(HOME=str(self.home), USERPROFILE=str(self.home),
                   PATH=os.pathsep.join(str(item) for item in path))
        env.update(extra)
        return env

    def run_launcher(self, *args, path=(), stdin="", cwd=None, **extra):
        return subprocess.run([SH, str(self.dir / "pyrun.sh"), *args], input=stdin, text=True,
                              capture_output=True, env=self.env(path or [self.empty], **extra), cwd=cwd, timeout=60)

    def cache(self):
        return self.state / f"claude-{TOOL}" / "python"

    def test_shell_syntax_is_posix(self):
        for shell in ("dash", "bash"):
            found = shutil.which(shell)
            if not found:
                continue
            flags = ["--posix", "-n"] if shell == "bash" else ["-n"]
            result = subprocess.run([found, *flags, str(LAUNCHER)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, (shell, result.stderr))
        self.assertNotIn(b"\r", LAUNCHER.read_bytes())

    def test_fast_path_from_the_environment_needs_no_other_command(self):
        result = self.run_launcher(TOOL, "warn", "probe.py", "a b", "c", stdin="{}",
                                   **{VARIABLE: sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = json.loads(result.stdout)
        self.assertEqual(seen, {"argv": ["a b", "c"], "utf8": "1", "nopyc": "1", "stdin": "{}"})
        self.assertFalse(self.cache().exists(), "the fast path wrote a cache")

    def test_hook_streams_stay_utf8_under_an_ascii_environment(self):
        script = self.dir / "unicode.py"
        script.write_bytes(b"import sys\nprint(sys.flags.utf8_mode)\nprint('caf\\u00e9')\nprint(sys.stdin.read(), end='')\n")
        result = subprocess.run([SH, str(self.dir / "pyrun.sh"), TOOL, "warn", script.name],
                                input="entrée\n".encode("utf-8"), capture_output=True, timeout=30,
                                env=self.env([self.empty], **{VARIABLE: sys.executable, "PYTHONUTF8": "0",
                                            "PYTHONIOENCODING": "ascii", "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0"}))
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(result.stdout.replace(b"\r\n", b"\n"), "1\ncafé\nentrée\n".encode("utf-8"))

    def test_installer_startup_enforces_utf8_before_its_python_body(self):
        # Keep the actual shell startup byte-for-byte, replacing only the installer body.
        # No install or profile write is needed to observe interpreter startup settings.
        tree = self.tmp / "source café"
        relative = "hooks/router/pyrun.sh" if TOOL == "router" else "hooks/pyrun.sh"
        (tree / relative).parent.mkdir(parents=True)
        shutil.copy2(LAUNCHER, tree / relative)
        (tree / "café.txt").write_bytes("entrée\n".encode("utf-8"))
        startup = (ROOT / "install.sh").read_bytes().split(b"<<'PY'\n", 1)[0]
        body = b"from pathlib import Path\nimport sys\nprint(sys.flags.utf8_mode)\nprint((Path(sys.argv[1]) / 'caf\\u00e9.txt').read_text(), end='')\nprint('caf\\u00e9')\n"
        (tree / "install.sh").write_bytes(startup + b"<<'PY'\n" + body + b"PY\n")
        path = [Path(folder) for folder in os.environ.get("PATH", os.defpath).split(os.pathsep) if folder]
        for streams in (None, "ascii"):
            with self.subTest(streams=streams):
                env = self.env(path, **{VARIABLE: sys.executable, "PYTHONUTF8": "0", "LC_ALL": "C",
                                       "PYTHONCOERCECLOCALE": "0"})
                if streams:
                    env["PYTHONIOENCODING"] = streams
                result = subprocess.run([BASH, posix(tree / "install.sh")], env=env,
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
                self.assertEqual((result.returncode, result.stderr), (0, b""))
                self.assertEqual(result.stdout.replace(b"\r\n", b"\n"), "1\nentrée\ncafé\n".encode("utf-8"))

    def test_unset_and_empty_home_match_runtime_without_profile_writes(self):
        folder = ROOT / ("hooks/router" if TOOL == "router" else "hooks/checkpoint")
        runtime = self.dir / "home.py"
        runtime.write_bytes(b"import sys\nsys.path.insert(0, sys.argv[1])\n" +
                            (b"import common\nprint(common.state_path() / 'python')\n" if TOOL == "router" else
                             b"import storage\nprint(storage.local_cache() / 'python')\n"))
        prefix = LAUNCHER.read_text().split('if ! usable "$py"; then', 1)[0]
        selector = self.dir / "select-home.sh"
        shell = self.dir / "runtime-home.sh"
        cases = [("HOME", None), ("HOME", "")]
        if WINDOWS:
            cases += [("USERPROFILE", None), ("USERPROFILE", "")]
        for variable, value in cases:
            for xdg in ("", "relative", "~", "~/chosen state"):
                with self.subTest(variable=variable, value=value, xdg=xdg):
                    env = self.env([self.empty], XDG_STATE_HOME=xdg, **{VARIABLE: sys.executable})
                    prelude = ""
                    if variable == "USERPROFILE":
                        prelude = "unset HOME\n"
                        # Both fallbacks are expendable paths. No account profile is written.
                        drive, tail = os.path.splitdrive(str(self.home))
                        env.update(HOMEDRIVE=drive, HOMEPATH=tail)
                    if value is None:
                        prelude += "unset " + variable + "\n"
                    else:
                        prelude += variable + "=" + shlex.quote(value) + "; export " + variable + "\n"
                    # Set presence/value inside the shell, after its startup. Git Bash can
                    # synthesize HOME, and importing HOME='' can turn it into a literal '='.
                    shell.write_bytes((prelude + 'exec "$1" -B "$2" "$3"\n').encode())
                    selector.write_bytes((prelude + prefix + '\nprintf "%s\\n" "$cache"\n' +
                                          '''exec "$3" -B -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$cache"
''').encode())
                    expected = subprocess.run([SH, str(shell),
                                               sys.executable, str(runtime), str(folder)], env=env,
                                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                    self.assertEqual(expected.returncode, 0, expected.stderr)
                    chosen = subprocess.run([SH, str(selector), TOOL, "warn", sys.executable], env=env,
                                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                    self.assertEqual(chosen.returncode, 0, chosen.stderr)
                    raw, normalized = chosen.stdout.splitlines()
                    # Shell and Python may spell the same local path differently (Mac /var).
                    self.assertEqual(os.path.realpath(normalized),
                                     os.path.realpath(expected.stdout.strip()))
                    if xdg:
                        continue
                    # Prove the cache branch with no discoverable interpreter while avoiding
                    # any write to the account's real default cache. The selection is verified
                    # above and asserted again in shell. Only reads of that exact path can
                    # return the isolated interpreter. Wrong candidate paths remain misses.
                    cache = self.tmp / "isolated python cache"
                    cache.write_bytes((posix(sys.executable) + "\n").encode())
                    assertion = '\n[ "$cache" = ' + shlex.quote(raw) + ' ] || exit 93\n'
                    redirect = ('read_cached_python() {\n cached=\n if [ "$cache" = ' + shlex.quote(raw) +
                                ' ]; then IFS= read -r cached < ' + shlex.quote(posix(cache)) + ' || :; fi\n}')
                    source = LAUNCHER.read_text()
                    start = source.index('read_cached_python() {')
                    end = source.index('\n}', start) + 2
                    source = source[:start] + redirect + source[end:]
                    source = source.replace('if ! usable "$py"; then', assertion + 'if ! usable "$py"; then', 1)
                    isolated = self.dir / "isolated-home.sh"
                    isolated.write_bytes((prelude + source).encode())
                    env.pop(VARIABLE)
                    result = subprocess.run([SH, str(isolated), TOOL, "warn", "probe.py"], input="", env=env,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual((result.returncode, result.stderr), (0, ""))
                    self.assertEqual(json.loads(result.stdout)["argv"], [])

    def test_fast_path_from_the_cache_file(self):
        self.cache().parent.mkdir(parents=True)
        self.cache().write_bytes((posix(sys.executable) + "\n").encode("utf-8"))  # LF, as pyrun.sh writes it
        result = self.run_launcher(TOOL, "warn", "probe.py", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"], ["x"])

    def test_xdg_state_home_moves_the_cache(self):
        state = self.tmp / "xdg state"
        (state / f"claude-{TOOL}").mkdir(parents=True)
        (state / f"claude-{TOOL}/python").write_text(posix(sys.executable), encoding="utf-8")  # no newline
        result = self.run_launcher(TOOL, "status", "probe.py", XDG_STATE_HOME=str(state))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"], [])

    def test_router_state_override_reads_the_configured_cache(self):
        custom = self.home / "custom state"
        custom.mkdir()
        selected = custom / "python" if TOOL == "router" else self.cache()
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_bytes((posix(sys.executable) + "\n").encode("utf-8"))
        for override in (str(custom), "~/custom state"):
            with self.subTest(override=override):
                result = self.run_launcher(TOOL, "warn", "probe.py", ROUTER_STATE=override)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["utf8"], "1")

    def test_xdg_tilde_cache_matches_each_tools_state_directory(self):
        for value, expanded in (("~", self.home), ("~/chosen state", self.home / "chosen state"),
                                ("~\\chosen state", self.home / "chosen state")):
            with self.subTest(value=value):
                selected = expanded / "claude-router/python" if TOOL == "router" else self.cache()
                selected.parent.mkdir(parents=True, exist_ok=True)
                selected.write_bytes((posix(sys.executable) + "\n").encode("utf-8"))
                if TOOL == "router":
                    runtime = subprocess.run(
                        [sys.executable, "-B", "-c", "import common; print(common.state_path() / 'python')"],
                        cwd=ROOT / "hooks/router", env=self.env([self.empty], XDG_STATE_HOME=value),
                        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
                    self.assertEqual(runtime.returncode, 0, runtime.stderr)
                    self.assertEqual(Path(runtime.stdout.strip()), selected)
                result = self.run_launcher(TOOL, "warn", "probe.py", XDG_STATE_HOME=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["utf8"], "1")
                selected.unlink()

    def test_relative_xdg_state_home_uses_default_cache(self):
        self.cache().parent.mkdir(parents=True)
        self.cache().write_bytes((posix(sys.executable) + "\n").encode("utf-8"))
        result = self.run_launcher(TOOL, "warn", "probe.py", XDG_STATE_HOME="relative-state")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["utf8"], "1")

    def test_state_cache_selection_matches_the_native_runtime(self):
        # Run the real selection prefix, then observe both paths in the native child.
        # Git Bash converts environment and argv paths: a PureWindowsPath simulation
        # or a Python child started directly would miss that boundary.
        source = LAUNCHER.read_text().split('if ! usable "$py"; then', 1)[0]
        selector = self.dir / "select.sh"
        selector.write_bytes((source + '\nexec "$py" "$3" "$cache" "$tool" "$4"\n').encode())
        probe = self.dir / "state.py"
        probe.write_bytes(b'''import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
chosen = os.environ.get('ROUTER_STATE', '') if sys.argv[2] == 'router' else ''
chosen = chosen or os.environ.get('XDG_STATE_HOME', '')
unc = chosen.startswith(('//', chr(92) * 2))
normalize = os.path.abspath if unc else os.path.realpath
if sys.argv[2] == 'router':
 import common
 selected = common.state_path()
else:
 import storage
 # UNC selection is tested without opening a share or resolving its filesystem.
 if unc:
  storage.os.path.realpath = lambda path: str(path)
 selected = storage.local_cache()
print(json.dumps([os.path.normcase(str(Path(normalize(sys.argv[1])))),
                  os.path.normcase(str(Path(normalize(str(selected / 'python')))))]))
''')
        folder = ROOT / ("hooks/router" if TOOL == "router" else "hooks/checkpoint")
        user = os.environ.get("USERNAME") if WINDOWS else getpass.getuser()
        values = ("", "relative-state", "/", "\\", "//", "//server", "//server/", "//server//share", "\\\\server", "\\\\server\\",
                  "C:", "C:/", "C:\\", "/chosen-state", "C:/chosen-state", "C:\\chosen-state",
                  "C:chosen-state", "//server/share/chosen-state", "\\\\server\\share\\chosen-state",
                  "/c/chosen-state", "~", "~/chosen-state", "~\\chosen-state", "~///server", "~///server/", "~" + user)
        for variable in ("XDG_STATE_HOME", "ROUTER_STATE"):
            for value in values:
                # Incomplete UNC names are classification cases for XDG. A Router
                # override accepts them unconditionally and could address a share.
                if variable == "ROUTER_STATE" and value in ("//", "//server", "//server/", "//server//share", "\\\\server", "\\\\server\\"):
                    continue
                for exclude in (False, True) if WINDOWS else (False,):
                    with self.subTest(variable=variable, value=value, exclude=exclude):
                        extra = {VARIABLE: sys.executable, variable: value}
                        if exclude:
                            extra["MSYS2_ENV_CONV_EXCL"] = variable
                        result = subprocess.run([SH, str(selector), TOOL, "warn", str(probe), str(folder)],
                                                cwd=self.tmp, env=self.env([self.empty], **extra),
                                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        paths = json.loads(result.stdout)
                        self.assertEqual(paths[0], paths[1])

    def test_drive_xdg_cache_without_discovery_matches_the_runtime(self):
        state = self.tmp / "chosen state"
        chosen = str(state) if WINDOWS else "C:/chosen-state"
        selected = state / f"claude-{TOOL}/python" if WINDOWS else self.cache()
        selected.parent.mkdir(parents=True)
        selected.write_bytes((posix(sys.executable) + "\n").encode())
        result = self.run_launcher(TOOL, "warn", "probe.py", XDG_STATE_HOME=chosen)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout)["argv"], [])

    def test_tilde_suffix_joins_match_the_native_runtime_lexically(self):
        # The cache --find reads must be the runtime's: compat.expanduser joins HOME and the
        # suffix as pathlib does, so ~//tmp is /tmp on POSIX and keeps HOME's drive on Windows.
        # All rows are lexical, including tilde-expanded and incomplete UNC forms.
        # Never resolve, stat or open any candidate path in this matrix.
        source = LAUNCHER.read_text().split('if ! usable "$py"; then', 1)[0]
        selector = self.dir / "select-suffix.sh"
        selector.write_bytes((source + '\nexec "$py" "$3" "$cache" "$tool" "$4"\n').encode())
        probe = self.dir / "suffix.py"
        probe.write_bytes(b'''import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
if sys.argv[2] == 'router':
 import common
 selected = common.state_path()
else:
 import storage
 storage.os.path.realpath = lambda path: str(path)
 selected = storage.local_cache()
normalize = lambda path: os.path.normcase(str(Path(os.path.abspath(path))))
print(json.dumps([normalize(sys.argv[1]), normalize(str(selected / 'python'))]))
''')
        folder = ROOT / ("hooks/router" if TOOL == "router" else "hooks/checkpoint")
        drive = self.home.drive or "C:"
        other = "Z:" if drive.upper() != "Z:" else "D:"
        suffixes = ("", ".", "..", "child", "child//next", "child\\next", "/child", "//child", "///child",
                    "/", "//", "\\child", "\\\\child", "//server/share/state", "//server//share/state",
                    "///server/share/state", "\\\\server\\share\\state", "//server", "//server/",
                    "\\\\server", "\\\\server\\", drive, drive + "child", drive.lower() + "child",
                    drive + "/child", drive + "\\child", other + "child", other + "/child")
        user = os.environ.get("USERNAME") if WINDOWS else getpass.getuser()
        values = [prefix + suffix for prefix in ("~/", "~\\") for suffix in suffixes]
        values += ["~", "~//tmp", "~/x", "~" + user + "/x", "", "relative-state", posix(self.tmp / "absolute state")]
        # Git Bash shows a TEMP HOME as its /tmp mount and a profile HOME as /c/...: test both.
        homes = (("mount", {}), ("drive", {"HOME": "/" + drive[0].lower() + posix(self.home)[2:]})) if WINDOWS else (("home", {}),)
        cases = [(home, exclude) for home in homes for exclude in ((False, True) if WINDOWS else (False,))
                 if not (exclude and home[0] == "drive")]
        for value in values:
            for variable in ("XDG_STATE_HOME", "ROUTER_STATE"):
                for (name, home), exclude in cases:
                    with self.subTest(value=value, variable=variable, exclude=exclude, home=name):
                        extra = {VARIABLE: sys.executable, variable: value, **home}
                        if exclude:
                            extra["MSYS2_ENV_CONV_EXCL"] = variable
                        result = subprocess.run([SH, str(selector), "--find", TOOL, str(probe), str(folder)],
                                                cwd=self.tmp, env=self.env([self.empty], **extra),
                                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        paths = json.loads(result.stdout)
                        self.assertEqual(paths[0], paths[1])

    def test_rooted_tilde_suffix_cache_without_discovery(self):
        # ~//<path> leaves HOME (POSIX) or keeps only HOME's drive (Windows), in shell alone, so a
        # cache-only startup finds the interpreter. Other native drive suffixes need a resolver.
        state = self.tmp / "rooted state"
        rooted = posix(state)[2:] if WINDOWS else posix(state)
        cached = self.shim("cached-python", f"exec '{posix(sys.executable)}' \"$@\" cache-hit")
        self.real_python()  # discoverable, yet the cache at the runtime's path must win
        if WINDOWS:
            # Git Bash shows a profile HOME as /c/...; a HOME under TEMP becomes its /tmp
            # mount, whose drive only the interpreter knows, as for a drive suffix.
            drive_home = {"HOME": "/" + self.home.drive[0].lower() + posix(self.home)[2:]}
            rows = [(prefix + rooted, home, home is drive_home) for prefix in ("~/", "~\\")
                    for home in (drive_home, {})] + [("~/" + posix(state), {}, False)]
        else:
            rows = [(prefix + rooted, {}, True) for prefix in ("~/", "~\\", "~//")]
        for value, home, shell_only in rows:
            for variable in ("XDG_STATE_HOME", "ROUTER_STATE"):
                with self.subTest(value=value, variable=variable, home=home):
                    selected = self.cache() if TOOL == "harness" else state / ("claude-router/python" if variable == "XDG_STATE_HOME" else "python")
                    selected.parent.mkdir(parents=True, exist_ok=True)
                    selected.write_bytes((posix(cached) + "\n").encode())
                    for path in ([self.bin, Path(SH).parent], [self.empty]) if shell_only else ([self.bin, Path(SH).parent],):
                        found = self.run_launcher("--find", TOOL, path=path, **home, **{variable: value})
                        self.assertEqual((found.returncode, found.stdout, found.stderr), (0, posix(cached) + "\n", ""))
                        result = self.run_launcher(TOOL, "warn", "probe.py", path=path, **home, **{variable: value})
                        self.assertEqual((result.returncode, result.stderr), (0, ""))
                        self.assertEqual(json.loads(result.stdout)["argv"], ["cache-hit"])
                    selected.unlink()

    @unittest.skipUnless(WINDOWS, "Windows Path.home reads USERPROFILE")
    def test_home_and_userprofile_choose_each_tools_runtime_cache(self):
        profile = self.tmp / "other profile"
        selected = (profile / ".local/state/claude-harness/python" if TOOL == "harness" else self.cache())
        selected.parent.mkdir(parents=True)
        selected.write_bytes((posix(sys.executable) + "\n").encode())
        result = self.run_launcher(TOOL, "warn", "probe.py", USERPROFILE=str(profile))
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(json.loads(result.stdout)["argv"], [])

    @unittest.skipUnless(WINDOWS, "Git Bash converts native Windows environment paths")
    def test_windows_slash_state_paths_use_the_runtime_cache_without_discovery(self):
        state = self.tmp / "chosen state"
        drive_form = "/" + state.drive[0].lower() + posix(state)[2:]
        native_root = posix(state)[2:]
        for variable, value, excluded in (("XDG_STATE_HOME", drive_form, False),
                                          ("XDG_STATE_HOME", drive_form, True),
                                          ("ROUTER_STATE", native_root, True)):
            with self.subTest(variable=variable, excluded=excluded):
                selected = self.cache()
                if variable == "XDG_STATE_HOME" and not excluded:
                    selected = state / f"claude-{TOOL}/python"
                elif variable == "ROUTER_STATE" and TOOL == "router":
                    selected = state / "python"
                selected.parent.mkdir(parents=True, exist_ok=True)
                selected.write_bytes((posix(sys.executable) + "\n").encode())
                extra = {variable: value}
                if excluded:
                    extra["MSYS2_ENV_CONV_EXCL"] = variable
                # Root-relative paths use the child cwd drive, which may differ from CI TEMP.
                result = self.run_launcher(TOOL, "warn", "probe.py", cwd=self.tmp, **extra)
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(json.loads(result.stdout)["argv"], [])
                selected.unlink()

    def test_named_user_state_cache_matches_runtime_and_is_used(self):
        user = os.environ.get("USERNAME") if WINDOWS else getpass.getuser()
        named_home = self.home if WINDOWS else Path("~" + user).expanduser()
        relative = os.path.relpath(self.tmp / "named state", named_home)
        value = "~" + user + "/" + posix(relative)
        cached_python = self.shim("cached-python", f"exec '{posix(sys.executable)}' \"$@\" cache-hit")
        self.real_python()
        for variable in ("XDG_STATE_HOME", "ROUTER_STATE"):
            with self.subTest(variable=variable):
                selected = self.cache()
                if TOOL == "router":
                    runtime = subprocess.run(
                        [sys.executable, "-B", "-c", "import common; print(common.state_path() / 'python')"],
                        cwd=ROOT / "hooks/router", env=self.env([self.empty], **{variable: value}),
                        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
                    self.assertEqual(runtime.returncode, 0, runtime.stderr)
                    selected = Path(runtime.stdout.strip()).resolve()
                    self.assertTrue(selected.is_relative_to(self.tmp.resolve()))
                selected.parent.mkdir(parents=True, exist_ok=True)
                selected.write_bytes((posix(cached_python) + "\n").encode())
                result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent],
                                           **{variable: value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["argv"], ["cache-hit"])
                selected.unlink()
                result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent],
                                           **{variable: value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(os.path.realpath(selected.read_text().strip()), os.path.realpath(sys.executable))
                selected.unlink()

    def test_unknown_named_user_does_not_use_a_different_cache(self):
        self.real_python()
        self.cache().parent.mkdir(parents=True)
        fake = self.shim("cached-python", f"exec '{posix(sys.executable)}' \"$@\" wrong-cache")
        self.cache().write_bytes((posix(fake) + "\n").encode())
        for variable in ("XDG_STATE_HOME", "ROUTER_STATE"):
            with self.subTest(variable=variable):
                value = "~pyrun-no-such-user-031/state"
                extra = {variable: value, "USERPROFILE": str(self.home), "USERNAME": "pyrun-current-user"}
                result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent], **extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["argv"], [] if TOOL == "router" else ["wrong-cache"])
                if TOOL == "router":
                    runtime = subprocess.run(
                        [sys.executable, "-B", "-c", "import common; common.state_path()"],
                        cwd=ROOT / "hooks/router", env=self.env([self.empty], **extra),
                        stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30)
                    self.assertNotEqual(runtime.returncode, 0)
                    self.assertIn("RuntimeError", runtime.stderr)

    def test_router_state_override_recache_uses_the_configured_directory(self):
        custom = self.home / "custom state"
        custom.mkdir()
        self.cache().parent.mkdir(parents=True, exist_ok=True)
        self.real_python("python3")
        result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent],
                                   ROUTER_STATE=str(custom))
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = custom / "python" if TOOL == "router" else self.cache()
        unused = self.cache() if TOOL == "router" else custom / "python"
        self.assertEqual(os.path.realpath(selected.read_text().strip()), os.path.realpath(sys.executable))
        self.assertFalse(unused.exists())

    def test_a_missing_cached_interpreter_is_found_again_and_recached(self):
        self.cache().parent.mkdir(parents=True)  # the tool's hooks made its state folder
        self.cache().write_text(posix(self.tmp / "gone/python3") + "\n", encoding="utf-8")
        self.real_python("python3")
        result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        cached = self.cache().read_text(encoding="utf-8").strip()
        self.assertTrue(Path(cached).is_file(), cached)
        self.assertNotIn("gone", cached)

    def test_cache_miss_probes_and_caches_the_real_executable(self):
        self.real_python("python3")
        self.cache().parent.mkdir(parents=True, mode=0o700)
        result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        cached = self.cache().read_text(encoding="utf-8").strip()
        self.assertEqual(os.path.realpath(cached), os.path.realpath(sys.executable))
        if not WINDOWS:
            self.assertEqual(self.cache().stat().st_mode & 0o777, 0o600)

    def test_cache_miss_without_a_state_folder_writes_nothing(self):
        self.real_python("python3")
        result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.home.rglob("*")), [], "a read-only command created files")

    def test_store_stub_that_exits_49_silently_is_skipped(self):
        self.shim("python3", "exit 49")
        self.real_python("python")
        self.cache().parent.mkdir(parents=True)
        result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["utf8"], "1")
        self.assertEqual(os.path.realpath(self.cache().read_text(encoding="utf-8").strip()),
                         os.path.realpath(sys.executable))

    def test_python_older_than_3_9_is_rejected(self):
        self.shim("python3", 'case $1 in -c) exit 0;; esac; echo ran-old; exit 0')  # prints nothing for the probe
        result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin])
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("ran-old", result.stdout)

    def test_a_too_old_python3_does_not_end_the_search(self):
        self.shim("python3", 'case $1 in -c) exit 0;; esac; echo ran-old; exit 0')
        self.real_python("python")
        result = self.run_launcher(TOOL, "warn", "probe.py", path=[self.bin, Path(SH).parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["utf8"], "1")

    def test_no_python_warn_exits_1_with_one_stderr_line(self):
        result = self.run_launcher(TOOL, "warn", "probe.py")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("Python 3.9", result.stderr)
        self.assertIn(VARIABLE, result.stderr)
        self.assertFalse(self.cache().exists())

    def test_no_python_status_prints_on_stdout_and_exits_0(self):
        result = self.run_launcher(TOOL, "status", "probe.py")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertIn("Python 3.9", result.stdout)

    def test_no_python_block_exits_2(self):
        result = self.run_launcher(TOOL, "block", "probe.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Python 3.9", result.stderr)

    def test_find_prints_the_interpreter_and_writes_nothing(self):
        self.real_python("python3")
        result = self.run_launcher("--find", TOOL, path=[self.bin, Path(SH).parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.path.realpath(result.stdout.strip()), os.path.realpath(sys.executable))
        self.assertFalse(self.cache().exists(), "--find wrote a cache")
        missing = self.run_launcher("--find", TOOL)
        self.assertEqual((missing.returncode, missing.stdout), (1, ""))
        self.assertIn("Python 3.9", missing.stderr)

    def test_dash_runs_a_script_from_stdin(self):
        result = self.run_launcher(TOOL, "warn", "-", "one", stdin="import sys, os\nprint(sys.argv[1:], os.environ['PYTHONUTF8'])\n",
                                   **{VARIABLE: sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "['one'] 1")

    def test_a_backslash_launcher_path_finds_its_scripts(self):
        # Windows callers may pass C:\x\pyrun.sh; on POSIX a file name holding a backslash stands in for it.
        name = "hooks dir\pyrun.sh"
        if not WINDOWS:
            shutil.copy2(LAUNCHER, self.tmp / name)
        result = subprocess.run([SH, name, TOOL, "warn", "probe.py", "q"], input="", text=True, capture_output=True,
                                cwd=self.tmp, env=self.env([self.empty], **{VARIABLE: sys.executable}), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"], ["q"])

    def test_absolute_script_path(self):
        result = self.run_launcher(TOOL, "warn", posix(self.dir / "probe.py"), "z", **{VARIABLE: sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["argv"], ["z"])

    def test_git_bash_drive_paths_become_windows_paths(self):
        echo = self.shim("fakepy", 'for a; do printf "[%s]\\n" "$a"; done')
        result = self.run_launcher(TOOL, "warn", "/c/Users/A B/x.py", "/c/arg", path=[self.empty],
                                   MSYSTEM="MINGW64", **{VARIABLE: posix(echo)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["[c:/Users/A B/x.py]", "[/c/arg]"])
        plain = self.run_launcher(TOOL, "warn", "/c/Users/x.py", path=[self.empty], **{VARIABLE: posix(echo)})
        self.assertEqual(plain.stdout.splitlines(), ["[/c/Users/x.py]"])

    def test_unknown_tool_or_mode_is_an_error(self):
        self.assertEqual(self.run_launcher("motion", "warn", "probe.py").returncode, 1)
        result = self.run_launcher(TOOL, "loud", "probe.py", **{VARIABLE: sys.executable})
        self.assertEqual(result.returncode, 1)
        self.assertIn("mode", result.stderr)

    @unittest.skipUnless(WINDOWS, "launcher timing is measured on Windows Git Bash")
    def test_windows_launcher_timing(self):
        times = []
        for _ in range(20):
            start = time.perf_counter()
            result = self.run_launcher(TOOL, "warn", "probe.py", **{VARIABLE: sys.executable})
            times.append((time.perf_counter() - start) * 1000)
            self.assertEqual(result.returncode, 0, result.stderr)
        direct = []
        for _ in range(20):
            start = time.perf_counter()
            subprocess.run([sys.executable, str(self.dir / "probe.py")], input="", capture_output=True, timeout=60)
            direct.append((time.perf_counter() - start) * 1000)
        print(f"pyrun timing: launcher median {statistics.median(times):.0f} ms, "
              f"python alone {statistics.median(direct):.0f} ms, over 20 runs", file=sys.stderr)


def hook_command():
    """install.sh's hook_command(), lifted out of its Python heredoc."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    tree = ast.parse(text.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0])
    keep = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "hook_command"
            or isinstance(node, ast.Assign) and any(getattr(target, "id", "") in ("NO_PYTHON_EXIT", "WINDOWS_PYTHON")
                                                    for target in node.targets)]
    namespace = {"Path": Path, "shlex": shlex, "sys": sys, "os": os}
    sys.path.insert(0, str(ROOT / ("hooks/router" if TOOL == "router" else "hooks/checkpoint")))
    import compat  # both installers read the home through compat.home()
    namespace.update(compat=compat, common=types.SimpleNamespace(compat=compat))
    exec(compile(ast.Module(body=keep, type_ignores=[]), "install.sh", "exec"), namespace)
    return namespace["hook_command"]


# (rel, mode, Claude Code command with the default home) per tool
CASES = {"router": [("context_guard.py", "warn", 'sh "$HOME/.claude/hooks/router/pyrun.sh" router warn context_guard.py')],
         "harness": [("guard/danger_cmd_guard.py", "block",
                      'sh "$HOME/.claude/harness/hooks/pyrun.sh" harness block guard/danger_cmd_guard.py'),
                     ("../statusline/statusline.py", "status",
                      'sh "$HOME/.claude/harness/hooks/pyrun.sh" harness status ../statusline/statusline.py')]}
FOLDER = "hooks/router" if TOOL == "router" else "harness/hooks"


class HookCommands(unittest.TestCase):
    def setUp(self):
        self.build = hook_command()
        self.tmp = Path(tempfile.mkdtemp(prefix="hook cmd "))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_default_home_uses_home_and_no_windows_form(self):
        for rel, mode, expected in CASES[TOOL]:
            user = sys.modules["compat"].home()  # $HOME first, as the installers read it
            fields = self.build("claude", (user / ".claude").absolute(), rel, mode, False)
            self.assertEqual(fields, {"command": expected})
            codex = self.build("codex", (user / ".codex").absolute(), rel, mode, False)
            self.assertEqual(set(codex), {"command"})
            self.assertIn(f"/{FOLDER}/pyrun.sh\" {TOOL} {mode} {rel}", codex["command"])

    def test_windows_or_spaced_home_writes_quoted_absolute_paths(self):
        home = self.tmp / "O'Brien home" / ".claude"
        for windows in (False, True):
            for rel, mode, _ in CASES[TOOL]:
                words = shlex.split(self.build("claude", home, rel, mode, windows)["command"])
                self.assertEqual(words[-4:], [(home / FOLDER / "pyrun.sh").as_posix(), TOOL, mode, rel])
                self.assertEqual(words[-5], "sh")

    def test_codex_command_windows_shape(self):
        home = self.tmp / "A B's" / ".codex"
        for rel, mode, _ in CASES[TOOL]:
            fields = self.build("codex", home, rel, mode, True)
            self.assertEqual(set(fields), {"command", "command_windows"})
            text = fields["command_windows"]
            python = "'" + Path(sys.executable).as_posix().replace("'", "''") + "'"
            script = "'" + (home / FOLDER / rel).as_posix().replace("'", "''") + "'"
            self.assertIn(f"$p={python}; if (Test-Path -LiteralPath $p) {{ & $p -B -X utf8 {script}", text)
            self.assertIn("exit $LASTEXITCODE }", text)
            self.assertIn("$env:PYTHONIOENCODING='utf-8';", text)
            self.assertIn("A B''s", text)
            self.assertTrue(text.endswith(f"exit {dict(warn=1, block=2, status=0)[mode]}"), text)
            self.assertIn(f"[Console]::Error.WriteLine('{TOOL}: Python missing; rerun install.sh')", text)
            self.assertNotIn(" sh ", text)
            if TOOL == "router":
                self.assertTrue(text.startswith("$env:CODEX_HOME="), text)
                self.assertIn("; $env:ROUTER_HOME='" + (home / "router").as_posix().replace("'", "''") + "'; ", text)
        self.assertEqual(set(self.build("claude", home, CASES[TOOL][0][0], CASES[TOOL][0][1], True)), {"command"})

    @unittest.skipUnless(WINDOWS and shutil.which("powershell.exe"), "PowerShell runs on Windows")
    def test_native_hook_streams_stay_utf8_under_an_ascii_environment(self):
        home = self.tmp / "unicode home" / ".codex"
        folder = home / FOLDER
        folder.mkdir(parents=True)
        (folder / "unicode.py").write_bytes(
            b"import sys\nprint(sys.flags.utf8_mode)\nprint('caf\\u00e9')\nprint(sys.stdin.read(), end='')\n")
        command = self.build("codex", home, "unicode.py", "warn", True)["command_windows"]
        env = dict(os.environ, HOME=str(self.tmp), USERPROFILE=str(self.tmp),
                   PYTHONUTF8="0", PYTHONIOENCODING="ascii")
        result = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-Command", command],
                                input="entrée\n".encode("utf-8"), env=env, capture_output=True, timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(result.stdout.replace(b"\r\n", b"\n"), "1\ncafé\nentrée\n".encode("utf-8"))

    @unittest.skipUnless(WINDOWS and shutil.which("powershell.exe"), "PowerShell runs on Windows")
    def test_powershell_passes_the_hook_exit_code_through(self):
        home = self.tmp / "ps home" / ".codex"
        folder = home / FOLDER
        if TOOL == "harness":
            shutil.copytree(ROOT / "hooks", folder)
            rel, mode, payload, code = "guard/danger_cmd_guard.py", "block", {
                "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(self.tmp),
                "tool_input": {"command": "rm -rf /"}}, 2
        else:
            folder.mkdir(parents=True)
            (folder / "exit_with.py").write_text("import json, sys\nsys.exit(json.load(sys.stdin)['code'])\n",
                                                 encoding="utf-8")
            rel, mode, payload, code = "exit_with.py", "warn", {"code": 2}, 2
        command = self.build("codex", home, rel, mode, True)["command_windows"]
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], input=json.dumps(payload),
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        missing = command.replace(Path(sys.executable).as_posix(), (self.tmp / "no/python.exe").as_posix())
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", missing], input="{}",
                                text=True, capture_output=True, timeout=120)
        self.assertEqual(result.returncode, dict(warn=1, block=2, status=0)[mode], result.stderr)
        self.assertIn("Python missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
