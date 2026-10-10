#!/usr/bin/env python3
"""C8 token budget: scripts/budget.py counts, ceilings, body limits, trims, doubled descriptions."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "scripts/budget.py"
IS_ROUTER = (ROOT / "bin/router").is_file()
LABEL = "router" if IS_ROUTER else "harness"
CEILING = 650 if IS_ROUTER else 600
SPARE_MAX = 500 if IS_ROUTER else 560
TIMEOUT = 30
NINE = ("builder", "builder-in-place", "test-writer", "docs-writer", "worker", "judge",
        "sweeper", "researcher", "planner")
SIZES = {"builder-in-place": 2141, "builder": 2101, "docs-writer": 1717, "judge": 2884,
         "planner": 1674, "researcher": 1682, "sweeper": 1603, "test-writer": 1785, "worker": 1737}
FIRST = "budget (estimate: bytes/4, no tokenizer)"


def run(*args, env=None, script=BUDGET):
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=TIMEOUT, env=env)


def agent(name, description, body="Body.\n", extra=""):
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}"


def make_home(path, agents=None, skills=None, host="claude"):
    path = Path(path)
    (path / "agents").mkdir(parents=True, exist_ok=True)
    for name, text in (agents or {}).items():
        if host == "codex":
            (path / f"agents/{name}.toml").write_text(text)
        else:
            (path / f"agents/{name}.md").write_text(text)
    for name, text in (skills or {}).items():
        (path / f"skills/{name}").mkdir(parents=True, exist_ok=True)
        (path / f"skills/{name}/SKILL.md").write_text(text)
    return path


def make_checkout(path):
    """A temp copy of this repo's measured parts, with the label marker."""
    path = Path(path)
    for folder in ("agents", "skills", "codex", "scripts"):
        shutil.copytree(ROOT / folder, path / folder)
    if IS_ROUTER:
        (path / "bin").mkdir()
        (path / "bin/router").write_text("")
    else:
        (path / "hooks/checkpoint").mkdir(parents=True)
    return path


def pad_description(text, pad):
    return re.sub(r"^(description: .*)$", lambda m: m[1] + "x" * pad, text, count=1, flags=re.MULTILINE)


def tokens_of(path, *extra):
    result = run("--json", "--home", str(path), *extra)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def skill_text(name, description):
    return f"---\nname: {name}\ndescription: {description}\n---\nBody.\n"


class Counting(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)

    def test_five_bytes_is_two_tokens(self):
        home = make_home(self.tmp / "h", {"ab": agent("ab", "cde")})
        result = run("--home", str(home))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], FIRST)
        self.assertEqual(lines[1], "tree: 5 B always-on = 2 tokens (agents 5 B, skills 0 B)")
        data = json.loads(run("--json", "--home", str(home)).stdout)
        self.assertEqual(data["trees"][0]["bytes"], 5)
        self.assertEqual(data["trees"][0]["tokens"], 2)

    def test_agents_and_skills_both_count(self):
        home = make_home(self.tmp / "h", {"ab": agent("ab", "cde")}, {"sk": skill_text("sk", "wxyz")})
        lines = run("--home", str(home)).stdout.splitlines()
        self.assertEqual(lines[1], "tree: 11 B always-on = 3 tokens (agents 5 B, skills 6 B)")

    def test_continuation_lines_and_utf8(self):
        text = "---\nname: ab\ndescription: cd\n  eé\n---\nBody.\n"
        home = make_home(self.tmp / "h", {"ab": text})
        data = tokens_of(home)
        self.assertEqual(data["trees"][0]["bytes"], 2 + len("cd eé".encode()))

    def test_codex_layout(self):
        toml = '# Generated\nname = "ab"\ndescription = "cde"\ndeveloper_instructions = "name = \\"zz\\""\n'
        home = make_home(self.tmp / "h", {"ab": toml}, host="codex")
        (home / "agents/stray.md").write_text(agent("stray", "not counted"))
        result = run("--home", str(home), "--host", "codex")
        self.assertEqual(result.stdout.splitlines()[1], "tree: 5 B always-on = 2 tokens (agents 5 B, skills 0 B)")

    def test_checkout_default_measures_script_parent(self):
        checkout = make_checkout(self.tmp / "c")
        data = json.loads(run("--json", script=checkout / "scripts/budget.py").stdout)
        self.assertEqual(data["trees"][0]["label"], LABEL)
        codex = json.loads(run("--json", "--host", "codex", script=checkout / "scripts/budget.py").stdout)
        self.assertNotEqual(codex["trees"][0]["bytes"], 0)
        expect = sum(len(p.stem.encode()) for p in (checkout / "agents").glob("*.md"))
        self.assertGreater(data["trees"][0]["bytes"], expect)

    def test_with_counts_identical_roles_once(self):
        first = make_home(self.tmp / "a", {r: agent(r, "Use " + r) for r in NINE})
        second = make_home(self.tmp / "b", {r: agent(r, "Use " + r) for r in NINE},
                           {"sk": skill_text("sk", "wxyz")})
        data = json.loads(run("--json", "--home", str(first), "--with", str(second)).stdout)
        one = data["trees"][0]["bytes"]
        self.assertEqual(data["trees"][1]["bytes"], one + 6)
        self.assertEqual(data["union"]["bytes"], one + 6)
        self.assertEqual(data["flags"], [])
        text = run("--home", str(first), "--with", str(second)).stdout.splitlines()
        self.assertEqual(len([line for line in text if "always-on =" in line]), 3)
        self.assertRegex(text[3], r"^together: \d+ B always-on = \d+ tokens \(agents \d+ B, skills \d+ B\)$")

    def test_json_keys_and_fixed_lines(self):
        home = make_home(self.tmp / "h", {"ab": agent("ab", "cde")})
        data = json.loads(run("--json", "--home", str(home)).stdout)
        self.assertEqual(sorted(data), ["bodies", "estimate", "flags", "problems", "trees", "union"])
        self.assertIs(data["estimate"], True)
        text = run("--home", str(home)).stdout.splitlines()
        self.assertIn("hooks: 0 B always-on (default config)", text)
        self.assertEqual(text.count("injection caps: checked by tests/together.sh"), 1)

    def test_pin_matches_shared_sha256(self):
        rows = (ROOT / "agents/SHARED.sha256").read_text().splitlines()
        self.assertEqual(len(rows), 19)
        digest, name = rows[18].split()
        self.assertEqual(name, "../scripts/budget.py")
        self.assertEqual(hashlib.sha256(BUDGET.read_bytes()).hexdigest(), digest)
        copy = self.tmp / "pin"
        (copy / "agents").mkdir(parents=True)
        (copy / "scripts").mkdir()
        shutil.copy(BUDGET, copy / "scripts/budget.py")
        (copy / "agents/SHARED.sha256").write_text(rows[18] + "\n")
        check = lambda: subprocess.run(["sha256sum", "-c", "SHARED.sha256"], cwd=copy / "agents",
                                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT)
        self.assertEqual(check().returncode, 0)
        (copy / "scripts/budget.py").write_text(BUDGET.read_text() + "# drift\n")
        self.assertNotEqual(check().returncode, 0)


class Ceilings(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)

    def check(self, checkout, *extra):
        return run("--check", *extra, script=checkout / "scripts/budget.py")

    def test_shipped_tree_is_clean(self):
        result = run("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_over_the_ceiling(self):
        checkout = make_checkout(self.tmp / "c")
        data = json.loads(run("--json", script=checkout / "scripts/budget.py").stdout)
        pad = CEILING * 4 - data["trees"][0]["bytes"] + 8
        path = checkout / "agents/worker.md"
        path.write_text(pad_description(path.read_text(), pad))
        result = self.check(checkout)
        self.assertEqual(result.returncode, 1)
        want = math.ceil((data["trees"][0]["bytes"] + pad) / 4)
        self.assertRegex(result.stderr, rf"\Abudget: {LABEL} always-on {want} tokens is over the ceiling {CEILING}\n\Z")

    def test_body_over_limit(self):
        checkout = make_checkout(self.tmp / "c")
        path = checkout / "agents/builder.md"
        path.write_text(path.read_text() + "x" * (SIZES["builder"] * 6 // 100 + 1))
        result = self.check(checkout)
        size = SIZES["builder"] + SIZES["builder"] * 6 // 100 + 1
        limit = SIZES["builder"] * 105 // 100
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, rf"\Abudget: body builder is {size} B, limit {limit} B\n\Z")

    def test_codex_word_outside_tomls(self):
        checkout = make_checkout(self.tmp / "c")
        path = checkout / "agents/builder.md"
        path.write_text(path.read_text().replace("Job", "Codex Job", 1))
        result = self.check(checkout)
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"\Abudget: agents/builder\.md mentions Codex outside the TOMLs\n\Z")

    def test_union_over_the_together_ceiling(self):
        checkout = make_checkout(self.tmp / "c")
        sibling = make_home(self.tmp / "s", {"bigone": agent("bigone", "x" * 3000)})
        base = json.loads(run("--json", "--with", str(sibling), script=checkout / "scripts/budget.py").stdout)
        tokens = base["union"]["tokens"]
        self.assertGreater(tokens, 900)
        result = self.check(checkout, "--with", str(sibling))
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, rf"\Abudget: together always-on {tokens} tokens is over the ceiling 900\n\Z")


class Trims(unittest.TestCase):
    def test_skill_descriptions_are_short_and_keep_triggers(self):
        paths = sorted((ROOT / "skills").glob("*/SKILL.md"))
        self.assertTrue(paths)
        for path in paths:
            description = re.search(r"^description: (.*)$", path.read_text(), re.MULTILINE)[1]
            with self.subTest(skill=path.parent.name):
                self.assertLessEqual(len(description.encode()), 170)
                self.assertGreater(len(description.split()), 8)
                self.assertRegex(description, r"\b(Use|Load)\b")

    TRIGGERS = {"dispatch": ("bounded parallel work", "evidence sweeps", "independent verification")}

    def test_trimmed_descriptions_keep_every_base_trigger_item(self):
        for skill, items in self.TRIGGERS.items():
            description = re.search(r"^description: (.*)$", (ROOT / "skills" / skill / "SKILL.md").read_text(),
                                    re.MULTILINE)[1]
            for item in items:
                with self.subTest(skill=skill, item=item):
                    self.assertRegex(description, re.compile(r"\b" + re.escape(item) + r"\b", re.IGNORECASE))

    def test_builder_in_place_phrase_restored(self):
        phrase = "when the caller already selected the checkout"
        self.assertIn(phrase, (ROOT / "agents/builder-in-place.md").read_text())
        self.assertIn(phrase, (ROOT / "codex/agents/builder-in-place.toml").read_text())

    def test_nine_role_sum_and_whole_home_spare(self):
        total = 0
        for role in NINE:
            text = (ROOT / f"agents/{role}.md").read_text()
            total += len(role.encode()) + len(re.search(r"^description: (.*)$", text, re.MULTILINE)[1].encode())
        self.assertLessEqual(total, 1300)
        data = json.loads(run("--json").stdout)
        self.assertLessEqual(data["trees"][0]["tokens"], SPARE_MAX)
        self.assertLessEqual(data["trees"][0]["tokens"], CEILING - 40)


class Doubled(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)

    def test_copied_agent_is_doubled(self):
        home = make_home(self.tmp / "h", {"builder": agent("builder", "Use to build.")})
        shutil.copy(home / "agents/builder.md", home / "agents/builder2.md")
        result = run("--home", str(home))
        self.assertIn("flag: doubled description: builder and builder2", result.stdout.splitlines())
        self.assertEqual(json.loads(run("--json", "--home", str(home)).stdout)["flags"],
                         ["doubled description: builder and builder2"])
        result = run("--check", "--home", str(home))
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"\Abudget: doubled description: builder and builder2\n\Z")

    def test_skill_and_agent_sharing_a_description(self):
        home = make_home(self.tmp / "h", {"alpha": agent("alpha", "Use to do things.")},
                         {"beta": skill_text("beta", "Use to do things.")})
        result = run("--check", "--home", str(home))
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"\Abudget: doubled description: alpha and beta\n\Z")

    def test_tier_files_share_descriptions_by_design(self):
        home = make_home(self.tmp / "h", {"builder-light": agent("builder-light", "Tier of builder"),
                                          "builder-up": agent("builder-up", "Tier of builder")})
        result = run("--check", "--home", str(home))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("flag:", result.stdout)

    def test_sibling_with_changed_role_differs(self):
        first = make_home(self.tmp / "a", {"judge": agent("judge", "Use to judge.")})
        second = make_home(self.tmp / "b", {"judge": agent("judge", "Use to judge.", "Body.\nx")})
        self.assertIn("flag: judge differs between trees",
                      run("--home", str(first), "--with", str(second)).stdout.splitlines())
        result = run("--check", "--home", str(first), "--with", str(second))
        self.assertEqual(result.returncode, 1)
        self.assertRegex(result.stderr, r"\Abudget: judge differs between trees\n\Z")

    def test_identical_roles_have_no_flag(self):
        first = make_home(self.tmp / "a", {"judge": agent("judge", "Use to judge.")})
        second = make_home(self.tmp / "b", {"judge": agent("judge", "Use to judge.")})
        result = run("--check", "--home", str(first), "--with", str(second))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("flag:", result.stdout)

    def test_shipped_tree_has_no_flags(self):
        self.assertEqual(json.loads(run("--json").stdout)["flags"], [])


# --- router status tests (Router only; the Harness copy of this file ends above) ---
class StatusLine(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.claude = self.tmp / "claude"
        self.codex = self.tmp / "codex"

    def install(self, home, host="claude", harness=False):
        make_home(home)
        if host == "claude":
            for path in (ROOT / "agents").glob("*.md"):
                shutil.copy(path, home / "agents" / path.name)
        else:
            for path in (ROOT / "codex/agents").glob("*.toml"):
                shutil.copy(path, home / "agents" / path.name)
        shutil.copytree(ROOT / "skills", home / "skills", dirs_exist_ok=True)
        (home / "router").mkdir(parents=True, exist_ok=True)
        (home / "router/install-manifest.json").write_text("{}")
        if harness:
            (home / "harness").mkdir()
            (home / "harness/install-manifest.json").write_text("{}")
        return home

    def status(self, root=ROOT, outer_timeout=TIMEOUT, **env):
        merged = dict(os.environ, CLAUDE_HOME=str(self.claude), CODEX_HOME=str(self.codex),
                      HOME=str(self.tmp / "nohome"), XDG_CONFIG_HOME=str(self.tmp / "config"), ROUTER_LOCAL="off")
        merged.update(env)
        return subprocess.run([sys.executable, str(root / "bin/router"), "status"], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=outer_timeout, env=merged)

    def measured(self, home, host="claude", ceiling=650):
        data = tokens_of(home, "--host", host)
        tree = data["trees"][0]
        return tree

    def test_within_ceiling(self):
        self.install(self.claude)
        result = self.status()
        self.assertEqual(result.returncode, 0, result.stderr)
        tree = tokens_of(self.claude)["trees"][0]
        self.assertEqual(tree["label"], "router")
        line = (f"budget claude: ~{tree['tokens']} tokens always-on ({tree['bytes']} B names and "
                f"descriptions, estimate; ceiling 650)")
        self.assertIn(line, result.stdout.splitlines())
        self.assertEqual(result.stdout.splitlines()[0], "on")

    def test_over_ceiling(self):
        self.install(self.claude)
        pad = 700 * 4
        path = self.claude / "agents/worker.md"
        path.write_text(pad_description(path.read_text(), pad))
        tree = tokens_of(self.claude)["trees"][0]
        result = self.status()
        line = (f"budget claude: ~{tree['tokens']} tokens always-on ({tree['bytes']} B names and "
                f"descriptions, estimate; ceiling 650) OVER")
        self.assertIn(line, result.stdout.splitlines())

    def test_doubled_description_line(self):
        self.install(self.claude)
        shutil.copy(self.claude / "agents/builder.md", self.claude / "agents/builder2.md")
        result = self.status()
        self.assertIn("budget claude: doubled description: builder and builder2", result.stdout.splitlines())
        self.assertEqual(result.returncode, 0)

    def test_codex_home(self):
        self.install(self.codex, host="codex")
        result = self.status()
        tree = tokens_of(self.codex, "--host", "codex")["trees"][0]
        line = (f"budget codex: ~{tree['tokens']} tokens always-on ({tree['bytes']} B names and "
                f"descriptions, estimate; ceiling 650)")
        self.assertIn(line, result.stdout.splitlines())
        self.assertFalse([x for x in result.stdout.splitlines() if x.startswith("budget claude")])

    def test_together_label_with_harness_manifest(self):
        self.install(self.claude, harness=True)
        tree = tokens_of(self.claude)["trees"][0]
        self.assertEqual(tree["label"], "together")
        result = self.status()
        line = (f"budget claude: ~{tree['tokens']} tokens always-on ({tree['bytes']} B names and "
                f"descriptions, estimate; ceiling 900)")
        self.assertIn(line, result.stdout.splitlines())

    def test_checkout_fallback(self):
        result = self.status()
        tree = json.loads(run("--json").stdout)["trees"][0]
        line = (f"budget checkout: ~{tree['tokens']} tokens always-on ({tree['bytes']} B names and "
                f"descriptions, estimate; ceiling 650)")
        self.assertIn(line, result.stdout.splitlines())

    def test_missing_budget_script_is_not_fatal(self):
        copy = self.tmp / "copy"
        for folder in ("bin", "hooks", "agents", "skills", "codex"):
            shutil.copytree(ROOT / folder, copy / folder)
        result = self.status(root=copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("budget: unavailable (scripts/budget.py is missing)", result.stdout.splitlines())

    def test_raising_budget_script_is_not_fatal(self):
        copy = self.tmp / "copy"
        for folder in ("bin", "hooks", "agents", "skills", "codex"):
            shutil.copytree(ROOT / folder, copy / folder)
        (copy / "scripts").mkdir()
        (copy / "scripts/budget.py").write_text("raise RuntimeError('x')\n")
        result = self.status(root=copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("budget: unavailable (scripts/budget.py failed)", result.stdout.splitlines())

    def test_budget_script_that_exits_is_not_fatal(self):
        head = 'CEILINGS = {"router": 650}\n'
        marker = "from pathlib import Path\nPath(__file__).with_name('ran').touch()\n"
        good = '{"trees": [{"label": "router", "tokens": 1, "bytes": 4}], "flags": []}'
        stubs = {
            "import-exit-3": head + marker + "import sys\nsys.exit(3)\n",
            "import-exit-0": head + marker + "import sys\nsys.exit(0)\n",
            "exit-message": head + "import sys\nsys.exit('budget exploded')\n",
            "report-exit-3": "import sys\nCEILINGS = {}\ndef load_tree(*a):\n    return None\n"
                             "def report(*a):\n    sys.exit(3)\n",
            "report-exit-0": "import sys\nCEILINGS = {}\ndef load_tree(*a):\n    return None\n"
                             "def report(*a):\n    sys.exit(0)\n",
            "hard-exit": head + marker + "import os\nos._exit(0)\n",
            "partial-then-hard-exit": head + "import os\nprint('{\"trees\": [', flush=True)\nos._exit(0)\n",
            "json-then-exit-3": head + f"import sys\nprint({good!r})\nsys.exit(3)\n",
            "garbage": head + "print('\\x00\\x01 not json {{{')\n",
            "not-json": "print('not json')\n",
            "sleep": head + "import time\ntime.sleep(5)\n",
        }
        for name, source in stubs.items():
            with self.subTest(stub=name):
                copy = self.tmp / name
                for folder in ("bin", "hooks", "agents", "skills", "codex"):
                    shutil.copytree(ROOT / folder, copy / folder)
                (copy / "scripts").mkdir()
                (copy / "scripts/budget.py").write_text(source)
                result = self.status(root=copy, outer_timeout=2 if name == "sleep" else TIMEOUT,
                                     ROUTER_STATUS_BUDGET_TIMEOUT="1")
                if name in ("import-exit-3", "import-exit-0", "hard-exit"):
                    self.assertTrue((copy / "scripts/ran").exists(), name)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = [x for x in result.stdout.splitlines() if x.startswith("budget")]
                self.assertEqual(len(lines), 1, result.stdout)
                self.assertEqual(lines[0], "budget: unavailable (scripts/budget.py failed)")
                self.assertEqual(len([x for x in result.stdout.splitlines() if "budget" in x]), 1, result.stdout)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_ceilings_come_from_budget_script(self):
        copy = self.tmp / "copy"
        for folder in ("bin", "hooks", "agents", "skills", "codex", "scripts"):
            shutil.copytree(ROOT / folder, copy / folder)
        script = copy / "scripts/budget.py"
        text = script.read_text()
        self.assertIn('CEILINGS = {"router": 650, "harness": 600, "together": 900}', text)
        script.write_text(text.replace('CEILINGS = {"router": 650,', 'CEILINGS = {"router": 11,'))
        tree = json.loads(run("--home", str(copy), "--json", script=script).stdout)["trees"][0]
        result = self.status(root=copy)
        self.assertEqual(result.returncode, 0, result.stderr)
        line = (f"budget checkout: ~{tree['tokens']} tokens always-on ({tree['bytes']} B names and "
                f"descriptions, estimate; ceiling 11) OVER")
        self.assertIn(line, result.stdout.splitlines())

    def test_status_ignores_caller_overlay(self):
        decoy = self.tmp / "decoy.json"
        decoy.write_text('{"context":{"files_threshold":99}}')
        from unittest.mock import patch
        with patch.dict(os.environ, {"ROUTER_LOCAL": str(decoy)}):
            result = self.status()
        self.assertIn("overlay: off (ROUTER_LOCAL=off)", result.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
