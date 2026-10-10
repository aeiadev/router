#!/usr/bin/env python3
"""Shared helpers and switch CLI, runnable with Python and no external state."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "hooks/router/common.py"
ROUTES = ROOT / "hooks/router/routes.json"
CLI = ROOT / "bin/router"
PATH_ENV = ("ROUTER_HOME", "CLAUDE_HOME", "ROUTES_JSON", "ROUTER_STATE",
            "ROUTER_OFF_FILE", "ROUTER_OFF", "XDG_STATE_HOME")


def import_common():
    spec = importlib.util.spec_from_file_location("router_common_test", COMMON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-common-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.env = patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in PATH_ENV:
            os.environ.pop(name, None)
        os.environ.update(HOME=str(self.tmp / "home"),
                          CLAUDE_HOME=str(self.tmp / "claude"),
                          ROUTER_HOME=str(self.tmp / "router"),
                          ROUTER_STATE=str(self.tmp / "state"),
                          ROUTER_OFF_FILE=str(self.tmp / "OFF"),
                          ROUTES_JSON=str(ROUTES), PYTHONDONTWRITEBYTECODE="1",
                          XDG_CONFIG_HOME=str(self.tmp / "config"), ROUTER_LOCAL="off")
        self.assertTrue(all(name in os.environ for name in ("HOME", "XDG_CONFIG_HOME", "ROUTER_LOCAL")))
        self.common = import_common()
        self.routes = self.common.load_routes()
        self.state = self.tmp / "state"

    def transcript(self, name, rows):
        path = self.tmp / name
        path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n"
                                for row in rows), encoding="utf-8")
        return path

    def cli(self, command):
        return subprocess.run([sys.executable, "-B", str(CLI), command],
                              env=os.environ.copy(), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)

    def run_cli(self, *args, cli=CLI, env=None):
        return subprocess.run([sys.executable, "-B", str(cli), *args], env=env or os.environ.copy(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)

    def package_copy(self, name):
        """A throwaway copy of the package: bin/router plus hooks/router."""
        top = self.tmp / name
        (top / "bin").mkdir(parents=True)
        shutil.copy2(CLI, top / "bin/router")
        shutil.copytree(ROOT / "hooks/router", top / "hooks/router",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return top

    def test_cli_discovers_command_files_beside_common(self):
        top = self.package_copy("package")
        (top / "hooks/router/cmd_x.py").write_text(
            'HELP = "throwaway test command"\n\n\ndef main(argv):\n'
            '    print("x ran", argv)\n    return 3\n', encoding="utf-8")
        ran = self.run_cli("x", "first", "--second", cli=top / "bin/router")
        self.assertEqual((ran.returncode, ran.stdout, ran.stderr), (3, "x ran ['first', '--second']\n", ""))
        unknown = self.run_cli("nosuch", cli=top / "bin/router")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("unknown command 'nosuch'", unknown.stderr)
        for name in ("on", "off", "status", "auto", "ladder", "x", "throwaway test command"):
            self.assertIn(name, unknown.stderr)
        self.assertIn("throwaway test command", self.run_cli("--help", cli=top / "bin/router").stdout)
        self.assertEqual(self.run_cli("status", cli=top / "bin/router").returncode, 0, "built-ins still run")
        self.assertNotIn("x", self.run_cli("nosuch").stderr.split(), "the throwaway file stays in its copy")
        # Installed CLIs may find the package through ROUTER_HOME, whose common.py is a symlink.
        home = self.tmp / "installed"
        (home / "router").mkdir(parents=True)
        shutil.copytree(top / "hooks", home / "hooks")
        (home / "router/common.py").symlink_to("../hooks/router/common.py")
        (home / "router/routes.json").symlink_to("../hooks/router/routes.json")
        alone = self.tmp / "alone/bin/router"
        alone.parent.mkdir(parents=True)
        shutil.copy2(CLI, alone)
        linked = self.run_cli("x", cli=alone, env=dict(os.environ, ROUTER_HOME=str(home / "router")))
        self.assertEqual((linked.returncode, linked.stdout), (3, "x ran []\n"), linked.stderr)

    def test_router_ladder_lists_and_resets_a_spawned_ladder(self):
        project = self.tmp / "project"
        (project / ".git").mkdir(parents=True)
        prompt = "TASK add a helper\nFILES src/a.py\nBAR tests\nRETURN five lines"
        event = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(project), "session_id": "s",
                 "tool_input": {"subagent_type": "builder", "prompt": prompt, "description": "helper"}}
        spawn = subprocess.run([sys.executable, "-B", str(ROOT / "hooks/router/spawn_guard.py")],
                               input=json.dumps(event), env=os.environ.copy(), text=True,
                               capture_output=True, timeout=20)
        self.assertEqual(spawn.returncode, 0, spawn.stderr)
        key = self.common.claude_key(self.common.project_key(str(project)), self.common.brief_hash(prompt))
        listed = self.run_cli("ladder", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        rows = [line.split() for line in listed.stdout.splitlines()]
        self.assertEqual(rows[0], ["key", "round", "tier", "age"])
        self.assertEqual(rows[1][:3], [key, "1/3", "std"])
        self.assertRegex(rows[1][3], r"^\d+m$")
        reset = self.run_cli("ladder", "reset", key)
        self.assertEqual(reset.returncode, 0, reset.stderr)
        self.assertIn(key, reset.stdout)
        self.assertNotIn(key, self.run_cli("ladder", "list").stdout)
        again = self.run_cli("ladder", "reset", key)
        self.assertEqual(again.returncode, 1)
        self.assertIn("no attempts", again.stderr)
        self.assertEqual(self.run_cli("ladder").returncode, 2, "a missing ladder action is a usage error")

    def test_router_ladder_list_needs_no_ledger_and_creates_none(self):
        listed = self.run_cli("ladder", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("no live ladders", listed.stdout)
        self.assertFalse((self.state / "ledger.sqlite3").exists())

    def test_import_has_no_state_side_effects_and_stdlib_only(self):
        self.assertFalse(self.state.exists(), "import created state")
        self.assertFalse((self.tmp / "router").exists(), "import created config")
        for source in (COMMON, CLI):
            names = set()
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names.update(item.name.split(".")[0] for item in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names.add(node.module.split(".")[0])
            self.assertFalse(names - sys.stdlib_module_names - {"common"}, source.name)

    def test_route_table_has_only_public_required_sections(self):
        self.assertEqual(self.common.validate_routes(self.routes), [])
        self.assertEqual(set(self.common.REQUIRED_KEYS), {"version", "tiers", "router", "context"})
        minimal = {key: value for key, value in self.routes.items()
                   if key in {"version", "tiers", "router", "context", "tier_sources"}}
        self.assertEqual(self.common.validate_routes(minimal), [])
        self.assertEqual(self.routes["router"]["modes"]["ladder"], "enforce")

    def test_path_defaults_and_environment_precedence(self):
        for name in PATH_ENV:
            os.environ.pop(name, None)
        home = self.tmp / "home"
        self.assertEqual(self.common.claude_home(), home / ".claude")
        self.assertEqual(self.common.router_home(), home / ".claude/router")
        self.assertEqual(self.common.state_path(), home / ".local/state/claude-router")
        self.assertEqual(self.common.off_file(), home / ".local/state/claude-router/OFF")
        self.assertEqual(import_common().ROUTES_PATH, ROUTES)
        os.environ["CLAUDE_HOME"] = "~/custom-claude"
        self.assertEqual(self.common.router_home(), home / "custom-claude/router")
        os.environ["ROUTER_HOME"] = "~/custom-router"
        os.environ["XDG_STATE_HOME"] = "~/xdg-state"
        self.assertEqual(self.common.router_home(), home / "custom-router")
        self.assertEqual(self.common.state_path(), home / "xdg-state/claude-router")
        os.environ["ROUTER_STATE"] = "~/custom-state"
        self.assertEqual(self.common.off_file(), home / "custom-state/OFF")
        os.environ["ROUTER_OFF_FILE"] = "~/switch"
        self.assertEqual(self.common.off_file(), home / "switch")
        self.assertFalse(home.exists(), "path resolution created directories")

    def test_routes_override_and_explicit_path(self):
        custom = copy.deepcopy(self.routes)
        custom["router"]["modes"]["risk"] = "off"
        alternate = self.tmp / "routes.json"
        alternate.write_text(json.dumps(custom), encoding="utf-8")
        os.environ["ROUTES_JSON"] = str(alternate)
        self.assertEqual(self.common.load_routes(), custom)
        self.assertEqual(self.common.load_routes(ROUTES), self.routes)

    def test_unreadable_and_invalid_route_tables_report_source(self):
        bad = self.tmp / "bad-routes.json"
        for raw in (None, b"{broken", b"[]", b"\xff"):
            with self.subTest(raw=raw):
                if raw is not None:
                    bad.write_bytes(raw)
                with self.assertRaises(ValueError) as caught:
                    self.common.load_routes(bad)
                self.assertIn(str(bad), str(caught.exception))

    def test_validation_reports_invalid_modes_models_caps_and_sources(self):
        mutations = [
            (lambda r: r["router"]["modes"].update(risk="invalid"), "risk"),
            (lambda r: r["context"]["modes"].update(large_read="invalid"), "large_read"),
            (lambda r: r["tiers"]["builder"]["std"].update(model="unknown"), "model"),
            (lambda r: r["tiers"]["builder"]["std"].update(effort="none"), "effort"),
            (lambda r: r["tiers"]["sweeper"]["light"].update(effort="low"), "effort"),
            (lambda r: r["tiers"]["builder"]["std"].update(agent="other"), "agent"),
            (lambda r: r["tiers"]["planner"]["up"].update(agent="planner-std"), "duplicate"),
            (lambda r: r["context"]["caps"].pop("exec"), "exec"),
            (lambda r: r["context"]["caps"].update(judge=0), "judge"),
            (lambda r: r["context"]["caps"].update(sweep=True), "sweep"),
            (lambda r: r["router"]["types"]["planner"].pop("allow"), "allow"),
            (lambda r: r["router"]["types"]["planner"].pop("class"), "class"),
            (lambda r: r.pop("router"), "router"),
            (lambda r: r["tier_sources"]["planner"].pop("source"), "source"),
            (lambda r: r["tier_sources"]["planner"].update(prefix=7), "prefix"),
            (lambda r: r["router"]["unlisted"].update(default_model="unknown"), "default_model"),
            (lambda r: r["router"]["unlisted"].update(models=[]), "models"),
            (lambda r: r["router"]["unlisted"].pop("mode_key"), "mode_key"),
            (lambda r: r["router"].update(top_tier_models="upper"), "top_tier_models"),
            (lambda r: r["router"].update(top_tier_models=[None]), "top_tier_models"),
            (lambda r: r["router"].update(top_tier_models=[" "]), "top_tier_models"),
        ]
        for mutate, expected in mutations:
            with self.subTest(expected=expected):
                changed = copy.deepcopy(self.routes)
                mutate(changed)
                errors = self.common.validate_routes(changed)
                self.assertTrue(any(expected in error for error in errors), errors)
        for invalid in (None, [], "bad", 1):
            self.assertTrue(self.common.validate_routes(invalid))

    def test_validation_handles_malformed_type_shapes(self):
        for invalid in (7, True, [], None, "invalid"):
            with self.subTest(invalid=invalid):
                changed = copy.deepcopy(self.routes)
                changed["router"]["types"] = invalid
                errors = self.common.validate_routes(changed)
                self.assertTrue(any("router.types" in error for error in errors), errors)

    def test_switch_file_and_environment_are_equivalent(self):
        self.assertFalse(self.common.killed())
        self.common.off_file().touch()
        self.assertTrue(self.common.killed())
        self.common.off_file().unlink()
        with patch.dict(os.environ, ROUTER_OFF="1"):
            self.assertTrue(self.common.killed())
        for value in ("0", "", "true"):
            with patch.dict(os.environ, ROUTER_OFF=value):
                self.assertFalse(self.common.killed())
        self.assertFalse(self.state.exists())

    def test_cli_off_on_status_share_the_switch(self):
        status = self.cli("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.splitlines()[0], "on")
        self.assertIn(f"switch: {self.common.off_file()}", status.stdout)
        for section in ("router", "context"):
            for rule, mode in self.routes[section]["modes"].items():
                self.assertIn(f"{section}.{rule}: {mode}", status.stdout)
        self.assertFalse(self.state.exists(), "status created state")
        for _ in range(2):
            result = self.cli("off")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[0], "off")
            self.assertTrue(self.common.killed())
        for _ in range(2):
            result = self.cli("on")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[0], "on")
            self.assertFalse(self.common.killed())

    def test_cli_on_cannot_clear_an_environment_switch(self):
        self.common.off_file().touch()
        os.environ["ROUTER_OFF"] = "1"
        result = self.cli("on")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.common.off_file().exists())
        self.assertEqual(result.stdout.splitlines()[0], "off")
        self.assertIn("ROUTER_OFF=1", result.stdout)
        self.assertIn("unset", result.stdout)

    def test_cli_reports_invalid_configuration(self):
        bad = self.tmp / "invalid.json"
        bad.write_text("{", encoding="utf-8")
        os.environ["ROUTES_JSON"] = str(bad)
        result = self.cli("status")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("routes table", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_read_event_rejects_malformed_input(self):
        self.assertEqual(self.common.read_event(io.StringIO("{}")), {})
        for raw in ("", " \n", "{bad", "[1]", "null", "42", "\"text\""):
            self.assertIsNone(self.common.read_event(io.StringIO(raw)), raw)
        closed = io.StringIO()
        closed.close()
        self.assertIsNone(self.common.read_event(closed))
        for event in ({}, {"agent_id": ""}, {"agent_id": None}):
            self.assertTrue(self.common.is_main(event))
        self.assertFalse(self.common.is_main({"agent_id": "child"}))

    def test_modes_enforce_immediately_without_transcript(self):
        for mode in ("off", "shadow", "enforce"):
            self.assertEqual(self.common.effective_mode(mode, {}, self.routes), mode)
        for mode in (None, "", "bad", True, 7, ["enforce"]):
            self.assertEqual(self.common.effective_mode(mode, {}, self.routes), "shadow")

    def test_classification_covers_every_declared_variant(self):
        for base, entry in self.routes["router"]["types"].items():
            classified = self.common.classify(base.upper(), self.routes)
            self.assertEqual((classified["base"], classified["class"]), (base, entry["class"]))
            for tier, spec in self.routes["tiers"].get(base, {}).items():
                classified = self.common.classify(spec["agent"].upper(), self.routes)
                self.assertEqual((classified["base"], classified["tier"]), (base, tier))
        self.assertIsNone(self.common.classify("unknown-agent", self.routes))
        self.assertIsNone(self.common.classify("researcher-mid", self.routes))
        self.assertEqual(self.common.classify("", self.routes)["base"], "worker")

    def test_route_lines_require_a_known_code_and_take_the_first_match(self):
        parse = lambda prompt: self.common.route_line(prompt, self.routes)
        for code in self.routes["router"]["up_codes"]:
            self.assertEqual(parse(f"  Route: UP {code.upper()}\nTASK fix"),
                             {"kind": "up", "code": code})
        self.assertEqual(parse("route: light"), {"kind": "light", "code": None})
        self.assertEqual(parse("route: up unknown"), {"kind": "up", "code": None})
        self.assertEqual(parse("route: up"), {"kind": "up", "code": None})
        self.assertEqual(parse("route: light\nroute: up risk"), {"kind": "light", "code": None})
        for prompt in ("mention route: up risk", "route: upgrade", "no route", None):
            self.assertEqual(parse(prompt), {"kind": None})

    def test_brief_hash_tracks_task_and_files_only(self):
        first = "route: up risk\nTASK   Fix parser\nFILES  src/a.py   src/b.py\nBAR tests"
        second = "TASK Fix parser\n FILES src/a.py\tsrc/b.py\nBAR another check"
        digest = self.common.brief_hash(first)
        self.assertRegex(digest, r"^[0-9a-f]{16}$")
        self.assertEqual(digest, self.common.brief_hash(second))
        self.assertEqual(digest, self.common.brief_hash(first + "\nRETURN five lines"))
        self.assertNotEqual(digest, self.common.brief_hash(first.replace("parser", "reader")))
        self.assertNotEqual(digest, self.common.brief_hash(first.replace("b.py", "c.py")))
        self.assertEqual(self.common.brief_hash("TASK only"), self.common.brief_hash(" TASK   only "))

    def test_brief_hash_recognizes_labels_case_insensitively(self):
        first = "TASK: Fix parser\nFILES: src/a.py\nBAR tests"
        second = " task:  Fix  parser\n files: src/a.py\nBAR another check\nroute: up ladder"
        self.assertEqual(self.common.brief_hash(first), self.common.brief_hash(second))
        fields = dict(self.common.brief_fields(second))
        self.assertEqual(fields["TASK"], "Fix  parser")
        self.assertEqual(fields["FILES"], "src/a.py")

    def test_brief_fields_follow_the_c1_field_rule(self):
        fields = self.common.brief_fields
        for prompt in ("TASK fix it", "TASK: fix it", "task: fix it", "  Task:fix it", "\tTASK\tfix it"):
            with self.subTest(field=prompt):
                self.assertEqual(fields(prompt), [("TASK", "fix it")])
        for prose in ("Task fix it", "task fix it", "TASKS: fix it", "Files are listed below", "the TASK: x"):
            with self.subTest(prose=prose):
                self.assertEqual(fields(prose), [])
        brief = ("Preamble is no field.\nTASK fix the parser\nkeep the old API\n"
                 "FILES src/a.py\n  src/b.py\nBAR: run tests\nRETURN five lines\nNotes after RETURN.")
        self.assertEqual(fields(brief), [("TASK", "fix the parser\nkeep the old API"),
                                         ("FILES", "src/a.py\n  src/b.py"), ("BAR", "run tests"),
                                         ("RETURN", "five lines\nNotes after RETURN.")])
        self.assertEqual(fields("TASK:\n  fix it\nTASK again"), [("TASK", "fix it"), ("TASK", "again")],
                         "repeats stay visible and a colon field may start on the next line")
        for value in (None, 7, b"TASK x"):
            self.assertEqual(fields(value), [])

    def test_brief_hash_hashes_the_first_task_and_files_lines_of_the_fields(self):
        digest = self.common.brief_hash("TASK fix it\nFILES a.py")
        for same in ("TASK: fix it\nFILES: a.py", "task: fix it\nfiles: a.py\nBAR other",
                     "TASK fix it\nmore text\nFILES a.py\nmore files", "TASK:\nfix it\nFILES:   a.py"):
            with self.subTest(same=same):
                self.assertEqual(self.common.brief_hash(same), digest)
        self.assertNotEqual(self.common.brief_hash("Task fix it\nFILES a.py"), digest, "prose is not a TASK field")
        self.assertEqual(self.common.brief_hash("Task fix it\nFILES a.py"),
                         self.common.brief_hash("Task  fix it FILES a.py"), "no TASK field hashes the whole prompt")

    def test_project_key_walks_up_to_the_git_root_without_a_subprocess(self):
        def digest(path):
            return hashlib.sha256(str(path).encode()).hexdigest()[:12]
        root = (self.tmp / "repo").resolve()
        (root / ".git/worktrees/lane").mkdir(parents=True)
        (root / ".git/worktrees/lane/commondir").write_text("../..\n", encoding="utf-8")
        deep = root / "src/pkg"
        deep.mkdir(parents=True)
        worktree = root / ".claude/worktrees/lane"
        (worktree / "src").mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {root / '.git/worktrees/lane'}\n", encoding="utf-8")
        submodule = root / "vendor/sub"
        submodule.mkdir(parents=True)
        (submodule / ".git").write_text("gitdir: ../../.git/modules/sub\n", encoding="utf-8")
        plain = (self.tmp / "plain/dir").resolve()
        plain.mkdir(parents=True)
        other = (self.tmp / "other").resolve()
        (other / ".git").mkdir(parents=True)
        with patch("subprocess.Popen", side_effect=AssertionError("project_key started a subprocess")):
            key = self.common.project_key(str(root))
            self.assertRegex(key, r"^[0-9a-f]{12}$")
            self.assertEqual(key, digest(root))
            self.assertEqual(self.common.project_key(str(deep)), key)
            self.assertEqual(self.common.project_key(str(worktree / "src")), key,
                             "a worktree under the project root must keep the project key")
            self.assertEqual(self.common.project_key(str(submodule)), digest(submodule))
            # No .git up to the filesystem root means the cwd itself (a shared /tmp may hold one).
            above = next((path for path in plain.parents if (path / ".git").exists()), plain)
            self.assertEqual(self.common.project_key(str(plain)), digest(above))
            self.assertNotEqual(self.common.project_key(str(other)), key)

    def test_claude_and_codex_keys_include_the_project(self):
        common = self.common
        brief = common.brief_hash("TASK x\nFILES y")
        first = common.claude_key("aaaaaaaaaaaa", brief)
        self.assertRegex(first, r"^c:[0-9a-f]{16}$")
        self.assertNotEqual(first, common.claude_key("bbbbbbbbbbbb", brief))
        self.assertNotEqual(first, common.claude_key("aaaaaaaaaaaa", common.brief_hash("TASK z\nFILES y")))
        builder = common.classify("builder-std", self.routes)
        codex = common.codex_key("aaaaaaaaaaaa", "task", builder)
        self.assertRegex(codex, r"^x:[0-9a-f]{16}$")
        self.assertNotEqual(codex, common.codex_key("bbbbbbbbbbbb", "task", builder))
        self.assertEqual(codex, common.codex_key("aaaaaaaaaaaa", " task ", common.classify("builder-light", self.routes)))
        self.assertNotEqual(codex, common.codex_key("aaaaaaaaaaaa", "task", common.classify("builder-in-place", self.routes)))
        for bad in (None, "", "  ", 7):
            self.assertIsNone(common.codex_key("aaaaaaaaaaaa", bad, builder))
        self.assertFalse(hasattr(common, "codex_task_hash"), "codex_key replaces codex_task_hash")

    def test_lane_labels_are_scrubbed_per_c3(self):
        os.environ["HOME"] = "/home/u"  # short and fixed: the 60-character cap must not depend on TMPDIR
        home = os.environ["HOME"]
        label = lambda value, routes=self.routes: self.common.lane_label(value, routes)
        cases = [
            ("Implement the helper", "Implement the helper"),
            ("  fix\nthe\r\nparser\tnow\x00\x1b[31m  red ", "fix the parser now [31m red"),
            ("bidi\u202eflip\u2028line", "bidi flip line"),
            (home, "~"),
            (home + "/repo/src", "~/repo/src"),
            (home + "x/repo", home + "x/repo"),
            ("see " + home + "/repo", "see " + home + "/repo"),
            ("a" * 61, "a" * 60),
            ("\u00e9" * 70, "\u00e9" * 60),
            ("lone\ud800surrogate", "lone?surrogate"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                got = label(value)
                self.assertEqual(got, expected)
                got.encode("utf-8")
        for value in (None, 7, ["x"], {"a": 1}, "", " \n\t "):
            with self.subTest(value=value):
                self.assertIsNone(label(value))
        off = copy.deepcopy(self.routes)
        off["router"]["lane_labels"] = False
        self.assertIsNone(label("Implement the helper", off))
        self.assertEqual(self.common.session_hash("abc"), hashlib.sha256(b"abc").hexdigest()[:16])
        for value in (None, "", 7):
            self.assertIsNone(self.common.session_hash(value))

    def test_lane_labels_setting_is_validated(self):
        self.assertIs(self.routes["router"]["lane_labels"], True)
        changed = copy.deepcopy(self.routes)
        changed["router"].pop("lane_labels")
        self.assertEqual(self.common.validate_routes(changed), [])
        self.assertEqual(self.common.lane_label("x", changed), "x")
        for bad in (0, 1, "false", None, []):
            with self.subTest(bad=bad):
                changed["router"]["lane_labels"] = bad
                errors = self.common.validate_routes(changed)
                self.assertTrue(any("lane_labels" in error for error in errors), errors)

    def test_ladder_ttl_hours_is_validated_and_defaults_to_twelve(self):
        self.assertEqual(self.routes["router"]["ladder_ttl_hours"], 12)
        self.assertEqual(self.common.ladder_ttl_hours(self.routes), 12)
        changed = copy.deepcopy(self.routes)
        changed["router"].pop("ladder_ttl_hours")
        self.assertEqual(self.common.validate_routes(changed), [])
        self.assertEqual(self.common.ladder_ttl_hours(changed), 12)
        for good in (1, 720):
            changed["router"]["ladder_ttl_hours"] = good
            self.assertEqual(self.common.validate_routes(changed), [])
            self.assertEqual(self.common.ladder_ttl_hours(changed), good)
        for bad in (0, 721, -3, 12.5, "12", True, None):
            with self.subTest(bad=bad):
                changed["router"]["ladder_ttl_hours"] = bad
                errors = self.common.validate_routes(changed)
                self.assertTrue(any("ladder_ttl_hours" in error for error in errors), errors)

    def test_brief_hash_falls_back_to_normalized_prompt_without_routes(self):
        for prompt in ("fix it", "FILES src/a.py", "", "\n\t"):
            with self.subTest(prompt=prompt):
                digest = self.common.brief_hash(prompt)
                self.assertRegex(digest or "", r"^[0-9a-f]{16}$")
                self.assertEqual(digest, self.common.brief_hash("route: up ladder\n" + prompt))
                self.assertEqual(digest, self.common.brief_hash(prompt + "\nRoute: light"))
        self.assertEqual(self.common.brief_hash(" fix  it \n"), self.common.brief_hash("fix\tit"))
        self.assertNotEqual(self.common.brief_hash("fix it"), self.common.brief_hash("fix that"))

    def test_risk_detection_uses_brief_fields_and_whole_words(self):
        cases = [
            ("TASK tidy\nFILES .claude/hooks/helper.py", ["*/.claude/hooks/*"]),
            ("TASK deploy site\nFILES src/index.html", ["deploy"]),
            ("TASK deployment notes\nFILES notes.md", []),
            ("TASK tidy\nFILES .env,src/a.py", ["*.env"]),
            ("FILES .env;src/a.py", ["*.env"]),
            ("background discussion about deploy", []),
        ]
        for prompt, expected in cases:
            self.assertEqual(self.common.risk_hits(prompt, self.routes), expected, prompt)

    def test_session_timestamp_cache_and_bounded_read(self):
        rows = ["broken", {"timestamp": "2024-01-01T00:00:00Z"}]
        path = self.transcript("start.jsonl", rows)
        event = {"session_id": "timestamp-test", "transcript_path": str(path)}
        stamp = self.common.session_start(event)
        self.assertEqual(stamp, 1704067200.0)
        path.unlink()
        self.assertEqual(self.common.session_start(event), stamp)
        late = self.transcript("late.jsonl", [{}] * 20 + rows)
        self.assertIsNone(self.common.session_start({"transcript_path": str(late)}))
        self.assertIsNone(self.common.session_start({}))
        self.assertTrue(self.common.session_active({}, {"active_after": ""}))
        self.assertTrue(self.common.session_active(event, {"active_after": "2024-01-01T00:00:00Z"}))
        self.assertFalse(self.common.session_active(event, {"active_after": "2024-01-01T00:00:01Z"}))

    def test_session_model_reads_last_real_assistant_in_tail(self):
        def assistant(model):
            return {"type": "assistant", "message": {"model": model}}
        path = self.transcript("model.jsonl", [assistant("sonnet"),
                               {"type": "user", "message": {"model": "haiku"}},
                               assistant("opus"), assistant("<synthetic>"), "broken"])
        self.assertEqual(self.common.session_model(path), "opus")
        filler = {"type": "user", "message": {"content": "x" * 1024}}
        path = self.transcript("long.jsonl", [assistant("opus")] + [filler] * 270)
        self.assertIsNone(self.common.session_model(path))
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(assistant("sonnet")) + "\n")
        self.assertEqual(self.common.session_model(path), "sonnet")

    def test_logs_are_bounded_strict_json_and_errors_do_not_include_content(self):
        self.common.log("test", {"prompt": "x" * 5000, "nested": ["y" * 301], "value": float("nan")})
        self.common.log("test", {"event": "second"})
        raw = (self.state / "test.jsonl").read_text(encoding="utf-8")
        def reject_constant(value):
            raise ValueError(value)
        records = [json.loads(line, parse_constant=reject_constant) for line in raw.splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["prompt"], "<len 5000>")
        self.assertEqual(records[0]["nested"], ["<len 301>"])
        self.assertEqual(records[1]["event"], "second")
        with self.assertRaises(SystemExit) as caught:
            self.common.fail_open("test-hook", RuntimeError("example-private-content"))
        self.assertEqual(caught.exception.code, 0)
        error = (self.state / "errors.jsonl").read_text(encoding="utf-8")
        self.assertIn("RuntimeError", error)
        self.assertNotIn("example-private-content", error)
        blocker = self.tmp / "blocker"
        blocker.write_text("file", encoding="utf-8")
        with patch.dict(os.environ, ROUTER_STATE=str(blocker / "state")):
            self.assertIsNone(self.common.log("test", {"event": "cannot-write"}))

    def test_spawn_marker_and_safe_state_filenames(self):
        before = time.time()
        self.common.note_spawn("marker-test")
        stamp = self.common.last_spawn("marker-test")
        self.assertIsNotNone(stamp)
        self.assertLess(abs(stamp - before), 5)
        marker = self.state / "chain/marker-test.spawn"
        os.utime(marker, (before - 1000, before - 1000))
        self.assertLess(self.common.last_spawn("marker-test"), before - 900)
        self.common.note_spawn("../../escaped")
        self.assertFalse((self.tmp / "escaped.spawn").exists())
        self.assertIsNone(self.common.last_spawn(""))
        self.assertIsNone(self.common.last_spawn("missing"))

    def test_read_like_recognizes_reads_not_scripts(self):
        for tool in ("Read", "Grep", "Glob"):
            self.assertTrue(self.common.read_like(tool, {}))
        for command in ("cat file", "cd /tmp && timeout 20 rg word", "A=1 jq . file",
                        "python3 -c \"print(1)\"", "python3 -", "timeout -k 5 30 sed -n 1p file"):
            self.assertTrue(self.common.read_like("Bash", {"command": command}), command)
        for command in ("git status", "python3 tests/run.py", "python3 -m unittest", "bash run.sh", ""):
            self.assertFalse(self.common.read_like("Bash", {"command": command}), command)
        self.assertFalse(self.common.read_like("Write", {"command": "cat file"}))

    def test_model_normalization_and_compact_output(self):
        for raw, model in (("claude-opus-4-1", "opus"), (" SONNET ", "sonnet"),
                           ("claude-haiku-4-5", "haiku"), ("inherit", "inherit")):
            self.assertEqual(self.common.norm_model(raw), model)
        for raw in (None, "", 3, []):
            self.assertIsNone(self.common.norm_model(raw))
        self.assertTrue(self.common.is_fork(" Fork "))
        self.assertFalse(self.common.is_fork("forked"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.common.emit({"ok": True})
        self.assertEqual(output.getvalue(), "{\"ok\":true}\n")

    def test_agent_pins_use_frontmatter_and_project_precedence(self):
        home = self.common.claude_home()
        def put(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return str(path)
        user = put(home / "agents/example.md", "---\nmodel: \"haiku\" # pinned\n---\nmodel: opus\n")
        self.assertEqual(self.common.agent_file("example"), user)
        self.assertEqual(self.common.agent_pin("example"), "haiku")
        put(home / "agents/inherited.md", "---\nmodel: inherit\n---\n")
        put(home / "agents/body.md", "---\nname: body\n---\nmodel: opus\n")
        self.assertIsNone(self.common.agent_pin("inherited"))
        self.assertIsNone(self.common.agent_pin("body"))
        project = self.tmp / "project/sub"
        project.mkdir(parents=True)
        local = put(project.parent / ".claude/agents/example.md", "---\nmodel: opus\n---\n")
        self.assertEqual(self.common.agent_file("example", str(project)), local)
        self.assertEqual(self.common.agent_pin("example", str(project)), "opus")
        for name in (None, "", "../example", "a/b", "a\\b", ".hidden"):
            self.assertIsNone(self.common.agent_file(name), name)
        plugin = put(home / "plugins/cache/toolkit/1/agents/reviewer.md", "---\nmodel: sonnet\n---\n")
        self.assertEqual(self.common.agent_file("toolkit:reviewer"), plugin)
        put(home / "plugins/.hidden/agents/hidden.md", "---\nmodel: opus\n---\n")
        self.assertIsNone(self.common.agent_file("hidden"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
