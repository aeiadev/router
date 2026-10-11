"""Shared test helpers so the suite runs the same on Linux, macOS and native Windows (Git Bash).

BASH is the full path of the bash that runs shell scripts in tests: never the bare name, because on
Windows CreateProcess searches System32 before PATH and finds the WSL bash.exe, which hangs. Paths handed
to bash go through posix() (C:\\x becomes /c/x). python_shim() writes a small sh script named python3
instead of a symlink, because a symlinked python.exe cannot find its DLLs. run() is subprocess.run with a
timeout that kills the whole process tree, so neither GNU timeout nor coreutils are needed. Tests that
check a POSIX-only property skip elsewhere through skip_unless_posix_modes() or skip_unless_symlinks(),
and every skip prints its reason.

`python3 tests/_platform.py timeout SECONDS COMMAND...` is the same timeout for shell tests: exit 124
when the deadline passes.
"""
import functools
import ntpath
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

IS_WINDOWS = os.name == "nt"
POSIX_MODES = not IS_WINDOWS  # NTFS keeps only a read-only flag; chmod 0o700 reads back as 0o777


def _under(path, folder):
    path, folder = ntpath.normcase(ntpath.normpath(path)), ntpath.normcase(ntpath.normpath(folder))
    return path == folder or path.startswith(folder.rstrip("\\") + "\\")


def find_bash(environ=None, windows=IS_WINDOWS, isfile=os.path.isfile):
    """Full path of the bash to run; None when there is none."""
    environ = os.environ if environ is None else environ
    if not windows:
        given = environ.get("BASH", "")
        if os.path.isabs(given) and isfile(given):
            return given
        for folder in environ.get("PATH", os.defpath).split(os.pathsep):
            if folder and isfile(os.path.join(folder, "bash")):
                return os.path.join(folder, "bash")
        return "/bin/bash" if isfile("/bin/bash") else None
    system = environ.get("SystemRoot") or environ.get("SYSTEMROOT") or r"C:\Windows"
    apps = ntpath.join(environ.get("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"), "Microsoft", "WindowsApps")

    def usable(path):
        return ntpath.isabs(path) and not _under(path, system) and not _under(path, apps) and isfile(path)

    given = environ.get("BASH", "")
    if usable(given):
        return given
    # Derive from the Git for Windows install: any PATH entry inside it (cmd, bin, usr\bin, mingw64\bin).
    roots = []
    for folder in environ.get("PATH", "").split(";"):
        folder = folder.strip().rstrip("\\/")
        if not folder:
            continue
        for depth in (1, 2):
            root = folder
            for _ in range(depth):
                root = ntpath.dirname(root)
            roots.append(root)
    for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if environ.get(key):
            roots.append(ntpath.join(environ[key], "Git"))
    if environ.get("LOCALAPPDATA"):
        roots.append(ntpath.join(environ["LOCALAPPDATA"], "Programs", "Git"))
    for root in roots:
        for candidate in (ntpath.join(root, "bin", "bash.exe"), ntpath.join(root, "usr", "bin", "bash.exe")):
            if usable(candidate):
                return candidate
    return None


BASH = find_bash()
if BASH is None:
    raise RuntimeError("no bash found: set BASH to the full path of bash (Git Bash on Windows)")


def posix(path, windows=IS_WINDOWS):
    """A path as bash expects it: unchanged off Windows; C:\\x\\y becomes /c/x/y on Windows."""
    text = os.fspath(path)
    if not windows:
        return text
    text = text.replace("\\", "/")
    if text.startswith("//?/"):
        text = text[4:]
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        text = "/" + text[0].lower() + text[2:]
    return text


def shim(folder, name, target):
    """Write an executable sh script FOLDER/NAME that runs TARGET with the same arguments."""
    path = Path(folder) / name
    quoted = "'" + posix(target).replace("'", "'\\''") + "'"
    path.write_bytes(f"#!/bin/sh\nexec {quoted} \"$@\"\n".encode("utf-8"))
    os.chmod(path, 0o755)
    return path


def home_env(home):
    """HOME and USERPROFILE both naming HOME: Windows Python's Path.home(), which the 0.1 and 0.2 release
    installers use, reads USERPROFILE only."""
    return {"HOME": str(home), "USERPROFILE": str(home)}


def python_shim(folder, target=None):
    """FOLDER/python3 as a shim for TARGET (default: the interpreter running the tests), never a symlink."""
    return shim(folder, "python3", target or sys.executable)


def _skipper(reason):
    def decorate(test):
        @functools.wraps(test)
        def skipped(*args, **kwargs):
            print(f"SKIP {test.__qualname__}: {reason}", file=sys.stderr, flush=True)
            raise unittest.SkipTest(reason)
        return skipped
    return decorate


def skip_unless_posix_modes(reason, posix_modes=None):
    """Decorator: skip a test of file mode bits where the OS has none, printing why."""
    if POSIX_MODES if posix_modes is None else posix_modes:
        return lambda test: test
    return _skipper(f"no POSIX file modes on this OS ({reason})")


def posix_modes(test, reason, has_modes=None):
    """Inline form of skip_unless_posix_modes: True where the OS has POSIX file modes; elsewhere print
    SKIP with the reason and return False, so only the mode assertion is skipped, not the whole test."""
    if POSIX_MODES if has_modes is None else has_modes:
        return True
    print(f"SKIP {test.id()} mode check: no POSIX file modes on this OS ({reason})", file=sys.stderr, flush=True)
    return False


SYMLINKS = []


def can_symlink():
    if not SYMLINKS:
        with tempfile.TemporaryDirectory() as folder:
            try:
                os.symlink(folder, os.path.join(folder, "link"), target_is_directory=True)
                SYMLINKS.append(True)
            except (OSError, NotImplementedError):
                SYMLINKS.append(False)
    return SYMLINKS[0]


def skip_unless_symlinks(reason):
    """Decorator: skip a test that needs real symlinks when this user cannot create them, printing why."""
    def decorate(test):
        @functools.wraps(test)
        def wrapper(*args, **kwargs):
            if not can_symlink():
                return _skipper(f"this user cannot create symlinks ({reason})")(test)(*args, **kwargs)
            return test(*args, **kwargs)
        return wrapper
    return decorate


@functools.lru_cache(maxsize=None)
def _kernel32():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    kernel.resume = ntdll.NtResumeProcess
    return kernel


def _start(args, kwargs):
    """Popen; on Windows the child starts suspended, joins a job object, then resumes, so nothing it starts
    escapes the job (taskkill /T misses processes that Git Bash forks; a job does not)."""
    if not IS_WINDOWS:
        return subprocess.Popen(args, **kwargs)
    kernel = _kernel32()
    job = kernel.CreateJobObjectW(None, None)
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x00000004  # CREATE_SUSPENDED
    process = subprocess.Popen(args, **kwargs)
    try:
        if job and kernel.AssignProcessToJobObject(job, int(process._handle)):
            process._platform_job = job
        elif job:
            kernel.CloseHandle(job)
    finally:
        kernel.resume(int(process._handle))
    return process


def kill_tree(process):
    """Kill a process and everything it started."""
    if IS_WINDOWS:
        job = getattr(process, "_platform_job", None)
        if job:
            kernel = _kernel32()
            kernel.TerminateJobObject(job, 1)
            kernel.CloseHandle(job)
            process._platform_job = None
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    process.kill()


def run(args, timeout, input=None, capture_output=False, check=False, **kwargs):
    """subprocess.run whose timeout kills the whole process tree, then raises TimeoutExpired."""
    if IS_WINDOWS:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs.setdefault("start_new_session", True)
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    with _start(args, kwargs) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(process)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(process.args, timeout, output=stdout, stderr=stderr)
        except BaseException:
            kill_tree(process)
            raise
        job = getattr(process, "_platform_job", None)
        if job:
            _kernel32().CloseHandle(job)
    result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def main(argv):
    if len(argv) < 3 or argv[0] != "timeout":
        print("usage: _platform.py timeout SECONDS COMMAND...", file=sys.stderr)
        return 2
    try:
        return run(argv[2:], timeout=float(argv[1])).returncode
    except subprocess.TimeoutExpired:
        return 124
    except OSError as error:
        print(f"_platform.py timeout: {error}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
