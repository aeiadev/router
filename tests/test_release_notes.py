#!/usr/bin/env python3
"""Release notes extraction contract, including literal section names."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release-notes.sh"

class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.changelog = Path(self.temp.name) / "CHANGELOG.md"
        self.changelog.write_text("# Changelog\n\n## 0.3.0\n\nfirst\n\n## 0.2.0\n\nmiddle\n\n## 0.1.0\n\nlast\n\n## 0.2.*\n\nliteral\n\n")
        self.env = dict(os.environ, HOME=self.temp.name,
                        XDG_CONFIG_HOME=str(Path(self.temp.name) / "config"), ROUTER_LOCAL="off")

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *map(str, args)], env=self.env,
                              text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)

    def assert_message(self, actual, expected):
        self.assertIsNotNone(re.fullmatch(re.escape(expected), actual))

    def test_sections(self):
        for version, expected in (("0.3.0", "first\n"), ("0.2.0", "middle\n"),
                                  ("0.1.0", "last\n"), ("0.2.*", "literal\n")):
            result = self.run_script(version, self.changelog)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, expected, ""))

    def test_literal_backslashes_in_heading(self):
        for version in (r"a\tb", r"a\nb", r"a\\b", "a\\"):
            with self.subTest(version=version):
                other = "a\tb" if version == r"a\tb" else "other"
                self.changelog.write_text(
                    f"## {version}\n\nfirst\n\n## {other}\n\nsecond\n"
                )
                result = self.run_script(version, self.changelog)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "first\n", ""))

    def test_unknown_and_messages(self):
        for version in ("0.2", "missing"):
            result = self.run_script(version, self.changelog)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assert_message(result.stderr, f"release-notes: no section for {version}\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 2)
        self.assert_message(result.stderr, "usage: release-notes.sh VERSION [CHANGELOG]\n")
        missing = Path(self.temp.name) / "absent.md"
        result = self.run_script("0.2.0", missing)
        self.assertEqual(result.returncode, 1)
        self.assert_message(result.stderr, f"release-notes: {missing}: cannot read\n")

    def test_system_bash_when_it_is_older_than_four(self):
        system_bash = Path("/bin/bash")
        version = subprocess.run([str(system_bash), "-c", "printf %s \"${BASH_VERSINFO[0]}\""],
                                 stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=10)
        if version.returncode != 0 or int(version.stdout) >= 4:
            self.skipTest("/bin/bash is not older than 4")
        result = subprocess.run([str(system_bash), str(SCRIPT), "0.2.0", str(self.changelog)],
                                env=self.env, stdin=subprocess.DEVNULL, text=True,
                                capture_output=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "middle\n", ""))

    def test_real_changelog_default(self):
        result = self.run_script("0.2.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("Pairs with Harness 0.2.0.\n\n### Added\n"))
        source = (ROOT / "CHANGELOG.md").read_text().split("## 0.2.0\n", 1)[1].split("## 0.1.0\n", 1)[0]
        self.assertEqual(result.stdout, source.strip() + "\n")

    def test_pairs_with_line_leads_each_release(self):
        for version in ("0.3.0", "0.2.0"):
            with self.subTest(version=version):
                result = self.run_script(version)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("Pairs with Harness " + version + ".\n\n### "),
                                result.stdout[:80])

    def test_contributing_documents_the_github_release_step(self):
        text = (ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("## Releasing", text)
        self.assertIn('gh release create vX.Y.Z --title "Router X.Y.Z" --notes "$(scripts/release-notes.sh X.Y.Z)"', text)
        self.assertIn("same day", text)
        self.assertIn("Router first", text)


if __name__ == "__main__":
    unittest.main()
