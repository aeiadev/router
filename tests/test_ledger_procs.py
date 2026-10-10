#!/usr/bin/env python3
"""Real-process spool interleavings and replay recovery."""
import contextlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROUTER = Path(__file__).resolve().parents[1] / "hooks/router"
sys.path.insert(0, str(ROUTER))
import common  # noqa: E402
import ledger  # noqa: E402

OUTER_ENV = dict(os.environ)
PINNED = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "ROUTER_STATE", "ROUTER_LOCAL")
LINE = (r'\{"ts":"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ","script":"ledger","action":"spool","error":"LedgerError",'
        r'"detail":"spool %s","path":%s\}')

DRIVER = r'''
import fcntl, json, os, signal, sys, time
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import ledger
db, mode, gate, marker = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])
key = sys.argv[6] if len(sys.argv) > 6 else mode
if mode == "late":
    real = fcntl.flock
    def waiting(fd, op):
        marker.write_text("ready")
        print("ready", flush=True)
        while not gate.exists(): time.sleep(.002)
        return real(fd, op)
    fcntl.flock = waiting
    ledger._spool(db, {"op":"verdict", "key":key, "verdict":"PASS", "ts":1})
elif mode == "held":
    real = os.pread
    def waiting(fd, size, pos):
        marker.write_text("ready")
        while not gate.exists(): time.sleep(.002)
        return real(fd, size, pos)
    os.pread = waiting
    ledger._spool(db, {"op":"verdict", "key":key, "verdict":"PASS", "ts":1})
elif mode == "writer":
    for i in range(25):
        ledger._spool(db, {"op":"verdict", "key":f"{marker.name}-{i}", "verdict":"PASS", "ts":i})
elif mode == "replay":
    real = os.open
    def opened(path, *args, **kwargs):
        if Path(path).name == "ledger.spool": marker.write_text("ready")
        return real(path, *args, **kwargs)
    os.open = opened
    for i in range(40):
        with ledger.connect(db, hook=True): pass
        time.sleep(.002)
elif mode == "once":
    real = os.open
    def opened(path, *args, **kwargs):
        if Path(path).name == "ledger.spool": marker.write_text("ready")
        return real(path, *args, **kwargs)
    os.open = opened
    with ledger.connect(db, hook=True): pass
elif mode == "swap":
    # Hold the lock of whatever inode is the spool; on writer try k swap in a fresh locked spool.
    spool = db.with_name("ledger.spool")
    fd = os.open(spool, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    marker.write_text("ready")
    for k in range(1, 6):
        end = time.monotonic() + 10
        while not gate.with_name(f"{gate.name}-{k}").exists() and time.monotonic() < end: time.sleep(.002)
        fresh = db.with_name("ledger.spool.next")
        new = os.open(fresh, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        fcntl.flock(new, fcntl.LOCK_EX)
        os.replace(fresh, spool)
        os.close(fd)
        fd = new
    end = time.monotonic() + 10
    while not gate.exists() and time.monotonic() < end: time.sleep(.002)
    os.close(fd)
elif mode == "crash":
    real = Path.unlink
    def killed(path, *args, **kwargs):
        if path.name.startswith("ledger.spool.replay"):
            os.kill(os.getpid(), signal.SIGKILL)
        return real(path, *args, **kwargs)
    Path.unlink = killed
    with ledger.connect(db, hook=True): pass
'''


class ProcessSpoolTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="router-ledger-procs-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "state/ledger.sqlite3"
        self.driver = self.root / "driver.py"
        self.driver.write_text(DRIVER)
        # Parent and children: every home, config and state lookup lands in this test's temp dir.
        self.pins = {"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
                     "XDG_STATE_HOME": str(self.root / "xdg-state"), "ROUTER_STATE": str(self.root / "state"),
                     "ROUTER_LOCAL": "off"}
        self.pinned = mock.patch.dict(os.environ, self.pins)
        self.pinned.start()
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        with contextlib.closing(ledger.connect(self.db)):
            pass

    def tearDown(self):
        self.pinned.stop()

    def proc(self, mode, gate=None, marker=None, key=None):
        return subprocess.Popen([sys.executable, str(self.driver), str(ROUTER), str(self.db), mode,
                                 str(gate or self.root / "go"), str(marker or self.root / "ready")]
                                + ([key] if key else []),
                                env=self.env, cwd=self.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def finish(self, proc):
        out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 0, out + err)

    def ready(self, marker):
        end = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < end:
            time.sleep(.002)
        self.assertTrue(marker.exists(), "child did not reach spool interleave")

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return conn.execute("SELECT key, count(*) FROM verdicts GROUP BY key ORDER BY key").fetchall()

    def empty_spool(self):
        self.assertEqual(list(self.db.parent.glob("ledger.spool*")), [])

    def test_late_writer_reopens_after_replay_claim(self):
        marker, gate = self.root / "ready", self.root / "go"
        proc = self.proc("late", gate, marker)
        try:
            self.assertEqual(proc.stdout.readline().strip(), "ready")
            with contextlib.closing(ledger.connect(self.db, hook=True)):
                pass
            ledger._spool(self.db, {"op": "verdict", "key": "fresh", "verdict": "PASS", "ts": 1})
            gate.touch()
            self.finish(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        with contextlib.closing(ledger.connect(self.db, hook=True)):
            pass
        self.assertEqual(self.rows(), [("fresh", 1), ("late", 1)])
        self.empty_spool()

    def test_replay_waits_for_writer_inode_lock(self):
        marker, gate = self.root / "ready", self.root / "go"
        writer = self.proc("held", gate, marker)
        try:
            self.ready(marker)
            replay_marker = self.root / "replay-ready"
            replay = self.proc("replay", marker=replay_marker)
            self.ready(replay_marker)
            time.sleep(.05)
            gate.touch()
            self.finish(writer)
            self.finish(replay)
        finally:
            for proc in (writer, locals().get("replay")):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        with contextlib.closing(ledger.connect(self.db, hook=True)):
            pass
        self.assertEqual(self.rows(), [("held", 1)])
        self.empty_spool()

    def test_gated_writers_across_twenty_replays_keep_every_key_once(self):
        """Each round, writer H holds the spool lock paused before its write, writer L has the same
        spool open paused before its lock, and one replay runs: it must wait for H (flock in
        _replay). A fresh spool line then lands in a new spool inode, and L must notice that its
        inode was renamed away and reopen (inode recheck in _spool). Red with either one removed."""
        expected = []
        for r in range(20):
            names = {part: self.root / f"{part}-{r:02d}" for part in ("h-go", "h-ready", "l-go", "l-ready", "r-ready")}
            procs = [self.proc("held", names["h-go"], names["h-ready"], key=f"held-{r:02d}")]
            try:
                self.ready(names["h-ready"])
                procs.append(self.proc("late", names["l-go"], names["l-ready"], key=f"late-{r:02d}"))
                self.ready(names["l-ready"])
                procs.append(self.proc("once", marker=names["r-ready"]))
                self.ready(names["r-ready"])
                with contextlib.suppress(subprocess.TimeoutExpired):
                    procs[2].wait(timeout=.2)
                names["h-go"].touch()
                self.finish(procs[0])
                self.finish(procs[2])
                ledger._spool(self.db, {"op": "verdict", "key": f"fresh-{r:02d}", "verdict": "PASS", "ts": r})
                names["l-go"].touch()
                self.finish(procs[1])
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
            expected += [(f"fresh-{r:02d}", 1), (f"held-{r:02d}", 1), (f"late-{r:02d}", 1)]
        with contextlib.closing(ledger.connect(self.db, hook=True)):
            pass
        self.assertEqual(self.rows(), sorted(expected))
        self.empty_spool()

    def errors_log(self):
        log = self.root / "state" / "errors.jsonl"
        return log.read_text().splitlines() if log.exists() else []

    def spool_full(self):
        spool = self.db.with_name("ledger.spool")
        spool.write_text('{"op":"verdict"}\n' * 100)
        ledger._spool(self.db, {"op": "verdict", "key": "full-key", "verdict": "PASS", "ts": 1})
        self.assertEqual(spool.read_text(), '{"op":"verdict"}\n' * 100)

    def spool_busy(self):
        """Another process holds the spool inode lock through all 5 tries, swapping the inode each time."""
        run = len(list(self.root.glob("swap-ready-*")))
        gate, marker, tries = self.root / f"swap-go-{run}", self.root / f"swap-ready-{run}", []
        holder = self.proc("swap", gate, marker)
        real = __import__("fcntl").flock

        def flock(fd, op):
            tries.append(op)
            gate.with_name(f"{gate.name}-{len(tries)}").touch()
            return real(fd, op)
        try:
            self.ready(marker)
            with mock.patch("fcntl.flock", flock):
                ledger._spool(self.db, {"op": "verdict", "key": "busy-key", "verdict": "PASS", "ts": 1})
            gate.touch()
            self.finish(holder)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.communicate()
        self.assertEqual(len(tries), 5)
        self.assertNotIn("busy-key", self.db.with_name("ledger.spool").read_text())

    def assert_spool_lines(self, *details):
        spool = json.dumps(str(self.db.with_name("ledger.spool")))
        lines = self.errors_log()
        self.assertEqual(len(lines), len(details), lines)
        for line, detail in zip(lines, details):
            self.assertRegex(line, "^" + LINE % (detail, re.escape(spool)) + "$")

    def test_spool_busy_gives_up_and_logs_once_after_spool_full(self):
        self.spool_full()
        self.spool_busy()
        self.spool_busy()
        self.spool_full()
        self.assert_spool_lines("full", "busy")
        with contextlib.closing(ledger.connect(self.db, hook=True)):
            pass
        self.assertEqual(self.rows(), [])

    def test_spool_busy_first_does_not_silence_spool_full(self):
        self.spool_busy()
        self.spool_full()
        self.spool_full()
        self.spool_busy()
        self.assert_spool_lines("busy", "full")

    def test_state_fallback_through_home_stays_in_the_temp_dir(self):
        self.assertEqual({name: os.environ.get(name) for name in PINNED}, self.pins)
        with mock.patch.dict(os.environ):
            for name in ("ROUTER_STATE", "XDG_STATE_HOME"):
                del os.environ[name]
            self.assertEqual(common.state_path(), self.root / "home/.local/state/claude-router")
        self.assertEqual(common.state_path(), self.root / "state")

    def test_module_run_leaves_the_sentinel_dir_empty(self):
        """The parent-logging tests of this module, run from a shell without any state or config
        variables and HOME at a sentinel dir, write nothing there."""
        if os.environ.get("ROUTER_PROCS_SENTINEL_RUN"):
            self.skipTest("already inside the sentinel run")
        sentinel = self.root / "sentinel"
        sentinel.mkdir()
        env = {key: value for key, value in OUTER_ENV.items()
               if not key.startswith(("ROUTER_", "XDG_", "CLAUDE_")) and key not in ("HOME", "ROUTES_JSON")}
        env.update(HOME=str(sentinel), PYTHONDONTWRITEBYTECODE="1", ROUTER_PROCS_SENTINEL_RUN="1")
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "-k", "spool_busy",
                                 "-k", "state_fallback"], env=env, cwd=self.root, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stderr.splitlines()[-1], r"^OK$")
        self.assertIn("Ran 3 tests", result.stderr)
        self.assertEqual(sorted(str(path) for path in sentinel.rglob("*")), [])

    def test_sigkill_after_commit_is_replayed_once(self):
        ledger._spool(self.db, {"op": "verdict", "key": "crash", "verdict": "SEND_BACK", "ts": 1})
        proc = self.proc("crash")
        proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, -9)
        self.assertEqual(self.rows(), [("crash", 1)])
        self.assertEqual(len(list(self.db.parent.glob("ledger.spool.replay*"))), 1)
        with contextlib.closing(ledger.connect(self.db, hook=True)):
            pass
        self.assertEqual(self.rows(), [("crash", 1)])
        self.empty_spool()


if __name__ == "__main__":
    unittest.main(verbosity=2)
