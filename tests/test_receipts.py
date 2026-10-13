"""Frozen B1 receipt storage and derived state contract."""
import concurrent.futures
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/router"))
import receipts
from _platform import skip_unless_posix_modes, skip_unless_symlinks


DIGEST = "a" * 64
OTHER = "b" * 64
REVISION = "c" * 40


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="router-receipts-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name) / "state"
        self.payload = {"alignment_digest": DIGEST, "requirements": ["R1", "R2"],
                        "artifact": {"revision": REVISION}}

    def create(self):
        return receipts.create_task("task_1", self.payload, state_dir=self.state)

    def event(self, event):
        return receipts.record_event("task_1", event, state_dir=self.state)

    def attempt(self, event_id="e1", attempt_id="a1", actor="worker", state="running", revision=1):
        return {"event_id": event_id, "kind": "attempt", "attempt_id": attempt_id,
                "actor_id": actor, "revision": revision, "state": state}

    def attest(self, event_id="e3", verifier="judge", verdict="PASS", evidence=None,
               revision=1, artifact=None, provenance="coordinator-recorded"):
        return {"event_id": event_id, "kind": "attestation", "attempt_id": "a1",
                "verifier_id": verifier, "revision": revision, "artifact": artifact or self.payload["artifact"],
                "verdict": verdict, "evidence": evidence if evidence is not None else {"R1": ["proof1"], "R2": ["proof2"]},
                "provenance": provenance}

    def test_absent_read_invalid_input_and_opt_out_do_not_write(self):
        self.assertIsNone(receipts.get_task("task_1", state_dir=self.state))
        with self.assertRaises(receipts.ReceiptError):
            receipts.create_task("bad/path", self.payload, state_dir=self.state)
        with self.assertRaises(receipts.ReceiptError):
            receipts.create_task("task_1", {**self.payload, "note": "private text"}, state_dir=self.state)
        with self.assertRaises(receipts.ReceiptError):
            self.event(self.attempt())
        self.assertFalse(self.state.exists())

    def test_persistence_current_attestation_never_becomes_verification(self):
        first = self.create()
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["verification"], {"state": "unverified", "verdict": "UNKNOWN", "provenance": "unavailable"})
        self.assertEqual(first["attestation_summary"], {"verdict": "UNKNOWN", "eligibility": "missing", "provenance": "unavailable"})
        self.event(self.attempt())
        self.event(self.attempt("e2", state="returned"))
        final = self.event(self.attest())
        self.assertEqual(final["attestation_summary"], {"verdict": "PASS", "eligibility": "current", "provenance": "coordinator-recorded"})
        self.assertEqual(final["verification"], first["verification"])
        self.assertEqual(receipts.get_task("task_1", state_dir=self.state), final)
        with self.assertRaises(receipts.ReceiptError):
            self.create()

    def test_attempt_transitions_and_eligibility_recompute(self):
        self.create()
        self.event(self.attempt())
        self.event(self.attest("early"))
        self.assertEqual(self.event(self.attempt("e2", state="returned"))["attestation_summary"]["eligibility"], "current")
        self.assertEqual(self.event(self.attest("self", verifier="worker"))["attestation_summary"]["eligibility"], "self-reported")
        self.assertEqual(self.event(self.attest("short", evidence={"R1": ["proof1"]}))["attestation_summary"]["eligibility"], "incomplete")
        self.assertEqual(self.event(self.attest("unknown", verifier=None, verdict="UNKNOWN", evidence={}, provenance="unavailable"))["attestation_summary"],
                         {"verdict": "UNKNOWN", "eligibility": "unknown", "provenance": "unavailable"})
        revised = self.event({"event_id": "e4", "kind": "revise", "alignment_digest": OTHER,
                              "requirements": ["R1", "R2"], "artifact": {"revision": "d" * 40}})
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["attestations"][0]["eligibility"], "stale")
        self.assertEqual(revised["verification"]["verdict"], "UNKNOWN")
        with self.assertRaises(receipts.ReceiptError):
            self.event(self.attempt("retry", state="running"))
        with self.assertRaises(receipts.ReceiptError):
            self.event(self.attempt("again", state="failed"))
        with self.assertRaises(receipts.ReceiptError):
            self.event(self.attempt("terminal", state="failed"))

    def test_failed_and_abandoned_attempts_are_retained(self):
        self.create()
        self.event(self.attempt())
        self.event(self.attempt("e2", state="failed"))
        with self.assertRaises(receipts.ReceiptError):
            self.event(self.attempt("e3", state="returned"))
        self.event(self.attempt("e4", attempt_id="a2"))
        result = self.event(self.attempt("e5", attempt_id="a2", state="abandoned"))
        self.assertEqual([x["state"] for x in result["attempts"]], ["failed", "abandoned"])

    def test_duplicate_and_conflicting_events_are_atomic_even_under_concurrency(self):
        self.create()
        event = self.attempt()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.event(event), range(16)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(results[0]["attempts"]), 1)
        with self.assertRaises(receipts.ReceiptError):
            self.event({**event, "actor_id": "other"})
        self.assertEqual(receipts.get_task("task_1", state_dir=self.state), results[0])

    def test_concurrent_distinct_events_are_not_lost(self):
        self.create()
        events = [self.attempt(f"event{i}", attempt_id=f"attempt{i}") for i in range(16)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(self.event, events))
        stored = receipts.get_task("task_1", state_dir=self.state)
        self.assertEqual({item["attempt_id"] for item in stored["attempts"]},
                         {f"attempt{i}" for i in range(16)})

    def test_rejects_untrusted_provenance_flags_and_bad_shapes(self):
        self.create()
        self.event(self.attempt())
        for bad in (self.attest(provenance="host-observed"),
                    {**self.attest(), "verified": True},
                    {**self.attest(), "verdict": "PASS", "evidence": {"R1": ["https://example.com"]}},
                    {**self.attempt(), "revision": True},
                    {**self.attempt(), "kind": []}):
            with self.subTest(bad=bad), self.assertRaises(receipts.ReceiptError):
                self.event(bad)
        self.assertEqual(receipts.get_task("task_1", state_dir=self.state)["attestations"], [])

    def test_invalid_brief_and_artifact_leave_no_storage(self):
        for payload in ({**self.payload, "brief_id": None},
                        {**self.payload, "artifact": {"revision": "host.example"}},
                        {**self.payload, "requirements": ["R1", "R1"]},
                        {**self.payload, "artifact": {"revision": REVISION, "path": "/tmp/artifact"}}):
            with self.subTest(payload=payload), self.assertRaises(receipts.ReceiptError):
                receipts.create_task("task_1", payload, state_dir=self.state)
        self.assertFalse(self.state.exists())

    def test_artifact_mismatch_and_unknown_evidence(self):
        self.create()
        self.event(self.attempt())
        self.event(self.attempt("e2", state="returned"))
        result = self.event(self.attest(artifact={"revision": "d" * 40}))
        self.assertEqual(result["attestation_summary"]["eligibility"], "incomplete")
        with self.assertRaises(receipts.ReceiptError):
            self.event(self.attest("e4", evidence={"R3": ["proof"]}))
        self.assertEqual(len(receipts.get_task("task_1", state_dir=self.state)["attestations"]), 1)

    def test_unsupported_schema_is_not_mutated(self):
        self.create()
        database = self.state / "receipts.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute("PRAGMA user_version = 2")
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("PRAGMA user_version")
        before = database.read_bytes()
        connect = sqlite3.connect
        for operation in (lambda: receipts.get_task("task_1", state_dir=self.state),
                          lambda: self.event(self.attempt()),
                          lambda: receipts.create_task("task_2", self.payload, state_dir=self.state)):
            opened = []

            def capture(*args, **kwargs):
                connection = connect(*args, **kwargs)
                opened.append(connection)
                return connection

            with self.subTest(operation=operation), mock.patch.object(receipts.sqlite3, "connect", side_effect=capture):
                with self.assertRaises(receipts.ReceiptError):
                    operation()
            self.assertEqual(len(opened), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].execute("PRAGMA user_version")
            self.assertEqual(database.read_bytes(), before)

    def test_deep_event_is_a_validation_error_without_storage_mutation(self):
        nested = 0
        for _ in range(10000):
            nested = [nested]
        event = self.attempt(revision=nested)
        with self.assertRaises(receipts.ReceiptError):
            self.event(event)
        self.assertFalse(self.state.exists())
        self.create()
        database = self.state / "receipts.sqlite3"
        before = database.read_bytes()
        with self.assertRaises(receipts.ReceiptError):
            self.event(event)
        self.assertEqual(database.read_bytes(), before)

    @skip_unless_posix_modes("receipt storage permissions")
    def test_private_storage_and_separate_ledger(self):
        self.state.mkdir(mode=0o777)
        ledger = self.state / "ledger.sqlite3"
        ledger.write_bytes(b"untouched")
        self.create()
        database = self.state / "receipts.sqlite3"
        self.assertEqual(ledger.read_bytes(), b"untouched")
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(database.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(database)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("PRAGMA user_version")

    @skip_unless_symlinks("receipt path rejection")
    def test_symlinked_storage_is_rejected_without_touching_target(self):
        target = Path(self.temporary.name) / "target"
        target.mkdir()
        self.state.symlink_to(target, target_is_directory=True)
        with self.assertRaises(receipts.ReceiptError):
            self.create()
        self.assertEqual(list(target.iterdir()), [])

    @skip_unless_posix_modes("receipt read privacy")
    def test_reads_reject_public_storage_without_mutation(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        self.assertIsNone(receipts.get_task("task_1", state_dir=self.state))
        self.assertEqual(list(self.state.iterdir()), [])
        self.state.chmod(0o700)
        for name in ("directory", "database", "-journal", "-wal", "-shm"):
            with self.subTest(name=name):
                state = self.state / name
                receipts.create_task("task_1", self.payload, state_dir=state)
                database = state / "receipts.sqlite3"
                before = database.read_bytes()
                target = state if name == "directory" else database
                if name.startswith("-"):
                    target = Path(str(database) + name)
                    target.write_bytes(b"sidecar marker")
                mode = 0o755 if name == "directory" else 0o644
                target.chmod(mode)
                with self.assertRaises(receipts.ReceiptError):
                    receipts.get_task("task_1", state_dir=state)
                self.assertEqual(target.stat().st_mode & 0o777, mode)
                self.assertEqual(database.read_bytes(), before)
                if name.startswith("-"):
                    self.assertEqual(target.read_bytes(), b"sidecar marker")

    @skip_unless_symlinks("receipt database rejection")
    def test_symlinked_database_is_rejected_without_touching_target(self):
        self.state.mkdir()
        target = Path(self.temporary.name) / "target.db"
        target.write_bytes(b"untouched")
        (self.state / "receipts.sqlite3").symlink_to(target)
        with self.assertRaises(receipts.ReceiptError):
            self.create()
        self.assertEqual(target.read_bytes(), b"untouched")

    @skip_unless_symlinks("receipt ancestor rejection")
    def test_symlinked_ancestor_rejects_creation_before_making_directories(self):
        root = Path(self.temporary.name)
        target = root / "target"
        target.mkdir()
        link = root / "link"
        link.symlink_to(target, target_is_directory=True)
        dangling = root / "dangling"
        dangling.symlink_to(root / "absent", target_is_directory=True)
        for state in (link / "missing" / "state", link / ".." / "outside" / "state",
                      dangling / "state"):
            with self.subTest(state=state), self.assertRaises(receipts.ReceiptError):
                receipts.create_task("task_1", self.payload, state_dir=state)
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse((root / "outside").exists())
        self.assertFalse((root / "absent").exists())

    @skip_unless_symlinks("receipt ancestor rejection")
    def test_symlinked_ancestor_rejects_reads_and_events_without_mutation(self):
        self.create()
        database = self.state / "receipts.sqlite3"
        before = database.read_bytes()
        root = Path(self.temporary.name)
        link = root / "link"
        link.symlink_to(root, target_is_directory=True)
        redirected = link / "state"
        with self.assertRaises(receipts.ReceiptError):
            receipts.get_task("task_1", state_dir=redirected)
        with self.assertRaises(receipts.ReceiptError):
            receipts.record_event("task_1", self.attempt(), state_dir=redirected)
        with self.assertRaises(receipts.ReceiptError):
            receipts.create_task("task_2", self.payload, state_dir=redirected)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(receipts.get_task("task_1", state_dir=self.state)["attempts"], [])


if __name__ == "__main__":
    unittest.main()
