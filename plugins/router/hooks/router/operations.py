"""Pure, opt-in operations brief validation and advisory tool assessment."""
import re


class OperationsError(ValueError):
    """A bounded operations request is invalid."""


REQUIRED = ("OBJECTIVE", "TARGET", "ACTION", "SCOPE", "CONSTRAINTS", "PROOF", "READBACK", "RETURN")
OPTIONAL = ("REMOTE_IDENTITY", "IDEMPOTENCY", "ROLLBACK")
FIELDS = REQUIRED + OPTIONAL
FIELD_LINE = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_]*)([ \t]*):[ \t]*(.*)$")
URL_LINE = re.compile(r"^[ \t]*[A-Za-z][A-Za-z0-9+.-]*://\S")
BARE_FIELD_LINE = re.compile(r"^([ \t]*)([A-Z][A-Z_]*)(?:[ \t]+)(.*)$")
RESERVED = set(FIELDS) | {"TASK", "FILES", "BAR", "PROFILE"}
TOOL_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]{0,127}\Z", re.ASCII)
MAX_BYTES = 16 * 1024
MAX_FIELD = 4096
MAX_TOOLS = 128


def _text(value, label, limit=MAX_FIELD):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise OperationsError(f"{label} must be nonempty bounded text")
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        raise OperationsError(f"{label} must be UTF-8 text") from None
    return value.strip()


def parse_brief(text):
    """Validate an explicitly profiled brief; never interpret field values as commands."""
    if not isinstance(text, str):
        raise OperationsError("brief must be text")
    try:
        if len(text.encode("utf-8", "strict")) > MAX_BYTES:
            raise OperationsError("brief exceeds 16 KiB")
    except UnicodeError:
        raise OperationsError("brief must be UTF-8 text") from None
    lines = text.splitlines()
    if not lines or lines[0] != "PROFILE: operations/v1":
        raise OperationsError("operations profile must be the first line")
    values, current = {}, None
    for line in lines[1:]:
        match = FIELD_LINE.match(line)
        # URL continuations are data; reserved field names still require strict headers.
        if match and match.group(2).upper() not in RESERVED and URL_LINE.match(line):
            match = None
        if match:
            name = match.group(2).upper()
            if name in RESERVED:
                if match.group(1) or match.group(2) != name or match.group(3):
                    raise OperationsError("malformed operations field header")
                if name not in FIELDS:
                    raise OperationsError("mixed or duplicate profile field")
                if name in values:
                    raise OperationsError(f"{name} appears more than once")
                if list(FIELDS).index(name) != len(values):
                    if name in OPTIONAL and all(item in values for item in REQUIRED):
                        if any(FIELDS.index(item) > FIELDS.index(name) for item in values):
                            raise OperationsError(f"{name} is out of order")
                    else:
                        raise OperationsError(f"{name} is out of order")
                values[name], current = [match.group(4)], name
                continue
            raise OperationsError("unknown operations field")
        bare = BARE_FIELD_LINE.match(line)
        if bare and bare.group(2) in RESERVED:
            raise OperationsError("malformed operations field header")
        if current is None:
            raise OperationsError("text before operations fields")
        values[current].append(line)
    for name in REQUIRED:
        if name not in values:
            raise OperationsError(f"{name} missing")
    return {name: _text("\n".join(parts), name) for name, parts in values.items()}


def render_brief(mapping):
    if not isinstance(mapping, dict) or set(mapping) - set(FIELDS) or any(name not in mapping for name in REQUIRED):
        raise OperationsError("operations fields invalid")
    lines = ["PROFILE: operations/v1"]
    for name in FIELDS:
        if name in mapping:
            value = _text(mapping[name], name)
            lines.append(f"{name}: {value}")
    rendered = "\n".join(lines)
    parse_brief(rendered)
    return rendered


def _tools(value, label):
    if not isinstance(value, list) or len(value) > MAX_TOOLS:
        raise OperationsError(f"{label} must be a bounded list")
    if any(not isinstance(item, str) or not TOOL_NAME.fullmatch(item) for item in value):
        raise OperationsError(f"{label} contains an invalid tool name")
    if len(set(value)) != len(value):
        raise OperationsError(f"{label} contains duplicate tools")
    return value


def assess_agent(agent, required_tools, available_tools=None):
    """Report declarations only. This cannot verify host visibility or authorize an action."""
    agent = _text(agent, "agent", 128)
    if not TOOL_NAME.fullmatch(agent):
        raise OperationsError("agent name invalid")
    required = _tools(required_tools, "required_tools")
    available = None if available_tools is None else _tools(available_tools, "available_tools")
    missing = [] if available is None else [name for name in required if name not in available]
    status = "unknown" if available is None else "declared_missing" if missing else "declared_available"
    return {"agent": agent, "tool_status": status, "missing_tools": missing,
            "host_verified": False, "authorization": "unverified", "mode": "advisory"}
