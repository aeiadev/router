#!/usr/bin/env python3
"""Executable contracts for the 0.3.0 documentation."""
import difflib
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _platform import BASH, home_env, posix  # noqa: E402
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def retired_source_paths(root=ROOT):
    """Source files owned by Router; CI may put a Harness checkout below this root."""
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] in {".git", "harness"} or path == root / "CHANGELOG.md":
            continue
        if path.is_file() and path.suffix in (".md", ".py", ".sh"):
            yield path

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


# 0.3.1: the one platform table, every stated limit and the release pairing rule. Each line is
# matched whole, so deleting or rewording one goes red. Edit README.md or CHANGELOG.md with it.
PLATFORM_TABLE = (
    "| Platform | Claude Code | Codex CLI | Requirements |",
    "| --- | --- | --- | --- |",
    "| Linux | Hooks start with `sh` | Hooks start with `sh` | Bash 3.2+, Python 3.9+, Git |",
    "| Stock macOS | Hooks start with `sh` | Hooks start with `sh` | System Bash 3.2, `/usr/bin/python3` 3.9, Git |",
    "| Native Windows | Hooks run in Git Bash | Hooks run in PowerShell through `command_windows` | Git for Windows; Python 3.9+ as `python3`, `python` or `py -3` |",
    "| WSL | As Linux, inside WSL | As Linux, inside WSL | As Linux; separate from a native Windows install |",
)
README_LIMITS = (
    "- POSIX owner checks and permission bits such as 0600 and 0700 are not enforced on native Windows (NTFS keeps no POSIX modes), and the installer skips chmod there. Use Windows account and folder permissions to control access. File-mode statements elsewhere in this README apply to Linux, macOS and WSL.",
    "- Where the account cannot create symlinks (Windows without Developer Mode), the installer copies Router's internal links instead. The manifest records each copy by sha256, and upgrade and uninstall handle copies and links alike.",
    "- Codex runs hooks on native Windows in PowerShell through `command_windows`, with the Python that ran `install.sh`. If that Python is removed, the hook prints `router: Python missing; rerun install.sh` and exits 1; rerun `install.sh` after moving or replacing Python.",
    "- The Microsoft Store `python3` stub never counts as Python. With no real Python, Router prints the missing-Python line above instead of failing silently.",
    "- Use Windows-valid file names without control characters; names that Windows cannot create are outside native Windows support.",
    "- Keep installation, state and backup paths below the usual 260-character MAX_PATH limit, including the files created beneath them; longer paths are outside native Windows support.",
    "- Native Windows support starts with 0.3.1. Upgrade both Router and Harness to 0.3.1 before uninstalling either: a 0.2 uninstaller predates Windows support and can misread the current sibling's manifest keys.",
    "- Upgrading alongside Harness 0.2 on native Windows is not supported: Harness 0.2 claims no shared roles there, so Router refreshes them without the older-sibling notice.",
    "`ROUTER_STATE` or `XDG_STATE_HOME` written as `~user/...` needs `ROUTER_PYTHON` or a Python 3.9 or newer on PATH to resolve that account. On native Windows, a `~/` path whose rest starts with another separator or a drive, such as `~//tmp` or `~/C:state`, may need one too. If the saved interpreter is reachable only through such a path and no Python is found, the missing-Python line applies. Absolute paths, drive and UNC paths, and ordinary `~/` paths start from the saved interpreter.",
    "Router picks the pressure host from `harness_host` first, then an explicit `--host` argument, and only then treats an event that carries `turn_id` as Codex; live Codex visibility of pressure nudges remains unverified.",
    "Native Windows test runs skip POSIX owner and mode assertions and chmod-based unreadable-file fixtures; checks of file contents, copy lifecycles and injected errors still run. Where the account cannot create symlinks, symlink assertions are skipped and the copy fallback stays tested. FIFO fixtures need `os.mkfifo`, which Windows Python lacks. The system-Bash release-notes check runs only when `/bin/bash` is older than 4. The Windows simulation self-check runs only under `PYTHONPATH=tests/win_sim`. Tests that start the 0.1 or 0.2 installers need Python 3.10 or newer (a stock Mac has 3.9) and skip only those cases when none is on PATH; on native Windows, the cases where an old 0.2 uninstaller runs first are skipped.",
)
PAIRING_LINE = ("Pairs with Harness 0.3.1. If the installed Harness is older, the installer prints one "
                "upgrade notice; upgrade both tools.")
CHANGELOG_LIMITS = (
    "- Not enforced on native Windows: POSIX owner checks and file modes such as 0600 (NTFS keeps none); the installer skips chmod there.",
    "- Codex hooks on native Windows run the Python that ran `install.sh`; after moving or replacing it, rerun `install.sh`.",
    "- Control-character file names and paths beyond the 260-character MAX_PATH are outside native Windows support.",
    "- State paths written as `~user/...`, and on native Windows a `~/` path such as `~//tmp` or `~/C:state`, may need `ROUTER_PYTHON` or a Python on PATH; they cannot start from the saved interpreter alone.",
    "- On native Windows, upgrade both tools to 0.3.1 before uninstalling: a 0.2 uninstaller can misread current manifest keys. Upgrading alongside Harness 0.2 there is not supported.",
)


import _released_python

RELEASED_PYTHON = []  # one shim folder whose python3 is verified as 3.10 or newer


def released_env(env):
    return _released_python.released_env(env, RELEASED_PYTHON)


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        self.env = {**os.environ, **home_env(home), "XDG_CONFIG_HOME": str(home / "config"),
                    "XDG_STATE_HOME": str(home / "state"), "CLAUDE_HOME": str(home / "claude"),
                    "CODEX_HOME": str(home / "codex"),
                    "ROUTER_LOCAL": str(home / "config/router/routes.local.json"),
                    "PYTHONDONTWRITEBYTECODE": "1", "PATH": str(ROOT / "bin") + os.pathsep + os.environ["PATH"]}
        self.readme = README.read_text()

    def run_cmd(self, command, cwd=ROOT, env=None):
        return subprocess.run(command, cwd=cwd, env=env or self.env, text=True, capture_output=True, timeout=60)

    def test_result_first_and_demo(self):
        body = self.readme.split("# Router\n", 1)[1].lstrip()
        first = body.split("\n\n", 1)[0]
        self.assertRegex(first, r"(?s)cheaper models.*stronger.*judged.*brief.*compaction")
        self.assertNotRegex(first.lower(), r"hook|sqlite|install\.sh")
        self.assertRegex(self.readme, r"(?s)## Demo\n.*brief: BAR missing \(builder needs TASK, FILES, BAR, RETURN in that order\).*router lanes.*router report.*router doctor")
        overlay = Path(self.env["ROUTER_LOCAL"]); overlay.parent.mkdir(parents=True); overlay.write_text("{}")
        installed = self.run_cmd([BASH, posix(ROOT / "install.sh"), "--host", "both"])
        self.assertEqual(installed.returncode, 0, installed.stderr)
        outputs = []
        for match in re.finditer(r"(?m)^```bash\n(.*?)^```", self.readme, re.S | re.M):
            preceding = self.readme[:match.start()].splitlines()[-1:]
            if preceding == ["<!-- docs:skip -->"]:
                continue
            commands = [line for line in match.group(1).splitlines() if line and not line.startswith("#")]
            for command in commands:
                result = self.run_cmd([BASH, "-c", command])
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
        for path in retired_source_paths():
            if path != Path(__file__):
                if "dispatch" + "-log" in path.read_text(errors="replace"):
                    self.fail(f"retired helper mentioned in {path}")
        result = self.run_cmd([BASH, posix(ROOT / "install.sh"), "--host", "claude"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.tmp.name) / ("claude/router/bin/dispatch" + "-log.py")).exists())
        old = os.environ.get("ROUTER_V02")
        self.assertIsNotNone(old, "ROUTER_V02 must be set from old-trees.sh")
        result = self.run_cmd([BASH, posix(Path(old) / "install.sh"), "--host", "codex"], env=released_env(self.env))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cmd([BASH, posix(ROOT / "install.sh"), "--host", "codex"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((Path(self.tmp.name) / ("codex/router/bin/dispatch" + "-log.py")).exists())

    def test_retired_source_scan_excludes_nested_checkout_but_covers_owned_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owned = root / "hooks/router/active.py"
            sibling = root / "harness/CHANGELOG.md"
            owned.parent.mkdir(parents=True)
            sibling.parent.mkdir(parents=True)
            owned.write_text("retired helper", encoding="utf-8")
            sibling.write_text("old release note", encoding="utf-8")
            self.assertEqual(list(retired_source_paths(root)), [owned])

    def test_platform_table_requirements_and_limits(self):
        lines = self.readme.splitlines()
        starts = [n for n, line in enumerate(lines) if line.startswith("| Platform |")]
        self.assertEqual(len(starts), 1, "one platform table")
        self.assertEqual(tuple(lines[starts[0]:starts[0] + len(PLATFORM_TABLE) + 1]), PLATFORM_TABLE + ("",))
        requirements = [line for line in lines if line.startswith("Requirements:")]
        self.assertEqual(len(requirements), 1, requirements)
        for phrase in ("Bash 3.2 or newer", "Python 3.9 or newer", "Git for Windows", "Git Bash"):
            self.assertIn(phrase, requirements[0])
        for line in README_LIMITS:
            self.assertEqual(lines.count(line), 1, "README limit line missing or reworded: " + line[:70])
        self.assertNotIn("Unix-like environment", self.readme)
        self.assertNotIn("GNU coreutils", self.readme)
        # The floor and the messages the README quotes are the ones the code prints.
        installer = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("if sys.version_info < (3, 9):", installer)
        self.assertIn("printf 'router install: missing requirement: %s\\n' \"$requirement\"", installer)
        self.assertIn("router install: missing requirement: python3", self.readme)
        self.assertIn("[Console]::Error.WriteLine('router: Python missing; rerun install.sh')", installer)
        launcher = (ROOT / "hooks/router/pyrun.sh").read_text(encoding="utf-8")
        template = re.search(r'(?m)^  message="(.*)"$', launcher).group(1)
        message = template.replace("$tool", "router").replace("$name", "ROUTER_PYTHON")
        self.assertIn("```text\n" + message + "\n```", self.readme)
        self.assertIn("*/WindowsApps/*", launcher)
        self.assertIn('for candidate in python3 python \'py -3\'; do', launcher)

    def test_changelog_031_groups_pairing_and_limits(self):
        text = CHANGELOG.read_text()
        version = (ROOT / "VERSION").read_text().strip()
        self.assertEqual(version, "0.4.0")
        section = text.split("## 0.3.1\n", 1)[1].split("\n## 0.3.0\n", 1)[0]
        self.assertEqual(section.strip().split("\n\n", 1)[0], PAIRING_LINE)
        self.assertEqual(re.findall(r"(?m)^### (.+)$", section), ["Fixed", "Platforms", "Changed"])
        bullets = re.findall(r"(?m)^- .*$", section)
        self.assertEqual(len(bullets), len(set(bullets)), "duplicate bullets")
        self.assertTrue(all(line == "" or line.startswith(("- ", "### ", "Pairs with ")) for line in section.strip().splitlines()))
        self.assertNotRegex(section, r"\b\d{2}[A-Z](?:-[a-z])?\b|\bWave [A-Z]\b|\b[Ll]ane [A-Z0-9]")
        platforms = section.split("### Platforms\n", 1)[1].split("\n### ", 1)[0]
        for line in CHANGELOG_LIMITS:
            self.assertEqual(platforms.splitlines().count(line), 1, "CHANGELOG limit missing or reworded: " + line[:70])

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
