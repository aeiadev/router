"""Read-only health report for a Router installation."""
import sys

sys.dont_write_bytecode = True

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

import common
import overlay

HELP = "doctor [--host claude|codex]: read-only installation health report"
SHARED_ROLES = frozenset(
    f"agents/{name}{ext}"
    for name in ("sweeper", "researcher", "planner", "builder", "builder-in-place",
                 "judge", "worker", "test-writer", "docs-writer")
    for ext in (".md", ".toml")
)
HOOK_RE = re.compile(r"/hooks/router/([A-Za-z_][A-Za-z_0-9]*\.py)")
VERSION_RE = re.compile(r"[0-9A-Za-z.+-]{1,40}")


def python_version():
    return sys.version_info[:3]


def _line(level, name, detail):
    print(f"[{level}] {name}: {detail}")
    return level


def _read_json(path):
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        with path.open("rb") as stream:
            raw = stream.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("too large")
        return json.loads(raw.decode("utf-8")), None
    except RecursionError:
        return None, "is nested too deeply"
    except (UnicodeError, ValueError):
        return None, "is not valid JSON"
    except (OSError, TypeError):
        return None, "is not readable"


def _exists(path):
    try:
        path.lstat()
        return True
    except OSError:
        return False


def _probe(path):
    """True for a regular file, False when absent or not a file; raises OSError when unreadable."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _dir_error(folder):
    """"<folder> is not readable (<error>)" for a directory that exists but cannot be searched."""
    try:
        os.stat(os.path.join(folder, "."))
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        return f"{folder} is not readable ({type(exc).__name__})"
    return None


def _clean_version(value):
    """The version text install.sh prints for a manifest package; anything else is unknown."""
    return value if isinstance(value, str) and VERSION_RE.fullmatch(value) else "unknown"


def _harness_manifest(path):
    """A Harness manifest the way install.sh harness_manifest accepts it, else None."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("version") != 1
                or not isinstance(value.get("files"), dict) or not isinstance(value.get("hooks"), dict)):
            return None
        for relative, digest in value["files"].items():
            if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
                return None
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                return None
        return value
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        return None


def _manifest(path):
    if not _exists(path):
        return None, "absent"
    value, error = _read_json(path)
    if error or not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        return None, "unknown"
    return value, None


def _commands(value):
    found = []
    def visit(node):
        if isinstance(node, dict):
            for key, item in node.items():
                if key == "command" and isinstance(item, str):
                    found.extend(HOOK_RE.findall(item))
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    visit(item)
    visit(value)
    return found


def _fingerprint(path):
    try:
        if path.is_symlink():
            return {"link": os.readlink(path)}
        if path.is_file():
            return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    except OSError:
        pass
    return None


def _version(root, manifest):
    source = manifest.get("source") if manifest else None
    for candidate in (root / "VERSION", Path(source) / "VERSION" if isinstance(source, str) else None):
        if candidate is None:
            continue
        try:
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return _clean_version(value)
        except (OSError, UnicodeError, ValueError):
            pass
    return "unknown"


def _checkout_root(root, manifest):
    """Find a checkout with the files needed to compare shipped roles."""
    source = manifest.get("source") if manifest else None
    for candidate in (root, Path(source) if isinstance(source, str) and source else None):
        if candidate is not None:
            try:
                if (candidate / "VERSION").is_file() and (candidate / "install.sh").is_file():
                    return candidate
            except OSError:
                pass
    return None


def _leaf_count(node):
    if not isinstance(node, dict):
        return 1
    return sum(_leaf_count(value) for value in node.values())


def _last_write(state):
    try:
        if not state.exists():
            return "warn", "none yet"
        entries = []
        for path in state.iterdir():
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                entries.append((info.st_mtime, path))
        if not entries:
            return "warn", "none yet"
        stamp, path = max(entries, key=lambda item: item[0])
        iso = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return "ok", f"{iso} ({path})"
    except (OSError, OverflowError, ValueError) as exc:
        return "warn", f"unknown (state directory not readable: {state}, {type(exc).__name__})"


def _errors(state):
    path = state / "errors.jsonl"
    try:
        if not path.exists():
            return "ok", "none"
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 65536))
            data = stream.read()
        lines = data.splitlines()[1:] if size > 65536 else data.splitlines()
        records = []
        for line in lines:
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    records.append(item)
            except (UnicodeError, ValueError, RecursionError):
                pass
        if not records:
            return "ok", "none"
        last = records[-1]
        def field(key):
            return " ".join(str(last.get(key, "unknown")).split())[:60]
        return "warn", f"{len(records)} in errors.jsonl, last: {field('ts')} {field('script')} {field('class')}"
    except OSError as exc:
        return "warn", f"unavailable (errors.jsonl is not readable: {path}, {type(exc).__name__})"


def _ceilings(script):
    """CEILINGS as scripts/budget.py assigns it, read without running that file in this process."""
    tree = ast.parse(script.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "CEILINGS" for t in node.targets):
            value = ast.literal_eval(node.value)
            if (not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, int)
                                                       and not isinstance(v, bool) for k, v in value.items())):
                raise ValueError("invalid CEILINGS")
            return value
    raise ValueError("no CEILINGS")


def _budget(home, host, root):
    installed = home / "router/bin/budget.py"
    try:
        script = installed if installed.is_file() else root / "scripts/budget.py"
        found = script.is_file()
    except OSError:
        script, found = root / "scripts/budget.py", False
        try:
            found = script.is_file()
        except OSError:
            pass
    if not found:
        return [("warn", "unavailable (scripts/budget.py is missing)")]
    try:
        timeout = float(os.environ.get("ROUTER_DOCTOR_BUDGET_TIMEOUT", "10"))
        result = subprocess.run([sys.executable, str(script), "--home", str(home), "--host", host, "--json"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise ValueError("budget exited unsuccessfully")
        value = json.loads(result.stdout)
        row = value["trees"][0]
        label, tokens = row["label"], row["tokens"]
        if not isinstance(label, str) or not isinstance(tokens, int) or isinstance(tokens, bool):
            raise ValueError("invalid budget result")
        ceiling = _ceilings(script)[label]
        lines = [("ok", f"{label} {tokens} tokens always-on (estimate, ceiling {ceiling})")]
        if tokens > ceiling:
            lines.append(("warn", f"{tokens} tokens is over the ceiling {ceiling}"))
        return lines
    except (Exception, SystemExit):
        return [("warn", "unavailable (scripts/budget.py failed)")]


def _report(host, home, root):
    print(f"Router doctor ({host}): {home}")
    result = []
    def emit(level, name, detail):
        result.append(_line(level, name, detail))
    version = python_version()
    version_text = ".".join(map(str, version))
    executable = sys.executable
    emit("ok" if version >= (3, 10) else "FAIL", "python",
         f"{version_text} ({executable})" if version >= (3, 10) else
         f"{version_text} at {executable}, need 3.10 or newer")
    command = home / "router/bin/router"
    emit("ok" if shutil.which("router") == str(command) else "warn", "path",
         f"router found at {command}" if shutil.which("router") == str(command) else
         f"{home / 'router/bin'} is not on PATH")
    manifest_path = home / "router/install-manifest.json"
    manifest, condition = _manifest(manifest_path)
    if condition == "absent":
        emit("FAIL", "install", f"no manifest at {manifest_path}; run install.sh")
    elif condition:
        emit("FAIL", "install", f"manifest at {manifest_path} is not readable")
    settings = home / ("settings.json" if host == "claude" else "hooks.json")
    config, problem = _read_json(settings)
    if problem:
        emit("FAIL", "hooks", f"{settings.name} {problem}")
    else:
        try:
            scripts = _commands(config)
        except RecursionError:
            scripts = []
            emit("FAIL", "hooks", f"{settings.name} is nested too deeply")
        else:
            if not scripts:
                emit("FAIL", "hooks", f"none registered in {settings.name}")
            else:
                status = {}
                folder_error = _dir_error(home / "hooks/router")
                for script in sorted(set(scripts)) if folder_error is None else ():
                    target = home / "hooks/router" / script
                    try:
                        status[script] = _probe(target)
                    except OSError as exc:
                        status[script] = f"{target} is not readable ({type(exc).__name__})"
                existing = sum(status.get(script) is True for script in scripts)
                emit("ok" if existing == len(scripts) else "FAIL", "hooks",
                     f"{existing} of {len(scripts)} registered")
                if folder_error:
                    emit("FAIL", "hooks", folder_error)
                for script, state_of in status.items():
                    if state_of is False:
                        emit("FAIL", "hooks", f"{script} is registered but missing")
                    elif state_of is not True:
                        emit("FAIL", "hooks", state_of)
    if host == "codex":
        emit("warn", "hooks trust", "unreadable on Codex (hook trust state is not visible to doctor)")
    files = manifest.get("files", {}) if manifest else {}
    agents = [name for name in files if isinstance(name, str) and name.startswith("agents/")]
    absent, unreadable = [], []
    for name in agents:
        try:
            if not _probe(home / name):
                absent.append(name)
        except OSError as exc:
            unreadable.append(f"{home / name} is not readable ({type(exc).__name__})")
    present = len(agents) - len(absent) - len(unreadable)
    emit("ok" if present == len(agents) and agents else "FAIL", "agents", f"{present} of {len(agents)} role files present")
    for name in absent[:3]:
        emit("FAIL", "agents", f"missing {home / name}")
    for detail in unreadable[:1]:
        emit("FAIL", "agents", detail)
    checkout_version = _version(root, manifest)
    if condition == "absent":
        detail = f"not installed, checkout {checkout_version}"
    elif condition:
        detail = f"installed unknown, checkout {checkout_version}"
    elif "package" not in manifest:
        detail = f"installed 0.2.0 or earlier, checkout {checkout_version}"
    else:
        installed = _clean_version(manifest["package"])
        detail = f"installed {installed}, checkout {checkout_version} " + (
            "(current)" if installed == checkout_version else "(run install.sh to update)")
    emit("ok" if condition is None and "package" in manifest and _clean_version(manifest["package"]) == checkout_version else "warn", "version", detail)
    edited, missing, current = [], [], 0
    for name, fingerprint in files.items():
        if not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
            continue
        path = home / name
        actual = _fingerprint(path)
        if actual is None:
            missing.append(path)
        elif actual != fingerprint:
            edited.append(path)
        else:
            current += 1
    if not manifest:
        emit("warn", "files", "none installed" if condition == "absent" else "unknown (install manifest unreadable)")
    elif edited or missing:
        emit("FAIL" if missing else "warn", "files", f"{len(edited)} edited, {len(missing)} missing, {current} current")
    else:
        emit("ok", "files", f"{current} current")
    for kind, paths in (("edited", edited), ("missing", missing)):
        for path in paths:
            print(f"  {kind}: {path}")
    retired = [home / "agents" / name for name in ("Explore.md", "Plan.md", "general-purpose.md")]
    problem = None
    try:
        retired += list((home / "agents").glob("seat-*.md" if host == "claude" else "seat-*.toml")) if (home / "agents").is_dir() else []
    except OSError as exc:
        problem = f"{home / 'agents'} is not readable ({type(exc).__name__})"
    state = common.state_path()
    retired += [state / name for name in ("attempts.sqlite3", "attempts.sqlite3-wal", "attempts.sqlite3-shm", "attempts.sqlite3-journal")]
    retired = [path for path in retired if _exists(path)]
    emit("warn" if retired else "ok", "leftovers", str(len(retired)) if retired else "none")
    if problem:
        emit("warn", "leftovers", problem)
    for path in retired:
        print(f"  {path} (retired by an earlier release)")
    sibling_path = home / "harness/install-manifest.json"
    sibling, sibling_condition = None, None
    if not _exists(sibling_path):
        sibling_condition = "absent"
    else:
        sibling = _harness_manifest(sibling_path)
        sibling_condition = None if sibling else "unknown"
    sibling_version = "unknown"
    if sibling_condition == "absent":
        sibling_text, sibling_version = "Harness not installed", None
    elif sibling_condition:
        sibling_text = "Harness unknown"
    elif "package" not in sibling:
        sibling_text, sibling_version = "Harness 0.2.0 or earlier installed", "0.2.0 or earlier"
    else:
        sibling_version = _clean_version(sibling["package"])
        sibling_text = "Harness unknown" if sibling_version == "unknown" else f"Harness {sibling_version} installed"
    emit("ok" if sibling_condition is None and sibling_version != "unknown" else "warn", "sibling", sibling_text)
    checkout = _checkout_root(root, manifest)
    skew = False
    comparison_unknown = checkout is None
    if sibling and checkout is not None:
        for name in SHARED_ROLES & set(sibling["files"]):
            source = checkout / name if name.endswith(".md") else checkout / "codex" / name
            try:
                if (home / name).read_bytes() != source.read_bytes():
                    skew = True
            except OSError:
                comparison_unknown = True
    emit("warn" if skew or comparison_unknown else "ok", "skew",
         "unknown (checkout not found)" if checkout is None else
         "unknown (shared role not readable)" if comparison_unknown and not skew else
         f"shared roles come from Harness {sibling_version}; upgrade it for the 0.3 roles" if skew else "none")
    if host == "claude":
        for level, detail in common.plugin_report(home, checkout_version, _exists(manifest_path)):
            emit(level, "plugin", detail)
    source = Path(os.environ.get("ROUTES_JSON") or common.ROUTES_PATH)
    routes = None
    try:
        routes, issue = common.load_routes_checked()
        emit("ok", "routes", f"shipped table valid ({source})")
        local = overlay.overlay_path()
        if local is None:
            emit("ok", "overlay", "off (ROUTER_LOCAL=off)")
        elif issue:
            detail = issue["message"].removeprefix(f"routes overlay {local} is invalid: ")
            emit("FAIL", "overlay", f"invalid {local}: {detail}")
        elif not _exists(local):
            emit("ok", "overlay", f"none ({local})")
        else:
            patch = overlay.read_patch(local)
            emit("ok", "overlay", f"ok {local} ({_leaf_count(patch)} keys changed)")
    except (Exception, SystemExit) as exc:
        detail = str(exc) or type(exc).__name__
        if isinstance(exc, common.RoutesError):
            prefix = f"routes table {source} "
            detail = detail.removeprefix(prefix)
        emit("FAIL", "routes", f"{source}: {detail}")
    level, detail = _last_write(state)
    emit(level, "last hook write", detail)
    level, detail = _errors(state)
    emit(level, "errors", detail)
    if routes is not None:
        try:
            modes = {key: common.effective_mode(routes["router"]["modes"].get(key, "enforce" if key in ("brief", "rerun") else None))
                     for key in common.mode_keys(routes)}
            brief = "TASK: Sweep the repository\nRETURN: Report findings"
            decision = common.decide_spawn({"subagent_type": "sweeper", "prompt": brief}, routes, modes, [])
            emit("FAIL" if decision["decision"] == "block" else "ok", "spawn",
                 decision.get("message") or "dry run allowed a sweeper brief")
        except (Exception, SystemExit) as exc:
            emit("FAIL", "spawn", f"dry run failed ({type(exc).__name__})")
    else:
        emit("FAIL", "spawn", "dry run failed (RoutesError)")
    for level, detail in _budget(home, host, root):
        emit(level, "budget", detail)
    return result


def main(argv):
    parser = argparse.ArgumentParser(prog="router doctor", description=HELP)
    parser.add_argument("--host", choices=("claude", "codex"))
    args = parser.parse_args(argv)
    homes = {"claude": common.claude_home(),
             "codex": Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()}
    selected = [args.host] if args.host else [host for host, home in homes.items()
                                               if _exists(home / "router/install-manifest.json")]
    if not selected:
        selected = ["claude"]
    root = Path(common.__file__).resolve().parents[2]
    counts = {"ok": 0, "warn": 0, "FAIL": 0}
    for host in selected:
        for level in _report(host, homes[host], root):
            counts[level] += 1
    print(f"doctor: {counts['ok']} ok, {counts['warn']} warn, {counts['FAIL']} fail")
    return 1 if counts["FAIL"] else 0
