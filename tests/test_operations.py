"""Opt-in operations brief and read-only pre-spawn assessment."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common
import operations
from _platform import home_env, run as platform_run

BRIEF = ("PROFILE: operations/v1\nOBJECTIVE: update record\nTARGET: remote-record-a\n"
         "ACTION: update\nSCOPE: record-a only\nCONSTRAINTS: preserve owner\n"
         "PROOF: receipt digest\nREADBACK: fetch record-a\nRETURN: five lines")


class OperationsTests(unittest.TestCase):
    def cli(self, root, request=None, path="-", argv=None):
        env = dict(os.environ, **home_env(root / "home"), XDG_STATE_HOME=str(root / "state"),
                   ROUTER_STATE=str(root / "router-state"), PYTHONDONTWRITEBYTECODE="1")
        args = ["check", "--file", path] if argv is None else argv
        return platform_run([sys.executable, "-B", str(ROOT / "bin/router"), "operations", *args],
                            input=json.dumps(request) if request is not None else "", text=True,
                            capture_output=True, env=env, cwd=root, timeout=20)

    def test_roundtrip_and_identity(self):
        fields = operations.parse_brief(BRIEF)
        self.assertEqual(operations.parse_brief(operations.render_brief(fields)), fields)
        self.assertNotEqual(common.brief_hash(BRIEF), common.brief_hash(BRIEF.replace("record-a", "record-b")))
        self.assertNotEqual(common.brief_hash(BRIEF), common.brief_hash(BRIEF.replace("SCOPE: record-a only", "SCOPE: record-a group")))

    def test_malformed_and_mixed(self):
        for value in (BRIEF + "\nTASK x", BRIEF.replace("ACTION: update", "ACTION:"),
                      BRIEF + "\nTARGET: repeat", BRIEF.replace("TARGET: remote-record-a\nACTION: update", "ACTION: update\nTARGET: remote-record-a"),
                      BRIEF.replace("PROFILE: operations/v1", "PROFILE: operations/v2"), BRIEF + "\nPROFILE: operations/v1",
                      BRIEF.replace("TARGET: remote-record-a", "TARGET: " + "a" * 4097), BRIEF + "x" * 16384):
            with self.subTest(value=value[:60]), self.assertRaises(operations.OperationsError):
                operations.parse_brief(value)

    def test_strict_headers_and_prose_continuations(self):
        invalid = (
            "  TASK: unrelated repository brief", "\tPROFILE: operations/v1",
            "  TARGET: duplicate", "objective: duplicate", "FILES: repo paths",
            "OBJECTIVE: duplicate", "RETURN: duplicate", "OBJECTIVE missing colon",
            "UNKNOWN_FIELD: extra", "unknown_field: extra", "PROFILE operations/v1",
        )
        for header in invalid:
            with self.subTest(header=header), self.assertRaises(operations.OperationsError):
                operations.parse_brief(BRIEF + "\n" + header)
        prose = ("\nPlease preserve the owner when updating.\n"
                 "Read back the exact record before reporting.\n"
                 "API responses may span several lines.")
        fields = operations.parse_brief(BRIEF + prose)
        self.assertIn("Please preserve", fields["RETURN"])
        self.assertIn("API responses", fields["RETURN"])

    def test_malformed_spaced_and_numbered_headers(self):
        headers = (
            "  profile : operations/v1", "\tPrOfIlE\t: operations/v1",
            "  UNKNOWN2: extra", "UNKNOWN2: extra", "unknown2 : extra",
            "  task : repository", " files : repository", "bar : repository",
            "target : duplicate", "RETURN : duplicate", "  rollback : extra",
            "PROFILE://operations/v1", "TARGET://duplicate",
        )
        for header in headers:
            with self.subTest(header=header):
                with self.assertRaises(operations.OperationsError) as caught:
                    operations.parse_brief(BRIEF + "\n" + header)
                self.assertLess(len(str(caught.exception)), 128)
                self.assertNotIn(header, str(caught.exception))

    def test_readback_url_and_multiline_prose_are_data(self):
        continuation = ("\n  https://example.invalid/record-a\n"
                        "\thttp://example.invalid/record-a\n"
                        "https://example.invalid/record-a\n"
                        "  custom+read://example.invalid/record-a\n"
                        "Please preserve the owner when updating.\n"
                        "Read back the exact record before reporting.\n"
                        "API responses may span several lines.\n"
                        "Please note: values remain data.")
        brief = BRIEF.replace("READBACK: fetch record-a", "READBACK: fetch record-a" + continuation)
        fields = operations.parse_brief(brief)
        self.assertEqual(fields["READBACK"], "fetch record-a" + continuation)
        self.assertEqual(operations.parse_brief(operations.render_brief(fields)), fields)
        self.assertIsNone(common.brief_problem(brief, "worker", common.load_routes()))

    def test_cli_argument_errors_are_bounded_without_input_echo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            private = "private-input" + "x" * 12000
            cases = (
                [private, "--file", "-"],
                ["check", "--file", "-", "--" + private],
                ["check", "--file", "-", private],
                ["check", "--file=" + private, "--unexpected"],
                ["check", "--file"], [],
            )
            for index, argv in enumerate(cases):
                with self.subTest(case=index):
                    result = self.cli(root, argv=argv)
                    self.assertEqual(result.returncode, 2)
                    self.assertEqual(result.stdout, "")
                    self.assertLess(len(result.stderr.encode("utf-8")), 256)
                    self.assertNotIn("private-input", result.stderr)
            self.assertEqual(list(root.iterdir()), [])

    def test_assessment_and_roles(self):
        self.assertIsNone(common.brief_problem(BRIEF, "worker", common.load_routes()))
        self.assertIsNone(common.brief_problem(BRIEF, "researcher", common.load_routes()))
        self.assertIn("worker", common.brief_problem(BRIEF, "builder", common.load_routes()))
        self.assertEqual(operations.assess_agent("worker", ["connector.read"], None)["tool_status"], "unknown")
        missing = operations.assess_agent("worker", ["connector.read", "connector.write"], ["connector.read"])
        self.assertEqual(missing["tool_status"], "declared_missing")
        self.assertEqual(missing["missing_tools"], ["connector.write"])
        self.assertFalse(missing["host_verified"])
        with self.assertRaises(operations.OperationsError):
            operations.assess_agent("worker", ["connector.write"], ["*"])

    def test_cli_preflight_no_spawn_and_preserves_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for host, identity in (
                ("claude", {"subagent_type": "named-custom", "prompt": BRIEF}),
                ("codex", {"agent_type": "named-custom", "task_name": "operation", "message": BRIEF}),
            ):
                payload = dict(identity, model="pinned", permission_mode="custom",
                               tools=["connector.read"], other={"x": 1})
                original = json.dumps(payload)
                spawned = []
                for available, expected_exit, status, count in (
                    ([], 3, "declared_missing", 0),
                    (None, 0, "unknown", 1),
                    (["connector.read"], 0, "declared_available", 2),
                ):
                    with self.subTest(host=host, status=status):
                        request = {"brief": BRIEF, "agent": "named-custom", "required_tools": ["connector.read"],
                                   "available_tools": available}
                        result = self.cli(root, request)
                        self.assertEqual(result.returncode, expected_exit, result.stderr)
                        report = json.loads(result.stdout)
                        self.assertEqual(report["tool_status"], status)
                        self.assertFalse(report["host_verified"])
                        # Fake coordinator intentionally dispatches unknown; it never calls a host/model.
                        if result.returncode == 0:
                            spawned.append(json.dumps(payload))
                        self.assertEqual(len(spawned), count)
                self.assertEqual(spawned, [original, original])
            self.assertEqual(list(root.iterdir()), [])

    def test_cli_rejects_builder_and_bad_inputs_without_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            valid = {"brief": BRIEF, "agent": "worker", "required_tools": [], "available_tools": None}
            for request in (dict(valid, agent="builder"), dict(valid, extra="x"),
                            dict(valid, required_tools=["*"]), dict(valid, brief=BRIEF + "\nTASK x"),
                            dict(valid, brief=BRIEF + "\n  TASK: mixed"),
                            dict(valid, brief=BRIEF + "\n  PROFILE: operations/v1")):
                with self.subTest(request=request):
                    result = self.cli(root, request)
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("remote-record-a", result.stderr)
            deep = "[" * 300 + "0" + "]" * 300
            result = platform_run([sys.executable, "-B", str(ROOT / "bin/router"), "operations", "check", "--file", "-"],
                                  input=deep, text=True, capture_output=True, cwd=root, timeout=20)
            self.assertEqual(result.returncode, 1)
            if hasattr(os, "mkfifo"):
                fifo = root / "input.fifo"
                os.mkfifo(fifo)
                self.assertEqual(self.cli(root, path=str(fifo)).returncode, 1)
                fifo.unlink()
            self.assertEqual(list(root.iterdir()), [])

    def test_non_git_worker_and_host_passthrough(self):
        routes = common.load_routes()
        self.assertIsNone(common.brief_problem(BRIEF, "worker", routes))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertFalse((root / ".git").exists())
            env = dict(os.environ, **home_env(root / "home"), ROUTER_STATE=str(root / "state"),
                       ROUTER_LOCAL="off", PYTHONDONTWRITEBYTECODE="1")
            for role, expected in (("worker", 0), ("builder", 2), ("builder-in-place", 2)):
                event = {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                         "tool_input": {"subagent_type": role, "prompt": BRIEF}, "cwd": str(root)}
                result = platform_run([sys.executable, "-B", str(ROOT / "hooks/router/spawn_guard.py")],
                                      input=json.dumps(event), text=True, capture_output=True,
                                      env=env, cwd=root, timeout=20)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected == 2:
                    self.assertIn("named custom agent or generic worker", result.stderr)
            for guard, tool, payload in (
                ("spawn_guard.py", "Agent", {"subagent_type": "named-custom", "prompt": BRIEF,
                                              "model": "pinned", "permission_mode": "custom", "tools": ["connector.read"]}),
                ("codex_spawn_guard.py", "spawn_agent", {"agent_type": "named-custom", "task_name": "operation",
                                                        "message": "encrypted:opaque", "model": "pinned",
                                                        "permission_mode": "custom", "tools": ["connector.read"]}),
            ):
                event = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": payload,
                         "cwd": str(root)}
                result = platform_run([sys.executable, "-B", str(ROOT / "hooks/router" / guard)],
                                      input=json.dumps(event), text=True, capture_output=True,
                                      env=env, cwd=root, timeout=20)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
                self.assertEqual(event["tool_input"], payload)


class Coordinator:
    """Fake coordinator: preflights the agent and brief taken from the payload it is about to
    dispatch, and dispatches only what the preflight allows; never calls a host or model.
    Each dispatch is kept with the request the preflight received and the report it printed."""
    def __init__(self, test, root):
        self.test, self.root, self.dispatched = test, root, []

    def run(self, payload, required, available):
        request = {"brief": payload["prompt"], "agent": payload["subagent_type"], "required_tools": required,
                   "available_tools": available}
        result = self.test.cli(self.root, request)
        report = json.loads(result.stdout)
        if result.returncode == 0:
            self.dispatched.append((request, report, json.dumps(payload)))
        return result.returncode, report


class PairedWorkflow(unittest.TestCase):
    def test_missing_stops_unknown_stays_unknown_and_identity_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            payload = {"subagent_type": "named-custom", "prompt": BRIEF, "model": "pinned\u00e9",
                       "permission_mode": "custom", "tools": ["connector.read", "connector.write"]}
            coordinator = Coordinator(OperationsTests("cli"), root)
            code, report = coordinator.run(payload, ["connector.write"], ["connector.read"])
            self.assertEqual((code, report["tool_status"], coordinator.dispatched), (3, "declared_missing", []))
            code, report = coordinator.run(payload, ["connector.write"], None)
            self.assertEqual((code, report["tool_status"], report["host_verified"]), (0, "unknown", False))
            code, report = coordinator.run(payload, ["connector.write"], ["connector.write"])
            self.assertEqual((code, report["tool_status"], report["host_verified"]), (0, "declared_available", False))
            self.assertEqual(len(coordinator.dispatched), 2)
            # What was dispatched is what was preflighted: the agent the preflight itself reported
            # and the brief it validated. A different agent or prompt after the check fails here.
            for request, checked, sent in coordinator.dispatched:
                sent = json.loads(sent)
                self.assertEqual((sent["subagent_type"], sent["prompt"], checked["brief_valid"]),
                                 (checked["agent"], request["brief"], True))
            # The preflight envelope is exactly brief, agent and tool declarations (any other key
            # is rejected), so model, permission mode and tool grants never reach it: it cannot
            # rewrite them, and it does not check them either.
            self.assertEqual(report["agent"], "named-custom")
            self.assertEqual(set(report), {"agent", "tool_status", "missing_tools", "host_verified",
                                           "authorization", "mode", "routing_class", "brief_valid"})
            self.assertNotIn("pinned", json.dumps(report))
            self.assertEqual([set(request) for request, _, _ in coordinator.dispatched],
                             [{"brief", "agent", "required_tools", "available_tools"}] * 2)
            self.assertEqual(list(root.iterdir()), [])


class HostValidationDocs(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_checklist_is_on_demand_and_separates_evidence_classes(self):
        text = self.read("docs/host-validation.md")
        for heading in ("## Evidence classes", "## On-demand checklist", "## Unrun here"):
            self.assertIn(heading, text)
        for label in ("Offline adapter tests", "Installed executable and hook tests",
                      "Live host, connector or provider tests"):
            self.assertIn(label, text)
        for needed in ("installed Claude Code version", "installed Codex version", "hook trust",
                       "custom agent identity", "model", "benign connector", "observer",
                       "agreed budget", "SSH_FIXTURE_REQUIRED=1", "HARNESS_DIR"):
            self.assertIn(needed.lower(), text.lower(), needed)
        for refused in ("keeps no qualification registry", "sends no telemetry"):
            self.assertIn(refused, text.lower())

    def test_docs_claim_no_unrun_qualification(self):
        for relative in ("docs/host-validation.md", "docs/operations.md",
                         "tests/fixtures/operations_ssh/README.md"):
            lowered = self.read(relative).lower()
            for claim in ("is qualified", "are qualified", "has been qualified", "fully qualified",
                          "qualified on", "verified live", "production-ready", "guarantees"):
                self.assertNotIn(claim, lowered, (relative, claim))
        self.assertIn("unrun", self.read("docs/host-validation.md").lower())

    def test_operations_doc_links_workflow_and_fixture(self):
        text = self.read("docs/operations.md")
        for needed in ("host-validation.md", "tests/test_operations_ssh.py", "SSH_FIXTURE_REQUIRED=1"):
            self.assertIn(needed, text)
        self.assertIn("operations/v1", text)

    def test_fixture_readme_reuses_harness_fixture(self):
        text = self.read("tests/fixtures/operations_ssh/README.md")
        self.assertIn("HARNESS_DIR", text)
        self.assertIn("ssh-disposable", text)
        self.assertIn("no second", text.lower())
        self.assertFalse([p for p in (ROOT / "tests/fixtures/operations_ssh").iterdir() if p.name != "README.md"])


if __name__ == "__main__":
    unittest.main()
