#!/usr/bin/env bash
set -euo pipefail

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
fresh_home="$(mktemp -d)"
trap 'rm -rf -- "$fresh_home"' EXIT
export HOME="$fresh_home"
unset CLAUDE_HOME ROUTES_JSON
while IFS= read -r variable; do
  unset "$variable"
done < <(compgen -A variable ROUTER_ || true)
export CODEX_HOME="$HOME/.codex"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_STATE_HOME="$HOME/.local/state"
export XDG_CACHE_HOME="$HOME/.cache"
export PYTHONDONTWRITEBYTECODE=1
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null

python3 - "$source_root" <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path(sys.argv[1])
home = Path.home()
codex = Path(os.environ["CODEX_HOME"])


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
    return run(["bash", str(root / "install.sh"), "--host", "codex", *options],
               environment=environment, expected=expected)


def tree_snapshot(path):
    return {str(item.relative_to(path)): item.read_bytes()
            for item in path.rglob("*") if item.is_file()}


def installed_files_snapshot(path):
    return {name: data for name, data in tree_snapshot(path).items()
            if name != "router/install-manifest.json"}


def router_hook(config):
    blocks = [block for block in config["hooks"]["PreToolUse"]
              if any("codex_spawn_guard.py" in hook.get("command", "") for hook in block["hooks"])]
    check(len(blocks) == 1, "hooks.json needs exactly one Codex spawn guard")
    check(blocks[0]["matcher"] == ".*spawn_agent", "Codex guard matcher must include namespaced tools")
    return blocks[0]["hooks"][0]["command"]


def assert_routing(folder, environment):
    event = {"hook_event_name": "PreToolUse", "tool_name": "collaborationspawn_agent", "cwd": str(home),
             "session_id": "fresh-codex", "tool_input": {"agent_type": "sweeper", "task_name": "sweep_fresh",
             "message": "gAAAA-encrypted-test"}}
    config = json.loads((folder / "hooks.json").read_text())
    command = router_hook(config)
    allowed = run(["bash", "-c", command], environment=environment, input_text=json.dumps(event))
    check(not allowed.stderr, f"installed Codex guard wrote an error: {allowed.stderr}")
    output = json.loads(allowed.stdout) if allowed.stdout else {}
    check(output.get("hookSpecificOutput", {}).get("permissionDecision") != "deny", "sweep spawn was denied")
    blocked_event = dict(event, tool_input=dict(event["tool_input"], agent_type="worker-up"))
    denied = run(["bash", "-c", command], environment=environment,
                 input_text=json.dumps(blocked_event), expected=2)
    check("known `route: up <code>`" in denied.stderr, "installed guard bypassed the up-code check")
    for model in ("gpt-6-astra", "claude-opus-5-5"):
        for directive in ("", "\nroute: up novel"):
            upper = dict(event, tool_input=dict(event["tool_input"], agent_type="unlisted-role",
                                               model=model, message="encrypted:test" + directive))
            denied = run(["bash", "-c", command], environment=environment,
                         input_text=json.dumps(upper), expected=2)
            check("known `route: up <code>`" in denied.stderr,
                  "installed guard bypassed the top-tier model check")
    routes = json.loads((folder / "hooks/router/routes.json").read_text())
    routes["router"]["modes"] = dict.fromkeys(routes["router"]["modes"], "off")
    mode_path = home / "modes-off.json"
    mode_path.write_text(json.dumps(routes))
    for fields in ({"agent_type": "unknown-role"}, {}, {"agent_type": "builder", "model": "gpt-6-astra"}):
        disabled_event = dict(event, tool_input=dict(fields, task_name="mode-check", message="encrypted:test"))
        result = run(["bash", "-c", command], environment=dict(environment, ROUTES_JSON=str(mode_path)),
                     input_text=json.dumps(disabled_event))
        check(not result.stdout and not result.stderr, "installed guard ignored disabled router modes")
    mode_path.unlink()
    cli = folder / "router/bin/router"
    check(run([str(cli), "status"], environment=environment).stdout.startswith("on\n"), "Codex CLI status failed")
    run([str(cli), "off"], environment=environment)
    check((home / ".local/state/claude-router/OFF").is_file(), "Codex did not use the shared switch location")
    # A permitted sweep is silent even when routing is on. Use the denied spawn
    # to prove the OFF file actually bypasses the installed guard.
    disabled = run(["bash", "-c", command], environment=environment, input_text=json.dumps(blocked_event))
    check(not disabled.stdout and not disabled.stderr, "Codex routing was active with router off")
    run([str(cli), "on"], environment=environment)
    check(not (home / ".local/state/claude-router/OFF").exists(), "router on did not remove the shared switch")
    denied = run(["bash", "-c", command], environment=environment,
                 input_text=json.dumps(blocked_event), expected=2)
    check("known `route: up <code>`" in denied.stderr, "Codex routing did not resume after router on")
    disabled_env = dict(environment, ROUTER_OFF="1")
    disabled = run(["bash", "-c", command], environment=disabled_env, input_text=json.dumps(blocked_event))
    check(not disabled.stdout and not disabled.stderr, "Codex ignored ROUTER_OFF=1")


try:
    check(not codex.exists(), "fresh CODEX_HOME already exists")
    install("--dry-run")
    check(not codex.exists(), "dry-run wrote files in CODEX_HOME")
    installed = install()
    check("Trust" in installed.stdout and "Codex" in installed.stdout, "install omitted Codex hook trust instructions")
    check(not (home / ".claude").exists(), "Codex-only install created Claude files")
    for relative in ("hooks.json", "hooks/router/codex_spawn_guard.py", "hooks/router/common.py",
                     "hooks/router/routes.json", "skills/dispatch/SKILL.md", "router/bin/router",
                     "router/bin/close-lane.sh"):
        check((codex / relative).is_file(), f"missing installed Codex file: {relative}")
    for role, model, effort in (("sweeper", "gpt-6-luna", "low"), ("builder", "gpt-6-sol", "medium"),
                                ("builder-up", "gpt-6-astra", "high"), ("judge", "gpt-6-astra", "high")):
        contents = (codex / "agents" / (role + ".toml")).read_text()
        check(re.search(r'^model\s*=\s*"' + re.escape(model) + r'"\s*$', contents, re.M), f"missing model pin for {role}")
        check(re.search(r'^model_reasoning_effort\s*=\s*"' + effort + r'"\s*$', contents, re.M), f"missing effort pin for {role}")
        check("developer_instructions" in contents and all(field in contents for field in ("TASK", "FILES", "BAR", "RETURN")),
              f"missing brief instructions for {role}")
    assert_routing(codex, dict(os.environ))
    before = installed_files_snapshot(codex)
    install()
    check(installed_files_snapshot(codex) == before, "Codex reinstall changed files or duplicated hooks")

    hooks_file = codex / "hooks.json"
    config = json.loads(hooks_file.read_text())
    user_hook = {"matcher": "Write", "hooks": [{"type": "command", "command": "true"}]}
    config["hooks"]["PreToolUse"].insert(0, user_hook)
    config["user_setting"] = "preserve"
    hooks_file.write_text(json.dumps(config))
    install()
    check(json.loads(hooks_file.read_text()) == config, "Codex reinstall removed user configuration")
    modified = codex / "agents/sweeper.toml"
    modified.write_text(modified.read_text() + "\n# Local note.\n")
    collision = install(expected=1)
    check("refusing to overwrite" in collision.stderr, "Codex reinstall did not protect an edited role")
    before = tree_snapshot(codex)
    install("--uninstall", "--dry-run")
    check(tree_snapshot(codex) == before, "Codex uninstall dry-run changed files")
    # Python caches: tests run with PYTHONDONTWRITEBYTECODE, so seed one in the router's hook directory and one in a user directory.
    ((codex / "hooks/router/__pycache__")).mkdir(parents=True, exist_ok=True)
    (codex / "hooks/router/__pycache__/common.cpython-312.pyc").write_bytes(b"cache")
    user_cache = codex / "user-tool/__pycache__"
    user_cache.mkdir(parents=True)
    (user_cache / "tool.cpython-312.pyc").write_bytes(b"cache")
    install("--uninstall")
    remaining = json.loads(hooks_file.read_text())
    check(remaining["hooks"]["PreToolUse"] == [user_hook] and remaining["user_setting"] == "preserve",
          "Codex uninstall removed unrelated settings or hooks")
    check(modified.exists(), "Codex uninstall removed an edited role")
    check(not (codex / "router/bin/router").exists(), "Codex uninstall left an unchanged installed command")
    check(not (codex / "hooks/router").exists(), "Codex uninstall left the installed router hook directory")
    check(not (codex / "hooks/router/__pycache__").exists(), "Codex uninstall left the router's Python cache directory")
    check(user_cache.is_dir(), "Codex uninstall removed a user Python cache directory")
    check(list(codex.glob("hooks.json.router-backup-*")), "Codex hooks merge did not create a backup")
    install("--uninstall")

    custom = home / "custom config's folder"
    custom.mkdir()
    original = {"hooks": {"PreToolUse": [user_hook]}, "user_setting": "preserve"}
    (custom / "hooks.json").write_text(json.dumps(original))
    custom_env = dict(os.environ, CODEX_HOME=str(custom))
    install(environment=custom_env)
    check(json.loads(next(custom.glob("hooks.json.router-backup-*")).read_text()) == original,
          "Codex install did not back up custom hooks")
    # The configured command and CLI must work without the caller exporting the custom CODEX_HOME.
    assert_routing(custom, dict(os.environ))
    install("--uninstall", environment=custom_env)
    check(json.loads((custom / "hooks.json").read_text()) == original, "Codex custom uninstall removed user settings")

    # --purge alone removes backups and nothing else; with --uninstall it runs after the uninstall.
    purge_home = home / "purge home"
    purge_home.mkdir()
    (purge_home / "hooks.json").write_text(json.dumps(original))
    purge_env = dict(os.environ, CODEX_HOME=str(purge_home), ROUTER_STATE=str(home / "purge-state"))
    install(environment=purge_env)
    backups = list(purge_home.glob("hooks.json.router-backup-*"))
    check(len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600, "Codex install backup missing or not 0600")
    kept = {name: data for name, data in tree_snapshot(purge_home).items() if ".router-backup-" not in name}
    purged = install("--purge", environment=purge_env)
    check(f"Removed backup: {backups[0]}" in purged.stdout, "Codex --purge did not list the backup")
    check(not list(purge_home.glob("*.router-backup-*")), "Codex --purge left a backup")
    check(tree_snapshot(purge_home) == kept, "Codex --purge changed the installation")
    install("--uninstall", "--purge", environment=purge_env)
    check(not list(purge_home.glob("*.router-backup-*")), "Codex uninstall --purge left a backup")
    check(json.loads((purge_home / "hooks.json").read_text()) == original, "Codex uninstall --purge lost user settings")
except (AssertionError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
    sys.exit(f"fresh-user-codex: FAIL: {exc}")

print("fresh-user-codex: PASS (install, role pins, routing, shared switch, merge, backup, custom paths, uninstall, purge)")
PY
