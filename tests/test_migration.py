#!/usr/bin/env python3
"""Role migration and sibling install contract regressions, entirely offline."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common
import importlib.util
import test_spawn
import test_codex
import test_install

BRIEF = test_spawn.BRIEF
RENAMES = {"seat-exec": "builder", "seat-exec-here": "builder-in-place",
           "seat-judge": "judge", "seat-sweep": "sweeper", "general-purpose": "worker",
           "Explore": "researcher", "Plan": "planner"}
ALIASES = dict(RENAMES)
for old, new in list(RENAMES.items())[:4]:
    for tier in ("light", "std", "up"):
        ALIASES[old + "-" + tier] = new + "-" + tier
for old, new in (("explore", "researcher"), ("plan", "planner"), ("worker", "worker")):
    for tier in ("std", "up"):
        ALIASES[old + "-" + tier] = new + "-" + tier


class SharedRoles(unittest.TestCase):
    def test_writer_roles_enforce_execution_ladder_without_tier_files(self):
        routes = common.load_routes()
        for role in ("test-writer", "docs-writer"):
            self.assertEqual(common.classify(role, routes)["class"], "exec")
            self.assertNotIn(role, routes["tiers"])
            for count, model in ((0, "sonnet"), (1, "sonnet"), (2, "opus")):
                prompt = ("route: up ladder\n" if count == 2 else "") + BRIEF
                result = common.decide_spawn(dict(subagent_type=role, prompt=prompt, model="haiku"),
                                             routes, routes["router"]["modes"], [{}] * count)
                self.assertEqual(result["run_model"], model)
            result = common.decide_spawn(dict(subagent_type=role, prompt=BRIEF), routes,
                                         routes["router"]["modes"], [{}, {}, {}])
            self.assertEqual(result["decision"], "block")

    def test_tier_bodies_and_drift_detection(self):
        spec = importlib.util.spec_from_file_location("generate_roles", ROOT / "scripts/generate_roles.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        routes = common.load_routes()
        self.assertEqual(len(routes["tiers"]), 7)
        for base, levels in routes["tiers"].items():
            base_fields, body = generator.parse((ROOT / f"agents/{base}.md").read_text())
            for tier in levels.values():
                fields, tier_body = generator.parse((ROOT / f"agents/{tier['agent']}.md").read_text())
                self.assertEqual(tier_body, body)
                self.assertEqual(fields["description"], f"Tier of {base}")
                for key in base_fields.keys() - {"name", "description", "model", "effort"}:
                    self.assertEqual(fields[key], base_fields[key])
                self.assertEqual(fields["model"], tier["model"])
                self.assertEqual(fields.get("effort", "none"), tier["effort"])
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary)
            for folder in ("agents", "codex", "scripts", "hooks"):
                shutil.copytree(ROOT / folder, copy / folder)
            for name in ("agents/builder-up.md", "codex/agents/worker-std.toml", "agents/builder.md"):
                path = copy / name
                original = path.read_bytes()
                path.write_bytes(original + b"drift\n")
                result = subprocess.run([sys.executable, str(copy / "scripts/generate_roles.py"), "--check"],
                                        capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn("drift", result.stderr.lower())
                path.write_bytes(original)

    def test_shared_sources_and_generator(self):
        checksum = ROOT / "agents/SHARED.sha256"
        self.assertTrue(checksum.is_file(), "shared checksum manifest missing")
        rows = checksum.read_text().splitlines()
        self.assertEqual(len(rows), 9)
        for row in rows:
            digest, name = row.split()
            self.assertEqual(hashlib.sha256((ROOT / "agents" / name).read_bytes()).hexdigest(), digest)
        result = subprocess.run([sys.executable, str(ROOT / "scripts/generate_roles.py"), "--check"],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_docs_name_migration(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("Renamed in 0.2", readme)
        self.assertIn("deprecated aliases", readme)
        self.assertIn("0.2.0", (ROOT / "CHANGELOG.md").read_text())
        for name in ("skills/dispatch/SKILL.md", "examples/CLAUDE.md.snippet", "examples/CODEX.md.snippet"):
            self.assertNotIn("seat-exec", (ROOT / name).read_text())

    def test_alias_classification_and_policy(self):
        routes = common.load_routes()
        modes = routes["router"]["modes"]
        for old, new in ALIASES.items():
            with self.subTest(alias=old):
                self.assertIsNotNone(common.classify(new, routes), new)
                self.assertEqual(common.classify(old, routes), common.classify(new, routes))
                left = common.decide_spawn(dict(subagent_type=old, prompt=BRIEF), routes, modes, [])
                right = common.decide_spawn(dict(subagent_type=new, prompt=BRIEF), routes, modes, [])
                for field in ("class", "tier", "decision", "updated"):
                    self.assertEqual(left[field], right[field], field)
        self.assertIsNone(common.classify("unknown-role", routes))
        self.assertEqual(common.decide_spawn(dict(subagent_type="unknown-role", prompt=BRIEF),
                                            routes, modes, [])["rule"], "inject-unlisted")


class ClaudeAliases(test_spawn.SpawnTests):
    def test_mixed_names_share_ladder_and_log_names(self):
        for role in ("seat-exec", "builder"):
            self.assertEqual(self.updated(self.run_hook(self.input(agent=role)))["subagent_type"], "builder-std")
        self.assert_blocked(self.run_hook(self.input(agent="seat-exec")), "Round 3")
        self.assertEqual(self.updated(self.run_hook(self.input("route: up ladder\n" + BRIEF,
                                                              agent="builder-up")))["subagent_type"], "builder-up")
        self.assert_blocked(self.run_hook(self.input("route: up ladder\n" + BRIEF, agent="seat-exec-up")), "three times")
        logs = [json.loads(line) for line in (self.state / "spawns.jsonl").read_text().splitlines()]
        self.assertEqual((logs[0]["asked_type"], logs[0]["run_type"]), ("seat-exec", "builder-std"))


class CodexAliases(test_codex.CodexSpawnTests):
    def test_every_alias_matches_installed_canonical_policy(self):
        agents = Path(self.environment["CODEX_HOME"]) / "agents"
        agents.mkdir(parents=True)
        routes = common.load_routes()
        from unittest.mock import patch
        with patch.dict(os.environ, self.environment):
            for old, new in ALIASES.items():
                (agents / (old + ".toml")).write_text('name = "' + old + '"\n')
                for prior in ([], [{}, {}], [{}, {}, {}]):
                    with self.subTest(alias=old, prior=len(prior)):
                        left = common.decide_codex_spawn(self.spawn(old), routes, routes["router"]["modes"], prior)
                        right = common.decide_codex_spawn(self.spawn(new), routes, routes["router"]["modes"], prior)
                        for field in ("class", "tier", "decision", "brief", "rule", "round"):
                            self.assertEqual(left[field], right[field], field)
                        self.assertEqual(left["asked_type"], old)
                        self.assertEqual(left["run_type"], new)

    def test_missing_alias_denied_with_canonical_retry(self):
        self.assert_denied(self.hook(self.spawn("seat-exec")), "builder")
        self.assert_allowed(self.hook(self.spawn("builder")))
        self.assert_allowed(self.hook(self.spawn("builder")))
        self.assert_denied(self.hook(self.spawn("builder")), "builder-up")

    def test_installed_alias_and_new_name_share_ladder(self):
        agents = Path(self.environment["CODEX_HOME"]) / "agents"
        agents.mkdir(parents=True)
        for name in ("seat-exec", "seat-exec-up"):
            (agents / (name + ".toml")).write_text('name = "' + name + '"\n')
        self.assert_allowed(self.hook(self.spawn("seat-exec")))
        self.assert_allowed(self.hook(self.spawn("builder")))
        self.assert_denied(self.hook(self.spawn("seat-exec")), "builder-up")
        self.assert_allowed(self.hook(self.spawn("builder-up")))
        self.assert_denied(self.hook(self.spawn("seat-exec-up")), "three times")


class Sharing(test_install.InstallerTests):
    def sibling(self, host, first):
        """Model Harness v1's manifest contract with byte-identical shared inputs."""
        home = self.home / ("." + host)
        extension = ".md" if host == "claude" else ".toml"
        source = ROOT / ("agents" if host == "claude" else "codex/agents")
        files = {}
        for name in ("builder", "builder-in-place", "judge", "sweeper", "worker", "researcher", "planner", "test-writer", "docs-writer"):
            relative = "agents/" + name + extension
            target = home / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = (source / (name + extension)).read_bytes()
            if first:
                target.write_bytes(payload)
            else:
                self.assertEqual(target.read_bytes(), payload)
            files[relative] = hashlib.sha256(payload).hexdigest()
        manifest = home / "harness/install-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(dict(version=1, files=files, hooks={})))
        return manifest, files

    def test_shared_files_both_hosts_both_install_orders(self):
        for host in ("claude", "codex"):
            for first in (True, False):
                with self.subTest(host=host, harness_first=first):
                    if not first:
                        self.install("--host", host)
                    sibling, files = self.sibling(host, first)
                    self.install("--host", host)
                    home = self.home / ("." + host)
                    own = json.loads((home / "router/install-manifest.json").read_text())["files"]
                    self.assertTrue(set(files) <= set(own), "identical shared roles must be claimed")
                    result = self.install("--host", host, "--uninstall")
                    self.assertIn("Harness still uses it", result.stdout)
                    for name in files:
                        self.assertTrue((home / name).is_file(), name)
                    sibling.unlink()
                    self.install("--host", host)
                    self.install("--host", host, "--uninstall")
                    for name in files:
                        self.assertFalse((home / name).exists(), name)

    def test_invalid_sibling_manifest_keeps_shared_files(self):
        for host in ("claude", "codex"):
            for bad in ("{", "[]", '{"version":2,"files":{},"hooks":{}}',
                        '{"version":1,"files":{"agents/builder.md":"bad"},"hooks":{}}'):
                self.install("--host", host)
                sibling, files = self.sibling(host, False)
                sibling.write_text(bad)
                result = self.install("--host", host, "--uninstall")
                self.assertIn("invalid Harness manifest", result.stdout)
                self.assertTrue(all((sibling.parents[1] / name).is_file() for name in files))
                sibling.unlink()


class Mutations(unittest.TestCase):
    def test_alias_normalization_and_sibling_check_mutations_are_caught(self):
        changes = (
            ("hooks/router/common.py", "name = canonical_role(agent_type, routes)",
             'name = agent_type.strip() if isinstance(agent_type, str) else ""',
             "SharedRoles.test_alias_classification_and_policy"),
            ("install.sh", "relative in SHARED_ROLES and (harness_files is None or relative in harness_files)",
             "False", "Sharing.test_shared_files_both_hosts_both_install_orders"),
        )
        for filename, old, new, case in changes:
            with self.subTest(mutation=filename), tempfile.TemporaryDirectory() as temporary:
                copy = Path(temporary) / "router"
                shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
                path = copy / filename
                text = path.read_text()
                self.assertEqual(text.count(old), 1, "mutation site must be unique")
                path.write_text(text.replace(old, new))
                result = subprocess.run([sys.executable, str(copy / "tests/test_migration.py"), case],
                                        text=True, capture_output=True, timeout=45)
                self.assertNotEqual(result.returncode, 0, "mutation escaped the regression suite")
                self.assertIn("FAIL:", result.stderr, result.stderr)


def load_tests(loader, tests, pattern):
    # Reuse setup/assertion helpers without repeating their entire parent suites.
    suite = unittest.TestSuite()
    for cls in (SharedRoles, ClaudeAliases, CodexAliases, Sharing, Mutations):
        for name in cls.__dict__:
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
