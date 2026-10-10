#!/usr/bin/env python3
"""Local route overrides and config command."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common
import overlay


class OverlayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.local = self.tmp / "config/router/routes.local.json"
        self.env = patch.dict(os.environ, HOME=str(self.tmp), ROUTER_LOCAL=str(self.local),
                              ROUTER_STATE=str(self.tmp / "state"), ROUTER_OFF_FILE=str(self.tmp / "OFF"),
                              ROUTES_JSON=str(common.ROUTES_PATH))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.base = common.load_routes(overlay=False)

    def write(self, value):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_text(json.dumps(value), encoding="utf-8")

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), "config", *args],
                              env=os.environ.copy(), input="", text=True, capture_output=True, timeout=10)

    def hook(self):
        payload = {"tool_name":"Agent", "tool_input":{"subagent_type":"sweeper",
                   "prompt":"TASK: Find files\nRETURN: paths"}, "session_id":"overlay-test"}
        return subprocess.run([sys.executable, "-B", str(ROOT / "hooks/router/spawn_guard.py")],
                              env=os.environ.copy(), input=json.dumps(payload), text=True,
                              capture_output=True, timeout=10)

    def test_merge_vectors(self):
        vectors = [({"a":"b"},{"a":"c"},{"a":"c"}),
                   ({"a":"b"},{"b":"c"},{"a":"b","b":"c"}),
                   ({"a":"b"},{"a":None},{}),
                   ({"a":{"b":"c"}},{"a":{"b":"d","c":None}},{"a":{"b":"d"}}),
                   ({"a":["b"]},{"a":["c"]},{"a":["c"]}),
                   ({"a":"c"},{"a":["b"]},{"a":["b"]}),
                   ({"a":{"b":"c"}},{"a":None},{}),
                   ({"a":[{"b":"c"}]},{"a":[1]},{"a":[1]}),
                   ({"a":"b"},{"a":{"c":"d"}},{"a":{"c":"d"}}),
                   ({"a":{"b":"c"}},{"a":{"b":None}}, {"a":{}}),
                   ({"a":"b","b":"c"},{"a":None},{"b":"c"}),
                   ({"a":["b"]},{"a":"c"},{"a":"c"}),
                   (["a","b"],["c","d"],["c","d"]),
                   ({"a":"b"},["c"],["c"]),
                   ({"a":"foo"},None,None),
                   ({"a":"foo"},"bar","bar"),
                   ({"e":None},{"a":1},{"e":None,"a":1}),
                   ([1,2],{"a":"b","c":None},{"a":"b"}),
                   ({"a":{"bb":{"ccc":"ddd"}}},{"a":{"bb":{"ccc":None}}},{"a":{"bb":{}}})]
        for base, patch_value, expected in vectors:
            original = copy.deepcopy(base)
            self.assertEqual(overlay.merge(base, patch_value), expected)
            self.assertEqual(base, original)

    def test_load_overlay_and_base(self):
        before = common.ROUTES_PATH.read_bytes()
        self.write({"context":{"chain":{"first":3},"prompt_roles":{"sweeper":["locate"]}},
                    "router":{"modes":{"risk":None}}})
        merged = common.load_routes()
        self.assertEqual(merged["context"]["chain"]["first"], 3)
        self.assertEqual(merged["context"]["prompt_roles"]["sweeper"], ["locate"])
        self.assertNotIn("risk", merged["router"]["modes"])
        self.assertEqual(common.load_routes(common.ROUTES_PATH), self.base)
        self.assertEqual(common.ROUTES_PATH.read_bytes(), before)

    def test_validation(self):
        self.assertEqual(self.base["router"]["modes"]["judge_reminder"], "enforce")
        cases = [({"pressure": None}, "context.pressure must be an object"),
                 ({"pressure": []}, "context.pressure must be an object"),
                 ({"pressure": {"x": {}}}, "context.pressure: unknown level 'x' (want remind, urgent)"),
                 ({"pressure": {"urgent": []}}, "context.pressure.urgent must be an object"),
                 ({"pressure": {"urgent": {"x": 1}}}, "context.pressure.urgent: unknown key 'x' (want chain_first, large_read_bytes)"),
                 ({"pressure": {"urgent": {"chain_first": 6}}}, "context.pressure.urgent.chain_first: want an integer from 1 to 5, got 6"),
                 ({"pressure": {"remind": {"large_read_bytes": True}}}, "context.pressure.remind.large_read_bytes: want an integer from 1 to 24000, got True"),
                 ({"prompt_roles": {"x": ["ok"]}}, "context.prompt_roles: unknown role 'x'"),
                 ({"prompt_roles": None}, "context.prompt_roles must be an object"),
                 ({"prompt_roles": {"builder": 1}}, "context.prompt_roles.builder: want a list of strings or an object of keyword to weight"),
                 ({"prompt_roles": {"builder": {"fix": 0}}}, "context.prompt_roles.builder.fix: weight must be an integer from 1 to 5, got 0"),
                 ({"prompt_roles": {"builder": {"": 1}}}, "context.prompt_roles.builder: bad keyword ''"),
                 ({"hint_min_score": 11}, "context.hint_min_score: want an integer from 1 to 10, got 11"),
                 ({"hint_every": True}, "context.hint_every: want an integer from 0 to 100, got True")]
        for changes, error in cases:
            routes = copy.deepcopy(self.base)
            routes["context"].update(changes)
            self.assertIn(error, common.validate_routes(routes))
        good = copy.deepcopy(self.base)
        good["context"]["pressure"] = {"urgent":{"chain_first": 2, "large_read_bytes": 12000}}
        good["context"]["prompt_roles"] = {"builder": {"fix": 3}}
        good["context"].update(hint_min_score=2, hint_every=0)
        self.assertEqual(common.validate_routes(good), [])

    def test_bad_overlay_falls_back_and_warns_hourly(self):
        self.write({"context":{"caps":{"exec": -1}}})
        routes, problem = common.load_routes_checked()
        self.assertEqual(routes, self.base)
        self.assertEqual(problem["file"], str(self.local))
        self.assertIsNotNone(re.fullmatch(re.escape(f"routes overlay {self.local} is invalid: "
                                                    "context.caps.exec: cap must be a positive int, got -1"),
                                          problem["message"]), problem["message"])
        for _ in range(2):
            self.assertEqual(common.load_routes(), self.base)
        lines = (self.tmp / "state/errors.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["file"], str(self.local))
        marker = self.tmp / "state/overlay-warned"
        old = time.time() - 7200
        os.utime(marker, (old, old))
        common.load_routes()
        self.assertEqual(len((self.tmp / "state/errors.jsonl").read_text().splitlines()), 2)

    def baseline(self):
        with patch.dict(os.environ, ROUTER_LOCAL="off", ROUTER_STATE=str(self.tmp / "state-none")):
            result = self.hook()
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "state-none/errors.jsonl").exists())
        return result

    def assert_falls_back(self, name, detail_pattern):
        """Bad overlay at self.local: base table, same hook decision as no overlay, one report."""
        base = self.baseline()
        with patch.dict(os.environ, ROUTER_STATE=str(self.tmp / f"state-{name}")):
            routes, problem = common.load_routes_checked()
            self.assertEqual(routes, self.base)
            self.assertEqual(problem["file"], str(self.local))
            self.assertIsNotNone(re.fullmatch(
                re.escape(f"routes overlay {self.local} is invalid: ") + detail_pattern, problem["message"]),
                problem["message"])
            first, second = self.hook(), self.hook()
            for run in (first, second):
                self.assertEqual((run.returncode, run.stdout, run.stderr),
                                 (0, base.stdout, base.stderr))
            records = (self.tmp / f"state-{name}/errors.jsonl").read_text().splitlines()
            self.assertEqual(len(records), 1)
            record = json.loads(records[0])
            self.assertEqual(record["file"], str(self.local))
            self.assertEqual(record["script"], "overlay")

    def test_file_shapes_and_hook_fallback(self):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        bad = [("directory", None, "not a regular file"),
               ("large", b" " * 65537, "larger than 65536 bytes"),
               ("json", b"{broken", "not valid JSON: .+"),
               ("array", b"[]", "must be a JSON object"),
               ("version", b'{"version":2}', "version: cannot change"),
               ("merged", b'{"context":{"caps":{"exec":-1}}}',
                re.escape("context.caps.exec: cap must be a positive int, got -1"))]
        for name, data, detail in bad:
            with self.subTest(name=name):
                if self.local.exists():
                    if self.local.is_dir():
                        self.local.rmdir()
                    else:
                        self.local.unlink()
                if data is None:
                    self.local.mkdir()
                else:
                    self.local.write_bytes(data)
                self.assert_falls_back(name, detail)

    def test_symlink_shapes(self):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        target = self.tmp / "target.json"
        target.write_text('{"context":{"chain":{"first":3}}}', encoding="utf-8")
        self.local.symlink_to(target)
        self.assertEqual(common.load_routes()["context"]["chain"]["first"], 3)
        self.local.unlink()
        self.local.symlink_to(self.tmp / "nowhere.json")
        self.assert_falls_back("dangling", "not a regular file")
        self.local.unlink()
        self.local.symlink_to(self.tmp)
        self.assert_falls_back("symdir", "not a regular file")

    def test_deep_nesting_falls_back(self):
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_text('{"a":' * 10000 + "1" + "}" * 10000, encoding="utf-8")
        self.assertLess(self.local.stat().st_size, 65536)
        self.assert_falls_back("deep", "nested too deeply")
        result = self.cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIsNotNone(re.fullmatch(re.escape(f"overlay invalid: {self.local}: nested too deeply\n"), result.stderr), result.stderr)

    def test_overlay_too_deep_to_merge_falls_back(self):
        # 1100 levels parse, then the merge itself raises RecursionError.
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_text('{"a":' * 1100 + "1" + "}" * 1100, encoding="utf-8")
        self.assertIsInstance(overlay.read_patch(self.local), dict)
        self.assert_falls_back("merge-deep", "nested too deeply")
        record = json.loads((self.tmp / "state-merge-deep/errors.jsonl").read_text())
        self.assertEqual(set(record) - {"ts"}, {"script", "file", "error"})
        self.assertEqual(record["error"], f"routes overlay {self.local} is invalid: nested too deeply")
        result = self.cli("check")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"overlay invalid: {self.local}: nested too deeply\n"))

    def test_unsearchable_default_directory_is_not_absent(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores modes")
        os.environ.pop("ROUTER_LOCAL")
        default = self.tmp / ".config/router/routes.local.json"
        default.parent.mkdir(parents=True)
        default.parent.chmod(0o600)
        self.addCleanup(default.parent.chmod, 0o700)
        self.assertIsNotNone(re.fullmatch(re.escape(f"{default}\ninvalid: unreadable: ") + r"[^\n]+\n",
                                          self.cli("path").stdout), self.cli("path").stdout)

    def test_unreadable_file_and_directory_fall_back(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores modes")
        self.write({"context": {"chain": {"first": 3}}})
        self.local.chmod(0)
        self.addCleanup(self.local.chmod, 0o600)
        self.assert_falls_back("file", "unreadable: .+")
        self.local.chmod(0o600)
        folder = self.local.parent
        folder.chmod(0o600)
        self.addCleanup(folder.chmod, 0o700)
        self.assert_falls_back("dir", "unreadable: .+")
        result = self.cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIsNotNone(re.fullmatch(re.escape(f"overlay invalid: {self.local}: ") + r"unreadable: [^\n]+\n", result.stderr), result.stderr)
        self.assertIsNotNone(re.fullmatch(re.escape(f"{self.local}\n") + r"invalid: unreadable: [^\n]+\n", self.cli("path").stdout))
        folder.chmod(0o700)

    def test_missing_explicit_path_reads_the_same(self):
        message = "overlay file not found"
        self.assertEqual(self.cli("path").stdout, f"{self.local}\ninvalid: {message}\n")
        result = self.cli("check")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"overlay invalid: {self.local}: {message}\n"))

    def test_rule_and_message_pins(self):
        routes = copy.deepcopy(self.base)
        routes["context"]["prompt_roles"] = {"builder": {"fix": True}}
        self.assertEqual(common.validate_routes(routes),
                         ["context.prompt_roles.builder.fix: weight must be an integer from 1 to 5, got True"])
        routes["context"]["prompt_roles"] = {"builder": {"x" * 40: 1}}
        self.assertEqual(common.validate_routes(routes), [])
        routes["context"]["prompt_roles"] = {"builder": {"x" * 41: 1}}
        self.assertEqual(common.validate_routes(routes), [f"context.prompt_roles.builder: bad keyword {'x' * 41!r}"])
        home = self.tmp / "claude"
        with self.assertRaises(ValueError) as caught:
            common.plan_defaults(home, "claude", {"created": "yes"}, "block")
        self.assertEqual(str(caught.exception), f"{home}/router/install-manifest.json: invalid defaults ownership")
        (home / "router").mkdir(parents=True)
        manifest = home / "router/install-manifest.json"
        manifest.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            common.defaults_manifest(home)
        self.assertEqual(str(caught.exception), f"invalid defaults manifest: {manifest}")

    def test_chain_first_range(self):
        for value, shown in ((float("nan"), "nan"), (True, "True"), (0, "0"), (100001, "100001"), (2.5, "2.5")):
            routes = copy.deepcopy(self.base)
            routes["context"]["chain"]["first"] = value
            self.assertIn(f"context.chain.first: want an integer from 1 to 100000, got {shown}", common.validate_routes(routes))
        routes = copy.deepcopy(self.base)
        routes["context"]["chain"]["first"] = 100000
        self.assertEqual(common.validate_routes(routes), [])
        routes["context"]["chain"] = []
        self.assertIn("context.chain must be an object", common.validate_routes(routes))

    def test_paths_and_config_messages(self):
        self.assertEqual(overlay.overlay_path({"HOME":str(self.tmp), "XDG_CONFIG_HOME":"relative"}),
                         self.tmp / ".config/router/routes.local.json")
        self.assertEqual(overlay.overlay_path({"XDG_CONFIG_HOME":str(self.tmp / "xdg")}),
                         self.tmp / "xdg/router/routes.local.json")
        self.assertIsNone(overlay.overlay_path({"ROUTER_LOCAL":"off"}))
        self.assertEqual(self.cli("check").returncode, 1)  # explicit missing file
        self.assertEqual(self.cli("get", "context.chain.first").stdout, "5\n")
        self.assertTrue(self.cli("get", "nothing").stderr.endswith("router config: no such key: nothing\n"))
        self.assertEqual(self.cli("set", "context.chain.first", "bad").stderr,
                         "router config: not valid JSON: bad\n")
        self.assertEqual(self.cli("set", "version", "2").stderr,
                         "router config: version: invalid key\n")
        self.assertEqual(self.cli("unset", "context.chain.first").stderr,
                         f"router config: context.chain.first is not set in {self.local}\n")
        with patch.dict(os.environ, ROUTER_LOCAL="off"):
            self.assertEqual(self.cli("set", "context.chain.first", "3").stderr,
                             "router config: overlay is off (ROUTER_LOCAL=off)\n")
            self.assertEqual(self.cli("unset", "context.chain.first").returncode, 1)
        self.write({"context":{"chain":{"first": 2}}})
        self.assertEqual(self.cli("check").stdout, f"overlay ok: {self.local}\n")
        self.assertEqual(self.cli("path").stdout, f"{self.local}\nok\n")
        self.assertIn("config", subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), "--help"],
                         env=os.environ.copy(), input="", text=True, capture_output=True, timeout=10).stdout)
        self.local.write_text("{broken", encoding="utf-8")
        before = self.local.read_bytes()
        for args in (("set", "context.chain.first", "3"), ("unset", "context.chain.first")):
            self.assertIsNotNone(re.fullmatch(re.escape(f"router config: {self.local}: ") + r"not valid JSON: [^\n]+\n",
                                              self.cli(*args).stderr))
        self.assertEqual(self.local.read_bytes(), before)
        got = self.cli("get", "context.chain.first")
        self.assertEqual(got.stdout, "5\n")
        self.assertIsNotNone(re.fullmatch(re.escape(f"overlay invalid: {self.local}: ") + r"not valid JSON: [^\n]+\n", got.stderr))
        self.write({"context":{"caps":{"exec": -1}}})
        before = self.local.read_bytes()
        shown = self.cli("path").stdout
        self.assertIsNotNone(re.fullmatch(re.escape(f"{self.local}\ninvalid: context.caps.exec: cap must be a positive int, got -1\n"),
                                          shown), shown)
        self.assertEqual(self.cli("set", "context.caps.exec", "1500").returncode, 1)
        self.assertEqual(self.cli("unset", "context.caps.exec").returncode, 1)
        self.assertEqual(self.local.read_bytes(), before)

    def test_default_path_off_and_valid_overlay_no_warning(self):
        with patch.dict(os.environ, ROUTER_LOCAL="off"):
            self.write({"context":{"chain":{"first": 2}}})
            self.assertEqual(common.load_routes(), self.base)
            self.assertEqual(self.cli("path").stdout, "off\nabsent\n")
        os.environ.pop("ROUTER_LOCAL", None)
        self.assertEqual(common.load_routes(), self.base)
        default = self.tmp / ".config/router/routes.local.json"
        default.parent.mkdir(parents=True)
        default.write_text('{"context":{"chain":{"first":3}}}', encoding="utf-8")
        self.assertEqual(common.load_routes()["context"]["chain"]["first"], 3)
        self.assertFalse((self.tmp / "state/errors.jsonl").exists())
        self.assertEqual(common.load_routes(common.ROUTES_PATH), self.base)
        self.assertEqual(self.cli("check").stdout, f"overlay ok: {default}\n")
        default.unlink()
        self.assertEqual(self.cli("check").stdout, "no overlay\n")

    def test_file_named_failures(self):
        bad = self.tmp / "broken-routes.json"
        bad.write_text("{broken", encoding="utf-8")
        with patch.dict(os.environ, ROUTES_JSON=str(bad)):
            result = self.hook()
        self.assertEqual(result.returncode, 0)
        record = json.loads((self.tmp / "state/errors.jsonl").read_text().splitlines()[-1])
        self.assertEqual(record["file"], str(bad))
        self.assertIn(str(bad), record["message"])
        with self.assertRaises(SystemExit):
            common.fail_open("sentinel", RuntimeError("PRIVATE_SENTINEL"))
        record = json.loads((self.tmp / "state/errors.jsonl").read_text().splitlines()[-1])
        self.assertEqual(set(record) - {"ts"}, {"script", "error"})
        with self.assertRaises(SystemExit):
            common.fail_open("file-hook", OSError(2, "missing", str(bad)))
        record = json.loads((self.tmp / "state/errors.jsonl").read_text().splitlines()[-1])
        self.assertEqual(record["file"], str(bad))
        self.assertIn(str(bad), record["message"])

    def test_defaults_marker_and_manifest_name_file(self):
        home = self.tmp / "claude"
        (home / "router").mkdir(parents=True)
        (home / "hooks/router").mkdir(parents=True)
        (home / "router/templates").mkdir(parents=True)
        (home / "hooks/router/routes.json").write_bytes(common.ROUTES_PATH.read_bytes())
        (home / "router/templates/defaults.claude.md").write_text("template", encoding="utf-8")
        manifest = home / "router/install-manifest.json"
        manifest.write_text(json.dumps({"defaults":{"host":"claude","created":False}}), encoding="utf-8")
        notes = home / "CLAUDE.md"
        notes.write_text("<!-- router:defaults:start -->\n<!-- router:defaults:start -->\n<!-- router:defaults:end -->\n", encoding="utf-8")
        with patch.dict(os.environ, CLAUDE_HOME=str(home), CODEX_HOME=str(self.tmp / "codex")):
            first = subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), "auto", "nudge"],
                                   env=os.environ.copy(), input="", text=True, capture_output=True, timeout=10)
            self.assertEqual(first.returncode, 1)
            self.assertIsNotNone(re.fullmatch(re.escape(f"router: {notes}: defaults markers must be one complete, ordered start/end pair\n"),
                                              first.stderr), first.stderr)
            manifest.write_text("{broken", encoding="utf-8")
            second = subprocess.run([sys.executable, "-B", str(ROOT / "bin/router"), "auto", "nudge"],
                                    env=os.environ.copy(), input="", text=True, capture_output=True, timeout=10)
            self.assertEqual(second.returncode, 1)
            with self.assertRaises(ValueError) as broken:
                json.loads("{broken")
            self.assertIsNotNone(re.fullmatch(re.escape(f"router: {manifest}: not valid JSON: {broken.exception}\n"),
                                              second.stderr), second.stderr)

    def test_config_round_trip_and_invalid_set(self):
        self.assertEqual(self.cli("path").stdout, f"{self.local}\ninvalid: overlay file not found\n")
        result = self.cli("set", "context.chain.first", "8")
        self.assertEqual((result.returncode, result.stdout), (0, "context.chain.first = 8\n"))
        self.assertEqual(self.local.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.local.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.cli("get", "context.chain.first").stdout, "8\n")
        before = self.local.read_bytes()
        bad = self.cli("set", "context.caps.exec", "-1")
        self.assertEqual(bad.returncode, 1)
        self.assertEqual(bad.stderr, f"router config: {self.local}: context.caps.exec: cap must be a positive int, got -1\n")
        self.assertEqual(self.local.read_bytes(), before)
        self.assertEqual(self.cli("unset", "context.chain.first").stdout, "unset context.chain.first\n")
        self.assertEqual(self.local.read_text(), "{}\n")
        self.assertEqual(self.cli("set", "router.modes.risk", "null").returncode, 0)
        self.assertEqual(self.cli("get", "router.modes.risk").returncode, 1)

    def test_config_threshold_is_used_by_context_hook(self):
        self.assertEqual(self.cli("set", "context.chain.first", "8").returncode, 0)
        small = self.tmp / "small.txt"
        small.write_text("x", encoding="utf-8")
        event = {"hook_event_name":"PreToolUse", "tool_name":"Read",
                 "tool_input":{"file_path":str(small), "limit":1}, "session_id":"threshold-test"}
        for count in range(1, 9):
            with patch.dict(os.environ, ROUTER_TEST_NOW_MS=str(count * 2000)):
                result = subprocess.run([sys.executable, "-B", str(ROOT / "hooks/router/context_guard.py")],
                                        env=os.environ.copy(), input=json.dumps(event), text=True,
                                        capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(bool(result.stdout), count == 8)


if __name__ == "__main__":
    unittest.main()
