"""Explicit commands-only Router dispatcher; forbidden names never import a command."""
import importlib
import json
import os
from pathlib import Path
import sys

COMMANDS = {"operations", "observe", "receipt", "usage", "evaluate"}
META = {"status", "doctor", "help", "version"}


def _identity(root):
    profile = root / "profile.json"
    if profile.is_file():
        value = json.loads(profile.read_text(encoding="utf-8"))
        if value.get("host") not in ("claude", "codex"):
            raise ValueError("invalid profile identity")
        manifest = root / "manifest.json"
        if manifest.is_file():
            import hashlib
            record = json.loads(manifest.read_text(encoding="utf-8"))
            expected = record["files"]["profile.json"]["sha256"]
            if hashlib.sha256(profile.read_bytes()).hexdigest() != expected or value.get("source_revision") != record["source_revision"]:
                raise ValueError("profile identity drift")
        return value
    # Source entry is useful before installation; installed entry always has profile.json.
    if (root / "manifest.json").exists():
        raise ValueError("profile identity missing")
    version = root / "VERSION"
    return {"host": "claude", "version": version.read_text().strip() if version.is_file() else "unknown",
            "source_revision": "source"}


def _status(root, identity):
    import hashlib
    from receipt_context import state_path
    manifest = root / "manifest.json"
    return {"profile": "commands-only", "product": "router", "host": identity["host"],
            "version": identity["version"], "source_revision": identity["source_revision"],
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None,
            "state": str(state_path()), "registered_hooks": False,
            "host_availability": "unknown", "policy_interception": False,
            "mcp_qualification": "unknown"}


def main(argv, *, root):
    if not argv:
        argv = ["help"]
    name = argv[0]
    if name not in COMMANDS | META:
        print("router commands: command unavailable in commands-only profile", file=sys.stderr)
        return 2
    try:
        identity = _identity(root)
        os.environ["ROUTER_COMMANDS_HOST"] = identity["host"]
        if name == "help":
            print("router commands-only: " + ", ".join(sorted(COMMANDS | META)))
            return 0
        if name == "version":
            print(identity["version"])
            return 0
        if name in ("status", "doctor"):
            print(json.dumps(_status(root, identity), sort_keys=True))
            return 0
        if name == "operations":
            return operations_main(argv[1:])
        if name == "usage":
            return usage_main(argv[1:])
        if name == "receipt":
            sys.modules["_router_commands_receipt_context"] = importlib.import_module("receipt_context")
        return importlib.import_module("cmd_" + name).main(argv[1:])
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        print("router commands: request unavailable or invalid", file=sys.stderr)
        return 1


def operations_main(argv):
    import argparse
    import json
    import os
    import stat
    import operations
    import compat
    parser = argparse.ArgumentParser(prog="router operations")
    parser.add_argument("command", choices=("check",))
    parser.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    try:
        if args.file == "-":
            raw = sys.stdin.buffer.read(65537)
        else:
            with os.fdopen(compat.open_fd(args.file), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("regular file required")
                raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("input too large")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"brief", "agent", "required_tools", "available_tools"}:
            raise ValueError("invalid envelope")
        operations.parse_brief(request["brief"])
        if request["agent"] in ("builder", "builder-in-place"):
            raise ValueError("governed agent not supported")
        result = operations.assess_agent(request["agent"], request["required_tools"], request["available_tools"])
        result.update({"routing_class": "passthrough", "brief_valid": True})
    except (OSError, ValueError, TypeError, RecursionError):
        print("router operations: invalid input or operations profile", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 3 if result["tool_status"] == "declared_missing" else 0


def usage_main(argv):
    import argparse
    import json
    import os
    import stat
    import compat
    import usage
    from receipt_context import state_path
    parser = argparse.ArgumentParser(prog="router usage")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("declare", "record", "report"):
        p = sub.add_parser(name)
        p.add_argument("task_id", nargs="?" if name == "report" else None)
        if name != "report":
            p.add_argument("--file", required=True)
    args = parser.parse_args(argv)
    try:
        state = state_path() / "usage"
        if args.command == "report":
            result = usage.get_report(args.task_id, state_dir=state)
        else:
            if args.file == "-":
                raw = sys.stdin.buffer.read(65537)
            else:
                with os.fdopen(compat.open_fd(args.file), "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("regular file required")
                    raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("input too large")
            value = json.loads(raw)
            if args.command == "declare":
                result = usage.declare_task(args.task_id, value, state_dir=state)
            else:
                if set(value) != {"adapter", "context", "payload"}:
                    raise ValueError("invalid record")
                event = usage.normalize_event(value["adapter"], value["payload"], value["context"])
                result = usage.record_event(args.task_id, event, state_dir=state)
    except (OSError, ValueError, TypeError, RecursionError):
        print("router usage: input or usage store rejected request", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0
