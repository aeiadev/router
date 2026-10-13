"""Explicit local usage declaration, observation and read-only reporting."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

import common

HELP = "usage declare TASK_ID --file PATH | record TASK_ID --file PATH | report [TASK_ID]: opt-in usage"
MAX_INPUT_BYTES = 64 * 1024


def _read_object(path):
    if path == "-":
        source = sys.stdin.buffer
        raw = source.read(MAX_INPUT_BYTES + 1)
    else:
        # O_NONBLOCK avoids opening a FIFO indefinitely; fstat also rejects devices and directories.
        descriptor = common.compat.open_fd(path)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("input must be a regular file")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                raw = source.read(MAX_INPUT_BYTES + 1)
        finally:
            if descriptor != -1:
                os.close(descriptor)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 64 KiB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("input must be a UTF-8 JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def main(argv):
    parser = argparse.ArgumentParser(prog="router usage", description=HELP)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("declare", "record", "report"):
        command = commands.add_parser(name)
        command.add_argument("task_id", nargs="?" if name == "report" else None, metavar="TASK_ID")
        if name != "report":
            command.add_argument("--file", required=True, metavar="PATH", help="JSON object or - for stdin")
    args = parser.parse_args(argv)
    try:
        import usage
        state_dir = Path(common.state_path()) / "usage"
        if args.command == "report":
            result = usage.get_report(args.task_id, state_dir=state_dir)
        else:
            value = _read_object(args.file)
            if args.command == "declare":
                result = usage.declare_task(args.task_id, value, state_dir=state_dir)
            else:
                if set(value) != {"adapter", "context", "payload"}:
                    raise ValueError("record input requires adapter, context and payload")
                event = usage.normalize_event(value["adapter"], value["payload"], value["context"])
                result = usage.record_event(args.task_id, event, state_dir=state_dir)
    except ImportError:
        print("router usage: usage store unavailable", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError):
        print("router usage: input or usage store rejected request", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
