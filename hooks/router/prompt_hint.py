#!/usr/bin/env python3
"""One optional role hint from visible prompt keywords; never store prompt text."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common


def main():
    if common.killed() or common.auto_mode() != "suggest":
        return
    data = common.read_event()
    if data is None or data.get("hook_event_name") != "UserPromptSubmit":
        return
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or len(prompt.strip()) < 20 or prompt.lstrip().startswith("/"):
        return
    routes = common.load_routes()
    for role, keywords in routes["context"].get("prompt_roles", {}).items():
        if any(re.search(r"\b" + re.escape(word) + r"\b", prompt, re.IGNORECASE) for word in keywords):
            common.log("hints", {"chars": len(prompt), "role": role})
            common.emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                         "additionalContext": f"Router hint: consider {role} for this task."}})
            return


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        common.fail_open("prompt_hint", exc)
