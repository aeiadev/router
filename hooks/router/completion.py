"""Read-only completion evidence guidance shared by standalone packages."""
import sys
sys.dont_write_bytecode = True

CHECKLIST_VERSION = 1
_LINES = (
    "Inspection: Inspect the actual changed files or diff and confirm the intended edits landed.",
    "Acceptance: Check requirements against BAR evidence and record verifier identity and provenance. Inspection or a reported PASS alone is not independent acceptance.",
    "External readback: Observe the resulting external state without replaying the mutation; record evidence, not-applicable, or unknown.",
)


def render_checklist() -> str:
    return "\n".join(_LINES) + "\n"


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("completion checklist: no arguments expected", file=sys.stderr)
        return 2
    sys.stdout.write(render_checklist())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
