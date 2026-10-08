#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${HARNESS_DIR:-}" ]]; then
  printf '%s\n' 'SKIP: set HARNESS_DIR to a Harness checkout to run the together test.'
  exit 0
fi

router_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ ! -f "$HARNESS_DIR/install.sh" ]]; then
  printf '%s\n' 'HARNESS_DIR must name a Harness checkout with install.sh.' >&2
  exit 1
fi

python3 - "$router_root" "$HARNESS_DIR" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

router, harness = map(Path, sys.argv[1:])
roles = ("sweeper", "researcher", "planner", "builder", "builder-in-place",
         "judge", "worker", "test-writer", "docs-writer")

def check(ok, message):
    if not ok:
        raise AssertionError(message)

def run(command, env, payload=None, expected=0):
    result = subprocess.run(command, env=env, input=payload, text=True,
                            capture_output=True, stdin=subprocess.DEVNULL if payload is None else None,
                            timeout=60)
    check(result.returncode == expected,
          f"{command[0]} returned {result.returncode}, expected {expected}: {result.stderr}")
    return result

for order in (("router", "harness"), ("harness", "router")):
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("ROUTER_") and key not in ("CLAUDE_HOME", "CODEX_HOME", "ROUTES_JSON")}
        env.update(HOME=str(home), CLAUDE_HOME=str(home / ".claude"),
                   CODEX_HOME=str(home / ".codex"), XDG_STATE_HOME=str(home / "state"),
                   PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull)
        original = {"unrelated_setting": "keep", "hooks": {"PreToolUse": [
            {"matcher": "Write", "hooks": [{"type": "command", "command": "true"}]}]}}
        for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
            folder = Path(env[host.upper() + "_HOME"])
            folder.mkdir()
            (folder / filename).write_text(json.dumps(original))
            (folder / "unrelated.txt").write_bytes(b"keep this file\n")

        def install(product, uninstall=False):
            source = router if product == "router" else harness
            args = ["bash", str(source / "install.sh"), "--host", "both"]
            if uninstall:
                args.append("--uninstall")
            run(args, env)

        def check_survivors(stage):
            for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
                folder = Path(env[host.upper() + "_HOME"])
                config = json.loads((folder / filename).read_text())
                check(config.get("unrelated_setting") == "keep",
                      f"setting lost for {host} {stage}")
                check(original["hooks"]["PreToolUse"][0] in config["hooks"].get("PreToolUse", []),
                      f"unrelated hook lost for {host} {stage}")
                check((folder / "unrelated.txt").read_bytes() == b"keep this file\n",
                      f"unrelated file lost for {host} {stage}")

        for product in order:
            install(product)
            check_survivors(f"after installing {product}")
        for host, extension in (("claude", ".md"), ("codex", ".toml")):
            folder = Path(env[host.upper() + "_HOME"])
            router_manifest = json.loads((folder / "router/install-manifest.json").read_text())
            harness_manifest = json.loads((folder / "harness/install-manifest.json").read_text())
            for role in roles:
                relative = f"agents/{role}{extension}"
                installed = folder / relative
                source = (router / ("agents" if host == "claude" else "codex/agents") /
                          f"{role}{extension}")
                other = (harness / ("agents" if host == "claude" else "codex/agents") /
                         f"{role}{extension}")
                check(installed.read_bytes() == source.read_bytes() == other.read_bytes(),
                      f"shared role differs: {host} {role}")
                check(relative in router_manifest["files"] and relative in harness_manifest["files"],
                      f"shared role is not claimed by both: {host} {role}")
            check(not list((folder / "agents").glob("seat-*")), f"seat files installed for {host}")

            if host == "claude":
                payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(home),
                           "tool_input": {"subagent_type": "builder", "description": "Update docs",
                                          "prompt": "TASK update docs\nFILES README.md\nBAR true\nRETURN five lines"}}
                result = run([sys.executable, str(folder / "hooks/router/spawn_guard.py")],
                             env, json.dumps(payload))
                output = json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
                check(output["subagent_type"] == "builder-std" and output["model"] == "sonnet",
                      "installed Claude Router did not select the builder tier")
            else:
                payload = {"hook_event_name": "PreToolUse", "tool_name": "collaborationspawn_agent",
                           "cwd": str(home), "tool_input": {"agent_type": "builder",
                           "task_name": "together-builder", "message": "encrypted:opaque"}}
                command = [sys.executable, str(folder / "hooks/router/codex_spawn_guard.py")]
                run(command, env, json.dumps(payload))
                run(command, env, json.dumps(payload))
                result = run(command, env, json.dumps(payload), expected=2)
                check("builder-up" in result.stderr and "3" in result.stderr,
                      "installed Codex Router did not advance the builder ladder")

        for product in reversed(order):
            install(product, uninstall=True)
            for host, extension in (("claude", ".md"), ("codex", ".toml")):
                folder = Path(env[host.upper() + "_HOME"])
                for role in roles:
                    relative = f"agents/{role}{extension}"
                    check((folder / relative).is_file() == (product == order[-1]),
                          f"shared role lifecycle failed: {host} {role}")
                filename = "settings.json" if host == "claude" else "hooks.json"
                config = json.loads((folder / filename).read_text())
                check(all(blocks for blocks in config["hooks"].values()),
                      f"empty hook event remains for {host}")
            check_survivors(f"after uninstalling {product}")
        print(f"PASS: install together in {order[0]} then {order[1]} order")

for install_order in (("router", "harness"), ("harness", "router")):
    for uninstall_order in (("router", "harness"), ("harness", "router")):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("ROUTER_") and key not in ("CLAUDE_HOME", "CODEX_HOME", "ROUTES_JSON")}
            env.update(HOME=str(home), CLAUDE_HOME=str(home / ".claude"),
                       CODEX_HOME=str(home / ".codex"), XDG_STATE_HOME=str(home / "state"),
                       PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1",
                       GIT_CONFIG_GLOBAL=os.devnull)
            for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
                folder = Path(env[host.upper() + "_HOME"])
                folder.mkdir()
                (folder / filename).write_text(json.dumps({"unrelated_setting": "keep"}))

            for product in install_order:
                source = router if product == "router" else harness
                run(["bash", str(source / "install.sh"), "--host", "both"], env)
            for product in uninstall_order:
                source = router if product == "router" else harness
                run(["bash", str(source / "install.sh"), "--host", "both", "--uninstall"], env)
            for host, filename in (("claude", "settings.json"), ("codex", "hooks.json")):
                config = json.loads((Path(env[host.upper() + "_HOME"]) / filename).read_text())
                check(config == {"unrelated_setting": "keep"},
                      f"empty hooks key remains for {host}: install {install_order}, uninstall {uninstall_order}")
            print(f"PASS: empty hooks key cleanup: install {install_order}, uninstall {uninstall_order}")
PY
