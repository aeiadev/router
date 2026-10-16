"""Explicit, local receipt recording commands. No hook writes or acceptance decisions."""
import argparse
import json
import os
import stat
import sys

import compat

HELP = "receipt create TASK_ID --file PATH | record TASK_ID --file PATH | show TASK_ID: local recording only"
MAX_INPUT_BYTES = 64 * 1024


def _read_object(path):
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        else:
            descriptor = compat.open_fd(path)
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ValueError("input must be a regular file")
                with os.fdopen(descriptor, "rb") as source:
                    descriptor = -1
                    raw = source.read(MAX_INPUT_BYTES + 1)
            finally:
                if descriptor != -1:
                    os.close(descriptor)
    except OSError as exc:
        raise ValueError("cannot read input file") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 64 KiB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("input must be a UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("input must be a JSON object")
    return value


def main(argv):
    parser = argparse.ArgumentParser(prog="router receipt", description="Explicit local receipt recording")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "record", "show"):
        command = commands.add_parser(name)
        command.add_argument("task_id", metavar="TASK_ID")
        if name != "show":
            command.add_argument("--file", required=True, metavar="PATH", help="local JSON object, or - for stdin")
    args = parser.parse_args(argv)
    try:
        if args.command != "show":
            payload = _read_object(args.file)
    except ValueError as exc:
        print(f"router receipt: {exc}", file=sys.stderr)
        return 1
    try:
        import receipts
        if args.command == "create":
            result = receipts.create_task(args.task_id, payload)
        elif args.command == "record":
            result = receipts.record_event(args.task_id, payload)
        else:
            result = receipts.get_task(args.task_id)
            if result is None:
                print("router receipt: task not found", file=sys.stderr)
                return 1
    except ImportError:
        print("router receipt: receipt store unavailable", file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("router receipt: request rejected by receipt store", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
