#!/usr/bin/env python3
"""tests/run_all.py runs every test file, prints one line per file, lists every failure and stops
a file that hangs. Runs offline against scratch test folders; standard library only. The same file
ships in Router and Harness."""
import ast, importlib.util, os, re, shutil, subprocess, sys, tempfile, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN_ALL = ROOT / "tests" / "run_all.py"
WINDOWS = os.name == "nt"


def load_run_all():
    """Import tests/run_all.py without writing bytecode beside it."""
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("run_all_under_test", str(RUN_ALL))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = previous


SAMPLES = {
    "test_pass.py": "print('quiet pass output')\n",
    "test_fail.py": "print('loud failure output')\nraise SystemExit(3)\n",
    "test_hang.py": "import time\ntime.sleep(120)\n",
    "test_shell.sh": "echo \"kernel=$(uname -s)\"\nexit 0\n",
    "fresh-user-broken.sh": "echo 'fresh user broke'\nexit 4\n",
    "test_notes.txt": "not a test\n",
    "helper.py": "raise SystemExit(9)\n",
}


class RunAllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="run-all-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def scratch(self, files, copy_runner=False):
        tests = self.tmp / "repo" / "tests"
        (tests / "nested").mkdir(parents=True)
        (tests / "nested" / "test_nested.py").write_text("raise SystemExit(8)\n")
        for name, body in files.items():
            (tests / name).write_bytes(body.encode())
        if copy_runner:
            shutil.copyfile(str(RUN_ALL), str(tests / "run_all.py"))
        return tests

    def run_all(self, *args, runner=RUN_ALL, timeout=240, **extra_env):
        env = {key: value for key, value in os.environ.items() if key != "GITHUB_ACTIONS"}
        env.update(extra_env)
        started = time.monotonic()
        result = subprocess.run([sys.executable, str(runner), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout, env=env)
        result.elapsed = time.monotonic() - started
        result.out = result.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        return result

    def test_every_file_runs_and_every_failure_is_listed(self):
        tests = self.scratch(SAMPLES)
        result = self.run_all("--tests-dir", str(tests), "--timeout", "4")
        out = result.out
        self.assertEqual(result.returncode, 1, out)
        lines = out.splitlines()
        status = [line for line in lines if re.match(r"(PASS|FAIL|TIMEOUT) ", line)]
        self.assertEqual([line.split(" ")[:2] for line in status], [
            ["FAIL", "tests/fresh-user-broken.sh"],
            ["FAIL", "tests/test_fail.py"],
            ["TIMEOUT", "tests/test_hang.py"],
            ["PASS", "tests/test_pass.py"],
            ["PASS", "tests/test_shell.sh"],
        ], out)
        self.assertIn("FAIL tests/test_fail.py (exit 3,", out)
        self.assertIn("FAIL tests/fresh-user-broken.sh (exit 4,", out)
        self.assertIn("TIMEOUT tests/test_hang.py (stopped after 4 s)", out)
        self.assertIn("loud failure output", out)
        self.assertIn("fresh user broke", out)
        self.assertNotIn("quiet pass output", out)
        self.assertNotIn("nested", out)
        self.assertNotIn("helper.py", out)
        self.assertNotIn("test_notes.txt", out)
        tail = lines[lines.index("5 files: 2 passed, 3 failed"):]
        self.assertEqual(tail, [
            "5 files: 2 passed, 3 failed",
            "failed:",
            "  tests/fresh-user-broken.sh (exit 4)",
            "  tests/test_fail.py (exit 3)",
            "  tests/test_hang.py (timed out after 4 s)",
        ], out)
        self.assertLess(result.elapsed, 90, "the hanging file was not stopped")

    def test_shell_files_run_under_git_bash_not_wsl(self):
        tests = self.scratch({"test_shell.sh": SAMPLES["test_shell.sh"].replace("exit 0", "exit 1")})
        result = self.run_all("--tests-dir", str(tests))
        kernel = re.search(r"^kernel=(.*)$", result.out, re.M)
        self.assertIsNotNone(kernel, result.out)
        if WINDOWS:
            self.assertNotEqual(kernel.group(1).strip(), "Linux", "bash.exe resolved to the WSL launcher")
        self.assertEqual(result.returncode, 1, result.out)

    def test_green_run_exits_zero_from_its_own_folder(self):
        tests = self.scratch({"test_pass.py": SAMPLES["test_pass.py"], "test_shell.sh": SAMPLES["test_shell.sh"]},
                             copy_runner=True)
        result = self.run_all(runner=tests / "run_all.py")
        self.assertEqual(result.returncode, 0, result.out)
        self.assertEqual(result.out.splitlines()[-1], "2 files: 2 passed, 0 failed", result.out)
        self.assertNotIn("failed:", result.out)

    def test_github_actions_gets_one_annotation_per_failure(self):
        tests = self.scratch({"test_fail.py": SAMPLES["test_fail.py"], "test_pass.py": SAMPLES["test_pass.py"]})
        result = self.run_all("--tests-dir", str(tests), GITHUB_ACTIONS="true")
        self.assertEqual(result.returncode, 1, result.out)
        self.assertEqual([line for line in result.out.splitlines() if line.startswith("::")],
                         ["::error title=Test failed::tests/test_fail.py (exit 3)"], result.out)

    def test_files_run_from_the_repository_root(self):
        tests = self.scratch({"test_cwd.py": "import os\nprint('cwd=' + os.getcwd())\nraise SystemExit(1)\n"})
        result = self.run_all("--tests-dir", str(tests))
        cwd = re.search(r"^cwd=(.*)$", result.out, re.M)
        self.assertIsNotNone(cwd, result.out)
        self.assertEqual(Path(cwd.group(1).strip()).resolve(), (self.tmp / "repo").resolve())

    def test_empty_folder_is_an_error(self):
        tests = self.tmp / "repo" / "tests"
        tests.mkdir(parents=True)
        result = self.run_all("--tests-dir", str(tests))
        self.assertEqual(result.returncode, 2, result.out)
        self.assertIn("no test files", result.out + result.stderr.decode("utf-8", "replace"))

    def test_bash_comes_from_env_or_git_for_windows(self):
        run_all = load_run_all()
        fake = self.tmp / "Git"
        (fake / "cmd").mkdir(parents=True)
        (fake / "bin").mkdir()
        (fake / "mingw64" / "bin").mkdir(parents=True)
        for path in (fake / "cmd" / "git.exe", fake / "bin" / "bash.exe", fake / "mingw64" / "bin" / "git.exe"):
            path.write_bytes(b"")
        wsl = self.tmp / "Windows" / "System32" / "bash.exe"
        wsl.parent.mkdir(parents=True)
        wsl.write_bytes(b"")
        bash = str(fake / "bin" / "bash.exe")
        self.assertEqual(run_all.bash_beside_git(str(fake / "cmd" / "git.exe")), bash)
        self.assertEqual(run_all.bash_beside_git(str(fake / "mingw64" / "bin" / "git.exe")), bash)
        self.assertIsNone(run_all.bash_beside_git(str(self.tmp / "nowhere" / "git.exe")))
        self.assertEqual(run_all.find_bash({"BASH": bash}, windows=True), bash)
        self.assertEqual(run_all.find_bash({"BASH": bash}, windows=False), bash)
        self.assertEqual(run_all.find_bash({"BASH": str(wsl), "SystemRoot": str(self.tmp / "Windows"),
                                            "ProgramFiles": str(self.tmp), "PATH": ""}, windows=True), bash)
        self.assertEqual(run_all.find_bash({"ProgramFiles": str(self.tmp), "PATH": ""}, windows=True), bash)
        found = run_all.find_bash()
        self.assertIsNotNone(found)
        self.assertTrue(Path(found).is_absolute() and Path(found).is_file(), found)
        source = RUN_ALL.read_text()
        self.assertNotRegex(source, r"which\(\s*['\"]bash")
        self.assertNotRegex(source, r"\[\s*['\"]bash['\"]")

    def test_runner_is_the_same_in_both_repositories(self):
        siblings = [Path(os.environ[name]) for name in ("ROUTER_DIR", "HARNESS_DIR") if os.environ.get(name)]
        siblings = [path / "tests" / "run_all.py" for path in siblings
                    if (path / "tests" / "run_all.py").is_file() and path.resolve() != ROOT]
        if not siblings:
            self.skipTest("set ROUTER_DIR or HARNESS_DIR to the sibling checkout to compare run_all.py")
        for path in siblings:
            self.assertEqual(path.read_bytes(), RUN_ALL.read_bytes(), str(path))

    def test_runner_is_python_39_standard_library(self):
        source = RUN_ALL.read_text()
        ast.parse(source, str(RUN_ALL), feature_version=(3, 9))
        imports = set(re.findall(r"(?m)^(?:from|import) (\w+)", source))
        self.assertLessEqual(imports, {"argparse", "os", "signal", "subprocess", "sys", "tempfile", "time",
                                       "pathlib", "shutil"}, imports)


if __name__ == "__main__":
    unittest.main()
