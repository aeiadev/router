"""Portable core: every POSIX-only call Router makes, each with a native Windows path.

Python 3.9+, standard library only. No other shipped Router file imports fcntl or pwd or names
os.geteuid, os.getuid, os.O_NOFOLLOW, os.O_NONBLOCK, os.pread or os.fchmod (tests/test_compat.py).

- locked(path, timeout): exclusive lock on a separate lock file, never a data file. POSIX polls
  flock LOCK_NB, Windows polls msvcrt LK_NBLCK on byte 0; past the deadline it raises LockTimeout
  and the caller skips that update.
- READ_FLAGS and open_fd(): no-follow, non-blocking, binary, close-on-exec where the platform has
  them. Without O_NOFOLLOW the opened file must match the lstat of the path and not be a link.
- owned(st): the caller owns st (always true where there is no geteuid: Windows profile ACLs
  guard the state folders instead; a stated limit). set_private() applies modes where it can.
- replace(): os.replace, retried while Windows reports the target in use.
- pread(): os.pread, or seek and read with the position restored.
- write_bytes() / write_text(): atomic, private, bytes on disk exactly as given (LF stays LF).
- home_forms() / tilde(): the home folder in every spelling a Windows or POSIX tool may print.
"""
import contextlib
import errno
import os
import stat
import tempfile
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None
try:
    import pwd
except ImportError:  # Windows
    pwd = None

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)
BINARY = getattr(os, "O_BINARY", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)
DIRECTORY = getattr(os, "O_DIRECTORY", 0)
READ_FLAGS = os.O_RDONLY | NOFOLLOW | NONBLOCK | BINARY | CLOEXEC
LOCK_POLL_SECONDS = 0.005
REPLACE_TRIES = 8
_geteuid = getattr(os, "geteuid", None)
_fchmod = getattr(os, "fchmod", None)
_pread = getattr(os, "pread", None)
CAN_FCHMOD = os.name != "nt" and _fchmod is not None


class LockTimeout(OSError):
    """The lock stayed held past the deadline; the caller skips this update."""


def _busy(exc) -> bool:
    return isinstance(exc, (BlockingIOError, PermissionError)) or getattr(exc, "errno", None) in (
        errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK)


def _try_lock(fd) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            raise OSError(errno.ENOSYS, "no file lock on this platform")
    except OSError as exc:
        if _busy(exc):
            return False
        raise
    return True


def _unlock(fd) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def locked(path, timeout=2.0):
    """Hold an exclusive lock on the lock file `path` (created 0600) for the with block."""
    fd = open_fd(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        set_private(fd, 0o600)
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise LockTimeout(errno.ETIMEDOUT, f"lock held for over {timeout:g}s", str(path))
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)


def open_fd(path, flags=READ_FLAGS, mode=0o600) -> int:
    """os.open with the no-follow, binary and close-on-exec bits; refuses a symlink either way."""
    flags |= NOFOLLOW | BINARY | CLOEXEC
    before = None
    if not NOFOLLOW and not flags & os.O_EXCL:  # O_EXCL never opens an existing link
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            pass
        if before is not None and stat.S_ISLNK(before.st_mode):
            raise OSError(errno.ELOOP, "symlinked path", str(path))
    fd = os.open(path, flags, mode)
    if not NOFOLLOW:
        try:
            opened = os.fstat(fd)
            now = os.lstat(path)
            if stat.S_ISLNK(now.st_mode) or (opened.st_ino and now.st_ino and (
                    (opened.st_dev, opened.st_ino) != (now.st_dev, now.st_ino))):
                raise OSError(errno.ELOOP, "path changed while opening", str(path))
        except BaseException:
            os.close(fd)
            raise
    return fd


def owned(st) -> bool:
    """True when the caller owns the file behind st, or the platform has no owner ids."""
    return _geteuid is None or st.st_uid == _geteuid()


def set_private(target, mode=0o600) -> bool:
    """chmod an fd or a path where the platform can; False when skipped (Windows)."""
    if isinstance(target, int):
        if not CAN_FCHMOD or _fchmod is None:
            return False
        _fchmod(target, mode)
        return True
    if os.name == "nt":
        return False
    os.chmod(target, mode)
    return True


def replace(source, target) -> None:
    """os.replace, retried while another process (or a scanner) holds the target open."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def pread(fd, size, offset) -> bytes:
    """os.pread where it exists; else seek, read and restore the position."""
    if _pread is not None:
        return _pread(fd, size, offset)
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        chunks, left = [], size
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def write_bytes(path, data, mode=0o600) -> None:
    """Replace path atomically with data, byte for byte, through a private temp file beside it."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for _ in range(100):
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
        try:
            fd = open_fd(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(errno.EEXIST, "no free temp name", str(path))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def write_text(path, text, mode=0o600) -> None:
    """UTF-8 text with its newlines as given: never CRLF on Windows (Path.write_text has no
    newline argument before Python 3.10)."""
    write_bytes(path, text.encode("utf-8"), mode)


def is_absolute(path) -> bool:
    """An absolute path string on this platform: /x on POSIX, C:\\x or C:/x as well on Windows
    (Claude Code hands Windows hooks a drive-letter cwd)."""
    return isinstance(path, str) and bool(path) and (path.startswith("/") or os.path.isabs(path))


def home() -> Path:
    """$HOME when set (POSIX, Git Bash, Claude Code on Windows), else Path.home(); Windows Python's
    Path.home() reads USERPROFILE only. A Git Bash /c/... form becomes C:/... on Windows."""
    value = os.environ.get("HOME", "")
    if not value:
        return Path.home()
    if os.name == "nt" and len(value) >= 3 and value[0] == "/" and value[1].isalpha() and value[2] == "/":
        value = f"{value[1].upper()}:{value[2:]}"
    return Path(value)


def expanduser(path) -> Path:
    """Path(path) with a leading ~ read from home(); ~user goes to Path.expanduser."""
    text = os.fspath(path)
    if text == "~":
        return home()
    if text.startswith(("~/", "~\\")):
        return home() / text[2:]
    return Path(text).expanduser()


def _slashes(value) -> str:
    return value.replace("\\", "/").rstrip("/")


def home_forms() -> list:
    """The home folder as Path.home, HOME, USERPROFILE and the password database print it, each
    also as /c/... (Git Bash) and C:/... (Windows); forward slashes, no trailing slash."""
    raw = [os.environ.get("HOME", ""), os.environ.get("USERPROFILE", "")]
    with contextlib.suppress(RuntimeError, KeyError, OSError):
        raw.append(str(Path.home()))
    if pwd is not None and _geteuid is not None:
        with contextlib.suppress(KeyError, OSError):
            raw.append(pwd.getpwuid(_geteuid()).pw_dir)
    forms = []
    for value in raw:
        value = _slashes(value.strip())
        if not value:
            continue
        variants = [value]
        if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
            variants.append(f"/{value[0].lower()}{value[2:]}")
        elif len(value) >= 3 and value[0] == "/" and value[1].isalpha() and value[2] == "/":
            variants.append(f"{value[1].upper()}:{value[2:]}")
        forms += [item for item in variants if item and item not in forms]
    return forms


def tilde(text, forms=None) -> str:
    """text with a leading home folder (any form, any case, either slash) written as ~."""
    forms = home_forms() if forms is None else forms
    folded = text.replace("\\", "/").casefold()
    for form in sorted(forms, key=len, reverse=True):
        key = form.casefold()
        if key and (folded == key or folded.startswith(key + "/")):
            return "~" + text[len(form):]
    return text


def state_is_absolute(path) -> bool:
    """An absolute state directory; UNC paths need a server and a nonempty share."""
    text = str(path).replace("\\", "/")
    if os.name == "nt" and text.startswith("//"):
        parts = text[2:].split("/")
        if len(parts) < 2 or not all(parts[:2]):
            return False
    return Path(path).is_absolute()
