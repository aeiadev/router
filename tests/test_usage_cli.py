#!/usr/bin/env python3
"""Router usage command discovery and isolated, real-provider behavior."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from _platform import run

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "bin" / "router"


class UsageCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-usage-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("ROUTER_") and key not in ("HOME", "USERPROFILE", "XDG_STATE_HOME")}
        self.env.update({"HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
                         "ROUTER_STATE": str(self.state), "ROUTER_LOCAL": "off",
                         "PYTHONDONTWRITEBYTECODE": "1"})

    def invoke(self, *args, input=b""):
        return run([sys.executable, "-B", str(ENTRY), "usage", *args], timeout=8,
                   input=input, capture_output=True, env=self.env)

    def write(self, name, value):
        path = self.root / name
        path.write_bytes(json.dumps(value).encode())
        return str(path)

    def declaration(self):
        return {"host": "claude", "expected_event_ids": ["event_1"], "outcome": "returned"}

    def observation(self):
        return {"adapter": "claude-message",
                "context": {"event_id": "event_1", "attempt_id": "attempt_1",
                            "stage": "worker", "outcome": "returned"},
                "payload": {"type": "assistant", "message": {"usage": {"input_tokens": 10,
                                                                       "output_tokens": 5}}}}

    def test_report_absent_state_does_not_create_it(self):
        result = self.invoke("report")
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        self.assertEqual(json.loads(result.stdout)["attempted_tasks"], 0)
        self.assertFalse(self.state.exists())

    def test_declare_stdin_record_file_and_sorted_json(self):
        declared = self.invoke("declare", "demo", "--file", "-", input=json.dumps(self.declaration()).encode())
        self.assertEqual((declared.returncode, declared.stderr), (0, b""))
        expected = json.dumps(json.loads(declared.stdout), sort_keys=True).encode()
        self.assertIn(declared.stdout, (expected + b"\n", expected + b"\r\n"))
        recorded = self.invoke("record", "demo", "--file", self.write("event.json", self.observation()))
        self.assertEqual((recorded.returncode, recorded.stderr), (0, b""))
        report = json.loads(self.invoke("report", "demo").stdout)
        self.assertEqual(report["attempted_tasks"], 1)
        self.assertEqual(report["tasks"][0]["known_tokens"], 15)
        self.assertEqual(report["independently_accepted_tasks"], 0)
        self.assertIsNone(report["tokens_per_independently_accepted_task"])

    def test_bad_and_deep_json_errors_are_bounded_and_private(self):
        for raw in (b"x" * (64 * 1024 + 1), b"[" * 6000 + b"]" * 6000,
                    b'{"secret":"PRIVATE_SENTINEL", bad}'):
            result = self.invoke("declare", "demo", "--file", "-", input=raw)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertLess(len(result.stderr), 160)
            self.assertNotIn(b"PRIVATE_SENTINEL", result.stderr)
        self.assertFalse(self.state.exists())

    def test_nonregular_file_and_syntax(self):
        result = self.invoke("declare", "demo", "--file", str(self.root))
        self.assertEqual(result.returncode, 1)
        self.assertLess(len(result.stderr), 160)
        self.assertEqual(self.invoke("declare", "demo").returncode, 2)
        if hasattr(os, "mkfifo"):
            fifo = self.root / "pipe"
            os.mkfifo(fifo)
            self.assertEqual(self.invoke("declare", "demo", "--file", str(fifo)).returncode, 1)
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
