#!/usr/bin/env python3
"""Independent offline R2/R3 fixtures for the public receipts API.

All identities and hashes are synthetic. No hook, model, or network is involved.
"""
import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "hooks" / "router"))
from _platform import canonical_temp_root, home_env  # noqa: E402
import receipts  # noqa: E402


ALIGN_A = "a" * 64
ALIGN_B = "b" * 64
ART_A = {"revision": "c" * 40, "readback": "d" * 64}
ART_B = {"revision": "e" * 40, "readback": "f" * 64}
UNVERIFIED = {"state": "unverified", "verdict": "UNKNOWN", "provenance": "unavailable"}


def attempt(event_id, attempt_id, state, revision=1, actor_id="builder"):
    return {"event_id": event_id, "kind": "attempt", "attempt_id": attempt_id,
            "actor_id": actor_id, "revision": revision, "state": state}


def attestation(event_id, attempt_id, *, verifier_id="reviewer", revision=1,
                artifact=ART_A, verdict="PASS", evidence=None, provenance="coordinator-recorded"):
    return {"event_id": event_id, "kind": "attestation", "attempt_id": attempt_id,
            "verifier_id": verifier_id, "revision": revision, "artifact": artifact,
            "verdict": verdict, "evidence": {"R2": ["evidence_1"], "R3": ["evidence_2"]}
            if evidence is None else evidence, "provenance": provenance}


class ReceiptContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-receipt-contract-")
        self.addCleanup(temporary.cleanup)
        self.root = canonical_temp_root(temporary)
        self.state = self.root / "state"
        self.home = self.root / "home"
        self.home.mkdir()
        isolated = patch.dict(os.environ, {**home_env(self.home),
                                            "PYTHONDONTWRITEBYTECODE": "1"})
        isolated.start()
        self.addCleanup(isolated.stop)

    def create(self, task_id="task_1"):
        return receipts.create_task(task_id, {"alignment_digest": ALIGN_A,
                                            "requirements": ["R2", "R3"],
                                            "artifact": ART_A}, state_dir=self.state)

    def record(self, event, task_id="task_1"):
        return receipts.record_event(task_id, copy.deepcopy(event), state_dir=self.state)

    def snapshot(self, task_id="task_1"):
        return receipts.get_task(task_id, state_dir=self.state)

    def assert_unverified(self, snapshot):
        self.assertEqual(snapshot["verification"], UNVERIFIED)

    def assert_summary(self, snapshot, verdict, eligibility, provenance="coordinator-recorded"):
        self.assertEqual(snapshot["attestation_summary"], {
            "verdict": verdict, "eligibility": eligibility, "provenance": provenance})
        self.assert_unverified(snapshot)

    def test_absent_reads_and_invalid_writes_do_not_create_storage(self):
        self.assertIsNone(self.snapshot())
        self.assertFalse(self.state.exists())
        with self.assertRaises(receipts.ReceiptError):
            self.record(attempt("event_1", "attempt_1", "running"))
        self.assertFalse(self.state.exists())
        for payload in ({"alignment_digest": ALIGN_A, "requirements": []},
                        {"alignment_digest": ALIGN_A, "requirements": ["R2"], "raw_text": "SECRET"},
                        {"alignment_digest": ALIGN_A, "requirements": ["R2"],
                         "artifact": {"path": "/private/secret"}}):
            with self.subTest(payload=payload):
                with self.assertRaises(receipts.ReceiptError):
                    receipts.create_task("task_1", payload, state_dir=self.state)
                self.assertFalse(self.state.exists())

    def test_new_and_first_pass_coordinator_attestation_remain_unverified(self):
        created = self.create()
        self.assertEqual(created["schema_version"], 1)
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["attempts"], [])
        self.assertEqual(created["attestations"], [])
        self.assertEqual(created["attestation_summary"]["verdict"], "UNKNOWN")
        self.assertEqual(created["attestation_summary"]["eligibility"], "missing")
        self.assertEqual(created["attestation_summary"]["provenance"], "unavailable")
        self.assert_unverified(created)
        self.record(attempt("start_1", "attempt_1", "running"))
        self.record(attempt("return_1", "attempt_1", "returned"))
        self.record(attestation("attest_1", "attempt_1"))
        result = self.snapshot()
        self.assertEqual(result["attempts"], [{"attempt_id": "attempt_1", "actor_id": "builder",
                                               "revision": 1, "state": "returned",
                                               "provenance": "coordinator-recorded"}])
        self.assertEqual(result["attestations"][0]["eligibility"], "current")
        self.assert_summary(result, "PASS", "current")

    def test_failed_and_abandoned_attempts_survive_retries(self):
        self.create()
        for number, terminal in ((1, "failed"), (2, "abandoned"), (3, "returned")):
            ident = f"attempt_{number}"
            self.record(attempt(f"start_{number}", ident, "running"))
            self.record(attempt(f"end_{number}", ident, terminal))
        result = self.snapshot()
        self.assertEqual([(a["attempt_id"], a["state"]) for a in result["attempts"]],
                         [("attempt_1", "failed"), ("attempt_2", "abandoned"),
                          ("attempt_3", "returned")])
        self.assertTrue(all(a["provenance"] == "coordinator-recorded" for a in result["attempts"]))
        self.assert_unverified(result)

    def test_self_pass_missing_evidence_and_incomplete_attempt(self):
        self.create()
        self.record(attempt("start_1", "attempt_1", "running"))
        self.record(attestation("attest_running", "attempt_1"))
        self.assert_summary(self.snapshot(), "UNKNOWN", "incomplete")
        self.record(attempt("return_1", "attempt_1", "returned"))
        self.record(attestation("attest_missing", "attempt_1", evidence={"R2": ["evidence_1"]}))
        self.assert_summary(self.snapshot(), "UNKNOWN", "incomplete")
        self.record(attestation("attest_self", "attempt_1", verifier_id="builder"))
        self.assert_summary(self.snapshot(), "UNKNOWN", "self-reported")
        self.assertEqual([a["eligibility"] for a in self.snapshot()["attestations"]],
                         ["current", "incomplete", "self-reported"])

    def test_revision_and_artifact_change_stale_prior_evidence(self):
        self.create()
        self.record(attempt("start_1", "attempt_1", "running"))
        self.record(attempt("return_1", "attempt_1", "returned"))
        self.record(attestation("attest_1", "attempt_1"))
        self.record({"event_id": "revise_1", "kind": "revise", "alignment_digest": ALIGN_B,
                     "requirements": ["R2", "R3"], "artifact": ART_B})
        result = self.snapshot()
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["alignment_digest"], ALIGN_B)
        self.assertEqual(result["artifact"], ART_B)
        self.assertEqual(result["attestations"][0]["eligibility"], "stale")
        self.assert_summary(result, "UNKNOWN", "stale")
        self.record(attempt("start_2", "attempt_2", "running", revision=2))
        self.record(attempt("return_2", "attempt_2", "returned", revision=2))
        self.record(attestation("attest_old_artifact", "attempt_2", revision=2))
        self.assert_summary(self.snapshot(), "UNKNOWN", "incomplete")

    def test_unknown_unavailable_attestation_stays_unknown(self):
        self.create()
        self.record(attempt("start_1", "attempt_1", "running"))
        self.record(attestation("unavailable_1", "attempt_1", verifier_id=None,
                                verdict="UNKNOWN", evidence={}, provenance="unavailable"))
        self.assert_summary(self.snapshot(), "UNKNOWN", "unknown", "unavailable")

    def test_forged_authority_and_raw_content_are_rejected_atomically(self):
        self.create()
        self.record(attempt("start_1", "attempt_1", "running"))
        self.record(attempt("return_1", "attempt_1", "returned"))
        baseline = self.snapshot()
        bad_events = [
            dict(attestation("forged_host", "attempt_1"), provenance="host-observed"),
            dict(attestation("forged_verified", "attempt_1"), verified=True),
            dict(attestation("forged_accepted", "attempt_1"), accepted=True),
            dict(attestation("raw_content", "attempt_1"), raw_text="SECRET_RAW_CONTENT"),
            dict(attestation("raw_evidence", "attempt_1"), evidence={"R2": ["/private/secret"],
                                                                     "R3": ["evidence_2"]}),
            dict(attestation("hostname", "attempt_1"), verifier_id="host.example"),
            {key: value for key, value in attestation("missing_verdict", "attempt_1").items()
             if key != "verdict"},
        ]
        for event in bad_events:
            with self.subTest(event_id=event["event_id"]):
                with self.assertRaises(receipts.ReceiptError) as raised:
                    self.record(event)
                self.assertLess(len(str(raised.exception)), 512)
                self.assertNotIn("SECRET_RAW_CONTENT", str(raised.exception))
                self.assertEqual(self.snapshot(), baseline)
        self.assertNotIn(b"SECRET_RAW_CONTENT", b"".join(
            path.read_bytes() for path in self.state.rglob("*") if path.is_file()))

    def test_duplicate_event_is_idempotent_and_conflict_refused(self):
        self.create()
        event = attempt("start_1", "attempt_1", "running")
        first = self.record(event)
        self.assertEqual(self.record(event), first)
        self.assertEqual(len(self.snapshot()["attempts"]), 1)
        before = self.snapshot()
        with self.assertRaises(receipts.ReceiptError):
            self.record(dict(event, actor_id="other_builder"))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
