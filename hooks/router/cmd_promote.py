"""Promote one measured shadow rule through the local overlay."""
import importlib.util
import os
from pathlib import Path
import sys
import time

import common
import overlay

HELP = "promote RULE [--since N] [--force] [--dry-run]: shadow rule to enforce after report evidence"


def _error(message, code=1):
    print(f"router promote: {message}", file=sys.stderr)
    return code


def _report():
    spec = importlib.util.spec_from_file_location("router_cmd_report", Path(__file__).with_name("cmd_report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _present(path):
    """True unless the path is plainly absent (same rule as router config)."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _hits(section, rule, report, since):
    if section == "router":
        count = sum(record.get("shadow") is True and record.get("rule") == rule
                    for record in report.read_log("spawns", since))
        return count, [(f"spawn.{rule}", count)] if count else []
    events = ("stop_cap", "handback_cap") if rule in ("stop_cap_seats", "stop_cap_research") else (rule,)
    records = report.read_log("context", since)
    lines = [(f"context.{event}", sum(record.get("action") == "shadow" and record.get("event") == event
                                     for record in records)) for event in events]
    return sum(count for _, count in lines), [(name, count) for name, count in lines if count]


def main(argv):
    if not argv or argv[0].startswith("-"):
        return _error(f"usage: {HELP}", 2)
    name = argv[0]
    window = "7d"
    force = dry = False
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--since" and index + 1 < len(argv):
            window = argv[index + 1]
            index += 2
        elif arg == "--force":
            force = True
            index += 1
        elif arg == "--dry-run":
            dry = True
            index += 1
        else:
            return _error(f"usage: {HELP}", 2)
    report = _report()
    hours = report.parse_since(window)
    if hours is None:
        return _error("--since must look like 36h or 7d", 2)
    if hours > report.MAX_HOURS:
        return _error("--since must be at most 90d", 2)
    path = overlay.overlay_path()
    if path is None:
        return _error("overlay is off (ROUTER_LOCAL=off)")
    present = _present(path)
    try:
        if present:
            routes, problem = common.load_routes_checked()
        else:
            routes, problem = common.load_routes(overlay=False), None
    except common.RoutesError as exc:
        return _error(str(exc))
    if problem:
        detail = problem["message"].removeprefix(f"routes overlay {path} is invalid: ")
        return _error(f"{path}: {detail}")
    matches = [(section, rule) for section in ("router", "context")
               for rule in routes[section]["modes"]
               if name in (rule, f"{section}.modes.{rule}")]
    if len(matches) != 1:
        return _error(f"unknown rule: {name}")
    section, rule = matches[0]
    mode = routes[section]["modes"][rule]
    if mode != "shadow":
        return _error(f"{name} is {mode}, not shadow")
    count, lines = _hits(section, rule, report, time.time() - hours * 3600)
    print(f"evidence: {name} had {count} shadow hit(s) in the last {window}")
    for label, amount in lines:
        print(f"{label}  {amount}")
    if not count and not force:
        return _error(f"no shadow hits for {name} in the last {window}; run router report to look, or pass --force")
    try:
        patch = (overlay.read_patch(path) or {}) if present else {}
        changed = overlay.set_key(patch, f"{section}.modes.{rule}", "enforce")
        base = common.load_routes(overlay=False)
        merged = overlay.merge(base, changed)
        errors = []
        if merged.get("version") != base.get("version"):
            errors.append("version: cannot change")
        errors.extend(common.validate_routes(merged))
        if errors:
            return _error(f"{path}: {'; '.join(errors)}")
        if not dry:
            overlay.write_patch(path, changed)
    except (OSError, overlay.OverlayError, common.RoutesError) as exc:
        return _error(f"{path}: {exc}")
    verb = "would promote" if dry else "promoted"
    print(f"{verb} {name}: shadow -> enforce ({path})")
    return 0
