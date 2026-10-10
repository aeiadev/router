#!/usr/bin/env python3
"""Executable contracts for the 0.3.0 documentation."""
import difflib
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"

# The text Harness copies byte for byte. Edit it here and in README.md together.
SHARED_BLOCK = r'''
### Install both

[Router](https://github.com/aeiadev/router) routes bounded work to model tiers, checks Claude briefs before a run, and tracks lanes and verdicts. [Harness](https://github.com/aeiadev/harness) guards commands and restores project context. Together, they share roles and add a context pressure signal; each prints its own restore content, so they never write the same line twice.

Clone and install each checkout in either order:

<!-- docs:skip -->
```bash
git clone https://github.com/aeiadev/router.git
cd router && bash install.sh --host both
```

<!-- docs:skip -->
```bash
git clone https://github.com/aeiadev/harness.git
cd harness && bash install.sh --host both
```

For Codex, start a new session and review and trust the hooks. Changed scripts can prompt another trust review.

### Upgrade

Run `install.sh` from the new checkout of each tool. An unchanged old file that is no longer shipped is removed and listed; an edited one is kept and listed. `--dry-run` previews changes. Shared roles that the sibling already installed are claimed by this tool, never rewritten. If the sibling is older, the installer prints one notice; upgrade both to get the new shared roles. A 0.2 installer run after a 0.3 sibling still refuses and leaves files unchanged, so upgrade both.

Uninstall restores the original settings bytes and removes only files the tool created. Backups stay until `--purge`: alone, it lists and removes that tool's own backups; Router also removes its retired attempts.sqlite3. With `--uninstall`, it uninstalls then purges; with `--dry-run`, it lists only. Harness `install.sh --status` and `router doctor` show installed versions and skew; Router `install.sh --status` reports Claude Code plugin state.

From 0.1 or 0.2: those releases did not record which backup held your settings. Uninstall restores the oldest Router or Harness backup only when it holds exactly the settings left once both tools are removed; otherwise it writes that content in standard JSON layout, or removes the file when no backup is left and nothing else remains. If Harness 0.1 or 0.2 went first, the file stays 0600.

### Claude Code plugin route

In Claude Code, `/plugin marketplace add aeiadev/router`, then `/plugin install router@router` and `/plugin install shared-roles@router`. The settings form is `{"enabledPlugins": {"router@router": true}}`. Plugin agents use names such as `router:builder-std`. A script install wins over the plugin: plugin hooks stand down when the install manifest exists. Codex has no plugin; it is not shipped there.
'''


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_STATE_HOME": str(home / "state"), "CLAUDE_HOME": str(home / "claude"),
                    "CODEX_HOME": str(home / "codex"),
                    "ROUTER_LOCAL": str(home / "config/router/routes.local.json"),
                    "PYTHONDONTWRITEBYTECODE": "1", "PATH": str(ROOT / "bin") + os.pathsep + os.environ["PATH"]}
        self.readme = README.read_text()

    def run_cmd(self, command, cwd=ROOT):
        return subprocess.run(command, cwd=cwd, env=self.env, text=True, capture_output=True, timeout=60)

    def test_result_first_and_demo(self):
        body = self.readme.split("# Router\n", 1)[1].lstrip()
        first = body.split("\n\n", 1)[0]
        self.assertRegex(first, r"(?s)cheaper models.*stronger.*judged.*brief.*compaction")
        self.assertNotRegex(first.lower(), r"hook|sqlite|install\.sh")
        self.assertRegex(self.readme, r"(?s)## Demo\n.*brief: BAR missing \(builder needs TASK, FILES, BAR, RETURN in that order\).*router lanes.*router report.*router doctor")
        overlay = Path(self.env["ROUTER_LOCAL"]); overlay.parent.mkdir(parents=True); overlay.write_text("{}")
        installed = self.run_cmd(["bash", str(ROOT / "install.sh"), "--host", "both"])
        self.assertEqual(installed.returncode, 0, installed.stderr)
        outputs = []
        for match in re.finditer(r"(?m)^```bash\n(.*?)^```", self.readme, re.S | re.M):
            preceding = self.readme[:match.start()].splitlines()[-1:]
            if preceding == ["<!-- docs:skip -->"]:
                continue
            commands = [line for line in match.group(1).splitlines() if line and not line.startswith("#")]
            for command in commands:
                result = self.run_cmd(["bash", "-c", command])
                self.assertEqual(result.returncode, 0, (command, result.stderr))
                outputs.append(result.stdout)
        for line in ("no lanes", "codex lanes: 0 (no verdicts on Codex)", "[ok] spawn: dry run allowed a sweeper brief"):
            self.assertRegex(self.readme, re.compile(r"(?m)^" + re.escape(line) + r"$"))
            self.assertTrue(any(re.fullmatch(re.escape(line), actual, re.M) for actual in "\n".join(outputs).splitlines()), line)

    def test_shared_block_and_links(self):
        self.assertEqual(self.readme.count("<!-- shared:begin -->"), 1)
        self.assertEqual(self.readme.count("<!-- shared:end -->"), 1)
        block = self.readme.split("<!-- shared:begin -->", 1)[1].split("<!-- shared:end -->", 1)[0]
        for phrase in ("Install both", "Upgrade", "https://github.com/aeiadev/router", "https://github.com/aeiadev/harness", "--host both", "--dry-run", "--purge", "--uninstall", "install.sh --status", "router doctor", "/plugin marketplace add aeiadev/router", "/plugin install router@router", "/plugin install shared-roles@router", "{\"enabledPlugins\": {\"router@router\": true}}", "router:builder-std", "Codex has no plugin", "From 0.1 or 0.2: those releases did not record which backup held your settings. Uninstall restores the oldest Router or Harness backup only when it holds exactly the settings left once both tools are removed; otherwise it writes that content in standard JSON layout, or removes the file when no backup is left and nothing else remains. If Harness 0.1 or 0.2 went first, the file stays 0600."):
            self.assertIn(phrase, block)
        self.assertIn("Backups stay until `--purge`", block)
        self.assertNotIn(chr(0x2014), block)
        for target in re.findall(r"\]\(([^)]+)\)", block):
            self.assertRegex(target, r"^https://")
        headings = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in re.findall(r"(?m)^#{1,6} (.+)$", self.readme)}
        for target in re.findall(r"\]\(([^)]+)\)", self.readme):
            if target.startswith("https://"):
                continue
            if target.startswith("#"):
                self.assertIn(target[1:], headings)
            else:
                path, _, anchor = target.partition("#")
                self.assertTrue((ROOT / path).exists(), target)
                if anchor:
                    self.assertIn(anchor, headings)
        if block != SHARED_BLOCK:
            diff = "\n".join(difflib.unified_diff(SHARED_BLOCK.splitlines(), block.splitlines(), "pinned", "README", lineterm=""))
            self.fail("shared block differs from the pinned text:\n" + diff)
        print("shared block sha256:", hashlib.sha256(block.encode()).hexdigest())

    def test_commands_and_codex_limits(self):
        commands = {p.stem[4:] for p in (ROOT / "hooks/router").glob("cmd_*.py")}
        command_table = self.readme.split("## 0.3 commands", 1)[1].split("\n## ", 1)[0].split("\n\nThe local override", 1)[0]
        for name in commands:
            self.assertRegex(command_table, re.compile(r"(?m)^\| `router " + name + r"(?: |`|\|).*$"), name)
        for name in re.findall(r"`router ([a-z]+)(?: |`|\|)", self.readme):
            self.assertIn(name, commands | {"on", "off", "status", "auto"})
        table = self.readme.split("## What each host enforces", 1)[1].split("\n## ", 1)[0]
        for label, limit in (("C1 spawn check", "not possible"), ("C3 lanes", "partial"), ("C3 verdicts, reminder, re-run block", "not possible"), ("C4 Router note", "partial"), ("Resume cache warning", "not possible"), ("B2 window reminders", "partial"), ("C5 pressure nudges", "partial"), ("run_in_background exemption", "n/a"), ("router report", "partial"), ("Cache-hit %", "not possible")):
            self.assertRegex(table, re.compile(r"(?m)^\| " + re.escape(label) + r" \|[^\n]*" + re.escape(limit) + r"[^\n]*\|$"))
        for section, phrase in (("## Ladder", "archived"), ("## Configuration", "ROUTER_LOCAL"), ("## 0.3 commands", "symlink"), ("## What each host enforces", "unverified")):
            self.assertIn(phrase, self.readme.split(section, 1)[1].split("\n## ", 1)[0])
        self.assertIn("turn_id", self.readme)

    def test_table_rows_have_header_cell_count(self):
        def cells(row):
            return len(re.split(r"(?<!\\)\|", row.strip())) - 2
        lines = self.readme.splitlines()
        header = None
        for number, line in enumerate(lines, 1):
            if not line.startswith("|"):
                header = None
            elif header is None:
                header = (cells(line), number)
            else:
                self.assertEqual(cells(line), header[0], f"README.md:{number} {line}")

    def test_every_long_option_is_documented_in_its_own_row(self):
        # Each subcommand's long options come from its own cmd_<name>.py and must appear in that
        # subcommand's row of the "0.3 commands" table, not merely somewhere in README.md.
        table = self.readme.split("## 0.3 commands", 1)[1].split("\n## ", 1)[0].split("\n\nThe local override", 1)[0]
        with_options = set()
        for path in sorted((ROOT / "hooks/router").glob("cmd_*.py")):
            source = path.read_text()
            options = set(re.findall(r"add_argument\(\s*[\"'](--[a-z][a-z-]*)", source))
            options |= set(re.findall(r"arg == [\"'](--[a-z][a-z-]*)", source))
            name = path.stem[4:]
            rows = re.findall(r"(?m)^\| `router " + re.escape(name) + r"(?: |`|\|).*\|$", table)
            self.assertEqual(len(rows), 1, (name, rows))
            for option in sorted(options):
                self.assertRegex(rows[0], re.compile(r"(?<![\w-])" + re.escape(option) + r"(?![\w-])"),
                                 f"router {name} row lacks {option}")
            if options:
                with_options.add(name)
        self.assertLessEqual({"doctor", "lanes", "promote", "report"}, with_options)

    def test_dispatch_log_retired(self):
        self.assertFalse((ROOT / ("scripts/dispatch" + "-log.py")).exists())
        for path in ROOT.rglob("*"):
            if path.is_file() and ".git" not in path.parts and path != CHANGELOG and path != Path(__file__):
                if path.suffix in (".md", ".py", ".sh"):
                    self.assertNotIn("dispatch" + "-log", path.read_text(errors="replace"), str(path))
        result = self.run_cmd(["bash", str(ROOT / "install.sh"), "--host", "claude"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.tmp.name) / ("claude/router/bin/dispatch" + "-log.py")).exists())
        old = os.environ.get("ROUTER_V02")
        self.assertIsNotNone(old, "ROUTER_V02 must be set from old-trees.sh")
        result = self.run_cmd(["bash", str(Path(old) / "install.sh"), "--host", "codex"])
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cmd(["bash", str(ROOT / "install.sh"), "--host", "codex"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.tmp.name) / ("codex/router/bin/dispatch" + "-log.py")).exists())

    def test_changelog(self):
        text = CHANGELOG.read_text()
        versions = re.findall(r"(?m)^## (\d+\.\d+\.\d+)$", text)
        self.assertEqual(versions[0], (ROOT / "VERSION").read_text().strip())
        part = text.split("## 0.3.0", 1)[1].split("## 0.2.0", 1)[0]
        self.assertEqual(re.findall(r"(?m)^### (.+)$", part), ["Added", "Changed", "Fixed", "Removed", "Upgrade notes"])
        for name in re.findall(r"`router ([a-z]+)(?: |`|\|)", part):
            self.assertIn(name, {p.stem[4:] for p in (ROOT / "hooks/router").glob("cmd_*.py")} | {"on", "off", "status", "auto"})
        self.assertTrue(all(len(line) <= 400 for line in text.splitlines()))
        section02 = text.split("## 0.2.0", 1)[1].split("## 0.1.0", 1)[0]
        self.assertIn("Replace Router's own hook registrations when a reinstall changes their matcher", section02)
        for line in ("- Record `asked_type` alongside the routed role so the requested and actual seat remain visible.",
                     "- Drop the Explore, Plan and general-purpose built-in agent overrides; those names become aliases of `researcher`, `planner` and `worker`.",
                     "- Count builder, worker, test-writer and docs-writer seats as execution rounds in dispatch" + "-log.py."):
            self.assertRegex(section02, re.compile(r"(?m)^" + re.escape(line) + r"$"))
        self.assertNotIn("remove dropped overrides", section02)
        # 0.3.0 names the retired helper only in its Removed item; README is covered by test_dispatch_log_retired.
        self.assertEqual([line for line in part.splitlines() if "dispatch" + "-log" in line],
                         ["- Remove dispatch" + "-log.py; use the ledger, `router lanes`, and `router report` for run outcomes."])
        for phrase in ("spool", "project root", "bytecode", "checkout", "no-change", "Leave settings untouched"):
            self.assertIn(phrase, part)
        self.assertNotIn(chr(0x2014), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
