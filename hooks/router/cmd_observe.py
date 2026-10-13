#!/usr/bin/env python3
"""Bounded JSON wrapper for explicit Harness observation reports."""

import sys
sys.dont_write_bytecode = True

import json
import os
import stat

from compat import open_fd
from observation import ObservationError, assess


HELP = "observe --file PATH|-: assess readback proof without replay, repair or gating"
LIMIT = 65536


class InputError(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise InputError("duplicate JSON key")
        value[key] = item
    return value


def read_input(path):
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(LIMIT + 1)
        else:
            with os.fdopen(open_fd(path), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise InputError("input is not a regular file")
                raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise InputError("input exceeds limit")
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if type(value) is not dict or set(value) != {"expected", "observed"}:
            raise InputError("invalid envelope")
        return value
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise InputError("invalid JSON input") from None


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "--file" or not args[1]:
        print("usage: router observe --file PATH|-", file=sys.stderr)
        return 2
    try:
        value = read_input(args[1])
        result = assess(value["expected"], value["observed"])
        sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (InputError, ObservationError, OSError, ValueError, RecursionError):
        print("observation: invalid input", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
