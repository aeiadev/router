"""Inspect and edit the local routes override."""
import json
import os
import sys

import common
import overlay

HELP = "config path | get <key> | set <key> <json> | unset <key> | check: local routes override"


def _error(message):
    print(message, file=sys.stderr)
    return 1


def _compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _value(routes, key):
    node = routes
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


def _present(path):
    """True unless the path is plainly absent; an unreadable path counts as present."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _detail(problem, path):
    return problem["message"].removeprefix(f"routes overlay {path} is invalid: ")


def _existing(path):
    if not _present(path):
        return {}
    try:
        return overlay.read_patch(path) or {}
    except overlay.OverlayError as exc:
        raise ValueError(str(exc)) from exc


def main(argv):
    if not argv or argv[0] not in ("path", "get", "set", "unset", "check"):
        return _error(f"router config: usage: {HELP}")
    action = argv[0]
    expected = {"path": 1, "get": 2, "set": 3, "unset": 2, "check": 1}[action]
    if len(argv) != expected:
        return _error(f"router config: usage: {HELP}")
    path = overlay.overlay_path()
    if action == "path":
        print(path if path is not None else "off")
        if path is None or not (_present(path) or os.environ.get("ROUTER_LOCAL")):
            print("absent")
        else:
            try:
                _, problem = common.load_routes_checked()
                print(f"invalid: {_detail(problem, path)}" if problem else "ok")
            except common.RoutesError as exc:
                print(f"invalid: {exc}")
        return 0
    if action in ("set", "unset") and path is None:
        return _error("router config: overlay is off (ROUTER_LOCAL=off)")
    if action in ("get", "check"):
        try:
            routes, problem = common.load_routes_checked()
        except common.RoutesError as exc:
            return _error(f"router config: {exc}")
        if action == "check":
            if problem:
                return _error(f"overlay invalid: {path}: {_detail(problem, path)}")
            print(f"overlay ok: {path}" if path is not None and _present(path) else "no overlay")
            return 0
        if problem:
            print(f"overlay invalid: {path}: {_detail(problem, path)}", file=sys.stderr)
        try:
            print(_compact(_value(routes, argv[1])))
            return 0
        except KeyError:
            return _error(f"router config: no such key: {argv[1]}")
    key = argv[1]
    if not key or any(not part for part in key.split(".")) or key == "version" or key.startswith("version."):
        return _error(f"router config: {key}: invalid key")
    try:
        patch = _existing(path)
    except ValueError as exc:
        return _error(f"router config: {exc}")
    if _present(path):
        try:
            _, problem = common.load_routes_checked()
        except common.RoutesError as exc:
            return _error(f"router config: {exc}")
        if problem:
            return _error(f"router config: {path}: {_detail(problem, path)}")
    if action == "unset":
        changed = overlay.unset_key(patch, key)
        if changed is None:
            return _error(f"router config: {key} is not set in {path}")
        try:
            base = common.load_routes(overlay=False)
        except common.RoutesError as exc:
            return _error(f"router config: {exc}")
        errors = common.validate_routes(overlay.merge(base, changed))
        if errors:
            return _error(f"router config: {path}: {'; '.join(errors)}")
        try:
            overlay.write_patch(path, changed)
        except OSError as exc:
            return _error(f"router config: {path}: {exc}")
        print(f"unset {key}")
        return 0
    try:
        value = json.loads(argv[2])
    except ValueError:
        return _error(f"router config: not valid JSON: {argv[2]}")
    changed = overlay.set_key(patch, key, value)
    try:
        base = common.load_routes(overlay=False)
    except common.RoutesError as exc:
        return _error(f"router config: {exc}")
    merged = overlay.merge(base, changed)
    errors = []
    if merged.get("version") != base.get("version"):
        errors.append("version: cannot change")
    errors.extend(common.validate_routes(merged))
    if errors:
        return _error(f"router config: {path}: {'; '.join(errors)}")
    try:
        overlay.write_patch(path, changed)
    except OSError as exc:
        return _error(f"router config: {path}: {exc}")
    print(f"{key} = {_compact(value)}")
    return 0
