#!/usr/bin/env python3
"""Check citation paths and line ranges without printing file contents."""
import argparse
from pathlib import Path
import re
import sys

CITATION = re.compile(
    r"(?<![\w/.-])`?(?P<path>/?(?:[\w.-]+/)*[\w.-]+)`?:"
    r"(?P<first>\d+)(?:-(?P<last>\d+))?`?"
)
CONTINUATION = re.compile(r"\s+and\s+(\d+)(?:-(\d+))?(?![\d:])")


def citations(text):
    for match in CITATION.finditer(text):
        path = match["path"]
        if "/" not in path and "." not in path:
            continue
        first = int(match["first"])
        yield path, first, int(match["last"] or first)
        end = match.end()
        while continuation := CONTINUATION.match(text, end):
            first = int(continuation[1])
            yield path, first, int(continuation[2] or first)
            end = continuation.end()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="resolve citation paths inside this directory (default: cwd)")
    args = parser.parse_args()
    try:
        text = args.report.read_text(encoding="utf-8")
        root = args.root.resolve(strict=True)
    except (OSError, UnicodeError) as exc:
        print(f"cite-check: cannot read input: {exc}", file=sys.stderr)
        return 2
    found = list(citations(text))
    if not found:
        print("MISSING: report has no path:line citations")
        return 1
    failed = False
    counts = {}
    for path, first, last in found:
        label = f"{path}:{first}" + (f"-{last}" if last != first else "")
        try:
            resolved = (root / path).resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError("citation must name a file inside --root")
            if resolved not in counts:
                with resolved.open(encoding="utf-8", errors="replace") as stream:
                    counts[resolved] = sum(1 for _ in stream)
            if not 1 <= first <= last <= counts[resolved]:
                raise ValueError(f"invalid range for a file with {counts[resolved]} lines")
            print(f"OK {label}")
        except (OSError, ValueError) as exc:
            print(f"MISSING {label}: {exc}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
