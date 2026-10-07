#!/usr/bin/env bash
# Wait for a condition with a hard deadline. Exit 124 means timeout.
set -euo pipefail
exec python3 - "$@" <<'PY'
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import time

parser = argparse.ArgumentParser(description="Wait for a condition; timeout exits 124.")
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


def alive(pid):
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
    result = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], check=True,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL,
                            timeout=max(0.01, deadline - time.monotonic()))
    processes = [line.strip().split(None, 2) for line in result.stdout.splitlines()]
    parents = {int(row[0]): int(row[1]) for row in processes if len(row) == 3}
    excluded, pid = set(), os.getpid()
    while pid and pid not in excluded:
        excluded.add(pid)
        pid = parents.get(pid, 0)
    return not any(int(row[0]) not in excluded and "wait-until.sh" not in row[2]
                   and pattern.search(row[2]) for row in processes if len(row) == 3)


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
