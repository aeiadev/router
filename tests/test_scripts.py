#!/usr/bin/env python3
"""Standalone helper checks with temporary state and local Git repositories."""
import ast
import contextlib
import ctypes
import io
import re
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _platform import BASH, posix, run  # noqa: E402
SCRIPTS = ROOT / "scripts"


class ScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-scripts-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.env = os.environ.copy()
        self.env.update(HOME=str(self.tmp), ROUTER_STATE=str(self.tmp / "state"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        XDG_CONFIG_HOME=str(self.tmp / "config"), ROUTER_LOCAL="off")
        self.assertTrue(all(name in self.env for name in ("HOME", "XDG_CONFIG_HOME", "ROUTER_LOCAL")))
        for key in tuple(self.env):
            if key.startswith("CLOSE_LANE_"):
                self.env.pop(key)

    def run_script(self, name, *arguments, env=None):
        command = [BASH, posix(SCRIPTS / name)] if name.endswith(".sh") else [sys.executable, str(SCRIPTS / name)]
        # run() kills the whole tree at the deadline: a stuck grandchild cannot hold the pipes open.
        return run([*command, *map(str, arguments)], timeout=10, env=env or self.env, cwd=ROOT,
                   stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def assert_code(self, result, code):
        self.assertEqual(result.returncode, code,
                         f"expected exit {code}; stdout={result.stdout!r}; stderr={result.stderr!r}")

    def test_helpers_offer_help(self):
        for name in ("wait-until.sh", "close-lane.sh", "cite-check.py"):
            with self.subTest(script=name):
                self.assert_code(self.run_script(name, "--help"), 0)

    def test_wait_timeout_is_124_and_respects_deadline(self):
        start = time.monotonic()
        result = self.run_script("wait-until.sh", "0.15", "exists", self.tmp / "missing")
        self.assert_code(result, 124)
        self.assertIn("WAIT_TIMEOUT", result.stderr)
        self.assertLess(time.monotonic() - start, 2, "short wait overshot its hard deadline")

    def test_wait_existing_nonempty_file(self):
        target = self.tmp / "ready"
        target.write_text("ready\n")
        self.assert_code(self.run_script("wait-until.sh", "1", "exists", target), 0)

    def test_wait_line_does_not_accept_stale_content(self):
        target = self.tmp / "output"
        target.write_text("ready\n")
        self.assert_code(self.run_script("wait-until.sh", "0.1", "line", target, "ready"), 124)

    def test_wait_dead_writer_and_bad_arguments(self):
        with subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL) as process:
            process.wait(timeout=5)
        self.assert_code(self.run_script("wait-until.sh", "1", "exists", self.tmp / "missing",
                                         "--alive", process.pid), 3)
        self.assert_code(self.run_script("wait-until.sh", "1", "pid", "0"), 2)
        self.assert_code(self.run_script("wait-until.sh", "1", "no-proc", "["), 2)

    def test_wait_pid_and_no_matching_process(self):
        with subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL) as process:
            process.wait(timeout=5)
        self.assert_code(self.run_script("wait-until.sh", "1", "pid", process.pid), 0)
        self.assert_code(self.run_script("wait-until.sh", "1", "no-proc", "router_missing_process_marker"), 0)

    def test_wait_follows_a_background_job_of_the_shell(self):
        # Git Bash's $! is an MSYS pid; Python on Windows knows the process by another (Windows) pid.
        script = ('sleep 30 >/dev/null 2>&1 </dev/null & job=$!\n'
                  '"$BASH" "$1" 0.5 pid "$job"; echo "pid rc=$?"\n'
                  '"$BASH" "$1" 0.5 exists "$2" --alive "$job"; echo "alive rc=$?"\n'
                  'kill -0 "$job" && echo "sleeper alive"\n'
                  'kill "$job"\n')
        result = run([BASH, "-c", script, "job", posix(SCRIPTS / "wait-until.sh"), posix(self.tmp / "missing")],
                     timeout=30, env=self.env, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(result.stdout.split("\n")[:3], ["pid rc=124", "alive rc=124", "sleeper alive"],
                         f"stderr={result.stderr!r}")

    def test_wait_leaves_the_watched_process_running(self):
        # Windows os.kill(pid, 0) sends CTRL_C_EVENT to the process group pid: a probe must not stop what it
        # watches. The child leads its own group with Ctrl+C on, so such a probe would end it.
        marker = f"router_wait_marker_{os.getpid()}"
        ready = self.tmp / "child-ready"
        code, flags = ("import pathlib, sys; "
                       "pathlib.Path(sys.argv[1]).write_text('ready'); sys.stdin.buffer.read(1)"), 0
        if os.name == "nt":
            code = ("import ctypes, pathlib, sys; "
                    "ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False); "
                    "pathlib.Path(sys.argv[1]).write_text('ready'); sys.stdin.buffer.read(1)")
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
        child = subprocess.Popen([sys.executable, "-c", code, str(ready), marker],
                                 stdin=subprocess.PIPE, creationflags=flags)
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), f"child did not signal readiness; exit={child.poll()}")
        # no-proc matches the command line, not just the program name (python.exe on Windows).
        for arguments in (("pid", child.pid), ("exists", self.tmp / "missing", "--alive", child.pid),
                          ("no-proc", marker)):
            with self.subTest(mode=arguments[0]):
                result = self.run_script("wait-until.sh", "0.5", *arguments)
                self.assertIsNone(child.poll(), "the watched child exited before the wait result was checked")
                self.assert_code(result, 124)

    def test_wait_windows_snapshot_failure_is_not_absence(self):
        # Exercise the actual embedded scanner with a failed first read and a partial read.
        source = (SCRIPTS / "wait-until.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        scanner = next(node for node in ast.parse(source).body
                       if isinstance(node, ast.If) and
                       any(isinstance(child, ast.FunctionDef) and child.name == "windows_processes"
                           for child in node.body))
        function = next(child for child in scanner.body
                        if isinstance(child, ast.FunctionDef) and child.name == "windows_processes")
        module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
        for first, error in ((False, 5), (True, 5)):
            with self.subTest(first=first):
                closed = []
                entry = SimpleNamespace(size=0, pid=42, ppid=1, exe="python.exe")
                api = SimpleNamespace(CreateToolhelp32Snapshot=lambda *_: 7,
                                      Process32FirstW=lambda *_: first,
                                      Process32NextW=lambda *_: False,
                                      CloseHandle=closed.append)
                fake_ctypes = SimpleNamespace(sizeof=lambda *_: 1, create_string_buffer=lambda *_: None,
                                               byref=lambda value: value, set_last_error=lambda *_: None,
                                               get_last_error=lambda: error,
                                               WinError=lambda value: OSError(value, "snapshot read failed"))
                namespace = dict(kernel32=api, ProcessEntry=lambda: entry, ctypes=fake_ctypes,
                                 wintypes=SimpleNamespace(HANDLE=lambda value: SimpleNamespace(value=value)),
                                 command_line=lambda *_: "python marker")
                exec(compile(module, str(SCRIPTS / "wait-until.sh"), "exec"), namespace)
                with self.assertRaises(OSError):
                    namespace["windows_processes"]()
                self.assertEqual(closed, [7], "snapshot handle leaked after enumeration failure")

    def wait_namespace(self):
        source = (SCRIPTS / "wait-until.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        tree = ast.parse(source)
        block = next(node for node in tree.body if isinstance(node, ast.If)
                     and isinstance(node.test, ast.Name) and node.test.id == "WINDOWS")
        functions = [node for node in block.body + tree.body if isinstance(node, ast.FunctionDef)]
        namespace = dict(WINDOWS=True, os=os, time=time, sys=sys, subprocess=subprocess,
                         args=SimpleNamespace(kind="no-proc", alive=None, seconds=0.01),
                         pattern=re.compile("closure_marker"), deadline=0)
        exec(compile(ast.Module(body=functions, type_ignores=[]), "wait-until.sh", "exec"), namespace)
        loop = next(node for node in tree.body if isinstance(node, ast.Try)
                    and any(isinstance(child, ast.While) for child in node.body))
        namespace["_loop"] = compile(ast.Module(body=[loop] + tree.body[-2:], type_ignores=[]),
                                     "wait-until.sh", "exec")
        return namespace

    def wait_result(self, namespace):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as result:
            exec(namespace["_loop"], namespace)
        return result.exception.code, errors.getvalue()

    def test_wait_windows_reused_parent_is_not_ancestor(self):
        # A surviving ancestor retains its dead parent's PID. That PID can now name our target.
        namespace = self.wait_namespace()
        me, parent, target = os.getpid(), 10000001, 10000002
        rows = [(me, parent, "scanner"), (parent, target, "shell"),
                (target, 0, "python closure_marker")]
        namespace.update(windows_processes=lambda: rows,
                         process_started=lambda pid: {me: 30, parent: 10, target: 20}[pid])
        self.assertEqual(self.wait_result(namespace)[0], 124, "target was excluded as a fake ancestor")
        # The ancestor can exit and its PID be reused between edges. Keep its proven identity.
        parent_times = iter((10, 40))
        namespace["process_started"] = lambda pid: next(parent_times) if pid == parent else {me: 30, target: 20}[pid]
        self.assertEqual(self.wait_result(namespace)[0], 124, "ancestor identity changed during the walk")
        # A genuinely older ancestor remains excluded; unverifiable identity must not exclude it.
        namespace["process_started"] = lambda pid: {me: 30, parent: 20, target: 10}[pid]
        self.assertEqual(self.wait_result(namespace)[0], 0)
        namespace["process_started"] = lambda pid: None
        self.assertEqual(self.wait_result(namespace)[0], 124)

    def test_wait_windows_query_failure_is_not_absence(self):
        # Run the actual command-line reader and wait loop, with a controlled live process handle.
        for status, empty in ((0xC0000001, False), (0x8000000D, False), (0, True)):
            with self.subTest(status=status, empty=empty):
                namespace = self.wait_namespace()
                closed = []
                api = SimpleNamespace(OpenProcess=lambda *_: 7, CloseHandle=closed.append,
                                      GetExitCodeProcess=lambda handle, code: setattr(code._obj, "value", 259) or 1)
                namespace.update(kernel32=api, ntdll=SimpleNamespace(NtQueryInformationProcess=lambda *_: status),
                                 QUERY=0x1000, STILL_ACTIVE=259, ctypes=ctypes,
                                 wintypes=SimpleNamespace(ULONG=ctypes.c_uint32, DWORD=ctypes.c_uint32),
                                 UnicodeString=SimpleNamespace(from_buffer=lambda _: SimpleNamespace(buffer=None)),
                                 same_user=lambda _: True, process_started=lambda _: None)
                namespace["windows_processes"] = lambda: [
                    (10000002, 0, namespace["command_line"](10000002, ctypes.create_string_buffer(256))
                     or "python.exe")]
                code, error = self.wait_result(namespace)
                self.assertEqual(code, 2, "failed query silently lost the marker and returned success")
                self.assertIn("10000002", error)
                self.assertEqual(closed, [7])
                # The same unavailable query for another account retains executable-only matching.
                namespace["same_user"] = lambda _: False
                self.assertEqual(self.wait_result(namespace)[0], 0)
                namespace["pattern"] = re.compile("python")
                self.assertEqual(self.wait_result(namespace)[0], 124, "executable matching was lost")
                namespace["pattern"] = re.compile("closure_marker")
                namespace["same_user"] = lambda _: True
                api.GetExitCodeProcess = lambda handle, code: setattr(code._obj, "value", 0) or 1
                self.assertEqual(self.wait_result(namespace)[0], 0, "an exited process blocked absence")

    def test_wait_windows_open_errors(self):
        for pid, error, expected in ((0, 87, 0), (4, 5, 0), (10000002, 87, 0), (10000002, 8, 2)):
            with self.subTest(pid=pid, error=error):
                namespace = self.wait_namespace()
                namespace.update(kernel32=SimpleNamespace(OpenProcess=lambda *_: None), QUERY=0x1000,
                                 ctypes=SimpleNamespace(get_last_error=lambda: error))
                namespace["windows_processes"] = lambda: [(pid, 0, namespace["command_line"](pid, None)
                                                           or "system.exe")]
                result, diagnostic = self.wait_result(namespace)
                self.assertEqual(result, expected, diagnostic)

    @unittest.skipUnless(os.name == "nt", "native Windows API fault injection")
    def test_wait_windows_live_child_faults(self):
        ready = self.tmp / "native-ready"
        child = subprocess.Popen([sys.executable, "-c",
                                  "import pathlib,sys; pathlib.Path(sys.argv[1]).touch(); sys.stdin.buffer.read(1)",
                                  str(ready), "closure_marker"], stdin=subprocess.PIPE)
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.wait, 10)
        self.addCleanup(child.kill)
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        source = (SCRIPTS / "wait-until.sh").read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
        block = next(node for node in ast.parse(source).body if isinstance(node, ast.If)
                     and isinstance(node.test, ast.Name) and node.test.id == "WINDOWS")
        namespace = self.wait_namespace()
        exec(compile(ast.Module(body=[block], type_ignores=[]), "wait-until.sh", "exec"), namespace)
        kernel, ntdll = namespace["kernel32"], namespace["ntdll"]
        kernel.GetProcessId.argtypes = (namespace["wintypes"].HANDLE,)
        kernel.GetProcessId.restype = namespace["wintypes"].DWORD
        # Preserve native enumeration, handles and timestamps. Inject only a stale parent link.
        class KernelProxy:
            def __getattr__(self, name):
                return getattr(kernel, name)

            def Process32NextW(self, snapshot, entry):
                result = kernel.Process32NextW(snapshot, entry)
                if result and entry._obj.pid == os.getpid():
                    entry._obj.ppid = child.pid
                return result

        namespace["kernel32"] = KernelProxy()
        with self.subTest(fault="reused parent PID"):
            result = self.wait_result(namespace)
            self.assertIsNone(child.poll())
            self.assertEqual(result[0], 124, result[1])
        namespace["kernel32"] = kernel
        original = ntdll.NtQueryInformationProcess
        def fail_child(handle, kind, *rest):
            return -1073741823 if kind == 60 and kernel.GetProcessId(handle) == child.pid else original(handle, kind, *rest)
        namespace["ntdll"] = SimpleNamespace(NtQueryInformationProcess=fail_child)
        with self.subTest(fault="live child query failed"):
            result = self.wait_result(namespace)
            self.assertIsNone(child.poll())
            self.assertEqual(result[0], 2, result[1])
            self.assertIn(str(child.pid), result[1])
        namespace["ntdll"] = ntdll
        with self.subTest(fault="none"):
            result = self.wait_result(namespace)
            self.assertIsNone(child.poll())
            self.assertEqual(result[0], 124, result[1])

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo),
                                 *arguments], env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        self.assert_code(result, 0)
        return result.stdout.strip()

    def make_repo(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test.invalid")
        (self.repo / "allowed.txt").write_text("baseline\n")
        (self.repo / "outside.txt").write_text("baseline\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial files")
        self.brief = self.tmp / "brief.txt"
        self.write_brief()

    def write_brief(self, files="allowed.txt", bar="test -s allowed.txt"):
        self.brief.write_text(f"TASK Update allowed files\nFILES {files}\nBAR {bar}\n"
                              "RETURN CHANGED / BAR / OUTPUT / NOT DONE / OPEN\n")

    def close(self):
        return self.run_script("close-lane.sh", self.repo, self.brief)

    def test_close_lane_passes_allowed_unstaged_staged_and_untracked(self):
        self.make_repo()
        (self.repo / "allowed.txt").write_text("changed\n")
        self.assert_code(self.close(), 0)
        self.git("add", "allowed.txt")
        self.assert_code(self.close(), 0)
        (self.repo / "new file.txt").write_text("new\n")
        self.write_brief(files='allowed.txt "new file.txt"')
        result = self.close()
        self.assert_code(result, 0)
        self.assertTrue(result.stdout.startswith("PASS "), result.stdout)

    def test_close_lane_runs_bar_in_its_own_bash(self):
        # Native Windows Python resolves a bare "bash" to System32's WSL bash, which hangs: the BAR runs
        # in the bash that runs close-lane.sh, whatever "bash" PATH names first.
        self.make_repo()
        decoy = self.tmp / "decoy"
        decoy.mkdir()
        (decoy / "bash").write_bytes(b"#!/bin/sh\nexit 97\n")
        os.chmod(decoy / "bash", 0o755)
        env = dict(self.env, PATH=str(decoy) + os.pathsep + self.env.get("PATH", ""))
        result = self.run_script("close-lane.sh", self.repo, self.brief, env=env)
        self.assert_code(result, 0)
        self.assertTrue(result.stdout.startswith("PASS "), result.stdout)

    def test_close_lane_blocks_scope_before_running_bar(self):
        self.make_repo()
        (self.repo / "outside.txt").write_text("changed\n")
        self.write_brief(bar="touch should-not-run.txt")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("outside FILES", result.stdout)
        self.assertFalse((self.repo / "should-not-run.txt").exists(), "BAR ran despite scope violation")

    def test_close_lane_blocks_untracked_and_staged_renames(self):
        self.make_repo()
        (self.repo / "unexpected.txt").write_text("new\n")
        self.assert_code(self.close(), 1)
        (self.repo / "unexpected.txt").unlink()
        self.git("mv", "outside.txt", "renamed.txt")
        self.write_brief(files="renamed.txt")
        self.assert_code(self.close(), 1)

    def test_close_lane_checks_commits_from_base(self):
        self.make_repo()
        self.git("checkout", "-qb", "lane")
        (self.repo / "outside.txt").write_text("committed outside scope\n")
        self.git("add", "outside.txt")
        self.git("commit", "-qm", "Outside change")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("outside.txt", result.stdout)

    def test_close_lane_checks_bar_changes_and_nonzero_exit(self):
        self.make_repo()
        self.write_brief(bar="touch unexpected.txt")
        self.assert_code(self.close(), 1)
        (self.repo / "unexpected.txt").unlink()
        self.write_brief(bar="exit 7")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("BAR exited 7", result.stdout)

    def test_close_lane_fails_bar_timeout(self):
        self.make_repo()
        self.env["CLOSE_LANE_BAR_TIMEOUT"] = "0.1"
        self.write_brief(bar="sleep 10")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("timed out", result.stdout)

    def test_close_lane_accepts_notes_after_four_fields(self):
        self.make_repo()
        (self.repo / "allowed.txt").write_text("changed\n")
        with self.brief.open("a") as stream:
            stream.write("\nImplementation notes:\nKeep the existing behavior.\n"
                         "- Confirm the BAR output before returning.\n")
        result = self.close()
        self.assert_code(result, 0)
        self.assertTrue(result.stdout.startswith("PASS "), result.stdout)

    def test_close_lane_rejects_duplicate_and_escaping_fields(self):
        self.make_repo()
        for field in ("TASK", "FILES", "BAR", "RETURN"):
            with self.subTest(field=field):
                self.write_brief()
                with self.brief.open("a") as stream:
                    stream.write(f"\nNotes below the brief.\n{field} duplicate\n")
                result = self.close()
                self.assert_code(result, 1)
                self.assertIn("brief needs one TASK, FILES, BAR and RETURN line", result.stdout)
        self.write_brief(files="../*")
        self.assert_code(self.close(), 1)

    def test_close_lane_requires_complete_fields_before_notes(self):
        self.make_repo()
        self.brief.write_text("TASK Update allowed files\nFILES allowed.txt\n"
                              "BAR test -s allowed.txt\nNotes without RETURN.\n")
        self.assert_code(self.close(), 1)
        self.write_brief()
        self.brief.write_text(self.brief.read_text().replace("BAR ", "Notes before BAR.\nBAR "))
        self.assert_code(self.close(), 1)

    def test_close_lane_rejects_empty_extra_fields_after_notes(self):
        self.make_repo()
        for field in ("TASK", "FILES", "BAR", "RETURN"):
            with self.subTest(field=field):
                self.write_brief()
                with self.brief.open("a") as stream:
                    stream.write(f"\nNotes below the brief.\n{field}:\n")
                self.assert_code(self.close(), 1)

    def test_close_lane_globs_preserve_directory_boundaries(self):
        self.make_repo()
        nested = self.repo / "docs/sub"
        nested.mkdir(parents=True)
        (nested / "file.md").write_text("text\n")
        self.write_brief(files="docs/*")
        self.assert_code(self.close(), 1)
        self.write_brief(files="docs/**")
        self.assert_code(self.close(), 0)
        self.write_brief(files="docs/**/*.md")
        self.assert_code(self.close(), 0)

    def test_citations_validate_ranges_without_printing_contents(self):
        source = self.tmp / "source.txt"
        source.write_text("first private value\nsecond private value\n")
        report = self.tmp / "report.md"
        report.write_text("See source.txt:1-2 and 2.\n")
        result = self.run_script("cite-check.py", report, "--root", self.tmp)
        self.assert_code(result, 0)
        self.assertIn("OK source.txt:1-2", result.stdout)
        self.assertNotIn("private value", result.stdout)
        report.write_text("source.txt:2-1 source.txt:3\n")
        self.assert_code(self.run_script("cite-check.py", report, "--root", self.tmp), 1)

    def test_citations_reject_missing_and_outside_files(self):
        report = self.tmp / "report.md"
        root = self.tmp / "root"
        root.mkdir()
        report.write_text("../report.md:1\n")
        self.assert_code(self.run_script("cite-check.py", report, "--root", root), 1)
        report.write_text("No citations here.\n")
        self.assert_code(self.run_script("cite-check.py", report, "--root", root), 1)



if __name__ == "__main__":
    unittest.main(verbosity=2)
