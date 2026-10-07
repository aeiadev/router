#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fresh_home="$(mktemp -d)"
trap 'rm -rf -- "$fresh_home"' EXIT
export HOME="$fresh_home"
unset CLAUDE_HOME CODEX_HOME ROUTES_JSON
while IFS= read -r variable; do
  unset "$variable"
done < <(compgen -A variable ROUTER_ || true)
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_STATE_HOME="$HOME/.local/state"
export XDG_CACHE_HOME="$HOME/.cache"
export PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null

python3 - "$source_root" <<'PY'
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(sys.argv[1])
home = Path.home()
claude = home / ".claude"
installer = root / "install.sh"


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def run(command, *, environment=None, input_text=None, expected=0):
    result = subprocess.run(command, input=input_text, text=True, capture_output=True,
                            timeout=40, cwd=root, env=environment)
    check(result.returncode == expected,
          f"{command[0]} returned {result.returncode}, expected {expected}: {result.stderr}")
    return result


def install(*options, environment=None, expected=0):
    return run(["bash", str(installer), "--host", "claude", *options], environment=environment, expected=expected)


def tree_snapshot(path):
    return {str(item.relative_to(path)): item.read_bytes()
            for item in path.rglob("*") if item.is_file()}


def hooks_for(config, script):
    return [(event, block) for event, blocks in config["hooks"].items()
            for block in blocks for hook in block.get("hooks", [])
            if script in hook.get("command", "")]


def agent_fields(path):
    text = path.read_text()
    check(text.startswith("---\n"), f"agent has no frontmatter: {path.name}")
    header = text.split("---", 2)[1]
    return {key.strip(): value.strip().strip("\"'")
            for line in header.splitlines() if ":" in line
            for key, value in [line.split(":", 1)]}


def assert_routing(folder, environment):
    event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(home),
             "tool_input": {"subagent_type": "seat-exec", "description": "Update documentation",
                            "prompt": "TASK update documentation\nFILES README.md\nBAR true\nRETURN five lines"}}
    guard = folder / "hooks/router/spawn_guard.py"
    result = run([sys.executable, str(guard)], environment=environment, input_text=json.dumps(event))
    check(not result.stderr, f"installed spawn guard wrote an error: {result.stderr}")
    output = json.loads(result.stdout)["hookSpecificOutput"]
    check(output["updatedInput"]["subagent_type"] == "seat-exec-std", "seat-exec did not select its standard tier")
    check(output["updatedInput"]["model"] == "sonnet", "seat-exec did not select sonnet")
    for model in ("gpt-6-astra", "claude-opus-5-5"):
        for directive in ("", "\nroute: up novel"):
            upper = dict(event, tool_input=dict(subagent_type="unlisted-role", model=model,
                                               prompt="Find the helper" + directive))
            result = run([sys.executable, str(guard)], environment=environment,
                         input_text=json.dumps(upper), expected=0 if directive else 2)
            if not directive:
                check("known `route: up <code>`" in result.stderr,
                      "installed guard bypassed the top-tier model check")
    cli = folder / "router/bin/router"
    status = run([str(cli), "status"], environment=environment)
    check(status.stdout.startswith("on\n"), "installed router status did not report on")
    run([str(cli), "off"], environment=environment)
    disabled = run([sys.executable, str(guard)], environment=environment, input_text=json.dumps(event))
    check(disabled.stdout == "" and disabled.stderr == "", "router off still emitted a hook override")
    # Empty hook output is the host's contract for leaving the original event unchanged.
    unchanged = event["tool_input"] if not disabled.stdout else json.loads(disabled.stdout)["hookSpecificOutput"]["updatedInput"]
    check(unchanged == event["tool_input"], "router off changed the original input")
    run([str(cli), "on"], environment=environment)


try:
    check(not (claude / "settings.json").exists(), "fresh HOME already has settings.json")
    install("--dry-run")
    check(not claude.exists(), "dry-run wrote files in the fresh HOME")
    install()
    expected_files = ["hooks/router/spawn_guard.py", "hooks/router/context_guard.py",
                      "hooks/router/common.py", "hooks/router/routes.json", "agents/seat-exec.md",
                      "agents/seat-exec-std.md", "agents/seat-judge.md", "agents/seat-sweep.md",
                      "skills/dispatch/SKILL.md", "router/bin/router", "router/bin/wait-until.sh",
                      "router/bin/cite-check.py", "router/bin/close-lane.sh", "router/bin/dispatch-log.py"]
    for relative in expected_files:
        check((claude / relative).is_file(), f"installed file missing: {relative}")
    routes = json.loads((claude / "hooks/router/routes.json").read_text())
    for base, tiers in routes["tiers"].items():
        for tier in tiers.values():
            agent = claude / "agents" / (tier["agent"] + ".md")
            check(agent.is_file(), f"missing routed agent: {tier['agent']}")
            fields = agent_fields(agent)
            check(fields.get("name") == tier["agent"], f"wrong name in {agent.name}")
            check(fields.get("model") == tier["model"], f"wrong model in {agent.name}")
            if base == "seat-exec":
                check(fields.get("isolation") == "worktree", f"missing worktree isolation in {agent.name}")
    for name, model in {"seat-sweep": "haiku", "seat-exec": "sonnet", "seat-exec-here": "sonnet",
                        "seat-judge": "opus", "Explore": "sonnet", "Plan": "sonnet", "general-purpose": "sonnet"}.items():
        agent = claude / "agents" / (name + ".md")
        check(agent.is_file(), f"missing base agent or built-in override: {name}")
        fields = agent_fields(agent)
        check(fields.get("name") == name, f"wrong base name in {agent.name}")
        check(fields.get("model") == model, f"wrong base model in {agent.name}")
        if name == "seat-exec":
            check(fields.get("isolation") == "worktree", "seat-exec lacks worktree isolation")
    settings = claude / "settings.json"
    config = json.loads(settings.read_text())
    spawn_hooks = hooks_for(config, "spawn_guard.py")
    check(len(spawn_hooks) == 1 and spawn_hooks[0][0] == "PreToolUse" and spawn_hooks[0][1]["matcher"] == "Agent|Task",
          "settings.json lacks the Agent|Task spawn guard")
    check({event for event, _ in hooks_for(config, "context_guard.py")} == {"SubagentStart", "SubagentStop", "PreToolUse"},
          "settings.json lacks one or more context guard events")
    assert_routing(claude, dict(os.environ))
    installed = tree_snapshot(claude)
    install()
    check(tree_snapshot(claude) == installed, "reinstall changed files or duplicated hooks")
    scan = run(["grep", "-RIl", "/home/", str(claude)], expected=1)
    check(not scan.stdout, "installed tree contains an absolute home path")

    user_hook = {"matcher": "Write", "hooks": [{"type": "command", "command": "printf user-hook"}]}
    config["hooks"]["PreToolUse"].insert(0, user_hook)
    config["permissions"] = {"allow": ["Read"]}
    settings.write_text(json.dumps(config))
    install()
    check(json.loads(settings.read_text()) == config, "reinstall removed user settings")
    user_file = claude / "agents/user-agent.md"
    user_file.write_text("User content\n")
    modified = claude / "agents/seat-sweep.md"
    modified.write_text(modified.read_text() + "\nLocal note.\n")
    collision = install(expected=1)
    check("refusing to overwrite" in collision.stderr, "reinstall did not explain the modified-file collision")
    before = tree_snapshot(claude)
    install("--uninstall", "--dry-run")
    check(tree_snapshot(claude) == before, "uninstall dry-run changed files")
    # Python caches: tests run with PYTHONDONTWRITEBYTECODE, so seed one in the router's hook directory and one in a user directory.
    ((claude / "hooks/router/__pycache__")).mkdir(parents=True, exist_ok=True)
    (claude / "hooks/router/__pycache__/common.cpython-312.pyc").write_bytes(b"cache")
    user_cache = claude / "user-tool/__pycache__"
    user_cache.mkdir(parents=True)
    (user_cache / "tool.cpython-312.pyc").write_bytes(b"cache")
    install("--uninstall")
    remaining = json.loads(settings.read_text())
    check(remaining["permissions"] == config["permissions"], "uninstall removed unrelated settings")
    check(remaining["hooks"]["PreToolUse"] == [user_hook], "uninstall did not preserve exactly the user hook")
    check(user_file.exists() and modified.exists(), "uninstall removed user or modified files")
    check(not (claude / "router/bin/router").exists(), "uninstall left an unchanged installed command")
    check(not (claude / "hooks/router").exists(), "uninstall left the installed router hook directory")
    check(not (claude / "hooks/router/__pycache__").exists(), "uninstall left the router's Python cache directory")
    check(user_cache.is_dir(), "uninstall removed a user Python cache directory")
    check(list(claude.glob("settings.json.router-backup-*")), "settings change did not create a backup")
    install("--uninstall")

    custom = home / "custom config's folder"
    custom.mkdir()
    original = {"permissions": {"allow": ["Read"]}, "hooks": {"PreToolUse": [user_hook]}}
    (custom / "settings.json").write_text(json.dumps(original))
    custom_env = dict(os.environ, CLAUDE_HOME=str(custom), ROUTER_STATE=str(home / "custom-state"))
    install(environment=custom_env)
    check(json.loads(next(custom.glob("settings.json.router-backup-*")).read_text()) == original,
          "install did not back up the original custom settings")
    assert_routing(custom, custom_env)
    custom_settings = json.loads((custom / "settings.json").read_text())
    command = hooks_for(custom_settings, "spawn_guard.py")[0][1]["hooks"][0]["command"]
    event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_input":
             {"subagent_type": "seat-exec", "prompt": "TASK custom path\nFILES README.md\nBAR true\nRETURN five lines"}}
    configured = run(["bash", "-c", command], environment=dict(os.environ), input_text=json.dumps(event))
    check(json.loads(configured.stdout)["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet",
          "custom hook command failed with spaces or a quote in CLAUDE_HOME")
    install("--uninstall", environment=custom_env)
    check(json.loads((custom / "settings.json").read_text()) == original, "uninstall did not restore custom settings entries")

    missing_path = home / "requirements"
    missing_path.mkdir()
    requirement_env = dict(os.environ, PATH=str(missing_path))
    shell = shutil.which("bash")
    absent_python = run([shell, str(installer)], environment=requirement_env, expected=1)
    check("missing requirement: python3" in absent_python.stderr, "missing python3 error is unclear")
    (missing_path / "python3").symlink_to(sys.executable)
    absent_git = run([shell, str(installer)], environment=requirement_env, expected=1)
    check("missing requirement: git" in absent_git.stderr, "missing git error is unclear")
except (AssertionError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
    sys.exit(f"fresh-user: FAIL: {exc}")

print("fresh-user: PASS (install, route, switch, settings, dry-run, custom paths, uninstall, requirements)")
PY
