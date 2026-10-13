"""Offline evaluation CLI: python3 -B cmd_evaluate.py --file PATH|- ."""
import sys
sys.dont_write_bytecode = True

import argparse
import json
import os
import stat

import compat
import evaluation

HELP = "evaluate --file PATH|-: offline schema-1 evaluation; coordinator attestations only"
MAX_INPUT_BYTES = 4 * 1024 * 1024


def _read(path):
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        descriptor = compat.open_fd(path)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("input must be regular")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                raw = source.read(MAX_INPUT_BYTES + 1)
        finally:
            if descriptor != -1:
                os.close(descriptor)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("invalid JSON") from None


class BoundedParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "router evaluate: invalid arguments\n")


def main(argv=None):
    parser = BoundedParser(prog="router evaluate", description=HELP)
    parser.add_argument("--file", required=True, metavar="PATH", help="schema-1 JSON or - for stdin")
    args = parser.parse_args(argv)
    try:
        result = evaluation.evaluate(_read(args.file))
    except (evaluation.EvaluationError, OSError, ValueError, TypeError, RecursionError):
        print("router evaluate: rejected input", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
