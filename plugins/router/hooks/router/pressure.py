"""Read the Harness context pressure signal safely and only tighten limits."""
import hashlib
import json
import math
import os
import stat
import time
from pathlib import Path

import common


def host_of(data, argv=None):
    """Choose the Harness host before using the event shape as a fallback."""
    host = data.get("harness_host")
    if host in ("claude", "codex"):
        return host
    for arg in (argv or ())[1:]:
        if arg in ("--host=claude", "--host=codex"):
            return arg.split("=", 1)[1]
    return "codex" if "turn_id" in data else "claude"


def path_for(session_id):
    if not isinstance(session_id, str) or not session_id:
        return None
    xdg = os.environ.get("XDG_STATE_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local/state"
    digest = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return base / "claude-harness" / "pressure" / (digest + ".json")


def warn(path, error):
    try:
        marker = common.state_dir() / "pressure-warned"
        if marker.exists() and time.time() - marker.stat().st_mtime < 3600:
            return
        common.log("errors", {"script": "pressure", "file": str(path), "error": error})
        marker.touch()
    except OSError:
        pass


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def read(session_id, host, now=None):
    path = path_for(session_id)
    if path is None:
        return None
    try:
        for directory in (path.parent, path.parent.parent):
            if directory.exists() and not directory.stat().st_mode & 0o111:
                raise PermissionError("unsearchable directory")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular file")
            if not info.st_mode & 0o444:
                raise PermissionError("unreadable file")
            if info.st_size > 4096:
                raise ValueError("oversized file")
            raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError("oversized file")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("not an object")
        current = time.time() if now is None else now
        if (type(value.get("schema")) is not int or value["schema"] != 1
                or value.get("host") != host or not number(value.get("ts"))
                or not -60 <= current - value["ts"] <= 600):
            return None
        if value.get("level") not in ("ok", "remind", "urgent"):
            return None
        if (not number(value.get("percent")) or value["percent"] > 1000
                or not number(value.get("used")) or not number(value.get("window"))
                or value["window"] <= 0):
            return None
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        warn(path, type(exc).__name__)
        return None


def limits(routes, level):
    """Pressure limits for a level: explicit context.pressure values, else derived from the base."""
    context = routes["context"]
    first = context["chain"]["first"]
    size = context["large_read_bytes"]
    if level == "remind":
        derived = (-(-first * 6 // 10), size)
    else:
        derived = (-(-first * 4 // 10), size // 2)
    explicit = context.get("pressure", {}).get(level, {})
    out = []
    for key, base, default in (("chain_first", first, derived[0]), ("large_read_bytes", size, derived[1])):
        value = explicit.get(key, default)
        out.append(max(1, min(base, value)))
    return tuple(out)


def tightened(routes, signal):
    context = routes["context"]
    first = context["chain"]["first"]
    size = context["large_read_bytes"]
    if signal and signal.get("level") in ("remind", "urgent"):
        level_first, level_size = limits(routes, signal["level"])
        first = min(first, level_first)
        size = min(size, level_size)
    return first, size
