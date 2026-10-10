#!/usr/bin/env python3
"""Lanes and verdicts: SubagentStop linking, the judge reminder, the compact note and `router lanes`.

Hooks run as subprocesses (spawn_guard.py, context_guard.py, judge_reminder.py, session_note.py and
bin/router) against temporary state. Nothing here calls a model or the network.
"""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ROUTER = ROOT / "hooks" / "router"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROUTER))
import common  # noqa: E402
import ledger  # noqa: E402
from test_ledger import assert_absent_from_ledger  # noqa: E402

SPAWN_GUARD = ROUTER / "spawn_guard.py"
CONTEXT_GUARD = ROUTER / "context_guard.py"
JUDGE_REMINDER = ROUTER / "judge_reminder.py"
SESSION_NOTE = ROUTER / "session_note.py"
SESSION = "verdict-session"
HOUR = 3600


def brief(task, files="src/text.py"):
    return f"TASK {task}\nFILES {files}\nBAR python3 -m unittest\nRETURN five lines"


class LaneCase(unittest.TestCase):
    """A temporary home, state dir, routes copy and a git repo the spawns run in."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-verdicts-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.db = self.state / "ledger.sqlite3"
        self.project = self.root / "repo"
        (self.project / ".git").mkdir(parents=True)
        self.transcripts = self.root / "transcripts"
        self.transcripts.mkdir()
        self.routes = json.loads((ROUTER / "routes.json").read_text())
        self.routes_path = self.root / "routes.json"
        (self.root / "routes.local.json").write_text("{}", encoding="utf-8")
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("ROUTER_") and key not in ("ROUTES_JSON", "XDG_STATE_HOME")}
        self.environment.update({
            "HOME": str(self.root / "home"),
            "CLAUDE_HOME": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "ROUTER_HOME": str(self.root / "config"),
            "ROUTER_STATE": str(self.state),
            "ROUTER_OFF_FILE": str(self.state / "OFF"),
            "ROUTES_JSON": str(self.routes_path),
            "XDG_CONFIG_HOME": str(self.root / "xdg-config"),
            "ROUTER_LOCAL": str(self.root / "routes.local.json"),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        isolated = patch.dict(os.environ, self.environment, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.agents = 0

    def run_script(self, script, event, cwd=None, args=()):
        self.routes_path.write_text(json.dumps(self.routes))
        return subprocess.run([sys.executable, "-B", str(script), *args], input=json.dumps(event), text=True,
                              capture_output=True, timeout=20, cwd=cwd or self.root, env=self.environment)

    def spawn(self, prompt, description="Build the helper", session=SESSION, cwd=None, agent="builder"):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(cwd or self.project),
                 "session_id": session,
                 "tool_input": {"subagent_type": agent, "prompt": prompt, "description": description}}
        result = self.run_script(SPAWN_GUARD, event)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def stop(self, agent_type, prompt, message, session=SESSION, cwd=None, active=None, blocks=False):
        """A SubagentStop whose agent transcript starts with prompt (a string, or text blocks)."""
        self.agents += 1
        transcript = self.transcripts / f"agent-{self.agents}.jsonl"
        content = [{"type": "text", "text": prompt}] if blocks else prompt
        lines = [{"type": "user", "message": {"role": "user", "content": content}},
                 {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": message}]}}]
        transcript.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        event = {"hook_event_name": "SubagentStop", "session_id": session, "cwd": str(cwd or self.project),
                 "transcript_path": str(self.root / "main.jsonl"), "agent_id": f"agent{self.agents}",
                 "agent_type": agent_type, "agent_transcript_path": str(transcript),
                 "last_assistant_message": message}
        if active is not None:
            event["stop_hook_active"] = active
        result = self.run_script(CONTEXT_GUARD, event)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def returned(self, task, description="Build the helper", session=SESSION):
        """A builder lane that was spawned and has returned."""
        self.spawn(brief(task), description=description, session=session)
        self.stop("builder-std", brief(task), "CHANGED src/text.py", session=session)

    def rows(self, sql, *args):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql, args).fetchall()

    def lanes(self):
        return [dict(zip(ledger.LANE_FIELDS, row)) for row in
                self.rows(f"SELECT {', '.join(ledger.LANE_FIELDS)} FROM lanes ORDER BY started")]

    def verdicts(self):
        fields = ("key", "project", "session", "ts", "verdict", "findings", "round")
        return [dict(zip(fields, row)) for row in self.rows(f"SELECT {', '.join(fields)} FROM verdicts ORDER BY ts")]

    def set_auto(self, mode):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "auto").write_text(mode + "\n")


class SubagentStopLinkTests(LaneCase):
    def test_locked_judge_spools_one_complete_record(self):
        self.returned("locked judgment")
        returned_at = self.lanes()[0]["ended"]
        blocker = subprocess.Popen([sys.executable, "-c",
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],isolation_level=None); "
            "c.execute('BEGIN IMMEDIATE'); print('ready',flush=True); sys.stdin.readline(); c.execute('ROLLBACK')",
            str(self.db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.environment)
        try:
            self.assertEqual(blocker.stdout.readline().strip(), "ready")
            self.assertEqual(self.stop("judge-std", brief("locked judgment"),
                                       "SEND_BACK\n1. a\n2. b").stdout, "")
            spool = (self.state / "ledger.spool").read_text()
            self.assertEqual(json.loads(spool)["op"], "judge")
            self.assertEqual(len(spool.splitlines()), 1)
        finally:
            blocker.communicate("\n", timeout=10)
        self.assertEqual(blocker.returncode, 0)
        self.assertEqual(ledger.latest_verdict(self.lanes()[0]["key"], path=self.db)["verdict"], "SEND_BACK")
        self.assertEqual([(row["status"], row["ended"]) for row in self.lanes()],
                         [("judged", returned_at)])
        self.assertEqual([(v["verdict"], v["findings"], v["round"]) for v in self.verdicts()],
                         [("SEND_BACK", 2, 1)])
        self.assertEqual(list(self.state.glob("ledger.spool*")), [])

    def test_builder_return_marks_lane_returned(self):
        self.spawn(brief("add a text helper"))
        self.assertEqual([lane["status"] for lane in self.lanes()], ["running"])
        result = self.stop("builder-std", brief("add a text helper"), "CHANGED src/text.py")
        self.assertEqual(result.stdout, "")
        lane = self.lanes()[0]
        self.assertEqual(lane["status"], "returned")
        self.assertIsNotNone(lane["ended"])
        # A transcript whose first message is a list of text blocks links the same way.
        self.spawn(brief("add a second helper"))
        self.stop("builder-std", brief("add a second helper"), "CHANGED src/text.py", blocks=True)
        self.assertEqual([lane["status"] for lane in self.lanes()], ["returned", "returned"])
        self.assertEqual(self.verdicts(), [], "a builder return records no verdict")

    def test_judge_verdict_findings_round_and_judged_lane(self):
        self.returned("add a text helper")
        self.stop("judge-std", brief("add a text helper"), "PASS: the bar ran clean\n1. a nit\n  2) another nit\nend")
        lane = self.lanes()[0]
        self.assertEqual(lane["status"], "judged")
        [verdict] = self.verdicts()
        self.assertEqual((verdict["verdict"], verdict["findings"], verdict["round"]), ("PASS", 2, 1))
        self.assertEqual((verdict["key"], verdict["project"]), (lane["key"], lane["project"]))
        self.assertEqual(verdict["session"], common.session_hash(SESSION))

    def test_send_back_then_second_round(self):
        self.returned("add a text helper")
        self.stop("judge-std", brief("add a text helper"), "SEND_BACK\n1. the bar fails")
        self.returned("add a text helper")
        self.stop("judge-std", brief("add a text helper"), "NOT DONE\nthe tests are missing")
        self.assertEqual([(row["verdict"], row["findings"], row["round"]) for row in self.verdicts()],
                         [("SEND_BACK", 1, 1), ("NOT_DONE", 0, 2)])
        self.assertEqual([lane["status"] for lane in self.lanes()], ["judged", "judged"])

    def test_unparsed_first_line_stores_unknown(self):
        self.returned("add a text helper")
        self.stop("judge-std", brief("add a text helper"), "Blocked: PASS was not reached\n1. no access")
        self.assertEqual([(row["verdict"], row["findings"]) for row in self.verdicts()], [("UNKNOWN", 1)])
        self.assertEqual(self.lanes()[0]["status"], "judged")

    def test_worktree_stop_uses_the_lane_project(self):
        # A linked worktree whose gitdir has no commondir: hashing the stop's cwd gives another project.
        worktree = self.root / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {self.project}/.git/worktrees/w\n")
        self.assertNotEqual(common.project_key(str(worktree)), common.project_key(str(self.project)))
        self.spawn(brief("add a text helper"), cwd=self.project)
        self.stop("builder-std", brief("add a text helper"), "CHANGED src/text.py", cwd=worktree)
        self.stop("judge-std", brief("add a text helper"), "PASS", cwd=worktree)
        [lane] = self.lanes()
        [verdict] = self.verdicts()
        self.assertEqual(lane["status"], "judged")
        self.assertEqual(lane["project"], common.project_key(str(self.project)))
        self.assertEqual((verdict["key"], verdict["project"]), (lane["key"], lane["project"]))

    def test_blocking_return_links_nothing(self):
        self.spawn(brief("add a text helper"))
        blocked = self.stop("builder-std", brief("add a text helper"), "x" * 2000)
        self.assertEqual(json.loads(blocked.stdout)["decision"], "block")
        self.assertEqual(self.lanes()[0]["status"], "running")
        blocked = self.stop("judge-std", brief("add a text helper"), "PASS\n" + "x" * 2000)
        self.assertEqual(json.loads(blocked.stdout)["decision"], "block")
        self.assertEqual((self.lanes()[0]["status"], self.verdicts()), ("running", []))
        # The second pass is not blocked again, so it links.
        self.stop("judge-std", brief("add a text helper"), "PASS\n" + "x" * 2000, active=True)
        self.assertEqual(self.lanes()[0]["status"], "judged")
        self.assertEqual([row["verdict"] for row in self.verdicts()], ["PASS"])

    def test_no_lane_records_nothing(self):
        self.stop("judge-std", brief("never spawned"), "PASS")
        self.assertFalse(self.db.exists(), "a stop with no lane must not create the ledger")
        self.spawn(brief("add a text helper"))
        self.stop("judge-std", brief("another brief"), "PASS")
        self.stop("judge-std", brief("add a text helper"), "PASS", session="other-session")
        self.assertEqual((self.lanes()[0]["status"], self.verdicts()), ("running", []))

    def test_brief_and_judge_text_stay_out_of_the_ledger(self):
        task = "add a helper SENTINEL_BRIEF_7Q2"
        self.returned(task)
        self.stop("judge-std", brief(task), "SEND_BACK SENTINEL_JUDGE_9X4\n1. SENTINEL_JUDGE_9X4 is wrong")
        self.assertEqual([row["verdict"] for row in self.verdicts()], ["SEND_BACK"])
        assert_absent_from_ledger(self, self.db, "SENTINEL_BRIEF_7Q2", "SENTINEL_JUDGE_9X4")


def template_hooks(relative):
    """event -> the hook script names registered for it in a shipped template."""
    config = json.loads((ROOT / relative).read_text())
    return {event: [(block.get("matcher"), Path(hook["command"].split()[-1].strip('"')).name)
                    for block in blocks for hook in block["hooks"]]
            for event, blocks in config["hooks"].items()}


class JudgeReminderTests(LaneCase):
    """Main-session Stop: one block per set of returned lanes with no judge.

    Codex registers no Stop hook: Codex lanes stay running (no SubagentStop is registered there),
    so there is nothing to remind. That limit is checked in test_templates below.
    """
    REASON = "returned without a judge: {}; spawn judge with the same TASK and FILES"

    def remind(self, session=SESSION, active=None):
        event = {"hook_event_name": "Stop", "session_id": session, "cwd": str(self.project),
                 "transcript_path": str(self.root / "main.jsonl")}
        if active is not None:
            event["stop_hook_active"] = active
        result = self.run_script(JUDGE_REMINDER, event)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_blocks_once_per_set_and_again_for_a_new_set(self):
        self.returned("first lane", description="Lane alpha")
        self.returned("second lane", description="Lane beta")
        result = self.remind()
        self.assertEqual(json.loads(result.stdout),
                         {"decision": "block", "reason": self.REASON.format("Lane beta, Lane alpha")})
        self.assertEqual(self.remind().stdout, "", "the same set must not block twice")
        state = self.state / "judge-reminder" / f"{SESSION}.json"
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Lane", state.read_text(), "the reminder state holds hashes only")
        self.returned("third lane", description="Lane gamma")
        self.assertEqual(json.loads(self.remind().stdout)["reason"],
                         self.REASON.format("Lane gamma, Lane beta, Lane alpha"))
        self.assertEqual(self.remind().stdout, "")

    def test_null_label_uses_role_and_tier(self):
        self.routes["router"]["lane_labels"] = False
        self.returned("first lane", description="Lane alpha")
        self.assertEqual(json.loads(self.remind().stdout)["reason"], self.REASON.format("builder-std"))

    def test_judged_running_and_other_sessions_pass(self):
        self.spawn(brief("still running"))
        self.returned("judged lane")
        self.stop("judge-std", brief("judged lane"), "Blocked: no access")  # UNKNOWN still counts as judged
        self.returned("elsewhere", session="other-session")
        self.assertEqual(self.remind().stdout, "")
        self.assertEqual(self.remind(session="no-lanes-session").stdout, "")

    def test_stop_hook_active_auto_off_and_kill_switch_pass(self):
        self.returned("first lane")
        self.assertEqual(self.remind(active=True).stdout, "")
        self.set_auto("off")
        self.assertEqual(self.remind().stdout, "")
        self.set_auto("enforce")
        (self.state / "OFF").touch()
        self.assertEqual(self.remind().stdout, "")
        (self.state / "OFF").unlink()
        self.assertEqual(json.loads(self.remind().stdout)["decision"], "block", "nothing above used up the set")

    def test_shadow_logs_and_off_is_silent(self):
        self.returned("first lane")
        self.routes["router"]["modes"]["judge_reminder"] = "off"
        self.assertEqual(self.remind().stdout, "")
        self.routes["router"]["modes"]["judge_reminder"] = "shadow"
        self.assertEqual(self.remind().stdout, "")
        records = [json.loads(line) for line in (self.state / "context.jsonl").read_text().splitlines()]
        reminders = [record for record in records if record.get("event") == "judge_reminder"]
        self.assertEqual([(record["action"], record["lanes"]) for record in reminders], [("shadow", 1)])

    def test_unreadable_ledger_allows(self):
        self.state.mkdir(parents=True)
        self.db.write_bytes(b"not a sqlite file" * 100)
        self.assertEqual(self.remind().stdout, "")

    def test_templates(self):
        claude = template_hooks("examples/settings.example.json")
        self.assertIn((".*", "judge_reminder.py"), claude.get("Stop", []))
        codex = template_hooks("codex/hooks.json")
        self.assertNotIn("judge_reminder.py", [name for blocks in codex.values() for _, name in blocks])


class LedgerLaneCase(LaneCase):
    """Lanes written straight through ledger.py, with chosen start times."""

    def add_lane(self, label, started, status="running", verdict=None, session=SESSION, task=None,
                 project=None, tier="std"):
        brief_id = common.brief_hash(brief(task or label or f"lane at {started}"))
        project = project or common.project_key(str(self.project))
        key = common.claude_key(project, brief_id)
        session_id = common.session_hash(session)
        decision = ledger.record_attempt_and_lane(key, lambda prior: {"decision": "allow", "tier": tier},
                                                  role="builder", project=project, session=session_id,
                                                  brief=brief_id, label=label, now=started, path=self.db)
        self.assertIsNotNone(decision)
        if status != "running":
            self.assertIsNotNone(ledger.mark_lane(session_id, brief_id, status, now=started + 60, path=self.db))
        if verdict:
            self.assertTrue(ledger.record_verdict(key, verdict, project=project, session=session_id,
                                                  now=started + 120, path=self.db))
        return key


class LanesReportTests(LedgerLaneCase):
    def test_missing_ledger_is_empty_and_not_created(self):
        self.assertEqual(ledger.lanes_report(path=self.db), [])
        self.assertEqual(ledger.lanes_report(session="x", path=self.db, strict=True), [])
        self.assertFalse(self.db.exists())

    def test_round_verdict_order_and_filters(self):
        now = time.time()
        self.add_lane("first try", now - 900, status="judged", verdict="SEND_BACK", task="same brief")
        self.add_lane("second try", now - 600, task="same brief")
        self.add_lane("other session", now - 300, session="other-session")
        self.add_lane("other project", now - 100, project="0123456789ab")
        report = ledger.lanes_report(path=self.db)
        self.assertEqual([row["label"] for row in report], ["other project", "other session", "second try", "first try"])
        self.assertEqual(set(report[0]), set(ledger.LANE_FIELDS) | {"round", "verdict"})
        self.assertEqual([(row["round"], row["verdict"]) for row in report[2:]], [(2, None), (1, "SEND_BACK")])
        mine = common.session_hash(SESSION)
        self.assertEqual([row["label"] for row in ledger.lanes_report(session=mine, path=self.db)],
                         ["other project", "second try", "first try"])
        project = common.project_key(str(self.project))
        self.assertEqual([row["label"] for row in ledger.lanes_report(project=project, since=now - 700, path=self.db)],
                         ["other session", "second try"])
        self.assertEqual(len(ledger.lanes_report(limit=2, path=self.db)), 2)


class SessionNoteTests(LedgerLaneCase):
    ACTIONS = {"returned": "Spawn judge with the same TASK and FILES before closing.",
               "running": "Running lanes finish on their own; see `router lanes`.",
               "judged": "All lanes judged."}

    def note(self, source="compact", session=SESSION):
        event = {"hook_event_name": "SessionStart", "session_id": session, "source": source,
                 "cwd": str(self.project), "transcript_path": str(self.root / "main.jsonl")}
        result = self.run_script(SESSION_NOTE, event)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result

    def context(self, result):
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "SessionStart")
        return output["additionalContext"]

    def test_size_cap_with_long_non_ascii_labels(self):
        now = time.time()
        labels = [f"{index} " + ("界面の検証と修正" * 8)[:58] for index in range(6)]
        for index, label in enumerate(labels):
            self.add_lane(label, now - 600 + index * 60, status="returned" if index == 0 else "running")
        text = self.context(self.note())
        self.assertLessEqual(len(text.encode("utf-8")), 800, text)
        lines = text.splitlines()
        self.assertIn("auto mode: nudge", lines[0])
        self.assertEqual(lines[-1], self.ACTIONS["returned"], "the action counts every lane, shown or not")
        shown = lines[1:-1]
        self.assertTrue(1 <= len(shown) < 6, shown)
        self.assertEqual([line.split()[1] for line in shown], [str(index) for index in range(5, 5 - len(shown), -1)],
                         "the oldest lanes are dropped first")

    def test_other_sources_print_nothing(self):
        self.add_lane("lane", time.time() - 60, status="returned")
        for source in ("startup", "resume", "clear", "fork", None):
            with self.subTest(source=source):
                self.assertEqual(self.note(source=source).stdout, "")
        self.assertNotEqual(self.note().stdout, "")

    def test_no_lanes_kill_switch_and_broken_ledger_print_nothing(self):
        self.assertEqual(self.note().stdout, "")
        self.assertFalse(self.db.exists(), "the note must not create the ledger")
        self.add_lane("lane", time.time() - 60, session="other-session")
        self.assertEqual(self.note().stdout, "")
        self.add_lane("mine", time.time() - 30)
        (self.state / "OFF").touch()
        self.assertEqual(self.note().stdout, "")
        (self.state / "OFF").unlink()
        self.db.write_bytes(b"not a sqlite file" * 100)
        self.assertEqual(self.note().stdout, "")

    def test_line_order_and_action_lines(self):
        now = time.time()
        self.set_auto("enforce")
        self.add_lane("lane one", now - 300)
        self.add_lane(None, now - 200, status="returned")
        self.add_lane("lane three", now - 100, status="judged", verdict="PASS")
        self.assertEqual(self.context(self.note()).splitlines(), [
            "Router lanes after compaction (auto mode: enforce), newest first:",
            "builder-std lane three r1/3 judged PASS",
            "builder-std r1/3 returned",
            "builder-std lane one r1/3 running",
            self.ACTIONS["returned"]])
        self.add_lane("lane four", now - 300, session="running-session")
        self.add_lane("lane five", now - 200, session="running-session", status="judged")
        self.assertEqual(self.context(self.note(session="running-session")).splitlines()[1:],
                         ["builder-std lane five r1/3 judged", "builder-std lane four r1/3 running",
                          self.ACTIONS["running"]])
        self.add_lane("lane six", now - 100, session="judged-session", status="judged", verdict="FAIL")
        self.assertEqual(self.context(self.note(session="judged-session")).splitlines()[1:],
                         ["builder-std lane six r1/3 judged FAIL", self.ACTIONS["judged"]])

    def test_templates(self):
        self.assertIn(("compact", "session_note.py"), template_hooks("examples/settings.example.json").get("SessionStart", []))
        self.assertIn((".*", "session_note.py"), template_hooks("codex/hooks.json").get("SessionStart", []))


class LanesCommandTests(LedgerLaneCase):
    HEADER = ["age", "session", "role-tier", "round", "status", "verdict", "label"]

    def lanes_cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "bin" / "router"), "lanes", *args], text=True,
                                capture_output=True, timeout=20, cwd=self.project, env=self.environment,
                                stdin=subprocess.DEVNULL)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return result.stdout.splitlines()

    def labels(self, lines):
        self.assertEqual(lines[0].split(), self.HEADER)
        return [line.split()[-1] for line in lines[1:]]

    def test_newest_first(self):
        now = time.time()
        self.add_lane("old", now - 3 * HOUR, status="judged", verdict="PASS")
        self.add_lane("new", now - 1 * HOUR)
        self.add_lane("mid", now - 2 * HOUR, status="returned")
        lines = self.lanes_cli()
        self.assertEqual(self.labels(lines), ["new", "mid", "old"])
        self.assertEqual(lines[1].split(), ["1h00m", common.session_hash(SESSION), "builder-std", "1/3", "running", "-", "new"])
        self.assertEqual(lines[3].split()[2:], ["builder-std", "1/3", "judged", "PASS", "old"])

    def test_project_filter_all_and_day_window(self):
        now = time.time()
        self.add_lane("stale", now - 25 * HOUR)
        self.add_lane("here", now - 2 * HOUR)
        self.add_lane("there", now - 1 * HOUR, project="0123456789ab")
        self.assertEqual(self.labels(self.lanes_cli()), ["here"])
        self.assertEqual(self.labels(self.lanes_cli("--all")), ["there", "here"])

    def test_session_filter_and_limit(self):
        now = time.time()
        for index in range(22):
            self.add_lane(f"mine{index}", now - 600 + index)
        self.add_lane("theirs", now - 10, session="other-session")
        everything = self.labels(self.lanes_cli())
        self.assertEqual(everything[:2], ["theirs", "mine21"])
        self.assertEqual(len(everything), 20)
        self.assertEqual(self.labels(self.lanes_cli("--session", common.session_hash("other-session"))), ["theirs"])
        mine = self.labels(self.lanes_cli("--all", "--session", common.session_hash(SESSION)))
        self.assertEqual((mine[0], len(mine)), ("mine21", 20))

    def test_session_filter_ignores_project_and_day_window(self):
        now = time.time()
        self.add_lane("oldmine", now - 3 * 86400)
        self.add_lane("otherproject", now - 60, project="0123456789ab")
        self.add_lane("other_session", now - 30, session="other-session")
        self.assertEqual(self.labels(self.lanes_cli()), ["other_session"])
        self.assertEqual(self.labels(self.lanes_cli("--session", common.session_hash(SESSION))),
                         ["otherproject", "oldmine"])
        self.assertEqual(self.labels(self.lanes_cli("--all", "--session", common.session_hash(SESSION))),
                         ["otherproject", "oldmine"])

    def test_caller_overlay_cannot_change_hook_rule(self):
        self.routes_path.write_text(json.dumps(self.routes))
        decoy = self.root / "decoy.json"
        decoy.write_text(json.dumps({"router": {"modes": {"brief": "off"}}}))
        bad_brief = "TASK make helper\nRETURN five lines"
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(self.project),
                 "tool_input": {"subagent_type": "builder", "prompt": bad_brief}}
        with patch.dict(os.environ, ROUTER_LOCAL=str(decoy), XDG_CONFIG_HOME=str(self.root / "decoy-xdg")):
            result = subprocess.run([sys.executable, "-B", str(SPAWN_GUARD)], input=json.dumps(event),
                                    text=True, capture_output=True, timeout=20, cwd=self.project,
                                    env=dict(os.environ, **self.environment))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("brief", result.stderr)

    def test_no_lanes_without_creating_the_ledger(self):
        self.assertEqual(self.lanes_cli(), ["no lanes"])
        self.assertEqual(self.lanes_cli("--all"), ["no lanes"])
        self.assertFalse(self.db.exists(), "router lanes must not create the ledger")
        self.add_lane("there", time.time() - 60, project="0123456789ab")
        self.assertEqual(self.lanes_cli(), ["no lanes"])


class BadTranscriptLineTests(LaneCase):
    """A transcript line that is not JSON no longer stops a lane from linking."""

    def stop_with_lines(self, agent_type, prompt, message, before=()):
        self.agents += 1
        transcript = self.transcripts / f"agent-{self.agents}.jsonl"
        user = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}})
        transcript.write_text("".join(line + "\n" for line in (*before, user)), encoding="utf-8")
        event = {"hook_event_name": "SubagentStop", "session_id": SESSION, "cwd": str(self.project),
                 "transcript_path": str(self.root / "main.jsonl"), "agent_id": f"agent{self.agents}",
                 "agent_type": agent_type, "agent_transcript_path": str(transcript),
                 "last_assistant_message": message}
        result = self.run_script(CONTEXT_GUARD, event)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_garbage_and_truncated_lines_before_the_user_message_still_link(self):
        bad = ("this is not json at all", '{"type": "user", "message": {"role": "us', "[1, 2]", "42")
        self.spawn(brief("add a text helper"))
        self.stop_with_lines("builder-std", brief("add a text helper"), "CHANGED src/text.py", before=bad)
        self.assertEqual([lane["status"] for lane in self.lanes()], ["returned"])
        self.stop_with_lines("judge-std", brief("add a text helper"), "PASS: clean", before=bad)
        self.assertEqual(self.lanes()[0]["status"], "judged")
        self.assertEqual([row["verdict"] for row in self.verdicts()], ["PASS"])

    def test_only_bad_lines_link_nothing_and_log_no_transcript_text(self):
        sentinel = "SENTINEL_BAD_LINE_5K1"
        self.spawn(brief("add a text helper"))
        self.stop_with_lines("builder-std", brief("add a text helper"), "CHANGED src/text.py")
        self.assertEqual(self.lanes()[0]["status"], "returned")
        self.spawn(brief("add a second helper"))
        self.agents += 1
        transcript = self.transcripts / "only-bad.jsonl"
        transcript.write_text(f"{sentinel} not json\n{{\"cut\": \"{sentinel}\n", encoding="utf-8")
        event = {"hook_event_name": "SubagentStop", "session_id": SESSION, "cwd": str(self.project),
                 "transcript_path": str(self.root / "main.jsonl"), "agent_id": "agentbad",
                 "agent_type": "builder-std", "agent_transcript_path": str(transcript),
                 "last_assistant_message": "CHANGED src/text.py"}
        self.assertEqual(self.run_script(CONTEXT_GUARD, event).returncode, 0)
        self.assertEqual([lane["status"] for lane in self.lanes()], ["returned", "running"])
        for name in ("errors.jsonl", "context.jsonl"):
            path = self.state / name
            if path.exists():
                self.assertNotIn(sentinel, path.read_text(encoding="utf-8"), name)

    def test_user_message_cut_by_the_head_limit_links_nothing(self):
        import context_guard
        self.spawn(brief("add a text helper"))
        long_prompt = brief("add a text helper") + "x" * (context_guard.TRANSCRIPT_HEAD_BYTES + 10)
        self.stop_with_lines("builder-std", long_prompt, "CHANGED src/text.py", before=("garbage",))
        self.assertEqual(self.lanes()[0]["status"], "running")


class ReminderCapTests(JudgeReminderTests):
    """The reminder text names at most 5 lanes and stays within 600 bytes."""
    test_blocks_once_per_set_and_again_for_a_new_set = None
    test_null_label_uses_role_and_tier = None
    test_judged_running_and_other_sessions_pass = None
    test_stop_hook_active_auto_off_and_kill_switch_pass = None
    test_shadow_logs_and_off_is_silent = None
    test_unreadable_ledger_allows = None
    test_templates = None

    def test_seven_lanes_print_five_labels_and_a_count(self):
        names = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
        for name in names:
            self.returned(f"lane {name}", description=f"Lane {name}")
        self.assertEqual(json.loads(self.remind().stdout),
                         {"decision": "block", "reason": self.REASON.format(
                             "Lane golf, Lane foxtrot, Lane echo, Lane delta, Lane charlie, +2 more")})

    def test_long_non_ascii_labels_stay_within_600_bytes(self):
        for index in range(5):
            self.returned(f"wide lane {index}", description=f"{index}" + "\u8a9e" * 59)
        reason = json.loads(self.remind().stdout)["reason"]
        self.assertLessEqual(len(reason.encode("utf-8")), 600)
        self.assertRegex(reason, r"^returned without a judge: (.+, )?\+[1-5] more; spawn judge with the same TASK and FILES$")

    def test_two_lanes_print_the_old_text(self):
        self.returned("first lane", description="Lane alpha")
        self.returned("second lane", description="Lane beta")
        self.assertEqual(json.loads(self.remind().stdout)["reason"], self.REASON.format("Lane beta, Lane alpha"))

    def test_a_sixth_lane_makes_a_new_set_that_blocks_again(self):
        for index in range(5):
            self.returned(f"lane {index}", description=f"Lane {index}")
        self.assertEqual(json.loads(self.remind().stdout)["decision"], "block")
        self.assertEqual(self.remind().stdout, "")
        self.returned("lane 5", description="Lane 5")
        self.assertEqual(json.loads(self.remind().stdout)["reason"],
                         self.REASON.format("Lane 5, Lane 4, Lane 3, Lane 2, Lane 1, +1 more"))
        self.assertEqual(self.remind().stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
