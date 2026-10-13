"""Exercise explicit C/D commands from a standalone copied package."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from _platform import home_env, run

ROOT = Path(__file__).resolve().parents[1]
ROUTER = (ROOT / "bin/router").is_file()
PACKAGE = "router" if ROUTER else "harness"
HOOKS = "router" if ROUTER else "checkpoint"
BRIEF = "\n".join(("PROFILE: operations/v1", "OBJECTIVE: Check fixture state.",
    "TARGET: A disposable non-Git fixture.", "ACTION: Read only.",
    "SCOPE: Its title.", "CONSTRAINTS: Preserve existing permissions.",
    "PROOF: Title digest matches.", "READBACK: Use a separate read channel.",
    "RETURN: Verdict and evidence reference."))


class StandaloneCommands(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="phase-cd-commands-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.source = self.base / "package"
        shutil.copytree(ROOT / "hooks", self.source / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        if ROUTER:
            (self.source / "bin").mkdir()
            shutil.copy2(ROOT / "bin/router", self.source / "bin/router")
        self.home = self.base / "home"
        self.home.mkdir()
        self.work = self.base / "non-git-target"
        self.work.mkdir()
        self.env = {**os.environ, **home_env(self.home), "PYTHONDONTWRITEBYTECODE": "1",
                    "XDG_STATE_HOME": str(self.home / "state"),
                    "ROUTER_STATE": str(self.home / "router-state")}

    def command(self, name, value):
        names = {"operations": "operations_cli.py", "observe": "observation_cli.py",
                 "evaluate": "evaluation_cli.py"}
        if ROUTER:
            args = [sys.executable, "-B", str(self.source / "bin/router"), name]
        else:
            args = [sys.executable, "-B", str(self.source / "hooks" / HOOKS / names[name])]
        if name == "operations":
            args.append("check")
        return run(args + ["--file", "-"], input=json.dumps(value).encode(),
                   capture_output=True, env=self.env, cwd=self.work, timeout=15)

    def test_operations_readiness_does_not_change_non_git_target_or_home(self):
        for available, code, status in ((None, 0, "unknown"), ([], 3, "declared_missing"),
                                       (["connector.read"], 0, "declared_available")):
            result = self.command("operations", dict(brief=BRIEF, agent="custom-reader",
                                  required_tools=["connector.read"], available_tools=available))
            self.assertEqual(result.returncode, code, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["tool_status"], status)
            self.assertFalse(report["host_verified"])
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(list(self.work.iterdir()), [])

    def test_observation_unknown_is_a_successful_report_without_mutation(self):
        result = self.command("observe", dict(expected=dict(schema_version=1, task_id="task",
            target_id="target", revision_id="rev", requirements={"title": "a" * 64}),
            observed=dict(schema_version=1, read_status="denied")))
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["reported_verdict"], "UNKNOWN")
        self.assertFalse(report["gates_mutation"])
        self.assertEqual(list(self.home.iterdir()), [])

    def test_evaluator_works_without_sibling_install_and_rejects_private_input(self):
        from test_evaluation import fixture
        result = self.command("evaluate", fixture())
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["decision"], "inconclusive")
        self.assertFalse(report["adopted"])
        bad = self.command("evaluate", {"private": "DO_NOT_ECHO"})
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn(b"DO_NOT_ECHO", bad.stdout + bad.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_plugin_cores_match_standalone_source(self):
        for name in ("operations.py", "observation.py", "evaluation.py"):
            original = ROOT / "hooks" / HOOKS / name
            bundled = ROOT / "plugins" / PACKAGE / "hooks" / HOOKS / name
            self.assertEqual(original.read_bytes(), bundled.read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
