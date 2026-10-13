"""Inspect and edit the local routes override."""
import json
import os
import stat
import sys

import common
import overlay

HELP = "config path | get <key> | set <key> <json> | unset <key> | check | migrate inheritance [--apply]: local routes override"


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


def _backup(path):
    """Save exact source bytes privately, choosing a new name without replacing a collision."""
    with open(path, "rb") as source:
        data = source.read()
    for number in range(10000):
        candidate = path.with_name(path.name + ".bak" + (f".{number}" if number else ""))
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.chmod(candidate, stat.S_IRUSR | stat.S_IWUSR)
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
        return candidate
    raise OSError("no free backup name")


def _migrate_inheritance(path, apply):
    if path is None:
        return _error("router config: overlay is off (ROUTER_LOCAL=off)")
    try:
        base = common.load_routes(overlay=False)
        present = _present(path)
        if present and path.is_symlink():
            return _error(f"router config: {path}: symlink overlay refused for migration")
        patch = _existing(path) if present else {}
        merged = overlay.merge(base, patch)
        errors = (["version: cannot change"] if merged.get("version") != base.get("version") else [])
        errors.extend(common.validate_routes(merged))
        if errors:
            return _error(f"router config: {path}: {'; '.join(errors)}")
        key = merged["router"]["unlisted"]["mode_key"]
        if key in common.MODE_KEYS or key == "judge_reminder":
            return _error(f"router config: {path}: shared mode key {key!r} cannot be changed by inheritance cleanup")
        if common.effective_mode(merged["router"]["modes"].get(key)) == "off":
            print(f"legacy inheritance cleanup: already applied ({key}=off); no changes")
            return 0
        # The mode key is a literal JSON member; it may itself contain a dot.
        mode_patch = dict(patch.get("router", {}).get("modes", {}))
        mode_patch[key] = "off"
        changed = overlay.set_key(patch, "router.modes", mode_patch)
        errors = common.validate_routes(overlay.merge(base, changed))
        if errors:
            return _error(f"router config: {path}: {'; '.join(errors)}")
        if not apply:
            print(f"legacy inheritance cleanup preview: set router.modes.{key}=off in {path}; "
                  "named agents already inherit; run with --apply to write")
            return 0
        backup = _backup(path) if present else None
        overlay.write_patch(path, changed)
        print(f"legacy inheritance cleanup applied: router.modes.{key}=off in {path}")
        if backup:
            print(f"backup: {backup}")
        return 0
    except (OSError, ValueError, common.RoutesError, overlay.OverlayError) as exc:
        return _error(f"router config: {exc}")


def main(argv):
    if argv and argv[0] == "migrate":
        if argv[1:] == ["inheritance"]:
            return _migrate_inheritance(overlay.overlay_path(), False)
        if argv[1:] == ["inheritance", "--apply"]:
            return _migrate_inheritance(overlay.overlay_path(), True)
        return _error(f"router config: usage: {HELP}")
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
