#!/usr/bin/env python3
"""Record dispatch returns and enforce the two-Sonnet, one-Opus round cap."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys


def runs_path():
    if configured := os.environ.get("ROUTER_STATE"):
        state = Path(configured).expanduser()
    else:
        state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state").expanduser()
        state /= "claude-router"
    return state / "runs.jsonl"


def records(stream):
    stream.seek(0)
    rows = []
    for number, line in enumerate(stream, 1):
        try:
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("schema") != 1:
                raise ValueError("unknown record schema")
            rows.append(row)
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"runs.jsonl line {number} is invalid: {exc}") from exc
    return rows


def rounds(rows, family):
    return sum(1 for row in rows if row.get("family") == family and row.get("round") is not None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="record one return")
    add.add_argument("--family", required=True, help="stable task label shared by all retries")
    add.add_argument("--seat", required=True)
    add.add_argument("--model", choices=("haiku", "sonnet", "opus"), required=True)
    add.add_argument("--verdict", choices=("PASS", "SEND_BACK", "BLOCKED"), required=True)
    add.add_argument("--brief", default="", help="optional brief label")
    add.add_argument("--escalated", action="store_true", help="third exec round on Opus")
    add.add_argument("--no-edits", action="store_true", help="blocked attempt; does not count as a round")
    add.add_argument("--notes", default="")
    count = commands.add_parser("rounds", help="count exec rounds for a task")
    count.add_argument("family")
    commands.add_parser("list", help="print recorded JSON lines")
    args = parser.parse_args()
    family = getattr(args, "family", "").strip().casefold()
    if args.command != "list" and not family:
        parser.error("family must not be empty")
    if args.command == "add" and args.no_edits and args.verdict != "BLOCKED":
        parser.error("--no-edits requires --verdict BLOCKED")
    path = runs_path()
    try:
        if args.command != "add" and not path.exists():
            if args.command == "rounds":
                print(0)
            return 0
        if args.command == "add":
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("a+" if args.command == "add" else "r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if args.command == "add" else fcntl.LOCK_SH)
            rows = records(stream)
            if args.command == "rounds":
                print(rounds(rows, family))
                return 0
            if args.command == "list":
                for row in rows:
                    print(json.dumps(row, ensure_ascii=True))
                return 0
            is_exec = args.seat.startswith(("seat-exec", "worker-")) or args.seat == "general-purpose"
            number = rounds(rows, family) + 1 if is_exec and not args.no_edits else None
            if number is not None:
                permitted = ((number <= 2 and args.model == "sonnet" and not args.escalated)
                             or (number == 3 and args.model == "opus" and args.escalated))
                if not permitted:
                    print("ROUND CAP: use Sonnet twice, then Opus with --escalated once; "
                          "after that return to the owner", file=sys.stderr)
                    return 3
            row = {"schema": 1, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "family": family, "brief": args.brief, "seat": args.seat,
                   "model": args.model, "verdict": args.verdict, "round": number,
                   "escalated": args.escalated, "no_edits": args.no_edits, "notes": args.notes}
            stream.seek(0, os.SEEK_END)
            # An existing final record may be valid JSON without a final newline.
            if stream.tell():
                stream.seek(0)
                if not stream.read().endswith("\n"):
                    stream.write("\n")
            stream.write(json.dumps(row, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            print(json.dumps(row, ensure_ascii=True))
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"dispatch-log: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
