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

# The 0.3 cases below need the released trees from old-trees.sh; they fail, never skip, without them.
for old_tree in ROUTER_V01 ROUTER_V02 HARNESS_V01 HARNESS_V02; do
  if [[ -z "${!old_tree:-}" || ! -f "${!old_tree}/install.sh" ]]; then
    printf 'FAIL: %s must name a released tree with install.sh (source old-trees.sh first).\n' "$old_tree" >&2
    exit 1
  fi
done

# The scratch tree lives here so a trap removes it on every exit path, a failed run included.
# TOGETHER_KEEP=1 keeps it for debugging (the path is printed).
TG_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/tg.XXXXXX")"
export TG_SCRATCH
cleanup() {
  if [[ "${TOGETHER_KEEP:-}" == 1 ]]; then
    printf 'kept scratch: %s\n' "$TG_SCRATCH" >&2
  else
    chmod -R u+rwX "$TG_SCRATCH" 2>/dev/null || true
    rm -rf "$TG_SCRATCH"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

python3 - "$router_root" "$HARNESS_DIR" <<'PY'
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

router, harness = map(Path, sys.argv[1:3])
FIX = router / "tests/fixtures/together"
VERSION = (router / "VERSION").read_text().strip()
SHORT = ".".join(VERSION.split(".")[:2])
SRC = {("router", "0.1"): Path(os.environ["ROUTER_V01"]), ("router", "0.2"): Path(os.environ["ROUTER_V02"]),
       ("harness", "0.1"): Path(os.environ["HARNESS_V01"]), ("harness", "0.2"): Path(os.environ["HARNESS_V02"]),
       ("router", VERSION): router, ("harness", VERSION): harness}
NAME = {"router": "Router", "harness": "Harness"}
OTHER = {"router": "harness", "harness": "router"}
HOSTS = {"claude": (".claude", "settings.json", ".md"), "codex": (".codex", "hooks.json", ".toml")}
ROLES = ("sweeper", "researcher", "planner", "builder", "builder-in-place",
         "judge", "worker", "test-writer", "docs-writer")
BACKUP = re.compile(r"\.(router|harness)-backup-")
ONLY = set(filter(None, os.environ.get("TOGETHER_ONLY", "").split(",")))
tally = collections.Counter()
scratch = Path(os.environ["TG_SCRATCH"])


def check(ok, message):
    if not ok:
        raise AssertionError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def new_home():
    home = Path(tempfile.mkdtemp(prefix="h", dir=scratch))
    shutil.copytree(FIX / "home", home, symlinks=True, dirs_exist_ok=True)
    return home


def pinned(home, **extra):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ROUTER_", "HARNESS_", "CLAUDE", "CODEX_", "XDG_")) and key != "ROUTES_JSON"}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
               CLAUDE_HOME=str(home / ".claude"), CODEX_HOME=str(home / ".codex"),
               ROUTER_STATE=str(home / ".local/state/claude-router"), ROUTER_LOCAL="off",
               PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    env.update(extra)
    return env


def tree(root):
    found = {}
    for base, folders, files in os.walk(root):
        for name in folders + files:
            path = Path(base) / name
            info = path.lstat()
            relative = str(path.relative_to(root))
            if stat.S_ISLNK(info.st_mode):
                found[relative] = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[relative] = ("dir", stat.S_IMODE(info.st_mode))
            else:
                found[relative] = ("file", sha(path.read_bytes()), stat.S_IMODE(info.st_mode))
    return found


def changes(before, after):
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def run(command, env, payload=None, expected=0, cwd=None):
    result = subprocess.run([str(part) for part in command], env=env, input=payload, text=True,
                            capture_output=True, cwd=cwd, timeout=120,
                            stdin=subprocess.DEVNULL if payload is None else None)
    ok = (expected is None or (result.returncode != 0 if expected == "nonzero" else result.returncode == expected))
    check(ok, f"{' '.join(map(str, command))[-200:]} returned {result.returncode}: "
              f"{result.stdout[-500:]} {result.stderr[-800:]}")
    return result


def install(tool, version, home, host, *extra, expected=0):
    return run(["bash", SRC[(tool, version)] / "install.sh", "--host", host, *extra], pinned(home), expected=expected)


def folders(home, host):
    return [(name, home / HOSTS[name][0]) for name in (("claude", "codex") if host == "both" else (host,))]


def manifest(folder, tool):
    return json.loads((folder / tool / "install-manifest.json").read_text())


def recorded(value):
    """A manifest file digest: Router records {"sha256": hex}, Harness a bare hex string."""
    return value.get("sha256") if isinstance(value, dict) else value


def role_source(tree_root, name, role):
    return tree_root / ("agents" if name == "claude" else "codex/agents") / f"{role}{HOSTS[name][2]}"


def held_0600(snapshot, names):
    """The documented 0.1/0.2 limit: if Harness 0.1 or 0.2 went first, the settings file keeps the
    user's bytes but stays 0600. The snapshot with each settings file at that mode."""
    held = dict(snapshot)
    for name in names:
        key = f"{HOSTS[name][0]}/{HOSTS[name][1]}"
        if key in held and held[key][0] == "file":
            held[key] = held[key][:2] + (0o600,)
    return held


def compare_pre(pre, now, home, names, label, harness_first=False):
    """Every path under each host folder (and the rest of HOME) equals the pre-0.1 snapshot: bytes,
    modes and layout, the settings file included (at 0600 when Harness 0.1 or 0.2 went first)."""
    kept = {key: value for key, value in now.items() if not BACKUP.search(key)}
    if harness_first:
        pre = held_0600(pre, names)
    for name in names:
        prefix = HOSTS[name][0] + "/"
        diff = [key for key in changes(pre, kept) if key.startswith(prefix) or key == HOSTS[name][0]]
        check(not diff, f"{label}: {name} differs from the pre-0.1 snapshot: "
                        f"{[(key, pre.get(key), kept.get(key)) for key in diff[:4]]}")
        tally["pre01", name] += 1
    rest = [key for key in changes(pre, kept) if not key.startswith((".claude", ".codex"))]
    check(not rest, f"{label}: HOME differs from the pre-0.1 snapshot outside the host folders: {rest[:6]}")


def unrelated_intact(home, label):
    """A sentinel file and a user agent under each host folder keep the fixture bytes."""
    for name, (folder, _, extension) in HOSTS.items():
        for relative in (f"{folder}/unrelated.txt", f"{folder}/agents/my-helper{extension}"):
            check((home / relative).is_file() and (home / relative).read_bytes() == (FIX / "home" / relative).read_bytes(),
                  f"{label}: unrelated file {relative} changed")


def purge_lines(home, host, tool, wanted, attempts):
    """The whole --purge output each tool prints at its checkout, line by line, in its order."""
    lines = []
    for name, folder in folders(home, host):
        mine = [f"Removed backup: {home / key}" for key in sorted(wanted)
                if key.startswith(HOSTS[name][0] + "/") and key not in attempts]
        lines += mine
        if tool == "harness":
            lines.append(f"Removed {len(mine)} backup(s) from {folder}.")
    if tool == "router":
        count = len(lines)
        lines += [f"Removed retired state: {home / key}" for key in attempts]
        lines.append(f"Purged {count} backup(s)")
    return lines


def purge_both(home, host, label, attempts):
    """--purge alone removes only that tool's backups (Router also attempts.sqlite3) and lists each."""
    for tool in ("router", "harness"):
        before = tree(home)
        result = install(tool, VERSION, home, host, "--purge")
        output = result.stdout
        after = tree(home)
        mark = f".{tool}-backup-"
        prefixes = tuple(HOSTS[name][0] + "/" for name, _ in folders(home, host))
        wanted = {key for key in before if mark in key and key.startswith(prefixes)}
        if tool == "router":
            check(all(key in before for key in attempts), f"{label}: the retired state files are not planted before --purge")
            wanted.update(attempts)
        check(wanted, f"{label}: no {tool} backups to purge")
        check(set(before) - set(after) == wanted,
              f"{label}: {tool} --purge removed {sorted(set(before) - set(after))[:4]}, expected {sorted(wanted)[:4]}")
        check(not [key for key in after if before.get(key) != after[key]], f"{label}: {tool} --purge changed another file")
        # Every printed line whole: one removal line per wanted path, the summary, and nothing else.
        lines = purge_lines(home, host, tool, wanted, attempts)
        check(output.splitlines() == lines and result.stderr == "",
              f"{label}: {tool} --purge printed {output.splitlines()[:8]} {result.stderr[-200:]}, expected {lines[:8]}")


def shipped_role(name, role):
    """The bytes of one shared role as both checkouts ship it."""
    data = role_source(router, name, role).read_bytes()
    check(role_source(harness, name, role).read_bytes() == data, f"the checkouts ship different {name} {role} bytes")
    return data


def roles_shipped(home, host, label):
    """Every shared role is at the shipped bytes on disk and in both manifests (design C)."""
    for name, folder in folders(home, host):
        for role in ROLES:
            relative = f"agents/{role}{HOSTS[name][2]}"
            digest = sha(shipped_role(name, role))
            check(sha((folder / relative).read_bytes()) == digest, f"{label}: {name} {relative} is not at the shipped bytes")
            for tool in ("router", "harness"):
                check(recorded(manifest(folder, tool)["files"].get(relative)) == digest,
                      f"{label}: {name} {tool} manifest does not record the shipped bytes of {relative}")


def no_notice(lines, label):
    check(not [line for line in lines if line.startswith("shared roles come from")],
          f"{label}: printed the skew notice: {[line for line in lines if 'shared roles' in line][:2]}")


def reruns_settle(home, host, tools, behind, label):
    """Run tools again at VERSION. When no tool ran while its sibling was older (behind None), nothing
    changes. Otherwise behind recorded that sibling's role bytes (design C keeps a stale sibling record,
    and no tool writes its sibling's manifest): this run may change only behind's own manifests, and only
    their shared-role entries that still hold the older bytes, each to the shipped bytes now on disk,
    plus installed_at; then a third run of both changes nothing, installed_at included. Afterwards every
    role is at the shipped bytes in both manifests and no run prints the skew notice."""
    before = tree(home)
    stamps = {(name, tool): manifest(folder, tool) for name, folder in folders(home, host) for tool in ("router", "harness")}
    printed = []
    for tool in tools:
        printed += install(tool, VERSION, home, host).stdout.splitlines()
    moved = changes(before, tree(home))
    if behind is None:
        check(not moved, f"{label}: a second run changed {moved[:4]}")
    else:
        own = {f"{HOSTS[name][0]}/{behind}/install-manifest.json" for name, _ in folders(home, host)}
        check(set(moved) <= own, f"{label}: a second run changed {sorted(set(moved) - own)[:4]}")
        for name, folder in folders(home, host):
            then, now = stamps[name, behind], manifest(folder, behind)
            shipped = {f"agents/{role}{HOSTS[name][2]}": sha(shipped_role(name, role)) for role in ROLES}
            stale = sorted(relative for relative, digest in shipped.items() if recorded(then["files"].get(relative)) != digest)
            refreshed = sorted(relative for relative in set(then["files"]) | set(now["files"])
                               if then["files"].get(relative) != now["files"].get(relative))
            check(refreshed == stale, f"{label}: {behind}'s second run on {name} refreshed {refreshed}, expected {stale}")
            check(all(recorded(now["files"][relative]) == shipped[relative] for relative in refreshed),
                  f"{label}: {behind}'s second run on {name} recorded other than the shipped bytes")
            others = sorted(key for key in set(then) | set(now)
                            if key not in ("files", "installed_at") and then.get(key) != now.get(key))
            check(not others, f"{label}: {behind}'s second run changed {name} manifest keys {others}")
            tally["converged", name] += bool(stale)
        settled = tree(home)
        for tool in ("router", "harness"):
            printed += install(tool, VERSION, home, host).stdout.splitlines()
        check(tree(home) == settled, f"{label}: a third run changed {changes(settled, tree(home))[:4]}")
    no_notice(printed, label)
    roles_shipped(home, host, label)


START = [()] + [((tool, version),) for tool in ("router", "harness") for version in ("0.1", "0.2")] + [
    (("router", r), ("harness", h)) for r in ("0.1", "0.2") for h in ("0.1", "0.2")]
EDITED = {"claude": "agents/Explore.md", "codex": "agents/Explore.toml"}
ATTEMPTS = ".local/state/claude-router/attempts.sqlite3"
RETIRED = {ATTEMPTS: "attempts.sqlite3", ATTEMPTS + "-wal": "attempts.sqlite3-wal"}


def upgrade_case(host, start, first):
    label = f"upgrade {host} from {'+'.join(t + ' ' + v for t, v in start) or 'clean'}, {first} first"
    for setup in (start, start[::-1]):
        home = new_home()
        pre = tree(home)
        if all(install(tool, version, home, host, expected=None).returncode == 0 for tool, version in setup):
            break
        shutil.rmtree(home)
    else:
        # The old pair never coexisted (each old installer refuses the other's roles, audit C1): start
        # from the first one installed and the second one refused with every file unchanged.
        home = new_home()
        pre = tree(home)
        install(*start[0], home, host)
        before = tree(home)
        install(*start[1], home, host, expected="nonzero")
        check(tree(home) == before, f"{label}: the refused old install changed files")
        label += f" ({start[1][0]} {start[1][1]} refused)"
        setup = start[:1]
    # The first 0.3 run sees an older sibling when that sibling's 0.1 or 0.2 is installed; the second
    # 0.3 run always sees a 0.3 sibling.
    behind = first if OTHER[first] in {tool for tool, _ in setup} else None
    harness_first = bool(setup) and setup[0][0] == "harness"
    edited = []
    if ("router", "0.1") in start:
        for name, folder in folders(home, host):
            target = folder / EDITED[name]
            check(target.is_file(), f"{label}: 0.1 did not ship {target}")
            os.chmod(target, 0o644)
            with open(target, "a", encoding="utf-8") as handle:
                handle.write("A line the user added.\n")
            edited.append(target)
    old = {(name, tool): set(manifest(folder, tool)["files"]) for name, folder in folders(home, host)
           for tool in ("router", "harness") if (folder / tool / "install-manifest.json").is_file()}
    order = (first, OTHER[first])
    output = {}
    for tool in order:
        before = tree(home)
        install(tool, VERSION, home, host, "--dry-run")
        check(tree(home) == before, f"{label}: {tool} --dry-run wrote {changes(before, tree(home))[:4]}")
        output[tool] = install(tool, VERSION, home, host).stdout.splitlines()
    for name, folder in folders(home, host):
        for tool in ("router", "harness"):
            record = manifest(folder, tool)
            check(record.get("version") == 1 and record.get("package") == VERSION,
                  f"{label}: {name} {tool} manifest is not {VERSION}: {record.get('package')}")
        claimed = set(manifest(folder, "router")["files"]) | set(manifest(folder, "harness")["files"])
        for (owner_host, tool), files in old.items():
            for relative in sorted(files - claimed) if owner_host == name else ():
                target = folder / relative
                if target in edited:
                    check(target.is_file() and f"Keeping modified old file: {target}" in output["router"],
                          f"{label}: edited old file not kept and listed: {target}")
                else:
                    check(not os.path.lexists(target), f"{label}: {tool} file no longer shipped remains: {target}")
    installed = tree(home)
    keep = scratch / ("keep-" + home.name)
    shutil.copytree(home, keep, symlinks=True)
    reruns_settle(home, host, order, behind, label)
    tally["settled", host] += 1
    for unorder in (order, order[::-1]):
        shutil.rmtree(home)
        shutil.copytree(keep, home, symlinks=True)
        # Retired 0.2 state (a database and its wal) sits in Router's state dir before the uninstall:
        # a plain uninstall keeps it, only --purge removes it.
        (home / ATTEMPTS).parent.mkdir(parents=True, exist_ok=True)
        for key, fixture in RETIRED.items():
            shutil.copyfile(FIX / fixture, home / key)
        planted = tree(home)
        # The pre-0.1 tree plus what this case planted: the two files and any state directory above them,
        # and the old file the user edited, which every uninstall keeps as the user's.
        expected = dict(pre)
        for key in planted:
            if key in RETIRED or any(retired.startswith(key + "/") for retired in RETIRED) and key not in pre:
                expected[key] = planted[key]
        for target in edited:
            expected[str(target.relative_to(home))] = planted[str(target.relative_to(home))]
        for tool in unorder:
            install(tool, VERSION, home, host, "--uninstall")
        now = tree(home)
        for key in RETIRED:
            check(now.get(key) == planted[key], f"{label}: uninstall {unorder} did not keep {key}")
        unrelated_intact(home, f"{label}, uninstall {unorder}")
        backups = {key for key in now if BACKUP.search(key)}
        check(backups >= {key for key in installed if BACKUP.search(key)}, f"{label}: a backup went before --purge")
        stray = sorted(key for key in set(now) - set(expected) - backups
                       if now[key][0] != "dir" and home / key not in edited)
        check(not stray, f"{label}: uninstall {unorder} left installed files: {stray[:6]}")
        check(not [key for key in expected if key not in now], f"{label}: uninstall {unorder} removed a user file")
        for target in edited:
            check(target.is_file(), f"{label}: uninstall removed the edited old file {target}")
        # Exact pre-0.1 bytes, modes and layout for every start state and both uninstall orders; the one
        # documented 0.1/0.2 limit is the settings file mode at 0600 when Harness 0.1 or 0.2 went first.
        names = [name for name, _ in folders(home, host)]
        compare_pre(expected, now, home, names, f"{label}, uninstall {unorder}", harness_first)
        purge_both(home, host, f"{label}, uninstall {unorder}", tuple(RETIRED))
        after = tree(home)
        check(not [key for key in RETIRED if key in after], f"{label}: --purge left the retired state files")
        unrelated_intact(home, f"{label}, purged")
        final = {k: v for k, v in (held_0600(expected, names) if harness_first else expected).items() if k not in RETIRED}
        check(after == final, f"{label}: HOME after --purge is not the pre-0.1 tree: {changes(final, after)[:6]}")
    tally["upgrade", host] += 1
    shutil.rmtree(keep)
    shutil.rmtree(home)


if not ONLY or "upgrade" in ONLY:
    started = time.monotonic()
    # The full matrix on every host mode: 9 start states, both first tools, claude, codex and both.
    plan = [(host, start, first) for host in ("both", "claude", "codex") for start in START for first in ("router", "harness")]
    expected_strict = collections.Counter()
    for host, start, first in plan:
        upgrade_case(host, start, first)
        for name in (("claude", "codex") if host == "both" else (host,)):
            expected_strict[name] += 2  # Both uninstall orders, every start state.
    for name, count in expected_strict.items():
        check(tally["pre01", name] == count,
              f"pre-0.1 comparison ran {tally['pre01', name]} times for {name}, expected {count}")
        check(tally["converged", name] > 0, f"no upgrade case refreshed a stale {name} role record")
    check(sum(tally["settled", h] for h in ("both", "claude", "codex")) == len(plan), "a rerun check was skipped")
    print(f"PASS: upgrade and uninstall matrix ({', '.join(f'{h} {tally['upgrade', h]}' for h in ('both', 'claude', 'codex'))} "
          f"cases; exact pre-0.1 tree {tally['pre01', 'claude']} claude, {tally['pre01', 'codex']} codex; "
          f"stale role records refreshed {tally['converged', 'claude']} claude, {tally['converged', 'codex']} codex; "
          f"{time.monotonic() - started:.0f}s)")


def status_lines(home, host, env=None):
    """router doctor from the checkout bin/router and from every installed Router VERSION bin/router
    (it finds the checkout through its manifest source), and Harness install.sh --status, line by line."""
    env = env or pinned(home)
    doctors = [("checkout", run([sys.executable, router / "bin/router", "doctor"], env, expected=None).stdout.splitlines())]
    for name, folder in folders(home, host):
        if (folder / "router/install-manifest.json").is_file() and manifest(folder, "router").get("package") == VERSION:
            result = run([sys.executable, folder / "router/bin/router", "doctor"], env, expected=None)
            doctors.append((f"installed {name}", result.stdout.splitlines()))
    status = run(["bash", harness / "install.sh", "--status"], env)
    return doctors, status.stdout.splitlines()


def section(lines, header):
    """The lines of one host's section of a two-host report, header excluded."""
    out, inside = [], False
    for line in lines:
        if line.startswith(("Router doctor (", "Harness status (")):
            inside = line.startswith(header)
        elif inside:
            out.append(line)
    return out


def owned_intact(home, host, tool, label):
    for name, folder in folders(home, host):
        for relative, digest in manifest(folder, tool)["files"].items():
            target = folder / relative
            if isinstance(recorded(digest), str):
                check(target.is_file() and sha(target.read_bytes()) == recorded(digest),
                      f"{label}: {tool} file changed under {name}: {relative}")


def skew_case(new, host, order):
    old = OTHER[new]
    label = f"skew {NAME[new]} {VERSION} with {NAME[old]} 0.2 on {host}, {order}"
    home = new_home()
    pre = tree(home)
    notice = f"shared roles come from {NAME[old]} 0.2.0 or earlier; upgrade it for the {SHORT} roles"
    if order == "0.2 first":
        install(old, "0.2", home, host)
        lines = install(new, VERSION, home, host).stdout.splitlines()
        check(lines.count(notice) == len(folders(home, host)), f"{label}: notice missing: {lines[:3]}")
        for name, folder in folders(home, host):
            for role in ROLES:
                relative = f"agents/{role}{HOSTS[name][2]}"
                on_disk = (folder / relative).read_bytes()
                check(on_disk == role_source(SRC[(old, "0.2")], name, role).read_bytes(),
                      f"{label}: {VERSION} rewrote the 0.2 sibling's role {relative}")
                check(recorded(manifest(folder, new)["files"].get(relative)) == sha(on_disk),
                      f"{label}: {new} did not claim {relative} with the on-disk hash")
        doctors, status = status_lines(home, host)
        check(len(doctors) == (2 if new == "router" else 1), f"{label}: ran {[kind for kind, _ in doctors]} router doctor")
        installed = {new: f"installed {VERSION}, checkout {VERSION} (current)",
                     old: f"installed 0.2.0 or earlier, checkout {VERSION}"}
        sibling = {new: f"{VERSION} installed", old: "0.2.0 or earlier installed"}
        source = {new: VERSION, old: "0.2.0 or earlier"}
        wanted_doctor = [f"[{'ok' if new == 'router' else 'warn'}] version: {installed['router']}",
                         f"[ok] sibling: Harness {sibling['harness']}",
                         f"[warn] skew: shared roles come from Harness {source['harness']}; upgrade it for the {SHORT} roles"]
        wanted_status = [f"version: {installed['harness']}", f"sibling: Router {sibling['router']}",
                         f"skew: shared roles come from Router {source['router']}; upgrade it for the {SHORT} roles"]
        for name, folder in folders(home, host):
            for kind, doctor in doctors:
                found = [line for line in section(doctor, f"Router doctor ({name})")
                         if re.fullmatch(r"\[\w+\] (version|sibling|skew): .*", line)]
                check(found == wanted_doctor, f"{label}: {kind} router doctor shows {found}")
                tally["doctor skew", kind.split()[0]] += 1
            found = [line for line in section(status, f"Harness status ({name})")
                     if re.fullmatch(r"(version|sibling|skew): .*", line)]
            check(found == wanted_status, f"{label}: install.sh --status shows {found}")
        before = tree(home)
        install(old, "0.2", home, host, expected=None)
        check(tree(home) == before, f"{label}: the 0.2 rerun changed {changes(before, tree(home))[:4]}")
        keep = scratch / ("keep-" + home.name)
        shutil.copytree(home, keep, symlinks=True)
        for unorder in ((new, old), (old, new)):
            shutil.rmtree(home)
            shutil.copytree(keep, home, symlinks=True)
            install(unorder[0], VERSION if unorder[0] == new else "0.2", home, host, "--uninstall")
            owned_intact(home, host, unorder[1], f"{label}, {unorder[0]} uninstalled first")
            install(unorder[1], VERSION if unorder[1] == new else "0.2", home, host, "--uninstall")
            now = tree(home)
            stray = sorted(key for key in set(now) - set(pre) if not BACKUP.search(key) and now[key][0] != "dir")
            check(not stray, f"{label}: uninstall {unorder} left {stray[:4]}")
            unrelated_intact(home, f"{label}, uninstall {unorder}")
            # Exact pre-0.1 bytes, modes and layout after both uninstall, in both orders (the settings file
            # at 0600 when Harness 0.2 went first, the documented 0.1/0.2 limit). When the 0.2 tool goes
            # last its own uninstaller writes the settings, so the limit promises that content in the
            # standard JSON layout, not the original bytes.
            wanted = dict(pre)
            if unorder[1] == old:
                key = f"{HOSTS[host][0]}/{HOSTS[host][1]}"
                content = json.loads((FIX / "home" / key).read_bytes())
                wanted[key] = ("file", sha((json.dumps(content, indent=2, ensure_ascii=False) + "\n").encode()), pre[key][2])
            compare_pre(wanted, now, home, [host], f"{label}, uninstall {unorder}", old == "harness")
        shutil.rmtree(home)
        shutil.copytree(keep, home, symlinks=True)
        # The 0.2 sibling upgrades: it moves the shared roles to the VERSION bytes, and new's rerun then
        # refreshes its stale record of them (design C); no run prints the notice.
        no_notice(install(old, VERSION, home, host).stdout.splitlines(), f"{label}, {old} upgraded")
        reruns_settle(home, host, (new,), new, f"{label}, {old} upgraded")
        for name, folder in folders(home, host):
            for tool in ("router", "harness"):
                check(manifest(folder, tool).get("package") == VERSION, f"{label}: {tool} not {VERSION} after the upgrade")
        doctors, status = status_lines(home, host)
        check(len(doctors) == 2, f"{label}: ran {[kind for kind, _ in doctors]} router doctor after the upgrade")
        for name, _ in folders(home, host):
            for kind, doctor in doctors:
                found = [line for line in section(doctor, f"Router doctor ({name})") if re.fullmatch(r"\[\w+\] skew: .*", line)]
                check(found == ["[ok] skew: none"], f"{label}, upgraded: {kind} router doctor shows {found}")
            found = [line for line in section(status, f"Harness status ({name})") if re.fullmatch(r"skew: .*", line)]
            check(found == ["skew: none"], f"{label}, upgraded: install.sh --status shows {found}")
        shutil.rmtree(keep)
    else:
        install(new, VERSION, home, host)
        setup = tree(home)
        refused = install(old, "0.2", home, host, expected="nonzero")
        folder = folders(home, host)[0][1]
        message = (f"harness: refusing to overwrite existing or modified file: agents/builder-in-place{HOSTS[host][2]}; "
                   "move it aside before installing" if old == "harness" else
                   f"router install: refusing to overwrite an existing or modified file: {folder}/agents/builder-in-place{HOSTS[host][2]}")
        check(refused.stderr.splitlines()[-1:] == [message], f"{label}: refusal message {refused.stderr[-300:]}")
        check(tree(home) == setup, f"{label}: the refused 0.2 run changed {changes(setup, tree(home))[:4]}")
        install(new, VERSION, home, host, "--uninstall")
        compare_pre(pre, tree(home), home, [host], f"{label}, uninstalled")
        unrelated_intact(home, f"{label}, uninstalled")
    tally["skew", host] += 1


def checkout_gone_case(host):
    """The installed doctor reads the checkout from its manifest source. Router installs from a scratch
    copy of this checkout beside Harness 0.2: the installed doctor shows the skew; with the copy gone it
    says the comparison is unknown."""
    label = f"installed doctor without its checkout on {host}"
    home = new_home()
    copy = Path(tempfile.mkdtemp(prefix="c", dir=scratch)) / "router"
    shutil.copytree(router, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    install("harness", "0.2", home, host)
    run(["bash", copy / "install.sh", "--host", host], pinned(home))
    folder = folders(home, host)[0][1]
    check(manifest(folder, "router").get("source") == str(copy), f"{label}: the manifest source is not the copy")
    for gone, wanted in ((False, f"[warn] skew: shared roles come from Harness 0.2.0 or earlier; upgrade it for the {SHORT} roles"),
                         (True, "[warn] skew: unknown (checkout not found)")):
        if gone:
            shutil.rmtree(copy.parent)
        doctor = run([sys.executable, folder / "router/bin/router", "doctor"], pinned(home), expected=None).stdout.splitlines()
        found = [line for line in section(doctor, f"Router doctor ({host})") if re.fullmatch(r"\[\w+\] skew: .*", line)]
        check(found == [wanted], f"{label}{' (copy removed)' if gone else ''}: installed router doctor shows {found}")
    tally["checkout gone", host] += 1
    shutil.rmtree(home)


if not ONLY or "skew" in ONLY:
    for new in ("router", "harness"):
        for host in ("claude", "codex"):
            for order in ("0.2 first", f"{VERSION} first"):
                skew_case(new, host, order)
    for host in ("claude", "codex"):
        checkout_gone_case(host)
    check(tally["doctor skew", "installed"] == 2, f"the installed doctor showed the skew {tally['doctor skew', 'installed']} times, expected 2")
    print(f"PASS: version skew ({tally['skew', 'claude']} claude, {tally['skew', 'codex']} codex cases; installed doctor "
          f"skew {tally['doctor skew', 'installed']}, checkout gone {tally['checkout gone', 'claude']} claude, "
          f"{tally['checkout gone', 'codex']} codex)")


def hook_commands(folder, name, needle):
    """Installed hook commands from settings.json or hooks.json whose text names a script."""
    config = json.loads((folder / HOSTS[name][1]).read_text())
    found = []
    for event, blocks in config.get("hooks", {}).items():
        for block in blocks:
            for hook in block.get("hooks", []):
                if needle in hook.get("command", ""):
                    found.append((event, block.get("matcher"), hook["command"]))
    check(found, f"no installed hook names {needle} under {folder}")
    return found


def hook(command, env, payload, cwd):
    return run(["sh", "-c", command], env, json.dumps(payload), cwd=cwd, expected=None)


def injected(result):
    """The text a hook adds to the context: additionalContext from JSON output, else stdout."""
    check(result.returncode == 0 and "Traceback" not in result.stderr, f"hook failed: {result.stderr[-300:]}")
    text = result.stdout
    try:
        value = json.loads(text)
        text = value.get("hookSpecificOutput", {}).get("additionalContext", "") or value.get("systemMessage", "")
    except ValueError:
        pass
    return text


def project(home, env):
    folder = home / "work/project"
    shutil.copytree(FIX / "project", folder)
    git = ["git", "-c", "user.name=t", "-c", "user.email=fixture", "-c", "init.defaultBranch=main"]
    run(git + ["init", "-q", folder], env)
    run(git + ["-C", folder, "add", "-A"], env)
    run(git + ["-C", folder, "commit", "-qm", "fixture"], env)
    with open(folder / "notes.txt", "a", encoding="utf-8") as handle:
        handle.write("dirty\n")
    head = run(["git", "-C", folder, "rev-parse", "HEAD"], env).stdout.strip()
    return folder, head


def spawn_lanes(folder, name, env, cwd, session, count=3):
    command = hook_commands(folder, name, "spawn_guard.py")[0][2]
    labels = []
    for index in range(count):
        if name == "claude":
            label = f"Refresh the docs lane {index}"
            payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": session, "cwd": str(cwd),
                       "tool_input": {"subagent_type": "builder", "description": label,
                                      "prompt": f"TASK update docs part {index}\nFILES README.md\nBAR true\nRETURN five lines"}}
        else:
            label = f"refresh-docs-lane-{index}"
            payload = {"hook_event_name": "PreToolUse", "tool_name": "collaborationspawn_agent", "session_id": session,
                       "turn_id": f"turn-{index}", "cwd": str(cwd),
                       "tool_input": {"agent_type": "builder", "task_name": label, "message": "encrypted:opaque"}}
        result = hook(command, env, payload, cwd)
        check(result.returncode == 0, f"spawn hook denied lane {index}: {result.stderr[-300:]}")
        labels.append(label)
    return labels


def restore_outputs(folder, name, env, cwd, session):
    event = {"hook_event_name": "SessionStart", "source": "compact", "session_id": session, "cwd": str(cwd),
             "transcript_path": str(cwd / "missing-transcript.jsonl")}
    if name == "codex":
        event["model"] = "gpt-test"
    out = {}
    for key, needle in (("router", "session_note.py"), ("harness", "dispatch.py"),
                        ("context", "inject-project-context.sh")):
        # The project-context injection runs on SessionStart (Codex) or the next prompt (Claude).
        commands = [(event_name, command) for event_name, matcher, command in hook_commands(folder, name, needle)
                    if "--reset" not in command and (key != "harness" or " restore" in command)
                    and (key == "context" or event_name == "SessionStart" and re.fullmatch(matcher or ".*", "compact"))]
        check(len(commands) == 1 or key == "context" and commands,
              f"{name}: expected one SessionStart compact command for {needle}: {commands}")
        payload = dict(event, hook_event_name=commands[0][0], prompt="continue")
        out[key] = injected(hook(commands[0][1], env, payload, cwd))
    return out


def shared_lines(first, second):
    lines = {line.strip() for line in first.splitlines() if len(line.strip()) >= 20}
    return sorted(lines & {line.strip() for line in second.splitlines()})


def restore_case(name, order):
    label = f"restore {name}, {order[0]} first"
    home = new_home()
    env = pinned(home)
    for tool in order:
        install(tool, VERSION, home, name)
    folder = home / HOSTS[name][0]
    cwd, head = project(home, env)
    session = f"restore-{name}-{order[0]}"
    labels = spawn_lanes(folder, name, env, cwd, session)
    state_lines = [line for line in (cwd / "STATE.md").read_text().splitlines() if len(line.strip()) >= 20]
    for restore_bytes in (None, 1000, 4000, 10, 100000):
        settings = cwd / HOSTS[name][0] / "checkpoint.json"
        if restore_bytes is not None:
            settings.parent.mkdir(exist_ok=True)
            settings.write_text(json.dumps({"restore_bytes": restore_bytes}))
        cap = 4000 if restore_bytes is None else max(1000, min(4000, restore_bytes))
        out = restore_outputs(folder, name, env, cwd, session)
        sizes = {key: len(text.encode()) for key, text in out.items()}
        case = f"{label}, restore_bytes {restore_bytes}"
        check(0 < sizes["router"] <= 800, f"{case}: Router note is {sizes['router']} bytes")
        check(0 < sizes["harness"] <= cap, f"{case}: Harness restore is {sizes['harness']} bytes, cap {cap}")
        check(sizes["router"] + sizes["harness"] <= 4800, f"{case}: restore channel {sizes}")
        check(sizes["context"] <= 4000 and sum(sizes.values()) <= 8800, f"{case}: with project context {sizes}")
        check(any(lab in out["router"] for lab in labels), f"{case}: Router note names no lane: {out['router'][:200]}")
        check(not any(lab in out["harness"] for lab in labels) and not re.search(r"\b(PASS|SEND_BACK|verdict)\b", out["harness"]),
              f"{case}: Harness printed a lane or verdict")
        check(not [line for line in state_lines if line.strip() in out["router"]] and "STATE.md" not in out["router"]
              and head[:7] not in out["router"], f"{case}: Router printed STATE.md text or git facts")
        duplicated = shared_lines(out["router"], out["harness"])
        check(not duplicated, f"{case}: a line appears in both outputs: {duplicated[:2]}")
        tally["restore", name] += 1
    quiet = restore_outputs(folder, name, env, cwd, session + "-without-lanes")
    check(quiet["router"] == "", f"{label}: Router printed with no lanes: {quiet['router'][:120]}")
    shutil.rmtree(home)


if not ONLY or "restore" in ONLY:
    for name in ("claude", "codex"):
        for order in (("router", "harness"), ("harness", "router")):
            restore_case(name, order)
    print(f"PASS: restore caps, no shared lines ({tally['restore', 'claude']} claude, {tally['restore', 'codex']} codex cases)")


def pressure_file(home, session, base=None):
    return (base or home / ".local/state") / "claude-harness/pressure" / (sha(session.encode())[:16] + ".json")


def reads_until_note(folder, name, env, cwd, session, turn_id=None, limit=6):
    """Consecutive Read calls through Router's installed context hook; returns (count, note text)."""
    command = hook_commands(folder, name, "context_guard.py")[0][2]
    for count in range(1, limit + 1):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Read", "session_id": session, "cwd": str(cwd),
                   "tool_input": {"file_path": str(cwd / "notes.txt")}}
        if turn_id if turn_id is not None else name == "codex":
            payload["turn_id"] = f"turn-{count}"
        result = hook(command, env, payload, cwd)
        check(result.returncode == 0 and "Traceback" not in result.stderr, f"context hook failed: {result.stderr[-300:]}")
        if result.stdout.strip():
            return count, injected(result)
    return None, ""


def write_pressure(folder, name, env, cwd, session, used, window):
    if name == "claude":
        command = json.loads((folder / "settings.json").read_text())["statusLine"]["command"]
        payload = {"session_id": session, "cwd": str(cwd), "transcript_path": str(cwd / "none.jsonl"),
                   "model": {"id": "claude-test", "display_name": "Test"},
                   "context_window": {"context_window_size": window, "current_usage": {
                       "input_tokens": used, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}
    else:
        command = [c for e, m, c in hook_commands(folder, name, "dispatch.py") if " check" in c][0]
        rollout = cwd / f"rollout-{session}.jsonl"
        rollout.write_text(json.dumps({"type": "turn_context", "payload": {"model_context_window": window}}) + "\n" +
                           json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                               "last_token_usage": {"input_tokens": used}}}}) + "\n")
        payload = {"hook_event_name": "PostToolUse", "session_id": session, "turn_id": "turn-0", "cwd": str(cwd),
                   "transcript_path": str(rollout), "tool_name": "shell", "tool_input": {"command": "true"}}
    result = hook(command, env, payload, cwd)
    check(result.returncode == 0 and "Traceback" not in result.stderr, f"{name} pressure writer failed: {result.stderr[-300:]}")


def pressure_case(name):
    home = new_home()
    env = pinned(home, ROUTER_LOCAL=str(FIX / "routes.local.json"))
    for tool in ("harness", "router"):
        install(tool, VERSION, home, name)
    folder = home / HOSTS[name][0]
    cwd, _ = project(home, env)
    window = 200000
    first = {"ok": 5, "remind": 3, "urgent": 2}
    for percent, level in ((50, "ok"), (65, "remind"), (75, "remind"), (85, "urgent"), (95, "urgent")):
        session = f"pressure-{name}-{percent}"
        write_pressure(folder, name, env, cwd, session, window * percent // 100, window)
        path = pressure_file(home, session)
        check(path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600, f"{name} {percent}%: pressure file {path} missing or not 0600")
        check(stat.S_IMODE(path.parent.stat().st_mode) == 0o700, f"{name}: pressure directory is not 0700")
        record = json.loads(path.read_text())
        check(record.get("schema") == 1 and record.get("host") == name and record.get("level") == level and
              abs(record.get("percent") - percent) < 0.01 and record.get("window") == window,
              f"{name} {percent}%: wrote {record}")
        count, note = reads_until_note(folder, name, env, cwd, session)
        check(count == first[level], f"{name} {percent}%: note after {count} reads, expected {first[level]}")
        lead = f"Context is at {level} ({record['percent']:g}%)"
        check((lead in note) == (level != "ok"), f"{name} {percent}%: note does not name {level} {percent}%: {note[-160:]}")
        tally["pressure", name] += 1
    # Never loosens: a local chain limit of 2 stays 2 at remind (whose own limit is 3).
    tight = pinned(home, ROUTER_LOCAL=str(FIX / "routes.tight.local.json"))
    session = f"pressure-{name}-tight"
    write_pressure(folder, name, tight, cwd, session, window * 70 // 100, window)
    check(reads_until_note(folder, name, tight, cwd, session)[0] == 2, f"{name}: remind loosened a tighter local limit")
    # Carryover 3R9: the Router reads host codex if and only if the event carries turn_id.
    session = f"pressure-{name}-host"
    write_pressure(folder, name, env, cwd, session, window * 90 // 100, window)
    shutil.copyfile(pressure_file(home, session), pressure_file(home, session + "-b"))
    with_turn = reads_until_note(folder, name, env, cwd, session, turn_id=True)[0]
    without_turn = reads_until_note(folder, name, env, cwd, session + "-b", turn_id=False)[0]
    check((with_turn, without_turn) == ((5, 2) if name == "claude" else (2, 5)),
          f"{name}: turn_id host rule gave {with_turn} with and {without_turn} without turn_id")
    # Path rule: XDG_STATE_HOME when absolute, else ~/.local/state. The writer and the reader both follow it.
    elsewhere = Path(tempfile.mkdtemp(prefix="x", dir=scratch))
    check(home not in elsewhere.parents, "the second state home must not sit under HOME")
    moved = pinned(home, ROUTER_LOCAL=str(FIX / "routes.local.json"), XDG_STATE_HOME=str(elsewhere))
    session = f"pressure-{name}-xdg"
    write_pressure(folder, name, moved, cwd, session, window * 90 // 100, window)
    landed = pressure_file(home, session, elsewhere)
    check(landed.is_file() and stat.S_IMODE(landed.stat().st_mode) == 0o600 and not pressure_file(home, session).exists(),
          f"{name}: with XDG_STATE_HOME set the pressure file is not only at {landed}")
    count, note = reads_until_note(folder, name, moved, cwd, session)
    # The whole note line Router prints, copied from a real run.
    wanted = ("Context note (delegation guard): 2 consecutive single read-type tool calls in the main loop with no "
              "spawn in between. Delegate a multi-step investigation to sweeper or researcher to keep detailed "
              "results out of the main context. Context is at urgent (90%): delegate reads and sweeps.")
    check(count == 2 and note == wanted,
          f"{name}: the reader missed the file under XDG_STATE_HOME ({count}): {note!r}")
    tally["pressure", name] += 1
    relative = pinned(home, ROUTER_LOCAL=str(FIX / "routes.local.json"), XDG_STATE_HOME="relative-state")
    session = f"pressure-{name}-relative"
    write_pressure(folder, name, relative, cwd, session, window * 90 // 100, window)
    check(pressure_file(home, session).is_file() and not (cwd / "relative-state").exists(),
          f"{name}: a relative XDG_STATE_HOME did not fall back to ~/.local/state")
    check(reads_until_note(folder, name, relative, cwd, session)[0] == 2, f"{name}: the reader missed the fallback file")
    tally["pressure", name] += 1
    # Absent, stale, malformed and unreadable files: normal output, a logged error naming the file for the
    # last two, never a traceback.
    sample = json.loads((FIX / "pressure-sample.json").read_text())
    sample["host"] = name
    for kind in ("absent", "stale", "malformed", "unreadable"):
        session = f"pressure-{name}-{kind}"
        path = pressure_file(home, session)
        for marker in (home / ".local/state/claude-router").glob("pressure-warned"):
            marker.unlink()
        if kind == "stale":
            path.write_text(json.dumps(sample))
        elif kind == "malformed":
            shutil.copyfile(FIX / "pressure-malformed.json", path)
        elif kind == "unreadable":
            path.write_text(json.dumps(dict(sample, ts=time.time())))
            os.chmod(path, 0)
        logs_before = {p: p.read_bytes() for p in (home / ".local/state/claude-router").rglob("*") if p.is_file()}
        count, note = reads_until_note(folder, name, env, cwd, session)
        check(count == 5 and "Context is at" not in note, f"{name} {kind}: Router output changed ({count})")
        logged = b"".join(p.read_bytes()[len(logs_before.get(p, b"")):] for p in (home / ".local/state/claude-router").rglob("*")
                          if p.is_file() and p.suffix in (".jsonl", ".log"))
        check((str(path).encode() in logged) == (kind in ("malformed", "unreadable")),
              f"{name} {kind}: logged error naming the file is {'missing' if kind in ('malformed', 'unreadable') else 'unexpected'}")
        if kind == "unreadable":
            os.chmod(path, 0o600)
    shutil.rmtree(home)


if not ONLY or "pressure" in ONLY:
    for name in ("claude", "codex"):
        pressure_case(name)
    print(f"PASS: pressure round trip ({tally['pressure', 'claude']} claude, {tally['pressure', 'codex']} codex levels)")


def version_lines(lines, header, doctor):
    pattern = r"\[\w+\] (version|sibling|skew): .*" if doctor else r"(version|sibling|skew): .*"
    return [line for line in section(lines, header) if re.fullmatch(pattern, line)]


def versions_case():
    home = new_home()
    for tool in ("router", "harness"):
        install(tool, VERSION, home, "both")
    # PYTHONDONTWRITEBYTECODE is unset for these runs, so the check below is live: no bytecode and no
    # other write from the checkout doctor, Harness --status, or the installed bin/router doctor and
    # status of each host (each avoids bytecode itself).
    env = {key: value for key, value in pinned(home).items() if key != "PYTHONDONTWRITEBYTECODE"}
    before = tree(home)
    base = run([sys.executable, router / "bin/router", "doctor"], env, expected=None)
    status = run(["bash", harness / "install.sh", "--status"], env)
    installed_doctors = []
    for name in ("claude", "codex"):
        command = home / HOSTS[name][0] / "router/bin/router"
        installed_doctors.append(run([sys.executable, command, "doctor"], env, expected=None))
        run([sys.executable, command, "status"], env)
    check(tree(home) == before and not [key for key in tree(home) if "__pycache__" in key],
          f"versions: a status command wrote {changes(before, tree(home))[:4]}")
    for name in ("claude", "codex"):
        wanted = [f"[ok] version: installed {VERSION}, checkout {VERSION} (current)",
                  f"[ok] sibling: Harness {VERSION} installed", "[ok] skew: none"]
        for result in (base, *installed_doctors):
            found = version_lines(result.stdout.splitlines(), f"Router doctor ({name})", True)
            check(found == wanted, f"versions {name}: router doctor shows {found}")
        found = version_lines(status.stdout.splitlines(), f"Harness status ({name})", False)
        check(found == [f"version: installed {VERSION}, checkout {VERSION} (current)",
                        f"sibling: Router {VERSION} installed", "skew: none"], f"versions {name}: --status shows {found}")
        tally["versions", name] += 1
    saved_all = {}
    for tool in ("router", "harness"):
        for kind, text in (("missing", "0.2.0 or earlier"), ("unreadable", "unknown")):
            for name in ("claude", "codex"):
                path = home / HOSTS[name][0] / tool / "install-manifest.json"
                saved_all[tool, name] = path.read_bytes()
                saved = saved_all[tool, name]
                if kind == "missing":
                    value = json.loads(saved)
                    value.pop("package")
                    path.write_text(json.dumps(value))
                else:
                    path.write_text("{not json")
            doctor = run([sys.executable, router / "bin/router", "doctor"], env, expected=None)
            # Doctor's own rule still decides the exit code: 1 only when a check fails, and it finishes.
            fails = any(line.startswith("[FAIL]") for line in doctor.stdout.splitlines())
            check(doctor.returncode == (1 if fails else 0) and re.search(r"^doctor: \d+ ok", doctor.stdout, re.M),
                  f"versions: {kind} {tool} manifest broke doctor's exit code rule: {doctor.returncode} {doctor.stderr[-200:]}")
            status = run(["bash", harness / "install.sh", "--status"], env)
            for name in ("claude", "codex"):
                found = (version_lines(doctor.stdout.splitlines(), f"Router doctor ({name})", True) +
                         version_lines(status.stdout.splitlines(), f"Harness status ({name})", False))
                wanted = ([f"[warn] version: installed {text}, checkout {VERSION}", f"sibling: Router {text} installed"
                           if kind == "missing" else "sibling: Router unknown"] if tool == "router" else
                          [f"[{'ok' if kind == 'missing' else 'warn'}] sibling: Harness {text}" +
                           (" installed" if kind == "missing" else ""), f"version: installed {text}, checkout {VERSION}"])
                check(all(line in found for line in wanted), f"versions {name}: {kind} {tool} manifest shows {found}")
            for name in ("claude", "codex"):
                (home / HOSTS[name][0] / tool / "install-manifest.json").write_bytes(saved_all[tool, name])
    shutil.rmtree(home)


def plugin_case():
    home = new_home()
    install("router", VERSION, home, "claude")
    folder = home / ".claude"
    copy = home / "plugin-cache/router"
    shutil.copytree(router / "plugins/router", copy, symlinks=True)
    env = pinned(home, CLAUDE_PLUGIN_ROOT=str(copy))
    cwd = home / "work"
    cwd.mkdir()
    commands = [(event, command) for event, blocks in json.loads((copy / "hooks/hooks.json").read_text())["hooks"].items()
                for block in blocks for hook_entry in block["hooks"] for command in [hook_entry["command"]]]
    check(commands, "plugin: no generated hook commands")
    spawn = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "plugin", "cwd": str(cwd),
             "tool_input": {"subagent_type": "builder", "description": "Plugin check",
                            "prompt": "TASK update docs\nFILES README.md\nRETURN five lines"}}
    def payload(event):
        if event == "PreToolUse":
            return spawn
        return {"hook_event_name": event, "session_id": "plugin", "cwd": str(cwd), "source": "compact",
                "prompt": "continue", "stop_hook_active": False}
    before = tree(home)
    for event, command in commands:
        result = hook(command, env, payload(event), cwd)
        check(result.returncode == 0 and result.stdout == "" and result.stderr == "",
              f"plugin: {event} hook did not stand down: {result.returncode} {result.stdout[:120]} {result.stderr[:120]}")
    check(tree(home) == before, f"plugin: a stood-down hook wrote {changes(before, tree(home))[:4]}")
    doctor = run([sys.executable, router / "bin/router", "doctor"], env, expected=None).stdout.splitlines()
    check("[ok] plugin: router not enabled, not cached; stands down: script install present" in doctor,
          f"plugin: doctor lines {[line for line in doctor if 'plugin' in line]}")
    script = [c for e, m, c in hook_commands(folder, "claude", "spawn_guard.py")][0]
    expected = hook(script, env, spawn, cwd)
    check(expected.returncode in (0, 2) and (expected.stdout or expected.stderr), "plugin: script hook gave no decision")
    (folder / "router/install-manifest.json").unlink()
    for event, command in commands:
        result = hook(command, env, payload(event), cwd)
        check("Traceback" not in result.stderr, f"plugin: {event} hook failed: {result.stderr[-200:]}")
        if "spawn_guard.py" in command:
            check((result.returncode, result.stdout, result.stderr) == (expected.returncode, expected.stdout, expected.stderr),
                  f"plugin: spawn decision differs from the script hook: {result.stdout[:160]} {result.stderr[:160]}")
    tally["plugin", "claude"] += 1
    shutil.rmtree(home)


if not ONLY or "versions" in ONLY:
    versions_case()
    if (router / "plugins/router").is_dir() and (router / "scripts/gen_plugin.py").is_file():
        plugin_case()
    print(f"PASS: versions in both status commands ({tally['versions', 'claude']} claude, "
          f"{tally['versions', 'codex']} codex), plugin stand-down ({tally['plugin', 'claude']})")
PY
