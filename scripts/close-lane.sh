#!/usr/bin/env bash
# Usage: close-lane.sh <worktree> <brief-file>
# A lane is one task's worktree run.
# CLOSE_LANE_BASE: base ref (default: main, master, then HEAD).
# CLOSE_LANE_BAR_TIMEOUT: maximum BAR seconds (default: 900).
set -euo pipefail
if [[ $# -eq 1 && ( $1 == --help || $1 == -h ) ]]; then
  echo 'Usage: close-lane.sh <worktree> <brief-file>'
  echo "A lane is one task's worktree run."
  echo 'Check FILES globs before and after running BAR. Exit 0 on PASS, 1 on FAIL.'
  echo 'CLOSE_LANE_BASE: base ref; defaults to local main, master, then HEAD.'
  echo 'CLOSE_LANE_BAR_TIMEOUT: deadline in seconds; defaults to 900.'
  exit 0
fi
if [[ $# -ne 2 ]]; then
  echo 'Usage: close-lane.sh <worktree> <brief-file>' >&2
  exit 2
fi
exec python3 - "$@" <<'PY'
import fnmatch
import math
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys


def fail(message):
    print(f"FAIL {message}", flush=True)
    sys.exit(1)


def git(*arguments):
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(worktree),
                             *arguments], capture_output=True, stdin=subprocess.DEVNULL,
                            timeout=30)
    if result.returncode:
        raise ValueError("git " + arguments[0] + " failed: " +
                         result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def allowed(path):
    # '*' matches within a path segment; '**' matches zero or more segments.
    def match(parts, glob):
        if not glob:
            return not parts
        if glob[0] == "**":
            return match(parts, glob[1:]) or bool(parts and match(parts[1:], glob))
        return bool(parts and fnmatch.fnmatchcase(parts[0], glob[0])
                    and match(parts[1:], glob[1:]))
    return any(match(path.split("/"), pattern.split("/")) for pattern in patterns)


def check_scope():
    paths = set(git("diff", "--name-only", "--no-renames", "-z", base, "HEAD").split(b"\0"))
    # Separate index and working-tree diffs keep both sides of staged renames.
    for arguments in (("diff", "--cached", "--name-only", "--no-renames", "-z"),
                      ("diff", "--name-only", "--no-renames", "-z"),
                      ("ls-files", "--others", "--exclude-standard", "-z")):
        paths.update(git(*arguments).split(b"\0"))
    names = sorted(os.fsdecode(path) for path in paths if path)
    outside = [name for name in names if not allowed(name)]
    if outside:
        raise ValueError("outside FILES: " + ", ".join(repr(name) for name in outside))
    return len(names)


try:
    worktree = Path(sys.argv[1]).resolve(strict=True)
    text = Path(sys.argv[2]).read_text(encoding="utf-8")
    fields = {}
    required = {"TASK", "FILES", "BAR", "RETURN"}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"(TASK|FILES|BAR|RETURN):?\s+(.+)", line)
        if not match:
            # Notes may follow the complete brief, but cannot hide empty fields.
            if set(fields) != required or re.match(r"^(TASK|FILES|BAR|RETURN)(?::|\s|$)", line):
                raise ValueError("brief needs one TASK, FILES, BAR and RETURN line")
            continue
        if match[1] in fields:
            raise ValueError("brief needs one TASK, FILES, BAR and RETURN line")
        fields[match[1]] = match[2].strip()
    if set(fields) != required:
        raise ValueError("brief needs one TASK, FILES, BAR and RETURN line")
    patterns = shlex.split(fields["FILES"])
    if not patterns:
        raise ValueError("FILES has no paths or globs")
    for index, pattern in enumerate(patterns):
        pattern = pattern.removeprefix("./")
        if not pattern or pattern.startswith("/") or ".." in pattern.split("/"):
            raise ValueError("FILES paths must be relative and stay inside the worktree")
        patterns[index] = pattern + "**" if pattern.endswith("/") else pattern
    root = Path(os.fsdecode(git("rev-parse", "--show-toplevel")).strip()).resolve()
    if root != worktree:
        raise ValueError("worktree must name the Git root")
    configured = os.environ.get("CLOSE_LANE_BASE")
    if configured:
        reference = configured
    else:
        references = os.fsdecode(git("for-each-ref", "--format=%(refname:short)", "refs/heads")).splitlines()
        reference = next((ref for ref in ("main", "master") if ref in references), "HEAD")
    base = os.fsdecode(git("merge-base", reference, "HEAD")).strip()
    seconds = float(os.environ.get("CLOSE_LANE_BAR_TIMEOUT", "900"))
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("CLOSE_LANE_BAR_TIMEOUT must be a positive number")
    check_scope()
    process = subprocess.Popen(["bash", "-euo", "pipefail", "-c", fields["BAR"]],
                               cwd=worktree, stdin=subprocess.DEVNULL,
                               stdout=sys.stderr, stderr=sys.stderr, start_new_session=True)
    try:
        code = process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        fail(f"BAR timed out after {seconds:g}s")
    if code:
        fail(f"BAR exited {code}")
    count = check_scope()
except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as exc:
    fail(str(exc))
print(f"PASS {count} changed path(s) inside FILES; BAR exited 0")
PY
