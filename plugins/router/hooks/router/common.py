"""Shared helpers for standalone routing hooks. Python 3.10+, standard library only.

Paths are resolved from ROUTER_HOME, CLAUDE_HOME, ROUTES_JSON, ROUTER_STATE,
XDG_STATE_HOME, and ROUTER_OFF_FILE. ROUTER_OFF=1 disables all routing.
Importing this module does not read or create state.
"""
import fnmatch
import importlib.util
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import shutil
import tempfile
import textwrap
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

ROUTES_PATH = Path(os.environ.get("ROUTES_JSON") or Path(__file__).resolve().parent / "routes.json").expanduser()


class RoutesError(ValueError):
    def __init__(self, file, message):
        self.file = str(file)
        super().__init__(message)


def _overlay_module():
    spec = importlib.util.spec_from_file_location("router_overlay", Path(__file__).with_name("overlay.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

MODES = ("off", "shadow", "enforce")
AUTO_MODES = ("off", "suggest", "nudge", "enforce")
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
NO_EFFORT = "none"  # haiku tiers carry no effort line; only haiku may (and must) use it
REQUIRED_KEYS = ("version", "tiers", "router", "context")
MAX_LOG_STR = 300
TRANSCRIPT_SCAN_LINES = 20
TRANSCRIPT_TAIL_BYTES = 256 * 1024  # session_model reads at most this much of a transcript's end
FRONTMATTER_BYTES = 8192
PLUGIN_SEARCH_DEPTH = 5  # plugin dirs searched for agents/<name>.md, at most this deep below plugins/
PLUGIN_SEARCH_DIRS = 1500  # and at most this many dirs per lookup
PLUGIN_SKIP_DIRS = frozenset(("node_modules", "agents", "skills", "commands", "hooks"))  # never hold a plugin
PROJECT_SEARCH_LEVELS = 8  # <cwd>/.claude/agents, then up to this many parents
# Plugin agents are invoked as <plugin>:<name>; these prefixes name the same roles.
ROLE_PREFIXES = frozenset(("router", "harness", "shared-roles"))
PLUGIN_MARKETPLACE = "router"  # the marketplace in .claude-plugin/marketplace.json
PLUGINS = ("router", "shared-roles")
SHARED_ROLE_NAMES = ("builder", "builder-in-place", "docs-writer", "judge", "planner", "researcher",
                     "sweeper", "test-writer", "worker")
PLUGIN_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,40}")

READ_TOOLS = frozenset(("Read", "Grep", "Glob"))
READ_COMMANDS = frozenset((
    "cat", "head", "tail", "sed", "awk", "rg", "grep", "ls", "find", "fd",
    "jq", "wc", "stat", "tree", "cut", "sort", "uniq", "less", "file", "du",
))

ROUTE_RE = re.compile(r"^\s*route:\s*(light|up)\b(.*)$", re.IGNORECASE | re.MULTILINE)
BRIEF_FIELDS = ("TASK", "FILES", "BAR", "RETURN")
FIELD_LINE = re.compile(r"[ \t]*(TASK|FILES|BAR|RETURN)(:|[ \t])(.*)", re.IGNORECASE)
LADDER_TTL_HOURS = 12  # router.ladder_ttl_hours default; valid 1..720
GIT_FILE_BYTES = 4096  # a .git file or commondir is read at most this far
LANE_LABEL_CHARS = 60
TOKEN_SPLIT = re.compile(r"[\s,;]+")
CD_PREFIX = re.compile(r"""cd(?:\s+(?:"[^"]*"|'[^']*'|[^\s;&|]+))?\s*(?:&&|;|\n)""")
ENV_PREFIX = re.compile(r"""[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|[^\s;&|]*)\s+""")
TIMEOUT_PREFIX = re.compile(r"timeout\s+(?:-k\s*\S+\s+)?\d+(?:\.\d+)?[smhd]?\s+")
FIRST_WORD = re.compile(r"[^\s;&|<>()]+")


# Paths and switches

def _env_path(name, *default_parts) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else Path.home().joinpath(*default_parts)


def router_home() -> Path:
    """Configuration home, without creating directories."""
    value = os.environ.get("ROUTER_HOME")
    return Path(value).expanduser() if value else claude_home() / "router"


def state_path() -> Path:
    """State location, without creating directories."""
    value = os.environ.get("ROUTER_STATE")
    if value:
        return Path(value).expanduser()
    base = Path(os.environ.get("XDG_STATE_HOME") or "").expanduser()
    return (base if base.is_absolute() else Path.home() / ".local/state") / "claude-router"


def off_file() -> Path:
    """The single persistent switch; presence disables every router hook."""
    value = os.environ.get("ROUTER_OFF_FILE")
    return Path(value).expanduser() if value else state_path() / "OFF"


def auto_mode() -> str:
    """Read the user setting beside OFF; absent or invalid state defaults to nudge."""
    try:
        mode = (off_file().parent / "auto").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "nudge"
    return mode if mode in AUTO_MODES else "nudge"


def set_auto_mode(mode: str) -> None:
    if mode not in AUTO_MODES:
        raise ValueError("invalid automatic routing mode")
    path = off_file().parent / "auto"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".auto-{os.getpid()}")
    try:
        with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as stream:
            stream.write(mode + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_defaults(host, routes, mode, *, template=None) -> str:
    """Render host guidance from the same values used by the routing hooks."""
    if host not in ("claude", "codex") or mode not in AUTO_MODES:
        raise ValueError("invalid defaults host or automatic routing mode")
    if template is None:
        root = Path(__file__).resolve().parents[2]
        folder = root / "templates"
        if not folder.is_dir():
            folder = root / "router/templates"
        template = (folder / f"defaults.{host}.md").read_text(encoding="utf-8")
    context = routes["context"]
    roles_by_cap = {}
    for role in ("builder", "judge", "sweeper", "researcher", "planner", "worker"):
        cap = context["caps"][routes["router"]["types"][role]["class"]]
        roles_by_cap.setdefault(cap, []).append(role + "s")

    def join_roles(roles):
        if len(roles) == 1:
            return roles[0]
        if len(roles) == 2:
            return " and ".join(roles)
        return ", ".join(roles[:-1]) + " and " + roles[-1]

    return_limits = ", ".join(
        f"{cap} {'characters ' if index == 0 else ''}for {join_roles(roles_by_cap[cap])}"
        for index, cap in enumerate(sorted(roles_by_cap))
    )
    values = {
        "files_threshold": context["files_threshold"],
        "command_threshold": context["command_threshold"],
        "chain_first": context["chain"]["first"],
        "return_limits": return_limits,
        "enforce_clause": (", and in enforce mode it blocks further reads until you delegate"
                           if mode == "enforce" else ""),
        "reminder": (f"Automatic routing is off, so nothing reminds you; delegate on your own after {context['chain']['first']} reads in a row."
                     if mode == "off" else f"After {context['chain']['first']} reads in a row the router reminds you" +
                     (", and it blocks further reads until you delegate." if mode == "enforce" else ".")),
    }

    def replace(match):
        key = match.group(1)
        if key not in values:
            raise ValueError(f"unknown defaults placeholder: {key}")
        return str(values[key])

    rendered = re.sub(r"\{\{(.*?)\}\}", replace, template, flags=re.DOTALL)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("malformed defaults placeholder")
    # Rewrap every bullet with its continuation lines so any value stays within 80 columns.
    rendered = re.sub(
        r"(?m)^- .*(?:\n  .*)*",
        lambda match: textwrap.fill(
            " ".join(match.group(0).split()),
            width=78,
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        ),
        rendered,
    )
    # The managed range ends at the comment, never at an outside newline.
    return rendered.rstrip("\n")


def defaults_span(data: bytes):
    """Find the single complete marker pair; reject ambiguous or broken pairs."""
    tokens = list(re.finditer(rb"<!--\s*router:defaults:", data))
    if not tokens:
        return None
    start = re.compile(rb"<!-- router:defaults:start(?: [^\r\n<>]*?)? -->")
    end = re.compile(rb"<!-- router:defaults:end -->")
    first = start.match(data, tokens[0].start())
    last = end.match(data, tokens[-1].start())
    if len(tokens) != 2 or first is None or last is None or first.end() > last.start():
        raise ValueError("defaults markers must be one complete, ordered start/end pair")
    return first.start(), last.end()


def defaults_manifest(home: Path):
    """Load ownership without following a manifest or directory symlink."""
    path = home / "router/install-manifest.json"
    if (home / "router").is_symlink() or path.is_symlink():
        raise ValueError(f"defaults manifest must not be a symlink: {path}")
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid defaults manifest: {path}")
    return value


def plan_defaults(home: Path, host: str, record, block):
    """Prepare a byte-exact edit without writing; None means remove the block."""
    path = home / ("CLAUDE.md" if host == "claude" else "AGENTS.md")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"defaults instructions must be a regular file: {path}")
    if record is not None and (not isinstance(record, dict) or
                               type(record.get("created")) is not bool):
        raise ValueError(f"{home / 'router/install-manifest.json'}: invalid defaults ownership")
    before = path.read_bytes() if path.exists() else b""
    try:
        span = defaults_span(before)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    replacement = block.encode("utf-8") if block is not None else b""
    metadata = dict(record) if record is not None else {"created": not path.exists()}
    if span is None:
        separator = b"\n" if replacement and before and not before.endswith(b"\n") else b""
        after = before + separator + replacement
        metadata["separator"] = bool(separator)
    else:
        start, end = span
        if block is None and metadata.get("separator") and before[:start].endswith(b"\n"):
            start -= 1
        after = before[:start] + replacement + before[end:]
    backup = None
    if before != after and path.exists() and not metadata["created"] and not metadata.get("backup"):
        backup = path.with_name(f"{path.name}.router-backup-{time.time_ns()}")
        metadata["backup"] = backup.name
    remove = block is None and metadata["created"] and not after
    return {"path": path, "before": before, "after": after, "backup": backup,
            "record": metadata, "remove": remove}


def write_bytes_atomic(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".router-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_defaults(plan):
    path = plan["path"]
    if plan["backup"] is not None:
        shutil.copy2(path, plan["backup"])
    if plan["remove"]:
        path.unlink(missing_ok=True)
    elif plan["before"] != plan["after"]:
        write_bytes_atomic(path, plan["after"])


def refresh_defaults(mode: str):
    """Preflight all known hosts before changing guidance or the shared mode."""
    homes = {host: _env_path(host.upper() + "_HOME", "." + host)
             for host in ("claude", "codex")}
    # An installed CLI also knows its own home without environment overrides.
    root = Path(__file__).resolve().parents[2]
    manifest = defaults_manifest(root)
    if manifest.get("defaults"):
        host = manifest["defaults"].get("host")
        if host in homes:
            homes[host] = root
    plans = []
    skipped = []
    for host, home in homes.items():
        manifest = defaults_manifest(home)
        record = manifest.get("defaults")
        if record is not None:
            path = home / ("CLAUDE.md" if host == "claude" else "AGENTS.md")
            if not path.exists():
                skipped.append(path)
                continue
            try:
                span = defaults_span(path.read_bytes())
            except ValueError as exc:
                raise ValueError(f"{path}: {exc}") from exc
            if span is None:
                skipped.append(path)
                continue
            routes = load_routes(os.environ.get("ROUTES_JSON") or home / "hooks/router/routes.json", overlay=True)
            template = (home / "router/templates" / f"defaults.{host}.md").read_text(encoding="utf-8")
            block = render_defaults(host, routes, mode, template=template)
            plan = plan_defaults(home, host, record, block)
            plans.append((home, manifest, plan))
    for home, manifest, plan in plans:
        apply_defaults(plan)
        if manifest["defaults"] != plan["record"]:
            manifest["defaults"] = plan["record"]
            write_bytes_atomic(home / "router/install-manifest.json",
                               (json.dumps(manifest, indent=2) + "\n").encode())
    return skipped


def state_dir() -> Path:
    """State directory, created only when a caller needs to store state."""
    path = state_path()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def killed() -> bool:
    """True when either representation of the kill switch is enabled."""
    return os.environ.get("ROUTER_OFF") == "1" or off_file().exists()


def _safe_name(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(".")


def _state_file(sub, session_id, suffix):
    """state_dir()/<sub>/<session_id><suffix>, or None when unusable."""
    name = _safe_name(session_id)
    if not name:
        return None
    try:
        folder = state_dir() / sub
        folder.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        return None
    return folder / f"{name}{suffix}"


# Route table

def _base_routes(path=None) -> dict:
    """Parse and validate the base table."""
    source = Path(os.environ.get("ROUTES_JSON") or ROUTES_PATH).expanduser() if path is None else Path(path).expanduser()
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoutesError(source, f"routes table {source} is unreadable: {exc}") from exc
    try:
        routes = json.loads(text)
    except ValueError as exc:
        raise RoutesError(source, f"routes table {source} is not valid JSON: {exc}") from exc
    errors = validate_routes(routes)
    if errors:
        raise RoutesError(source, f"routes table {source} is invalid: " + "; ".join(errors))
    return routes


def load_routes_checked(path=None, *, overlay=None):
    """Return effective routes and an overlay problem, if any."""
    base = _base_routes(path)
    if overlay is False or (overlay is None and path is not None):
        return base, None
    route_overlay = _overlay_module()
    local = route_overlay.overlay_path()
    if local is None:
        return base, None
    try:
        patch = route_overlay.read_patch(local)
        if patch is None:
            return base, None
        merged = route_overlay.merge(base, patch)
        if merged.get("version") != base.get("version"):
            raise ValueError("version: cannot change")
        errors = validate_routes(merged)
        if errors:
            raise ValueError("; ".join(errors))
        return merged, None
    except Exception as exc:
        detail = "nested too deeply" if isinstance(exc, RecursionError) else str(exc) or type(exc).__name__
        if isinstance(exc, route_overlay.OverlayError):
            detail = detail.removeprefix(f"{local}: ")
        return base, {"file": str(local), "message": f"routes overlay {local} is invalid: {detail}"}


def _warn_overlay(problem):
    try:
        marker = state_dir() / "overlay-warned"
        if marker.exists() and time.time() - marker.stat().st_mtime < 3600:
            return
        log("errors", {"script": "overlay", "file": problem["file"], "error": problem["message"]})
        marker.touch()
    except OSError:
        pass


def load_routes(path=None, *, overlay=None) -> dict:
    routes, problem = load_routes_checked(path, overlay=overlay)
    if problem:
        _warn_overlay(problem)
    return routes


def _section(routes, key, errors):
    value = routes.get(key)
    if isinstance(value, dict):
        return value
    if key in routes:
        errors.append(f"{key} must be an object")
    return None


def validate_routes(routes) -> list[str]:
    """Return a list of problems; empty when the table is valid."""
    if not isinstance(routes, dict):
        return ["routes table must be a JSON object"]
    errors = [f"missing top-level key {key!r}" for key in REQUIRED_KEYS if key not in routes]
    brief = _section(routes, "brief", errors)
    tiers = _section(routes, "tiers", errors)
    router = _section(routes, "router", errors)
    context = _section(routes, "context", errors)
    sources = _section(routes, "tier_sources", errors) or {}
    for base, entry in sources.items():
        if base not in (tiers or {}):
            errors.append(f"tier_sources.{base}: not a base under tiers")
        source = entry.get("source") if isinstance(entry, dict) else None
        if not isinstance(source, str) or not source:
            errors.append(f"tier_sources.{base}: needs an object with a source path")
        prefix = entry.get("prefix") if isinstance(entry, dict) else None
        if prefix is not None and (not isinstance(prefix, str) or not prefix):
            errors.append(f"tier_sources.{base}: prefix must be a non-empty string")
    if "active_after" in routes and not isinstance(routes["active_after"], str):
        errors.append("active_after must be a string")

    for name, section in (("router", router), ("context", context)):
        if section is None:
            continue
        modes = section.get("modes")
        if not isinstance(modes, dict):
            errors.append(f"{name}.modes must be an object")
            continue
        for rule, mode in modes.items():
            if mode not in MODES:
                errors.append(f"{name}.modes.{rule}: bad mode {mode!r} (want off, shadow or enforce)")

    taken = {}  # lower-cased agent name -> where it is used
    reserved = {str(name).lower() for name in (tiers or {})}
    type_names = _router(routes).get("types")
    if isinstance(type_names, dict):
        reserved |= {str(name).lower() for name in type_names}
    for seat, levels in (tiers or {}).items():
        if not isinstance(levels, dict) or not levels:
            errors.append(f"tiers.{seat} must be a non-empty object")
            continue
        for tier, spec in levels.items():
            where = f"tiers.{seat}.{tier}"
            if not isinstance(spec, dict):
                errors.append(f"{where} must be an object")
                continue
            for field, allowed in (("agent", None), ("model", MODELS), ("effort", EFFORTS + (NO_EFFORT,))):
                value = spec.get(field)
                if not isinstance(value, str) or not value:
                    errors.append(f"{where}: {field} is missing or not a string")
                elif allowed and value not in allowed:
                    errors.append(f"{where}: bad {field} {value!r} (want {', '.join(allowed)})")
            model, effort = spec.get("model"), spec.get("effort")
            if model in MODELS and effort in EFFORTS + (NO_EFFORT,) and (effort == NO_EFFORT) != (model == "haiku"):
                errors.append(f"{where}: effort {NO_EFFORT!r} goes with model haiku and only with it "
                              f"(got model {model!r}, effort {effort!r})")
            agent = spec.get("agent")
            if isinstance(agent, str) and agent:
                # a base with a declared source declares its own prefix (default "<base>-"); no agent name may
                # repeat or equal a base or router type, because classify looks those up first
                entry = sources.get(seat)
                declared = entry.get("prefix") if isinstance(entry, dict) else None
                prefix = declared if isinstance(declared, str) and declared else f"{seat}-"
                if not agent.startswith(prefix):
                    errors.append(f"{where}: agent {agent!r} must start with {prefix!r}")
                if agent.lower() in reserved:
                    errors.append(f"{where}: agent {agent!r} is a base or router type name")
                if agent.lower() in taken:
                    errors.append(f"{where}: duplicate agent {agent!r}, also used by {taken[agent.lower()]}")
                taken.setdefault(agent.lower(), where)

    used = []
    if router is not None:
        top_models = router.get("top_tier_models", [])
        if (not isinstance(top_models, list)
                or any(not isinstance(model, str) or not model.strip() for model in top_models)):
            errors.append("router.top_tier_models must be a list of non-empty model names")
        types = router.get("types")
        if not isinstance(types, dict):
            errors.append("router.types must be an object")
            types = {}
        for name, entry in types.items():
            where = f"router.types.{name}"
            if not isinstance(entry, dict):
                errors.append(f"{where} must be an object")
                continue
            cls = entry.get("class")
            if not isinstance(cls, str) or not cls:
                errors.append(f"{where}: class is missing or not a string")
            elif cls not in used:
                used.append(cls)
            if not isinstance(entry.get("allow"), list):
                errors.append(f"{where}: allow is missing or not a list")
        if "unlisted" in router:
            errors.extend(_unlisted_errors(router["unlisted"]))
        if "lane_labels" in router and not isinstance(router["lane_labels"], bool):
            errors.append(f"router.lane_labels: want true or false, got {router['lane_labels']!r}")
        if "ladder_ttl_hours" in router and not _valid_ttl(router["ladder_ttl_hours"]):
            errors.append(f"router.ladder_ttl_hours: want an integer from 1 to 720, got {router['ladder_ttl_hours']!r}")
        if brief is not None:
            seen_roles = set()
            for group, expected in (("full", ("TASK", "FILES", "BAR", "RETURN")),
                                    ("read", ("TASK", "RETURN"))):
                spec = brief.get(group)
                if not isinstance(spec, dict):
                    errors.append(f"brief.{group} must be an object")
                    continue
                for key in (("fields", "roles") if group == "full" else ("fields", "optional", "roles")):
                    value = spec.get(key)
                    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                        errors.append(f"brief.{group}.{key} must be a list of strings")
                        continue
                    if len(value) != len(set(value)):
                        errors.append(f"brief.{group}.{key} has duplicates")
                    if key == "roles":
                        for role in value:
                            if role not in types:
                                errors.append(f"brief.{group}.roles: unknown role {role!r}")
                            if role in seen_roles:
                                errors.append(f"brief role {role!r} appears in both sets")
                            seen_roles.add(role)
                    elif any(item not in BRIEF_FIELDS for item in value):
                        errors.append(f"brief.{group}.{key}: unknown field")
                if spec.get("fields") != list(expected):
                    errors.append(f"brief.{group}.fields: wrong required fields or order")
                if group == "read" and spec.get("optional") != ["FILES", "BAR"]:
                    errors.append("brief.read.optional: wrong optional fields or order")

    if context is not None:
        if "chain" in context:
            chain = context["chain"]
            if not isinstance(chain, dict):
                errors.append("context.chain must be an object")
            elif "first" in chain:
                first = chain["first"]
                if type(first) is not int or not 1 <= first <= 100000:
                    errors.append(f"context.chain.first: want an integer from 1 to 100000, got {first!r}")
        caps = context.get("caps")
        if not isinstance(caps, dict):
            errors.append("context.caps must be an object")
        else:
            for cls, cap in caps.items():
                if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
                    errors.append(f"context.caps.{cls}: cap must be a positive int, got {cap!r}")
            for cls in used:
                if cls not in caps:
                    errors.append(f"context.caps: no cap for class {cls!r} used in router.types")
        if "pressure" in context:
            pressure = context["pressure"]
            if not isinstance(pressure, dict):
                errors.append("context.pressure must be an object")
            else:
                for level, values in pressure.items():
                    if level not in ("remind", "urgent"):
                        errors.append(f"context.pressure: unknown level {level!r} (want remind, urgent)")
                        continue
                    if not isinstance(values, dict):
                        errors.append(f"context.pressure.{level} must be an object")
                        continue
                    for key, value in values.items():
                        if key not in ("chain_first", "large_read_bytes"):
                            errors.append(f"context.pressure.{level}: unknown key {key!r} (want chain_first, large_read_bytes)")
                            continue
                        chain = context.get("chain")
                        limit = (chain.get("first") if isinstance(chain, dict) else None) if key == "chain_first" else context.get("large_read_bytes")
                        if type(value) is not int or type(limit) is not int or not 1 <= value <= limit:
                            errors.append(f"context.pressure.{level}.{key}: want an integer from 1 to {limit}, got {value!r}")
        if "prompt_roles" in context:
            roles = context["prompt_roles"]
            if not isinstance(roles, dict):
                errors.append("context.prompt_roles must be an object")
            else:
                types = (router or {}).get("types", {})
                if not isinstance(types, dict):
                    types = {}
                for role, words in roles.items():
                    if role not in types:
                        errors.append(f"context.prompt_roles: unknown role {role!r}")
                    if isinstance(words, list) and all(isinstance(word, str) for word in words):
                        keywords = [(word, 1) for word in words]
                    elif isinstance(words, dict):
                        keywords = list(words.items())
                    else:
                        errors.append(f"context.prompt_roles.{role}: want a list of strings or an object of keyword to weight")
                        continue
                    for word, weight in keywords:
                        if not isinstance(word, str) or not 1 <= len(word) <= 40:
                            errors.append(f"context.prompt_roles.{role}: bad keyword {word!r}")
                        if isinstance(words, dict) and (type(weight) is not int or not 1 <= weight <= 5):
                            errors.append(f"context.prompt_roles.{role}.{word}: weight must be an integer from 1 to 5, got {weight!r}")
        for key, minimum, maximum in (("hint_min_score", 1, 10), ("hint_every", 0, 100)):
            if key in context:
                value = context[key]
                if type(value) is not int or not minimum <= value <= maximum:
                    errors.append(f"context.{key}: want an integer from {minimum} to {maximum}, got {value!r}")
    return errors


def _unlisted_errors(unlisted) -> list[str]:
    """Problems in router.unlisted: models for spawns of types the table does not list."""
    if not isinstance(unlisted, dict):
        return ["router.unlisted must be an object"]
    errors = []
    if unlisted.get("default_model") not in MODELS:
        errors.append(f"router.unlisted.default_model: bad model {unlisted.get('default_model')!r} "
                      f"(want {', '.join(MODELS)})")
    models = unlisted.get("models", {})
    if not isinstance(models, dict):
        errors.append("router.unlisted.models must be an object")
    else:
        for name, model in models.items():
            if model not in MODELS:
                errors.append(f"router.unlisted.models.{name}: bad model {model!r} (want {', '.join(MODELS)})")
    key = unlisted.get("mode_key")
    if not isinstance(key, str) or not key:
        errors.append("router.unlisted.mode_key must be a non-empty string")
    return errors


def _router(routes) -> dict:
    router = routes.get("router") if isinstance(routes, dict) else None
    return router if isinstance(router, dict) else {}


def _strings(value) -> list:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


# Hook input and session state

def read_event(stream=None) -> dict | None:
    """Parse the hook's stdin JSON (stream defaults to sys.stdin); None when empty or malformed."""
    try:
        raw = (sys.stdin if stream is None else stream).read()
    except (AttributeError, OSError, ValueError):
        return None
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def is_main(data) -> bool:
    """True for the main thread: no non-empty agent_id."""
    return not (isinstance(data, dict) and data.get("agent_id"))




def _parse_ts(value) -> float | None:
    """Epoch seconds for an ISO 8601 stamp (Z or offset; naive means UTC), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _first_timestamp(path: Path) -> float | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= TRANSCRIPT_SCAN_LINES:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    stamp = _parse_ts(row.get("timestamp"))
                    if stamp is not None:
                        return stamp
    except OSError:
        return None
    return None


def session_start(data) -> float | None:
    """Epoch seconds of the first timestamped transcript row, cached per session."""
    if not isinstance(data, dict):
        return None
    transcript = data.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return None
    cache = _state_file("sessions", data.get("session_id"), ".start")
    if cache is not None:
        try:
            return float(cache.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            pass
    start = _first_timestamp(Path(transcript).expanduser())
    if start is not None and cache is not None:
        try:
            cache.write_text(repr(start), encoding="ascii")
        except OSError:
            pass
    return start


def session_active(data, routes) -> bool:
    """Optional timestamp query; an empty activation timestamp means active."""
    after = _parse_ts(routes.get("active_after")) if isinstance(routes, dict) else None
    if after is None:
        return True
    start = session_start(data)
    return start is not None and start >= after


def effective_mode(rule_mode, data=None, routes=None, env=None) -> str:
    """Rule modes apply immediately, including sessions without a transcript."""
    return rule_mode if rule_mode in MODES else "shadow"


# Prompt parsing

def unprefixed(name):
    """<p>:<role> -> <role> for a plugin p in ROLE_PREFIXES, any case; other names unchanged."""
    plugin, sep, short = name.partition(":")
    return short if sep and short and plugin.lower() in ROLE_PREFIXES else name


def canonical_role(agent_type, routes):
    """Resolve one deprecated name, preserving explicit tiers and unknown roles."""
    name = unprefixed(agent_type.strip()) if isinstance(agent_type, str) else ""
    aliases = routes.get("aliases", {})
    return next((target for alias, target in aliases.items() if alias.lower() == name.lower()), name)


def classify(agent_type, routes) -> dict | None:
    """Look up an agent type or tier agent name, case-insensitively; None when unknown."""
    name = canonical_role(agent_type, routes)
    wanted = (name or "worker").lower()
    types = _router(routes).get("types")
    if not isinstance(types, dict):
        return None

    def find(key):
        return next((type_key for type_key in types if type_key.lower() == key), None)

    def result(type_key, tier):
        entry = types[type_key]
        if not isinstance(entry, dict):
            return None
        return {"base": type_key, "class": entry.get("class"),
                "tier": tier, "entry": entry}

    found = find(wanted)
    if found is not None:
        return result(found, None)
    tiers = routes.get("tiers")
    for seat, levels in (tiers.items() if isinstance(tiers, dict) else ()):
        for tier, spec in (levels.items() if isinstance(levels, dict) else ()):
            agent = spec.get("agent") if isinstance(spec, dict) else None
            if isinstance(agent, str) and agent.lower() == wanted:
                seat_key = find(seat.lower())
                return None if seat_key is None else result(seat_key, tier)
    return None


def route_line(prompt, routes) -> dict:
    """Parse the first `route: light|up ...` line of a prompt."""
    match = ROUTE_RE.search(prompt) if isinstance(prompt, str) else None
    if match is None:
        return {"kind": None}
    kind = match.group(1).lower()
    if kind == "light":
        return {"kind": "light", "code": None}
    words = match.group(2).split()
    word = words[0].lower() if words else ""
    codes = _strings(_router(routes).get("up_codes"))
    known = {code.lower(): code for code in codes}
    return {"kind": kind, "code": known.get(word)}


def brief_fields(prompt) -> list[tuple[str, str]]:
    """The brief's fields in order, as (NAME, text); the one parser behind every brief rule.

    A field line is an optional indent, then TASK, FILES, BAR or RETURN in capitals followed by
    a colon or whitespace, or the name in any case followed by a colon. "TASK x", "TASK: x" and
    "task: x" are fields; "Task x" is prose. Text up to the next field line belongs to the field,
    so notes may follow RETURN; text before the first field belongs to none. Repeats are kept,
    so a checker can name a duplicate. An empty text is the checker's to report.
    """
    if not isinstance(prompt, str):
        return []
    fields = []
    for line in prompt.splitlines():
        match = FIELD_LINE.match(line)
        if match and (match.group(2) == ":" or match.group(1).isupper()):
            fields.append((match.group(1).upper(), [match.group(3)]))
        elif fields:
            fields[-1][1].append(line)
    return [(name, "\n".join(lines).strip()) for name, lines in fields]


def brief_problem(prompt, role, routes) -> str | None:
    """Return the first C1 violation for a listed role, using the shared parser."""
    classified = classify(canonical_role(role, routes), routes)
    if classified is None:
        return None
    base = classified["base"]
    brief = routes.get("brief", {})
    group = next((name for name in ("full", "read") if base in brief.get(name, {}).get("roles", [])), None)
    if group is None:
        return None
    required = brief[group]["fields"]
    explanation = (f"{base} needs TASK, FILES, BAR, RETURN in that order" if group == "full" else
                   f"{base} needs TASK and RETURN; FILES and BAR are optional, in order TASK, FILES, BAR, RETURN")
    fields = brief_fields(prompt)
    names = [name for name, _ in fields]
    problem = next((f"{name} missing" for name in required if name not in names), None)
    if problem is None:
        problem = next((f"{name} appears more than once" for name in BRIEF_FIELDS if names.count(name) > 1), None)
    if problem is None:
        problem = next((f"{name} is empty" for name, value in fields if not value), None)
    if problem is None:
        ordered = sorted(names, key=BRIEF_FIELDS.index)
        problem = next((f"{name} is out of order" for name, expected in zip(names, ordered)
                        if name != expected), None)
    return f"brief: {problem} ({explanation})" if problem else None


def rerun_denied(key, session_id, verdict, ttl_hours, now=None) -> str | None:
    """Consume the first recent PASS denial for this session and key."""
    now = time.time() if now is None else now
    if (not key or not session_id or not verdict or verdict.get("verdict") != "PASS"
            or verdict.get("session") != session_hash(session_id)):
        return None
    stamp = verdict.get("ts")
    if not isinstance(stamp, (int, float)) or stamp <= now - ttl_hours * 3600:
        return None
    path = _state_file("rerun", session_id, ".json")
    if path is None:
        return None
    lock = path.with_suffix(".lock")
    with open(lock, "a", encoding="utf-8") as stream:
        os.chmod(lock, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            seen = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except (OSError, ValueError):
            seen = []
        if key in seen:
            return None
        seen.append(key)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".rerun-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(seen, output)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    when = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"already passed at {when}; change TASK or FILES to rerun"


def _first_line(text) -> str:
    """The first line with text, route directives removed and whitespace collapsed."""
    for line in ROUTE_RE.sub("", text or "").splitlines():
        if line.strip():
            return " ".join(line.split())
    return ""


def brief_hash(prompt) -> str | None:
    """16 hex: the normalized first TASK and FILES lines, or the prompt without route directives.

    Only those two lines name a brief, so BAR, RETURN, notes and route lines never reset a ladder.
    """
    if not isinstance(prompt, str):
        return None
    first = {}
    for name, text in brief_fields(prompt):
        first.setdefault(name, text)
    if "TASK" in first:
        text = json.dumps([_first_line(first["TASK"]), _first_line(first.get("FILES"))], ensure_ascii=False)
    else:
        text = " ".join(ROUTE_RE.sub("", prompt).split())
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _main_worktree(marker) -> Path | None:
    """For a linked worktree's .git file, the main repository root (gitdir, then commondir)."""
    try:
        with open(marker, "rb") as stream:
            head = stream.read(GIT_FILE_BYTES).decode("utf-8").splitlines()
        if not head or not head[0].startswith("gitdir:"):
            return None
        gitdir = Path(head[0][len("gitdir:"):].strip())
        if not gitdir.is_absolute():
            gitdir = marker.parent / gitdir
        with open(gitdir / "commondir", "rb") as stream:
            shared = stream.read(GIT_FILE_BYTES).decode("utf-8").strip()
        main = Path(os.path.realpath(gitdir / shared))
        return main.parent if shared and main.name == ".git" and main.is_dir() else None
    except (OSError, UnicodeError, ValueError):
        return None


def project_root(cwd=None) -> Path:
    """The git root at or above cwd, found without a subprocess, else cwd itself.

    A .git directory marks a root. A .git file of a linked worktree leads to its main repository,
    so a worktree keeps its project; any other .git file (a submodule) marks its own root.
    """
    try:
        start = Path(os.path.realpath(cwd if isinstance(cwd, str) and cwd else os.getcwd()))
    except (OSError, ValueError):
        start = Path(os.sep)
    for folder in (start, *start.parents):
        marker = folder / ".git"
        try:
            if marker.is_dir():
                return folder
            if marker.is_file():
                return _main_worktree(marker) or folder
        except OSError:
            continue
    return start


def project_key(cwd=None) -> str:
    """12 hex naming the project of cwd: sha256 of project_root(cwd)."""
    return hashlib.sha256(str(project_root(cwd)).encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8", "surrogatepass")).hexdigest()[:16]


def claude_key(project, brief) -> str | None:
    """Ladder key of a Claude brief in one project: "c:" + 16 hex."""
    return "c:" + _digest(project, brief) if brief else None


def session_hash(session_id) -> str | None:
    """16 hex for a host session id; the id itself is never stored."""
    if not isinstance(session_id, str) or not session_id:
        return None
    return hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def lane_label(value, routes) -> str | None:
    """A lane's label: the Agent description (Claude) or task_name (Codex), scrubbed.

    Control and format characters and newlines become spaces, whitespace collapses, a leading
    $HOME becomes ~, and at most 60 characters stay (whole characters, so valid UTF-8).
    A non-string, an empty result or router.lane_labels false gives None.
    """
    if not isinstance(value, str) or _router(routes).get("lane_labels", True) is not True:
        return None
    text = value.encode("utf-8", "replace").decode("utf-8")
    text = " ".join("".join(" " if unicodedata.category(char) in ("Cc", "Cf") else char for char in text).split())
    home = " ".join(os.environ.get("HOME", "").split()).rstrip("/")
    if home and (text == home or text.startswith(home + "/")):
        text = "~" + text[len(home):]
    return text[:LANE_LABEL_CHARS].rstrip() or None


def ladder_ttl_hours(routes) -> int:
    """router.ladder_ttl_hours: attempts older than this no longer count toward the ladder."""
    value = _router(routes).get("ladder_ttl_hours", LADDER_TTL_HOURS)
    return value if _valid_ttl(value) else LADDER_TTL_HOURS


def _valid_ttl(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 720


def risk_hits(prompt, routes) -> list[str]:
    """Names of the risk globs hit by FILES tokens and risk words found in TASK, in table order.

    Risk is about files, so a FILES line without a TASK line still reports its glob hits
    (flagging is the safe side). Risk words are only searched in a TASK line.
    """
    lines = {}
    for name, text in brief_fields(prompt):
        lines.setdefault(name, text)
    router = _router(routes)
    hits = []
    if lines.get("FILES"):
        tokens = []
        for token in TOKEN_SPLIT.split(lines["FILES"]):
            if not token:
                continue
            tokens.append(token)
            rooted = token[2:] if token.startswith(("./", "~/")) else token
            if not rooted.startswith("/"):
                tokens.append("/" + rooted)  # relative paths still hit */x/* globs
        for pattern in _strings(router.get("risk_globs")):
            if pattern not in hits and any(fnmatch.fnmatchcase(token, pattern) for token in tokens):
                hits.append(pattern)
    if lines.get("TASK"):
        for word in _strings(router.get("risk_words")):
            if word not in hits and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lines["TASK"], re.IGNORECASE):
                hits.append(word)
    return hits


# Models, agent files and the session model

def norm_model(value, never=()) -> str | None:
    """The model a spawn asks for, as one name: lower-cased and stripped; a string holding a
    excluded model name is that name; one holding opus, sonnet or haiku is
    that family. Any other string comes back lower-cased; a non-string or blank is None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    for name in never if isinstance(never, (list, tuple, set, frozenset)) else ():
        word = name.strip().lower() if isinstance(name, str) else ""
        if word and word in text:
            return word
    for family in ("opus", "sonnet", "haiku"):
        if family in text:
            return family
    return text


def top_tier_models(routes) -> set[str]:
    """Upper route models plus configured model names for other hosts."""
    models = list(_router(routes).get("top_tier_models", []))
    models.extend(levels["up"]["model"] for levels in routes.get("tiers", {}).values() if "up" in levels)
    return {name for model in models if (name := norm_model(model))}


def is_fork(agent_type) -> bool:
    """True for the fork subagent type, in any case."""
    return isinstance(agent_type, str) and agent_type.strip().lower() == "fork"


def claude_home() -> Path:
    """Where user agent and plugin definitions live."""
    return _env_path("CLAUDE_HOME", ".claude")


def plugins_root() -> Path:
    """Where Claude Code keeps plugins: CLAUDE_CODE_PLUGIN_CACHE_DIR, else claude_home()/plugins."""
    value = os.environ.get("CLAUDE_CODE_PLUGIN_CACHE_DIR")
    return Path(value).expanduser() if value else claude_home() / "plugins"


def enabled_plugins(settings) -> tuple[dict, str | None]:
    """{plugin: bool} for PLUGINS from settings' enabledPlugins ("<plugin>@router"), read only.
    An absent file or key means not enabled. Any other problem returns ({}, "<path> <error>")."""
    try:
        if not settings.exists() and not settings.is_symlink():
            return {name: False for name in PLUGINS}, None
        value = json.loads(settings.read_bytes().decode("utf-8"))
    except RecursionError:
        return {}, f"{settings} is nested too deeply"
    except (UnicodeError, ValueError):
        return {}, f"{settings} is not valid JSON"
    except OSError as exc:
        return {}, f"{settings} is not readable ({type(exc).__name__})"
    if not isinstance(value, dict):
        return {}, f"{settings} is not a JSON object"
    table = value.get("enabledPlugins", {})
    if not isinstance(table, dict):
        return {}, f"{settings} enabledPlugins is not an object"
    result = {}
    for name in PLUGINS:
        key = f"{name}@{PLUGIN_MARKETPLACE}"
        flag = table.get(key, False)
        if not isinstance(flag, bool):
            return {}, f'{settings} enabledPlugins["{key}"] is not true or false'
        result[name] = flag
    return result, None


def cached_plugins() -> tuple[dict, list[str]]:
    """{plugin: sorted cached versions} under plugins_root()/cache/router, read only; a plugin
    whose folder cannot be listed maps to None, with one "<path> <error>" text per folder."""
    folder = plugins_root() / "cache" / PLUGIN_MARKETPLACE
    found, problems = {}, []

    def versions(path):
        with os.scandir(path) as entries:
            return sorted(entry.name for entry in entries
                          if entry.is_dir(follow_symlinks=False) and PLUGIN_VERSION.fullmatch(entry.name))
    try:
        with os.scandir(folder):
            pass
    except (FileNotFoundError, NotADirectoryError):
        return {name: [] for name in PLUGINS}, problems
    except OSError as exc:
        return {name: None for name in PLUGINS}, [f"{folder} is not readable ({type(exc).__name__})"]
    for name in PLUGINS:
        try:
            found[name] = versions(folder / name)
        except (FileNotFoundError, NotADirectoryError):
            found[name] = []
        except OSError as exc:
            found[name] = None
            problems.append(f"{folder / name} is not readable ({type(exc).__name__})")
    return found, problems


def _plugin_flag(settings) -> bool | None:
    """enabledPlugins["router@router"] of one settings file: True or False, or None when the
    file or key is absent or anything about the file is malformed. Never raises."""
    try:
        value = json.loads(settings.read_bytes().decode("utf-8"))
        flag = value["enabledPlugins"][f"router@{PLUGIN_MARKETPLACE}"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None
    return flag if isinstance(flag, bool) else None


def _project_dir(folder, home) -> Path:
    """The project root for settings: CLAUDE_PROJECT_DIR when it names a directory, else the
    first of folder and its parents (PROJECT_SEARCH_LEVELS up) holding a .claude dir that is
    not either user's Claude home, else folder itself."""
    value = os.environ.get("CLAUDE_PROJECT_DIR")
    if value and Path(value).is_absolute() and os.path.isdir(value):
        return Path(value)
    current = folder
    excluded = {os.path.realpath(home), os.path.realpath(Path.home() / ".claude")}
    for _ in range(PROJECT_SEARCH_LEVELS + 1):
        if (current / ".claude").is_dir() and os.path.realpath(current / ".claude") not in excluded:
            return current
        if current.parent == current:
            break
        current = current.parent
    return folder


def plugin_tier_agent(agent, cwd=None):
    """router:<agent> only when Claude Code can resolve it: no <agent>.md in the project agents
    dirs (cwd and its parents, and the project root) or claude_home()/agents, and the router
    plugin effectively enabled (the last enabledPlugins["router@router"] in the user settings,
    then the project root's .claude/settings.json, then .claude/settings.local.json; the root
    is _project_dir). A cached copy alone is not enough. Otherwise the name unchanged. cwd is
    the payload's; None means the process cwd. Reads only; never raises."""
    if not isinstance(agent, str) or not agent or any(mark in agent for mark in ":/\\\0") or agent.startswith("."):
        return agent
    try:
        home = claude_home()
        folder = Path(cwd) if isinstance(cwd, str) and cwd.startswith("/") else Path.cwd()
        project = _project_dir(folder, home) / ".claude"
        for _ in range(PROJECT_SEARCH_LEVELS + 1):
            if (folder / ".claude" / "agents" / f"{agent}.md").is_file():
                return agent
            if folder.parent == folder:
                break
            folder = folder.parent
        if (project / "agents" / f"{agent}.md").is_file() or (home / "agents" / f"{agent}.md").is_file():
            return agent
        enabled = False
        for settings in (home / "settings.json", project / "settings.json", project / "settings.local.json"):
            flag = _plugin_flag(settings)
            enabled = enabled if flag is None else flag
        if enabled:
            return f"router:{agent}"
    except (OSError, ValueError, RuntimeError):
        pass
    return agent


def plugin_report(home, version, manifest_present) -> list[tuple[str, str]]:
    """(level, text) lines for `router doctor` and `install.sh --status`: one per Router
    plugin, then the roles doubled in home/agents and the shared-roles plugin. Read only."""
    lines = []
    enabled, problem = enabled_plugins(home / "settings.json")
    if problem:
        lines.append(("warn", problem))
    cached, problems = cached_plugins()
    lines.extend(("warn", text) for text in problems)
    for name in PLUGINS:
        state = "enabled unknown" if problem else "enabled" if enabled[name] else "not enabled"
        versions, level = cached[name], "ok"
        if versions is None:
            cache = "cached unknown"
        elif not versions:
            cache = "not cached"
        elif version in versions:
            cache = f"cached {', '.join(versions)} (current)"
        else:
            cache = f"cached {', '.join(versions)} (stale, checkout {version})"
            level = "warn" if enabled.get(name) else "ok"
        text = f"{name} {state}, {cache}"
        if name == "router" and manifest_present:
            text += "; stands down: script install present"
        lines.append((level, text))
    if enabled.get("shared-roles") or cached.get("shared-roles"):
        try:
            doubled = [name for name in SHARED_ROLE_NAMES if (home / "agents" / f"{name}.md").is_file()]
        except OSError:
            doubled = []
        if doubled:
            lines.append(("warn", f"roles in both {home / 'agents'} and shared-roles: {', '.join(doubled)} "
                                  "(doubled descriptions cost tokens every turn)"))
    return lines


def _frontmatter_model(path) -> str | None:
    """The model: value of a markdown file's leading --- block, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = handle.read(FRONTMATTER_BYTES)
    except OSError:
        return None
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "model":
            value = value.split("#", 1)[0].strip().strip("\"'").strip()
            return value or None
    return None


def _plugin_agent(short, plugin) -> str | None:
    """Breadth-first capped search of claude_home()/plugins for agents/<short>.md. For a
    plugin:name type the first file under a dir named after the plugin wins, else the first
    file of the shallowest level that has one. Hidden dirs and PLUGIN_SKIP_DIRS are not entered."""
    level, visited, fallback = [str(plugins_root())], 0, None
    for _depth in range(PLUGIN_SEARCH_DEPTH + 1):
        following = []
        for folder in level:
            visited += 1
            if visited > PLUGIN_SEARCH_DIRS:
                return fallback
            candidate = os.path.join(folder, "agents", short + ".md")
            if os.path.isfile(candidate):
                if not plugin or plugin in Path(folder).parts:
                    return candidate
                fallback = fallback or candidate
            try:
                with os.scandir(folder) as entries:
                    subs = sorted(entry.path for entry in entries if entry.is_dir(follow_symlinks=False)
                                  and not entry.name.startswith(".") and entry.name not in PLUGIN_SKIP_DIRS)
            except OSError:
                continue
            following.extend(subs)
        if fallback:
            return fallback
        level = following
    return fallback


def agent_file(agent_type, cwd=None) -> str | None:
    """The definition file of an agent type: <dir>/.claude/agents/<name>.md for cwd and its
    parents, then claude_home()/agents/<name>.md, then (a plugin:name type, or no file yet)
    a capped search of the plugin agents. None when there is none or the name is unsafe."""
    name = agent_type.strip() if isinstance(agent_type, str) else ""
    if not name or name.startswith(".") or "/" in name or "\\" in name or "\0" in name:
        return None
    if isinstance(cwd, str) and cwd.startswith("/"):
        folder = Path(cwd)
        for _ in range(PROJECT_SEARCH_LEVELS + 1):
            candidate = folder / ".claude" / "agents" / f"{name}.md"
            if candidate.is_file():
                return str(candidate)
            if folder.parent == folder:
                break
            folder = folder.parent
    user = claude_home() / "agents" / f"{name}.md"
    if user.is_file():
        return str(user)
    plugin, _, short = name.rpartition(":")
    return _plugin_agent(short, plugin) if short else None


def agent_pin(agent_type, cwd=None) -> str | None:
    """The model an agent's definition pins (its frontmatter model:), or None when it has no
    file, no model line or model: inherit."""
    path = agent_file(agent_type, cwd)
    model = _frontmatter_model(path) if path else None
    return None if model is None or model.lower() == "inherit" else model


def session_model(transcript) -> str | None:
    """message.model of the last assistant row within the last TRANSCRIPT_TAIL_BYTES of a
    transcript, as written (synthetic "<...>" models are skipped); None when there is none."""
    if not isinstance(transcript, (str, Path)) or not str(transcript):
        return None
    try:
        with Path(transcript).expanduser().open("rb") as handle:
            handle.seek(0, 2)
            start = max(0, handle.tell() - TRANSCRIPT_TAIL_BYTES)
            handle.seek(start)
            raw = handle.read(TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return None
    lines = raw.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line is cut
    for line in reversed(lines):
        if b'"assistant"' not in line or b'"model"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) and row.get("type") == "assistant" else None
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model.strip() and not model.strip().startswith("<"):
            return model.strip()
    return None


# Logging and output

def _scrub(value):
    """JSON-safe copy where every string longer than MAX_LOG_STR becomes "<len N>"."""
    if isinstance(value, str):
        return value if len(value) <= MAX_LOG_STR else f"<len {len(value)}>"
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)  # "nan", "inf", "-inf": the log stays strict JSON
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {_scrub(key if isinstance(key, str) else str(key)): _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scrub(item) for item in value]
    return _scrub(str(value))


def log(name, record) -> None:
    """Append one JSON line (ts first) to state_dir()/<name>.jsonl. Never raises."""
    try:
        entry = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        fields = _scrub(record) if isinstance(record, dict) else {"record": _scrub(record)}
        for key, value in fields.items():
            if key != "ts":
                entry[key] = value
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        path = state_dir() / f"{_safe_name(name) or 'log'}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def note_spawn(session_id) -> None:
    """Touch state_dir()/chain/<session_id>.spawn."""
    path = _state_file("chain", session_id, ".spawn")
    if path is None:
        return
    try:
        path.touch()
    except OSError:
        pass


def last_spawn(session_id) -> float | None:
    """mtime of the session's spawn marker, or None."""
    path = _state_file("chain", session_id, ".spawn")
    if path is None:
        return None
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# Read-type detection

def _strip_prefixes(command: str) -> str:
    rest = command.lstrip()
    while True:
        for pattern in (CD_PREFIX, ENV_PREFIX, TIMEOUT_PREFIX):
            match = pattern.match(rest)
            if match:
                rest = rest[match.end():].lstrip()
                break
        else:
            return rest


def _python_inline(args: str) -> bool:
    """True when python's own options run inline code: -c, a lone -, or a heredoc."""
    tokens = args.split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "-" or "<<" in token:
            return True
        if token.startswith("--"):
            continue
        if not token.startswith("-"):
            return False  # a script path or a redirect: not inline code
        for position, flag in enumerate(token[1:]):
            if flag == "c":
                return True
            if flag == "m":
                return False
            if flag in "WX":
                if position == len(token) - 2:
                    index += 1  # the option value is the next token
                break
    return False


def read_like(tool_name, tool_input) -> bool:
    """True for a read-type tool call (used to spot inline investigation chains)."""
    if tool_name in READ_TOOLS:
        return True
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command")
    if not isinstance(command, str):
        return False
    rest = _strip_prefixes(command)
    match = FIRST_WORD.match(rest)
    if match is None:
        return False
    word = match.group(0)
    if word in READ_COMMANDS:
        return True
    if word in ("python3", "python"):
        return _python_inline(rest[match.end():])
    return False


def emit(obj) -> None:
    """Print compact JSON to stdout."""
    print(json.dumps(obj, separators=(",", ":")), flush=True)


def fail_open(name, exc) -> NoReturn:
    """Log safe exception details, then allow the original call unchanged."""
    record = {"script": name, "error": type(exc).__name__}
    file = exc.file if isinstance(exc, RoutesError) else (exc.filename if isinstance(exc, OSError) else None)
    if file:
        record.update(file=str(file), message=str(exc))
    log("errors", record)
    sys.exit(0)


# Shared spawn policy and attempt state

MODE_KEYS = ("inject", "redirect", "block_model", "risk", "ladder", "brief", "rerun")


def mode_keys(routes) -> tuple:
    unlisted = routes.get("router", {}).get("unlisted", {})
    key = unlisted.get("mode_key", "inject_unlisted")
    return MODE_KEYS if key in MODE_KEYS else MODE_KEYS + (key,)


def decide_spawn(tool_input, routes, modes, prior, session_model=None, agent_pin=None, route=None,
                 tier_agent=None, cwd=None) -> dict:
    """Return allow, rewrite, or block, without reading or writing state. tier_agent maps a
    tier agent to the name to emit; the default, plugin_tier_agent, reads the Claude home and
    the project of cwd (the payload's; None means the process cwd)."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    raw_type = ti.get("subagent_type")
    asked_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else "worker"
    prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
    router = routes.get("router", {})
    model = norm_model(ti.get("model"), router.get("never", []))
    prior = prior if isinstance(prior, list) else []
    res = {"decision": "allow", "rule": "allow", "notes": [], "shadow": [],
           "updated": None, "message": None, "asked_type": asked_type, "run_type": asked_type, "run_model": model,
           "class": None, "tier": None, "route_kind": None, "route_code": None,
           "risk": [], "brief": brief_hash(prompt), "round": None}

    def mode(key):
        return effective_mode(modes.get(key) if isinstance(modes, dict) else None)

    def finish(rule, decision="allow", **fields):
        res.update(fields, rule=rule, decision=decision)
        if decision == "rewrite":
            res["run_type"] = res["updated"]["subagent_type"]
            res["run_model"] = res["updated"]["model"]
        return res

    def trip(rule, key, message):
        if mode(key) == "enforce":
            return finish(rule, "block", message=message)
        if mode(key) == "shadow":
            res["shadow"].append(rule)
        return None

    never = {name.lower() for name in router.get("never", [])}
    if model in never:
        out = trip("never-model", "block_model", "This model is excluded by the route table.")
        if out:
            return out
    if is_fork(asked_type):
        parent = norm_model(session_model, never)
        res["session_model"] = parent
        if parent in never:
            out = trip("fork-never", "block_model", "This fork would inherit an excluded model. Use a named agent.")
            if out:
                return out
        return finish("fork" if parent else "fork-unknown-model")

    c = classify(raw_type, routes)
    route = route if route is not None else route_line(prompt, routes)
    res["route_kind"], res["route_code"] = route["kind"], route.get("code")
    up = route["kind"] == "up" and bool(route.get("code"))
    if route["kind"] == "up" and not up:
        res["notes"].append("bad-up-code")
    top_model = model in top_tier_models(routes)
    if c:
        res["class"], res["tier"] = c["class"], c["tier"]
    # Judges and the enforced execution ladder select their own tier. Every
    # other role, including unlisted and untiered roles, needs a visible code.
    selects_tier = c and (c["class"] == "judge" or (c["class"] == "exec" and mode("ladder") == "enforce"))
    if not up and not selects_tier and (top_model or (c and c["tier"] == "up")):
        out = trip("up-without-code", "block_model",
                   "An up tier needs a known `route: up <code>` line. Omit the model to use the default tier.")
        if out:
            return out
    if c is None:
        unlisted = router.get("unlisted", {})
        key = unlisted.get("mode_key", "inject_unlisted")
        target = next((value for name, value in unlisted.get("models", {}).items()
                       if name.lower() == asked_type.lower()), unlisted.get("default_model"))
        if model is None and target and mode(key) != "off":
            pinned = norm_model(agent_pin(asked_type) if callable(agent_pin) else None, never)
            if pinned and pinned not in never:
                return finish("unlisted-pinned", run_model=pinned)
            if mode(key) == "enforce":
                return finish("inject-unlisted", "rewrite", updated=dict(ti, subagent_type=asked_type, model=target))
            res["shadow"].append("inject-unlisted")
        return finish("unlisted")

    cls, base, entry = c["class"], c["base"], c["entry"]
    hits = risk_hits(prompt, routes)
    res["risk"] = hits

    # The exec ladder takes precedence over early escalation or requested models.
    ladder = cls == "exec"
    rnd = len(prior) + 1 if ladder else None
    res["round"] = rnd
    if ladder:
        if rnd >= 4:
            out = trip("ladder-owner", "ladder",
                       "This brief has run three times. Take the brief back to planning with the owner.")
            if out:
                return out
        elif rnd == 3 and not (route["kind"] == "up" and route.get("code") == "ladder"):
            out = trip("ladder-needs-up", "ladder",
                       "Round 3 requires the prompt line `route: up ladder` and runs on opus.")
            if out:
                return out

    levels = routes.get("tiers", {}).get(base, {})
    if not entry.get("tiered") or not levels:
        target = "opus" if cls == "judge" or (ladder and rnd == 3 and mode("ladder") == "enforce") else entry.get("inject")
        res["tier"] = "up" if target == "opus" else "std"
        force = ladder and mode("ladder") == "enforce"
        if target and (force or ((model is None or cls == "judge") and mode("inject") == "enforce")):
            updated = dict(ti, subagent_type=asked_type, model=target)
            if cls == "judge":
                updated.pop("resume", None)
            return finish("inject", "rewrite", updated=updated)
        return finish("allow")

    if cls == "judge":
        tier = "up" if up or c["tier"] == "up" else c["tier"] or "std"
    elif ladder and mode("ladder") == "enforce":
        if rnd == 3:
            tier = "up"
        else:
            light = route["kind"] == "light" or c["tier"] == "light"
            tier = "light" if light and rnd == 1 and not hits else "std"
    elif up:
        tier = "up"
    elif c["tier"] == "up" or top_model:
        tier = c["tier"] or "std"
    elif cls == "exec":
        tier = "light" if (route["kind"] == "light" or c["tier"] == "light") and not hits else "std"
    else:
        tier = c["tier"]
        if route["kind"] == "light" and "light" in levels:
            tier = "light"
        if tier is None and model:
            tier = next((name for name in ("light", "std") if levels.get(name, {}).get("model") == model), None)
        if tier is None:
            tier = "light" if "light" in levels else "std"

    if hits and cls == "exec" and tier != "up":
        if mode("risk") == "shadow":
            res["shadow"].append("risk-would-up")
        elif mode("risk") == "enforce" and not (ladder and mode("ladder") == "enforce"):
            tier = "up"
    if tier not in levels:
        tier = "std" if "std" in levels else next(iter(levels))
    res["tier"] = tier
    spec = levels[tier]
    target_model = "opus" if cls == "judge" else spec["model"]
    if ladder and mode("ladder") == "enforce":
        target_model = "opus" if rnd == 3 else "sonnet"
    updated = dict(ti, subagent_type=(tier_agent or (lambda name: plugin_tier_agent(name, cwd)))(spec["agent"]), model=target_model)
    if base == "builder":
        updated["isolation"] = "worktree"
    if cls == "judge":
        updated.pop("resume", None)
    if updated == ti:
        return finish("tier-ok")
    # Ladder and judge requirements cannot be disabled just by disabling redirect.
    force = (ladder and mode("ladder") == "enforce") or (cls == "judge" and mode("block_model") == "enforce")
    if mode("redirect") == "enforce" or force:
        return finish("redirect", "rewrite", updated=updated)
    if mode("redirect") == "shadow":
        res["shadow"].append("redirect")
    return finish("allow")


# Host normalization. The policy above remains the only tier/ladder decision engine.

def codex_brief(task_name, classified) -> str | None:
    """16 hex for a Codex task and base role (the lane's brief); None without a task_name.

    Tier aliases share the base role, so they cannot reset a role's ladder. Like Claude brief
    identities this ignores the parent and session, so a nested spawn or a restarted session
    cannot silently gain more attempts.
    """
    if not isinstance(task_name, str) or not task_name.strip() or not classified:
        return None
    return _digest(task_name.strip(), classified["base"])


def codex_key(project, task_name, classified) -> str | None:
    """Ladder key of a Codex task and base role in one project: "x:" + 16 hex."""
    if not isinstance(task_name, str) or not task_name.strip() or not classified:
        return None
    return "x:" + _digest(project, task_name.strip(), classified["base"])


def decide_codex_spawn(tool_input, routes, modes, prior) -> dict:
    """Normalize a visible role to the shared policy and require its pinned tier.

    Only execution upper roles with an enforced ladder express its escalation
    request. Other upper roles cannot supply an up code through encrypted text.
    Brief text and risk hints are unavailable; role instructions own that lint.
    """
    ti = tool_input if isinstance(tool_input, dict) else {}
    role = ti.get("agent_type")
    c = classify(role, routes)
    brief = codex_brief(ti.get("task_name"), c)
    levels = routes.get("tiers", {}).get(c["base"], {}) if c else {}
    actual_tier = (c["tier"] or ("light" if c["class"] == "sweep" and "light" in levels else "std")) if c else None
    ladder_mode = effective_mode(modes.get("ladder"))
    ladder = bool(c and c["class"] == "exec")
    route = route_line("route: up ladder", routes) if ladder and ladder_mode == "enforce" and actual_tier == "up" else None
    normalized = {"subagent_type": role, "prompt": ""}
    if "model" in ti:
        normalized["model"] = ti["model"]
    result = decide_spawn(normalized, routes, modes, prior, route=route, tier_agent=str)
    result["brief"] = brief
    result["run_type"] = role
    result["run_model"] = None  # selected by the editable role TOML, never guessed here
    canonical = canonical_role(role, routes)
    if canonical != role and isinstance(role, str) and effective_mode(modes.get("redirect")) != "off":
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        if not (home / "agents" / (role + ".toml")).is_file():
            if effective_mode(modes.get("redirect")) == "enforce":
                result.update(decision="block", rule="codex-alias", updated=None, run_type=canonical,
                              message=f"Deprecated agent_type {role} has no installed TOML. Retry with agent_type {canonical} and the same task_name.")
                return result
            result["shadow"].append("codex-alias")
    if canonical != role and isinstance(role, str):
        result["run_type"] = canonical
    if result["decision"] == "block":
        if result["rule"] == "ladder-needs-up" and "up" in levels:
            result["message"] = ("Round 3 requires agent_type " + levels["up"]["agent"]
                                 + " with the same task_name; this is the upper-tier ladder attempt.")
        elif result["rule"] == "ladder-needs-up" and not levels:
            result["message"] = "Round 3 needs an upper-tier role, but this role has no tier TOML. Return to planning with the owner."
        elif result["rule"] == "ladder-owner":
            result["message"] = "This task/role has run three times. Return to planning with the owner."
        return result
    # Only the execution ladder needs a visible identity. This host-specific
    # requirement obeys that rule's mode, including its diagnostic-only shadow.
    if ladder and not brief:
        if ladder_mode == "enforce":
            result.update(decision="block", rule="codex-input",
                          message="Use a nonempty stable task_name for the execution ladder.")
            return result
        if ladder_mode == "shadow":
            result["shadow"].append("codex-input")
    actual = levels.get(actual_tier, {})
    required = levels.get(result["tier"], {})
    equivalent = all(actual.get(key) == required.get(key) for key in ("model", "effort"))
    if (ladder and ladder_mode == "enforce" and result["decision"] == "rewrite"
            and actual_tier != result["tier"] and not equivalent):
        target = levels[result["tier"]]["agent"]
        result.update(decision="block", rule="codex-tier",
                      message=f"Use agent_type {target} with the same task_name for round {result['round'] or 1}. "
                              "The role TOML pins the required model; execution rounds 1-2 cannot use the upper tier.")
        return result
    # Role pins win over per-call overrides. Shared allow/rewrite decisions do
    # not become extra model/input bans on this host; only an incompatible
    # enforced ladder tier needs a retry with a different pinned role.
    result.update(decision="allow", updated=None, tier=actual_tier)
    return result
