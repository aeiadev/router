#!/usr/bin/env python3
"""Promotion requires measured shadow evidence and only edits the local patch."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common


class PromoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.local = self.home / "config/router/routes.local.json"
        self.state = self.home / "state"
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / "config"),
                        ROUTER_LOCAL="", ROUTER_STATE=str(self.state),
                        CLAUDE_HOME=str(self.home / "claude"), CODEX_HOME=str(self.home / "codex"))

    def run_cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / "bin/router"), *args],
                              env=env or self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=15)

    def snapshot(self):
        """Every file hash and every directory and link name under the scratch home."""
        found = {}
        for path in sorted(self.home.rglob("*")):
            name = str(path.relative_to(self.home))
            if path.is_symlink():
                found[name] = "link:" + os.readlink(path)
            elif path.is_file():
                found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                found[name] = "dir"
        return found

    def log(self, stream, record):
        self.state.mkdir(exist_ok=True)
        with patch.dict(os.environ, self.env):
            common.log(stream, record)

    def assert_error(self, result, line, code=1):
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertIsNotNone(re.fullmatch(re.escape(line) + "\n", result.stderr), repr(result.stderr))
        self.assertEqual(result.stdout.count("promoted"), 0)

    def test_refusal_lines_in_full(self):
        self.assert_error(self.run_cli("promote", "risk"),
                          "router promote: no shadow hits for risk in the last 7d; run router report to look, or pass --force")
        self.assertFalse(self.local.exists())
        self.assert_error(self.run_cli("promote", "risk", "--since", "2d"),
                          "router promote: no shadow hits for risk in the last 2d; run router report to look, or pass --force")
        self.assert_error(self.run_cli("promote", "unknown"), "router promote: unknown rule: unknown")
        self.assert_error(self.run_cli("promote", "risk", "--since", "oops"),
                          "router promote: --since must look like 36h or 7d", 2)
        self.assert_error(self.run_cli("promote", "risk", "--since", "91d"),
                          "router promote: --since must be at most 90d", 2)
        self.assert_error(self.run_cli("promote", "risk", "--force", env=dict(self.env, ROUTER_LOCAL="off")),
                          "router promote: overlay is off (ROUTER_LOCAL=off)")
        self.assertFalse(self.local.exists())

    def test_invalid_overlay_refused_with_whole_line(self):
        self.local.parent.mkdir(parents=True)
        for text, detail in (("[]", "must be a JSON object"),
                             ('{"version":2}', "version: cannot change"),
                             ("bad json", "not valid JSON: Expecting value: line 1 column 1 (char 0)")):
            with self.subTest(text=text):
                self.local.write_text(text)
                self.assert_error(self.run_cli("promote", "risk", "--force"),
                                  f"router promote: {self.local}: {detail}")
                self.assertEqual(self.local.read_text(), text)

    def test_success_and_dry_run_lines_in_full(self):
        before = (ROOT / "hooks/router/routes.json").read_bytes()
        self.log("spawns", {"shadow": True, "rule": "risk"})
        evidence = "evidence: risk had 1 shadow hit(s) in the last 7d\nspawn.risk  1\n"
        dry = self.run_cli("promote", "risk", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIsNotNone(re.fullmatch(re.escape(evidence + f"would promote risk: shadow -> enforce ({self.local})\n"),
                                          dry.stdout), repr(dry.stdout))
        self.assertEqual(dry.stderr, "")
        self.assertFalse(self.local.exists())
        done = self.run_cli("promote", "risk")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNotNone(re.fullmatch(re.escape(evidence + f"promoted risk: shadow -> enforce ({self.local})\n"),
                                          done.stdout), repr(done.stdout))
        self.assertEqual(json.loads(self.local.read_text()), {"router": {"modes": {"risk": "enforce"}}})
        self.assertEqual(stat.S_IMODE(self.local.stat().st_mode), 0o600)
        self.assertEqual((ROOT / "hooks/router/routes.json").read_bytes(), before)
        self.assert_error(self.run_cli("promote", "risk"), "router promote: risk is enforce, not shadow")
        forced = self.run_cli("promote", "router.modes.risk", "--force", "--dry-run")
        self.assertEqual(forced.returncode, 1)

    def test_force_with_zero_hits_prints_zero_evidence(self):
        result = self.run_cli("promote", "risk", "--force", "--since", "36h")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(re.fullmatch(
            re.escape("evidence: risk had 0 shadow hit(s) in the last 36h\n"
                      f"promoted risk: shadow -> enforce ({self.local})\n"), result.stdout), repr(result.stdout))

    def test_dry_run_changes_nothing_under_home(self):
        self.log("spawns", {"shadow": True, "rule": "risk"})
        self.local.parent.mkdir(parents=True)
        self.local.write_text('{"context":{"files_threshold":4}}')
        before = self.snapshot()
        result = self.run_cli("promote", "risk", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)
        absent = self.home / "elsewhere/routes.json"
        before = self.snapshot()
        env = dict(self.env, ROUTER_LOCAL=str(absent))
        result = self.run_cli("promote", "risk", "--dry-run", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(re.fullmatch(
            re.escape("evidence: risk had 1 shadow hit(s) in the last 7d\n"
                      "spawn.risk  1\n"
                      f"would promote risk: shadow -> enforce ({absent})\n"), result.stdout), repr(result.stdout))
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(absent.parent.exists())

    def test_missing_explicit_overlay_is_created_private(self):
        target = self.home / "new/deeper/routes.json"
        env = dict(self.env, ROUTER_LOCAL=str(target))
        result = self.run_cli("promote", "large_read", "--force", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         "evidence: large_read had 0 shadow hit(s) in the last 7d\n"
                         f"promoted large_read: shadow -> enforce ({target})\n")
        self.assertEqual(json.loads(target.read_text()), {"context": {"modes": {"large_read": "enforce"}}})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["routes.json"])
        check = self.run_cli("config", "get", "context.modes.large_read", env=env)
        self.assertEqual(check.stdout.strip(), '"enforce"')

    def test_symlinked_overlay_is_refused_in_one_line(self):
        link = self.home / "link.json"
        link.symlink_to(self.home / "nowhere.json")
        env = dict(self.env, ROUTER_LOCAL=str(link))
        before = self.snapshot()
        self.assert_error(self.run_cli("promote", "risk", "--force", env=env),
                          f"router promote: {link}: not a regular file")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.home / "nowhere.json").exists())

    def test_decoy_and_force(self):
        decoy = self.home / "decoy.json"
        decoy.write_text('{"context":{"files_threshold":99}}')
        with patch.dict(os.environ, {"ROUTER_LOCAL": str(decoy), "XDG_CONFIG_HOME": str(decoy.parent)}):
            result = self.run_cli("promote", "large_read", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.local.read_text()), {"context": {"modes": {"large_read": "enforce"}}})
        self.assertEqual(decoy.read_text(), '{"context":{"files_threshold":99}}')

    def test_context_evidence_matches_report_and_hook_sees_enforce(self):
        self.log("context", {"event": "large_read", "action": "shadow"})
        self.log("context", {"event": "large_read", "action": "block"})
        report = self.run_cli("report", "--json")
        self.assertEqual(report.returncode, 0, report.stderr)
        count = json.loads(report.stdout)["shadow_hits"]["context.large_read"]
        result = self.run_cli("promote", "context.modes.large_read")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         f"evidence: context.modes.large_read had {count} shadow hit(s) in the last 7d\n"
                         f"context.large_read  {count}\n"
                         f"promoted context.modes.large_read: shadow -> enforce ({self.local})\n")
        self.assertEqual(json.loads(self.local.read_text()), {"context": {"modes": {"large_read": "enforce"}}})
        big = self.home / "big.txt"
        big.write_bytes(b"a" * 30_000)
        event = {"hook_event_name": "PreToolUse", "session_id": "promote-test",
                 "tool_name": "Read", "tool_input": {"file_path": str(big)}}
        hook = subprocess.run([sys.executable, str(ROOT / "hooks/router/context_guard.py")],
                              input=json.dumps(event), env=self.env, capture_output=True,
                              text=True, timeout=15)
        self.assertEqual(hook.returncode, 2, hook.stderr)
        self.assertIn("offset and limit", hook.stderr)

    def test_stop_cap_evidence_names_match_report(self):
        self.log("context", {"event": "stop_cap", "action": "shadow"})
        self.log("context", {"event": "handback_cap", "action": "shadow"})
        report = self.run_cli("report", "--json")
        self.assertEqual(report.returncode, 0, report.stderr)
        hits = json.loads(report.stdout)["shadow_hits"]
        result = self.run_cli("promote", "stop_cap_research")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         "evidence: stop_cap_research had 2 shadow hit(s) in the last 7d\n"
                         f"context.stop_cap  {hits['context.stop_cap']}\n"
                         f"context.handback_cap  {hits['context.handback_cap']}\n"
                         f"promoted stop_cap_research: shadow -> enforce ({self.local})\n")


if __name__ == "__main__":
    unittest.main()
