"""Explicit, read-only completion evidence checklist."""

from completion import main as completion_main

HELP = "checklist: show inspection, acceptance, and external readback steps"


def main(argv) -> int:
    return completion_main(argv)
