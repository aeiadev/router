"""Simulate native Windows Python on Linux or macOS: PYTHONPATH=tests/win_sim python3 tests/x.py

On import (site runs this at startup) it hides the POSIX-only modules fcntl and pwd, deletes the
os names Windows Python lacks, and installs a fake msvcrt whose locking() really excludes a second
holder using host flock on a permanent sidecar per byte. Separate opens conflict even within
one process, overlapping ranges share sidecars, and the kernel releases locks on process exit.
Sidecars are never unlinked: replacing one would split contenders across different inodes.
This models explicit lock/unlock and crash release, not implicit unlock on closing a target fd.
Child processes inherit PYTHONPATH and so run simulated too.
"""
import errno
import os
import sys
import tempfile
import types

sys.dont_write_bytecode = True
ACTIVE = os.name != "nt"  # on real Windows this file changes nothing
if ACTIVE:
    import fcntl as _host_fcntl  # Capture privately before hiding it from product imports.
_MARKERS = os.path.join(tempfile.gettempdir(),
                       "win-sim-flock-{}".format(os.getuid() if hasattr(os, "getuid") else 0))
for _name in ("fcntl", "pwd") if ACTIVE else ():
    sys.modules[_name] = None  # `import fcntl` now raises ImportError
for _name in ("geteuid", "getuid", "O_NOFOLLOW", "O_NONBLOCK", "pread", "fchmod") if ACTIVE else ():
    if hasattr(os, _name):
        delattr(os, _name)

_HELD = {}  # (device, inode, offset, size) -> (target fd, private lock fds)


def _key(fd, size):
    if size <= 0:
        raise ValueError("lock size must be positive")
    info = os.fstat(fd)
    return info.st_dev, info.st_ino, os.lseek(fd, 0, os.SEEK_CUR), size


def _try(fd, key):
    if key in _HELD:
        return False
    os.makedirs(_MARKERS, exist_ok=True)
    device, inode, offset, size = key
    acquired = []
    try:
        for byte in range(offset, offset + size):
            path = os.path.join(_MARKERS, "{}-{}-{}".format(device, inode, byte))
            lock_fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            acquired.append(lock_fd)
            _host_fcntl.flock(lock_fd, _host_fcntl.LOCK_EX | _host_fcntl.LOCK_NB)
    except BaseException as exc:
        # Closing only our private descriptors rolls back any partial range.
        for lock_fd in acquired:
            os.close(lock_fd)
        if isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise
    _HELD[key] = (fd, acquired)
    return True


def locking(fd, mode, nbytes):
    key = _key(fd, nbytes)
    if mode == msvcrt.LK_UNLCK:
        held = _HELD.get(key)
        if held is None or held[0] != fd:
            raise PermissionError(errno.EACCES, "region not locked by this descriptor")
        for lock_fd in held[1]:
            os.close(lock_fd)
        del _HELD[key]
        return
    if mode in (msvcrt.LK_LOCK, msvcrt.LK_RLCK):
        # Real LK_LOCK retries for 10 seconds and then raises; Router must never use it.
        raise OSError(errno.EDEADLK, "win_sim: LK_LOCK is banned, use LK_NBLCK")
    if mode not in (msvcrt.LK_NBLCK, msvcrt.LK_NBRLCK):
        raise ValueError("bad locking mode")
    if not _try(fd, key):
        raise PermissionError(errno.EACCES, "Permission denied")


msvcrt = types.ModuleType("msvcrt")
msvcrt.LK_UNLCK, msvcrt.LK_LOCK, msvcrt.LK_NBLCK, msvcrt.LK_RLCK, msvcrt.LK_NBRLCK = 0, 1, 2, 3, 4
msvcrt.locking = locking
msvcrt.WIN_SIM = True
if ACTIVE:
    import subprocess  # noqa: F401  (3.12+ picks its Windows code when msvcrt imports; load it first)
    import shutil
    shutil._use_fd_functions = False  # as on Windows: rmtree without os.open(O_NONBLOCK)
    sys.modules["msvcrt"] = msvcrt
