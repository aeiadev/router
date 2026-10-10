#!/usr/bin/env python3
"""Standalone helper checks with temporary state and local Git repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class ScriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-scripts-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.env = os.environ.copy()
        self.env.update(HOME=str(self.tmp), ROUTER_STATE=str(self.tmp / "state"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        XDG_CONFIG_HOME=str(self.tmp / "config"), ROUTER_LOCAL="off")
        self.assertTrue(all(name in self.env for name in ("HOME", "XDG_CONFIG_HOME", "ROUTER_LOCAL")))
        for key in tuple(self.env):
            if key.startswith("CLOSE_LANE_"):
                self.env.pop(key)

    def run_script(self, name, *arguments, env=None):
        interpreter = "bash" if name.endswith(".sh") else sys.executable
        return subprocess.run([interpreter, str(SCRIPTS / name), *map(str, arguments)],
                              env=env or self.env, cwd=ROOT, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)

    def assert_code(self, result, code):
        self.assertEqual(result.returncode, code,
                         f"expected exit {code}; stdout={result.stdout!r}; stderr={result.stderr!r}")

    def test_helpers_offer_help(self):
        for name in ("wait-until.sh", "close-lane.sh", "cite-check.py"):
            with self.subTest(script=name):
                self.assert_code(self.run_script(name, "--help"), 0)

    def test_wait_timeout_is_124_and_respects_deadline(self):
        start = time.monotonic()
        result = self.run_script("wait-until.sh", "0.15", "exists", self.tmp / "missing")
        self.assert_code(result, 124)
        self.assertIn("WAIT_TIMEOUT", result.stderr)
        self.assertLess(time.monotonic() - start, 2, "short wait overshot its hard deadline")

    def test_wait_existing_nonempty_file(self):
        target = self.tmp / "ready"
        target.write_text("ready\n")
        self.assert_code(self.run_script("wait-until.sh", "1", "exists", target), 0)

    def test_wait_line_does_not_accept_stale_content(self):
        target = self.tmp / "output"
        target.write_text("ready\n")
        self.assert_code(self.run_script("wait-until.sh", "0.1", "line", target, "ready"), 124)

    def test_wait_dead_writer_and_bad_arguments(self):
        with subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL) as process:
            process.wait(timeout=5)
        self.assert_code(self.run_script("wait-until.sh", "1", "exists", self.tmp / "missing",
                                         "--alive", process.pid), 3)
        self.assert_code(self.run_script("wait-until.sh", "1", "pid", "0"), 2)
        self.assert_code(self.run_script("wait-until.sh", "1", "no-proc", "["), 2)

    def test_wait_pid_and_no_matching_process(self):
        with subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL) as process:
            process.wait(timeout=5)
        self.assert_code(self.run_script("wait-until.sh", "1", "pid", process.pid), 0)
        self.assert_code(self.run_script("wait-until.sh", "1", "no-proc", "router_missing_process_marker"), 0)

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo),
                                 *arguments], env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        self.assert_code(result, 0)
        return result.stdout.strip()

    def make_repo(self):
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test.invalid")
        (self.repo / "allowed.txt").write_text("baseline\n")
        (self.repo / "outside.txt").write_text("baseline\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Initial files")
        self.brief = self.tmp / "brief.txt"
        self.write_brief()

    def write_brief(self, files="allowed.txt", bar="test -s allowed.txt"):
        self.brief.write_text(f"TASK Update allowed files\nFILES {files}\nBAR {bar}\n"
                              "RETURN CHANGED / BAR / OUTPUT / NOT DONE / OPEN\n")

    def close(self):
        return self.run_script("close-lane.sh", self.repo, self.brief)

    def test_close_lane_passes_allowed_unstaged_staged_and_untracked(self):
        self.make_repo()
        (self.repo / "allowed.txt").write_text("changed\n")
        self.assert_code(self.close(), 0)
        self.git("add", "allowed.txt")
        self.assert_code(self.close(), 0)
        (self.repo / "new file.txt").write_text("new\n")
        self.write_brief(files='allowed.txt "new file.txt"')
        result = self.close()
        self.assert_code(result, 0)
        self.assertTrue(result.stdout.startswith("PASS "), result.stdout)

    def test_close_lane_blocks_scope_before_running_bar(self):
        self.make_repo()
        (self.repo / "outside.txt").write_text("changed\n")
        self.write_brief(bar="touch should-not-run.txt")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("outside FILES", result.stdout)
        self.assertFalse((self.repo / "should-not-run.txt").exists(), "BAR ran despite scope violation")

    def test_close_lane_blocks_untracked_and_staged_renames(self):
        self.make_repo()
        (self.repo / "unexpected.txt").write_text("new\n")
        self.assert_code(self.close(), 1)
        (self.repo / "unexpected.txt").unlink()
        self.git("mv", "outside.txt", "renamed.txt")
        self.write_brief(files="renamed.txt")
        self.assert_code(self.close(), 1)

    def test_close_lane_checks_commits_from_base(self):
        self.make_repo()
        self.git("checkout", "-qb", "lane")
        (self.repo / "outside.txt").write_text("committed outside scope\n")
        self.git("add", "outside.txt")
        self.git("commit", "-qm", "Outside change")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("outside.txt", result.stdout)

    def test_close_lane_checks_bar_changes_and_nonzero_exit(self):
        self.make_repo()
        self.write_brief(bar="touch unexpected.txt")
        self.assert_code(self.close(), 1)
        (self.repo / "unexpected.txt").unlink()
        self.write_brief(bar="exit 7")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("BAR exited 7", result.stdout)

    def test_close_lane_fails_bar_timeout(self):
        self.make_repo()
        self.env["CLOSE_LANE_BAR_TIMEOUT"] = "0.1"
        self.write_brief(bar="sleep 10")
        result = self.close()
        self.assert_code(result, 1)
        self.assertIn("timed out", result.stdout)

    def test_close_lane_accepts_notes_after_four_fields(self):
        self.make_repo()
        (self.repo / "allowed.txt").write_text("changed\n")
        with self.brief.open("a") as stream:
            stream.write("\nImplementation notes:\nKeep the existing behavior.\n"
                         "- Confirm the BAR output before returning.\n")
        result = self.close()
        self.assert_code(result, 0)
        self.assertTrue(result.stdout.startswith("PASS "), result.stdout)

    def test_close_lane_rejects_duplicate_and_escaping_fields(self):
        self.make_repo()
        for field in ("TASK", "FILES", "BAR", "RETURN"):
            with self.subTest(field=field):
                self.write_brief()
                with self.brief.open("a") as stream:
                    stream.write(f"\nNotes below the brief.\n{field} duplicate\n")
                result = self.close()
                self.assert_code(result, 1)
                self.assertIn("brief needs one TASK, FILES, BAR and RETURN line", result.stdout)
        self.write_brief(files="../*")
        self.assert_code(self.close(), 1)

    def test_close_lane_requires_complete_fields_before_notes(self):
        self.make_repo()
        self.brief.write_text("TASK Update allowed files\nFILES allowed.txt\n"
                              "BAR test -s allowed.txt\nNotes without RETURN.\n")
        self.assert_code(self.close(), 1)
        self.write_brief()
        self.brief.write_text(self.brief.read_text().replace("BAR ", "Notes before BAR.\nBAR "))
        self.assert_code(self.close(), 1)

    def test_close_lane_rejects_empty_extra_fields_after_notes(self):
        self.make_repo()
        for field in ("TASK", "FILES", "BAR", "RETURN"):
            with self.subTest(field=field):
                self.write_brief()
                with self.brief.open("a") as stream:
                    stream.write(f"\nNotes below the brief.\n{field}:\n")
                self.assert_code(self.close(), 1)

    def test_close_lane_globs_preserve_directory_boundaries(self):
        self.make_repo()
        nested = self.repo / "docs/sub"
        nested.mkdir(parents=True)
        (nested / "file.md").write_text("text\n")
        self.write_brief(files="docs/*")
        self.assert_code(self.close(), 1)
        self.write_brief(files="docs/**")
        self.assert_code(self.close(), 0)
        self.write_brief(files="docs/**/*.md")
        self.assert_code(self.close(), 0)

    def test_citations_validate_ranges_without_printing_contents(self):
        source = self.tmp / "source.txt"
        source.write_text("first private value\nsecond private value\n")
        report = self.tmp / "report.md"
        report.write_text("See source.txt:1-2 and 2.\n")
        result = self.run_script("cite-check.py", report, "--root", self.tmp)
        self.assert_code(result, 0)
        self.assertIn("OK source.txt:1-2", result.stdout)
        self.assertNotIn("private value", result.stdout)
        report.write_text("source.txt:2-1 source.txt:3\n")
        self.assert_code(self.run_script("cite-check.py", report, "--root", self.tmp), 1)

    def test_citations_reject_missing_and_outside_files(self):
        report = self.tmp / "report.md"
        root = self.tmp / "root"
        root.mkdir()
        report.write_text("../report.md:1\n")
        self.assert_code(self.run_script("cite-check.py", report, "--root", root), 1)
        report.write_text("No citations here.\n")
        self.assert_code(self.run_script("cite-check.py", report, "--root", root), 1)



if __name__ == "__main__":
    unittest.main(verbosity=2)
