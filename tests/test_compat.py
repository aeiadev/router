#!/usr/bin/env python3
"""hooks/router/compat.py: the one home of Router's POSIX-only calls, each with a Windows path.

Run plain, and again as `PYTHONPATH=tests/win_sim python3 tests/test_compat.py`, which hides fcntl,
pwd, os.geteuid, os.getuid, os.O_NOFOLLOW, os.O_NONBLOCK, os.pread and os.fchmod and fakes msvcrt.
The WinSim class runs the hooks under that simulation in child processes either way. A static test
fails when any shipped Python outside compat.py imports fcntl or pwd or names a banned os call.
Nothing here calls a model or the network.
"""
import ast
import contextlib
import errno
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks/router"
WIN_SIM = ROOT / "tests/win_sim"
BANNED_MODULES = {"fcntl", "pwd"}
BANNED_OS = {"geteuid", "getuid", "O_NOFOLLOW", "O_NONBLOCK", "pread", "fchmod"}
SIMULATED = getattr(sys.modules.get("msvcrt"), "WIN_SIM", False)
WORKERS, ROUNDS = 20, 10

spec = importlib.util.spec_from_file_location("router_compat_under_test", HOOKS / "compat.py")
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)

WORKER = r'''
import importlib.util, os, sys, time
sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("compat", os.path.join(sys.argv[1], "compat.py"))
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)
lock, data, counter, me = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
for round in range(int(sys.argv[6])):
    with compat.locked(lock, timeout=120):
        with open(counter, "rb") as stream:
            value = int(stream.read() or b"0")
        line = (f"{me}-{round}:" + "x" * 300 + "\n").encode("ascii")
        with open(data, "ab") as stream:
            stream.write(line[:150]); stream.flush(); time.sleep(0.002)
            stream.write(line[150:]); stream.flush()
        with open(counter, "wb") as stream:
            stream.write(str(value + 1).encode("ascii"))
print("ok")
'''


def sim_env(**extra):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", **extra)
    env["PYTHONPATH"] = os.pathsep.join([str(WIN_SIM)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p])
    return env


class CompatTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="router-compat-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def hammer(self, env):
        lock, data, counter = self.tmp / "work.lock", self.tmp / "work.txt", self.tmp / "count"
        counter.write_bytes(b"0")
        script = self.tmp / "worker.py"
        script.write_text(WORKER, encoding="utf-8")
        procs = [subprocess.Popen([sys.executable, "-B", str(script), str(HOOKS), str(lock), str(data),
                                   str(counter), f"w{index:02d}", str(ROUNDS)], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for index in range(WORKERS)]
        for proc in procs:
            out, err = proc.communicate(timeout=300)
            self.assertEqual((proc.returncode, out.strip()), (0, b"ok"), err.decode(errors="replace")[-400:])
        lines = data.read_bytes().split(b"\n")
        self.assertEqual(lines.pop(), b"")
        expected = {f"w{i:02d}-{r}:".encode() + b"x" * 300 for i in range(WORKERS) for r in range(ROUNDS)}
        self.assertEqual(len(lines), WORKERS * ROUNDS, "lost or torn lines")
        self.assertEqual(set(lines), expected, "torn lines")
        self.assertEqual(counter.read_bytes(), str(WORKERS * ROUNDS).encode(), "lost update")

    def test_lock_excludes_twenty_processes(self):
        self.hammer(dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))

    @unittest.skipIf(os.name == "nt", "the real msvcrt is under test on Windows")
    def test_lock_excludes_twenty_simulated_windows_processes(self):
        self.hammer(sim_env())

    def test_lock_timeout_raises_and_frees(self):
        lock = self.tmp / "state.lock"
        with compat.locked(lock):
            start = time.monotonic()
            with self.assertRaises(compat.LockTimeout):
                with compat.locked(lock, timeout=0.3):
                    self.fail("second holder got the lock")
            self.assertGreaterEqual(time.monotonic() - start, 0.25)
        with compat.locked(lock, timeout=0.3):
            pass  # the timed-out attempt left nothing held
        self.assertTrue(issubclass(compat.LockTimeout, OSError))
        self.assertTrue(lock.exists(), "the lock is its own file")

    def test_lock_never_blocks_in_msvcrt(self):
        source = (HOOKS / "compat.py").read_text(encoding="utf-8")
        self.assertIn("LK_NBLCK", source)
        self.assertNotIn("LK_LOCK", source.replace("LK_NBLCK", ""))

    def test_read_flags(self):
        for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY", "O_CLOEXEC"):
            bit = getattr(os, name, 0)
            self.assertEqual(compat.READ_FLAGS & bit, bit, name)
        self.assertEqual(compat.READ_FLAGS & (os.O_WRONLY | os.O_RDWR), 0)
        target = self.tmp / "target"
        target.write_bytes(b"data")
        fd = compat.open_fd(target)
        try:
            self.assertEqual(os.read(fd, 10), b"data")
        finally:
            os.close(fd)
        link = self.tmp / "link"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        for nofollow in (compat.NOFOLLOW, 0):  # 0: the lstat and fstat comparison alone refuses it
            with mock.patch.object(compat, "NOFOLLOW", nofollow), self.assertRaises(OSError):
                os.close(compat.open_fd(link))

    def test_binary_bytes_round_trip(self):
        path = self.tmp / "raw"
        data = b"a\nb\r\nc\x1ad\n"
        fd = compat.open_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        fd = compat.open_fd(path)
        try:
            self.assertEqual(os.read(fd, 100), data)
        finally:
            os.close(fd)
        self.assertEqual(path.read_bytes(), data)

    def test_owned(self):
        info = os.stat(self.tmp)
        self.assertTrue(compat.owned(info))
        foreign = os.stat_result((info.st_mode, 0, 0, 1, info.st_uid + 1, 0, 0, 0, 0, 0))
        with mock.patch.object(compat, "_geteuid", lambda: info.st_uid):
            self.assertFalse(compat.owned(foreign))
            self.assertTrue(compat.owned(os.stat_result((0, 0, 0, 1, info.st_uid, 0, 0, 0, 0, 0))))
        with mock.patch.object(compat, "_geteuid", None):
            self.assertTrue(compat.owned(foreign), "without geteuid (Windows) ownership is not checked")

    def test_set_private(self):
        path = self.tmp / "file"
        path.write_bytes(b"x")
        fd = os.open(path, os.O_RDONLY)
        try:
            with mock.patch.object(compat, "_fchmod", None):
                self.assertFalse(compat.set_private(fd, 0o600))
        finally:
            os.close(fd)
        if os.name == "nt" or SIMULATED:
            return
        os.chmod(path, 0o644)
        self.assertTrue(compat.set_private(path, 0o600))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_replace_retries_permission_error(self):
        source, target = self.tmp / "a", self.tmp / "b"
        source.write_bytes(b"new")
        real, calls = os.replace, []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) < 3:
                raise PermissionError(errno.EACCES, "in use")
            return real(src, dst)
        with mock.patch.object(compat.os, "replace", flaky), mock.patch.object(compat.time, "sleep"):
            compat.replace(source, target)
        self.assertEqual((len(calls), target.read_bytes()), (3, b"new"))
        always = mock.Mock(side_effect=PermissionError(errno.EACCES, "in use"))
        with mock.patch.object(compat.os, "replace", always), mock.patch.object(compat.time, "sleep"):
            with self.assertRaises(PermissionError):
                compat.replace(target, source)
        self.assertEqual(always.call_count, compat.REPLACE_TRIES)

    def test_pread_fallback(self):
        path = self.tmp / "data"
        path.write_bytes(b"0123456789")
        fd = os.open(path, os.O_RDONLY | compat.BINARY)
        try:
            os.lseek(fd, 7, os.SEEK_SET)
            self.assertEqual(compat.pread(fd, 4, 2), b"2345")
            with mock.patch.object(compat, "_pread", None):
                self.assertEqual(compat.pread(fd, 4, 1), b"1234")
                self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 7, "fallback keeps the position")
        finally:
            os.close(fd)

    def test_text_and_bytes_writes_are_lf(self):
        path = self.tmp / "sub/out.toml"
        compat.write_text(path, 'a = "b"\nc = 1\n')
        self.assertEqual(path.read_bytes(), b'a = "b"\nc = 1\n')
        compat.write_bytes(path, b"x\ny\n")
        self.assertEqual(path.read_bytes(), b"x\ny\n")
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["out.toml"], "no temp file left")
        if os.name != "nt" and not SIMULATED:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_home_forms(self):
        env = {"HOME": "/c/Users/A B", "USERPROFILE": "C:\\Users\\A B"}
        with mock.patch.dict(os.environ, env):
            forms = compat.home_forms()
            self.assertIn("/c/Users/A B", forms)
            self.assertIn("C:/Users/A B", forms)
            self.assertEqual(compat.tilde("c:\\users\\a b\\proj x"), "~\\proj x")
            self.assertEqual(compat.tilde("/C/Users/A B/x"), "~/x")
            self.assertEqual(compat.tilde("C:/Users/A B"), "~")
            self.assertEqual(compat.tilde("C:/Users/A Bx/y"), "C:/Users/A Bx/y")
        with mock.patch.dict(os.environ, {"HOME": "/workspace/u", "USERPROFILE": ""}):
            self.assertEqual(compat.tilde("/workspace/u/repo"), "~/repo")
            self.assertEqual(compat.tilde("/workspace/user"), "/workspace/user")

    def test_home_prefers_HOME_over_the_profile(self):
        with mock.patch("pathlib.Path.home", return_value=Path("/profile")):  # Windows: USERPROFILE only
            with mock.patch.dict(os.environ, {"HOME": str(self.tmp / "h")}):
                self.assertEqual(compat.home(), self.tmp / "h")
                self.assertEqual(compat.expanduser("~/a/b"), self.tmp / "h/a/b")
                self.assertEqual(compat.expanduser("~"), self.tmp / "h")
                self.assertEqual(compat.expanduser(self.tmp / "x"), self.tmp / "x")
            with mock.patch.dict(os.environ, {"HOME": ""}):
                self.assertEqual(compat.home(), Path("/profile"))

    def test_is_absolute_takes_windows_drive_paths(self):
        import ntpath
        self.assertTrue(compat.is_absolute("/srv/work/p"))
        for text in ("rel/p", "", None, 3):
            self.assertFalse(compat.is_absolute(text), text)
        with mock.patch.object(compat.os, "path", ntpath):  # how Windows Python sees a hook cwd
            self.assertTrue(compat.is_absolute("D:\\work\\p"))
            self.assertTrue(compat.is_absolute("D:/work/p"))
            self.assertFalse(compat.is_absolute("work\\p"))

    def test_overlay_reads_tilde_from_the_given_HOME(self):
        spec = importlib.util.spec_from_file_location("router_overlay_under_test", HOOKS / "overlay.py")
        overlay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(overlay)
        env = {"HOME": str(self.tmp / "h"), "ROUTER_LOCAL": "~/local.json"}
        self.assertEqual(overlay.overlay_path(env), self.tmp / "h/local.json")

    def test_simulation_is_active_when_requested(self):
        if str(WIN_SIM) not in [str(Path(p).resolve()) for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]:
            self.skipTest("plain run; WinSim covers the simulation in child processes")
        if os.name == "nt":
            self.skipTest("real Windows")
        self.assertTrue(SIMULATED)
        for name in BANNED_MODULES:
            with self.assertRaises(ImportError):
                __import__(name)
        self.assertFalse([name for name in BANNED_OS if hasattr(os, name)])
        self.assertEqual(compat.NOFOLLOW, 0)


# The shared commands installer is standalone (vendored byte-for-byte into Harness, so it cannot
# import Router compat); it guards its own platform calls and has native Windows/macOS tests.
SHARED_INSTALL_HELPER = "scripts/commands_install.py"


def shipped_python():
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        from test_py39_compat import shipped_sources
    finally:
        sys.path.remove(str(ROOT / "tests"))
    return [(label, text) for label, text in shipped_sources()
            if not label.replace("\\", "/").endswith(("hooks/router/compat.py", SHARED_INSTALL_HELPER))]


def banned_uses(text):
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.split(".")[0] in BANNED_MODULES]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in BANNED_MODULES:
            found.append(node.module)
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_OS and isinstance(node.value, ast.Name) \
                and node.value.id == "os":
            found.append(f"os.{node.attr}")
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("getattr", "hasattr") \
                and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value in BANNED_OS:
            found.append(f"getattr {node.args[1].value}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__" \
                and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in BANNED_MODULES:
            found.append(node.args[0].value)
    return found


class StaticTests(unittest.TestCase):
    def test_no_posix_only_call_outside_compat(self):
        sources = shipped_python()
        self.assertGreater(len(sources), 20)
        bad = {label: uses for label, text in sources for uses in [banned_uses(text)] if uses}
        self.assertEqual(bad, {}, "route these through hooks/router/compat.py")

    def test_scanner_catches_each_banned_form(self):
        for text in ("import fcntl", "import pwd as p", "from fcntl import flock", "os.geteuid()",
                     "x = os.O_NOFOLLOW", "getattr(os, 'O_NONBLOCK', 0)", "os.pread(1, 2, 3)", "os.fchmod(1, 2)",
                     "os.getuid()", "__import__('fcntl')"):
            self.assertTrue(banned_uses(text), text)
        self.assertEqual(banned_uses("compat.pread(1, 2, 3)\nimport os"), [])

    def test_compat_ships_and_is_linked(self):
        self.assertTrue((ROOT / "plugins/router/hooks/router/compat.py").is_file())
        self.assertEqual((ROOT / "plugins/router/hooks/router/compat.py").read_bytes(),
                         (HOOKS / "compat.py").read_bytes())
        self.assertIn('"router/compat.py": "../hooks/router/compat.py"', (ROOT / "install.sh").read_text(encoding="utf-8"))


@unittest.skipIf(os.name == "nt", "real Windows runs the suites themselves")
class WinSimTests(unittest.TestCase):
    """The hooks run in child processes with the Windows simulation on."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="router-winsim-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = sim_env(HOME=str(self.tmp / "home"), USERPROFILE="", XDG_STATE_HOME="", XDG_CONFIG_HOME="",
                           ROUTER_STATE=str(self.tmp / "state"), ROUTER_LOCAL="off", ROUTER_OFF="",
                           CLAUDE_HOME=str(self.tmp / "home/.claude"))

    def run_py(self, args, stdin=""):
        return subprocess.run([sys.executable, "-B"] + args, input=stdin, env=self.env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)

    def errors(self):
        log = self.tmp / "state/errors.jsonl"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def test_lock_owner_turnover_cannot_reap_replacement(self):
        # Stop the old reaper after reading A's PID. A exits and B acquires
        # before the PID check resumes: the old implementation unlinks B.
        code = r'''
import errno, msvcrt, os, select, sitecustomize, subprocess, sys
from unittest import mock
owner = """
import msvcrt, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print('held', flush=True)
sys.stdin.readline()
msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
os.close(fd)
"""
children = []
def acquire():
    proc = subprocess.Popen([sys.executable, '-B', '-c', owner, 'lock'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    children.append(proc)
    assert select.select([proc.stdout], [], [], 10)[0], 'owner did not start'
    assert proc.stdout.readline().strip() == 'held'
    return proc

def release(proc):
    out, err = proc.communicate('release\n', timeout=10)
    assert proc.returncode == 0, err

first = acquire()
replacement = []
def rotate():
    release(first)
    replacement.append(acquire())

def stale_check(pid, signal):
    assert pid == first.pid and signal == 0
    rotate()
    raise ProcessLookupError(errno.ESRCH, 'old owner exited')

fd = os.open('lock', os.O_RDWR)
try:
    with mock.patch.object(sitecustomize.os, 'kill', stale_check):
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except PermissionError:
            pass
        else:
            raise AssertionError('contender entered while owner held lock')
    if not replacement:  # Kernel locks do not need a PID check.
        rotate()
    release(replacement[0])  # Must not raise FileNotFoundError on unlock.
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
finally:
    os.close(fd)
    for proc in children:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
print('ok')
'''
        result = self.run_py(["-c", code])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr[-1600:])

    def test_simulated_ranges_ownership_and_crash_release(self):
        code = r'''
import msvcrt, os, select, subprocess, sys

def deny(fd, mode, size):
    try:
        msvcrt.locking(fd, mode, size)
    except PermissionError:
        return
    raise AssertionError('conflicting lock operation succeeded')

first = os.open('lock', os.O_RDWR | os.O_CREAT, 0o600)
other = os.open('lock', os.O_RDWR)
os.lseek(first, 1, os.SEEK_SET)
msvcrt.locking(first, msvcrt.LK_NBLCK, 2)
deny(first, msvcrt.LK_NBLCK, 2)  # Same descriptor cannot acquire twice.
deny(other, msvcrt.LK_NBLCK, 2)  # Partial overlap rolls byte zero back.
msvcrt.locking(other, msvcrt.LK_NBLCK, 1)
msvcrt.locking(other, msvcrt.LK_UNLCK, 1)
os.lseek(other, 1, os.SEEK_SET)
deny(other, msvcrt.LK_UNLCK, 2)  # A different descriptor cannot unlock.
os.lseek(other, 3, os.SEEK_SET)
msvcrt.locking(other, msvcrt.LK_NBLCK, 1)  # Adjacent range is independent.
msvcrt.locking(other, msvcrt.LK_UNLCK, 1)
os.close(other)  # Must not drop the first descriptor's lock.
probe = """
import msvcrt, os
fd = os.open('lock', os.O_RDWR)
os.lseek(fd, 1, os.SEEK_SET)
try:
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
except PermissionError:
    print('busy')
else:
    raise AssertionError('unrelated close released the lock')
"""
result = subprocess.run([sys.executable, '-B', '-c', probe],
                        capture_output=True, text=True, timeout=10)
assert (result.returncode, result.stdout.strip()) == (0, 'busy'), result.stderr
msvcrt.locking(first, msvcrt.LK_UNLCK, 2)
deny(first, msvcrt.LK_UNLCK, 2)
for mode in (msvcrt.LK_LOCK, msvcrt.LK_RLCK):
    try:
        msvcrt.locking(first, mode, 1)
    except OSError:
        pass
    else:
        raise AssertionError('blocking mode allowed')
owner = """
import msvcrt, os, sys
fd = os.open('lock', os.O_RDWR)
os.lseek(fd, 1, os.SEEK_SET)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 2)
print('held', flush=True)
sys.stdin.readline()
os._exit(0)
"""
proc = subprocess.Popen([sys.executable, '-B', '-c', owner], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    assert select.select([proc.stdout], [], [], 10)[0], 'owner did not start'
    assert proc.stdout.readline().strip() == 'held'
    deny(first, msvcrt.LK_NBLCK, 2)
    proc.communicate('exit\n', timeout=10)
    assert proc.returncode == 0
    msvcrt.locking(first, msvcrt.LK_NBLCK, 2)
    msvcrt.locking(first, msvcrt.LK_UNLCK, 2)
finally:
    os.close(first)
    if proc.poll() is None:
        proc.kill()
    proc.communicate(timeout=10)
print('ok')
'''
        result = self.run_py(["-c", code])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr[-1600:])

    def test_simulator_is_inactive_on_native_windows(self):
        code = r'''
import os, shutil, sys
from unittest import mock
with open(sys.argv[1], encoding='utf-8') as stream:
    source = stream.read()
modules = {name: sys.modules.get(name) for name in ('fcntl', 'pwd', 'msvcrt')}
names = dict(vars(os))
walk = shutil._use_fd_functions
namespace = {}
with mock.patch.object(os, 'name', 'nt'):
    exec(compile(source, sys.argv[1], 'exec'), namespace)
assert namespace['ACTIVE'] is False
assert modules == {name: sys.modules.get(name) for name in modules}
assert names == dict(vars(os))
assert walk == shutil._use_fd_functions
print('ok')
'''
        result = self.run_py(["-c", code, str(WIN_SIM / "sitecustomize.py")])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr[-800:])

    def test_every_module_imports(self):
        names = sorted(path.stem for path in HOOKS.glob("*.py"))
        code = ("import sys, importlib; sys.path.insert(0, sys.argv[1]); import msvcrt; assert msvcrt.WIN_SIM\n"
                "for name in sys.argv[2:]: importlib.import_module(name)\nprint('ok')")
        result = self.run_py(["-c", code, str(HOOKS)] + names)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr[-800:])

    def test_ledger_spool_and_replay(self):
        code = ("import sys, contextlib; sys.path.insert(0, sys.argv[1]); import ledger, common\n"
                "db = common.state_path() / 'ledger.sqlite3'\n"
                "with contextlib.closing(ledger.connect(db)): pass\n"
                "for i in range(3): ledger._spool(db, {'op': 'verdict', 'key': f'k{i}', 'verdict': 'PASS', 'ts': i})\n"
                "assert db.with_name('ledger.spool').read_bytes().count(b'\\n') == 3\n"
                "with contextlib.closing(ledger.connect(db, hook=True)) as conn:\n"
                "    print(conn.execute('SELECT count(*) FROM verdicts').fetchone()[0])\n"
                "print(sorted(p.name for p in db.parent.glob('ledger.spool*')))")
        result = self.run_py(["-c", code, str(HOOKS)])
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertEqual(result.stdout.split("\n")[:2], ["3", "['ledger.spool.lock']"], result.stdout)
        self.assertEqual(self.errors(), "")

    def test_hooks_run_without_posix_calls(self):
        project = self.tmp / "project"
        (project / ".git").mkdir(parents=True)
        events = [("context_guard.py", {"hook_event_name": "PreToolUse", "tool_name": "Read", "session_id": "s1",
                                         "cwd": str(project), "tool_input": {"file_path": str(project / "a.py")}}),
                  ("prompt_hint.py", {"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": str(project),
                                      "prompt": "review the whole repository for bugs and write a report"})]
        for _ in range(2):
            for script, event in events:
                result = self.run_py([str(HOOKS / script)], json.dumps(event))
                self.assertEqual(result.returncode, 0, f"{script}: {result.stderr[-800:]}")
                for word in ("fcntl", "pwd", "AttributeError", "ImportError", "cannot load"):
                    self.assertNotIn(word, result.stderr, script)
        self.assertTrue(list((self.tmp / "state").rglob("s1.lock")), "context_guard took its chain lock")
        for word in ("AttributeError", "ImportError", "LockTimeout"):
            self.assertNotIn(word, self.errors())


if __name__ == "__main__":
    unittest.main()
