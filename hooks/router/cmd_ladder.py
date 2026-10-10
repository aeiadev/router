"""router ladder list | router ladder reset <key>: show or clear execution ladders.

A ladder is the attempts for one key (Claude "c:", Codex "x:") newer than
router.ladder_ttl_hours. Reset archives every attempt for that key, so its next spawn is round 1.
"""
import argparse
import sqlite3
import sys

import common
import ledger

HELP = "ladder list | ladder reset <key>: show or archive execution ladders"
ROUNDS = 3  # two standard rounds, then one upper-tier round


def age(seconds) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h{minutes % 60:02d}m"
    return f"{minutes // (24 * 60)}d"


def main(argv) -> int:
    parser = argparse.ArgumentParser(prog="router ladder", description=HELP)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="key, round, tier and age of each live ladder")
    reset = actions.add_parser("reset", help="archive every attempt for one key")
    reset.add_argument("key")
    args = parser.parse_args(argv)
    path = ledger.ledger_path()
    try:
        if args.action == "reset":
            archived = ledger.reset_ladder(args.key, path)
            if not archived:
                print(f"router ladder: no attempts for {args.key} in {path}", file=sys.stderr)
                return 1
            print(f"reset {args.key}: {archived} attempt(s) archived; its next spawn is round 1")
            return 0
        ttl = common.ladder_ttl_hours(common.load_routes())
        rows = ledger.ladders(ttl_hours=ttl, path=path)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"router ladder: {path}: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print(f"no live ladders (attempts newer than {ttl} h) in {path}")
        return 0
    width = max(len("key"), *(len(row["key"]) for row in rows))
    print(f"{'key':<{width}}  round  tier  age")
    for row in rows:
        print(f"{row['key']:<{width}}  {row['round']}/{ROUNDS:<3}  {row['tier'] or '-':<4}  {age(row['age'])}")
    return 0
