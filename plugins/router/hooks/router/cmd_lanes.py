"""router lanes [--all] [--session HASH]: the lanes Router recorded, newest first.

By default it lists lanes of the current project (from the working directory) started in the last
24 hours, at most 20. --all drops the project filter and --session selects only its session,
without a project or time filter, by the hash shown
in the session column. Round is the ladder round the lane ran as; verdict is the newest judge
verdict for its brief since it started. Codex lanes stay running: no SubagentStop is registered there.
"""
import argparse
import sqlite3
import sys
import time

import common
import ledger
from cmd_ladder import ROUNDS, age

HELP = "lanes [--all] [--session HASH]: recent lanes, or one session across projects and time"
WINDOW_HOURS = 24
LIMIT = 20
COLUMNS = ("age", "session", "role-tier", "round", "status", "verdict", "label")


def row(lane, now) -> tuple:
    return (age(max(0.0, now - lane["started"])), lane["session"] or "-",
            "-".join(part for part in (lane["role"], lane["tier"]) if part) or "-",
            f"{lane['round']}/{ROUNDS}" if lane["round"] else "-", lane["status"], lane["verdict"] or "-",
            lane["label"] or "-")


def main(argv) -> int:
    parser = argparse.ArgumentParser(prog="router lanes", description=HELP)
    parser.add_argument("--all", action="store_true", help="lanes of every project, not only this one")
    parser.add_argument("--session", metavar="HASH", help="only the lanes of this session hash")
    args = parser.parse_args(argv)
    path = ledger.ledger_path()
    now = time.time()
    try:
        lanes = ledger.lanes_report(session=args.session, project=None if args.all or args.session else common.project_key(),
                                    since=None if args.session else now - WINDOW_HOURS * 3600,
                                    limit=LIMIT, path=path, strict=True)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"router lanes: {path}: {exc}", file=sys.stderr)
        return 1
    if not lanes:
        print("no lanes")
        return 0
    rows = [row(lane, now) for lane in lanes]
    widths = [max(len(name), *(len(cells[index]) for cells in rows)) for index, name in enumerate(COLUMNS)]
    for cells in (COLUMNS, *rows):
        print("  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip())
    return 0
