#!/usr/bin/env python3
"""The explicit receipt CLI, exercised through Router's command discovery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "bin" / "router"
sys.path.insert(0, str(ROOT / "tests"))
from _platform import BASH, IS_WINDOWS, home_env, posix  # noqa: E402

CLI = [BASH, posix(ROUTER)] if IS_WINDOWS else [sys.executable, "-B", str(ROUTER)]
ALIGNMENT = "a" * 64
ARTIFACT = {"revision": "b" * 40}


class ReceiptCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-receipt-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("ROUTER_") and key not in ("HOME", "XDG_STATE_HOME", "CLAUDE_HOME", "CODEX_HOME")}
        self.env.update({"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "xdg"),
                         "ROUTER_STATE": str(self.state), "CLAUDE_HOME": str(self.root / "claude"),
                         "CODEX_HOME": str(self.root / "codex"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.update(home_env(self.root / "home"))

    def run_router(self, *args, input_text=None):
        stdin = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run([*CLI, "receipt", *args],
                              text=True, capture_output=True, env=self.env,
                              cwd=ROOT, timeout=15, **stdin)

    def payload_file(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def create(self):
        payload = {"alignment_digest": ALIGNMENT, "requirements": ["R1"], "artifact": ARTIFACT}
        result = self.run_router("create", "task_1", "--file", self.payload_file("create.json", payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_discovery_create_show_and_record_stay_unverified(self):
        created = self.create()
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["verification"], {"state": "unverified", "verdict": "UNKNOWN", "provenance": "unavailable"})
        self.assertEqual(json.loads(self.run_router("show", "task_1").stdout), created)

        events = [
            {"event_id": "e1", "kind": "attempt", "attempt_id": "try_1", "actor_id": "worker_1", "revision": 1, "state": "running"},
            {"event_id": "e2", "kind": "attempt", "attempt_id": "try_1", "actor_id": "worker_1", "revision": 1, "state": "returned"},
            {"event_id": "e3", "kind": "attestation", "attempt_id": "try_1", "verifier_id": "reviewer_1",
             "revision": 1, "artifact": ARTIFACT, "verdict": "PASS", "evidence": {"R1": ["evidence_1"]},
             "provenance": "coordinator-recorded"},
        ]
        for index, event in enumerate(events):
            source = self.payload_file(f"event-{index}.json", event)
            result = self.run_router("record", "task_1", "--file", source)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, json.dumps(json.loads(result.stdout), sort_keys=True) + "\n")
        snapshot = json.loads(result.stdout)
        self.assertEqual(snapshot["attempts"][0]["state"], "returned")
        self.assertEqual(snapshot["attestation_summary"]["verdict"], "PASS")
        self.assertEqual(snapshot["verification"], created["verification"])

    def test_stdin_missing_show_and_syntax(self):
        missing = self.run_router("show", "missing")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("router receipt:", missing.stderr)
        self.assertFalse(self.state.exists(), "show must not create storage")
        payload = json.dumps({"alignment_digest": ALIGNMENT, "requirements": ["R1"]})
        created = self.run_router("create", "task_1", "--file", "-", input_text=payload)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertEqual(json.loads(created.stdout)["task_id"], "task_1")
        for args in (("create", "task_2"), ("show", "task_1", "--file", "-"), ("unknown", "task_1")):
            with self.subTest(args=args):
                self.assertEqual(self.run_router(*args).returncode, 2)

    def test_input_errors_are_bounded_and_do_not_echo_content(self):
        marker = "SECRET_PAYLOAD_NEVER_ECHO"
        for name, content in (("bad.json", '{"x":"' + marker + '",}'),
                              ("large.json", " " * 65537),
                              ("array.json", "[]")):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text(content, encoding="utf-8")
                result = self.run_router("create", "task_1", "--file", str(path))
                self.assertEqual(result.returncode, 1)
                self.assertIn("router receipt:", result.stderr)
                self.assertNotIn(marker, result.stderr)
                self.assertLess(len(result.stderr), 250)
                self.assertFalse(self.state.exists())

    def test_public_host_provenance_is_rejected(self):
        self.create()
        event = {"event_id": "e1", "kind": "attempt", "attempt_id": "try_1", "actor_id": "worker_1", "revision": 1, "state": "running"}
        self.assertEqual(self.run_router("record", "task_1", "--file", self.payload_file("attempt.json", event)).returncode, 0)
        event = {"event_id": "e2", "kind": "attestation", "attempt_id": "try_1", "verifier_id": "reviewer_1",
                 "revision": 1, "artifact": ARTIFACT, "verdict": "PASS", "evidence": {"R1": ["evidence_1"]},
                 "provenance": "host-observed"}
        rejected = self.run_router("record", "task_1", "--file", self.payload_file("attestation.json", event))
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("router receipt:", rejected.stderr)
        self.assertEqual(json.loads(self.run_router("show", "task_1").stdout)["attestations"], [])

    def test_deep_json_errors_are_bounded_for_files_and_stdin(self):
        content = '{"revision":' + '[' * 10000 + '0' + ']' * 10000 + '}'
        self.assertLess(len(content), 64 * 1024)
        path = self.root / "deep.json"
        path.write_text(content, encoding="utf-8")
        for command in ("create", "record"):
            for source in (str(path), "-"):
                with self.subTest(command=command, source=source):
                    result = self.run_router(command, "task_1", "--file", source,
                                             input_text=content if source == "-" else None)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("router receipt:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertLess(len(result.stderr), 250)
                    self.assertEqual(result.stdout, "")
                    self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
