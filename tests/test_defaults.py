#!/usr/bin/env python3
"""Written defaults stay in sync and preserve host instruction bytes."""
import copy
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common

MARKER = re.compile(r"<!-- router:defaults:start(?: [^\r\n<>]*?)? -->|<!-- router:defaults:end -->")


class RenderTests(unittest.TestCase):
    def test_render_values_and_examples(self):
        routes = common.load_routes()
        self.assertEqual(routes["context"]["command_threshold"], 2)
        for host, example in (("claude", "CLAUDE"), ("codex", "CODEX")):
            block = common.render_defaults(host, routes, "nudge")
            self.assertEqual(block, (ROOT / "examples" / (example + ".md.snippet")).read_text())
            self.assertNotIn("{{", block)
            self.assertLess(len(block.splitlines()), 25)
            self.assert_lines_fit(block)
            self.assertIn("1500 characters for builders and judges, 3000 for sweepers, "
                          "5000 for researchers, planners and workers",
                          " ".join(block.split()))
            with tempfile.TemporaryDirectory() as temporary:
                route_copy = Path(temporary) / "routes.json"
                moved = copy.deepcopy(routes)
                moved["context"]["caps"][moved["router"]["types"]["judge"]["class"]] = 3000
                route_copy.write_text(json.dumps(moved))
                regrouped = common.render_defaults(host, common.load_routes(route_copy), "nudge")
                self.assertIn("1500 characters for builders, 3000 for judges and sweepers, "
                              "5000 for researchers, planners and workers",
                              " ".join(regrouped.split()))
                self.assert_lines_fit(regrouped)
            changed = copy.deepcopy(routes)
            changed["context"].update(files_threshold=8, command_threshold=9)
            changed["context"]["chain"]["first"] = 11
            changed["context"]["caps"][changed["router"]["types"]["builder"]["class"]] = 1234
            changed["context"]["caps"].update(judge=2345, sweep=3456, research=4567, worker=5678)
            rendered = common.render_defaults(host, changed, "enforce")
            self.assert_lines_fit(rendered)
            for phrase in ("8 files", "9 commands", "11 reads", "1234 characters for builders",
                           "2345 for judges", "3456 for sweepers",
                           "4567 for researchers and planners", "5678 for workers",
                           "blocks further reads"):
                self.assertIn(phrase, " ".join(rendered.split()))
            caps = changed["context"]["caps"]
            classes = changed["router"]["types"]
            expected_numbers = [changed["context"]["files_threshold"],
                                changed["context"]["command_threshold"],
                                changed["context"]["chain"]["first"]]
            expected_numbers.extend(set(caps[classes[role]["class"]] for role in
                                        ("builder", "judge", "sweeper", "researcher", "planner", "worker")))
            self.assertEqual(Counter(map(int, re.findall(r"\b\d+\b", rendered))),
                             Counter(expected_numbers))
            for mode in ("off", "suggest", "nudge"):
                self.assertNotIn("blocks further reads", common.render_defaults(host, routes, mode))
            if host == "codex":
                for word in ("$dispatch", "spawn_agent", "task_name"):
                    self.assertIn(word, block)

    def assert_lines_fit(self, block):
        lines = block.splitlines()
        markers = [line for line in lines if MARKER.fullmatch(line)]
        self.assertEqual(markers, [lines[0], lines[-1]], "expected exactly the two marker lines")
        self.assertTrue(any(line.startswith("- Returns stay under ") for line in lines),
                        "rendered return bullet missing")
        for line in lines[1:-1]:
            self.assertLessEqual(len(line), 80, f"line exceeds 80 columns: {line!r}")

    def test_every_line_fits_for_every_host_mode_and_caps(self):
        routes = common.load_routes()
        with tempfile.TemporaryDirectory() as temporary:
            route_copy = Path(temporary) / "routes.json"
            changed = copy.deepcopy(routes)
            changed["context"].update(files_threshold=12345, command_threshold=23456)
            changed["context"]["chain"]["first"] = 34567
            caps = changed["context"]["caps"]
            for offset, cap_class in enumerate(sorted(caps)):
                caps[cap_class] = 98760 + offset
            route_copy.write_text(json.dumps(changed))
            variants = (("default", routes), ("changed caps", common.load_routes(route_copy)))
            self.assertEqual(variants[1][1]["context"]["caps"], caps)
            for host in ("claude", "codex"):
                for mode in ("off", "suggest", "nudge", "enforce"):
                    for name, table in variants:
                        with self.subTest(host=host, mode=mode, routes=name):
                            block = common.render_defaults(host, table, mode)
                            self.assert_lines_fit(block)
                            if mode == "enforce":
                                self.assertIn("blocks further reads until you delegate",
                                              " ".join(block.split()))

    def test_unknown_placeholder(self):
        with self.assertRaisesRegex(ValueError, "placeholder"):
            common.render_defaults("claude", common.load_routes(), "nudge", template="{{surprise}}")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = os.environ.copy()
        excluded = {"CLAUDE_HOME", "CODEX_HOME", "ROUTES_JSON"}
        for variable in tuple(self.env):
            if variable in excluded or variable.startswith("ROUTER_"):
                del self.env[variable]
        self.env.update(HOME=str(self.home), XDG_STATE_HOME=str(self.home / "state"),
                        PYTHONDONTWRITEBYTECODE="1")
        self.paths = {}
        for host, filename in (("claude", "CLAUDE.md"), ("codex", "AGENTS.md")):
            folder = self.home / (host + " custom")
            folder.mkdir()
            self.env[host.upper() + "_HOME"] = str(folder)
            self.paths[host] = folder / filename

    def install(self, *args, ok=True):
        result = subprocess.run(["bash", str(ROOT / "install.sh"), "--host", "both", *args],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def cli(self, mode, ok=True, installed=None):
        command = ROOT / "bin/router" if installed is None else self.paths[installed].parent / "router/bin/router"
        result = subprocess.run([sys.executable, str(command), "auto", mode], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*") if p.is_file()}

    def test_insert_replace_idempotence_outside_bytes_and_backup(self):
        prefix, suffix = b"# User rules\r\n\xffkeep\r\n", b"\r\n# After\nno final newline"
        old = b"<!-- router:defaults:start old -->\nold body\n<!-- router:defaults:end -->"
        for path in self.paths.values():
            path.write_bytes(prefix + old + suffix)
        result = self.install("--with-defaults")
        for host, path in self.paths.items():
            expected = common.render_defaults(host, common.load_routes(), "nudge").encode()
            self.assertEqual(path.read_bytes(), prefix + expected + suffix)
            backups = list(path.parent.glob(path.name + ".router-backup-*"))
            self.assertEqual([p.read_bytes() for p in backups], [prefix + old + suffix])
            self.assertIn(f"Backed up defaults: {backups[0]}", result.stdout.splitlines())
            manifest = json.loads((path.parent / "router/install-manifest.json").read_text())
            self.assertTrue(manifest["defaults"])
        before = self.snapshot()
        result = self.install("--with-defaults")
        self.assertNotIn("Backed up defaults:", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.install("--uninstall")
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), prefix + suffix)

    def test_insert_append_and_uninstall(self):
        original = b"user text\r\nwith no final newline"
        for path in self.paths.values():
            path.write_bytes(original)
        self.install("--with-defaults")
        for host, path in self.paths.items():
            block = common.render_defaults(host, common.load_routes(), "nudge").encode()
            self.assertEqual(path.read_bytes(), original + b"\n" + block)
        self.install("--with-defaults")
        self.cli("enforce")
        self.install("--uninstall")
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), original)

    def test_append_preserves_existing_newline(self):
        original = b"user text\r\n"
        for path in self.paths.values():
            path.write_bytes(original)
        self.install("--with-defaults")
        for host, path in self.paths.items():
            block = common.render_defaults(host, common.load_routes(), "nudge").encode()
            self.assertEqual(path.read_bytes(), original + block)
        self.install("--uninstall")
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), original)

    def test_auto_preserves_deleted_block(self):
        for host, path in self.paths.items():
            with self.subTest(host=host):
                self.install("--with-defaults")
                original = b"only user rules\r\n\xffno final newline"
                path.write_bytes(original)
                manifest = path.parent / "router/install-manifest.json"
                before = manifest.read_bytes()
                result = self.cli("enforce", installed=host)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(manifest.read_bytes(), before)
                notices = [line for line in result.stdout.splitlines() if "Defaults were not updated" in line]
                self.assertEqual(len(notices), 1)
                self.assertIn(str(path), notices[0])
                self.assertIn("install.sh --with-defaults", notices[0])
                self.assertEqual(result.stdout.splitlines()[-1], "enforce")
                self.install("--with-defaults")
                self.assertIn(b"router:defaults:start", path.read_bytes())

    def test_auto_preserves_deleted_file(self):
        for host, path in self.paths.items():
            with self.subTest(host=host):
                self.install("--with-defaults")
                path.unlink()
                manifest = path.parent / "router/install-manifest.json"
                before = manifest.read_bytes()
                result = self.cli("enforce", installed=host)
                self.assertFalse(path.exists())
                self.assertEqual(manifest.read_bytes(), before)
                notices = [line for line in result.stdout.splitlines() if "Defaults were not updated" in line]
                self.assertEqual(len(notices), 1)
                self.assertIn(str(path), notices[0])
                self.assertIn("install.sh --with-defaults", notices[0])
                self.assertEqual(result.stdout.splitlines()[-1], "enforce")
                self.install("--with-defaults")
                self.assertTrue(path.exists())

    def test_create_remove_and_preserve_user_additions(self):
        self.install("--with-defaults")
        for path in self.paths.values():
            self.assertTrue(path.exists())
        self.install("--uninstall")
        for path in self.paths.values():
            self.assertFalse(path.exists())
        self.install("--with-defaults")
        for path in self.paths.values():
            path.write_bytes(path.read_bytes() + b"\nnew user rule")
        self.install("--uninstall")
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), b"\nnew user rule")

    def test_no_flag_never_touches_instructions(self):
        for path in self.paths.values():
            path.write_bytes(b"<!-- router:defaults:start broken")
        self.install()
        for path in self.paths.values():
            self.assertEqual(path.read_bytes(), b"<!-- router:defaults:start broken")
            self.assertEqual(list(path.parent.glob(path.name + ".router-backup-*")), [])
        self.install("--uninstall")
        for path in self.paths.values():
            path.unlink()
        self.install()
        self.cli("enforce")
        self.install("--uninstall")
        for path in self.paths.values():
            self.assertFalse(path.exists())

    def test_marker_errors_change_nothing_across_hosts(self):
        start = b"<!-- router:defaults:start -->"
        end = b"<!-- router:defaults:end -->"
        for invalid in (start, end, end + start, start + start + end, start + end + end,
                        b"<!-- router:defaults:start broken", start + end + start + end):
            with self.subTest(invalid=invalid):
                self.paths["codex"].write_bytes(invalid)
                before = self.snapshot()
                result = self.install("--with-defaults", ok=False)
                self.assertIn("marker", result.stderr.lower())
                self.assertEqual(before, self.snapshot())

    def test_dry_run(self):
        before = self.snapshot()
        result = self.install("--with-defaults", "--dry-run")
        self.assertIn("defaults", result.stdout)
        self.assertEqual(before, self.snapshot())
        self.install("--with-defaults")
        before = self.snapshot()
        result = self.install("--uninstall", "--dry-run")
        self.assertIn("defaults", result.stdout)
        self.assertEqual(before, self.snapshot())

    def test_auto_rerenders_both_and_reinstall_preserves_ownership(self):
        self.install("--with-defaults")
        self.install()  # no-flag upgrade must retain management metadata
        for mode in ("enforce", "suggest", "off", "nudge"):
            self.cli(mode, installed="codex")
            for host, path in self.paths.items():
                self.assertEqual(path.read_text(), common.render_defaults(host, common.load_routes(), mode))
        self.install("--uninstall")
        for path in self.paths.values():
            self.assertFalse(path.exists())

    def test_bad_markers_block_auto_and_uninstall_without_changes(self):
        self.install("--with-defaults")
        for host, path in self.paths.items():
            for broken in (b"<!-- router:defaults:end -->",
                           b"<!-- router:defaults:start --><!-- router:defaults:end -->"
                           b"<!-- router:defaults:end -->"):
                with self.subTest(host=host, broken=broken):
                    original = path.read_bytes()
                    path.write_bytes(broken)
                    before = self.snapshot()
                    result = self.cli("enforce", ok=False)
                    self.assertIn(str(path), result.stderr)
                    self.assertEqual(before, self.snapshot())
                    self.install("--uninstall", ok=False)
                    self.assertEqual(before, self.snapshot())
                    path.write_bytes(original)


class DocumentationTests(unittest.TestCase):
    def test_installation_explains_opt_in_and_manual_markers(self):
        readme = (ROOT / "README.md").read_text()
        paragraph = next(line for line in readme.splitlines() if line.startswith("Review [the settings example]"))
        self.assertIn("Without `--with-defaults`", paragraph)
        self.assertIn("`CLAUDE.md` or `AGENTS.md`", paragraph)
        self.assertIn("[Delegation defaults](#delegation-defaults)", paragraph)
        self.assertIn("without the markers", paragraph)

    def test_off_row(self):
        row = next(line for line in (ROOT / "README.md").read_text().splitlines() if line.startswith("| `off` |"))
        self.assertIn("large_read", row)
        self.assertIn("return caps keep their own modes", row)

    def test_installed_skill_points_to_checkout_documentation(self):
        skill = (ROOT / "skills/dispatch/SKILL.md").read_text()
        reference = next(line for line in skill.splitlines() if "route-derived thresholds" in line)
        self.assertIn("Router checkout", reference)
        self.assertIn("README.md", reference)
        self.assertNotIn("](../../README.md", reference)

    def test_defaults_documented(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("## Delegation defaults", readme)
        self.assertIn("--with-defaults", readme)
        self.assertIn("HARNESS_DIR=/path/to/harness bash tests/together.sh", readme)
        self.assertIn("return caps for each role", readme)
        self.assertIn("Delegation defaults", (ROOT / "skills/dispatch/SKILL.md").read_text())
        added = (ROOT / "CHANGELOG.md").read_text().split("### Added", 1)[1].split("### Changed", 1)[0]
        self.assertIn("--with-defaults", added)


if __name__ == "__main__":
    unittest.main()
