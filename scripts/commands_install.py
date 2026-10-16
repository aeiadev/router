#!/usr/bin/env python3
"""Guarded, isolated commands-profile installer shared by Router and Harness."""

import argparse
from contextlib import contextmanager, ExitStack
try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import time
import uuid

PRODUCT = "router"
HOST = "claude"
HOST_HOME = None
JSON_LIMIT = 1024 * 1024


def launcher_name():
    return HOST + "-" + PRODUCT


def executable_name():
    return PRODUCT + "-commands"


def host_root(home):
    return HOST_HOME if HOST_HOME is not None else home / ("." + HOST)


def state_root(home):
    base = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state")))
    require(base.is_absolute(), "XDG_STATE_HOME must be absolute")
    # Validate the original spelling before normalization can hide a symlink/..
    # component. The canonical spelling is then shared by plan, journal and lock.
    for ancestor in reversed(base.parents):
        safe_dir(ancestor)
    safe_dir(base)
    return Path(canonical_path(base)) / (PRODUCT + "-commands-installer") / HOST


class Refusal(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def checked_record(value):
    data = encoded(value)
    require(len(data) <= JSON_LIMIT, "JSON record exceeds size limit")
    return data


def windows_component_limit(anchor):
    """Read the actual volume's component limit without changing host settings."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    volume_path = kernel.GetVolumePathNameW
    volume_path.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    volume_path.restype = wintypes.BOOL
    information = kernel.GetVolumeInformationW
    information.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD)
    information.restype = wintypes.BOOL
    root = ctypes.create_unicode_buffer(32768)
    component = wintypes.DWORD()
    require(volume_path(str(anchor), root, len(root)) and
            information(root.value, None, 0, None, ctypes.byref(component), None, None, 0)
            and component.value > 0, "filesystem path limits unavailable")
    return component.value


def path_bounds(items):
    """Check every derived path before mutation, including temporary names."""
    cache = {}
    for path, directory in items:
        anchor = path.parent
        missing = []
        while anchor not in cache:
            try:
                info = anchor.stat()
            except OSError as exc:
                import errno
                if exc.errno not in (errno.ENOENT, errno.ENAMETOOLONG):
                    raise Refusal("filesystem path limits unavailable") from None
                missing.append(anchor)
                require(anchor.parent != anchor, "filesystem path limits unavailable")
                anchor = anchor.parent
                continue
            require(stat.S_ISDIR(info.st_mode), "filesystem path ancestor invalid")
            if os.name == "nt":
                cache[anchor] = (windows_component_limit(anchor), 260)
            else:
                cache[anchor] = (os.pathconf(anchor, "PC_NAME_MAX"),
                                 os.pathconf(anchor, "PC_PATH_MAX"))
            break
        limits = cache[anchor]
        for absent in missing:
            cache[absent] = limits
        name_max, path_max = limits
        require(name_max > 0 and path_max > 0, "filesystem path limits unavailable")
        if os.name == "nt":
            measure = lambda text: len(str(text).encode("utf-16-le", "surrogatepass")) // 2
            # No read-only documented API proves this running process's long
            # path opt-in universally. Keep unqualified layouts explicit; do
            # not infer capability from a registry flag or modify the host.
            require(measure(path) < (248 if directory else path_max),
                    "Windows long-path lifecycle is unqualified")
        else:
            measure = lambda text: len(os.fsencode(text))
            require(measure(path) < path_max, "filesystem path length exceeds limit")
        require(measure(path.name) <= name_max, "filesystem path component exceeds limit")


def path_preflight(home, journal, transaction):
    target, link, state = paths(home)
    entry = state / transaction
    boot = bootstrap_path(home)
    record = entry / "journal.json"
    candidates = [(p, True) for p in parent_paths(home)]
    candidates += [(target, True), (entry, True), (link, False),
                   (link.with_name(link.name + ".installer-tmp"), False)]
    candidates += [(p, False) for p in owner_paths(home)]
    for path in (boot, record):
        candidates.extend(((path, False),
                           (path.with_name(path.name + "." + "0" * 32 + ".tmp"), False)))
    inventories = (journal["before"]["tree"] or {}, journal["after"])
    for root in (target, entry / "stage", entry / "backup",
                 entry / "recovery_stage", entry / "rollback_stage"):
        candidates.append((root, True))
        for inventory in inventories:
            candidates.extend((root / relative, value["type"] == "dir")
                              for relative, value in inventory.items())
        if root.name in ("stage", "recovery_stage"):
            candidates.extend((root / (relative + ".writing-" + "0" * 32), False)
                              for relative, value in journal["after"].items()
                              if value["type"] == "file")
    path_bounds(candidates)


def lifecycle_preflight(journal, transaction=None):
    """Reserve the maximum serialized growth of staging/recovery/rollback."""
    checked_record(journal)
    if transaction is not None:
        checked_record({"transaction": transaction, "journal": journal})
    largest = {key: journal[key] for key in ("writing", "writing_temp") if key in journal}
    largest_size = len(encoded(largest))
    for relative, value in journal["after"].items():
        if value["type"] == "file":
            intent = {"writing": relative, "writing_temp": relative + ".writing-" + "0" * 32}
            size = len(encoded(intent))
            if size > largest_size:
                largest, largest_size = intent, size
    # False is longer than true; rolled_back is the longest phase. Combining
    # optional fields conservatively covers recovery of an interrupted write.
    maximum = dict(journal, phase="rolled_back", operation="rollback", staging=False,
                   recovering=False, unlinking=False, cleanup=False)
    maximum.update(largest)
    checked_record(maximum)


def is_reparse(info):
    # Junctions are directories to stat on Windows, but must never be traversed
    # as installer-owned material. Intentional owner/launcher links have their
    # own exact-target checks and do not enter these regular-path readers.
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def read_json(path):
    try:
        if path.is_symlink() or is_reparse(path.lstat()):
            raise Refusal("unsafe JSON input")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) |
                     getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= JSON_LIMIT,
                    "unsafe JSON input")
            require(path.lstat().st_ino == info.st_ino, "unsafe JSON input")
            data = handle.read(JSON_LIMIT + 1)
            require(len(data) <= JSON_LIMIT, "unsafe JSON input")
            return json.loads(data)
    except (OSError, ValueError) as exc:
        raise Refusal("invalid JSON input") from exc


def read_regular(path, limit):
    try:
        require(not path.is_symlink() and not is_reparse(path.lstat()), "unsafe recovery file")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) |
                     getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_size <= limit
                    and path.lstat().st_ino == info.st_ino, "unsafe recovery file")
            data = handle.read(limit + 1)
            require(len(data) <= limit, "unsafe recovery file")
            return data
    except OSError as exc:
        raise Refusal("unsafe recovery file") from exc


def safe_dir(path, required=False):
    try:
        info = path.lstat()
        mode = info.st_mode
        require(not is_reparse(info), "unsafe directory or reparse parent")
    except FileNotFoundError:
        require(not required, "required directory missing")
        return False
    require(stat.S_ISDIR(mode), "unsafe directory or symlink parent")
    return True


def parents(home):
    require(home.is_absolute(), "home must be absolute")
    safe_dir(home, True)
    for path in (home, host_root(home), home / ".local/bin", state_root(home)):
        for ancestor in reversed(path.parents):
            safe_dir(ancestor)
        safe_dir(path)
    state = state_root(home)
    if state.exists():
        info = state.lstat()
        require(private_creation_mode(stat.S_IMODE(info.st_mode), True),
                "installer state must be private")
    target, link, state = paths(home)
    # Command data is preserved, never part of an owned runtime/transaction.
    command_data = state.parent.parent / (PRODUCT + "-commands") / HOST
    reserved = (target, link, state, command_data, *owner_paths(home),
                link.with_name(link.name + ".installer-tmp"), bootstrap_path(home))
    canonical = [Path(canonical_path(path)) for path in reserved]
    boot = Path(canonical_path(bootstrap_path(home)))
    for path in canonical:
        for component in (path, *path.parents):
            temporary = (component.parent == boot.parent
                         and component.name.startswith(boot.name + ".")
                         and component.name.endswith(".tmp"))
            require(not temporary, "installer paths overlap bootstrap temporary namespace")
    components = [path.parts for path in canonical]
    for index, first in enumerate(components):
        for second in components[index + 1:]:
            shared = min(len(first), len(second))
            require(first[:shared] != second[:shared], "installer paths overlap")


def paths(home):
    return (host_root(home) / (PRODUCT + "-commands"),
            home / ".local/bin" / launcher_name(), state_root(home))


def owner_paths(home):
    target, link, _ = paths(home)
    return (target.with_name("." + target.name + ".owner"),
            link.with_name("." + link.name + ".owner"))


def owner_token(home):
    return digest(encoded(identity(home)))


def owner_preimages(home):
    result = {}
    for path in owner_paths(home):
        present = path.exists() or path.is_symlink()
        require(not present or path.is_symlink() and os.readlink(path) == owner_token(home),
                "destination reserved by another installation or owner marker drift")
        result[str(path)] = os.readlink(path) if present else None
    return result


def require_owners(home):
    require(all(value == owner_token(home) for value in owner_preimages(home).values()),
            "installation owner marker missing")


def release_owners(home):
    owner_preimages(home)  # Validate both markers before unlinking either.
    for path in owner_paths(home):
        if path.is_symlink():
            path.unlink()


def parent_paths(home):
    roots = (host_root(home), home / ".local/bin", state_root(home))
    result = set()
    for root in roots:
        while root != home and root not in result and root != root.parent:
            result.add(root)
            root = root.parent
    return sorted(result, key=lambda p: (len(p.parts), str(p)))


def parent_preimages(home):
    return {str(part): stat.S_IMODE(part.lstat().st_mode) if part.exists() else None
            for part in parent_paths(home)}


def snapshot(root):
    """Exact file, directory and mode inventory; never follows links."""
    require(safe_dir(root, True), "payload directory missing")
    result = {}

    def visit(directory):
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                rel = path.relative_to(root).as_posix()
                entry_info = entry.stat(follow_symlinks=False)
                require(not is_reparse(entry_info), "reparse point inside payload")
                mode = entry_info.st_mode
                if stat.S_ISDIR(mode):
                    result[rel] = {"type": "dir", "mode": stat.S_IMODE(mode)}
                    visit(path)
                elif stat.S_ISREG(mode):
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) |
                                 getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as handle:
                        info = os.fstat(handle.fileno())
                        require(stat.S_ISREG(info.st_mode) and info.st_size <= 64 * 1024 * 1024
                                and info.st_ino == entry.inode(), "payload file changed")
                        data = handle.read(64 * 1024 * 1024 + 1)
                        require(len(data) <= 64 * 1024 * 1024, "payload file too large")
                        result[rel] = {"type": "file", "mode": stat.S_IMODE(mode),
                                       "sha256": digest(data)}
                else:
                    raise Refusal("symlink or special file inside payload")

    visit(root)
    return result


def validate_inventory(inventory, message):
    require(isinstance(inventory, dict), message)
    for relative, value in inventory.items():
        require(isinstance(relative, str) and relative and relative == str(PurePosixPath(relative))
                and not PurePosixPath(relative).is_absolute() and "\\" not in relative
                and ":" not in relative and all(part not in (".", "..") for part in PurePosixPath(relative).parts)
                and isinstance(value, dict) and value.get("type") in ("dir", "file")
                and type(value.get("mode")) is int and 0 <= value["mode"] <= 0o777
                and (value["type"] == "dir" and set(value) == {"type", "mode"}
                     or value["type"] == "file" and set(value) == {"type", "mode", "sha256"}
                     and isinstance(value["sha256"], str) and len(value["sha256"]) == 64), message)


def created_mode(mode, directory=False):
    """Observed creation bits; Windows reports read-only state, not ACL access."""
    if os.name != "nt":
        return mode
    return (0o666 if mode & 0o200 else 0o444) | (0o111 if directory else 0)


def private_creation_mode(mode, directory=False):
    # POSIX access checks are unchanged. Windows has no equivalent stat ACL
    # bits: require the expected writable creation state, never claim privacy.
    if os.name == "nt":
        return mode == created_mode(0o700 if directory else 0o600, directory)
    return not mode & 0o077


def launcher_target(path):
    value = os.readlink(path)
    if os.name == "nt":
        # readlink adds the Win32 substitution prefix to absolute symlinks.
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return canonical_path(value)
    return value


def validate_manifest(root):
    require(root.is_absolute(), "payload must be absolute")
    for ancestor in reversed(root.parents):
        safe_dir(ancestor, True)
    safe_dir(root, True)
    inventory = snapshot(root)
    validate_inventory(inventory, "invalid payload inventory")
    require(stat.S_IMODE(root.lstat().st_mode) == created_mode(0o700, True),
            "payload root must have mode 0700")
    require(inventory.get("manifest.json", {}).get("type") == "file"
            and private_creation_mode(inventory["manifest.json"]["mode"]),
            "payload manifest must be private")
    require(all(private_creation_mode(value["mode"], True) for value in inventory.values()
                if value["type"] == "dir"), "payload directories must be private")
    manifest = read_json(root / "manifest.json")
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1,
            "unsupported manifest")
    require(isinstance(manifest.get("source_revision"), str) and manifest["source_revision"],
            "missing source revision")
    require(set(manifest) == {"schema_version", "source_revision", "files"}, "invalid manifest fields")
    files = manifest.get("files")
    require(isinstance(files, dict), "invalid file list")
    actual = {k: {"sha256": v["sha256"], "mode": v["mode"]}
              for k, v in inventory.items() if v["type"] == "file" and k != "manifest.json"}
    for key, value in files.items():
        p = PurePosixPath(key)
        require(isinstance(key, str) and key == str(p) and not p.is_absolute()
                and "\\" not in key and ":" not in key and not key.startswith("//")
                and all(part not in (".", "..") for part in p.parts), "unsafe manifest path")
        require(isinstance(value, dict) and set(value) == {"sha256", "mode"}
                and isinstance(value["sha256"], str) and len(value["sha256"]) == 64
                and type(value["mode"]) is int and 0 <= value["mode"] <= 0o777,
                "invalid manifest entry")
        require(os.name != "nt" or value["mode"] & 0o200,
                "read-only payload files are unsupported on Windows")
    observed_files = {key: dict(value, mode=created_mode(value["mode"]))
                      for key, value in files.items()}
    require(observed_files == actual, "payload hash, mode, or file list mismatch")
    command = "bin/" + executable_name()
    require(command in files and files[command]["mode"] & 0o111,
            "missing executable command")
    require("manifest.json" in inventory and "bin" in inventory, "incomplete payload")
    return inventory, manifest


def owned(home):
    target, link, _ = paths(home)
    tree = None
    if target.exists() or target.is_symlink():
        require(not target.is_symlink(), "unknown owned directory")
        private_directory(target)
        tree, _ = validate_manifest(target)
    link_target = None
    if link.exists() or link.is_symlink():
        require(link.is_symlink(), "unknown command ownership")
        link_target = launcher_target(link)
        require(link_target == str(target / "bin" / executable_name()), "unknown command symlink")
    require((tree is None) == (link_target is None), "incomplete owned installation")
    return {"tree": tree, "link": link_target}


def plan(home, payload):
    parents(home)
    owners = owner_preimages(home)
    source, manifest = validate_manifest(payload)
    state = paths(home)[2]
    require(not bootstrap_pending(home) and not pending(state, home),
            "pending transaction; run recover")
    before = owned(home)
    settled_history(home, before)
    if before["tree"] is not None:
        require_owners(home)
    result = {"schema_version": 1, "home": str(home), "payload": str(payload),
              "payload_inventory": source, "payload_manifest_sha256": source["manifest.json"]["sha256"],
              "before": before, "parents": parent_preimages(home), "owners": owners,
              "product": PRODUCT, "host": HOST, "host_home": str(host_root(home)),
              "source_revision": manifest["source_revision"], "identity": identity(home)}
    result["plan_sha256"] = digest(encoded(result))
    future = initial_journal(home, payload, result)
    transaction = "0" * 16 + "-" + "0" * 32
    lifecycle_preflight(future, transaction)
    path_preflight(home, future, transaction)
    require(len((json.dumps(result, sort_keys=True) + "\n").encode()) <= JSON_LIMIT,
            "saved plan exceeds size limit")
    return result


def initial_journal(home, payload, saved):
    target, link, _ = paths(home)
    made = [str(p) for p in parent_paths(home) if saved["parents"][str(p)] is None]
    return {"phase": "pending", "plan_sha256": saved["plan_sha256"],
            "before": saved["before"], "after": saved["payload_inventory"], "made": made,
            "staging": True, "home": str(home), "host_home": str(host_root(home)),
            "product": PRODUCT, "host": HOST, "target": str(target), "link": str(link),
            "payload": str(payload), "parent_before": saved["parents"],
            "identity": identity(home), "owners": saved["owners"]}


def check_plan(saved, home, payload):
    require(isinstance(saved, dict) and saved.get("schema_version") == 1, "invalid plan")
    check = dict(saved)
    signature = check.pop("plan_sha256", None)
    require(signature == digest(encoded(check)), "plan integrity mismatch")
    require(saved.get("home") == str(home) and saved.get("payload") == str(payload)
            and saved.get("product") == PRODUCT and saved.get("host") == HOST
            and saved.get("host_home") == str(host_root(home))
            and saved.get("identity") == identity(home),
            "plan home or payload mismatch")
    require(saved.get("payload_inventory") == validate_manifest(payload)[0], "payload drift")


def canonical_path(path, path_module=None):
    """Normalize with the platform's semantics; preserve Windows drives/UNC."""
    module = path_module or os.path
    normalized = module.normcase(module.normpath(str(path)))
    if module.sep == "/":
        normalized = "/" + normalized.lstrip("/")
    return normalized


def identity(home):
    target, link, state = paths(home)
    return {"home": str(home), "host_home": str(host_root(home)),
            "target": str(target), "link": str(link), "state": str(state),
            "product": PRODUCT, "host": HOST}


def canonical_home(home):
    raw = home.absolute()
    # Check the spelling before collapsing '..'; never hide symlink ancestors.
    parents(raw)
    return Path(canonical_path(raw))


@contextmanager
def lock_path(resource):
    # Each shared destination has one lock, regardless of invoking home/host.
    name = digest(canonical_path(resource).encode())
    # macOS exposes /tmp through a system symlink. Use its fixed physical
    # directory, without resolving arbitrary ancestors or trusting TMPDIR.
    lock_base = (Path("/private/tmp" if sys.platform == "darwin" else "/tmp") if os.name != "nt" else
                 Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))))
    path = lock_base / ("commands-installer-" + name + ".lock")
    for ancestor in reversed(path.parents):
        safe_dir(ancestor, True)
    require(not path.is_symlink(), "unsafe installer lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        info = os.fstat(fd)
        path_info = path.lstat()
        require(stat.S_ISREG(path_info.st_mode) and info.st_ino == path_info.st_ino
                and stat.S_ISREG(info.st_mode) and (not hasattr(os, "getuid") or info.st_uid == os.getuid())
                and (os.name == "nt" or stat.S_IMODE(info.st_mode) == created_mode(0o600))
                and info.st_nlink == 1, "unsafe installer lock")
        if fcntl is None:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        if acquired:
            if fcntl is None:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def lock(home):
    home = canonical_home(home)
    # Stable ordering prevents deadlock when invocations share only some paths.
    with ExitStack() as stack:
        for resource in sorted({canonical_path(p) for p in (*paths(home), bootstrap_path(home))}):
            stack.enter_context(lock_path(resource))
        yield


def journal_preflight(path):
    legacy_temp = path.with_name(path.name + ".tmp")
    require(not legacy_temp.exists() and not legacy_temp.is_symlink(),
            "temporary journal conflict")
    for entry in path.parent.glob(path.name + ".*.tmp"):
        mode = entry.lstat().st_mode
        require(stat.S_ISREG(mode) and private_creation_mode(stat.S_IMODE(mode)),
                "unsafe temporary journal")


def atomic_json(path, value):
    data = checked_record(value)
    journal_preflight(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    path_bounds(((path, False), (temp, False)))
    # Never open an existing entry: it could be a link to unrelated user data.
    # Unique names also let recovery bypass an abandoned partial journal write.
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def bootstrap_entry_exists(entry):
    """Check old failed bootstrap entries without probing unusable future paths."""
    try:
        return entry.exists() or entry.is_symlink()
    except OSError as exc:
        import errno
        if exc.errno != errno.ENAMETOOLONG:
            raise
        # The former absolute-path mkdir could not create this entry. Inspect
        # its usable parent by names, however, so material created via dir_fd
        # by another actor is never mistaken for absence or deleted.
        if entry.parent.exists():
            require(all(child.name != entry.name for child in entry.parent.iterdir()),
                    "bootstrap entry exceeds path limit")
        return False


def validate_journal(home, entry, journal, bootstrap=False):
    target, link, state = paths(home)
    require(entry.parent == state and len(entry.name) == 49
            and entry.name[16] == "-"
            and all(c in "0123456789abcdef" for c in entry.name.replace("-", "")),
            "unsafe transaction identity")
    entry_present = bootstrap_entry_exists(entry) if bootstrap else True
    if entry_present:
        private_directory(entry)
    require(isinstance(journal, dict) and journal.get("home") == str(home)
            and journal.get("host_home") == str(host_root(home))
            and journal.get("product") == PRODUCT and journal.get("host") == HOST
            and journal.get("target") == str(target) and journal.get("link") == str(link)
            and journal.get("identity") == identity(home),
            "journal installation identity mismatch")
    # Bootstrap cleanup never writes a new journal. Its actual partial files
    # are validated below and by recover_bootstrap, not hypothetical temp paths.
    if not bootstrap:
        journal_preflight(entry / "journal.json")
    allowed_entries = {"journal.json", "stage", "backup", "recovery_stage", "rollback_stage"}
    for child in entry.iterdir() if entry_present else ():
        name = child.name
        require(name in allowed_entries or
                (name.startswith("journal.json.") and name.endswith(".tmp")
                 and len(name) == len("journal.json.") + 32 + len(".tmp")
                 and all(c in "0123456789abcdef" for c in name[len("journal.json."):-len(".tmp")])),
                "unexpected transaction material")
    journal_path = entry / "journal.json"
    if not bootstrap and (journal_path.exists() or journal_path.is_symlink()):
        info = journal_path.lstat()
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == created_mode(0o600)
                and info.st_nlink == 1, "unsafe journal mode or identity")
    require(journal.get("phase") in ("pending", "committed", "recovered", "rolled_back")
            and journal.get("operation") in (None, "rollback")
            and isinstance(journal.get("plan_sha256"), str)
            and len(journal["plan_sha256"]) == 64,
            "invalid journal phase or plan")
    for flag in ("staging", "cleanup", "recovering", "unlinking"):
        require(flag not in journal or type(journal[flag]) is bool, "invalid journal flag")
    require(set(journal) <= {"phase", "operation", "plan_sha256", "before", "after",
            "made", "parent_before", "staging", "home", "host_home", "product", "host",
            "target", "link", "payload", "identity", "writing", "writing_temp",
            "cleanup", "recovering", "unlinking", "owners"}, "unknown journal field")
    before = journal.get("before")
    require(isinstance(before, dict) and set(before) == {"tree", "link"}
            and (before["tree"] is None or isinstance(before["tree"], dict))
            and (before["link"] is None or before["link"] == str(target / "bin" / executable_name()))
            and ((before["tree"] is None) == (before["link"] is None))
            and isinstance(journal.get("after"), dict), "invalid journal inventory")
    for inventory in (before["tree"], journal["after"]):
        if inventory is None:
            continue
        validate_inventory(inventory, "invalid journal inventory")
    owners = journal.get("owners")
    require(isinstance(owners, dict) and set(owners) == {str(p) for p in owner_paths(home)}
            and all(value is None or value == owner_token(home) for value in owners.values()),
            "invalid journal owner preimage")
    if before["tree"] is not None:
        require(all(value == owner_token(home) for value in owners.values()),
                "invalid installed owner preimage")
    allowed = {str(p) for p in parent_paths(home)}
    made = journal.get("made")
    require(isinstance(made, list) and len(set(made)) == len(made)
            and all(type(part) is str and part in allowed for part in made),
            "invalid journal created directories")
    parent_before = journal.get("parent_before")
    require(isinstance(parent_before, dict) and set(parent_before) == allowed
            and all(mode is None or type(mode) is int and 0 <= mode <= 0o7777
                    for mode in parent_before.values())
            and set(made) == {str(part) for part in parent_paths(home)
                              if parent_before[str(part)] is None},
            "journal parent preimage mismatch")
    if "writing" in journal:
        relative = journal["writing"]
        require(isinstance(relative, str) and relative in journal["after"]
                and journal["after"][relative]["type"] == "file"
                and isinstance(journal.get("writing_temp"), str)
                and journal["writing_temp"].startswith(relative + ".writing-")
                and len(journal["writing_temp"]) == len(relative) + len(".writing-") + 32
                and all(c in "0123456789abcdef" for c in journal["writing_temp"][-32:])
                and isinstance(journal.get("payload"), str),
                "invalid journal staging intent")
    if "payload" in journal:
        require(isinstance(journal["payload"], str) and Path(journal["payload"]).is_absolute(),
                "invalid journal payload")
    if not bootstrap and journal["phase"] in ("pending", "committed"):
        lifecycle_preflight(journal)
        path_preflight(home, journal, entry.name)

    # Settled transactions must not silently acquire recovery material. Check
    # every historical entry before a later operation changes any journal.
    if journal["phase"] != "pending":
        for name in ("stage", "recovery_stage", "rollback_stage"):
            material = entry / name
            require(not material.exists() and not material.is_symlink(),
                    "unexpected settled transaction material")
        backup = entry / "backup"
        if journal["phase"] == "committed" and before["tree"] is not None:
            require(owned_snapshot(backup) == before["tree"], "backup drift")
        else:
            require(not backup.exists() and not backup.is_symlink(), "unexpected settled backup")


def transactions(state, home):
    if not state.exists():
        return []
    result = []
    for entry in state.iterdir():
        require(not entry.is_symlink(), "unsafe installer state entry")
        if entry.is_dir() and (entry / "journal.json").exists():
            require(not (entry / "journal.json").is_symlink(), "unsafe journal")
            journal = read_json(entry / "journal.json")
            validate_journal(home, entry, journal)
            result.append((entry, journal))
    return sorted(result, key=lambda pair: pair[0].name)


def settled_history(home, current):
    """A settled namespace equals the end of its still-committed history.

    Recovered and rolled-back records remain valid history but have no active
    effect. Call only after refusing pending transactions, never mid-recovery.
    """
    committed = [(path, journal) for path, journal in transactions(paths(home)[2], home)
                 if journal["phase"] == "committed"]
    expected = current["tree"]
    for _, journal in reversed(committed):
        require(journal["after"] == expected, "installation history drift")
        expected = journal["before"]["tree"]
    require(expected is None, "installation history has no absent preimage")
    return committed


def pending(state, home):
    return [(p, j) for p, j in transactions(state, home) if j["phase"] == "pending"]


def private_directory(path):
    require(safe_dir(path, True) and stat.S_IMODE(path.stat().st_mode) == created_mode(0o700, True),
            "owned directory mode drift")


def owned_snapshot(path):
    private_directory(path)
    return snapshot(path)


def bootstrap_path(home):
    return home / ("." + PRODUCT + "-commands-" + HOST + "-bootstrap.json")


def bootstrap_pending(home):
    path = bootstrap_path(home)
    journal_preflight(path)
    return path.exists() or path.is_symlink()


def recover_bootstrap(home):
    """Undo only bootstrap directories, before runtime staging could start.

    The intent is published in the existing home before the first mkdir. It
    remains until the transaction journal is complete. Cleanup is restartable.
    """
    path = bootstrap_path(home)
    require(stat.S_IMODE(path.lstat().st_mode) == created_mode(0o600), "bootstrap mode drift")
    value = read_json(path)
    require(isinstance(value, dict) and set(value) == {"transaction", "journal"}
            and isinstance(value["transaction"], str), "invalid bootstrap")
    entry = paths(home)[2] / value["transaction"]
    journal = value["journal"]
    validate_journal(home, entry, journal, bootstrap=True)
    entry_present = bootstrap_entry_exists(entry)
    require(journal["phase"] == "pending" and journal.get("staging") is True
            and not any(k in journal for k in ("operation", "writing", "writing_temp",
                                             "recovering", "cleanup", "unlinking")),
            "invalid bootstrap phase")
    require(owned(home) == journal["before"], "bootstrap preimage drift")
    owners = owner_preimages(home)
    require(all(value is None or owners[path] == value for path, value in journal["owners"].items()),
            "bootstrap owner drift")
    files = []
    if entry_present:
        for child in entry.iterdir():
            require(child.name == "journal.json" or
                    child.name.startswith("journal.json.") and child.name.endswith(".tmp"),
                    "unexpected bootstrap material")
            info = child.lstat()
            require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == created_mode(0o600)
                    and info.st_nlink == 1, "unsafe bootstrap journal")
            data = read_regular(child, JSON_LIMIT)
            require(data == encoded(journal) if child.name == "journal.json" else
                    encoded(journal).startswith(data), "bootstrap journal drift")
            files.append(child)
    made = [Path(p) for p in journal["made"]]
    # Validate the entire cleanup set before the first unlink/rmdir. A newly
    # added child or changed mode is user drift, even in an empty directory.
    allowed = set(made) | {entry} | set(owner_paths(home))
    for part in parent_paths(home):
        mode = journal["parent_before"][str(part)]
        if mode is not None:
            require(safe_dir(part, True) and stat.S_IMODE(part.stat().st_mode) == mode,
                    "bootstrap parent drift")
        elif part.exists() or part.is_symlink():
            private_directory(part)
            require(all(child in allowed for child in part.iterdir()), "bootstrap directory drift")
    for marker in owner_paths(home):
        if journal["owners"][str(marker)] is None and marker.is_symlink():
            marker.unlink()
    for child in files:
        child.unlink()
    if entry_present:
        entry.rmdir()
    for part in reversed(made):
        if part.exists():
            part.rmdir()
    path.unlink()
    return {"recovered": True, "transaction": entry.name}


def ensure_parents(home):
    made = []
    try:
        for path in parent_paths(home):
            if not safe_dir(path):
                path.mkdir(mode=0o700)
                made.append(str(path))
    except OSError:
        remove_created(home, made)
        raise
    return made


def validate_created(home, journal):
    for part in journal["made"]:
        path = Path(part)
        if path.exists() or path.is_symlink():
            private_directory(path)


def remove_created(home, made):
    allowed = {str(part) for part in parent_paths(home)}
    require(isinstance(made, list) and all(type(part) is str and part in allowed for part in made),
            "invalid created directories")
    for part in reversed(made):
        try:
            Path(part).rmdir()
        except OSError:
            pass


def copy_payload(payload, destination, expected, journal, journal_path):
    destination.mkdir(mode=0o700)
    for relative, value in sorted(expected.items()):
        path = destination / relative
        if value["type"] == "dir":
            path.mkdir(mode=value["mode"])
            path.chmod(value["mode"])
        else:
            source = payload / relative
            require(not source.is_symlink(), "payload changed during staging")
            source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) |
                                getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(source_fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_size <= 64 * 1024 * 1024,
                        "payload changed during staging")
                data = handle.read(64 * 1024 * 1024 + 1)
            require(len(data) <= 64 * 1024 * 1024 and digest(data) == value["sha256"],
                    "payload changed during staging")
            temp_relative = relative + ".writing-" + uuid.uuid4().hex
            temp = destination / temp_relative
            journal["writing"] = relative
            journal["writing_temp"] = temp_relative
            atomic_json(journal_path, journal)
            dest_fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY |
                              getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(dest_fd, "wb") as handle:
                checkpoint("after_stage_file_create")
                middle = max(1, len(data) // 2)
                handle.write(data[:middle])
                handle.flush()
                checkpoint("during_stage_write")
                handle.write(data[middle:])
            temp.chmod(value["mode"])
            checkpoint("after_stage_file_chmod")
            os.replace(temp, path)
            checkpoint("after_stage_file_rename")
            journal.pop("writing")
            journal.pop("writing_temp")
            atomic_json(journal_path, journal)
            path.chmod(value["mode"])
        checkpoint("during_staging")
    require(snapshot(destination) == expected, "payload changed during staging")


def checked_material(path, expected, partial=False, journal=None):
    if not path.exists() and not path.is_symlink():
        return False
    require(safe_dir(path, True) and stat.S_IMODE(path.stat().st_mode) == created_mode(0o700, True),
            "recovery stage directory drift")
    observed = snapshot(path)
    if journal is not None and journal.get("writing_temp") in observed:
        relative = journal["writing"]
        temp_relative = journal["writing_temp"]
        temp_info = observed.pop(temp_relative)
        require(temp_info["type"] == "file" and temp_info["mode"] in (created_mode(0o600), expected[relative]["mode"])
                and relative not in observed, "recovery partial stage drift")
        source = Path(journal["payload"]) / relative
        source_bytes = read_regular(source, 64 * 1024 * 1024)
        require(digest(source_bytes) == expected[relative]["sha256"], "recovery source drift")
        staged_bytes = read_regular(path / temp_relative, 64 * 1024 * 1024)
        require(len(staged_bytes) <= len(source_bytes)
                and source_bytes.startswith(staged_bytes)
                and (temp_info["mode"] == created_mode(0o600) or staged_bytes == source_bytes),
                "recovery partial stage drift")
    require(observed == expected or (partial and all(expected.get(k) == v
            for k, v in observed.items())), "recovery stage drift")
    return True


def remove_material(path):
    # Preflight validates every recovery tree before this function is called.
    # A cleanup intent permits only missing entries on the next recovery.
    if path.exists():
        for child in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
            checkpoint("during_recover_cleanup")
        path.rmdir()


def checkpoint(name):
    if os.environ.get("COMMANDS_INSTALL_CRASH_AT") == name:
        os._exit(91)


def do_apply(home, payload, saved):
    parents(home)
    check_plan(saved, home, payload)
    owners = owner_preimages(home)
    target, link, state = paths(home)
    require(not bootstrap_pending(home) and not pending(state, home),
            "pending transaction; run recover")
    current = owned(home)
    settled_history(home, current)
    if current["tree"] is not None:
        require_owners(home)
    # Repeating an already completed apply with the same saved plan is safe.
    for entry, journal in transactions(state, home):
        if journal["phase"] == "committed" and journal["plan_sha256"] == saved["plan_sha256"]:
            if current["tree"] == saved["payload_inventory"]:
                return {"transaction": entry.name, "installed": True, "idempotent": True}
    require(parent_preimages(home) == saved.get("parents"), "parent preimage drift")
    require(owners == saved.get("owners"), "owner preimage drift")
    require(current == saved["before"], "preimage drift")
    if current["tree"] == saved["payload_inventory"]:
        return {"installed": True, "idempotent": True, "transaction": None}
    temporary_link = link.with_name(link.name + ".installer-tmp")
    require(not temporary_link.exists() and not temporary_link.is_symlink(),
            "temporary link conflict")
    # Wall clocks can move backwards. Under the shared state lock, allocate a
    # strictly increasing sequence so rollback/uninstall retain causal order.
    latest = max((int(p.name[:16], 16) for p, _ in transactions(state, home)), default=0)
    sequence = max(time.time_ns(), latest + 1)
    require(sequence < 1 << 64, "transaction sequence exhausted")
    transaction = f"{sequence:016x}-{uuid.uuid4().hex}"
    entry = state / transaction
    journal = initial_journal(home, payload, saved)
    lifecycle_preflight(journal, transaction)
    path_preflight(home, journal, transaction)
    boot = bootstrap_path(home)
    atomic_json(boot, {"transaction": transaction, "journal": journal})
    ensure_parents(home)
    entry.mkdir(mode=0o700)
    atomic_json(entry / "journal.json", journal)
    for marker in owner_paths(home):
        if owners[str(marker)] is None:
            marker.symlink_to(owner_token(home))
    boot.unlink()
    copy_payload(payload, entry / "stage", saved["payload_inventory"], journal,
                 entry / "journal.json")
    journal["staging"] = False
    atomic_json(entry / "journal.json", journal)
    if current["tree"] is not None:
        os.replace(target, entry / "backup")
    checkpoint("after_backup")
    os.replace(entry / "stage", target)
    checkpoint("after_install")
    temporary_link.symlink_to(target / "bin" / executable_name())
    os.replace(temporary_link, link)
    checkpoint("after_link")
    journal["phase"] = "committed"
    atomic_json(entry / "journal.json", journal)
    return {"transaction": transaction, "installed": True, "idempotent": False}


def do_recover(home):
    parents(home)
    if bootstrap_pending(home):
        return recover_bootstrap(home)
    target, link, state = paths(home)
    items = pending(state, home)
    require(len(items) <= 1, "multiple pending transactions")
    if not items:
        # A crash after final journal publication may leave only own markers.
        if not any(j["phase"] == "committed" for _, j in transactions(state, home)):
            if not target.exists() and not target.is_symlink() and not link.exists() and not link.is_symlink():
                owner_preimages(home)
                release_owners(home)
        return {"recovered": False}
    entry, journal = items[0]
    require_owners(home)
    validate_created(home, journal)
    if journal.get("operation") == "rollback":
        return recover_rollback(home, entry, journal)
    before, after = journal["before"], journal["after"]
    target_is_before = False
    target_is_after = False
    if target.exists() or target.is_symlink():
        require(not target.is_symlink(), "recovery target drift")
        observed = owned_snapshot(target)
        target_is_before = before["tree"] is not None and observed == before["tree"]
        target_is_after = observed == after
        require(target_is_before or target_is_after, "recovery target drift")
    backup = entry / "backup"
    if before["tree"] is not None and not target_is_before:
        require(backup.is_dir() and not backup.is_symlink()
                and owned_snapshot(backup) == before["tree"], "recovery backup drift")
    else:
        require(not backup.exists() and not backup.is_symlink(), "unexpected recovery backup")
    require(not (entry / "rollback_stage").exists()
            and not (entry / "rollback_stage").is_symlink(), "unexpected rollback stage")
    require(not link.exists() or link.is_symlink(), "recovery link drift")
    if link.is_symlink():
        require(launcher_target(link) == str(target / "bin" / executable_name()),
                "recovery link drift")
    elif before["link"] is not None and not journal.get("unlinking"):
        raise Refusal("recovery link drift")
    temp_link = link.with_name(link.name + ".installer-tmp")
    if temp_link.is_symlink():
        require(launcher_target(temp_link) == str(target / "bin" / executable_name()), "recovery temp link drift")
    else:
        require(not temp_link.exists(), "recovery temp link conflict")
    stage = entry / "stage"
    checked_material(stage, after, partial=journal.get("staging", False)
                     or journal.get("cleanup", False), journal=journal)
    recovery_stage = entry / "recovery_stage"
    if checked_material(recovery_stage, after, partial=journal.get("cleanup", False)):
        require(journal.get("recovering") and not target_is_after,
                "unexpected recovery stage")
    # All recovery inputs are checked before removing any installed material.
    journal["recovering"] = True
    atomic_json(entry / "journal.json", journal)
    checkpoint("after_recover_intent")
    if target_is_after:
        os.replace(target, recovery_stage)
    checkpoint("after_recover_stage")
    journal["unlinking"] = True
    atomic_json(entry / "journal.json", journal)
    if link.is_symlink():
        link.unlink()
    checkpoint("after_recover_unlink")
    if temp_link.is_symlink():
        temp_link.unlink()
    if before["tree"] is not None:
        if not target_is_before:
            os.replace(backup, target)
    checkpoint("after_recover_restore")
    if before["tree"] is not None:
        link.symlink_to(target / "bin" / executable_name())
    journal["unlinking"] = False
    atomic_json(entry / "journal.json", journal)
    checkpoint("after_recover_link")
    journal["cleanup"] = True
    atomic_json(entry / "journal.json", journal)
    checkpoint("after_recover_cleanup_intent")
    remove_material(stage)
    remove_material(recovery_stage)
    checkpoint("after_recover_cleanup")
    journal["phase"] = "recovered"
    atomic_json(entry / "journal.json", journal)
    if journal["before"]["tree"] is None:
        release_owners(home)
    remove_created(home, journal["made"])
    return {"recovered": True, "transaction": entry.name}


def recover_rollback(home, entry, journal):
    journal_preflight(entry / "journal.json")
    target, link, _ = paths(home)
    temporary_link = link.with_name(link.name + ".installer-tmp")
    require(not temporary_link.exists() and not temporary_link.is_symlink(),
            "rollback temporary link conflict")
    before, after = journal["before"]["tree"], journal["after"]
    stage, backup = entry / "rollback_stage", entry / "backup"
    require(not (entry / "stage").exists() and not (entry / "stage").is_symlink()
            and not (entry / "recovery_stage").exists()
            and not (entry / "recovery_stage").is_symlink(),
            "unexpected apply recovery material")
    target_kind = None
    if target.exists() or target.is_symlink():
        require(not target.is_symlink(), "rollback recovery target drift")
        observed = owned_snapshot(target)
        require(observed == after or (before is not None and observed == before),
                "rollback recovery target drift")
        target_kind = "after" if observed == after else "before"
    if stage.exists() or stage.is_symlink():
        require(not stage.is_symlink() and (owned_snapshot(stage) == after or
                (journal.get("cleanup") and all(after.get(k) == v
                 for k, v in owned_snapshot(stage).items()))),
                "rollback recovery stage drift")
        require(target_kind != "after", "rollback recovery duplicate target")
    else:
        require(target_kind == "after" or journal.get("cleanup"), "rollback recovery stage missing")
    if before is not None and target_kind != "before":
        require(not backup.is_symlink() and backup.is_dir()
                and owned_snapshot(backup) == before, "rollback recovery backup drift")
    else:
        require(not backup.exists() and not backup.is_symlink(), "unexpected rollback backup")
    require(not link.exists() or link.is_symlink(), "rollback recovery link drift")
    if link.is_symlink():
        require(launcher_target(link) == str(target / "bin" / executable_name()),
                "rollback recovery link drift")
    if target_kind == "after":
        os.replace(target, stage)
        checkpoint("after_rollback_stage")
    if link.is_symlink():
        link.unlink()
        checkpoint("after_rollback_unlink")
    if before is not None:
        if target_kind != "before":
            os.replace(backup, target)
    checkpoint("after_rollback_restore")
    if before is not None:
        link.symlink_to(target / "bin" / executable_name())
    checkpoint("after_rollback_link")
    journal["cleanup"] = True
    atomic_json(entry / "journal.json", journal)
    checkpoint("after_rollback_cleanup_intent")
    remove_material(stage)
    journal["phase"] = "rolled_back"
    atomic_json(entry / "journal.json", journal)
    if journal["before"]["tree"] is None:
        release_owners(home)
    remove_created(home, journal["made"])
    return {"recovered": True, "transaction": entry.name, "operation": "rollback"}


def do_rollback(home, transaction):
    parents(home)
    target, link, state = paths(home)
    temporary_link = link.with_name(link.name + ".installer-tmp")
    require(not temporary_link.exists() and not temporary_link.is_symlink(),
            "rollback temporary link conflict")
    require(not bootstrap_pending(home) and not pending(state, home),
            "pending transaction; run recover")
    committed = settled_history(home, owned(home))
    require(committed, "no committed transaction")
    entry, journal = committed[-1]
    require_owners(home)
    validate_created(home, journal)
    require(transaction is None or transaction == entry.name, "only latest transaction can roll back")
    require(owned(home)["tree"] == journal["after"], "installed payload drift")
    backup = entry / "backup"
    if journal["before"]["tree"] is not None:
        require(backup.is_dir() and owned_snapshot(backup) == journal["before"]["tree"], "backup drift")
    journal["phase"] = "pending"
    journal["operation"] = "rollback"
    atomic_json(entry / "journal.json", journal)
    result = recover_rollback(home, entry, journal)
    return {"rolled_back": True, "transaction": result["transaction"]}


def do_status(home):
    parents(home)
    target, _, state = paths(home)
    require(not bootstrap_pending(home) and not pending(state, home),
            "pending transaction; run recover")
    current = owned(home)
    settled_history(home, current)
    if current["tree"] is None:
        return {"installed": False}
    require_owners(home)
    manifest = read_json(target / "manifest.json")
    return {"installed": True, "source_revision": manifest["source_revision"],
            "payload_integrity": True, "product": PRODUCT, "host": HOST}


def do_uninstall(home):
    parents(home)
    target, link, state = paths(home)
    require(not bootstrap_pending(home) and not pending(state, home),
            "pending transaction; run recover")
    current = owned(home)
    committed = settled_history(home, current)
    if current["tree"] is None:
        release_owners(home)
        return {"uninstalled": False}
    require(committed, "unknown installation ownership")
    require_owners(home)
    expected = current["tree"]
    for entry, journal in reversed(committed):
        validate_created(home, journal)
        require(journal["after"] == expected, "installation history drift")
        expected = journal["before"]["tree"]
        if expected is not None:
            backup = entry / "backup"
            require(not backup.is_symlink() and backup.is_dir() and owned_snapshot(backup) == expected,
                    "backup drift")
    require(expected is None, "installation history has no absent preimage")
    while current["tree"] is not None:
        committed = [(p, j) for p, j in transactions(state, home) if j["phase"] == "committed"]
        require(committed and committed[-1][1]["after"] == current["tree"], "unknown installation ownership")
        do_rollback(home, committed[-1][0].name)
        current = owned(home)
    return {"uninstalled": True}


def require_creation_umask():
    if os.name != "nt":
        # This standalone CLI has no concurrent threads. Read and immediately
        # restore the mask; the temporary mask only makes creation stricter.
        current = os.umask(0o777)
        os.umask(current)
        require(not current & 0o700,
                "umask removes owner permissions; use an owner-preserving umask")


def main():
    global PRODUCT, HOST, HOST_HOME
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply", "status", "rollback", "recover", "uninstall"):
        p = sub.add_parser(command)
        p.add_argument("--home", required=True, type=Path)
        p.add_argument("--product", required=True, choices=("router", "harness"))
        p.add_argument("--host", required=True, choices=("claude", "codex"))
        p.add_argument("--host-home", type=Path)
        p.add_argument("--payload", type=Path)
        if command == "apply":
            p.add_argument("--plan", required=True, type=Path)
        if command == "rollback":
            p.add_argument("--transaction")
    args = parser.parse_args()
    try:
        require_creation_umask()  # Before even creating coordination locks.
        PRODUCT, HOST = args.product, args.host
        HOST_HOME = args.host_home.absolute() if args.host_home else None
        home = canonical_home(args.home)
        if HOST_HOME is not None:
            parents(home)  # Check spelling before collapsing symlink/.. aliases.
            HOST_HOME = Path(canonical_path(HOST_HOME))
        if args.command in ("plan", "apply"):
            require(args.payload is not None, "payload required")
        parents(home)
        if args.command == "plan":
            output = plan(home, args.payload.absolute())
        else:
            with lock(home):
                if args.command == "apply":
                    output = do_apply(home, args.payload.absolute(), read_json(args.plan))
                elif args.command == "status":
                    output = do_status(home)
                elif args.command == "rollback":
                    output = do_rollback(home, args.transaction)
                elif args.command == "uninstall":
                    output = do_uninstall(home)
                else:
                    output = do_recover(home)
        print(json.dumps(output, sort_keys=True))
    except (Refusal, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, Refusal) else "installer operation failed"}),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
