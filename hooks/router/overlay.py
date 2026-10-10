"""Local JSON Merge Patch for the route table. No dependency on common."""
import copy
import json
import os
from pathlib import Path
import tempfile


class OverlayError(ValueError):
    """An override file cannot be used."""


def overlay_path(environ=None):
    env = os.environ if environ is None else environ
    configured = env.get("ROUTER_LOCAL")
    if configured == "off":
        return None
    if configured:
        return Path(configured).expanduser()
    base = Path(env.get("XDG_CONFIG_HOME") or "").expanduser()
    if not base.is_absolute():
        base = Path(env.get("HOME") or Path.home()) / ".config"
    return base / "router/routes.local.json"


def read_patch(path):
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    except OSError as exc:
        raise OverlayError(f"{path}: unreadable: {exc}") from exc
    if info is None:
        if not os.environ.get("ROUTER_LOCAL"):
            return None
        raise OverlayError(f"{path}: overlay file not found")
    try:
        regular = path.is_file()
    except OSError as exc:
        raise OverlayError(f"{path}: unreadable: {exc}") from exc
    if not regular:
        raise OverlayError(f"{path}: not a regular file")
    try:
        with path.open("rb") as stream:
            data = stream.read(65537)
    except OSError as exc:
        raise OverlayError(f"{path}: unreadable: {exc}") from exc
    if len(data) > 65536:
        raise OverlayError(f"{path}: larger than 65536 bytes")
    try:
        value = json.loads(data.decode("utf-8"))
    except RecursionError as exc:
        raise OverlayError(f"{path}: nested too deeply") from exc
    except (UnicodeError, ValueError) as exc:
        raise OverlayError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise OverlayError(f"{path}: must be a JSON object")
    return value


def merge(base, patch):
    """RFC 7396 merge, leaving both inputs untouched."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge(result.get(key), value)
    return result


def set_key(patch, key, value):
    result = copy.deepcopy(patch)
    parts = key.split(".")
    node = result
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = copy.deepcopy(value)
    return result


def unset_key(patch, key):
    result = copy.deepcopy(patch)
    parts = key.split(".")
    nodes = [result]
    for part in parts[:-1]:
        node = nodes[-1].get(part)
        if not isinstance(node, dict):
            return None
        nodes.append(node)
    if parts[-1] not in nodes[-1]:
        return None
    del nodes[-1][parts[-1]]
    for index in range(len(parts) - 2, -1, -1):
        if nodes[index + 1]:
            break
        del nodes[index][parts[index]]
    return result


def write_patch(path, patch):
    """Replace the patch atomically, with private directory and file modes."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".routes.local.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(patch, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
