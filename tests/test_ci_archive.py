"""Execute the CI old-tree step against isolated Git histories and archive failures."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_ci_workflow as workflow


class OldTreeArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = workflow.find_bash()
        cls.real_git = shutil.which("git")
        if not cls.real_git:
            raise unittest.SkipTest("git not installed")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-archive-")
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.root = self.tmp / "checkout"
        self.root.mkdir()
        self.sibling_name = next(line.split()[0] for line in workflow.OLD_TREES if line.split()[0] != ".")
        self.sibling = self.root / self.sibling_name
        self.sibling.mkdir()
        self.runner_temp = self.tmp / "runner"
        self.runner_temp.mkdir()
        self.github_env = self.tmp / "github-env"
        self.github_env.write_bytes(b"")
        (self.tmp / "gitconfig").write_bytes(b"")
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update({
            "HOME": str(self.tmp), "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "CI test",
            "GIT_AUTHOR_EMAIL": "t", "GIT_COMMITTER_NAME": "CI test",
            "GIT_COMMITTER_EMAIL": "t", "REAL_GIT": self.real_git,
            "PYTHON_BIN": sys.executable, "RUNNER_TEMP": self.runner_temp.as_posix(),
            "GITHUB_ENV": self.github_env.as_posix(),
        })
        self.refs = {}
        self.contents = {}
        for label, repo in (("root", self.root), (self.sibling_name, self.sibling)):
            self.git("init", "-q", str(repo))
            for number in (1, 2):
                content = (label + "-" + str(number) + "\n").encode() * 4096
                (repo / "payload.bin").write_bytes(content)
                self.git("-C", str(repo), "add", "payload.bin")
                self.git("-C", str(repo), "-c", "core.hooksPath=/dev/null", "commit", "-qm", "archive fixture %s" % number)
                self.refs[label, number] = self.git("-C", str(repo), "rev-parse", "HEAD").strip()
                self.contents[label, number] = content

    def git(self, *args):
        result = subprocess.run([self.real_git, *args], env=self.env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def step(self, job):
        steps = workflow.load_yaml(workflow.CI.read_text())["jobs"][job]["steps"]
        script = next(step["run"] for step in steps if step.get("name") == "Old release trees")
        for old in workflow.OLD_TREES:
            where, label, sha, _ = old.split()
            source = "root" if where == "." else self.sibling_name
            version = 1 if label.endswith("0.1") else 2
            script = script.replace(sha, self.refs[source, version])
        return script

    def run_step(self, job, early_close=False, break_tar=False, bad_ref=False, archive_error=False):
        script = self.step(job)
        if bad_ref:
            where = workflow.OLD_TREES[0].split()[0]
            source = "root" if where == "." else self.sibling_name
            script = script.replace(self.refs[source, 1], "deadbeef", 1)
        prefix = r'''
if command -v cygpath >/dev/null 2>&1; then
  REAL_GIT=$(cygpath -m "$REAL_GIT")
  PYTHON_BIN=$(cygpath -u "$PYTHON_BIN")
fi
git() {
  "$REAL_GIT" "$@" || return $?
  local is_archive=0 is_output=0
  for arg in "$@"; do
    case "$arg" in archive) is_archive=1;; --output*|-o) is_output=1;; esac
  done
  if [ "${ARCHIVE_ERROR:-0}" = 1 ] && [ "$is_archive" = 1 ]; then return 77; fi
  if [ "${EARLY_CLOSE:-0}" = 1 ] && [ "$is_archive" = 1 ] && [ "$is_output" = 0 ]; then
    "$PYTHON_BIN" -c 'import os, signal; signal.signal(signal.SIGPIPE, signal.SIG_DFL); os.write(1, b"\0" * (16 * 1024 * 1024))'
  fi
}
tar() {
  if [ "${BREAK_TAR:-0}" = 1 ]; then
    printf broken > "$RUNNER_TEMP/corrupt.tar"
    command tar -xf "$RUNNER_TEMP/corrupt.tar" -C "$RUNNER_TEMP"
  else
    command tar "$@"
  fi
}
'''
        env = dict(self.env, EARLY_CLOSE=str(int(early_close)), BREAK_TAR=str(int(break_tar)),
                   ARCHIVE_ERROR=str(int(archive_error)))
        return subprocess.run([self.bash, "--noprofile", "--norc", "-e", "-o", "pipefail"],
                              input=prefix + script, cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=30)

    def test_complete_archives_survive_early_reader_close(self):
        for job in ("test", "together"):
            with self.subTest(job=job):
                self.github_env.write_bytes(b"")
                result = self.run_step(job, early_close=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                exports = dict(line.split("=", 1) for line in self.github_env.read_text().splitlines())
                self.assertEqual(set(exports), {line.split()[3] for line in workflow.OLD_TREES})
                for line in workflow.OLD_TREES:
                    where, label, _, var = line.split()
                    source = "root" if where == "." else self.sibling_name
                    version = 1 if label.endswith("0.1") else 2
                    extracted = Path(exports[var]) / "payload.bin"
                    self.assertEqual(extracted.read_bytes(), self.contents[source, version])
                self.assertEqual(list((self.runner_temp / "old-trees").glob("*.tar")), [])

    def test_bad_ref_and_tar_failure_propagate(self):
        for failure in ("bad_ref", "archive_error", "break_tar"):
            with self.subTest(failure=failure):
                self.github_env.write_bytes(b"")
                result = self.run_step("test", **{failure: True})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.github_env.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
