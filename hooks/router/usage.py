"""Private, opt-in, stdlib-only accounting of selected host usage counters."""
import json
from compat import NOFOLLOW, owned  # identical interface in each standalone package
import os
from pathlib import Path
import re
import sqlite3
import stat


SCHEMA_VERSION = 1
FILE_NAME = "usage.sqlite3"
METRICS = ("input_tokens", "output_tokens", "cached_input_tokens",
           "cache_creation_input_tokens", "reasoning_output_tokens", "total_tokens")
EVENT_KEYS = {"event_id", "attempt_id", "stage", "outcome", "context_bytes",
              "adapter", "host", "usage", "missing", "stream_id", "sequence", "from_start"}
STAGES = {"coordinator", "worker", "judge", "observer", "hook", "restore", "continuation", "unknown"}
OUTCOMES = {"returned", "failed", "abandoned", "unknown"}
HOSTS = {"claude", "codex"}
ADAPTER_HOST = {"claude-message": "claude", "codex-turn": "codex",
                "codex-cumulative": "codex", "missing": None}
ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
MAX_COUNT = (1 << 63) - 1
MAX_JSON = 65536


class UsageError(ValueError):
    """Bounded, content-free usage validation or storage failure."""


def _fail(reason="invalid usage data"):
    raise UsageError(reason)


def _id(value, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        _fail("invalid usage identifier")
    return value


def _count(value, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        _fail("invalid usage count")
    return value


def _keys(obj, required, optional=()):
    if not isinstance(obj, dict) or not set(required) <= obj.keys() or obj.keys() - set(required) - set(optional):
        _fail("invalid usage fields")


def _bounded(value):
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_JSON:
            _fail("usage input too large")
        return encoded
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        if isinstance(exc, UsageError):
            raise
        raise UsageError("invalid usage JSON") from exc


def _declaration(value):
    _bounded(value)
    _keys(value, {"host", "expected_event_ids", "outcome"})
    if (not isinstance(value["host"], str) or value["host"] not in HOSTS
            or not isinstance(value["outcome"], str) or value["outcome"] not in OUTCOMES):
        _fail("invalid usage declaration")
    ids = value["expected_event_ids"]
    if ids is not None:
        if not isinstance(ids, list) or len(ids) > 10000:
            _fail("invalid usage inventory")
        ids = [_id(item) for item in ids]
        if len(set(ids)) != len(ids):
            _fail("invalid usage inventory")
    return {"host": value["host"], "expected_event_ids": ids, "outcome": value["outcome"]}


def _context(value, cumulative):
    _bounded(value)
    _keys(value, {"event_id", "attempt_id", "stage", "outcome"},
          {"context_bytes", "stream_id", "sequence", "from_start"})
    event_id = _id(value["event_id"])
    attempt_id = _id(value["attempt_id"], nullable=True)
    if (not isinstance(value["stage"], str) or value["stage"] not in STAGES
            or not isinstance(value["outcome"], str) or value["outcome"] not in OUTCOMES):
        _fail("invalid usage attribution")
    context_bytes = _count(value.get("context_bytes"))
    stream_id = _id(value.get("stream_id"), nullable=True)
    sequence = _count(value.get("sequence"))
    from_start = value.get("from_start", False)
    if type(from_start) is not bool:
        _fail("invalid cumulative baseline")
    if cumulative:
        if stream_id is None or sequence is None or (from_start and sequence != 0):
            _fail("invalid cumulative sequence")
    elif stream_id is not None or sequence is not None or from_start:
        _fail("invalid usage context")
    return dict(event_id=event_id, attempt_id=attempt_id, stage=value["stage"],
                outcome=value["outcome"], context_bytes=context_bytes,
                stream_id=stream_id, sequence=sequence, from_start=from_start)


def _usage(raw, adapter):
    if raw is None:
        # Distinguish absent from explicitly present null at the caller.
        return {key: None for key in METRICS}
    if not isinstance(raw, dict):
        _fail("invalid host usage")
    names = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
             "reasoning_output_tokens": "reasoning_output_tokens", "total_tokens": "total_tokens"}
    if adapter == "claude-message":
        names.update(cache_read_input_tokens="cached_input_tokens",
                     cache_creation_input_tokens="cache_creation_input_tokens")
    else:
        names.update(cached_input_tokens="cached_input_tokens",
                     cache_write_input_tokens="cache_creation_input_tokens")
    result = {key: None for key in METRICS}
    for source, target in names.items():
        if source in raw:
            result[target] = _count(raw[source], nullable=False)
    _consistent(result, ADAPTER_HOST[adapter])
    return result


def _billable(values, host):
    names = ("input_tokens", "output_tokens")
    if host == "claude":
        names += ("cached_input_tokens", "cache_creation_input_tokens")
    return names


def _consistent(values, host):
    if host == "codex":
        for part, whole in (("cached_input_tokens", "input_tokens"),
                            ("cache_creation_input_tokens", "input_tokens"),
                            ("reasoning_output_tokens", "output_tokens")):
            if values[part] is not None and values[whole] is not None and values[part] > values[whole]:
                _fail("contradictory host usage")
    components = [values[key] for key in _billable(values, host)]
    if values["total_tokens"] is not None and all(x is not None for x in components):
        if values["total_tokens"] != sum(components):
            _fail("contradictory host total")


def normalize_event(adapter, payload, context) -> dict:
    """Select numeric host counters and discard all other host payload content."""
    if not isinstance(adapter, str) or adapter not in ADAPTER_HOST:
        _fail("unknown usage adapter")
    _bounded(payload)
    c = _context(context, adapter == "codex-cumulative")
    if adapter == "missing":
        if payload != {}:
            _fail("invalid missing observation")
        values = {key: None for key in METRICS}
    else:
        if not isinstance(payload, dict):
            _fail("invalid host payload")
        if adapter == "claude-message":
            message = payload.get("message")
            if payload.get("type") != "assistant" or not isinstance(message, dict):
                _fail("invalid host payload")
            raw = message.get("usage")
            present = "usage" in message
        elif adapter == "codex-turn":
            if payload.get("type") != "turn.completed":
                _fail("invalid host payload")
            raw = payload.get("usage")
            present = "usage" in payload
        else:
            nested = payload.get("payload")
            if payload.get("type") != "event_msg" or not isinstance(nested, dict) or nested.get("type") != "token_count":
                _fail("invalid host payload")
            info = nested.get("info")
            if "info" not in nested:
                raw, present = None, False
            elif isinstance(info, dict):
                raw, present = info.get("total_token_usage"), "total_token_usage" in info
            else:
                _fail("invalid host payload")
        if present and not isinstance(raw, dict):
            _fail("invalid host usage")
        values = _usage(raw, adapter)
    return {**c, "adapter": adapter, "host": ADAPTER_HOST[adapter],
            "usage": values, "missing": {key: "not_exposed" for key in METRICS if values[key] is None}}


def _event(value):
    _bounded(value)
    _keys(value, EVENT_KEYS)
    if (not isinstance(value["adapter"], str) or value["adapter"] not in ADAPTER_HOST
            or value["host"] != ADAPTER_HOST[value["adapter"]]):
        _fail("invalid normalized adapter")
    c = _context({key: value[key] for key in ("event_id", "attempt_id", "stage", "outcome", "context_bytes", "stream_id", "sequence", "from_start")},
                 value["adapter"] == "codex-cumulative")
    _keys(value["usage"], METRICS)
    values = {key: _count(value["usage"][key]) for key in METRICS}
    if value["adapter"] == "missing" and (any(v is not None for v in values.values())
            or value["missing"] != {key: "not_exposed" for key in METRICS}):
        _fail("invalid missing observation")
    nulls = {key for key in METRICS if values[key] is None}
    if (not isinstance(value["missing"], dict) or value["missing"].keys() != nulls
            or any(not isinstance(reason, str) or reason not in {"not_exposed", "not_collected"}
                   for reason in value["missing"].values())):
        _fail("invalid missing reasons")
    if value["host"] is not None:
        _consistent(values, value["host"])
    return {**c, "adapter": value["adapter"], "host": value["host"],
            "usage": values, "missing": value["missing"].copy()}


def _storage_path(state_dir):
    if not isinstance(state_dir, Path):
        _fail("invalid usage state directory")
    return state_dir / FILE_NAME


def _lexical_parts(path):
    absolute = path if path.is_absolute() else Path.cwd() / path
    current = Path(absolute.anchor)
    yield current
    for piece in absolute.parts[1:]:
        if piece == ".":
            continue
        if piece == "..":
            current = current.parent
        else:
            current = current / piece
        yield current


def _check(path, directory=False, private=False):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        _fail("unsafe usage storage")
    if not owned(info):
        _fail("unsafe usage storage")
    if private and os.name != "nt" and info.st_mode & 0o077:
        _fail("unsafe usage storage")
    return True


def _open(state_dir, write=False, create=False):
    path = _storage_path(state_dir)
    parts = list(_lexical_parts(path))
    for ancestor in parts[:-2]:
        try:
            info = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            _fail("unsafe usage storage")
    parent = path.parent
    if not _check(parent, directory=True):
        if not create:
            return None
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _check(parent, directory=True)
    _check(parent, directory=True, private=True)
    exists = _check(path)
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if _check(sidecar):
            _check(sidecar, private=True)
    if not exists:
        if not create:
            return None
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            _check(path)
        else:
            os.close(fd)
    _check(path, private=True)
    if NOFOLLOW:
        # sqlite3 only accepts a pathname; this no-follow check narrows the
        # race before connect. Windows lacks this flag and POSIX mode checks.
        fd = os.open(path, os.O_RDONLY | NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (not owned(info)):
                _fail("unsafe usage storage")
        finally:
            os.close(fd)
    uri = path.absolute().as_uri() + ("?mode=rw" if write else "?mode=ro")
    connection = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    try:
        _check(parent, directory=True, private=True)
        _check(path, private=True)
        if write:
            connection.execute("BEGIN IMMEDIATE")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and create and write:
            connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)")
            connection.execute("PRAGMA user_version = 1")
        elif version != SCHEMA_VERSION:
            _fail("unsupported usage schema")
        return connection
    except BaseException:
        connection.close()
        raise


def _load(connection, task_id=None):
    rows = connection.execute("SELECT task_id, state_json FROM tasks" + (" WHERE task_id = ?" if task_id is not None else ""),
                              (task_id,) if task_id is not None else ()).fetchall()
    states = []
    for key, encoded in rows:
        try:
            state = json.loads(encoded)
            _id(key)
            if not isinstance(state, dict) or state.keys() != {"declaration", "events"}:
                _fail("corrupt usage storage")
            declaration = _declaration(state["declaration"])
            if not isinstance(state["events"], list):
                _fail("corrupt usage storage")
            seen = set()
            prior_events = []
            for item in state["events"]:
                if not isinstance(item, dict) or item.keys() != EVENT_KEYS | {"counted_usage", "coverage_complete"}:
                    _fail("corrupt usage storage")
                normalized = {k: item[k] for k in EVENT_KEYS}
                if normalized["adapter"] == "missing":
                    normalized["host"] = None
                checked = _event(normalized)
                if ({k: checked[k] for k in EVENT_KEYS if k != "host"} !=
                        {k: item[k] for k in EVENT_KEYS if k != "host"}
                        or item["host"] != declaration["host"] or item["event_id"] in seen):
                    _fail("corrupt usage storage")
                seen.add(item["event_id"])
                _keys(item["counted_usage"], METRICS)
                for metric in METRICS:
                    _count(item["counted_usage"][metric])
                if type(item["coverage_complete"]) is not bool:
                    _fail("corrupt usage storage")
                counted, coverage = _counted(item, prior_events, declaration["host"])
                if item["counted_usage"] != counted or item["coverage_complete"] != coverage:
                    _fail("corrupt usage storage")
                prior_events.append(item)
            states.append((key, {"declaration": declaration, "events": state["events"]}))
        except (ValueError, TypeError, RecursionError) as exc:
            raise UsageError("corrupt usage storage") from exc
    return states


def _total(values, host):
    if values["total_tokens"] is not None:
        return values["total_tokens"]
    parts = [values[key] for key in _billable(values, host)]
    return sum(parts) if all(x is not None for x in parts) else None


def _counted(event, events, host):
    counted = event["usage"].copy()
    coverage = True
    if event["adapter"] == "codex-cumulative":
        stream = [old for old in events if old["stream_id"] == event["stream_id"]]
        if stream:
            prior = stream[-1]
            if (event["sequence"] <= prior["sequence"] or event["from_start"]
                    or (event["stage"], event["attempt_id"]) != (prior["stage"], prior["attempt_id"])):
                _fail("conflicting cumulative stream")
            coverage = prior["coverage_complete"] and event["sequence"] == prior["sequence"] + 1
            counted = {}
            for key in METRICS:
                now, before = event["usage"][key], prior["usage"][key]
                if now is not None and before is not None and now < before:
                    _fail("cumulative counter reset")
                counted[key] = now - before if now is not None and before is not None else None
        else:
            coverage = event["from_start"]
            counted = event["usage"].copy() if coverage else {key: None for key in METRICS}
    _consistent(counted, host)
    return counted, coverage


def _task_report(task_id, state):
    declaration = state["declaration"]
    events = [{**item,
               "counted_missing": {key: item["missing"].get(key, "not_collected")
                                   for key in METRICS if item["counted_usage"][key] is None},
               "coverage_missing_reason": None if item["coverage_complete"] else "not_collected"}
              for item in state["events"]]
    ids = {item["event_id"] for item in events}
    expected = declaration["expected_event_ids"]
    missing_ids = sorted(set(expected) - ids) if expected is not None else []
    unexpected = sorted(ids - set(expected)) if expected is not None else []
    inventory = "unknown" if expected is None else ("complete" if not missing_ids and not unexpected else "incomplete")
    stage_counts = {stage: sum(item["stage"] == stage for item in events) for stage in sorted(STAGES)}
    outcome_counts = {outcome: sum(item["outcome"] == outcome for item in events) for outcome in sorted(OUTCOMES)}
    missing_fields = {metric: sum(item["counted_usage"][metric] is None for item in events) for metric in METRICS}
    totals = [_total(item["counted_usage"], declaration["host"]) for item in events]
    subtotals = []
    for item, total in zip(events, totals):
        if total is not None:
            subtotals.append(total)
        else:
            subtotals.extend(item["counted_usage"][key] for key in _billable(item["counted_usage"], declaration["host"])
                             if item["counted_usage"][key] is not None)
    missing_attribution = sum(item["attempt_id"] is None or item["stage"] == "unknown" for item in events)
    return {"task_id": task_id, "host": declaration["host"], "outcome": declaration["outcome"],
            "event_count": len(events), "attempt_count": len({e["attempt_id"] for e in events if e["attempt_id"] is not None}),
            "stage_counts": stage_counts, "outcome_counts": outcome_counts,
            "known_tokens": sum(subtotals) if subtotals else None,
            "tokens_complete": inventory == "complete" and bool(events) and all(x is not None for x in totals)
                               and all(e["coverage_complete"] for e in events) and missing_attribution == 0,
            "inventory_state": inventory, "missing_event_ids": missing_ids, "unexpected_event_ids": unexpected,
            "missing_fields": missing_fields, "missing_attribution_events": missing_attribution, "events": events}


def _report(states):
    tasks = [_task_report(key, state) for key, state in sorted(states)]
    by_host = {}
    for host in sorted({task["host"] for task in tasks}):
        subset = [task for task in tasks if task["host"] == host]
        known = [task["known_tokens"] for task in subset if task["known_tokens"] is not None]
        by_host[host] = {"attempted_tasks": len(subset), "event_count": sum(x["event_count"] for x in subset),
                         "attempt_count": sum(x["attempt_count"] for x in subset),
                         "known_tokens": sum(known) if known else None,
                         "complete_tasks": sum(x["tokens_complete"] for x in subset),
                         "incomplete_tasks": sum(not x["tokens_complete"] for x in subset)}
    return {"schema_version": 1, "tasks": tasks, "by_host": by_host, "attempted_tasks": len(tasks),
            "independently_accepted_tasks": 0, "acceptance_state": "unverified",
            "tokens_per_independently_accepted_task": None, "observed_cost_usd": None,
            "cost_missing_reason": "not_collected", "estimated": False}


def _execute(state_dir, task_id=None, declaration=None, event=None):
    write = declaration is not None or event is not None
    try:
        connection = _open(state_dir, write=write, create=declaration is not None)
        if connection is None:
            if event is not None:
                _fail("usage task absent")
            return _report([])
        try:
            states = _load(connection)
            index = {key: state for key, state in states}
            if declaration is not None:
                if task_id in index:
                    if index[task_id]["declaration"] != declaration:
                        _fail("conflicting usage declaration")
                else:
                    state = {"declaration": declaration, "events": []}
                    connection.execute("INSERT INTO tasks VALUES (?, ?)", (task_id, json.dumps(state, sort_keys=True)))
                    index[task_id] = state
            elif event is not None:
                if task_id not in index:
                    _fail("usage task absent")
                state = index[task_id]
                host = state["declaration"]["host"]
                if event["host"] is not None and event["host"] != host:
                    _fail("usage host mismatch")
                event = {**event, "host": host}
                events = state["events"]
                old = next((e for e in events if e["event_id"] == event["event_id"]), None)
                if old is not None:
                    if {k: old[k] for k in EVENT_KEYS} != event:
                        _fail("conflicting usage event")
                else:
                    counted, coverage = _counted(event, events, host)
                    events.append({**event, "counted_usage": counted, "coverage_complete": coverage})
                    connection.execute("UPDATE tasks SET state_json = ? WHERE task_id = ?",
                                       (json.dumps(state, sort_keys=True, separators=(",", ":")), task_id))
            if write:
                connection.execute("COMMIT")
            selected = [(task_id, index[task_id])] if task_id is not None and task_id in index else ([] if task_id is not None else list(index.items()))
            return _report(selected)
        finally:
            connection.close()
    except UsageError:
        raise
    except (sqlite3.Error, OSError, ValueError, TypeError, RecursionError) as exc:
        raise UsageError("usage storage unavailable") from exc


def declare_task(task_id, declaration, *, state_dir: Path) -> dict:
    _id(task_id)
    return _execute(state_dir, task_id, declaration=_declaration(declaration))


def record_event(task_id, event, *, state_dir: Path) -> dict:
    _id(task_id)
    return _execute(state_dir, task_id, event=_event(event))


def get_report(task_id=None, *, state_dir: Path) -> dict:
    if task_id is not None:
        _id(task_id)
    return _execute(state_dir, task_id)
