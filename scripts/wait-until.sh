#!/usr/bin/env bash
# Wait for a condition with a hard deadline. Exit 124 means timeout.
set -euo pipefail
# pyrun.sh (beside the hooks in a checkout or an install) finds Python 3.9+, never the Store stub.
here=$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for pyrun in "$here/../hooks/router/pyrun.sh" "$here/../../hooks/router/pyrun.sh" ""; do
  [[ -z $pyrun || -f $pyrun ]] && break
done
[[ -n $pyrun ]] || { echo 'wait-until: pyrun.sh not found; reinstall Router' >&2; exit 1; }
# Git Bash: $! names an MSYS pid, while Python on Windows needs the Windows pid in /proc/<pid>/winpid.
# A pid with no such entry is taken as a Windows pid.
if [[ -n ${MSYSTEM:-} || -e /proc/$$/winpid ]]; then
  winpid() {
    local w=""
    if [[ $1 =~ ^[0-9]+$ && -r /proc/$1/winpid ]]; then read -r w < "/proc/$1/winpid" || true; fi
    if [[ $w =~ ^[0-9]+$ ]]; then printf '%s' "$w"; else printf '%s' "$1"; fi
  }
  args=() n=0 kind="" pid_next=""
  for arg in "$@"; do
    if [[ -n $pid_next ]]; then
      pid_next="" arg=$(winpid "$arg")
    else
      case $arg in
        --a*=*) arg="${arg%%=*}=$(winpid "${arg#*=}")" ;;
        --a*) pid_next=1 ;;
        -*) ;;
        *) n=$((n + 1))
           if ((n == 2)); then kind=$arg; elif ((n > 2)) && [[ $kind == pid ]]; then arg=$(winpid "$arg"); fi ;;
      esac
    fi
    args+=("$arg")
  done
  set -- ${args[@]+"${args[@]}"}
fi
exec sh "$pyrun" router warn - "$@" <<'PY'
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description="Wait for a condition; timeout exits 124.",
    epilog="Windows no-proc: matches readable command lines; inaccessible processes and unreadable "
           "processes of other accounts match by executable name only. An unreadable command line "
           "of a live process owned by this account is an error (exit 2).")
parser.add_argument("seconds", type=float)
parser.add_argument("kind", choices=("pid", "no-proc", "line", "exists"))
parser.add_argument("targets", nargs="+")
parser.add_argument("--alive", type=int, help="fail with exit 3 if this producer exits")
args = parser.parse_args()
if not 0 < args.seconds <= 86400:
    parser.error("seconds must be greater than zero and at most 86400")
if args.alive is not None and (args.alive < 1 or args.kind not in ("line", "exists")):
    parser.error("--alive requires a positive pid and line or exists mode")
if args.kind in ("exists", "no-proc") and len(args.targets) != 1:
    parser.error("this mode takes one target")
if args.kind == "line" and len(args.targets) != 2:
    parser.error("line mode takes a file and a regular expression")
if args.kind == "pid" and any(not p.isdigit() or int(p) < 1 for p in args.targets):
    parser.error("pid mode requires positive process IDs")
try:
    pattern = re.compile(args.targets[-1]) if args.kind in ("line", "no-proc") else None
except re.error as exc:
    parser.error(f"invalid regular expression: {exc}")


WINDOWS = os.name == "nt"
if WINDOWS:
    # On Windows os.kill(pid, 0) sends CTRL_C_EVENT to the process group pid: it can stop the process
    # it watches and never reports an exit. Ask for the exit code with a read-only handle instead.
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION: read only, no right to stop the process
    STILL_ACTIVE = 259

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("ppid", wintypes.DWORD), ("priority", ctypes.c_long), ("flags", wintypes.DWORD),
                    ("exe", ctypes.c_wchar * 260)]

    class UnicodeString(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("maximum", ctypes.c_ushort), ("buffer", ctypes.c_void_p)]

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi32.EqualSid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                wintypes.ULONG, ctypes.POINTER(wintypes.ULONG))

    def windows_alive(pid):
        handle = kernel32.OpenProcess(QUERY, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists; 87: no such process
        try:
            code = wintypes.DWORD()
            return not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def process_started(pid):
        handle = kernel32.OpenProcess(QUERY, False, pid)
        if not handle:
            return None  # Unverified identity is never grounds for excluding an ancestor.
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return None
        finally:
            kernel32.CloseHandle(handle)

    def same_user(handle):
        # Compare TokenUser SIDs only when a command-line read failed. Never emit token data.
        users = []
        for process in (wintypes.HANDLE(-1), handle):  # Current-process pseudo handle, then target.
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(process, 8, ctypes.byref(token)):  # TOKEN_QUERY
                if ctypes.get_last_error() == 5 and users:
                    return False  # Protected/inaccessible target.
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                user = ctypes.create_string_buffer(256)  # TOKEN_USER + SID (maximum 68 bytes).
                if not advapi32.GetTokenInformation(token, 1, user, len(user), ctypes.byref(wintypes.DWORD())):
                    raise ctypes.WinError(ctypes.get_last_error())
                users.append(user)
            finally:
                kernel32.CloseHandle(token)
        return bool(advapi32.EqualSid(*(ctypes.c_void_p.from_buffer(user) for user in users)))

    def command_line(pid, buffer):
        handle = kernel32.OpenProcess(QUERY, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error in (5, 87):  # Access denied; exited process or System Idle (PID 0).
                return None
            raise OSError(f"OpenProcess pid={pid} error={error}")
        try:
            # 60 = ProcessCommandLineInformation (Windows 8.1+): a UNICODE_STRING, then its text.
            status = ntdll.NtQueryInformationProcess(handle, 60, buffer, len(buffer), ctypes.byref(wintypes.ULONG()))
            if not status:
                text = UnicodeString.from_buffer(buffer)
                if text.buffer and text.length:
                    return ctypes.wstring_at(text.buffer, text.length // 2)
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != STILL_ACTIVE:
                return None  # Exited during the scan.
            # Some system processes have no user-mode command line, even with a query handle.
            # Preserve exe-only matching for inaccessible/other-account processes, not our live children.
            if same_user(handle):
                raise OSError(f"command line unavailable pid={pid} status=0x{status & 0xffffffff:08x}")
            return None
        finally:
            kernel32.CloseHandle(handle)

    def windows_processes():
        snapshot = kernel32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        if not snapshot or snapshot == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        rows, entry, buffer = [], ProcessEntry(), ctypes.create_string_buffer(0x10100)
        entry.size = ctypes.sizeof(entry)
        try:
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            if not more:
                raise ctypes.WinError(ctypes.get_last_error())
            while more:
                rows.append((entry.pid, entry.ppid, command_line(entry.pid, buffer) or entry.exe))
                ctypes.set_last_error(0)
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
                if not more and ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES ends enumeration.
                    raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(snapshot)
        return rows


def alive(pid):
    if WINDOWS:
        return windows_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie has finished even if its parent has not reaped it yet.
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state != "Z"
    except (OSError, IndexError):
        return True


target = Path(args.targets[0])
offset = target.stat().st_size if args.kind == "line" and target.is_file() else 0
deadline = time.monotonic() + args.seconds


def ready():
    if args.kind == "pid":
        return all(not alive(int(pid)) for pid in args.targets)
    if args.kind == "exists":
        return target.is_file() and target.stat().st_size > 0
    if args.kind == "line":
        if not target.is_file():
            return False
        with target.open("rb") as stream:
            stream.seek(offset)
            return any(pattern.search(line.decode("utf-8", "replace")) for line in stream)
    processes = windows_processes() if WINDOWS else ps_processes()
    parents = {row[0]: row[1] for row in processes}
    excluded, pid = set(), os.getpid()
    started = process_started(pid) if WINDOWS else None
    while pid and pid not in excluded:
        excluded.add(pid)
        parent = parents.get(pid, 0)
        if WINDOWS and parent:
            parent_started = process_started(parent)
            # Windows retains a dead parent's PID, which may now belong to a younger unrelated process.
            if started is None or parent_started is None or parent_started >= started:
                break
            started = parent_started
        pid = parent
    return not any(row[0] not in excluded and "wait-until.sh" not in row[2]
                   and pattern.search(row[2]) for row in processes)


def ps_processes():
    """(pid, parent pid, command line) per process. Git Bash's ps has no -o: Windows reads its own table."""
    result = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], check=True,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL,
                            timeout=max(0.01, deadline - time.monotonic()))
    rows = [line.strip().split(None, 2) for line in result.stdout.splitlines()]
    return [(int(row[0]), int(row[1]), row[2]) for row in rows if len(row) == 3]


try:
    while True:
        if ready():
            sys.exit(0)
        if args.alive is not None and not alive(args.alive):
            print("WAIT_DEAD_WRITER: producer exited before the condition held", file=sys.stderr)
            sys.exit(3)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.1, remaining))
except subprocess.TimeoutExpired:
    pass
except (OSError, subprocess.CalledProcessError) as exc:
    print(f"WAIT_ERROR: {exc}", file=sys.stderr)
    sys.exit(2)
print(f"WAIT_TIMEOUT after {args.seconds:g}s: {args.kind}", file=sys.stderr)
sys.exit(124)
PY
