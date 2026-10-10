#!/usr/bin/env python3
"""Ledger regressions: schema, file modes, concurrency, failure handling, pruning and privacy.

All state lives in temporary directories. Nothing here calls a model or the network.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "hooks" / "router"
sys.path.insert(0, str(ROUTER))
import ledger  # noqa: E402

GUARD = ROUTER / "spawn_guard.py"
ROUTES = ROUTER / "routes.json"
BRIEF = "TASK add a text helper\nFILES src/text.py\nBAR run unit tests\nRETURN five lines"
COLUMNS = {
    "attempts": ["key", "round", "tier", "role", "project", "session", "ts"],
    "lanes": ["session", "brief", "key", "project", "role", "tier", "label", "status", "started", "ended"],
    "verdicts": ["key", "project", "session", "ts", "verdict", "findings", "round"],
}
PRIMARY_KEYS = {"attempts": ["key", "round"], "lanes": ["session", "brief", "started"], "verdicts": []}
DAY = 86400


def ledger_dump(path) -> str:
    """The ledger as SQL text, the way a person inspecting it would see it."""
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return "\n".join(conn.iterdump())


def assert_absent_from_ledger(case, path, *sentinels):
    """Fail when a sentinel is in the ledger dump or in any raw state file next to it.

    Shared privacy check: 3R4 reuses it for the judge-return half of the sentinel test.
    """
    path = Path(path)
    dump = ledger_dump(path)
    case.assertIn("INSERT INTO", dump, f"{path} holds no rows, so the privacy check proves nothing")
    for sentinel in sentinels:
        case.assertNotIn(sentinel, dump, f"{sentinel!r} is stored in the ledger dump of {path}")
        for item in path.parent.rglob("*"):
            if item.is_file():
                case.assertNotIn(sentinel.encode(), item.read_bytes(), f"{sentinel!r} is stored in {item}")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-ledger-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.db = self.state / "ledger.sqlite3"
        self.project = self.root / "project"
        (self.project / ".git").mkdir(parents=True)
        (self.root / "routes.local.json").write_text("{}", encoding="utf-8")
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("ROUTER_") and key not in ("ROUTES_JSON", "XDG_STATE_HOME")}
        self.environment.update({
            "HOME": str(self.root / "home"),
            "CLAUDE_HOME": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "ROUTER_HOME": str(self.root / "config"),
            "ROUTER_STATE": str(self.state),
            "ROUTER_OFF_FILE": str(self.root / "OFF"),
            "ROUTES_JSON": str(ROUTES),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "ROUTER_LOCAL": str(self.root / "routes.local.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        isolated = patch.dict(os.environ, self.environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)

    def hook(self, prompt=BRIEF, agent="builder", description="Implement the helper", session="session-one"):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(self.project),
                 "session_id": session,
                 "tool_input": {"subagent_type": agent, "prompt": prompt, "description": description}}
        return subprocess.run([sys.executable, "-B", str(GUARD)], input=json.dumps(event),
                              text=True, capture_output=True, timeout=20, cwd=self.root, env=self.environment)

    def rows(self, sql, *args):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, args).fetchall()

    def test_schema_tables_columns_and_user_version(self):
        conn = ledger.connect(self.db)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertEqual(tables, set(COLUMNS), "the ledger must hold exactly the C3 tables")
            for table, columns in COLUMNS.items():
                info = conn.execute(f"PRAGMA table_info({table})").fetchall()
                self.assertEqual([row[1] for row in info], columns, table)
                keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
                self.assertEqual(keys, PRIMARY_KEYS[table], f"{table} primary key")
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertEqual(ledger.ledger_path(), self.db)

    def test_ledger_path_follows_router_state_then_absolute_xdg(self):
        os.environ.pop("ROUTER_STATE")
        home = self.root / "home"
        self.assertEqual(ledger.ledger_path(), home / ".local/state/claude-router/ledger.sqlite3")
        os.environ["XDG_STATE_HOME"] = str(self.root / "xdg")
        self.assertEqual(ledger.ledger_path(), self.root / "xdg/claude-router/ledger.sqlite3")
        os.environ["XDG_STATE_HOME"] = "relative/xdg"
        self.assertEqual(ledger.ledger_path(), home / ".local/state/claude-router/ledger.sqlite3",
                         "a relative XDG_STATE_HOME must be ignored")
        self.assertFalse(home.exists(), "path resolution created directories")

    def test_spawn_creates_private_file_and_directory_under_a_permissive_umask(self):
        previous = os.umask(0o000)
        try:
            result = self.hook()
        finally:
            os.umask(previous)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.db.is_file(), "an accepted builder spawn must create the ledger")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700, "state directory mode")
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600, "ledger file mode")

    def test_existing_modes_are_tightened_and_symlink_target_is_untouched(self):
        self.state.mkdir(mode=0o755)
        with contextlib.closing(ledger.connect(self.db)):
            pass
        self.state.chmod(0o755)
        self.db.chmod(0o644)
        with contextlib.closing(ledger.connect(self.db)):
            pass
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        target = self.root / "target.sqlite3"
        self.db.rename(target)
        target.chmod(0o644)
        self.db.symlink_to(target)
        with contextlib.closing(ledger.connect(self.db)):
            pass
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_existing_sidecars_and_spool_are_tightened(self):
        with contextlib.closing(ledger.connect(self.db)):
            pass
        paths = [self.state / ("ledger.sqlite3" + suffix) for suffix in ("-wal", "-shm", "-journal")]
        paths.append(self.state / "ledger.spool")
        for path in paths:
            path.write_bytes(b"")
            path.chmod(0o644)
        with contextlib.closing(ledger.connect(self.db)):
            pass
        for path in paths:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, str(path))

    def test_locked_verdict_spools_and_replays_once(self):
        with contextlib.closing(ledger.connect(self.db)):
            pass
        blocker = sqlite3.connect(self.db, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(ledger, "HOOK_BUSY_SECONDS", 0.1):
                self.assertFalse(ledger.record_verdict("c:key", "PASS", session="s", path=self.db))
            spool = self.state / "ledger.spool"
            self.assertEqual(len(spool.read_text().splitlines()), 1)
            self.assertNotIn("PRIVATE_LABEL_SENTINEL", spool.read_text())
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        ledger.latest_verdict("c:key", path=self.db)
        self.assertEqual(self.rows("SELECT verdict FROM verdicts"), [("PASS",)])
        self.assertFalse(spool.exists())
        self.assertEqual(self.claims(), [])
        ledger.latest_verdict("c:key", path=self.db)
        self.assertEqual(self.rows("SELECT count(*) FROM verdicts"), [(1,)])
        self.assertEqual(self.claims(), [])

    def test_locked_judge_spools_verdict_and_bad_line_is_dropped(self):
        with contextlib.closing(ledger.connect(self.db)) as conn:
            conn.execute("INSERT INTO lanes VALUES ('s','b','c:key','p','builder','std',"
                         "'PRIVATE_LABEL_SENTINEL','returned',1,2)")
        blocker = sqlite3.connect(self.db, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(ledger, "HOOK_BUSY_SECONDS", 0.1):
                self.assertIsNone(ledger.judge_lane("s", "b", "SEND_BACK", 2, path=self.db))
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        spool = self.state / "ledger.spool"
        self.assertNotIn("PRIVATE_LABEL_SENTINEL", spool.read_text())
        self.assertEqual(json.loads(spool.read_text())["op"], "judge")
        with spool.open("a") as file:
            file.write("bad json\n")
        ledger.latest_verdict("c:key", path=self.db)
        self.assertEqual(self.rows("SELECT status FROM lanes"), [("judged",)])
        self.assertEqual(self.rows("SELECT verdict, findings FROM verdicts"), [("SEND_BACK", 2)])
        self.assertFalse(spool.exists())

    def test_legacy_judged_spool_still_replays_as_unknown(self):
        with contextlib.closing(ledger.connect(self.db)) as conn:
            conn.execute("INSERT INTO lanes VALUES ('s','b','c:key','p','builder','std',NULL,'returned',1,2)")
        (self.state / "ledger.spool").write_text(json.dumps({"op": "judged", "session": "s", "brief": "b", "ts": 3}) + "\n")
        ledger.latest_verdict("c:key", path=self.db)
        self.assertEqual(self.rows("SELECT status FROM lanes"), [("judged",)])
        self.assertEqual(self.rows("SELECT verdict FROM verdicts"), [("UNKNOWN",)])

    def test_locked_judge_record_preserves_verdict_and_round(self):
        with contextlib.closing(ledger.connect(self.db)) as conn:
            conn.execute("INSERT INTO attempts VALUES ('c:key',2,'std','judge','p','s',1)")
            conn.execute("INSERT INTO lanes VALUES ('s','b','c:key','p','judge','std',"
                         "'PRIVATE_LABEL_SENTINEL','returned',2,3)")
        blocker = sqlite3.connect(self.db, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(ledger, "HOOK_BUSY_SECONDS", 0.1):
                self.assertIsNone(ledger.judge_lane("s", "b", "SEND_BACK", 2, path=self.db))
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        spool = (self.state / "ledger.spool").read_text()
        self.assertEqual(json.loads(spool)["op"], "judge")
        self.assertNotIn("PRIVATE_LABEL_SENTINEL", spool)
        ledger.latest_verdict("c:key", path=self.db)
        self.assertEqual(self.rows("SELECT status, ended FROM lanes"), [("judged", 3)])
        self.assertEqual(self.rows("SELECT verdict, findings, round FROM verdicts"), [("SEND_BACK", 2, 1)])

    def test_spool_limit_drops_extra_line_and_logs_once(self):
        with contextlib.closing(ledger.connect(self.db)):
            pass
        spool = self.state / "ledger.spool"
        spool.write_text('{"op":"verdict"}\n' * 100)
        ledger._spool(self.db, {"op": "verdict", "key": "c:key"})
        self.assertEqual(len(spool.read_text().splitlines()), 100)
        self.assertIn(str(spool), (self.state / "errors.jsonl").read_text())

    def spool_one(self, key="c:key"):
        with contextlib.closing(ledger.connect(self.db)):
            pass
        line = {"op": "verdict", "key": key, "project": "p", "session": "s", "ts": 5.0,
                "verdict": "PASS", "findings": 0, "round": 1}
        (self.state / "ledger.spool").write_text(json.dumps(line) + "\n")

    def errors_log(self):
        log = self.state / "errors.jsonl"
        return log.read_text() if log.exists() else ""

    def claims(self):
        return sorted(item.name for item in self.state.glob("ledger.spool.replay*"))

    def test_second_replay_between_commit_and_unlink_applies_nothing_twice(self):
        self.spool_one()
        real, seen = Path.unlink, []

        def unlink(path, *args, **kwargs):
            if path.name.startswith("ledger.spool.replay") and not seen:
                seen.append(path.name)
                with contextlib.closing(ledger.connect(self.db, hook=True)):
                    pass
            return real(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            with contextlib.closing(ledger.connect(self.db, hook=True)):
                pass
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.rows("SELECT verdict FROM verdicts"), [("PASS",)])
        self.assertEqual(self.errors_log(), "")
        self.assertEqual(self.claims(), [])

    def test_crash_after_commit_replays_once_and_then_removes_the_file(self):
        self.spool_one()
        real = Path.unlink

        def skipped(path, *args, **kwargs):
            if not path.name.startswith("ledger.spool.replay"):
                return real(path, *args, **kwargs)

        with patch.object(Path, "unlink", skipped):
            with contextlib.closing(ledger.connect(self.db, hook=True)):
                pass
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(self.rows("SELECT count(*) FROM verdicts"), [(1,)])
        with contextlib.closing(ledger.connect(self.db, hook=True)):
            pass
        self.assertEqual(self.rows("SELECT count(*) FROM verdicts"), [(1,)])
        self.assertEqual(self.claims(), [])
        self.assertEqual(self.errors_log(), "")

    def test_legacy_fixed_name_replay_file_drains_once(self):
        self.spool_one()
        (self.state / "ledger.spool").rename(self.state / "ledger.spool.replay")
        for _ in range(2):
            ledger.latest_verdict("c:key", path=self.db)
        self.assertEqual(self.rows("SELECT verdict FROM verdicts"), [("PASS",)])
        self.assertEqual(self.claims(), [])

    def test_symlinked_claim_is_refused_with_one_log_line(self):
        self.spool_one()
        target = self.root / "elsewhere.txt"
        (self.state / "ledger.spool").rename(target)
        before = target.read_bytes()
        link = self.state / ("ledger.spool.replay." + "ab" * 16)
        link.symlink_to(target)
        for _ in range(3):
            ledger.latest_verdict("c:key", path=self.db)
        self.assertEqual(self.rows("SELECT count(*) FROM verdicts"), [(0,)])
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(link.is_symlink())
        self.assertEqual(len(self.errors_log().splitlines()), 1)
        self.assertIn(str(link), self.errors_log())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo is missing on this platform")
    def test_special_replay_claims_do_not_block_verdict(self):
        self.state.mkdir(mode=0o700)
        claims = [self.state / f"ledger.spool.replay.{name}" for name in ("fifo", "link", "dir")]
        os.mkfifo(claims[0])
        target = self.root / "elsewhere.txt"
        target.write_text("keep")
        claims[1].symlink_to(target)
        claims[2].mkdir()
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import ledger; "
                "print(ledger.record_verdict('c:key', 'PASS', path=sys.argv[2])); "
                "print(ledger.record_verdict('c:key', 'PASS', path=sys.argv[2]))")
        result = subprocess.run([sys.executable, "-c", code, str(ROOT / "hooks/router"), str(self.db)],
                                env=self.environment, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=5)
        self.assertEqual((result.returncode, result.stdout), (0, "True\nTrue\n"), result.stderr)
        self.assertEqual(self.rows("SELECT verdict FROM verdicts"), [("PASS",), ("PASS",)])
        lines = self.errors_log().splitlines()
        self.assertEqual(len(lines), 3, lines)
        for claim in claims:
            pattern = (r'\{"ts":"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ","script":"ledger",'
                       r'"action":"replay","error":"OSError","detail":null,"path":%s\}'
                       % re.escape(json.dumps(str(claim))))
            self.assertEqual(sum(re.fullmatch(pattern, line) is not None for line in lines), 1, lines)
        self.assertEqual(target.read_text(), "keep")

    def test_claim_deleted_under_a_running_replay_is_not_a_failure(self):
        self.spool_one()
        with contextlib.closing(ledger.connect(self.db)) as conn:
            conn.execute("INSERT INTO verdicts VALUES ('c:real','p','s',1.0,'SEND_BACK',2,1)")
        real_open, real_read = os.open, Path.read_bytes

        def vanish(path):
            if Path(path).name.startswith("ledger.spool.replay"):
                Path(path).unlink(missing_ok=True)

        def opening(path, *args, **kwargs):
            vanish(path)
            return real_open(path, *args, **kwargs)

        def reading(path):
            vanish(path)
            return real_read(path)

        with patch.object(ledger.os, "open", opening), patch.object(Path, "read_bytes", reading):
            result = ledger.latest_verdict("c:real", path=self.db)
        self.assertEqual(result["verdict"], "SEND_BACK")
        self.assertEqual(self.errors_log(), "")
        self.assertEqual(self.claims(), [])

    def test_spool_full_logs_once_for_three_writes(self):
        with contextlib.closing(ledger.connect(self.db)):
            pass
        spool = self.state / "ledger.spool"
        spool.write_text('{"op":"verdict"}\n' * 100)
        for _ in range(3):
            ledger._spool(self.db, {"op": "verdict", "key": "c:key"})
        lines = self.errors_log().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r'.*"action": ?"spool".*')
        self.assertIn(str(spool), lines[0])
        self.assertEqual(len(spool.read_text().splitlines()), 100)

    def unsafe_spool_costs_one_line(self, make):
        """3 hook verdicts with something unsafe at ledger.spool; then 3 writer-side spools at a
        second ledger whose spool is made the same way. Each path logs exactly one whole line."""
        hook_db, writer_db = self.db, self.root / "writer" / "ledger.sqlite3"
        for db in (hook_db, writer_db):
            with contextlib.closing(ledger.connect(db)):
                pass
        hook_spool, writer_spool = make(hook_db.with_name("ledger.spool")), make(writer_db.with_name("ledger.spool"))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            stored = [ledger.record_verdict(f"c:key{i}", "PASS", session="s", path=hook_db) for i in range(3)]
            for i in range(3):
                ledger._spool(writer_db, {"op": "verdict", "key": f"c:w{i}", "verdict": "PASS", "ts": i})
            ledger._spool(hook_db, {"op": "verdict", "key": "c:late", "verdict": "PASS", "ts": 9})
            with contextlib.closing(ledger.connect(writer_db, hook=True)):
                pass
        self.assertEqual(stored, [True, True, True])
        self.assertEqual(self.rows("SELECT key FROM verdicts ORDER BY key"), [("c:key0",), ("c:key1",), ("c:key2",)])
        self.assertEqual(stderr.getvalue(), "")
        line = r'\{"ts":"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ","script":"ledger","action":"%s","error":"LedgerError",' \
               r'"detail":"unsafe spool path","path":%s\}'
        lines = self.errors_log().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertRegex(lines[0], "^" + line % ("replay", re.escape(json.dumps(str(hook_spool)))) + "$")
        self.assertRegex(lines[1], "^" + line % ("spool", re.escape(json.dumps(str(writer_spool)))) + "$")
        return hook_spool, writer_spool

    def test_symlink_spool_to_a_file_costs_one_line_and_leaves_the_target(self):
        def make(spool):
            target = spool.parent.parent / (spool.parent.name + "-target.txt")
            target.write_bytes(b"keep\n")
            target.chmod(0o644)
            spool.symlink_to(target)
            return spool
        for spool in self.unsafe_spool_costs_one_line(make):
            target = spool.parent.parent / (spool.parent.name + "-target.txt")
            self.assertTrue(spool.is_symlink())
            self.assertEqual((target.read_bytes(), stat.S_IMODE(target.stat().st_mode)), (b"keep\n", 0o644))

    def test_dangling_symlink_spool_costs_one_line_and_creates_nothing(self):
        def make(spool):
            spool.symlink_to(spool.parent / "missing-target")
            return spool
        for spool in self.unsafe_spool_costs_one_line(make):
            self.assertTrue(spool.is_symlink())
            self.assertFalse((spool.parent / "missing-target").exists())

    def test_directory_spool_costs_one_line_and_is_left_alone(self):
        def make(spool):
            spool.mkdir(mode=0o755)
            spool.chmod(0o755)
            return spool
        for spool in self.unsafe_spool_costs_one_line(make):
            self.assertEqual((spool.is_dir(), stat.S_IMODE(spool.stat().st_mode), list(spool.iterdir())),
                             (True, 0o755, []))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "os.mkfifo is missing on this platform")
    def test_fifo_spool_costs_one_line_and_never_blocks(self):
        def make(spool):
            os.mkfifo(spool, 0o644)
            os.chmod(spool, 0o644)
            return spool
        for spool in self.unsafe_spool_costs_one_line(make):
            self.assertTrue(stat.S_ISFIFO(os.lstat(spool).st_mode))

    def test_reset_cli_says_archived_in_full(self):
        with contextlib.closing(ledger.connect(self.db)) as conn:
            conn.execute("INSERT INTO attempts VALUES ('c:key',1,'std','builder','p','s',?)", (time.time(),))
        result = subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), "ladder", "reset", "c:key"],
                                cwd=self.project, env=self.environment, text=True, capture_output=True,
                                timeout=20, stdin=subprocess.DEVNULL)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "reset c:key: 1 attempt(s) archived; its next spawn is round 1\n", ""))

    def test_two_concurrent_spawns_get_rounds_one_and_two(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.hook(), range(2)))
        for result in results:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["model"], "sonnet")
        self.assertEqual(self.rows("SELECT round FROM attempts ORDER BY round"), [(1,), (2,)])

    def test_corrupt_ledger_allows_the_spawn_and_logs_the_path(self):
        self.state.mkdir(mode=0o700)
        garbage = b"this is not a sqlite database\n" * 64
        self.db.write_bytes(garbage)
        result = self.hook()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""),
                         "a broken ledger must allow the call unchanged")
        self.assertEqual(self.db.read_bytes(), garbage, "a broken ledger must not be rewritten")
        errors = [json.loads(line) for line in
                  (self.state / "errors.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(entry.get("path") == str(self.db) for entry in errors),
                        f"no error line names the ledger path: {errors}")

    def test_newer_schema_version_allows_and_logs(self):
        conn = ledger.connect(self.db)
        conn.execute("PRAGMA user_version = 7")
        conn.close()
        result = self.hook()
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        self.assertIn(str(self.db), (self.state / "errors.jsonl").read_text(encoding="utf-8"))

    def test_existing_attempts_database_is_left_byte_identical(self):
        self.state.mkdir(mode=0o700)
        old = self.state / "attempts.sqlite3"
        with contextlib.closing(sqlite3.connect(old)) as conn:
            conn.execute("CREATE TABLE attempts (brief TEXT, round INTEGER, tier TEXT, PRIMARY KEY (brief, round))")
            conn.execute("INSERT INTO attempts VALUES ('0123456789abcdef', 1, 'std')")
            conn.commit()
        before = (old.read_bytes(), old.stat().st_mtime_ns, stat.S_IMODE(old.stat().st_mode))
        for _ in range(2):
            self.assertEqual(self.hook().returncode, 0)
        self.assertEqual((old.read_bytes(), old.stat().st_mtime_ns, stat.S_IMODE(old.stat().st_mode)), before,
                         "the 0.2 attempts.sqlite3 must never be touched")
        self.assertEqual(len(self.rows("SELECT * FROM attempts")), 2)

    def test_brief_text_is_stored_nowhere(self):
        sentinel = "SENTINEL_BRIEF_TEXT_7F3A"
        prompt = (f"TASK add the {sentinel} helper\nFILES src/{sentinel}.py\nBAR run {sentinel} tests\n"
                  f"RETURN five lines about {sentinel}")
        for _ in range(2):
            result = self.hook(prompt=prompt)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hook(prompt=prompt).returncode, 2, "round 3 without the ladder line is denied")
        self.assertEqual(len(self.rows("SELECT * FROM lanes")), 2)
        self.assertEqual(self.rows("SELECT DISTINCT label FROM lanes"), [("Implement the helper",)])
        assert_absent_from_ledger(self, self.db, sentinel)

    def test_prune_removes_only_rows_past_each_threshold(self):
        now = time.time()
        conn = ledger.connect(self.db)
        try:
            for days, table in ((7, "lanes"), (30, "attempts"), (90, "verdicts")):
                for offset, name in ((-60, "old"), (60, "new")):
                    stamp = now - days * DAY + offset
                    if table == "lanes":
                        conn.execute("INSERT INTO lanes VALUES (?, 'b', 'k', 'p', 'builder', 'std', NULL, 'running', ?, NULL)",
                                     (name, stamp))
                    elif table == "attempts":
                        conn.execute("INSERT INTO attempts VALUES (?, 1, 'std', 'builder', 'p', 's', ?)", (name, stamp))
                    else:
                        conn.execute("INSERT INTO verdicts VALUES (?, 'p', 's', ?, 'PASS', 0, 1)", (name, stamp))
            ledger.prune(conn, now)
            self.assertEqual(conn.execute("SELECT session FROM lanes").fetchall(), [("new",)])
            self.assertEqual(conn.execute("SELECT key FROM attempts").fetchall(), [("new",)])
            self.assertEqual(conn.execute("SELECT key FROM verdicts").fetchall(), [("new",)])
        finally:
            conn.close()

    def test_prune_runs_at_most_daily(self):
        now = time.time()
        conn = ledger.connect(self.db)
        try:
            def add_old_lane(name):
                conn.execute("INSERT INTO lanes VALUES (?, 'b', 'k', 'p', 'builder', 'std', NULL, 'running', ?, NULL)",
                             (name, now - 10 * DAY))
            add_old_lane("first")
            self.assertTrue(ledger.maybe_prune(conn, now))
            add_old_lane("second")
            self.assertFalse(ledger.maybe_prune(conn, now + DAY - 60), "pruned twice in one day")
            self.assertEqual(conn.execute("SELECT session FROM lanes").fetchall(), [("second",)])
            self.assertTrue(ledger.maybe_prune(conn, now + DAY + 60))
            self.assertEqual(conn.execute("SELECT count(*) FROM lanes").fetchone()[0], 0)
        finally:
            conn.close()

    def spawn(self, key, now, session="s1", brief="b1"):
        return ledger.record_attempt_and_lane(key, lambda prior: {"decision": "allow", "tier": "std"},
                                              role="builder", project="p", session=session, brief=brief, now=now)

    def test_expired_attempt_survives_the_next_spawn_but_is_not_prior(self):
        now = time.time()
        self.spawn("c:k", now - 13 * 3600)
        self.spawn("c:k", now)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM attempts"), [(2,)], "expired attempt was deleted")
        self.assertEqual(self.rows("SELECT round FROM attempts WHERE round > 0"), [(1,)])
        conn = ledger.connect(self.db)
        try:
            self.assertEqual(ledger.load_prior(conn, "c:k", now), [{"tier": "std"}])
        finally:
            conn.close()
        self.assertEqual([row["round"] for row in ledger.ladders(now=now, path=self.db)], [1])

    def test_expired_attempts_do_not_break_live_numbering_or_round_counts(self):
        now = time.time()
        self.spawn("c:k", now - 20 * 3600)
        self.spawn("c:k", now - 11 * 3600)
        self.spawn("c:k", now - 1 * 3600)
        self.spawn("c:k", now)
        self.assertEqual(self.rows("SELECT round FROM attempts WHERE round > 0 ORDER BY round"), [(1,), (2,), (3,)])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM attempts"), [(4,)])
        rounds = [lane["round"] for lane in ledger.lanes_report(path=self.db)]
        self.assertEqual(rounds, [3, 2, 1, None])

    def test_reset_archives_live_attempts_and_keeps_history(self):
        now = time.time()
        self.spawn("c:k", now - 13 * 3600)
        self.spawn("c:k", now)
        self.spawn("c:k", now + 1)
        self.assertEqual(ledger.reset_ladder("c:k", self.db), 2)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM attempts"), [(3,)], "reset deleted history")
        self.assertEqual(ledger.reset_ladder("c:k", self.db), 0)
        self.assertEqual(ledger.ladders(now=now + 2, path=self.db), [])
        self.spawn("c:k", now + 2)
        self.assertEqual(self.rows("SELECT round FROM attempts WHERE round > 0"), [(1,)])

    def test_prune_removes_archived_attempts_at_thirty_days(self):
        now = time.time()
        self.spawn("c:k", now - 31 * DAY)
        os.utime(self.state / ledger.PRUNE_STAMP, (now - 13 * 3600 - 60,) * 2)  # keep the daily auto prune out of this test
        self.spawn("c:k", now - 13 * 3600)
        self.spawn("c:k", now)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM attempts"), [(3,)])
        conn = ledger.connect(self.db)
        try:
            ledger.prune(conn, now)
        finally:
            conn.close()
        self.assertEqual(self.rows("SELECT COUNT(*) FROM attempts"), [(2,)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
