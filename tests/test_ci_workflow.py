#!/usr/bin/env python3
"""Offline checks for the fixed-format CI contract."""
import os
import re
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
REF_SCRIPT = ROOT / "scripts/harness-ref.sh"
CI = ROOT / ".github/workflows/ci.yml"
SECURITY = [
    "Only the latest release is supported.",
    "Report a vulnerability privately through GitHub\x27s private vulnerability reporting on this repository. If that is not available, open an issue that says a security report is waiting and leave the details out.",
    "These tools read command text and install files under your Claude Code and Codex homes. The danger guard is a best-effort check of common command forms, not a sandbox.",
    "No response time is promised.",
]
CONTRIBUTING = [
    "Run the checks in .github/workflows/ci.yml; none of them calls a model or the network.",
    "Python 3.10 or newer, standard library only.",
    "Keep a change small and add a test that fails without it.",
]

def job(text, name):
    match = re.search(r"(?ms)^  " + re.escape(name) + r":\n(.*?)(?=^  [a-z][\w-]*:|\Z)", text)
    return match.group(1) if match else ""

GITLEAKS = 'docker run --rm -v "$PWD:/repo" -w /repo zricethezav/gitleaks:v8.18.4 detect --source . --no-banner'
GIT_VALUE = r'''(?:"[^"]*"|'[^']*'|\S+)'''
GIT_OPTIONS = r"(?:\s+(?:-[Cc]\s+" + GIT_VALUE + r"|--?[\w-]+(?:=" + GIT_VALUE + r")?))*"
NETWORK_WORDS = (r"\b(?:pip3?|apt(?:-get)?|brew|curl|wget|npm|npx|docker)\b"
                 r"|\bgit" + GIT_OPTIONS + r"\s+(?:clone|fetch|pull|submodule\s+update|remote\s+update|ls-remote)\b")

def split_jobs(text):
    """Map job name to its block lines, by indentation under the top-level jobs: key."""
    jobs, current, in_jobs = {}, None, False
    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        if line.strip() and not line.startswith(" "):
            break
        found = re.match(r"^  ([\w-]+):\s*$", line)
        if found:
            current = found.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)
    return jobs

def network_problems(text):
    """Every run: line of every job must avoid network tools; the pinned gitleaks line is the one exception."""
    errors = []
    for name, lines in split_jobs(text).items():
        for line, in_run in run_lines("\n".join(lines)):
            if in_run and re.search(NETWORK_WORDS, line):
                if name != "secrets" or not re.fullmatch(r"\s*-?\s*run: " + re.escape(GITLEAKS), line):
                    errors.append("network use in " + name)
    return errors

def split_steps(block):
    steps = []
    for line in block.splitlines():
        if re.match(r"^      - ", line):
            steps.append([])
        if steps:
            steps[-1].append(line)
    return steps

def run_lines(block):
    run_indent = None
    for line in block.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if run_indent is not None and stripped and indent <= run_indent:
            run_indent = None
        started = stripped.lstrip("- ").startswith("run:")
        yield line, run_indent is not None or started
        if started:
            run_indent = indent

OPERATOR_END = re.compile(r"(==|!=|&&|\|\||!|<|>)$")

def expr_problems(text):
    """Every `if:` value and every ${{ }} must be whole: non-empty, balanced, no dangling operator."""
    errors = []
    values = [(m.group(1).strip(), "if") for m in re.finditer(r"(?m)^\s*(?:-\s+)?if:(.*)$", text)]
    if text.count("${{") != text.count("}}"):
        errors.append("unbalanced ${{ }}")
    values += [(m.group(1).strip(), "expr") for m in re.finditer(r"\$\{\{(.*?)\}\}", text)]
    for value, kind in values:
        body = value[3:-2].strip() if value.startswith("${{") and value.endswith("}}") else value
        if not body:
            errors.append("empty " + kind)
        if value.count("'") % 2 or value.count('"') % 2:
            errors.append("unbalanced quotes: " + value)
        if OPERATOR_END.search(body):
            errors.append("dangling operator: " + value)
    return errors

def problems(text):
    errors = expr_problems(text)
    for line in ("os: [ubuntu-latest, macos-latest]", "python-version: [\"3.10\", \"3.13\"]", "harness-ref: [main, latest-tag]"):
        if line not in text:
            errors.append(line)
    for name in ("test", "together", "shared-drift", "secrets"):
        if not job(text, name):
            errors.append(name)
    if not re.search(r"(?m)^env:\n  HARNESS_REPOSITORY: aeiadev/harness$", text):
        errors.append("HARNESS_REPOSITORY")
    if text.count("fetch-depth: 0") < 4:
        errors.append("fetch-depth")
    for name in ("ROUTER_V01", "ROUTER_V02", "HARNESS_V01", "HARNESS_V02"):
        if name not in job(text, "test") or not re.search(r"\b" + name + r"=", text):
            errors.append(name)
    for ref in ("36acbaf", "063388b", "v0.2.0"):
        if ref not in job(text, "test"):
            errors.append(ref)
    if "shasum -a 256 -c SHARED.sha256" not in job(text, "test"):
        errors.append("mac sha")
    steps = split_steps(job(text, "test"))
    shared = [index for index, step in enumerate(steps)
              if any(line.strip() == "- name: Shared pins" for line in step)]
    for name, run_lines_expected, problem in (
        ("Plugin drift", ["        run: python3 scripts/gen_plugin.py --check"], "plugin drift step"),
        ("Plugin validate", ["        run: |", "          if command -v claude >/dev/null; then claude plugin validate .; claude plugin validate plugins/router; claude plugin validate plugins/shared-roles; fi"], "plugin validate step"),
    ):
        matches = [(index, step) for index, step in enumerate(steps)
                   if any(line.strip() == "- name: " + name for line in step)]
        if (len(shared) != 1 or len(matches) != 1 or matches[0][0] <= shared[0]
                or matches[0][1] != ["      - name: " + name, *run_lines_expected]):
            errors.append(problem)
    together = job(text, "together")
    for part in ("${{ env.HARNESS_REPOSITORY }}", "HARNESS_DIR=$PWD/harness bash tests/together.sh", "bash scripts/harness-ref.sh harness VERSION", "checkout --detach", "notice: no Harness release tag"):
        if part not in together:
            errors.append(part)
    together_steps = split_steps(together)
    checkout_steps = [step for step in together_steps
                      if any(re.fullmatch(r"\s*repository: \$\{\{ env\.HARNESS_REPOSITORY \}\}", line)
                             for line in step)]
    if len(checkout_steps) != 1 or not any(re.fullmatch(r"\s*path: harness", line)
                                            for line in checkout_steps[0]):
        errors.append("Harness checkout path")
    command = "HARNESS_DIR=$PWD/harness bash tests/together.sh"
    runner_steps = [step for step in together_steps
                    if any("tests/together.sh" in line and in_run
                           for line, in_run in run_lines("\n".join(step)))]
    if len(runner_steps) != 1 or not any(re.fullmatch(r"\s*run: " + re.escape(command), line)
                                          for line in runner_steps[0]):
        errors.append("together run line")
    if "cmp agents/SHARED.sha256 harness/agents/SHARED.sha256" not in job(text, "shared-drift"):
        errors.append("shared drift")
    secrets = job(text, "secrets")
    if "zricethezav/gitleaks:v8.18.4 detect --source . --no-banner" not in secrets or "gitleaks-action" in text:
        errors.append("secrets")
    errors += network_problems(text)
    secret_runs = [line for line, in_run in run_lines(secrets)
                   if in_run and re.match(r"\s*-?\s*run:", line)]
    if secret_runs != ["      - run: " + GITLEAKS]:
        errors.append("gitleaks command")
    for use in re.findall(r"(?m)^\s+uses:\s*(\S+)", text):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+", use):
            errors.append("unversioned uses")
    for path in re.findall(r"(?<![\w/])(?:tests|scripts|codex|agents)/[\w./-]+", text):
        path = path.rstrip(".,;:)")
        if not (ROOT / path).exists() and path + "*.py" not in text:
            errors.append(path)
    return errors

def pick_tag(tags, version="0.3.0", raw=None):
    """Run the workflow's own script against a throwaway repo holding exactly these tags."""
    with tempfile.TemporaryDirectory(prefix="harness-ref-") as tmp:
        tmp = Path(tmp)
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp / "home"), "XDG_CONFIG_HOME": str(tmp / "xdg"),
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t"}
        repo = tmp / "harness"
        git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo)]
        repo.mkdir()
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "c"]):
            subprocess.run(git + args, env=env, stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=30)
        for tag in tags:
            subprocess.run(git + ["tag", tag], env=env, stdin=subprocess.DEVNULL, check=True, capture_output=True, timeout=30)
        (tmp / "VERSION").write_bytes(raw if raw is not None else (version + "\n").encode())
        result = subprocess.run(["bash", str(REF_SCRIPT), str(repo), str(tmp / "VERSION")], env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        result.version_path = tmp / "VERSION"
        result.version_bytes = result.version_path.read_bytes()
        return result

class CIWorkflowTests(unittest.TestCase):
    def test_workflow(self):
        self.assertTrue(CI.is_file())
        self.assertEqual(problems(CI.read_text()), [])

    def test_broken_workflows_are_rejected(self):
        text = CI.read_text()
        for changed in (text.replace("macos-latest", "ubuntu-latest"),
                        re.sub(r"(?ms)^  together:\n.*?(?=^  [a-z][\w-]*:|\Z)", "", text),
                        text + "\n      - run: bash tests/nope.sh\n",
                        re.sub(r"(?ms)^  shared-drift:\n.*?(?=^  [a-z][\w-]*:|\Z)", "", text),
                        text.replace("zricethezav/gitleaks:v8.18.4", "zricethezav/gitleaks")):
            self.assertTrue(problems(changed))
        drift = "      - name: Plugin drift\n        run: python3 scripts/gen_plugin.py --check\n"
        validate = ("      - name: Plugin validate\n        run: |\n"
                    "          if command -v claude >/dev/null; then claude plugin validate .; claude plugin validate plugins/router; claude plugin validate plugins/shared-roles; fi\n")
        self.assertIn(drift, text)
        self.assertIn(validate, text)
        for changed, expected in (
            (text.replace(drift, ""), ["plugin drift step"]),
            (text.replace(validate, ""), ["plugin validate step"]),
            (text.replace("python3 scripts/gen_plugin.py --check", "python3 scripts/gen_plugin.py --check || true"),
             ["plugin drift step"]),
            (text.replace("python3 scripts/gen_plugin.py --check", "python3 scripts/gen_plugin.py"),
             ["plugin drift step"]),
            (text.replace(drift, "").replace(
                "        run: HARNESS_DIR=$PWD/harness bash tests/together.sh\n",
                "        run: HARNESS_DIR=$PWD/harness bash tests/together.sh\n" + drift),
             ["plugin drift step"]),
            (text.replace(validate, validate.replace("; fi\n", "; pip install x; fi\n")),
             ["plugin validate step", "network use in test"]),
        ):
            with self.subTest(expected=expected):
                self.assertNotEqual(changed, text)
                self.assertEqual(problems(changed), expected)

    def test_network_check_covers_every_job(self):
        text = CI.read_text()
        self.assertEqual(network_problems(text), [])
        extra = text.rstrip("\n") + "\n  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pip install x\n"
        self.assertEqual(network_problems(extra), ["network use in extra"])
        self.assertEqual(problems(extra), ["network use in extra"])

    def test_network_words_cover_git_forms(self):
        text = CI.read_text()
        for command in ("git fetch", "git -C harness fetch", "git -c k=v pull", "git clone x",
                        "git submodule update --init", "git --no-pager fetch origin",
                        'git -C "dir with space" fetch', "git -C 'dir with space' fetch",
                        'git -c "k=a b" pull', "git remote update", "git remote update origin",
                        "git ls-remote origin"):
            with self.subTest(command=command):
                changed = text.replace("      - name: Tests\n", "      - run: " + command + "\n      - name: Tests\n")
                self.assertNotEqual(changed, text)
                self.assertEqual(network_problems(changed), ["network use in test"])
        for command in ("git -C harness archive v0.2.0", 'git -C "dir with space" archive v0.2.0',
                        "git remote -v", "git remote show origin", "git tag --list"):
            with self.subTest(command=command):
                changed = text.replace("      - name: Tests\n", "      - run: " + command + "\n      - name: Tests\n")
                self.assertEqual(network_problems(changed), [])

    def test_expressions_are_whole(self):
        text = CI.read_text()
        self.assertEqual(expr_problems(text), [])
        self.assertIn("if: env.HARNESS_REF != ''", text)
        for changed in (text.replace("if: env.HARNESS_REF != ''", "if: env.HARNESS_REF != "),
                        text.replace("if: env.HARNESS_REF != ''", "if: "),
                        text.replace("if: env.HARNESS_REF != ''", "if: env.HARNESS_REF != '"),
                        text.replace("${{ matrix.os }}", "${{ matrix.os "),
                        text.replace("${{ matrix.os }}", "${{ }}"),
                        text.replace("${{ matrix.os }}", "${{ matrix.os && }}"),
                        text.replace("${{ matrix.os }}", "${{ matrix.os == 'x }}"),
                        text.replace("${{ matrix.os }}", '${{ "x }}')):
            self.assertTrue(expr_problems(changed), changed[:0])
            self.assertTrue(problems(changed))

    def test_router_twin_mutations_are_rejected(self):
        text = CI.read_text()
        checkout = "          path: harness\n          fetch-depth: 0"
        self.assertIn(checkout, text)
        mutations = (
            text.replace(checkout, "          fetch-depth: 0\n      - name: Misplaced path\n        path: harness", 2),
            text.replace("run: HARNESS_DIR=$PWD/harness bash tests/together.sh",
                         "run: bash tests/together.sh\n      - name: Misplaced command\n"
                         "        run: HARNESS_DIR=$PWD/harness bash tests/together.sh"),
            text.replace("run: HARNESS_DIR=$PWD/harness bash tests/together.sh",
                         "run: pip install x && HARNESS_DIR=$PWD/harness bash tests/together.sh"),
            text.replace("zricethezav/gitleaks:v8.18.4", "zricethezav/gitleaks:v8.18.5"),
            text.replace("      - run: docker run --rm", "      - run: curl x\n      - run: docker run --rm"),
            text.replace("      - name: Tests\n", "      - run: curl x\n      - name: Tests\n"),
        )
        expected = (
            ["Harness checkout path"],
            ["together run line"],
            ["together run line", "network use in together"],
            ["secrets", "network use in secrets", "gitleaks command"],
            ["network use in secrets", "gitleaks command"],
            ["network use in test"],
        )
        for index, changed in enumerate(mutations):
            with self.subTest(index=index):
                self.assertNotEqual(changed, text)
                self.assertEqual(problems(changed), expected[index])
        for index, job_name in ((2, "together"), (4, "secrets"), (5, "test")):
            with self.subTest(network=index):
                self.assertEqual(network_problems(mutations[index]), ["network use in " + job_name])

    def test_invalid_versions_are_rejected(self):
        cases = [(version, None) for version in ("1x.2.3", "1.2.3x", "1.2", "1.2.3.4", "a.b.c", "")]
        cases.append(("zero bytes", b""))
        for version, raw in cases:
            with self.subTest(version=version):
                result = pick_tag(["v0.3.1"], version=version, raw=raw)
                if raw is not None:
                    self.assertEqual(result.version_bytes, b"")
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIsNotNone(re.fullmatch(
                    re.escape(f"harness-ref: cannot read a version from {result.version_path}\n"),
                    result.stderr))
        result = pick_tag(["v0.3.1", "v0.3.10"], version="0.3.0")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "v0.3.10\n", ""))

    def test_workflow_runs_the_tag_script(self):
        text = CI.read_text()
        self.assertEqual(text.count("scripts/harness-ref.sh"), 1)
        self.assertNotIn("tag --list", text)
        self.assertNotIn("sort=", text)

    def test_tag_selection_runs_the_workflow_script(self):
        for tags, expected in ((["v0.3.2", "v0.3.3-rc1"], "v0.3.2"),
                               (["v0.3.9", "v0.3.10"], "v0.3.10"),
                               (["v0.2.9", "v0.4.0"], ""),
                               (["v0.3.1", "v0.30.0"], "v0.3.1"),
                               (["v0.3.1+b", "v0.3.2.1", "0.3.4", "v0.3.x"], ""),
                               ([], "")):
            with self.subTest(tags=tags):
                ran = pick_tag(tags)
                self.assertEqual((ran.returncode, ran.stdout, ran.stderr),
                                 (0, expected + "\n" if expected else "", ""))

    def test_policy_files(self):
        for name, lines in (("SECURITY.md", SECURITY), ("CONTRIBUTING.md", CONTRIBUTING)):
            path = ROOT / name
            self.assertTrue(path.is_file(), name)
            body = path.read_text()
            self.assertLessEqual(len(body.splitlines()), 40)
            for line in lines:
                self.assertIn(line, body.splitlines())
            self.assertNotIn("\u2014", body)
            self.assertIsNone(re.search(r"guarantee|SLA|within 24|within 48|always secure", body, re.I))

    def test_shared_lines_match_twin_brief_when_present(self):
        brief = Path.home() / "projects/oss-staging/briefs/3H6.md"
        if not brief.is_file():
            self.skipTest("twin brief unavailable")
        text = brief.read_text()
        for name, lines in (("SECURITY.md", SECURITY), ("CONTRIBUTING.md", CONTRIBUTING)):
            actual = (ROOT / name).read_text().splitlines()
            for line in lines:
                self.assertIn("\"" + line + "\"", text)
                self.assertIn(line, actual)

if __name__ == "__main__":
    unittest.main()
