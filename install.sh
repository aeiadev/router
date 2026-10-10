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
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time

if sys.version_info < (3, 10):
    sys.exit("router install: missing requirement: python3 3.10 or newer")

source = Path(sys.argv[1])
try:
    package_version = (source / "VERSION").read_text(encoding="utf-8").strip()
except (OSError, UnicodeError) as exc:
    sys.exit(f"router install: cannot read {source / 'VERSION'}: {exc}")
sys.dont_write_bytecode = True
sys.path.insert(0, str(source / "hooks/router"))
import common

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--host", choices=("claude", "codex", "both"),
                    help="install for selected hosts (default: hosts present, or Claude for a fresh HOME)")
parser.add_argument("--dry-run", action="store_true", help="show changes without writing files")
parser.add_argument("--uninstall", action="store_true", help="remove unchanged installed files and added hooks")
parser.add_argument("--with-defaults", action="store_true", help="manage delegation defaults in host instructions")
parser.add_argument("--purge", action="store_true",
                    help="remove Router backups and its retired 0.2 run state; with --uninstall, after uninstalling")
parser.add_argument("--status", action="store_true", help="report Claude Code plugin state and exit; changes nothing")
args = parser.parse_args(sys.argv[2:])
if args.purge and args.with_defaults:
    sys.exit("router install: --purge cannot be combined with --with-defaults")
homes = {name: Path(os.environ.get(name.upper() + "_HOME") or Path.home() / ("." + name)).expanduser().absolute()
         for name in ("claude", "codex")}
if args.status:
    if args.host or args.dry_run or args.uninstall or args.with_defaults or args.purge:
        sys.exit("router install: --status cannot be combined with other options")
    manifest_present = os.path.lexists(homes["claude"] / "router/install-manifest.json")
    for _, line in common.plugin_report(homes["claude"], package_version, manifest_present):
        print(f"plugin: {line}")
    sys.exit(0)
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
    if path.is_symlink():
        fail(f"refusing symlink: {path}")
    if not path.exists():
        return copy.deepcopy(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read JSON in {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected a JSON object in {path}")
    return value


def validate_hooks(path, value):
    if "hooks" not in value:
        return
    hooks = value["hooks"]
    if not isinstance(hooks, dict):
        fail(f"{path} hooks must be an object")
    for event, blocks in hooks.items():
        if not isinstance(event, str) or not isinstance(blocks, list):
            fail(f"{path} hooks must map events to arrays")
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("hooks"), list):
                fail(f"{path} hooks.{event} has an invalid block")


def validate_manifest(path, value):
    if value.get("version") != 1 or not isinstance(value.get("files"), dict) or not isinstance(value.get("hooks"), list):
        fail(f"invalid router install manifest: {path}")
    for entry in value["hooks"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("event"), str) or not isinstance(entry.get("block"), dict):
            fail(f"invalid router install manifest: {path}")
    created = value.get("created_dirs", [])
    if not isinstance(created, list) or any(
            not isinstance(item, str) or not item or Path(item).is_absolute() or ".." in Path(item).parts
            for item in created):
        fail(f"invalid router install manifest: {path}")
    for relative, digest in value["files"].items():
        if (not isinstance(relative, str) or not relative or Path(relative).is_absolute() or
            ".." in Path(relative).parts or not isinstance(digest, dict) or
            not (set(digest) == {"sha256"} and isinstance(digest["sha256"], str) and
                 re.fullmatch(r"[0-9a-f]{64}", digest["sha256"]) or
                 set(digest) == {"link"} and isinstance(digest["link"], str))):
            fail(f"invalid router install manifest: {path}")


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


def prune_dirs(relatives):
    """Remove empty directories below the host home, deepest first, never through a symlink."""
    real_home = home.resolve()
    for relative in sorted(set(relatives), key=lambda item: len(Path(item).parts), reverse=True):
        folder = home / relative
        try:
            resolved = folder.resolve()
        except (OSError, RuntimeError):
            resolved = None
        # Equal only when the path stays under the resolved home and no component is a symlink.
        if resolved != real_home / relative:
            print(f"Keeping directory reached through a symlink: {folder}")
            continue
        if folder.is_dir() and not folder.is_symlink():
            try:
                folder.rmdir()
            except OSError:
                pass


def old_install_dirs(manifest):
    """Folders below the home that hold a file an old manifest (0.1 or 0.2, no created_dirs) lists."""
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        return []
    return sorted({str(parent) for relative in files for parent in Path(relative).parents
                   if str(parent) != "."})


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


def regular_file(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def purge():
    """Remove Router's own backups directly in each selected host home, then the retired 0.2 state."""
    verb = "Would remove" if args.dry_run else "Removed"
    count = 0
    for name in selected:
        folder = homes[name]
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            # Only regular files directly in the home: never a link, never recursive.
            if fnmatch.fnmatchcase(path.name, "*.router-backup-*") and regular_file(path):
                if not args.dry_run:
                    path.unlink()
                print(f"{verb} backup: {path}")
                count += 1
    # Shared by both hosts. The ledger that replaced it is never touched.
    retired = common.state_path() / "attempts.sqlite3"
    for path in (retired, *(retired.with_name(retired.name + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        if regular_file(path):
            if not args.dry_run:
                path.unlink()
            print(f"{verb} retired state: {path}")
    print(f"{'Would purge' if args.dry_run else 'Purged'} {count} backup(s)")


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


def write_bytes(path, data, mode=None):
    """Replace path atomically with the given mode, else the mode of the file it replaces."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".router-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        if mode is not None:
            os.chmod(temporary, mode)
        elif path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def canon(value):
    """The one settings layout both installers write."""
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_json(path, data):
    write_bytes(path, canon(data))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def file_mode(value):
    return value & ~0o022 if type(value) is int and 0 <= value <= 0o777 else None


def plain_name(value):
    return isinstance(value, str) and Path(value).name == value and value not in ("", ".", "..")


def copy_original(data):
    """Keep the original settings in a private backup of Router's own."""
    backup = settings.with_name(f"{settings.name}.router-backup-{time.time_ns()}")
    write_bytes(backup, data, 0o600)
    print(f"Backed up original settings: {backup}")
    return backup.name


def sibling_original(sibling):
    """The sibling's recorded original, when its backup is still the bytes it recorded."""
    existed, name = sibling.get("settings_original_existed"), sibling.get("settings_original_backup")
    if existed is False:
        return {"existed": False, "backup": None, "sha256": None, "mode": None}
    if existed is None and "settings_original_existed" in sibling:
        return {"existed": None, "backup": None, "sha256": None, "mode": None}  # Unknown is inherited too.
    if existed is not True or not plain_name(name) or not regular_file(home / name):
        return None
    try:
        data = (home / name).read_bytes()
    except OSError:
        return None  # Unreadable: no source here, the next one in order.
    recorded = sibling.get("settings_original_sha256")  # Absent in Harness 0.3.0 manifests.
    if recorded is not None and recorded != sha256(data):
        return None
    return {"existed": True, "backup": copy_original(data), "sha256": sha256(data),
            "mode": file_mode(sibling.get("settings_original_mode"))}


def legacy_backups():
    """The settings backups of 0.1 and 0.2 releases, as (age, name, is Router's own)."""
    found = []
    try:
        names = list(home.iterdir())
    except OSError:
        return found
    for path in names:
        number = re.fullmatch(re.escape(settings.name) + r"\.router-backup-([0-9]+)", path.name)
        if number and regular_file(path):
            found.append((int(number[1]), path.name, True))  # Named by the time it was made.
        elif path.name.startswith(settings.name + ".harness-backup-") and regular_file(path):
            found.append((path.lstat().st_mtime_ns, path.name, False))
    return found


def legacy_original(manifest, sibling):
    """With a 0.1 or 0.2 install present, the oldest settings backup either tool left; with none,
    unknown when an old install's hooks are in the file, since its bytes are then not the original."""
    old_router = manifest_path.exists() and "package" not in manifest
    old_sibling = bool(sibling) and ("package" not in sibling or "settings_original_existed" not in sibling)
    if not (old_router or old_sibling):
        return None
    found = legacy_backups()
    if not found:
        hooks = sibling.get("hooks") if old_sibling and isinstance(sibling.get("hooks"), dict) else {}
        touched = old_router and bool(manifest["hooks"]) or any(hooks.values())
        return {"existed": None, "backup": None, "sha256": None, "mode": None} if touched else None
    _, name, own = min(found)
    try:
        data = (home / name).read_bytes()
    except OSError:
        return None  # Unreadable: no source here, the next one in order.
    return {"existed": True, "backup": name if own else copy_original(data), "sha256": sha256(data),
            "mode": file_mode(stat.S_IMODE((home / name).lstat().st_mode)), "baseline": data}


def record_original(manifest, sibling):
    """Fix the original settings once, on the first 0.3 run: the first source that exists."""
    found = sibling_original(sibling) or legacy_original(manifest, sibling)
    if found is not None:
        return found
    if not settings.is_file():
        return {"existed": False, "backup": None, "sha256": None, "mode": None}
    return {"existed": True, "backup": None, "sha256": sha256(settings.read_bytes()),
            "mode": file_mode(stat.S_IMODE(settings.stat().st_mode)), "current": True}


def shipped(relative):
    """The fingerprint of a shared role as this checkout ships it, or None."""
    path = source / ("agents" if host == "claude" else "codex/agents") / Path(relative).name
    return fingerprint(path) if relative in SHARED_ROLES else None


def private_backup(path):
    """Backups can hold secrets from settings or instructions: owner read and write only."""
    if path is not None and regular_file(path):
        os.chmod(path, 0o600)


def save_settings(data, original):
    if data == original and settings.exists():
        return None
    backup = None
    if settings.exists():
        backup = settings.with_name(f"{settings.name}.router-backup-{time.time_ns()}")
        shutil.copy2(settings, backup)
        os.chmod(backup, 0o600)
        print(f"Backed up settings: {backup}")
    write_json(settings, data)
    return backup


def run(preflight=False):
    check_path(settings.name)
    check_path("router/install-manifest.json")
    original = load_json(settings, {})
    config = copy.deepcopy(original)
    validate_hooks(settings, config)
    manifest = load_json(manifest_path, {"version": 1, "files": {}, "hooks": []})
    validate_manifest(manifest_path, manifest)
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
            current = fingerprint(target)
            # A role equal to the bytes this checkout ships is unchanged, whoever wrote it last.
            if current == recorded or current is not None and current == shipped(relative):
                removable.append(target)
            elif target.exists() or target.is_symlink():
                print(f"Preserving modified file: {target}")
        hooks = config.get("hooks", {})
        for entry in manifest["hooks"]:
            event, block = entry["event"], entry["block"]
            blocks = hooks.get(event, [])
            if not isinstance(blocks, list):
                fail(f"{settings} hooks.{event} must be an array")
            if block in blocks:
                blocks.remove(block)
                if not blocks and event not in manifest.get("existing_empty_events", []):
                    hooks.pop(event, None)
        had_hooks = manifest.get("had_hooks", True)
        if "had_hooks" not in manifest:
            # A 0.1 manifest did not record it (decision D3): its oldest backup shows if the key was there.
            old = legacy_backups()
            try:
                baseline = json.loads((home / min(old)[1]).read_bytes()) if old else None
            except (OSError, ValueError, RecursionError):
                baseline = None
            if isinstance(baseline, dict):
                had_hooks = "hooks" in baseline
        if not hooks and not had_hooks:
            config.pop("hooks", None)
        if preflight:
            return
        if args.dry_run:
            print(f"Would uninstall {len(removable)} unchanged files and remove added hooks from {settings}")
            if defaults is not None:
                print(f"Would remove delegation defaults from {defaults['path']}")
            return
        if defaults is not None:
            common.apply_defaults(defaults)
            private_backup(defaults["backup"])
        # The shared rule: the verified original when it holds exactly what is left, else
        # removal when nothing is left of a file that was absent or is unknown, else canon.
        backup_name = manifest.get("settings_original_backup")
        backup = settings.with_name(backup_name) if plain_name(backup_name) else None
        existed, recorded_sha, recorded_mode = (manifest.get("settings_original_existed"),
                                                manifest.get("settings_original_sha256"),
                                                manifest.get("settings_original_mode"))
        if "settings_original_existed" not in manifest:
            # No record (a 0.1 or 0.2 install): only what that release recorded says the file existed.
            old = legacy_backups()
            existed = True if manifest.get("had_hooks") is True or manifest.get("existing_empty_events") or old else None
            if old:
                backup = home / min(old)[1]
                recorded_sha = None
                try:
                    recorded_mode = stat.S_IMODE(backup.lstat().st_mode)
                except OSError:
                    pass
        try:
            data = backup.read_bytes() if backup is not None and regular_file(backup) else None
        except OSError:
            data = None  # Unreadable: it does not verify.
        try:
            same = (data is not None and (recorded_sha is None and "settings_original_existed" not in manifest
                                          or recorded_sha == sha256(data)) and
                    canon(json.loads(data)) == canon(config))
        except (ValueError, RecursionError):
            same = False
        if same:
            write_bytes(settings, data, file_mode(recorded_mode))
        elif config in ({}, {"hooks": {}}) and existed is not True:
            if settings.exists():
                settings.unlink()
        else:
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
        # Only directories this install created, only when empty, deepest first.
        # A manifest upgraded from 0.1 or 0.2 has no record: take the folders its listed files live in.
        prune_dirs(manifest["created_dirs"] if "created_dirs" in manifest else old_install_dirs(manifest))
        print("Router uninstalled. Modified files and the run ledger are preserved." if args.purge else
              "Router uninstalled. Settings backups, modified files and run state are preserved.")
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
    # An upgrade drops what this checkout no longer ships: unchanged files go, edited ones
    # stay as the user's. Shared roles are never dropped here, and no directory is removed.
    stale, kept_old = [], []
    planned = {}
    for relative, recorded in sorted(manifest["files"].items()):
        if relative in files or relative in links or relative in SHARED_ROLES:
            planned[relative] = recorded
            continue
        target = check_path(relative)
        if fingerprint(target) == recorded:
            stale.append(target)
        elif target.exists() or target.is_symlink():
            kept_old.append(target)
    writes = []
    # A role the Harness manifest claims is not rewritten here when Harness is another version or
    # the role differs from what Harness recorded: Router claims the bytes on disk and says so.
    # A same-version claim on unchanged bytes is no user file. An invalid manifest claims nothing.
    harness = harness_manifest()
    claimed_roles = SHARED_ROLES & set(harness["files"]) if harness else set()
    same_version = bool(harness) and harness.get("package") == package_version
    skewed = False
    for relative, payload in {**files, **links}.items():
        target = check_path(relative)
        wanted = fingerprint(payload) if isinstance(payload, Path) else {"link": payload}
        if wanted is None:
            fail(f"missing package file: {payload}")
        current = fingerprint(target)
        exists = target.exists() or target.is_symlink()
        released = relative in claimed_roles and same_version and current == {"sha256": harness["files"][relative]}
        if relative in claimed_roles and not released and current is not None and current != wanted:
            planned[relative] = current
            skewed = True
            continue
        if exists and current != wanted and current != manifest["files"].get(relative) and not released:
            fail(f"refusing to overwrite an existing or modified file: {target}")
        if exists and current is None:
            fail(f"expected a file: {target}")
        if current != wanted:
            writes.append((target, payload))
            planned[relative] = wanted
        elif relative in manifest["files"] or relative in SHARED_ROLES:
            planned[relative] = wanted

    template = "examples/settings.example.json" if host == "claude" else "codex/hooks.json"
    template_path = source / template
    template_value = load_json(template_path, {})
    validate_hooks(template_path, template_value)
    if "hooks" not in template_value:
        fail(f"missing hooks in {template_path}")
    additions = template_value["hooks"]
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
                fail(f"{settings} hooks.{event} must be an array")
            if block in existing:
                existing.remove(block)
        else:
            recorded_hooks.append(entry)
    for event, blocks in additions.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            fail(f"{settings} hooks.{event} must be an array")
        for block in blocks:
            if block not in existing:
                existing.append(block)
                recorded_hooks.append({"event": event, "block": block})
    if preflight:
        return
    if skewed:
        package = harness.get("package")
        version = ("0.2.0 or earlier" if "package" not in harness else
                   package if isinstance(package, str) and re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", package) else "unknown")
        print(f"shared roles come from Harness {version}; upgrade it for the "
              f"{'.'.join(package_version.split('.')[:2])} roles")
    if args.dry_run:
        print(f"Would install {len(writes)} files under {home} and merge router hooks into {settings}")
        for target in stale:
            print(f"Would remove no-longer-shipped: {target}")
        for target in kept_old:
            print(f"Would keep modified old file: {target}")
        if defaults is not None:
            print(f"Would insert or replace delegation defaults in {defaults['path']}")
        return
    candidates = {home}
    for target in [target for target, _ in writes] + [settings, manifest_path]:
        candidates.update(parent for parent in target.parents if parent == home or home in parent.parents)
    created_dirs = list(manifest.get("created_dirs", []))
    for folder in sorted(candidates, key=lambda path: len(path.parts)):
        if not folder.exists() and not folder.is_symlink():
            relative = str(folder.relative_to(home))
            if relative not in created_dirs:
                created_dirs.append(relative)
    sibling = harness_manifest() or {}
    # A folder the sibling created that holds a path Router writes goes when the last tool leaves.
    held = {str(parent) for relative in [*files, *links, settings.name, "router/install-manifest.json"]
            for parent in Path(relative).parents}
    sibling_dirs = sibling.get("created_dirs") if isinstance(sibling.get("created_dirs"), list) else (
        old_install_dirs(sibling) if "created_dirs" not in sibling else [])
    created_dirs += [item for item in dict.fromkeys(item for item in sibling_dirs if isinstance(item, str))
                     if item in held and item not in created_dirs]
    for target, payload in writes:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            target.unlink()
        if isinstance(payload, Path):
            executable = target.parent == home / "router/bin" or bool(payload.stat().st_mode & 0o111)
            descriptor, name = tempfile.mkstemp(dir=target.parent, prefix="." + target.name + ".")
            try:
                with os.fdopen(descriptor, "wb") as handle, open(payload, "rb") as source_file:
                    shutil.copyfileobj(source_file, handle)
                os.chmod(name, 0o755 if executable else 0o644)
                os.replace(name, target)
            except BaseException:
                if os.path.exists(name):
                    os.unlink(name)
                raise
        else:
            target.symlink_to(payload)
    for target in stale:
        target.unlink()
        print(f"Removed no-longer-shipped: {target}")
    for target in kept_old:
        print(f"Keeping modified old file: {target}")
    first_install = not manifest_path.exists()
    record = record_original(manifest, sibling) if "settings_original_sha256" not in manifest else None
    wrote = config != original or not settings.exists()
    settings_backup = save_settings(config, original)
    if record is not None and record.get("current") and settings_backup is not None:
        record["backup"] = settings_backup.name  # The bytes on disk, kept before Router's write.
    try:  # An earlier release's backup is the best evidence of the settings before any tool.
        baseline = json.loads(record["baseline"]) if record and "baseline" in record else None
    except (ValueError, RecursionError):
        baseline = None
    baseline = baseline if isinstance(baseline, dict) else None
    if "had_hooks" in manifest:
        had_hooks = manifest["had_hooks"]
    elif first_install and sibling and "had_hooks" not in sibling and baseline is not None:
        had_hooks = "hooks" in baseline  # A 0.1 sibling did not record it: its backup shows it.
    elif first_install:
        had_hooks = "hooks" in original and sibling.get("had_hooks") is not False
    else:
        had_hooks = "hooks" in baseline if baseline is not None else True
    if "existing_empty_events" in manifest:
        empty_events = manifest["existing_empty_events"]
    else:
        present = original.get("hooks", {})
        empty_events = [event for event, blocks in present.items() if blocks == []]
        # The sibling recorded the user's empty events before its own hooks filled them.
        theirs = sibling.get("existing_empty_events") if isinstance(sibling.get("existing_empty_events"), list) else []
        before = [event for event, blocks in baseline.get("hooks", {}).items()
                  if blocks == []] if baseline is not None and isinstance(baseline.get("hooks"), dict) else []
        empty_events += [event for event in dict.fromkeys(item for item in theirs + before if isinstance(item, str))
                         if event in present and event not in empty_events]
    result = {"version": 1, "package": package_version,
              "installed_at": datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat(),
              "source": str(source),
              "files": planned, "hooks": recorded_hooks}
    for key in ("existed", "backup", "sha256", "mode"):
        name = "settings_original_" + key
        if record is not None:
            result[name] = record[key]
        elif name in manifest:
            result[name] = manifest[name]
    # Only Router's own write moves this hash: a sibling's later write is no user edit.
    written = sha256(settings.read_bytes()) if wrote else manifest.get("settings_written_sha256")
    if written is not None:
        result["settings_written_sha256"] = written
    result.update(had_hooks=had_hooks, existing_empty_events=empty_events)
    if first_install or "created_dirs" in manifest:
        result["created_dirs"] = created_dirs  # Absent after a 0.2 upgrade: uninstall derives the folders.
    if defaults is not None:
        common.apply_defaults(defaults)
        private_backup(defaults["backup"])
        if defaults["backup"] is not None:
            print(f"Backed up defaults: {defaults['backup']}")
        result["defaults"] = dict(defaults["record"], host=host)
    elif "defaults" in manifest:
        result["defaults"] = manifest["defaults"]
    if isinstance(manifest.get("installed_at"), str) and dict(result, installed_at=manifest["installed_at"]) == manifest:
        result["installed_at"] = manifest["installed_at"]  # Nothing else changed: a true no-op.
    if result != manifest or not manifest_path.exists():
        write_json(manifest_path, result)
    # Backups can hold secrets; one an older release left with the settings' mode becomes private too.
    for path in sorted(home.iterdir()):
        if (fnmatch.fnmatchcase(path.name, "*.router-backup-*") and regular_file(path)
                and stat.S_IMODE(path.lstat().st_mode) != 0o600):
            try:
                os.chmod(path, 0o600)
            except OSError:
                continue
            print(f"Made backup private (0600): {path}")
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
    if args.purge and not args.uninstall:
        purge()  # On its own --purge installs nothing and changes nothing else.
        sys.exit(0)
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
        run(preflight=True)
    for host in selected:
        home = homes[host]
        settings = home / ("settings.json" if host == "claude" else "hooks.json")
        manifest_path = home / "router" / "install-manifest.json"
        run()
    if args.purge:
        purge()  # After the uninstall, so its exact-bytes restore still finds the backup.
except (OSError, ValueError, KeyError, TypeError) as exc:
    sys.exit(f"router install: {exc}")
PY
