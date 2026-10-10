#!/usr/bin/env python3
"""`router report`: ledger tables, log counts, token sums and advice, against temporary state.

The ledger is built through ledger.py's own writers with explicit times. Nothing here calls a model
or the network. The Claude transcript row shape is reasoned from common.session_model, not verified
against a live session.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROUTER = HERE.parent / "hooks" / "router"
sys.path.insert(0, str(ROUTER))
import cmd_report  # noqa: E402
import ledger  # noqa: E402

HOUR = 3600
DAY = 86400
SENTINEL = "SENTINEL_LABEL_8W2"
BRIEF_1 = "brief-sentinel-b1"
BRIEF_2 = "brief-sentinel-b2"
BRIEF_3 = "brief-sentinel-b3"
BRIEF_4 = "brief-sentinel-b4"
BRIEF_5 = "brief-sentinel-b5"


def stamp(seconds) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


class ReportCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-report-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.claude = self.root / "claude"
        self.db = self.state / "ledger.sqlite3"
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("ROUTER_") and key not in ("XDG_STATE_HOME", "CLAUDE_HOME")}
        environment.update({"HOME": str(self.root / "home"), "XDG_CONFIG_HOME": str(self.root / "config"),
                            "ROUTER_LOCAL": "off", "ROUTER_STATE": str(self.state),
                            "CLAUDE_HOME": str(self.claude)})
        isolated = patch.dict(os.environ, environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.now = time.time()

    def run_report(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cmd_report.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def lane(self, key, brief, tier, age, status="running", verdicts=(), label=None, role="builder"):
        """One spawn age seconds ago; then optional (verdict, round, age) rows and the final status."""
        started = self.now - age
        decision = ledger.record_attempt_and_lane(key, lambda prior: {"decision": "allow", "tier": tier}, role=role,
                                                  project="proj", session="s1", brief=brief, label=label, now=started)
        self.assertIsNotNone(decision)
        if status != "running":
            self.assertIsNotNone(ledger.mark_lane("s1", brief, status, now=started + 60))
        for verdict, round_, verdict_age in verdicts:
            self.assertTrue(ledger.record_verdict(key, verdict, project="proj", session="s1", round=round_,
                                                  now=self.now - verdict_age))

    def write_log(self, name, records, pad=0):
        self.state.mkdir(parents=True, exist_ok=True)
        lines = []
        if pad:
            lines.append(json.dumps({"ts": stamp(self.now - 40 * DAY), "event": "pad", "x": "x" * pad}))
        lines += [r if isinstance(r, str) else json.dumps(r) for r in records]
        (self.state / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def transcript(self, name, rows, age=HOUR):
        path = self.claude / "projects" / "p1" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows), encoding="utf-8")
        os.utime(path, (self.now - age, self.now - age))


def assistant(model, **usage):
    return {"type": "assistant", "message": {"role": "assistant", "model": model, "usage": usage,
                                             "content": [{"type": "text", "text": "SENTINEL_REPLY_4D"}]}}


class LedgerSectionTests(ReportCase):
    def assert_private(self, out):
        self.assertNotIn(SENTINEL, out)
        for key in ("c:k1", "c:k2", "x:k4", BRIEF_1):
            self.assertNotIn(key, out)

    def build(self):
        self.lane("c:k1", BRIEF_1, "std", 3 * HOUR, "returned", [("SEND_BACK", 1, 3 * HOUR - 120)], label=SENTINEL)
        ledger.mark_lane("s1", BRIEF_1, "judged", now=self.now - 3 * HOUR + 130)
        self.lane("c:k1", BRIEF_1, "std", 2 * HOUR, "returned", [("PASS", 2, 2 * HOUR - 120)])
        ledger.mark_lane("s1", BRIEF_1, "judged", now=self.now - 2 * HOUR + 130)
        self.lane("c:k2", BRIEF_2, "std", 4 * HOUR, "returned", [("PASS", 1, 4 * HOUR - 120)])
        ledger.mark_lane("s1", BRIEF_2, "judged", now=self.now - 4 * HOUR + 130)
        self.lane("c:k3", BRIEF_3, "up", 5 * HOUR, "returned", [("UNKNOWN", None, 5 * HOUR - 120)])
        self.lane("x:k4", BRIEF_4, "std", 6 * HOUR)
        # Old rows go last: their spawn times make the daily prune run with an old clock.
        self.lane("c:old", BRIEF_5, "std", 10 * DAY, "judged", [("PASS", 1, 10 * DAY - 120)])

    def test_grouping_counts_and_the_claude_codex_split(self):
        self.build()
        code, out, err = self.run_report()
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"(?m)^router report: last 7d \(since \d{4}-\d\d-\d\d \d\d:\d\d UTC\)$")
        self.assertRegex(out, r"(?ms)^spawns by role and tier\nrole +tier +host +spawns\n"
                              r"builder +std +claude +3\nbuilder +std +codex +1\nbuilder +up +claude +1\n\n")
        self.assertRegex(out, r"(?m)^returned 4  judged 3  unjudged 1  judged-rate 75%$")
        self.assertRegex(out, r"(?m)^codex lanes: 1 \(no verdicts on Codex\)$")

    def test_first_pass_uses_the_first_verdict_and_excludes_unknown(self):
        self.build()
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^first pass by tier\ntier +briefs +first-pass +rate +rounds-to-PASS\n"
                              r"std +2 +1 +50% +1\.5\nunknown verdicts: 1\n")
        self.assertNotRegex(out, r"(?m)^up ")

    def test_zero_returned_gives_a_dash(self):
        self.lane("c:k1", BRIEF_1, "std", HOUR)
        self.assertRegex(self.run_report()[1], r"(?m)^returned 0  judged 0  unjudged 0  judged-rate -$")

    def test_window_excludes_old_rows_and_a_longer_window_includes_them(self):
        self.build()
        self.assertRegex(self.run_report("--since", "36h")[1], r"(?m)^builder +std +claude +3$")
        self.assertRegex(self.run_report("--since", "36h")[1], r"(?m)^router report: last 36h ")
        out = self.run_report("--since", "30d")[1]
        self.assertRegex(out, r"(?m)^builder +std +claude +4$")
        self.assertRegex(out, r"(?m)^std +3 +2 +67% +1\.3$")
        small = self.run_report("--since", "3h30m")
        self.assertEqual(small[0], 2)

    def test_json_keys_and_privacy(self):
        self.build()
        code, out, err = self.run_report("--json")
        self.assertEqual((code, err), (0, ""))
        data = json.loads(out)
        self.assertEqual(sorted(data), ["advice", "context", "first_pass", "judge_coverage", "now", "shadow_hits",
                                        "since", "spawns", "tokens"])
        self.assertEqual(out, json.dumps(data, sort_keys=True, indent=2) + "\n")
        self.assertEqual(data["judge_coverage"]["judged"], 3)
        for text in (out, self.run_report()[1]):
            self.assert_private(text)

    def test_privacy_key_is_distinct_from_temporary_path(self):
        self.root = self.root / "b1b1"
        self.state = self.root / "state"
        self.claude = self.root / "claude"
        self.db = self.state / "ledger.sqlite3"
        os.environ["ROUTER_STATE"] = str(self.state)
        os.environ["CLAUDE_HOME"] = str(self.claude)
        self.build()
        out = self.run_report()[1]
        self.assertIn("b1", out)
        self.assert_private(out)

    def test_setup_pins_home_config_and_overlay(self):
        hostile = {"HOME": "/hostile-home", "XDG_CONFIG_HOME": "/hostile-config", "ROUTER_LOCAL": "/hostile-overlay.json"}
        with patch.dict(os.environ, hostile):
            fresh = ReportCase("runTest")
            fresh.setUp()
            try:
                self.assertEqual((os.environ.get("HOME"), os.environ.get("XDG_CONFIG_HOME"), os.environ.get("ROUTER_LOCAL")),
                                 (str(fresh.root / "home"), str(fresh.root / "config"), "off"))
            finally:
                fresh.doCleanups()

    def test_since_limit_and_format(self):
        self.assertEqual(self.run_report("--since", "91d"), (2, "", "router report: --since must be at most 90d\n"))
        self.assertEqual(self.run_report("--since", "2161h")[2], "router report: --since must be at most 90d\n")
        self.assertEqual(self.run_report("--since", "90d")[0], 0)
        self.assertEqual(self.run_report("--since", "soon"),
                         (2, "", "router report: --since must look like 36h or 7d\n"))

    def test_no_ledger_gives_empty_sections_and_creates_nothing(self):
        code, out, err = self.run_report()
        self.assertEqual((code, err), (0, ""))
        self.assertRegex(out, r"(?m)^spawns by role and tier\nnone\n")
        self.assertRegex(out, r"(?m)^first pass by tier\nnone\nunknown verdicts: 0$")
        self.assertRegex(out, r"(?m)^returned 0  judged 0  unjudged 0  judged-rate -$")
        self.assertFalse(self.db.exists())
        self.assertFalse(self.state.exists(), "a report must not create the state directory")

    def test_broken_ledger_exits_one_with_the_path(self):
        self.state.mkdir(mode=0o700)
        self.db.write_bytes(b"not a sqlite database\n" * 64)
        code, out, err = self.run_report()
        self.assertEqual((code, out), (1, ""))
        self.assertRegex(err, r"\Arouter report: " + re.escape(str(self.db)) + r": .+\n\Z")


class LadderAttributionTests(ReportCase):
    def test_ladder_climb_credits_each_verdict_to_the_tier_it_ran_at(self):
        self.lane("c:k1", BRIEF_1, "std", 3 * HOUR, "returned", [("SEND_BACK", 1, 3 * HOUR - 120)])
        self.lane("c:k1", BRIEF_2, "up", 2 * HOUR, "returned", [("PASS", 2, 2 * HOUR - 120)])
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^first pass by tier\ntier +briefs +first-pass +rate +rounds-to-PASS\n"
                              r"std +1 +0 +0% +-\nup +0 +0 +- +2\.0\nunknown verdicts: 0\n")

    def test_verdict_with_a_deleted_lane_shows_tier_question_mark(self):
        self.lane("c:k1", BRIEF_1, "std", 3 * HOUR, "returned", [("PASS", 1, 3 * HOUR - 120)])
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM lanes")
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^first pass by tier\ntier +briefs +first-pass +rate +rounds-to-PASS\n"
                              r"\? +1 +1 +100% +1\.0\nunknown verdicts: 0\n")


class FirstPassCountingTests(ReportCase):
    def test_only_a_pass_first_verdict_counts_as_first_pass(self):
        for index, verdict in enumerate(("FAIL", "NOT_DONE", "CANNOT_COMPLETE", "SEND_BACK", "PASS")):
            self.lane(f"c:k{index}", f"b{index}", "std", 3 * HOUR, "returned", [(verdict, 1, 3 * HOUR - 120)])
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^first pass by tier\ntier +briefs +first-pass +rate +rounds-to-PASS\n"
                              r"std +5 +1 +20% +1\.0\nunknown verdicts: 0\n")


class ArchivedSpawnTests(ReportCase):
    def test_archived_attempts_count_in_spawns(self):
        self.lane("c:k1", BRIEF_1, "std", 20 * HOUR)
        self.lane("c:k1", BRIEF_2, "up", HOUR)
        import sqlite3
        with sqlite3.connect(self.db) as conn:
            rounds = sorted(r[0] for r in conn.execute("SELECT round FROM attempts"))
        self.assertTrue(rounds[0] < 0 and rounds[1] > 0, rounds)
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^spawns by role and tier\nrole +tier +host +spawns\n"
                              r"builder +std +claude +1\nbuilder +up +claude +1\n\n")


class LogSectionTests(ReportCase):
    def records(self):
        new, old = self.now - HOUR, self.now - 20 * DAY
        ctx = lambda age, **fields: {"ts": stamp(self.now - age), **fields}  # noqa: E731
        context = [
            ctx(40 * DAY, event="stop_cap", action="block", **{"class": "exec"}),
            "this line is not json",
            ctx(HOUR, event="stop_cap", action="block", **{"class": "exec"}),
            ctx(HOUR, event="stop_cap", action="block", **{"class": "exec"}),
            ctx(HOUR, event="stop_cap", action="shadow", **{"class": "exec"}),
            ctx(HOUR, event="stop_cap", action="second_pass", **{"class": "judge"}),
            ctx(HOUR, event="stop_cap", action="ok", **{"class": "exec"}),
            ctx(HOUR, event="large_read", action="block", bytes=50000),
            ctx(HOUR, event="large_read", action="block", bytes=50000),
            ctx(HOUR, event="large_read", action="shadow", bytes=50000),
            ctx(HOUR, event="auto_block", role="builder"),
            ctx(20 * DAY, event="large_read", action="block", bytes=50000),
            '{"ts": "2026-01-01T00:00:00Z", "event": "stop_ca',
        ]
        spawns = [ctx(HOUR, rule="role-pin", shadow=True), ctx(HOUR, rule="role-pin", shadow=True),
                  ctx(HOUR, rule="ladder", shadow=False), ctx(40 * DAY, rule="role-pin", shadow=True),
                  ctx(HOUR, rule="brief-shape", shadow=True, brief=SENTINEL)]
        self.write_log("context", context, pad=5 * 1024 * 1024)
        self.write_log("spawns", spawns)
        return new, old

    def test_context_guard_counts_ignore_old_bad_and_cut_lines(self):
        self.records()
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^context guard\nover-cap returns: 4\n  exec: 3\n  judge: 1\n"
                              r"large-read blocks: 2 \(shadow: 1\)\nauto blocks: 1\n\nshadow hits\n")
        data = json.loads(self.run_report("--json")[1])
        self.assertEqual(data["context"]["over_cap"], 4)

    def test_shadow_hits_use_spawn_and_context_key_names(self):
        self.records()
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^shadow hits\ncontext\.large_read  1\ncontext\.stop_cap  1\n"
                              r"spawn\.brief-shape  1\nspawn\.role-pin  2\n\n")
        self.assertNotIn(SENTINEL, out)

    def test_missing_logs_and_no_hits(self):
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^context guard\nover-cap returns: 0\nlarge-read blocks: 0 \(shadow: 0\)\n"
                              r"auto blocks: 0\n\nshadow hits\nnone\n")

    def test_window_filter_drops_old_records(self):
        self.write_log("context", [{"ts": stamp(self.now - 30 * DAY), "event": "auto_block"}])
        self.assertRegex(self.run_report()[1], r"(?m)^auto blocks: 0$")
        self.assertRegex(self.run_report("--since", "90d")[1], r"(?m)^auto blocks: 1$")


class TokenTests(ReportCase):
    def test_sums_per_model_and_skips_synthetic_bad_and_old_files(self):
        usage = dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=100, cache_creation_input_tokens=7)
        self.transcript("a.jsonl", [assistant("claude-sonnet-x", **usage), "not json at all",
                                    assistant("claude-sonnet-x", **usage), assistant("claude-opus-y", **usage),
                                    assistant("<synthetic>", **usage), {"type": "user", "message": {"role": "user"}}])
        self.transcript("b.jsonl", [assistant("claude-old-z", **usage)], age=30 * DAY)
        out = self.run_report()[1]
        self.assertRegex(out, r"(?ms)^tokens by model\nsampled 1 transcripts\n"
                              r"model +input +output +cache_read +cache_creation\n"
                              r"claude-opus-y +10 +5 +100 +7\nclaude-sonnet-x +20 +10 +200 +14\n"
                              r"tokens: Codex rollouts are not read\n")
        self.assertNotIn("claude-old-z", out)
        self.assertNotIn("<synthetic>", out)
        self.assertNotIn("SENTINEL_REPLY_4D", out)
        self.assertNotRegex(out, r"\$|dollar|cost")

    def test_no_readable_transcripts(self):
        out = self.run_report()[1]
        self.assertIn(f"tokens by model\ntokens: no readable transcripts under {self.claude / 'projects'}\n"
                      "tokens: Codex rollouts are not read\n", out)


class AdviceTests(ReportCase):
    def advice(self):
        return self.run_report()[1].split("\nadvice\n", 1)[1].splitlines()

    def test_none_fires(self):
        self.assertEqual(self.advice(), ["advice: none"])

    def test_unjudged_builder_returns(self):
        self.lane("c:k1", BRIEF_1, "std", HOUR, "returned")
        self.lane("c:k2", BRIEF_2, "std", HOUR, "returned")
        self.assertEqual(self.advice(), ["advice: 2 builder return(s) were never judged; "
                                         "spawn judge with the same TASK and FILES."])

    def test_low_first_pass_tier_needs_five_briefs(self):
        for index in range(4):
            self.lane(f"c:k{index}", f"b{index}", "std", HOUR, "judged", [("SEND_BACK", 1, HOUR - 120)])
        self.assertEqual(self.advice(), ["advice: none"])
        self.lane("c:k4", BRIEF_4, "std", HOUR, "judged", [("SEND_BACK", 1, HOUR - 120)])
        self.assertEqual(self.advice(), ["advice: tier std passed first time in 0% of 5 briefs; "
                                         "tighten TASK, FILES and BAR before spawning."])

    def test_half_first_pass_is_not_low(self):
        for index in range(6):
            self.lane(f"c:k{index}", f"b{index}", "std", HOUR, "judged",
                      [("PASS" if index % 2 else "SEND_BACK", 1, HOUR - 120)])
        self.assertEqual(self.advice(), ["advice: none"])

    def test_over_cap_needs_three(self):
        record = {"ts": stamp(self.now - HOUR), "event": "stop_cap", "action": "block", "class": "exec"}
        self.write_log("context", [record, record])
        self.assertEqual(self.advice(), ["advice: none"])
        self.write_log("context", [record, record, record])
        self.assertEqual(self.advice(), ["advice: 3 returns went over the cap; ask for conclusions first and a file path."])


class WholeReportTest(ReportCase):
    def test_one_full_report(self):
        LedgerSectionTests.build(self)
        record = {"ts": stamp(self.now - HOUR), "event": "stop_cap", "action": "block", "class": "exec"}
        self.write_log("context", [record, record, record, {"ts": stamp(self.now - HOUR), "event": "large_read",
                                                            "action": "shadow"}])
        self.write_log("spawns", [{"ts": stamp(self.now - HOUR), "rule": "role-pin", "shadow": True}])
        self.transcript("a.jsonl", [assistant("claude-sonnet-x", input_tokens=3, output_tokens=4)])
        out = self.run_report()[1]
        expected = [
            r"router report: last 7d \(since \d{4}-\d\d-\d\d \d\d:\d\d UTC\)",
            "",
            "spawns by role and tier",
            r"role {4,}tier +host +spawns",
            r"builder +std +claude +3",
            r"builder +std +codex +1",
            r"builder +up +claude +1",
            "",
            "judge coverage",
            "returned 4  judged 3  unjudged 1  judged-rate 75%",
            r"codex lanes: 1 \(no verdicts on Codex\)",
            "",
            "first pass by tier",
            r"tier +briefs +first-pass +rate +rounds-to-PASS",
            r"std +2 +1 +50% +1\.5",
            "unknown verdicts: 1",
            "",
            "context guard",
            "over-cap returns: 3",
            "  exec: 3",
            r"large-read blocks: 0 \(shadow: 1\)",
            "auto blocks: 0",
            "",
            "shadow hits",
            r"context\.large_read  1",
            r"spawn\.role-pin  1",
            "",
            "tokens by model",
            "sampled 1 transcripts",
            r"model +input +output +cache_read +cache_creation",
            r"claude-sonnet-x +3 +4 +0 +0",
            "tokens: Codex rollouts are not read",
            "",
            "advice",
            r"advice: 1 builder return\(s\) were never judged; spawn judge with the same TASK and FILES\.",
            r"advice: 3 returns went over the cap; ask for conclusions first and a file path\.",
        ]
        self.assertRegex(out, "\\A" + "\n".join(expected) + "\\n\\Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
