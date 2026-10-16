#!/usr/bin/env python3
"""The explicit receipt CLI, exercised through Router's command discovery."""
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "bin" / "router"
sys.path.insert(0, str(ROOT / "tests"))
from _platform import BASH, IS_WINDOWS, canonical_temp_root, home_env, posix, python_shim, run  # noqa: E402

CLI = [BASH, posix(ROUTER)] if IS_WINDOWS else [sys.executable, "-B", str(ROUTER)]
ALIGNMENT = "a" * 64
ARTIFACT = {"revision": "b" * 40}


class ReceiptCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-receipt-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = canonical_temp_root(temporary)
        self.state = self.root / "state"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("ROUTER_") and key not in ("HOME", "XDG_STATE_HOME", "CLAUDE_HOME", "CODEX_HOME")}
        self.env.update({"HOME": str(self.root / "home"), "XDG_STATE_HOME": str(self.root / "xdg"),
                         "ROUTER_STATE": str(self.state), "CLAUDE_HOME": str(self.root / "claude"),
                         "CODEX_HOME": str(self.root / "codex"), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.update(home_env(self.root / "home"))

    def run_router(self, *args, input_text=None, timeout=15, cli=CLI):
        stdin = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
        return run([*cli, "receipt", *args], timeout=timeout,
                   text=True, capture_output=True, env=self.env, cwd=ROOT, **stdin)

    def state_inventory(self):
        if not self.state.exists():
            return None
        return {str(path.relative_to(self.state)): path.read_bytes() if path.is_file() else None
                for path in self.state.rglob("*")}

    def assert_rejected_without_writes(self, command, source, *, input_text=None, timeout=15):
        before = self.state_inventory()
        result = self.run_router(command, "task_1", "--file", str(source),
                                 input_text=input_text, timeout=timeout)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("router receipt:", result.stderr)
        self.assertNotIn("SECRET_PAYLOAD_NEVER_ECHO", result.stderr)
        self.assertLess(len(result.stderr), 250)
        self.assertEqual(self.state_inventory(), before)

    def payload_file(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def create(self):
        payload = {"alignment_digest": ALIGNMENT, "requirements": ["R1"], "artifact": ARTIFACT}
        result = self.run_router("create", "task_1", "--file", self.payload_file("create.json", payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def create_and_warm(self):
        """Create task_1, then run one successful command before any snapshot. On Windows the CLI
        starts through pyrun.sh, which saves its interpreter as <state>/python on the first run that
        exits 0 once the state folder exists (README, Requirements). The snapshot then already holds that
        documented cache, so a rejection must leave every byte as it was, cache included."""
        created = self.create()
        shown = self.run_router("show", "task_1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        if IS_WINDOWS:
            self.assertTrue((self.state / "python").is_file(), "the successful run saves the launcher cache")
        return created

    def launcher_cli(self):
        """`router` started through pyrun.sh on every OS, with a python3 shim first on PATH and no
        ROUTER_PYTHON, so a run with no saved interpreter probes for one (README, Requirements)."""
        shims = self.root / "shims"
        shims.mkdir()
        python_shim(shims)
        self.env["PATH"] = os.pathsep.join([str(shims), os.environ.get("PATH", "")])
        return [BASH, posix(ROUTER)]

    def create_through_the_launcher(self, cli):
        payload = {"alignment_digest": ALIGNMENT, "requirements": ["R1"]}
        created = self.run_router("create", "task_1", "--file", self.payload_file("create.json", payload), cli=cli)
        self.assertEqual(created.returncode, 0, created.stderr)
        (self.state / "python").unlink(missing_ok=True)

    def test_a_rejected_command_through_the_launcher_saves_no_python_cache(self):
        cli = self.launcher_cli()
        self.create_through_the_launcher(cli)
        before = self.state_inventory()
        bad = self.root / "bad.json"
        bad.write_text('{"kind":', encoding="utf-8")
        result = self.run_router("record", "task_1", "--file", str(bad), cli=cli)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("router receipt:", result.stderr)
        self.assertEqual(self.state_inventory(), before, "a rejected command changed the state folder")

    def test_a_successful_command_through_the_launcher_saves_the_python_cache(self):
        cli = self.launcher_cli()
        self.create_through_the_launcher(cli)
        shown = self.run_router("show", "task_1", cli=cli)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        saved = (self.state / "python").read_text(encoding="utf-8").strip()
        self.assertEqual(os.path.realpath(saved), os.path.realpath(sys.executable))

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

    def test_invalid_input_preserves_existing_and_absent_receipts(self):
        malformed = self.root / "malformed.json"
        malformed.write_text('{"private":"SECRET_PAYLOAD_NEVER_ECHO",}', encoding="utf-8")
        oversized = self.root / "oversized.json"
        oversized.write_bytes(b" " * (64 * 1024 + 1))
        directory = self.root / "directory"
        directory.mkdir()
        sources = (malformed, oversized, directory)
        for command in ("create", "record"):
            for source in sources:
                with self.subTest(state="absent", command=command, source=source.name):
                    self.assert_rejected_without_writes(command, source)
        self.create_and_warm()
        for command in ("create", "record"):
            for source in sources:
                with self.subTest(state="existing", command=command, source=source.name):
                    self.assert_rejected_without_writes(command, source)

    def test_record_accepts_finite_explicit_stdin(self):
        self.create()
        event = {"event_id": "e1", "kind": "attempt", "attempt_id": "try_1",
                 "actor_id": "worker_1", "revision": 1, "state": "running"}
        result = self.run_router("record", "task_1", "--file", "-", input_text=json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["attempts"][0]["state"], "running")

    @unittest.skipIf(IS_WINDOWS or not hasattr(os, "mkfifo"), "FIFO requires POSIX mkfifo")
    def test_fifo_without_writer_returns_before_deadline_and_preserves_state(self):
        fifo = self.root / "input.fifo"
        os.mkfifo(fifo)
        self.assert_rejected_without_writes("create", fifo, timeout=3)
        self.create_and_warm()
        self.assert_rejected_without_writes("record", fifo, timeout=3)

    @unittest.skipIf(IS_WINDOWS, "device and Unix socket fixtures require POSIX")
    def test_device_and_socket_input_preserve_state(self):
        if not Path("/dev/null").is_char_device() or not hasattr(socket, "AF_UNIX"):
            self.skipTest("character device or Unix sockets unavailable")
        socket_path = self.root / "input.sock"
        # Only the filesystem type matters. Linux can create this inode without
        # opening/binding a socket, including inside a network-disabled judge.
        if sys.platform.startswith("linux"):
            os.mknod(socket_path, stat.S_IFSOCK | 0o600)
        else:
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(socket_path))
        self.assertTrue(stat.S_ISSOCK(socket_path.stat().st_mode))
        for command in ("create", "record"):
            for source in ("/dev/null", socket_path):
                with self.subTest(state="absent", command=command, source=str(source)):
                    self.assert_rejected_without_writes(command, source, timeout=3)
        self.create_and_warm()
        for command in ("create", "record"):
            for source in ("/dev/null", socket_path):
                with self.subTest(state="existing", command=command, source=str(source)):
                    self.assert_rejected_without_writes(command, source, timeout=3)


if __name__ == "__main__":
    unittest.main()
