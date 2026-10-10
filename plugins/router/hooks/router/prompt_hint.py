#!/usr/bin/env python3
"""Score visible prompt keywords without persisting prompt text."""
import sys
sys.dont_write_bytecode = True

import json
import os
import re
import stat
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import common
    import pressure
except (OSError, ImportError, SyntaxError) as exc:
    print(f"prompt_hint.py: cannot load the router hooks from {Path(__file__).resolve().parent}: {type(exc).__name__}",
          file=sys.stderr)
    sys.exit(1)

HINT_QUIET_AFTER_SPAWN_S = 600
READ_ONLY = {"sweeper", "researcher", "planner"}


def pattern(word):
    parts = []
    for token in word.split():
        forms = {token, token + "s", token + "es", token + "ed", token + "ing"}
        if token.endswith("e"):
            forms.update((token[:-1] + "ed", token[:-1] + "ing"))
        parts.append("(?:" + "|".join(re.escape(form) for form in sorted(forms, key=len, reverse=True)) + ")")
    return re.compile(r"(?<!\w)" + r"\s+".join(parts) + r"(?!\w)", re.IGNORECASE)


def best(prompt, context):
    choices = []
    for role, table in context.get("prompt_roles", {}).items():
        weights = {word: 1 for word in table} if isinstance(table, list) else table
        matches = [(word, weights[word], found.start()) for word in weights
                   if (found := pattern(word).search(prompt))]
        if matches:
            matches.sort(key=lambda item: item[2])
            first_word = re.match(r"\s*(\w+)", prompt)
            bonus = int(bool(first_word and matches[0][2] == first_word.start(1)))
            score = sum(weight for _, weight, _ in matches) + bonus
            choices.append((score, matches[0][2], role, [word for word, _, _ in matches]))
    if not choices:
        return None
    score, _, role, words = sorted(choices, key=lambda item: (-item[0], item[1]))[0]
    return (role, words) if score >= context.get("hint_min_score", 1) else None


def counter_path(session_id):
    digest = common.session_hash(session_id)
    return common.state_dir() / "hints" / (digest + ".json") if digest else None


def counter(path):
    if path is None or (not path.exists() and not path.is_symlink()):
        return {"count": 0, "level": None}, True
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError("invalid counter file")
            raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError("oversized counter")
            data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get("count")) is not int or not 0 <= data["count"] <= 1000000000 or data.get("level") not in (None, "ok", "remind", "urgent"):
            raise ValueError("invalid counter")
        return data, True
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        marker = path.with_suffix(".warned")
        try:
            if not marker.exists() or time.time() - marker.stat().st_mtime >= 3600:
                common.log("errors", {"script": "prompt_hint", "file": str(path), "error": type(exc).__name__})
                marker.touch()
        except OSError:
            pass
        return {"count": 0, "level": None}, False


def save_counter(path, data):
    if path is None:
        return
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = path.with_name(path.name + f".{os.getpid()}.tmp")
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
            json.dump(data, stream, separators=(",", ":"))
        os.replace(temp, path)
    except OSError:
        pass


def emit(line):
    common.emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": line}})


def main():
    if common.killed() or common.auto_mode() == "off":
        return
    data = common.read_event()
    if data is None or data.get("hook_event_name") != "UserPromptSubmit":
        return
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return
    routes = common.load_routes()
    context = routes["context"]
    session_id = data.get("session_id")
    path = counter_path(session_id)
    state, healthy = counter(path)
    if not healthy:
        return
    host = pressure.host_of(data, sys.argv)
    signal = pressure.read(session_id, host)
    original = state.copy()
    lines = []
    if healthy and signal and signal["level"] in ("remind", "urgent") and state["level"] != signal["level"]:
        lines.append(f"Router context pressure: {signal['level']} ({signal['percent']:g}%); delegate reads and sweeps to sweeper or researcher.")
    if signal:
        state["level"] = signal["level"]
    if common.auto_mode() != "suggest" or len(prompt.strip()) < 20 or prompt.lstrip().startswith("/"):
        if lines:
            emit("\n".join(lines))
        if healthy and state != original:
            save_counter(path, state)
        return
    choice = best(prompt, context)
    if context.get("hint_every", 0) and healthy:
        state["count"] += 1
    every = context.get("hint_every", 0) if healthy else 0
    allowed = not every or (state["count"] - 1) % every == 0
    spawned = common.last_spawn(session_id)
    if choice and allowed and (spawned is None or time.time() - spawned > HINT_QUIET_AFTER_SPAWN_S):
        role, words = choice
        syntax = f"spawn_agent agent_type={role}" if host == "codex" else f"Agent subagent_type={role}"
        fields = "TASK/RETURN" if role in READ_ONLY else "TASK/FILES/BAR/RETURN"
        lines.append(f"Router hint: consider {role} (matched: {', '.join(words)}); {syntax}, send {fields}.")
        common.log("hints", {"chars": len(prompt), "role": role})
    if lines:
        emit("\n".join(lines))
    if healthy and state != original:
        save_counter(path, state)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        common.fail_open("prompt_hint", exc)
