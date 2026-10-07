"""Shared helpers for standalone routing hooks. Python 3.10+, standard library only.

Paths are resolved from ROUTER_HOME, CLAUDE_HOME, ROUTES_JSON, ROUTER_STATE,
XDG_STATE_HOME, and ROUTER_OFF_FILE. ROUTER_OFF=1 disables all routing.
Importing this module does not read or create state.
"""
import fnmatch
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

ROUTES_PATH = Path(os.environ.get("ROUTES_JSON") or Path(__file__).resolve().parent / "routes.json").expanduser()

MODES = ("off", "shadow", "enforce")
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

READ_TOOLS = frozenset(("Read", "Grep", "Glob"))
READ_COMMANDS = frozenset((
    "cat", "head", "tail", "sed", "awk", "rg", "grep", "ls", "find", "fd",
    "jq", "wc", "stat", "tree", "cut", "sort", "uniq", "less", "file", "du",
))

ROUTE_RE = re.compile(r"^\s*route:\s*(light|up)\b(.*)$", re.IGNORECASE | re.MULTILINE)
BRIEF_KEYS = (("task", re.compile(r"TASK\b", re.IGNORECASE)),
              ("files", re.compile(r"FILES\b", re.IGNORECASE)))
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
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".local/state") / "claude-router"


def off_file() -> Path:
    """The single persistent switch; presence disables every router hook."""
    value = os.environ.get("ROUTER_OFF_FILE")
    return Path(value).expanduser() if value else state_path() / "OFF"


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

def load_routes(path=None) -> dict:
    """Parse and validate the routes table; ValueError names the file and the problem."""
    source = Path(os.environ.get("ROUTES_JSON") or ROUTES_PATH).expanduser() if path is None else Path(path).expanduser()
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"routes table {source} is unreadable: {exc}") from exc
    try:
        routes = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"routes table {source} is not valid JSON: {exc}") from exc
    errors = validate_routes(routes)
    if errors:
        raise ValueError(f"routes table {source} is invalid: " + "; ".join(errors))
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

    if context is not None:
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

def classify(agent_type, routes) -> dict | None:
    """Look up an agent type or tier agent name, case-insensitively; None when unknown."""
    name = agent_type.strip() if isinstance(agent_type, str) else ""
    wanted = (name or "general-purpose").lower()
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


def brief_lines(prompt) -> dict:
    """The first TASK and FILES lines (stripped, keyword included), or None for each."""
    found = {"task": None, "files": None}
    if not isinstance(prompt, str):
        return found
    for line in prompt.splitlines():
        text = line.strip()
        for key, pattern in BRIEF_KEYS:
            if found[key] is None and pattern.match(text):
                found[key] = text
        if found["task"] is not None and found["files"] is not None:
            break
    return found


def brief_hash(prompt) -> str | None:
    """Hash normalized TASK/FILES lines, or the prompt without route directives."""
    if not isinstance(prompt, str):
        return None
    lines = brief_lines(prompt)
    if lines["task"] is None:
        text = " ".join(ROUTE_RE.sub("", prompt).split())
    else:
        text = "\n".join(" ".join(pattern.sub(key.upper(), lines[key] or "", count=1).split())
                         for key, pattern in BRIEF_KEYS)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def risk_hits(prompt, routes) -> list[str]:
    """Names of the risk globs hit by FILES tokens and risk words found in TASK, in table order.

    Risk is about files, so a FILES line without a TASK line still reports its glob hits
    (flagging is the safe side). Risk words are only searched in a TASK line.
    """
    lines = brief_lines(prompt)
    router = _router(routes)
    hits = []
    if lines["files"]:
        tokens = []
        for token in TOKEN_SPLIT.split(lines["files"]):
            if not token:
                continue
            tokens.append(token)
            rooted = token[2:] if token.startswith(("./", "~/")) else token
            if not rooted.startswith("/"):
                tokens.append("/" + rooted)  # relative paths still hit */x/* globs
        for pattern in _strings(router.get("risk_globs")):
            if pattern not in hits and any(fnmatch.fnmatchcase(token, pattern) for token in tokens):
                hits.append(pattern)
    if lines["task"]:
        for word in _strings(router.get("risk_words")):
            if word not in hits and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lines["task"], re.IGNORECASE):
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
    level, visited, fallback = [str(claude_home() / "plugins")], 0, None
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
    """Log only the exception class, then allow the original call unchanged."""
    log("errors", {"script": name, "error": type(exc).__name__})
    sys.exit(0)


# Shared spawn policy and attempt state

MODE_KEYS = ("inject", "redirect", "block_model", "risk", "ladder")


def mode_keys(routes) -> tuple:
    unlisted = routes.get("router", {}).get("unlisted", {})
    key = unlisted.get("mode_key", "inject_unlisted")
    return MODE_KEYS if key in MODE_KEYS else MODE_KEYS + (key,)


def decide_spawn(tool_input, routes, modes, prior, session_model=None, agent_pin=None, route=None) -> dict:
    """Return allow, rewrite, or block, without reading or writing state."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    raw_type = ti.get("subagent_type")
    asked_type = raw_type.strip() if isinstance(raw_type, str) and raw_type.strip() else "general-purpose"
    prompt = ti.get("prompt") if isinstance(ti.get("prompt"), str) else ""
    router = routes.get("router", {})
    model = norm_model(ti.get("model"), router.get("never", []))
    prior = prior if isinstance(prior, list) else []
    res = {"decision": "allow", "rule": "allow", "notes": [], "shadow": [],
           "updated": None, "message": None, "run_type": asked_type, "run_model": model,
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
        target = "opus" if cls == "judge" else entry.get("inject")
        if target and (model is None or cls == "judge") and mode("inject") == "enforce":
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
    updated = dict(ti, subagent_type=spec["agent"], model=target_model)
    if base == "seat-exec":
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


def load_prior(brief, connection=None) -> list:
    """Accepted exec attempts for this hash; diagnostic logs are not authority."""
    if not brief:
        return []
    if connection is not None:
        rows = connection.execute("SELECT tier FROM attempts WHERE brief = ? ORDER BY round", (brief,))
        return [{"tier": row[0]} for row in rows]
    path = state_path() / "attempts.sqlite3"
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        return load_prior(brief, conn)


def record_spawn(brief, classified, choose):
    """Choose and record an accepted execution spawn in one shared transaction."""
    if not classified or classified["class"] != "exec":
        return choose([])
    with sqlite3.connect(state_dir() / "attempts.sqlite3", timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS attempts (brief TEXT, round INTEGER, tier TEXT, PRIMARY KEY (brief, round))")
        conn.execute("BEGIN IMMEDIATE")
        prior = load_prior(brief, conn)
        result = choose(prior)
        if result["decision"] != "block":
            conn.execute("INSERT INTO attempts VALUES (?, ?, ?)", (brief, len(prior) + 1, result["tier"]))
    return result


# Host normalization. The policy above remains the only tier/ladder decision engine.

def codex_task_hash(task_name, classified) -> str | None:
    """Stable task/role identity; tier aliases cannot reset a role's ladder.

    Like Claude brief identities this is independent of the parent/session, so a
    nested spawn or a restarted session cannot silently gain more attempts.
    """
    if not isinstance(task_name, str) or not task_name.strip() or not classified:
        return None
    identity = json.dumps([task_name.strip(), classified["base"]], ensure_ascii=True)
    return "codex:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def decide_codex_spawn(tool_input, routes, modes, prior) -> dict:
    """Normalize a visible role to the shared policy and require its pinned tier.

    Only execution upper roles with an enforced ladder express its escalation
    request. Other upper roles cannot supply an up code through encrypted text.
    Brief text and risk hints are unavailable; role instructions own that lint.
    """
    ti = tool_input if isinstance(tool_input, dict) else {}
    role = ti.get("agent_type")
    c = classify(role, routes)
    brief = codex_task_hash(ti.get("task_name"), c)
    levels = routes.get("tiers", {}).get(c["base"], {}) if c else {}
    actual_tier = (c["tier"] or ("light" if c["class"] == "sweep" and "light" in levels else "std")) if c else None
    ladder_mode = effective_mode(modes.get("ladder"))
    ladder = bool(c and c["class"] == "exec")
    route = route_line("route: up ladder", routes) if ladder and ladder_mode == "enforce" and actual_tier == "up" else None
    normalized = {"subagent_type": role, "prompt": ""}
    if "model" in ti:
        normalized["model"] = ti["model"]
    result = decide_spawn(normalized, routes, modes, prior, route=route)
    result["brief"] = brief
    result["run_type"] = role
    result["run_model"] = None  # selected by the editable role TOML, never guessed here
    if result["decision"] == "block":
        if result["rule"] == "ladder-needs-up" and "up" in levels:
            result["message"] = ("Round 3 requires agent_type " + levels["up"]["agent"]
                                 + " with the same task_name; this is the upper-tier ladder attempt.")
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
