#!/usr/bin/env python3
"""Run every test file, one process each, and list every failure.

Usage: python3 tests/run_all.py [--timeout SECONDS] [--tests-dir DIR]

Runs DIR/test_*.py with this Python and DIR/test_*.sh and DIR/fresh-user*.sh with bash, one after
another from the folder above DIR (default DIR: the folder holding this file), each under a
per-file timeout (default 900 seconds) that stops the file and everything it started. Prints one
line per file, the output of every file that failed, then the list of failures. Exit 0 when every
file passed, 1 when any failed or timed out, 2 when there is nothing to run.

Bash is $BASH when it names a file. Otherwise, on Windows, the Git for Windows bash.exe found from
git.exe or under Program Files; elsewhere /bin/bash. Never the bare name `bash`: Windows looks in
System32 first and would start the WSL launcher, which hangs.

Standard library only, Python 3.9 or newer. Router and Harness ship the same file.
"""
import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import shutil

PATTERNS = ("test_*.py", "test_*.sh", "fresh-user*.sh")
DEFAULT_TIMEOUT = 900


def bash_beside_git(git):
    """The Git for Windows bash.exe for this git.exe (cmd\\git.exe, bin\\git.exe or mingw64\\bin\\git.exe)."""
    try:
        path = Path(git).resolve()
    except OSError:
        return None
    for root in list(path.parents)[:3]:
        bash = root / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    return None


def find_bash(environ=None, windows=None):
    """Full path of the bash that runs .sh files, or None."""
    environ = os.environ if environ is None else environ
    windows = os.name == "nt" if windows is None else windows
    candidates = []
    if environ.get("BASH"):
        candidates.append(environ["BASH"])
    if windows:
        git = shutil.which("git", path=environ.get("PATH", ""))
        if git:
            candidates.append(bash_beside_git(git))
        for name in ("ProgramFiles", "ProgramW6432"):
            if environ.get(name):
                candidates.append(str(Path(environ[name]) / "Git" / "bin" / "bash.exe"))
        candidates.append("C:\\Program Files\\Git\\bin\\bash.exe")
    else:
        candidates += ["/bin/bash", "/usr/bin/bash"]
    system = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or "C:\\Windows"
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        if windows and is_inside(candidate, system):
            continue
        return candidate
    return None


def is_inside(path, folder):
    try:
        Path(os.path.normcase(str(Path(path).resolve()))).relative_to(os.path.normcase(str(Path(folder).resolve())))
        return True
    except (OSError, ValueError):
        return False


def stop_tree(proc):
    """Stop a timed-out file and every process it started."""
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32" / "taskkill.exe"
        subprocess.run([str(taskkill), "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    proc.wait()


def run_file(command, cwd, timeout, log):
    """Run one file; returns (exit code or None on timeout, seconds)."""
    extra = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    started = time.monotonic()
    proc = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=log,
                            stderr=subprocess.STDOUT, **extra)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_tree(proc)
        code = None
    return code, time.monotonic() - started


def say(text=""):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def show_output(name, log):
    log.seek(0)
    data = log.read()
    say("---- output of " + name + " ----")
    sys.stdout.buffer.write(data)
    if data and not data.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()
    say("---- end of " + name + " ----")


def main(argv=None):
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Run every test file and list every failure.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="seconds per file (default 900)")
    parser.add_argument("--tests-dir", default=str(here), help="folder of test files (default: this file's folder)")
    args = parser.parse_args(argv)
    tests = Path(args.tests_dir).resolve()
    root = tests.parent
    files = sorted({path for pattern in PATTERNS for path in tests.glob(pattern) if path.is_file()},
                   key=lambda path: path.name)
    if not files:
        say("run_all: no test files in " + str(tests))
        return 2
    bash = find_bash() if any(path.suffix == ".sh" for path in files) else None
    failures = []
    for path in files:
        name = path.relative_to(root).as_posix()
        if path.suffix == ".py":
            command = [sys.executable, name]
        elif bash:
            command = [bash, name]
        else:
            say("FAIL " + name + " (no bash found: set BASH to the full path of bash)")
            failures.append(name + " (no bash)")
            continue
        with tempfile.TemporaryFile() as log:
            code, seconds = run_file(command, root, args.timeout, log)
            if code == 0:
                say("PASS %s (%.1f s)" % (name, seconds))
                continue
            if code is None:
                say("TIMEOUT %s (stopped after %d s)" % (name, args.timeout))
                failures.append("%s (timed out after %d s)" % (name, args.timeout))
            else:
                say("FAIL %s (exit %d, %.1f s)" % (name, code, seconds))
                failures.append("%s (exit %d)" % (name, code))
            show_output(name, log)
    say()
    say("%d files: %d passed, %d failed" % (len(files), len(files) - len(failures), len(failures)))
    if failures:
        say("failed:")
        for failure in failures:
            say("  " + failure)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            for failure in failures:
                say("::error title=Test failed::" + failure)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
