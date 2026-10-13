"""Standalone opt-in usage accounting contract regressions."""
import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/router"))
import usage


METRICS = ("input_tokens", "output_tokens", "cached_input_tokens",
           "cache_creation_input_tokens", "reasoning_output_tokens", "total_tokens")


class UsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="usage-core-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"

    def declare(self, ids=None, host="codex", outcome="returned", task="task"):
        return usage.declare_task(task, {"host": host, "expected_event_ids": ids,
                                         "outcome": outcome}, state_dir=self.state)

    def event(self, event_id="e1", adapter="codex-turn", values=None, **context):
        c = {"event_id": event_id, "attempt_id": "a1", "stage": "worker", "outcome": "returned", **context}
        if adapter == "missing":
            return usage.normalize_event(adapter, {}, c)
        if adapter == "claude-message":
            return usage.normalize_event(adapter, {"type": "assistant", "message": {"usage": values}}, c)
        if adapter == "codex-cumulative":
            return usage.normalize_event(adapter, {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": values}}}, c)
        return usage.normalize_event(adapter, {"type": "turn.completed", "usage": values}, c)

    def record(self, event):
        return usage.record_event("task", event, state_dir=self.state)

    def test_absent_and_invalid_do_not_write(self):
        report = usage.get_report(state_dir=self.state)
        self.assertEqual(report["tasks"], [])
        self.assertFalse(self.state.exists())
        for bad in ({"host": "codex", "expected_event_ids": [], "outcome": "returned", "raw": "secret"},
                    {"host": "codex", "expected_event_ids": ["x", "x"], "outcome": "returned"}):
            with self.assertRaises(usage.UsageError):
                usage.declare_task("task", bad, state_dir=self.state)
        with self.assertRaises(usage.UsageError):
            self.event(values={"input_tokens": True})
        self.assertFalse(self.state.exists())

    def test_host_math_missing_zero_and_inventory(self):
        claude = self.event(adapter="claude-message", values={"input_tokens": 2, "output_tokens": 3,
                            "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7})
        self.assertEqual(claude["usage"]["cached_input_tokens"], 5)
        self.assertIsNone(claude["usage"]["total_tokens"])
        self.declare(["e1"], host="claude")
        report = self.record(claude)
        self.assertEqual(report["tasks"][0]["known_tokens"], 17)
        self.assertTrue(report["tasks"][0]["tokens_complete"])
        self.assertEqual(report["tasks"][0]["missing_fields"]["total_tokens"], 1)
        self.assertEqual(report["independently_accepted_tasks"], 0)
        self.assertIsNone(report["tokens_per_independently_accepted_task"])
        self.assertEqual(report["by_host"]["claude"]["known_tokens"], 17)

        other = "codex"
        usage.declare_task(other, {"host": "codex", "expected_event_ids": ["z"], "outcome": "failed"}, state_dir=self.state)
        zero = self.event("z", values={"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0})
        report = usage.record_event(other, zero, state_dir=self.state)
        self.assertEqual(report["tasks"][0]["known_tokens"], 0)
        self.assertTrue(report["tasks"][0]["tokens_complete"])
        self.assertEqual(report["tasks"][0]["missing_fields"]["cache_creation_input_tokens"], 1)
        self.assertEqual(usage.get_report(state_dir=self.state)["attempted_tasks"], 2)

    def test_duplicate_conflict_equal_distinct_and_concurrent(self):
        self.declare(["e1", "e2"])
        first = self.event(values={"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 4})
        self.record(first)
        self.assertEqual(self.record(first)["tasks"][0]["event_count"], 1)
        second = self.event("e2", values={"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 4})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self.record, [second] * 8))
        self.assertTrue(all(r["tasks"][0]["event_count"] == 2 for r in results))
        self.assertEqual(results[0]["tasks"][0]["known_tokens"], 24)
        with self.assertRaises(usage.UsageError):
            self.record(self.event(values={"input_tokens": 9, "output_tokens": 2}))
        self.assertEqual(self.declare(["e1", "e2"])["tasks"][0]["event_count"], 2)
        with self.assertRaises(usage.UsageError):
            self.declare(["e1"])

    def test_cumulative_prefix_gap_reset_and_new_stream(self):
        self.declare(["e1", "e2", "e3", "e4", "e5"])
        def sample(event_id, sequence, input_tokens, stream_id="s", **more):
            return self.event(event_id, adapter="codex-cumulative",
                              values={"input_tokens": input_tokens, "output_tokens": sequence},
                              stream_id=stream_id, sequence=sequence, **more)
        self.record(sample("e1", 0, 5, from_start=True))
        self.record(sample("e2", 1, 8))
        self.assertEqual(usage.get_report("task", state_dir=self.state)["tasks"][0]["known_tokens"], 9)
        self.record(sample("e3", 3, 12))
        self.assertFalse(usage.get_report("task", state_dir=self.state)["tasks"][0]["events"][2]["coverage_complete"])
        with self.assertRaises(usage.UsageError):
            self.record(sample("e4", 4, 1))
        with self.assertRaises(usage.UsageError):
            self.record(sample("e4", 3, 13))
        self.record(sample("e4", 0, 1, stream_id="new", from_start=True))
        self.record(sample("e5", 1, 2, stream_id="new"))
        final = usage.get_report("task", state_dir=self.state)["tasks"][0]
        self.assertEqual(final["inventory_state"], "complete")
        self.assertFalse(final["tokens_complete"])

    def test_unknown_prefix_missing_fields_and_attribution(self):
        self.declare(None, outcome="abandoned")
        first = self.event("e1", adapter="codex-cumulative", values={"input_tokens": 10},
                           stream_id="s", sequence=4, stage="judge", attempt_id=None, outcome="failed")
        self.record(first)
        second = self.event("e2", adapter="codex-cumulative", values={"input_tokens": 12},
                            stream_id="s", sequence=5, stage="judge", attempt_id=None, outcome="failed")
        self.record(second)
        self.record(self.event("e3", adapter="missing", stage="hook", attempt_id=None, outcome="abandoned"))
        task = usage.get_report("task", state_dir=self.state)["tasks"][0]
        self.assertEqual(task["inventory_state"], "unknown")
        self.assertEqual(task["event_count"], 3)
        self.assertEqual(task["outcome_counts"]["failed"], 2)
        self.assertEqual(task["stage_counts"]["judge"], 2)
        self.assertEqual(task["missing_attribution_events"], 3)
        self.assertIsNone(task["events"][0]["counted_usage"]["input_tokens"])
        self.assertEqual(task["events"][1]["counted_usage"]["input_tokens"], 2)
        self.assertFalse(task["tokens_complete"])

    def test_malformed_deep_and_privacy(self):
        with self.assertRaises(usage.UsageError):
            self.event(values={"input_tokens": 1.0})
        with self.assertRaises(usage.UsageError):
            self.event(values={"input_tokens": 1, "output_tokens": 1, "total_tokens": 3})
        with self.assertRaises(usage.UsageError):
            self.event(values={"input_tokens": 1, "cached_input_tokens": 2})
        deep = {}
        cursor = deep
        for _ in range(1100):
            cursor["x"] = {}
            cursor = cursor["x"]
        with self.assertRaises(usage.UsageError):
            usage.normalize_event("codex-turn", deep, {"event_id": "x", "attempt_id": None,
                                   "stage": "worker", "outcome": "unknown"})
        self.declare(["e1"])
        raw = {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 2},
               "private": "secret prompt"}
        item = usage.normalize_event("codex-turn", raw, {"event_id": "e1", "attempt_id": "a1",
                                      "stage": "worker", "outcome": "returned"})
        self.record(item)
        self.assertNotIn("secret prompt", (self.state / "usage.sqlite3").read_bytes().decode("latin1"))

    def test_storage_safety_and_fresh_process(self):
        self.declare(["e1"])
        self.record(self.event(values={"input_tokens": 2, "output_tokens": 3}))
        code = "import sys,json;sys.path.insert(0,sys.argv[1]);import usage;from pathlib import Path;print(json.dumps(usage.get_report(state_dir=Path(sys.argv[2]))))"
        run = subprocess.run([sys.executable, "-B", "-c", code, str(Path(usage.__file__).parent), str(self.state)],
                             input=b"", capture_output=True, check=True)
        self.assertEqual(json.loads(run.stdout)["tasks"][0]["known_tokens"], 5)
        if os.name != "nt":
            os.chmod(self.state, 0o755)
            with self.assertRaises(usage.UsageError):
                usage.get_report(state_dir=self.state)
            os.chmod(self.state, 0o700)
            os.chmod(self.state / "usage.sqlite3", 0o644)
            with self.assertRaises(usage.UsageError):
                usage.get_report(state_dir=self.state)
            with self.assertRaises(usage.UsageError):
                self.declare(["e1"])
            self.assertEqual((self.state / "usage.sqlite3").stat().st_mode & 0o777, 0o644)
            os.chmod(self.state / "usage.sqlite3", 0o600)
            self.declare(["e1"])

    def test_nonregular_symlink_and_corrupt_storage(self):
        self.state.mkdir()
        db = self.state / "usage.sqlite3"
        db.mkdir()
        with self.assertRaises(usage.UsageError):
            usage.get_report(state_dir=self.state)
        db.rmdir()
        db.write_bytes(b"not sqlite")
        with self.assertRaises(usage.UsageError):
            usage.get_report(state_dir=self.state)
        db.unlink()
        if hasattr(os, "symlink"):
            db.symlink_to(self.state / "target")
            with self.assertRaises(usage.UsageError):
                usage.get_report(state_dir=self.state)
            db.unlink()
            link = self.state.parent / "link"
            link.symlink_to(self.state, target_is_directory=True)
            with self.assertRaises(usage.UsageError):
                usage.get_report(state_dir=link)
            with self.assertRaises(usage.UsageError):
                usage.get_report(state_dir=link / ".." / "state")

    def test_failed_retry_judge_and_unknown_acceptance(self):
        self.declare(["fail1", "fail2", "retry", "judge", "missing"])
        for event_id, attempt, stage, outcome, values in (
                ("fail1", "a1", "worker", "failed", {"input_tokens": 3, "output_tokens": 1}),
                ("fail2", "a2", "worker", "failed", {"input_tokens": 4, "output_tokens": 1}),
                ("retry", "a3", "worker", "returned", {"input_tokens": 5, "output_tokens": 2}),
                ("judge", "j1", "judge", "returned", {"input_tokens": 6, "output_tokens": 2})):
            self.record(self.event(event_id, values=values, attempt_id=attempt, stage=stage, outcome=outcome))
        report = self.record(self.event("missing", adapter="missing", attempt_id="a4", outcome="abandoned"))
        task = report["tasks"][0]
        self.assertEqual(task["attempt_count"], 5)
        self.assertEqual(task["known_tokens"], 24)
        self.assertEqual(task["outcome_counts"]["failed"], 2)
        self.assertEqual(task["stage_counts"]["judge"], 1)
        self.assertEqual(task["inventory_state"], "complete")
        self.assertFalse(task["tokens_complete"])
        self.assertEqual(report["independently_accepted_tasks"], 0)
        self.assertEqual(report["acceptance_state"], "unverified")
        self.assertIsNone(report["observed_cost_usd"])
        usage.declare_task("empty", {"host": "codex", "expected_event_ids": [], "outcome": "abandoned"}, state_dir=self.state)
        empty = usage.get_report("empty", state_dir=self.state)["tasks"][0]
        self.assertEqual(empty["inventory_state"], "complete")
        self.assertIsNone(empty["known_tokens"])
        self.assertFalse(empty["tokens_complete"])

    def test_corrupt_rows_sidecars_and_bounded_errors(self):
        self.declare(["e1"])
        db = self.state / "usage.sqlite3"
        sidecar = Path(str(db) + "-wal")
        sidecar.mkdir()
        with self.assertRaises(usage.UsageError):
            usage.get_report(state_dir=self.state)
        sidecar.rmdir()
        with closing(sqlite3.connect(db)) as connection:
            with connection:
                state = json.loads(connection.execute("SELECT state_json FROM tasks").fetchone()[0])
                state["events"] = [{"private": "do not expose"}]
                connection.execute("UPDATE tasks SET state_json = ?", (json.dumps(state),))
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT state_json FROM tasks")
        with self.assertRaises(usage.UsageError) as raised:
            usage.get_report(state_dir=self.state)
        self.assertNotIn("do not expose", str(raised.exception))
        self.assertLess(len(str(raised.exception)), 100)

    def test_missing_adapter_rejects_forged_counters_and_reasons(self):
        self.declare(["e1"])
        before = (self.state / "usage.sqlite3").read_bytes()
        event = self.event(adapter="missing")
        forged = {**event, "usage": {**event["usage"], "input_tokens": 10, "output_tokens": 5},
                  "missing": {k: v for k, v in event["missing"].items()
                              if k not in {"input_tokens", "output_tokens"}}}
        for bad in (forged, {**event, "missing": {k: "not_collected" for k in METRICS}}):
            with self.subTest(event=bad), self.assertRaises(usage.UsageError):
                self.record(bad)
        self.assertEqual((self.state / "usage.sqlite3").read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_rejected_unsupported_write_preserves_modes_and_bytes(self):
        self.state.mkdir(mode=0o755)
        db = self.state / "usage.sqlite3"
        with closing(sqlite3.connect(db)) as conn:
            with conn:
                conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("PRAGMA user_version")
        os.chmod(self.state, 0o755)
        os.chmod(db, 0o644)
        before = (db.read_bytes(), self.state.stat().st_mode, db.stat().st_mode)
        with self.assertRaises(usage.UsageError):
            self.declare([])
        self.assertEqual((db.read_bytes(), self.state.stat().st_mode, db.stat().st_mode), before)

    def test_cumulative_counted_missing_reasons(self):
        self.declare(["e1", "e2"])
        first = self.event("e1", adapter="codex-cumulative", values={"input_tokens": 10, "output_tokens": 5},
                           stream_id="s", sequence=4)
        event = self.record(first)["tasks"][0]["events"][0]
        self.assertEqual(event["counted_missing"]["input_tokens"], "not_collected")
        self.assertEqual(event["counted_missing"]["cached_input_tokens"], "not_exposed")
        self.assertEqual(event["coverage_missing_reason"], "not_collected")
        self.assertNotIn("input_tokens", event["missing"])
        second = self.event("e2", adapter="codex-cumulative", values={"input_tokens": 12, "output_tokens": 6},
                            stream_id="s", sequence=6)
        event = self.record(second)["tasks"][0]["events"][1]
        self.assertEqual(event["counted_usage"]["input_tokens"], 2)
        self.assertNotIn("input_tokens", event["counted_missing"])
        self.assertEqual(event["coverage_missing_reason"], "not_collected")

    def test_malformed_normalized_event_never_writes(self):
        self.declare(["e1"])
        db = self.state / "usage.sqlite3"
        before = db.read_bytes()
        event = self.event(values={"input_tokens": 1, "output_tokens": 1})
        for altered in ({**event, "raw": "secret"},
                        {**event, "missing": {"total_tokens": "wrong"}},
                        {**event, "usage": {**event["usage"], "input_tokens": -1}},
                        {**event, "stage": []}):
            with self.assertRaises(usage.UsageError):
                self.record(altered)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual(usage.get_report("task", state_dir=self.state)["tasks"][0]["event_count"], 0)


if __name__ == "__main__":
    unittest.main()
