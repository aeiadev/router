"""Report-only observation and a service-enforced fixture boundary."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from _platform import home_env, run

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks" / "router"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

from observation import ObservationError, assess
from observation_connector import FakeConnector

CLI = ROOT / "hooks" / "router" / "cmd_observe.py"


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class ObservationContract(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="harness-observation-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.service = FakeConnector(self.base / "service.json")
        self.addCleanup(self.service.close)
        self.expected = {"schema_version": 1, "task_id": "task_1", "target_id": "target_1",
                         "revision_id": "rev_2", "requirements": {"r1": digest("after")}}

    def healthy(self):
        self.service.worker.write("after", "rev_2")
        return self.service.observer.read("task_1", "target_1")

    def test_observer_cannot_escape_to_worker_credential(self):
        before = self.service.path.read_bytes()
        observer = self.service.observer
        self.assertNotEqual(self.service._actor.pid, os.getpid())
        for request in ({"op": "write", "value": "intrusion", "revision_id": "rev_3"},
                        {"op": "write", "value": "intrusion", "revision_id": "rev_3",
                         "credential": "worker"},
                        {"op": "availability", "available": False},
                        {"op": "stats"}):
            self.assertEqual(observer.request(request), {"error": "denied"})
        self.assertEqual(self.service.path.read_bytes(), before)
        self.assertEqual(self.service.mutation_count, 0)
        self.service.worker.write("after", "rev_2")
        self.assertEqual(self.service.mutation_count, 1)
        self.assertEqual(assess(self.expected, observer.read("task_1", "target_1"))
                         ["reported_verdict"], "PASS")

    def test_service_enforces_observer_write_denial_and_report_does_not_replay(self):
        observed = self.healthy()
        before = self.service.path.read_bytes()
        with self.assertRaises(PermissionError):
            self.service.observer.write("intrusion", "rev_3")
        self.assertEqual(self.service.path.read_bytes(), before)
        self.assertEqual(assess(self.expected, observed)["reported_verdict"], "PASS")
        self.assertEqual(self.service.mutation_count, 1)
        self.assertEqual(self.service.observer.read("task_1", "target_1"), observed)

    def test_complete_wrong_proof_fails_and_incomplete_proof_is_unknown(self):
        observed = self.healthy()
        for key, value in (("task_id", "other_task"), ("target_id", "other_target"),
                           ("revision_id", "rev_1")):
            bad = {**observed, key: value}
            self.assertEqual(assess(self.expected, bad)["reported_verdict"], "FAIL")
        bad = {**observed, "requirements": {"r1": digest("wrong")}}
        self.assertEqual(assess(self.expected, bad)["reported_verdict"], "FAIL")
        for key in ("task_id", "target_id", "revision_id", "evidence_id", "verifier_id", "requirements"):
            partial = {k: v for k, v in observed.items() if k != key}
            self.assertEqual(assess(self.expected, partial)["reported_verdict"], "UNKNOWN")
        self.assertEqual(self.service.mutation_count, 1)

    def test_denied_unavailable_and_invalid_input(self):
        self.assertEqual(assess(self.expected, self.service.worker.read("task_1", "target_1"))
                         ["reported_verdict"], "UNKNOWN")
        self.service.available = False
        self.assertEqual(assess(self.expected, self.service.observer.read("task_1", "target_1"))
                         ["reported_verdict"], "UNKNOWN")
        for bad in ({}, {"schema_version": 1, "read_status": "ok", "secret": "raw"},
                    {"schema_version": True, "read_status": "ok"},
                    {"schema_version": 1, "read_status": "bad"},
                    {"schema_version": 1, "read_status": "denied", "task_id": "/raw/path"}):
            with self.assertRaises(ObservationError):
                assess(self.expected, bad)
        with self.assertRaises(ObservationError):
            assess({**self.expected, "requirements": {}}, {"schema_version": 1,
                                                              "read_status": "ok"})

    def test_stale_evidence_fails_and_supplied_json_is_not_authenticated(self):
        stale = self.healthy()
        # Same bytes at a later revision: only the revision tells stale evidence apart.
        self.service.worker.write("after", "rev_3")
        current = {**self.expected, "revision_id": "rev_3"}
        count = self.service.mutation_count
        self.assertEqual(assess(current, stale)["reported_verdict"], "FAIL")
        self.assertEqual(assess(current, self.service.observer.read("task_1", "target_1"))
                         ["reported_verdict"], "PASS")
        # A hand-built proof for bytes the service never held also reports PASS: the command
        # cannot authenticate supplied JSON, so it must say nothing was verified.
        forged = {**stale, "revision_id": "rev_9", "requirements": {"r1": digest("never written")}}
        report = assess({**self.expected, "revision_id": "rev_9",
                         "requirements": {"r1": digest("never written")}}, forged)
        self.assertEqual(report["reported_verdict"], "PASS")
        self.assertFalse(report["host_verified"])
        self.assertEqual(report["independent_acceptance"], "unverified")
        self.assertNotIn(b"never written", self.service.path.read_bytes())
        self.assertEqual(self.service.mutation_count, count)

    def test_cli_is_bounded_report_only_and_private(self):
        observed = self.healthy()
        env = {**os.environ, **home_env(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        for value, code, verdict in (({"expected": self.expected, "observed": observed}, 0, "PASS"),
                            ({"expected": self.expected, "observed": {**observed,
                                                                          "target_id": "other_target"}}, 0, "FAIL"),
                            ({"expected": self.expected, "observed": {"schema_version": 1,
                                                                         "read_status": "unavailable"}}, 0, "UNKNOWN"),
                            ({"expected": self.expected, "observed": {**observed, "raw": "SECRET"}}, 1, None)):
            result = run([sys.executable, "-B", str(CLI), "--file", "-"], timeout=10,
                         input=json.dumps(value).encode(), capture_output=True, env=env,
                         cwd=self.base)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertNotIn(b"SECRET", result.stdout + result.stderr)
            if code == 0:
                report = json.loads(result.stdout)
                self.assertEqual(report["reported_verdict"], verdict)
                self.assertEqual(report["mode"], "report-only")
                self.assertFalse(report["gates_mutation"])
                self.assertFalse(report["host_verified"])
                self.assertEqual(report["independent_acceptance"], "unverified")
        for raw in (b'{"expected":{},"expected":{},"observed":{}}', b"{" + b"x" * 65536 + b"}",
                    b'{"expected":{},"observed":{"read_status":NaN}}'):
            result = run([sys.executable, "-B", str(CLI), "--file", "-"], timeout=10,
                         input=raw, capture_output=True, env=env, cwd=self.base)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertIn(result.stderr, (b"observation: invalid input\n",
                                          b"observation: invalid input\r\n"))
        self.assertEqual(self.service.mutation_count, 1)
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ["home", "service.json"])


if __name__ == "__main__":
    unittest.main()
