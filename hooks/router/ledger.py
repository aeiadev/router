"""Router ledger: ladder attempts, spawned lanes and judge verdicts in one SQLite file.

This module owns all ledger SQL. The file is ${ROUTER_STATE:-<xdg>/claude-router}/ledger.sqlite3,
where xdg is XDG_STATE_HOME when set and absolute, else ~/.local/state. The file is 0600 in a 0700
directory and carries PRAGMA user_version = 1. The 0.2 attempts.sqlite3 beside it is never opened,
migrated or deleted: hooks of a 0.2 install on the same host keep using it.

Privacy: rows hold hashes, role and tier names, times, counts and a short scrubbed label. Never
prompt, BAR, RETURN or finding text.

Failures: hook-facing calls never raise on a sqlite or parse error. They log one line with the
ledger path to errors.jsonl and return a safe default (None, False or an empty list), so the hook
allows the call. Calls made for the router CLI (ladders, reset_ladder) raise, so the CLI can say why.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path

import common

FILE_NAME = "ledger.sqlite3"
PRUNE_STAMP = "ledger.pruned"  # mtime = last prune; the C3 tables stay exactly as listed
SCHEMA_VERSION = 1
BUSY_SECONDS = 10.0
HOOK_BUSY_SECONDS = 2.0
SPOOL_NAME = "ledger.spool"
SPOOL_MAX_BYTES = 64 * 1024
SPOOL_MAX_LINES = 100
DAY = 86400
RETENTION = (("lanes", "started", 7), ("attempts", "ts", 30), ("verdicts", "ts", 90))  # days
LANE_STATUSES = ("running", "returned", "judged")
VERDICTS = ("PASS", "SEND_BACK", "NOT_DONE", "FAIL", "CANNOT_COMPLETE", "UNKNOWN")
LANE_FIELDS = ("session", "brief", "key", "project", "role", "tier", "label", "status", "started", "ended")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS attempts (key TEXT NOT NULL, round INTEGER NOT NULL, tier TEXT, role TEXT,"
    " project TEXT, session TEXT, ts REAL NOT NULL, PRIMARY KEY (key, round))",
    "CREATE TABLE IF NOT EXISTS lanes (session TEXT, brief TEXT, key TEXT, project TEXT, role TEXT, tier TEXT,"
    " label TEXT, status TEXT NOT NULL CHECK (status IN ('running', 'returned', 'judged')),"
    " started REAL NOT NULL, ended REAL, PRIMARY KEY (session, brief, started))",
    "CREATE TABLE IF NOT EXISTS verdicts (key TEXT, project TEXT, session TEXT, ts REAL NOT NULL,"
    " verdict TEXT NOT NULL CHECK (verdict IN ('PASS', 'SEND_BACK', 'NOT_DONE', 'FAIL', 'CANNOT_COMPLETE',"
    " 'UNKNOWN')), findings INTEGER, round INTEGER)",
    "CREATE INDEX IF NOT EXISTS attempts_ts ON attempts (ts)",
    "CREATE INDEX IF NOT EXISTS lanes_started ON lanes (started)",
    "CREATE INDEX IF NOT EXISTS verdicts_key ON verdicts (key, ts)",
)


class LedgerError(ValueError):
    """The ledger file exists but this code cannot use it (for example a newer schema)."""


FAILURES = (sqlite3.Error, OSError, ValueError)


def ledger_path() -> Path:
    """The ledger location, without creating anything."""
    return common.state_path() / FILE_NAME


_MODE_WARNED = set()  # str(path) for mode lines; (str(spool), "busy" | "full" | "unsafe") for spool lines


def _private_mode(path, mode):
    """Tighten only owned, nonsymlink paths, without following a replacement symlink."""
    try:
        info = os.lstat(path)
        if info.st_uid != os.geteuid() or stat.S_ISLNK(info.st_mode):
            raise OSError("unowned or symlinked path")
        if stat.S_IMODE(info.st_mode) != mode:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            if stat.S_ISDIR(info.st_mode):
                flags |= getattr(os, "O_DIRECTORY", 0)
            fd = os.open(path, flags)
            try:
                os.fchmod(fd, mode)
            finally:
                os.close(fd)
    except (OSError, ValueError) as exc:
        if str(path) not in _MODE_WARNED:
            _MODE_WARNED.add(str(path))
            _failed(path, exc, "mode")


def _private_file(path: Path) -> None:
    """Create the ledger file and tighten existing owned state paths."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_mode(path.parent, 0o700)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
    for suffix in ("", "-wal", "-shm", "-journal"):
        item = Path(str(path) + suffix)
        if item.exists() or item.is_symlink():
            _private_mode(item, 0o600)
    spool = path.with_name(SPOOL_NAME)
    for item in [spool] + _claim_files(spool):
        # An unsafe spool is never opened here (a fifo would block); _replay and _spool log it.
        if (item.exists() or item.is_symlink()) and not _unsafe_spool(item):
            _private_mode(item, 0o600)


def connect(path=None, timeout=BUSY_SECONDS, *, hook=False) -> sqlite3.Connection:
    """Open (and on first use create) the ledger in autocommit mode; callers BEGIN themselves.

    Raises sqlite3.Error, OSError or LedgerError. Hook code goes through the wrappers below.
    """
    path = Path(path) if path is not None else ledger_path()
    _private_file(path)
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                for statement in SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise LedgerError(f"unsupported ledger version {version} (this Router reads {SCHEMA_VERSION})")
        if hook:
            _replay(conn, path)
        _private_file(path)
    except BaseException:
        conn.close()
        raise
    return conn


def _failed(path, exc, action) -> None:
    """One log line naming the ledger path; sqlite messages never carry row values."""
    detail = str(exc)[:200] if isinstance(exc, (sqlite3.Error, LedgerError)) else None
    common.log("errors", {"script": "ledger", "action": action, "error": type(exc).__name__,
                          "detail": detail, "path": str(path)})


def _locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _spool_path(path):
    return Path(path).with_name(SPOOL_NAME)


def _unsafe_spool(spool, action=None):
    """True when the spool path holds anything but a regular file owned by the caller (a symlink,
    directory, fifo or foreign file). Callers then leave it unopened. With an action, log one line
    per path per process, shared by the writer and replay sides."""
    try:
        info = os.lstat(spool)
    except FileNotFoundError:
        return False
    except OSError:
        info = None
    if info is not None and info.st_uid == os.geteuid() and stat.S_ISREG(info.st_mode):
        return False
    if action is not None and (str(spool), "unsafe") not in _MODE_WARNED:
        _MODE_WARNED.add((str(spool), "unsafe"))
        _failed(spool, LedgerError("unsafe spool path"), action)
    return True


def _spool(path, item):
    spool = _spool_path(path)
    line = (json.dumps(item, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    try:
        import fcntl
        for _ in range(5):
            if _unsafe_spool(spool, "spool"):
                return
            fd = os.open(spool, os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                opened = os.fstat(fd)
                try:
                    current = os.stat(spool, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    continue
                if opened.st_uid != os.geteuid() or not stat.S_ISREG(opened.st_mode):
                    raise OSError("unowned or special spool path")
                os.fchmod(fd, 0o600)
                lines = os.pread(fd, SPOOL_MAX_BYTES + 1, 0).count(b"\n")
                if opened.st_size + len(line) > SPOOL_MAX_BYTES or lines >= SPOOL_MAX_LINES:
                    if (str(spool), "full") not in _MODE_WARNED:
                        _MODE_WARNED.add((str(spool), "full"))
                        _failed(spool, LedgerError("spool full"), "spool")
                else:
                    os.write(fd, line)
                return
            finally:
                os.close(fd)
        if (str(spool), "busy") not in _MODE_WARNED:
            _MODE_WARNED.add((str(spool), "busy"))
            _failed(spool, LedgerError("spool busy"), "spool")
    except OSError as exc:
        _failed(spool, exc, "spool")


def _claim_files(spool):
    """Replay claims next to the spool: unique claim names plus the legacy fixed name."""
    found = [spool.with_name(spool.name + ".replay")]
    with contextlib.suppress(OSError):
        found.extend(sorted(spool.parent.glob(spool.name + ".replay.*")))
    return found


def _read_claim(claim):
    """The claim bytes, None when it vanished, or an OSError for an unsafe file."""
    try:
        info = os.lstat(claim)
    except FileNotFoundError:
        return None
    if info.st_uid != os.geteuid() or not stat.S_ISREG(info.st_mode):
        raise OSError("unowned, symlinked or special claim file")
    try:
        fd = os.open(claim, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    try:
        opened = os.fstat(fd)
        current = os.stat(claim, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise OSError("claim path changed while opening")
        if opened.st_uid != os.geteuid() or not stat.S_ISREG(opened.st_mode):
            raise OSError("unowned or special claim file")
        return os.read(fd, SPOOL_MAX_BYTES + 1)
    finally:
        os.close(fd)


def _replay(conn, path):
    """Apply the spool once. Each claim file is recorded in `replayed` in the transaction that applies
    it, so a second hook (or a retry after a crash before the unlink) skips an applied claim."""
    spool = _spool_path(path)
    if not spool.exists() and not spool.is_symlink() and not any(
            item.exists() or item.is_symlink() for item in _claim_files(spool)):
        return
    # Claim and read under the spool inode lock, before waiting for SQLite. An unsafe spool path
    # costs one log line; the database and any valid claim files are still applied.
    unsafe = _unsafe_spool(spool, "replay")
    try:
        fd = None if unsafe else os.open(spool, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        fd = None
    if fd is not None:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
            opened = os.fstat(fd)
            try:
                current = os.stat(spool, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current and (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino):
                if opened.st_uid != os.geteuid() or not stat.S_ISREG(opened.st_mode):
                    raise OSError("unowned or special spool path")
                claim = spool.with_name(f"{spool.name}.replay.{uuid.uuid4().hex}")
                os.rename(spool, claim)
                _read_claim(claim)
        finally:
            os.close(fd)
    conn.execute("BEGIN IMMEDIATE")
    try:
        claims = [item for item in _claim_files(spool) if item.exists() or item.is_symlink()]
        done = []
        dropped = []
        if claims:
            conn.execute("CREATE TABLE IF NOT EXISTS replayed (claim TEXT PRIMARY KEY, ts REAL NOT NULL)")
            conn.execute("DELETE FROM replayed WHERE ts < ?", (time.time() - 30 * DAY,))
        for claim in claims:
            try:
                data = _read_claim(claim)
            except OSError as exc:
                if str(claim) not in _MODE_WARNED:
                    _MODE_WARNED.add(str(claim))
                    _failed(claim, exc, "replay")
                continue
            done.append(claim)
            if data is None:
                continue
            claim_id = claim.name + ":" + hashlib.sha256(data).hexdigest()
            if conn.execute("SELECT 1 FROM replayed WHERE claim = ?", (claim_id,)).fetchone():
                continue
            dropped.append((claim, _apply_lines(conn, data)))
            conn.execute("INSERT INTO replayed VALUES (?, ?)", (claim_id, time.time()))
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise
    for claim in done:
        claim.unlink(missing_ok=True)
    for claim, bad in dropped:
        if bad:
            _failed(claim, LedgerError(f"dropped {bad} invalid spool lines"), "replay")


def _apply_lines(conn, data):
    """Apply spool lines; returns how many were invalid and dropped."""
    bad = 0
    for raw in data.splitlines():
        try:
            item = json.loads(raw)
            if item["op"] == "verdict" and item["verdict"] in VERDICTS:
                conn.execute("INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (item["key"], item.get("project"), item.get("session"), item["ts"],
                              item["verdict"], item.get("findings"), item.get("round")))
            elif item["op"] == "judged":
                row = conn.execute("SELECT key, project, ended FROM lanes WHERE session IS ? AND brief IS ?"
                                   " ORDER BY started DESC LIMIT 1", (item["session"], item["brief"])).fetchone()
                if row:
                    conn.execute("UPDATE lanes SET status = 'judged', ended = COALESCE(ended, ?)"
                                 " WHERE session IS ? AND brief IS ? AND key IS ?",
                                 (item["ts"], item["session"], item["brief"], row[0]))
                    conn.execute("INSERT INTO verdicts VALUES (?, ?, ?, ?, 'UNKNOWN', NULL, NULL)",
                                 (row[0], row[1], item["session"], item["ts"]))
            elif item["op"] == "judge" and item["verdict"] in VERDICTS:
                _judge_in_tx(conn, item["session"], item["brief"], item["verdict"],
                             item["findings"], item["ts"])
            else:
                bad += 1
        except (ValueError, KeyError, TypeError, sqlite3.Error):
            bad += 1
    return bad


def _rollback(conn) -> None:
    with contextlib.suppress(sqlite3.Error):
        if conn.in_transaction:
            conn.execute("ROLLBACK")


@contextlib.contextmanager
def _open(path, action, create, on_locked=None):
    """Yield a connection, or None when the ledger is absent (and create is False) or broken."""
    path = Path(path) if path is not None else ledger_path()
    if not create and not path.exists():
        yield None
        return
    try:
        conn = connect(path, timeout=HOOK_BUSY_SECONDS, hook=True)
    except FAILURES as exc:
        _failed(path, exc, action)
        if on_locked is not None and _locked(exc):
            on_locked(path)
        yield None
        return
    try:
        yield conn
    finally:
        conn.close()


def _db_file(conn) -> Path:
    return Path(next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"))


# Ladder attempts and lanes

def load_prior(conn, key, now=None, ttl_hours=12) -> list:
    """Accepted attempts for key newer than the ladder TTL, oldest first; decide_spawn's prior."""
    if not key:
        return []
    now = time.time() if now is None else now
    rows = conn.execute("SELECT tier FROM attempts WHERE key = ? AND round > 0 AND ts > ? ORDER BY round",
                        (key, now - ttl_hours * 3600))
    return [{"tier": row[0]} for row in rows]


ARCHIVE_BASE = 1000000000  # archived attempts hold round = -(ARCHIVE_BASE + rowid); live rounds are > 0


def _archive(conn, where, args) -> int:
    """Move live attempts matching where out of the ladder; prune() deletes them at 30 days."""
    return conn.execute(f"UPDATE attempts SET round = -({ARCHIVE_BASE} + rowid) WHERE round > 0 AND {where}",
                        args).rowcount


def _expire(conn, key, cutoff) -> None:
    """Archive attempts for key at or past the TTL and number the live rest 1..n, so round n+1 is free."""
    _archive(conn, "key = ? AND ts <= ?", (key, cutoff))
    rounds = [row[0] for row in conn.execute("SELECT round FROM attempts WHERE key = ? AND round > 0 ORDER BY round",
                                             (key,))]
    if rounds != list(range(1, len(rounds) + 1)):
        for index, old in enumerate(rounds, 1):
            conn.execute("UPDATE attempts SET round = ? WHERE key = ? AND round = ?", (-index, key, old))
        conn.execute("UPDATE attempts SET round = -round WHERE key = ? AND round < 0 AND round > ?",
                     (key, -ARCHIVE_BASE))


def _lane_start(conn, session, brief, now) -> float:
    """now, nudged past the newest lane of the same session and brief to keep the key unique."""
    latest = conn.execute("SELECT MAX(started) FROM lanes WHERE session IS ? AND brief IS ?",
                          (session, brief)).fetchone()[0]
    return now if latest is None or latest < now else latest + 1e-6


def record_attempt_and_lane(key, choose, *, role, project=None, session=None, brief=None, label=None,
                            ttl_hours=12, now=None, path=None):
    """Decide under one write lock; an accepted spawn adds its attempt and a running lane together.

    choose(prior) receives the attempts for key newer than ttl_hours and returns the decision
    dict. A block writes nothing. Returns the decision, or None after a logged ledger failure;
    the caller then allows the call unchanged.
    """
    now = time.time() if now is None else float(now)
    path = Path(path) if path is not None else ledger_path()
    try:
        conn = connect(path, timeout=HOOK_BUSY_SECONDS, hook=True)
    except FAILURES as exc:
        _failed(path, exc, "connect")
        return None
    deciding = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = load_prior(conn, key, now, ttl_hours)
        deciding = True
        result = choose(prior)
        deciding = False
        if result.get("decision") != "block":
            _expire(conn, key, now - ttl_hours * 3600)
            tier = result.get("tier")
            conn.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (key, len(prior) + 1, tier, role, project, session, now))
            conn.execute("INSERT INTO lanes VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL)",
                         (session, brief, key, project, role, tier, label, _lane_start(conn, session, brief, now)))
        conn.execute("COMMIT")
    except FAILURES as exc:
        _rollback(conn)
        conn.close()
        if deciding:
            raise
        _failed(path, exc, "record")
        return None
    except BaseException:
        _rollback(conn)
        conn.close()
        raise
    try:
        maybe_prune(conn, now)
    except FAILURES as exc:
        _rollback(conn)
        _failed(path, exc, "prune")
    finally:
        conn.close()
    return result


def mark_lane(session, brief, status, *, now=None, path=None):
    """Set the newest lane of this session and brief to status; returns that lane as a dict or None.

    Leaving running stamps ended once; judged keeps the time the lane returned.
    """
    if status not in LANE_STATUSES:
        raise ValueError(f"lane status must be one of {', '.join(LANE_STATUSES)}")
    now = time.time() if now is None else float(now)
    def queue(ledger_file):
        if status == "judged":
            _spool(ledger_file, {"op": "judged", "session": session, "brief": brief, "ts": now})

    with _open(path, "mark_lane", create=False, on_locked=queue) as conn:
        if conn is None:
            return None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT {', '.join(LANE_FIELDS)} FROM lanes WHERE session IS ? AND brief IS ?"
                               " ORDER BY started DESC LIMIT 1", (session, brief)).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            lane = dict(zip(LANE_FIELDS, row))
            ended = None if status == "running" else (lane["ended"] if lane["ended"] is not None else now)
            conn.execute("UPDATE lanes SET status = ?, ended = ? WHERE session IS ? AND brief IS ? AND started = ?",
                         (status, ended, session, brief, lane["started"]))
            conn.execute("COMMIT")
        except FAILURES as exc:
            _rollback(conn)
            _failed(path or ledger_path(), exc, "mark_lane")
            if _locked(exc):
                queue(path or ledger_path())
            return None
        return dict(lane, status=status, ended=ended)


def _judge_in_tx(conn, session, brief, verdict, findings, now):
    row = conn.execute(f"SELECT {', '.join(LANE_FIELDS)} FROM lanes WHERE session IS ? AND brief IS ?"
                       " ORDER BY started DESC LIMIT 1", (session, brief)).fetchone()
    if row is None:
        return None
    lane = dict(zip(LANE_FIELDS, row))
    ended = lane["ended"] if lane["ended"] is not None else now
    conn.execute("UPDATE lanes SET status = 'judged', ended = ? WHERE session IS ? AND brief IS ? AND started = ?",
                 (ended, session, brief, lane["started"]))
    round = conn.execute("SELECT COUNT(*) FROM attempts WHERE key = ? AND round > 0 AND ts <= ?",
                         (lane["key"], lane["started"])).fetchone()[0] or None
    conn.execute("INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (lane["key"], lane["project"], session, now, verdict, findings, round))
    return dict(lane, status="judged", ended=ended)


def judge_lane(session, brief, verdict, findings, *, now=None, path=None):
    """Judge the newest matching lane and store its verdict in one transaction."""
    verdict = verdict if verdict in VERDICTS else "UNKNOWN"
    findings = findings if type(findings) is int else None
    now = time.time() if now is None else float(now)

    def queue(ledger_file):
        _spool(ledger_file, {"op": "judge", "session": session, "brief": brief,
                             "verdict": verdict, "findings": findings, "ts": now})

    with _open(path, "judge_lane", create=False, on_locked=queue) as conn:
        if conn is None:
            return None
        try:
            conn.execute("BEGIN IMMEDIATE")
            lane = _judge_in_tx(conn, session, brief, verdict, findings, now)
            conn.execute("COMMIT")
            return lane
        except FAILURES as exc:
            _rollback(conn)
            _failed(path or ledger_path(), exc, "judge_lane")
            if _locked(exc):
                queue(path or ledger_path())
            return None


def lanes_for_session(session, *, limit=None, path=None) -> list:
    """Lanes of one session, newest first, as dicts; [] when there are none or the ledger fails."""
    with _open(path, "lanes_for_session", create=False) as conn:
        if conn is None:
            return []
        try:
            rows = conn.execute(f"SELECT {', '.join(LANE_FIELDS)} FROM lanes WHERE session IS ?"
                                " ORDER BY started DESC LIMIT ?", (session, -1 if limit is None else int(limit)))
            return [dict(zip(LANE_FIELDS, row)) for row in rows]
        except FAILURES as exc:
            _failed(path or ledger_path(), exc, "lanes_for_session")
            return []


REPORT_FIELDS = LANE_FIELDS + ("round", "verdict")
_REPORT_SQL = (
    f"SELECT {', '.join('l.' + field for field in LANE_FIELDS)},"
    " NULLIF((SELECT COUNT(*) FROM attempts a WHERE a.key = l.key AND a.round > 0 AND a.ts <= l.started), 0),"
    " (SELECT v.verdict FROM verdicts v WHERE v.key = l.key AND v.ts >= l.started"
    " ORDER BY v.ts DESC, v.rowid DESC LIMIT 1)"
    " FROM lanes l{where} ORDER BY l.started DESC, l.rowid DESC LIMIT ?")


def _report_rows(conn, session, project, since, limit) -> list:
    clauses, args = [], []
    for clause, value in (("l.session IS ?", session), ("l.project IS ?", project)):
        if value is not None:
            clauses.append(clause)
            args.append(value)
    if since is not None:
        clauses.append("l.started >= ?")
        args.append(float(since))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(_REPORT_SQL.format(where=where), (*args, -1 if limit is None else int(limit)))
    return [dict(zip(REPORT_FIELDS, row)) for row in rows]


def lanes_report(session=None, project=None, since=None, limit=None, path=None, *, strict=False) -> list:
    """Lanes newest first, filtered by session, project and start time, as dicts of REPORT_FIELDS.

    round counts the attempts for the lane's key made at or before it started (None when the key has
    none left); verdict is the newest verdict for that key at or after it started, else None. A missing
    ledger gives [] and is never created. A failing ledger is logged and gives [], unless strict (the
    router CLI), which raises so the command can say why.
    """
    path = Path(path) if path is not None else ledger_path()
    if strict:
        if not path.exists():
            return []
        with contextlib.closing(connect(path)) as conn:
            return _report_rows(conn, session, project, since, limit)
    with _open(path, "lanes_report", create=False) as conn:
        if conn is None:
            return []
        try:
            return _report_rows(conn, session, project, since, limit)
        except FAILURES as exc:
            _failed(path, exc, "lanes_report")
            return []


# Verdicts

def record_verdict(key, verdict, *, findings=0, project=None, session=None, round=None, now=None, path=None) -> bool:
    """Store one judge verdict; findings is a count, never text. False after a logged failure."""
    verdict = verdict if verdict in VERDICTS else "UNKNOWN"
    findings = findings if isinstance(findings, int) and not isinstance(findings, bool) else None
    round = round if isinstance(round, int) and not isinstance(round, bool) else None
    now = time.time() if now is None else float(now)
    def queue(ledger_file):
        _spool(ledger_file, {"op": "verdict", "key": key, "project": project, "session": session,
                             "ts": now, "verdict": verdict, "findings": findings, "round": round})

    with _open(path, "record_verdict", create=True, on_locked=queue) as conn:
        if conn is None:
            return False
        try:
            conn.execute("INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (key, project, session, now, verdict, findings, round))
        except FAILURES as exc:
            _failed(path or ledger_path(), exc, "record_verdict")
            if _locked(exc):
                queue(path or ledger_path())
            return False
    return True


def latest_verdict(key, *, path=None):
    """The newest verdict for key as a dict (verdict, ts, findings, round, session, project), or None."""
    with _open(path, "latest_verdict", create=False) as conn:
        if conn is None:
            return None
        try:
            row = conn.execute("SELECT verdict, ts, findings, round, session, project FROM verdicts"
                               " WHERE key = ? ORDER BY ts DESC, rowid DESC LIMIT 1", (key,)).fetchone()
        except FAILURES as exc:
            _failed(path or ledger_path(), exc, "latest_verdict")
            return None
    return dict(zip(("verdict", "ts", "findings", "round", "session", "project"), row)) if row else None


# Retention

def prune(conn, now=None) -> dict:
    """Delete lanes over 7 days, attempts over 30 and verdicts over 90; returns rows removed per table."""
    now = time.time() if now is None else now
    removed = {}
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    for table, column, days in RETENTION:
        removed[table] = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (now - days * DAY,)).rowcount
    if own:
        conn.execute("COMMIT")
    return removed


def maybe_prune(conn, now=None) -> bool:
    """Prune when the last prune is a day old or more (or never ran); True when it pruned."""
    now = time.time() if now is None else now
    stamp = _db_file(conn).with_name(PRUNE_STAMP)
    try:
        last = stamp.stat().st_mtime
    except OSError:
        last = None
    if last is not None and 0 <= now - last < DAY:
        return False
    prune(conn, now)
    os.close(os.open(stamp, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600))
    os.utime(stamp, (now, now))
    return True


# Router CLI (these raise; the command reports the error)

def ladders(now=None, ttl_hours=12, path=None) -> list:
    """Live ladders: key, round (attempts inside the TTL), newest tier and its age in seconds."""
    path = Path(path) if path is not None else ledger_path()
    if not path.exists():
        return []
    now = time.time() if now is None else now
    with contextlib.closing(connect(path)) as conn:
        rows = conn.execute(
            "SELECT key, COUNT(*), MAX(ts) FROM attempts WHERE round > 0 AND ts > ? GROUP BY key ORDER BY MAX(ts) DESC",
            (now - ttl_hours * 3600,)).fetchall()
        result = []
        for key, count, newest in rows:
            tier = conn.execute("SELECT tier FROM attempts WHERE key = ? AND round > 0 ORDER BY ts DESC, round DESC LIMIT 1",
                                (key,)).fetchone()[0]
            result.append({"key": key, "round": count, "tier": tier, "age": max(0.0, now - newest)})
    return result


def reset_ladder(key, path=None) -> int:
    """Archive every live attempt for key, so its next spawn is round 1; returns the rows archived."""
    path = Path(path) if path is not None else ledger_path()
    if not path.exists():
        return 0
    with contextlib.closing(connect(path)) as conn:
        with conn:
            return _archive(conn, "key = ?", (key,))


def report_rows(since, path=None) -> dict:
    """Read-only rows for `router report`; never creates the ledger, raises on a broken one.

    attempts: (key, role, tier, ts) with ts >= since, archived rows included; lanes: (key, role, tier,
    status) started at or after since; verdicts: (key, ts, verdict, round, tier) for non-UNKNOWN
    verdicts at or after since in time order, tier being that of the key's newest lane started before
    the verdict (None when the lane is gone); unknown: the count of UNKNOWN verdicts in the window.
    No label, brief or finding is ever selected.
    """
    path = Path(path) if path is not None else ledger_path()
    empty = {"attempts": [], "lanes": [], "verdicts": [], "unknown": 0}
    if not path.exists():
        return empty
    since = float(since)
    with contextlib.closing(connect(path)) as conn:
        return {
            "attempts": [tuple(row) for row in conn.execute(
                "SELECT key, role, tier, ts FROM attempts WHERE ts >= ? ORDER BY ts", (since,))],
            "lanes": [tuple(row) for row in conn.execute(
                "SELECT key, role, tier, status FROM lanes WHERE started >= ? ORDER BY started", (since,))],
            "verdicts": [tuple(row) for row in conn.execute(
                "SELECT v.key, v.ts, v.verdict, v.round, (SELECT l.tier FROM lanes l WHERE l.key = v.key"
                " AND l.started <= v.ts ORDER BY l.started DESC, l.rowid DESC LIMIT 1)"
                " FROM verdicts v WHERE v.ts >= ? AND v.verdict != 'UNKNOWN' ORDER BY v.ts, v.rowid", (since,))],
            "unknown": conn.execute("SELECT COUNT(*) FROM verdicts WHERE ts >= ? AND verdict = 'UNKNOWN'",
                                    (since,)).fetchone()[0],
        }
