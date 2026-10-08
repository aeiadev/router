#!/usr/bin/env bash
set -euo pipefail

for requirement in python3 git; do
  if ! command -v "$requirement" >/dev/null 2>&1; then
    printf 'router install: missing requirement: %s\n' "$requirement" >&2
    exit 1
  fi
done

source_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 - "$source_root" "$@" <<'PY'
"""Install local router files and merge hooks without replacing user settings."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
import time

if sys.version_info < (3, 10):
    sys.exit("router install: missing requirement: python3 3.10 or newer")

source = Path(sys.argv[1])
sys.dont_write_bytecode = True
sys.path.insert(0, str(source / "hooks/router"))
import common

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--host", choices=("claude", "codex", "both"),
                    help="install for selected hosts (default: hosts present, or Claude for a fresh HOME)")
parser.add_argument("--dry-run", action="store_true", help="show changes without writing files")
parser.add_argument("--uninstall", action="store_true", help="remove unchanged installed files and added hooks")
parser.add_argument("--with-defaults", action="store_true", help="manage delegation defaults in host instructions")
args = parser.parse_args(sys.argv[2:])
homes = {name: Path(os.environ.get(name.upper() + "_HOME") or Path.home() / ("." + name)).expanduser().absolute()
         for name in ("claude", "codex")}
if args.host:
    selected = ["claude", "codex"] if args.host == "both" else [args.host]
else:
    selected = [name for name, folder in homes.items()
                if folder.is_dir() or os.environ.get(name.upper() + "_HOME") or shutil.which(name)]
    if not selected:
        selected = ["claude"]


def fail(message):
    raise ValueError(message)


def load_json(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read JSON in {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected a JSON object in {path}")
    return value


def fingerprint(path):
    if path.is_symlink():
        return {"link": os.readlink(path)}
    if path.is_file():
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return None


SHARED_ROLES = frozenset(
    f"agents/{name}{extension}"
    for name in ("sweeper", "researcher", "planner", "builder", "builder-in-place",
                 "judge", "worker", "test-writer", "docs-writer")
    for extension in (".md", ".toml")
)


def harness_manifest():
    """Return a valid Harness manifest, or None when it is unsafe to trust."""
    sibling = home / "harness" / "install-manifest.json"
    if not sibling.exists() and not sibling.is_symlink():
        return {}
    try:
        if sibling.is_symlink() or not sibling.is_file():
            return None
        value = json.loads(sibling.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("files"), dict) or not isinstance(value.get("hooks"), dict):
            return None
        for relative, digest in value["files"].items():
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                return None
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                return None
        return value
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def harness_shared_files():
    """Return Harness claims, or None when its manifest is unsafe to trust."""
    sibling = harness_manifest()
    return None if sibling is None else set(sibling.get("files", {}))


def check_path(relative):
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts or not part.parts:
        fail(f"invalid installed relative path: {relative!r}")
    target = home / part
    for parent in target.parents:
        if parent == home:
            break
        if parent.is_symlink():
            fail(f"refusing to write through a directory symlink: {parent}")
        if parent.exists() and not parent.is_dir():
            fail(f"expected a directory: {parent}")
    return target


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".router-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_settings(data, original):
    if data == original and settings.exists():
        return
    if settings.exists():
        backup = settings.with_name(f"{settings.name}.router-backup-{time.time_ns()}")
        shutil.copy2(settings, backup)
        print(f"Backed up settings: {backup}")
    write_json(settings, data)


def run():
    check_path(settings.name)
    check_path("router/install-manifest.json")
    if settings.is_symlink() or manifest_path.is_symlink():
        fail(f"{settings.name} and install-manifest.json must not be symlinks")
    original = load_json(settings, {})
    config = copy.deepcopy(original)
    if "hooks" in config and not isinstance(config["hooks"], dict):
        fail(f"{settings.name} hooks must be an object")
    manifest = load_json(manifest_path, {"version": 1, "files": {}, "hooks": []})
    if manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("hooks"), list):
        fail("invalid router install manifest")
    defaults = defaults_plans.get(host)

    if args.uninstall:
        if not manifest_path.exists():
            print("Router is not installed by this installer; nothing to remove.")
            return
        removable = []
        harness_files = harness_shared_files() if any(name in SHARED_ROLES for name in manifest["files"]) else set()
        for relative, recorded in manifest["files"].items():
            target = check_path(relative)
            if relative in SHARED_ROLES and (harness_files is None or relative in harness_files):
                reason = "invalid Harness manifest" if harness_files is None else "Harness still uses it"
                print(f"Keeping shared role file: {relative} ({reason}).")
                continue
            if fingerprint(target) == recorded:
                removable.append(target)
            elif target.exists() or target.is_symlink():
                print(f"Preserving modified file: {target}")
        hooks = config.get("hooks", {})
        for entry in manifest["hooks"]:
            event, block = entry["event"], entry["block"]
            blocks = hooks.get(event, [])
            if not isinstance(blocks, list):
                fail(f"{settings.name} hooks.{event} must be an array")
            if block in blocks:
                blocks.remove(block)
                if not blocks and event not in manifest.get("existing_empty_events", []):
                    hooks.pop(event, None)
        if not hooks and not manifest.get("had_hooks", True):
            config.pop("hooks", None)
        if args.dry_run:
            print(f"Would uninstall {len(removable)} unchanged files and remove added hooks from {settings}")
            if defaults is not None:
                print(f"Would remove delegation defaults from {defaults['path']}")
            return
        if defaults is not None:
            common.apply_defaults(defaults)
        save_settings(config, original)
        for target in removable:
            target.unlink()
        installed_python_roots = {home / Path(relative).parent
                                  for relative in manifest["files"]
                                  if Path(relative).suffix == ".py"}
        for root in installed_python_roots:
            if root.is_dir():
                for cache in root.rglob("__pycache__"):
                    if cache.is_dir():
                        shutil.rmtree(cache)
        manifest_path.unlink()
        parents = {parent for target in removable + [manifest_path] for parent in target.parents if parent != home and home in parent.parents}
        for parent in sorted(parents, key=lambda path: len(path.parts), reverse=True):
            try:
                parent.rmdir()
            except OSError:
                pass
        print("Router uninstalled. Settings backups, modified files and run state are preserved.")
        return

    files = {}
    for directory, destination in (("hooks/router", "hooks/router"),
                                   ("agents" if host == "claude" else "codex/agents", "agents"),
                                   ("skills/dispatch", "skills/dispatch"), ("scripts", "router/bin"),
                                   ("templates", "router/templates")):
        folder = source / directory
        if not folder.is_dir():
            fail(f"missing package directory: {directory}")
        for path in sorted(folder.rglob("*")):
            if path.name == "SHARED.sha256":
                continue  # Repository drift check, not an installable agent.
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                files[str(Path(destination) / path.relative_to(folder))] = path
    files["router/bin/router"] = source / "bin/router"
    # Each installed CLI resolves the shared core from its own host tree.
    links = {"router/common.py": "../hooks/router/common.py", "router/routes.json": "../hooks/router/routes.json"}
    planned = dict(manifest["files"])
    writes = []
    for relative, payload in {**files, **links}.items():
        target = check_path(relative)
        wanted = fingerprint(payload) if isinstance(payload, Path) else {"link": payload}
        if wanted is None:
            fail(f"missing package file: {payload}")
        current = fingerprint(target)
        exists = target.exists() or target.is_symlink()
        if exists and current != wanted and current != manifest["files"].get(relative):
            fail(f"refusing to overwrite an existing or modified file: {target}")
        if exists and current is None:
            fail(f"expected a file: {target}")
        if current != wanted:
            writes.append((target, payload))
            planned[relative] = wanted
        elif relative in manifest["files"] or relative in SHARED_ROLES:
            planned[relative] = wanted

    template = "examples/settings.example.json" if host == "claude" else "codex/hooks.json"
    additions = load_json(source / template, {})["hooks"]
    if home != (Path.home() / ("." + host)).absolute():
        for blocks in additions.values():
            for block in blocks:
                for hook in block["hooks"]:
                    script = re.search(r"/([a-z_]+\.py)", hook["command"])[1]
                    if host == "codex":
                        environment = (f"CODEX_HOME={shlex.quote(str(home))} "
                                       f"ROUTER_HOME={shlex.quote(str(home / 'router'))}")
                    else:
                        environment = f"CLAUDE_HOME={shlex.quote(str(home))}"
                    hook["command"] = f"{environment} python3 {shlex.quote(str(home / 'hooks/router' / script))}"
    hooks = config.setdefault("hooks", {})
    # Replace exact, installer-owned old registrations when the matcher changes.
    # Leaving both installed would count every read twice after an upgrade.
    recorded_hooks = []
    for entry in manifest["hooks"]:
        event, block = entry["event"], entry["block"]
        if event in additions and block not in additions[event]:
            existing = hooks.get(event, [])
            if not isinstance(existing, list):
                fail(f"{settings.name} hooks.{event} must be an array")
            if block in existing:
                existing.remove(block)
        else:
            recorded_hooks.append(entry)
    for event, blocks in additions.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            fail(f"{settings.name} hooks.{event} must be an array")
        for block in blocks:
            if block not in existing:
                existing.append(block)
                recorded_hooks.append({"event": event, "block": block})
    if args.dry_run:
        print(f"Would install {len(writes)} files under {home} and merge router hooks into {settings}")
        if defaults is not None:
            print(f"Would insert or replace delegation defaults in {defaults['path']}")
        return
    for target, payload in writes:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        if isinstance(payload, Path):
            shutil.copy2(payload, target)
            if target.parent == home / "router/bin":
                target.chmod(target.stat().st_mode | 0o111)
        else:
            target.symlink_to(payload)
    save_settings(config, original)
    sibling = harness_manifest() if not manifest_path.exists() else None
    had_hooks = ("hooks" in original and not (sibling is not None and sibling.get("had_hooks") is False))
    result = {"version": 1, "files": planned, "hooks": recorded_hooks,
              "had_hooks": manifest.get("had_hooks", had_hooks if not manifest_path.exists() else True),
              "existing_empty_events": manifest.get("existing_empty_events",
                                                     [event for event, blocks in original.get("hooks", {}).items()
                                                      if blocks == []])}
    if defaults is not None:
        common.apply_defaults(defaults)
        if defaults["backup"] is not None:
            print(f"Backed up defaults: {defaults['backup']}")
        result["defaults"] = dict(defaults["record"], host=host)
    elif "defaults" in manifest:
        result["defaults"] = manifest["defaults"]
    if result != manifest or not manifest_path.exists():
        write_json(manifest_path, result)
    print(f"Router installed for {host} in {home}")
    print(f"Add {home / 'router/bin'} to PATH. Run router status to inspect routing.")
    if host == "codex":
        print(f"Roles are installed in the global agents directory: {home / 'agents'}")
        print("Alternatively, register a role file with -c agents.<name>.config_file=...")
        print("As of Codex CLI 0.156, project-level .codex/agents/ may not load under codex exec.")
        print("Trust the new or changed hooks in Codex before using router. Codex prompts for hook trust on startup.")


try:
    if len(selected) > 1 and homes["claude"] == homes["codex"]:
        fail("CLAUDE_HOME and CODEX_HOME must differ when installing both hosts")
    # Reject marker errors for every selected host before changing any files.
    defaults_plans = {}
    if args.with_defaults or args.uninstall:
        for host in selected:
            home = homes[host]
            manifest = common.defaults_manifest(home)
            record = manifest.get("defaults")
            if args.uninstall and record is None:
                continue
            block = (None if args.uninstall else
                     common.render_defaults(host, common.load_routes(), common.auto_mode()))
            defaults_plans[host] = common.plan_defaults(home, host, record, block)
    for host in selected:
        home = homes[host]
        settings = home / ("settings.json" if host == "claude" else "hooks.json")
        manifest_path = home / "router" / "install-manifest.json"
        run()
except (OSError, ValueError, KeyError, TypeError) as exc:
    sys.exit(f"router install: {exc}")
PY
