"""Read-only, opt-in operations brief and declared tool preflight."""
import argparse
import json
import os
import stat
import sys

import common
import operations

HELP = "operations check --file PATH|-: validate profile and declared tool readiness before a spawn"
MAX_INPUT_BYTES = 64 * 1024


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Argparse messages can contain whole argument values, including private data.
        self.print_usage(sys.stderr)
        self.exit(2, "router operations: invalid arguments\n")


def _read_object(path):
    if path == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        descriptor = common.compat.open_fd(path)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("input must be a regular file")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read(MAX_INPUT_BYTES + 1)
        finally:
            if descriptor != -1:
                os.close(descriptor)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds 64 KiB")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise ValueError("input must be a UTF-8 JSON object") from None
    if not isinstance(value, dict) or set(value) != {"brief", "agent", "required_tools", "available_tools"}:
        raise ValueError("input requires brief, agent, required_tools and available_tools")
    return value


def main(argv):
    parser = _ArgumentParser(prog="router operations", description=HELP)
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--file", required=True, metavar="PATH", help="JSON object or - for stdin")
    args = parser.parse_args(argv)
    try:
        request = _read_object(args.file)
        operations.parse_brief(request["brief"])
        result = operations.assess_agent(request["agent"], request["required_tools"], request["available_tools"])
        routes = common.load_routes()
        classified = common.classify(request["agent"], routes)
        role_class = classified["class"] if classified else "passthrough"
        if classified and classified["base"] in ("builder", "builder-in-place"):
            raise operations.OperationsError("operations profile requires a named custom agent or generic worker")
        result["routing_class"] = role_class
        result["brief_valid"] = True
    except (OSError, ValueError, TypeError, RecursionError):
        print("router operations: invalid input or operations profile", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 3 if result["tool_status"] == "declared_missing" else 0
