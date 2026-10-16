"""Paired operations workflow against the accepted Harness disposable SSH fixture.

HARNESS_DIR names the exact Harness checkout. Its fixture and its command guard are
reused unchanged; this file has no SSH parser or server of its own. The real tests skip
explicitly unless SSH_FIXTURE_RUN=1 or SSH_FIXTURE_REQUIRED=1; when required, a missing
checkout, engine, image or guard fails instead of skipping.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
from observation import assess  # noqa: E402

REQUIRED = os.environ.get("SSH_FIXTURE_REQUIRED") == "1"
ENABLED = REQUIRED or os.environ.get("SSH_FIXTURE_RUN") == "1"
HARNESS = Path(os.environ["HARNESS_DIR"]) if os.environ.get("HARNESS_DIR") else None
BRIEF = ("PROFILE: operations/v1\nOBJECTIVE: promote the disposable artifact\nTARGET: /srv/site/VERSION\n"
         "ACTION: upload then rename once\nSCOPE: the fixture release folder only\n"
         "CONSTRAINTS: use the worker credential; never remove the target\n"
         "PROOF: observer digest equals the predeclared digest\nREADBACK: read the target as observer\n"
         "RETURN: result and evidence reference")
PAYLOAD = {"subagent_type": "named-custom", "model": "pinned", "tools": ["fixture.ssh"],
           "permission_mode": "custom", "prompt": BRIEF}


def load_fixture():
    run = HARNESS / "tests/fixtures/ssh-disposable/run.py"
    spec = importlib.util.spec_from_file_location("harness_ssh_disposable", run)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Gate(unittest.TestCase):
    def test_required_gate_has_a_harness_checkout(self):
        if REQUIRED:
            self.assertIsNotNone(HARNESS, "SSH_FIXTURE_REQUIRED=1 needs HARNESS_DIR")
            self.assertTrue((HARNESS / "tests/fixtures/ssh-disposable/run.py").is_file())
            self.assertTrue((HARNESS / "hooks/pyrun.sh").is_file())
        elif not ENABLED:
            self.skipTest("real SSH workflow unrun; set SSH_FIXTURE_REQUIRED=1 and HARNESS_DIR for the gate")


class RealPairedWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ENABLED:
            raise unittest.SkipTest("real SSH workflow unrun; set SSH_FIXTURE_REQUIRED=1 and HARNESS_DIR for the gate")
        if HARNESS is None:
            raise AssertionError("HARNESS_DIR must name the exact Harness checkout")
        cls.module = load_fixture()

    def fixture(self):
        return self.module.SSHFixture(os.environ.get("SSH_FIXTURE_ENGINE"),
                                      os.environ.get("SSH_FIXTURE_IMAGE", self.module.DEFAULT_IMAGE))

    def preflight(self, home, payload, required, available):
        """Preflight the agent and brief of the payload about to be dispatched. Model, permission
        mode and tool grants cannot be sent: the envelope rejects any other key."""
        request = {"brief": payload["prompt"], "agent": payload["subagent_type"], "required_tools": required,
                   "available_tools": available}
        env = dict(os.environ, HOME=str(home), XDG_STATE_HOME=str(home / "state"),
                   XDG_CONFIG_HOME=str(home / "config"), ROUTER_STATE=str(home / "router-state"),
                   PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), "operations", "check",
                                 "--file", "-"], input=json.dumps(request), text=True, capture_output=True,
                                env=env, cwd=home, timeout=20)
        return result.returncode, json.loads(result.stdout) if result.stdout else None

    def guard(self, home, project, command, tool):
        env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / "config"),
                   XDG_STATE_HOME=str(home / "state"), HARNESS_PYTHON=sys.executable,
                   PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONPATH", None)
        key = "command" if tool == "Bash" else "cmd"
        payload = {"tool_name": tool, "tool_input": {key: command}, "cwd": str(project)}
        return subprocess.run(["sh", str(HARNESS / "hooks/pyrun.sh"), "harness", "block",
                               "guard/danger_cmd_guard.py"], input=json.dumps(payload).encode(),
                              capture_output=True, env=env, cwd=home, timeout=10)

    def enroll(self, home, server):
        project = home / "project"
        (project / ".harness").mkdir(parents=True)
        raw = json.dumps(server.policy()).encode()
        (project / ".harness/operations.json").write_bytes(raw)
        registry = home / "config/harness/operations-bindings.json"
        registry.parent.mkdir(parents=True, mode=0o700)
        registry.write_text(json.dumps({"schema_version": 1, "bindings": [{
            "project_root": str(project), "policy_path": ".harness/operations.json",
            "sha256": digest(raw), "mode": "enforce"}]}))
        registry.chmod(0o600)
        return project

    def expected(self, data, revision="rev_1"):
        return {"schema_version": 1, "task_id": "paired", "target_id": "site_version",
                "revision_id": revision, "requirements": {"content": digest(data)}}

    def observed(self, data, revision="rev_1"):
        return {"schema_version": 1, "read_status": "ok", "task_id": "paired",
                "target_id": "site_version", "revision_id": revision,
                "evidence_id": "actual_ssh_readback", "verifier_id": "observer_account",
                "requirements": {"content": digest(data)}}

    def report(self, home, expected, observed):
        env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), "observe", "--file", "-"],
                                input=json.dumps({"expected": expected, "observed": observed}),
                                text=True, capture_output=True, env=env, cwd=home, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_missing_tool_stops_and_unknown_stays_unknown_before_any_ssh(self):
        with tempfile.TemporaryDirectory(prefix="ops-ssh-") as temporary, self.fixture() as server:
            home = Path(temporary)
            dispatched = []
            for available, code, status in (([], 3, "declared_missing"), (None, 0, "unknown"),
                                            (["fixture.ssh"], 0, "declared_available")):
                exit_code, report = self.preflight(home, PAYLOAD, ["fixture.ssh"], available)
                self.assertEqual((exit_code, report["tool_status"], report["host_verified"]),
                                 (code, status, False))
                if exit_code == 0:
                    dispatched.append((report, json.dumps(PAYLOAD)))
            self.assertEqual(len(dispatched), 2)
            # The dispatched agent is the one the preflight reported checking. Preflight never
            # receives model, permission mode or tool grants, so it neither alters nor checks them.
            for checked, sent in dispatched:
                self.assertEqual((json.loads(sent)["subagent_type"], checked["brief_valid"]), (checked["agent"], True))
            self.assertEqual(report["agent"], "named-custom")
            self.assertEqual(set(report), {"agent", "tool_status", "missing_tools", "host_verified",
                                           "authorization", "mode", "routing_class", "brief_valid"})
            self.assertEqual(server.mutations, 0)
            self.assertEqual(server.execute(server.ssh("observer", "cat " + self.module.TARGET)).stdout,
                             b"fixture revision zero\n")

    def test_authorized_operation_reaches_server_and_observer_reads_actual_bytes(self):
        module = self.module
        with tempfile.TemporaryDirectory(prefix="ops-ssh-") as temporary, self.fixture() as server:
            home = Path(temporary)
            project = self.enroll(home, server)
            self.assertEqual(self.preflight(home, PAYLOAD, ["fixture.ssh"], ["fixture.ssh"])[0], 0)
            before = server.execute(server.ssh("observer", "cat " + module.TARGET))
            self.assertNotEqual(before.stdout, module.PAYLOAD)
            for command in (server.upload(),
                            server.ssh("deploy", "mv /srv/site/releases/artifact.txt " + module.TARGET)):
                for tool in ("Bash", "functions.exec_command"):
                    result = self.guard(home, project, command, tool)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), tool)
                self.assertEqual(server.execute(command, mutation=True).returncode, 0)
            self.assertEqual(server.mutations, 2)
            actual = server.execute(server.ssh("observer", "cat " + module.TARGET))
            self.assertEqual(actual.stdout, module.PAYLOAD)
            expected = self.expected(module.PAYLOAD)
            report = self.report(home, expected, self.observed(actual.stdout))
            self.assertEqual(report["reported_verdict"], "PASS")
            self.assertEqual((report["gates_mutation"], report["host_verified"],
                              report["independent_acceptance"]), (False, False, "unverified"))
            # Stale (earlier revision, earlier bytes) and mismatched readback fail.
            self.assertEqual(self.report(home, expected, self.observed(before.stdout, "rev_0"))
                             ["reported_verdict"], "FAIL")
            self.assertEqual(self.report(home, expected, self.observed(actual.stdout, "rev_0"))
                             ["reported_verdict"], "FAIL")
            self.assertEqual(self.report(home, expected, self.observed(b"other bytes\n"))
                             ["reported_verdict"], "FAIL")
            # The observer credential is refused a write and worker-only evidence, by the server.
            write = server.execute(server.ssh("observer", "mkdir /srv/site/releases/observer-write"))
            self.assertNotEqual(write.returncode, 0)
            self.assertEqual(server.execute(server.ssh("observer", "test -e /srv/site/releases/observer-write"))
                             .returncode, 1)
            denied = server.execute(server.ssh("observer", "cat /srv/site/worker-only"))
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(denied.stdout, b"")
            unavailable = server.execute(server.ssh("observer", "cat " + module.TARGET).replace("-p 22 ", "-p 23 ", 1))
            self.assertEqual(unavailable.returncode, 255)
            count = server.mutations
            for status in ("denied", "unavailable"):
                report = self.report(home, expected, {"schema_version": 1, "read_status": status})
                self.assertEqual(report["reported_verdict"], "UNKNOWN")
                self.assertEqual(server.mutations, count)
            self.assertEqual(server.execute(server.ssh("observer", "cat " + module.TARGET)).stdout,
                             module.PAYLOAD)

    def test_supplied_json_is_not_authenticated_readback(self):
        with tempfile.TemporaryDirectory(prefix="ops-ssh-") as temporary, self.fixture() as server:
            home = Path(temporary)
            claimed = b"bytes the server never held\n"
            report = self.report(home, self.expected(claimed), self.observed(claimed))
            self.assertEqual(report["reported_verdict"], "PASS")
            self.assertEqual((report["host_verified"], report["independent_acceptance"]), (False, "unverified"))
            actual = server.execute(server.ssh("observer", "cat " + self.module.TARGET))
            self.assertNotEqual(actual.stdout, claimed)
            self.assertEqual(server.mutations, 0)

    def test_prohibited_operation_is_denied_before_dispatch(self):
        module = self.module
        with tempfile.TemporaryDirectory(prefix="ops-ssh-") as temporary, self.fixture() as server:
            home = Path(temporary)
            project = self.enroll(home, server)
            command = server.ssh("deploy", "rm " + module.TARGET)
            dispatched = []
            for tool in ("Bash", "functions.exec_command"):
                result = self.guard(home, project, command, tool)
                self.assertEqual(result.returncode, 2, (tool, result.stderr))
                self.assertEqual(result.stdout, b"")
                self.assertTrue(result.stderr)
                self.assertLessEqual(len(result.stderr), 512)
                self.assertNotIn(str(home).encode(), result.stderr)
                if result.returncode == 0:
                    dispatched.append(server.execute(command, mutation=True))
            self.assertEqual((dispatched, server.mutations), ([], 0))
            self.assertEqual(server.execute(server.ssh("observer", "cat " + module.TARGET)).stdout,
                             b"fixture revision zero\n")


if __name__ == "__main__":
    unittest.main()
