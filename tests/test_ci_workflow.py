#!/usr/bin/env python3
"""Offline checks of the CI workflow, the sibling-ref script and the public policy files.

The workflow is parsed with a small loader for the YAML subset it uses (standard library only;
when PyYAML is installed the two loaders must agree), then its structure is asserted: every
test job on ubuntu, macOS and Windows at Python 3.9 and 3.13 without fail-fast, every test file
through tests/run_all.py, old release trees from commit SHAs, and the Harness repository and ref
from repository variables. The sibling-ref script runs against scratch git repositories."""
import importlib.util, os, re, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github/workflows/ci.yml"
REF_SCRIPT = ROOT / "scripts/harness-ref.sh"
SIBLING_SCRIPT = "scripts/router-ref.sh"   # the same bytes ship in Harness under this name
SIBLING_ENV = "HARNESS_DIR"
SCRIPT_NAME = "harness-ref"
OSES = ["ubuntu-latest", "macos-latest", "windows-latest"]
PYTHONS = ["3.9", "3.13"]
REPOSITORY = "${{ vars.HARNESS_REPOSITORY || 'aeiadev/harness' }}"
PINNED = "${{ vars.HARNESS_REF }}"
OLD_TREES = (
    ". router-0.1 36acbafd839886fdff9d7ba7a416d6a8fad13ec5 ROUTER_V01",
    ". router-0.2 421454545008e0c361def01049989df5b9056b83 ROUTER_V02",
    "harness harness-0.1 063388b95632e935d53ba5ae058659b51960308d HARNESS_V01",
    "harness harness-0.2 3b80d6fbaa3d15970e6f0e1409d141b952f0b7f2 HARNESS_V02",
)
AUTOCRLF = {"name": "Keep checkouts LF", "run": "git config --global core.autocrlf false"}
CHECKOUT = {"uses": "actions/checkout@v4", "with": {"fetch-depth": 0}}
SIBLING_CHECKOUT = {"uses": "actions/checkout@v4",
                    "with": {"repository": "${{ env.HARNESS_REPOSITORY }}", "path": "harness", "fetch-depth": 0}}
PINNED_CHECKOUT = {"uses": "actions/checkout@v4",
                   "with": {**SIBLING_CHECKOUT["with"], "ref": "${{ env.HARNESS_PINNED_REF }}"}}
SETUP_PYTHON = {"uses": "actions/setup-python@v5", "with": {"python-version": "${{ matrix.python-version }}"}}
AFTER_FAILURE = "${{ !cancelled() }}"
TEST_CHECKS = (
    ("Shared pins", "cd agents\nif command -v sha256sum >/dev/null 2>&1; then sha256sum -c SHARED.sha256; "
                    "else shasum -a 256 -c SHARED.sha256; fi\n"),
    ("Generated roles", "python scripts/generate_roles.py --check"),
    ("Plugin drift", "python scripts/gen_plugin.py --check"),
    ("Plugin validate", "if command -v claude >/dev/null; then claude plugin validate .; claude plugin validate "
                        "plugins/router; claude plugin validate plugins/shared-roles; fi\n"),
)
RESOLVE = 'ref=$(bash scripts/harness-ref.sh resolve harness VERSION "$HARNESS_PINNED_REF")'
LATEST = "ref=$(bash scripts/harness-ref.sh latest harness VERSION)"
DETACH = 'git -C harness checkout -q --detach "$ref"'
TOGETHER = "HARNESS_DIR=$PWD/harness bash tests/together.sh"
GITLEAKS = 'docker run --rm -v "$PWD:/repo" -w /repo zricethezav/gitleaks:v8.18.4 detect --source . --no-banner'
ACTIONS = {"actions/checkout@v4", "actions/setup-python@v5"}
SECURITY = [
    "Only the latest release is supported.",
    "Report a vulnerability privately through GitHub\x27s private vulnerability reporting on this repository. If that is not available, open an issue that says a security report is waiting and leave the details out.",
    "These tools read command text and install files under your Claude Code and Codex homes. The danger guard is a best-effort check of common command forms, not a sandbox.",
    "No response time is promised.",
]
CONTRIBUTING = [
    "Run the whole suite with `python3 tests/run_all.py` (`python tests/run_all.py` in Git Bash on Windows); it runs every test file and lists every failure.",
    "No test calls a model or the network; CI also checks out the sibling repository and builds the old release trees.",
    "Python 3.9 or newer, standard library only.",
    "Keep a change small and add a test that fails without it.",
]
GIT_VALUE = r'''(?:"[^"]*"|'[^']*'|\S+)'''
GIT_OPTIONS = r"(?:\s+(?:-[Cc]\s+" + GIT_VALUE + r"|--?[\w-]+(?:=" + GIT_VALUE + r")?))*"
NETWORK_WORDS = (r"\b(?:pip3?|apt(?:-get)?|brew|curl|wget|npm|npx|docker)\b"
                 r"|\bgit" + GIT_OPTIONS + r"\s+(?:clone|fetch|pull|submodule\s+update|remote\s+update|ls-remote)\b")
OPERATOR_END = re.compile(r"(==|!=|&&|\|\||!|<|>)$")


class YamlError(ValueError):
    pass


def load_yaml(text):
    """Parse the YAML subset the workflow uses: block maps and lists, `|` block scalars, flow lists,
    plain and quoted scalars, whole-line comments. Anything else raises YamlError."""
    lines = text.split("\n")
    pos = [0]

    def indent(line):
        return len(line) - len(line.lstrip(" "))

    def peek():
        while pos[0] < len(lines) and (not lines[pos[0]].strip() or lines[pos[0]].lstrip().startswith("#")):
            pos[0] += 1
        return lines[pos[0]] if pos[0] < len(lines) else None

    def fail(why):
        raise YamlError("line %d: %s" % (pos[0] + 1, why))

    def node(level):
        body = peek()[level:]
        return sequence(level) if body == "-" or body.startswith("- ") else mapping(level)

    def mapping(level):
        out = {}
        while True:
            line = peek()
            if line is None or indent(line) < level:
                return out
            if indent(line) > level or "\t" in line[:indent(line) + 1]:
                fail("unexpected indent")
            body = line[level:]
            if body.startswith("- "):
                return out
            found = re.match(r"([\w.-]+):(?: (.*))?$", body)
            if not found:
                fail("not a key: " + body)
            key, rest = found.group(1), (found.group(2) or "").strip()
            if key in out:
                fail("duplicate key " + key)
            pos[0] += 1
            out[key] = value(rest, level)

    def value(rest, level):
        if rest in ("|", "|-"):
            return block(level, rest == "|")
        if rest:
            return scalar(rest)
        line = peek()
        if line is not None and indent(line) > level:
            return node(indent(line))
        return None

    def sequence(level):
        out = []
        while True:
            line = peek()
            if line is None or indent(line) < level:
                return out
            body = line[level:]
            if indent(line) > level or not (body == "-" or body.startswith("- ")):
                fail("bad list item")
            item = body[2:]
            if re.match(r"[\w.-]+:(?: |$)", item):
                lines[pos[0]] = " " * (level + 2) + item
                out.append(mapping(level + 2))
            else:
                pos[0] += 1
                out.append(scalar(item.strip()))

    def block(level, keep):
        out, inner = [], None
        while pos[0] < len(lines):
            line = lines[pos[0]]
            if not line.strip():
                out.append("")
                pos[0] += 1
                continue
            if indent(line) <= level:
                break
            inner = indent(line) if inner is None else inner
            if indent(line) < inner:
                fail("block scalar dedent")
            out.append(line[inner:])
            pos[0] += 1
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out) + ("\n" if keep and out else "")

    def scalar(text):
        if text.startswith("["):
            if not text.endswith("]"):
                fail("open flow list")
            parts, current, quote = [], "", None
            for char in text[1:-1]:
                if quote:
                    quote = None if char == quote else quote
                elif char in "'\"":
                    quote = char
                elif char == ",":
                    parts.append(current)
                    current = ""
                    continue
                current += char
            if quote:
                fail("open quote")
            parts.append(current)
            return [scalar(part.strip()) for part in parts if part.strip()]
        if text[0] in "\"'":
            if len(text) < 2 or text[-1] != text[0] or text[0] in text[1:-1].replace(text[0] * 2, ""):
                fail("bad quoted scalar " + text)
            return text[1:-1].replace(text[0] * 2, text[0]) if text[0] == "'" else text[1:-1]
        if text[0] in "!&*{|>%@`]" or ": " in text or text.endswith(":") or " #" in text:
            fail("plain scalar this loader does not take: " + text)
        if text in ("true", "false"):
            return text == "true"
        if text in ("null", "~"):
            return None
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        if re.fullmatch(r"-?\d+\.\d+", text):
            return float(text)
        return text

    if peek() is None:
        return None
    result = node(0)
    if peek() is not None:
        fail("trailing content")
    return result


def pyyaml_tree(text):
    """The PyYAML reading of text with `on` as a string key, or None when PyYAML is absent."""
    if importlib.util.find_spec("yaml") is None:
        return None
    import yaml
    tree = yaml.safe_load(text)
    if isinstance(tree, dict) and True in tree:
        tree = {("on" if key is True else key): value for key, value in tree.items()}
    return tree


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


def named(steps, name):
    found = [index for index, step in enumerate(steps) if step.get("name") == name]
    return found[0] if len(found) == 1 else None


def runs(tree):
    """(job, run text) for every step of every job."""
    for job_name, job in (tree.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if isinstance(step.get("run"), str):
                yield job_name, step["run"]


def matrix_problems(name, job, extra=None):
    errors = []
    strategy = job.get("strategy") or {}
    matrix = strategy.get("matrix") or {}
    if job.get("runs-on") != "${{ matrix.os }}" or matrix.get("os") != OSES:
        errors.append("os matrix in " + name)
    if matrix.get("python-version") != PYTHONS:
        errors.append("python matrix in " + name)
    if strategy.get("fail-fast") is not False:
        errors.append("fail-fast in " + name)
    for key, values in (extra or {}).items():
        if matrix.get(key) != values:
            errors.append(key + " matrix in " + name)
    return errors


def setup_problems(name, steps):
    errors = []
    if not steps or steps[0] != AUTOCRLF:
        errors.append("autocrlf in " + name)
    sibling = PINNED_CHECKOUT if name == "test" else SIBLING_CHECKOUT
    if CHECKOUT not in steps or sibling not in steps:
        errors.append("checkouts in " + name)
    if SETUP_PYTHON not in steps:
        errors.append("setup-python in " + name)
    trees = named(steps, "Old release trees")
    text = steps[trees].get("run", "") if trees is not None else ""
    for line in OLD_TREES:
        if not re.search(r"(?m)^old_tree " + re.escape(line) + "$", text):
            errors.append("old tree " + line.split()[-1] + " in " + name)
    if re.search(r"\bv\d+\.\d+\.\d+\b", text) or 'echo "$4=$base/$2" >> "$GITHUB_ENV"' not in text:
        errors.append("old tree export in " + name)
    return errors, text


def problems(text):
    """Every way the workflow breaks the CI contract, as short names; [] when it holds."""
    try:
        tree = load_yaml(text)
    except YamlError as error:
        return ["yaml: " + str(error)]
    errors = expr_problems(text)
    loaded = pyyaml_tree(text)
    if loaded is not None and loaded != tree:
        errors.append("loaders disagree")
    if not isinstance(tree, dict):
        return errors + ["not a mapping"]
    if (tree.get("defaults") or {}).get("run") != {"shell": "bash"}:
        errors.append("bash default")
    env = tree.get("env") or {}
    if env.get("HARNESS_REPOSITORY") != REPOSITORY:
        errors.append("sibling repository variable")
    if env.get("HARNESS_PINNED_REF") != PINNED:
        errors.append("sibling ref variable")
    jobs = tree.get("jobs") or {}
    if set(jobs) != {"test", "together", "shared-drift", "secrets"}:
        return errors + ["jobs"]
    test, together, drift, secrets = (jobs[name] for name in ("test", "together", "shared-drift", "secrets"))
    errors += matrix_problems("test", test)
    errors += matrix_problems("together", together, {"harness-ref": ["resolved", "latest-tag"]})
    steps = test.get("steps") or []
    found, test_trees = setup_problems("test", steps)
    errors += found
    suite = named(steps, "Tests")
    if suite is None or steps[suite].get("run") != "python tests/run_all.py" or "if" in steps[suite]:
        errors.append("run_all in test")
    places = [named(steps, check) for check, _ in TEST_CHECKS]
    if (suite is None or None in places or places != sorted(places) or places[0] < suite
            or any(steps[index].get("run") != command for index, (_, command) in zip(places, TEST_CHECKS))):
        errors.append("checks in test")
    elif any(step.get("if") != AFTER_FAILURE for step in steps[suite + 1:]):
        errors.append("checks after a failure")
    steps = together.get("steps") or []
    found, together_trees = setup_problems("together", steps)
    errors += found
    if test_trees != together_trees:
        errors.append("old trees differ")
    ref, run = named(steps, "Harness ref"), named(steps, "Together")
    ref_text = steps[ref].get("run", "") if ref is not None else ""
    if (ref is None or steps[ref].get("env") != {"MODE": "${{ matrix.harness-ref }}"}
            or any(part not in ref_text for part in (RESOLVE, LATEST, DETACH, 'echo "HARNESS_REF=$ref" >> "$GITHUB_ENV"',
                                                     '"$MODE" = latest-tag', "notice: no Harness release tag"))):
        errors.append("harness ref")
    if run is None or ref is None or run < ref or steps[run] != {
            "name": "Together", "if": "env.HARNESS_REF != ''", "run": TOGETHER}:
        errors.append("together run")
    steps = drift.get("steps") or []
    ref = named(steps, "Harness ref")
    cmp_step = {"run": "cmp agents/SHARED.sha256 harness/agents/SHARED.sha256"}
    if (drift.get("runs-on") != "ubuntu-latest" or SIBLING_CHECKOUT not in steps or ref is None
            or RESOLVE not in steps[ref].get("run", "") or DETACH not in steps[ref].get("run", "")
            or cmp_step not in steps[ref:]):
        errors.append("shared drift")
    if secrets != {"runs-on": "ubuntu-latest", "steps": [{"uses": "actions/checkout@v4"}, {"run": GITLEAKS}]}:
        errors.append("secrets")
    for job_name, script in runs(tree):
        if re.search(NETWORK_WORDS, script) and not (job_name == "secrets" and script == GITLEAKS):
            errors.append("network use in " + job_name)
        if re.search(r"\bfor \w+ in [^;\n]*tests/", script):
            errors.append("test loop in " + job_name)
        if re.search(r"(?m)(^|[\s;&|(])python3\b", script):
            errors.append("python3 in " + job_name)
        for path in re.findall(r"(?<![\w/$])(?:tests|scripts|codex|agents)/[\w.*-]+(?:/[\w.*-]+)*", script):
            if "*" not in path and not (ROOT / path).exists():
                errors.append("missing " + path)
    for job in jobs.values():
        for step in job.get("steps") or []:
            if "uses" in step and step["uses"] not in ACTIONS:
                errors.append("action pin " + str(step["uses"]))
    return list(dict.fromkeys(errors))


def find_bash():
    """bash for this platform, chosen the way tests/run_all.py chooses it."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("run_all_for_ci_test", str(ROOT / "tests/run_all.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    bash = module.find_bash()
    if not bash:
        raise unittest.SkipTest("no bash found")
    return bash


class Scratch:
    """A bare 'sibling' remote, a work clone that pushes to it, and the CI-style checkout of it."""

    def __init__(self, tmp):
        self.tmp = tmp
        (tmp / "hooks").mkdir()
        (tmp / "gitconfig").write_bytes(b"")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({"HOME": str(tmp), "GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                         "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t",
                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t"})
        self.env.pop("SIBLING_REF_TRIES", None)
        self.env.pop("SIBLING_REF_INTERVAL", None)
        self.remote, self.work, self.clone = tmp / "remote.git", tmp / "work", tmp / "harness"
        self.git("init", "-q", "--bare", str(self.remote))
        self.git("init", "-q", str(self.work))
        self.git("-C", str(self.work), "remote", "add", "origin", str(self.remote))
        self.own = tmp / "VERSION"

    def git(self, *args):
        done = subprocess.run(["git", "-c", "core.hooksPath=" + str(self.tmp / "hooks"), "-c", "init.defaultBranch=main",
                               "-c", "core.autocrlf=false", *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        if done.returncode:
            raise AssertionError("git %s: %s" % (" ".join(args), done.stderr))
        return done.stdout.strip()

    def commit(self, version, branch="main"):
        (self.work / "VERSION").write_bytes((version + "\n").encode())
        self.git("-C", str(self.work), "add", "VERSION")
        self.git("-C", str(self.work), "commit", "-q", "--allow-empty", "-m", version)
        self.git("-C", str(self.work), "push", "-q", "-f", "origin", "HEAD:refs/heads/" + branch)
        return self.git("-C", str(self.work), "rev-parse", "HEAD")

    def tag(self, name, commit):
        self.git("-C", str(self.work), "tag", name, commit)
        self.git("-C", str(self.work), "push", "-q", "origin", "refs/tags/" + name)

    def checkout(self):
        self.git("clone", "-q", str(self.remote), str(self.clone))

    def command(self, mode, version, *extra):
        self.own.write_bytes((version + "\n").encode())
        return [BASH[0], REF_SCRIPT.as_posix(), mode, self.clone.as_posix(), self.own.as_posix(), *extra]

    def run(self, mode, version, *extra, **env):
        merged = dict(self.env, **env)
        return subprocess.run(self.command(mode, version, *extra), env=merged, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=240)


BASH = []


class CIWorkflowTests(unittest.TestCase):
    def test_workflow(self):
        self.assertTrue(CI.is_file())
        self.assertEqual(problems(CI.read_text()), [])

    def test_loader_reads_the_workflow_like_pyyaml(self):
        text = CI.read_text()
        tree = load_yaml(text)
        self.assertEqual(tree["jobs"]["test"]["strategy"]["matrix"]["python-version"], PYTHONS)
        loaded = pyyaml_tree(text)
        if loaded is None:
            self.skipTest("PyYAML not installed; the structure checks above still ran")
        self.assertEqual(loaded, tree)
        for bad in ("a: [x\n", "a:\n  b: 1\n   c: 2\n", "a: x: y\n", "a: 'x\n", "a: 1\na: 2\n", "- a\nb: 1\n"):
            with self.subTest(bad=bad), self.assertRaises(YamlError):
                load_yaml(bad)

    def test_broken_workflows_are_rejected(self):
        text = CI.read_text()
        trees = OLD_TREES[1]
        mutations = (
            ("os: [ubuntu-latest, macos-latest, windows-latest]", "os: [ubuntu-latest, macos-latest]",
             ["os matrix in test", "os matrix in together"]),
            ('python-version: ["3.9", "3.13"]', 'python-version: ["3.10", "3.13"]',
             ["python matrix in test", "python matrix in together"]),
            ("fail-fast: false", "fail-fast: true", ["fail-fast in test", "fail-fast in together"]),
            ("old_tree " + trees + "\n", "old_tree . router-0.2 v0.2.0 ROUTER_V02\n",
             ["old tree ROUTER_V02 in test", "old tree export in test",
              "old tree ROUTER_V02 in together", "old tree export in together"]),
            ("          old_tree " + OLD_TREES[3] + "\n", "",
             ["old tree HARNESS_V02 in test", "old tree HARNESS_V02 in together"]),
            ("  HARNESS_REPOSITORY: " + REPOSITORY, "  HARNESS_REPOSITORY: aeiadev/harness",
             ["sibling repository variable"]),
            ("  HARNESS_PINNED_REF: " + PINNED, "  HARNESS_PINNED_REF: main", ["sibling ref variable"]),
            ("          ref: ${{ env.HARNESS_PINNED_REF }}\n", "", ["checkouts in test"]),
            ("run: python tests/run_all.py", 'run: for t in tests/test_*.py; do python3 "$t" || exit 1; done',
             ["run_all in test", "test loop in test", "python3 in test"]),
            ("run: python scripts/gen_plugin.py --check", "run: python3 scripts/gen_plugin.py --check",
             ["checks in test", "python3 in test"]),
            ("        if: ${{ !cancelled() }}\n        run: python scripts/gen_plugin.py --check",
             "        run: python scripts/gen_plugin.py --check", ["checks after a failure"]),
            ("        run: git config --global core.autocrlf false\n", "        run: true\n",
             ["autocrlf in test", "autocrlf in together"]),
            ("python-version: ${{ matrix.python-version }}", "python-version: '3.13'",
             ["setup-python in test", "setup-python in together"]),
            (RESOLVE, "ref=main", ["harness ref", "shared drift"]),
            ("run: " + TOGETHER, "run: bash tests/together.sh", ["together run"]),
            ("harness-ref: [resolved, latest-tag]", "harness-ref: [main, latest-tag]", ["harness-ref matrix in together"]),
            ("run: cmp agents/SHARED.sha256", "run: pip install x; cmp agents/SHARED.sha256",
             ["shared drift", "network use in shared-drift"]),
            ("zricethezav/gitleaks:v8.18.4", "zricethezav/gitleaks", ["secrets", "network use in secrets"]),
            ("actions/setup-python@v5", "actions/setup-python@main",
             ["setup-python in test", "setup-python in together", "action pin actions/setup-python@main"]),
            ("    runs-on: ${{ matrix.os }}", "    runs-on: ${{ matrix.os ", None),
            ("python tests/run_all.py", "python tests/run_every.py", ["run_all in test", "missing tests/run_every.py"]),
        )
        for old, new, expected in mutations:
            with self.subTest(old=old[:50]):
                self.assertIn(old, text)
                changed = text.replace(old, new)
                if expected is None:
                    self.assertTrue(problems(changed))
                else:
                    self.assertEqual(problems(changed), expected)
        extra = text.rstrip("\n") + "\n  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pip install x\n"
        self.assertEqual(problems(extra), ["jobs"])

    def test_network_words_cover_git_forms(self):
        for command in ("git fetch", "git -C harness fetch", "git -c k=v pull", "git clone x",
                        "git submodule update --init", "git --no-pager fetch origin",
                        'git -C "dir with space" fetch', "git -C 'dir with space' fetch",
                        'git -c "k=a b" pull', "git remote update", "git remote update origin",
                        "git ls-remote origin", "curl x", "pip install x", "npm ci"):
            with self.subTest(command=command):
                self.assertRegex(command, NETWORK_WORDS)
        for command in ("git -C harness archive v0.2.0", 'git -C "dir with space" archive v0.2.0',
                        "git remote -v", "git remote show origin", "git tag --list", "git config --global x y"):
            with self.subTest(command=command):
                self.assertNotRegex(command, NETWORK_WORDS)

    def test_expressions_are_whole(self):
        text = CI.read_text()
        self.assertEqual(expr_problems(text), [])
        for changed in (text.replace("if: env.HARNESS_REF != ''", "if: env.HARNESS_REF != "),
                        text.replace("if: env.HARNESS_REF != ''", "if: env.HARNESS_REF != '"),
                        text.replace("${{ matrix.os }}", "${{ }}"),
                        text.replace("${{ matrix.os }}", "${{ matrix.os && }}"),
                        text.replace("${{ matrix.os }}", "${{ matrix.os == 'x }}")):
            self.assertNotEqual(changed, text)
            self.assertTrue(expr_problems(changed))
            self.assertTrue(problems(changed))

    def test_old_tree_commits_are_the_published_releases(self):
        """Each SHA is the release commit (the v0.2.0 tags point at the 0.2 ones); checked where the
        history is present, which it is in CI after a full checkout."""
        sibling = os.environ.get(SIBLING_ENV)
        dirs = {".": ROOT, "harness": Path(sibling) if sibling else None}
        checked = 0
        for line in OLD_TREES:
            where, _, sha, _ = line.split()
            repo = dirs[where]
            if repo is None or not (repo / ".git").exists():
                continue
            done = subprocess.run(["git", "-C", str(repo), "cat-file", "-t", sha], stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=30)
            if done.returncode == 0:
                self.assertEqual(done.stdout.strip(), "commit", sha)
                checked += 1
        if not checked:
            self.skipTest("release history not in this checkout")


class SiblingRefTests(unittest.TestCase):
    """scripts/harness-ref.sh against scratch repositories, offline."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git not installed")
        BASH[:] = [find_bash()]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sibling-ref-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = Scratch(self.tmp)

    def test_release_tag_wins(self):
        tagged = self.repo.commit("0.3.1")
        self.repo.tag("v0.3.1", tagged)
        self.repo.commit("0.3.2")
        self.repo.checkout()
        done = self.repo.run("resolve", "0.3.1", SIBLING_REF_TRIES="0")
        self.assertEqual((done.returncode, done.stdout), (0, tagged + "\n"), done.stderr)
        self.assertEqual(done.stderr, "%s: harness tag v0.3.1 (%s)\n" % (SCRIPT_NAME, tagged))

    def test_main_that_matches_or_leads(self):
        for main, own in (("0.3.1", "0.3.1"), ("0.4.0", "0.3.1"), ("0.3.10", "0.3.9"), ("1.0.0", "0.9.9")):
            with self.subTest(main=main, own=own):
                head = self.repo.commit(main)
                if not self.repo.clone.exists():
                    self.repo.checkout()
                else:
                    self.repo.git("-C", str(self.repo.clone), "fetch", "-q", "origin")
                done = self.repo.run("resolve", own, SIBLING_REF_TRIES="0")
                self.assertEqual((done.returncode, done.stdout), (0, head + "\n"), done.stderr)
                self.assertEqual(done.stderr, "%s: harness main at %s (%s)\n" % (SCRIPT_NAME, main, head))

    def test_main_behind_then_catches_up(self):
        self.repo.commit("0.3.0")
        self.repo.checkout()
        proc = subprocess.Popen(self.repo.command("resolve", "0.3.1"), env=dict(self.repo.env, SIBLING_REF_INTERVAL="2"),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first = proc.stderr.readline()
            self.assertRegex(first, r"^%s: harness has no tag v0\.3\.1 and its main is at 0\.3\.0; "
                                    r"fetching again in 2 s \(1 of 30\)$" % SCRIPT_NAME)
            caught_up = self.repo.commit("0.3.1")
            out, err = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual((proc.returncode, out), (0, caught_up + "\n"), err)
        self.assertTrue(err.endswith("%s: harness main at 0.3.1 (%s)\n" % (SCRIPT_NAME, caught_up)), err)

    def test_main_never_catches_up(self):
        self.repo.commit("0.3.0")
        self.repo.checkout()
        started = time.monotonic()
        done = self.repo.run("resolve", "0.3.1", SIBLING_REF_INTERVAL="0")
        self.assertEqual((done.returncode, done.stdout), (1, ""), done.stderr)
        lines = done.stderr.splitlines()
        self.assertEqual(len(lines), 31, done.stderr)
        self.assertEqual(lines[29], "%s: harness has no tag v0.3.1 and its main is at 0.3.0; "
                                    "fetching again in 0 s (30 of 30)" % SCRIPT_NAME)
        self.assertEqual(lines[30], "%s: harness has no tag v0.3.1 and its main is still at 0.3.0 after 30 "
                                    "fetches; push its release (main or the v0.3.1 tag) or pin a ref, then rerun"
                         % SCRIPT_NAME)
        self.assertLess(time.monotonic() - started, 200)
        script = REF_SCRIPT.read_text()
        self.assertIn("${SIBLING_REF_TRIES:-30}", script)
        self.assertIn("${SIBLING_REF_INTERVAL:-20}", script)

    def test_pinned_ref_wins(self):
        tagged = self.repo.commit("0.3.1")
        self.repo.tag("v0.3.1", tagged)
        branch = self.repo.commit("0.1.0", branch="harness-main")
        self.repo.checkout()
        for pin, expected in (("harness-main", branch), ("v0.3.1", tagged), (branch[:12], branch)):
            with self.subTest(pin=pin):
                done = self.repo.run("resolve", "0.3.1", pin, SIBLING_REF_TRIES="0")
                self.assertEqual((done.returncode, done.stdout), (0, expected + "\n"), done.stderr)
                self.assertEqual(done.stderr, "%s: harness pinned ref %s (%s)\n" % (SCRIPT_NAME, pin, expected))
        done = self.repo.run("resolve", "0.3.1", "no-such-ref")
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertEqual(done.stderr, "%s: harness has no ref no-such-ref\n" % SCRIPT_NAME)

    def test_latest_tag_matches_version_exactly(self):
        for tags, version, expected in ((["v0.3.2", "v0.3.3-rc1"], "0.3.0", ""),
                                        (["v0.3.9", "v0.3.10"], "0.3.9", "v0.3.9"),
                                        (["v0.3.1", "v0.3.2"], "0.3.1", "v0.3.1"),
                                        (["v0.3.2"], "0.3.1", ""),
                                        (["v0.2.9", "v0.4.0"], "0.3.0", ""),
                                        (["v0.3.1", "v0.30.0"], "0.3.1", "v0.3.1"),
                                        (["v0.3.1+b", "v0.3.2.1", "0.3.4", "v0.3.x"], "0.3.0", ""),
                                        (["v0.3.0"], "0.3.1", ""),
                                        (["v0.3.0", "v0.3.1"], "0.3.1", "v0.3.1"),
                                        ([], "0.3.0", "")):
            with self.subTest(tags=tags, version=version):
                repo = Scratch(Path(tempfile.mkdtemp(prefix="latest-", dir=str(self.tmp))))
                head = repo.commit("9.9.9")
                for tag in tags:
                    repo.tag(tag, head)
                repo.checkout()
                done = repo.run("latest", version)
                self.assertEqual((done.returncode, done.stdout, done.stderr),
                                 (0, expected + "\n" if expected else "", ""))

    def test_bad_version_and_usage(self):
        self.repo.commit("0.3.1")
        self.repo.checkout()
        for mode in ("latest", "resolve"):
            for version in ("1x.2.3", "1.2.3x", "1.2", "1.2.3.4", "a.b.c", ""):
                with self.subTest(mode=mode, version=version):
                    done = self.repo.run(mode, version)
                    self.assertEqual((done.returncode, done.stdout), (1, ""))
                    self.assertEqual(done.stderr, "%s: cannot read a version from %s\n"
                                     % (SCRIPT_NAME, self.repo.own.as_posix()))
        for args in ([], ["resolve"], ["other", "a", "b"], ["latest", "a", "b", "c"]):
            with self.subTest(args=args):
                done = subprocess.run([BASH[0], REF_SCRIPT.as_posix(), *args], env=self.repo.env,
                                      stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 2)
                self.assertTrue(done.stderr.startswith("usage: "), done.stderr)

    def test_script_is_the_same_in_both_repositories(self):
        sibling = os.environ.get(SIBLING_ENV)
        twin = Path(sibling) / SIBLING_SCRIPT if sibling else None
        if twin is None or not twin.is_file():
            self.skipTest("set %s to a checkout that has %s" % (SIBLING_ENV, SIBLING_SCRIPT))
        self.assertEqual(twin.read_bytes(), REF_SCRIPT.read_bytes())


class PolicyTests(unittest.TestCase):
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
        self.assertNotIn("3.10", (ROOT / "CONTRIBUTING.md").read_text())

    def test_shared_lines_match_the_sibling_when_present(self):
        sibling = os.environ.get(SIBLING_ENV)
        if not sibling or not (Path(sibling) / "CONTRIBUTING.md").is_file():
            self.skipTest("set %s to the sibling checkout to compare policy lines" % SIBLING_ENV)
        for name, lines in (("SECURITY.md", SECURITY), ("CONTRIBUTING.md", CONTRIBUTING)):
            theirs = (Path(sibling) / name).read_text().splitlines()
            for line in lines:
                self.assertIn(line, theirs, name)


if __name__ == "__main__":
    unittest.main()
