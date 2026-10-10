#!/usr/bin/env python3
"""Claude Code plugin packaging: generated trees and --check, stand-down hook commands,
plugin-prefixed role names and the plugin lines of `install.sh --status`.

Every test runs in scratch homes: HOME, CLAUDE_HOME, XDG_CONFIG_HOME, XDG_STATE_HOME and
ROUTER_LOCAL are pinned, and no path depends on the length or name of TMPDIR.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks/router"))
import common  # noqa: E402

VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
BASE_ROLES = ("builder", "builder-in-place", "docs-writer", "judge", "planner", "researcher",
              "sweeper", "test-writer", "worker")
GUARD = re.compile(r'\[ -e "\$\{CLAUDE_HOME:-\$HOME/\.claude\}/router/install-manifest\.json" \] \|\| '
                   r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/router/([a-z_]+\.py)"')
SCRIPT_INSTALL = re.compile(r'python3 "\$HOME/\.claude/hooks/router/([a-z_]+\.py)"')
BRIEF = "TASK: Sweep the repository\nRETURN: Report findings"
OWNER = {"name": "AEIA", "url": "https://aeia.dev"}
REPOSITORY = "https://github.com/aeiadev/router"


def tree_hash(path):
    """Every entry below path: relative name, type and mode, bytes or link target."""
    digest = hashlib.sha256()
    for base, folders, files in os.walk(path):
        folders.sort()
        for name in sorted(folders + files):
            item = Path(base) / name
            info = item.lstat()
            digest.update(str(item.relative_to(path)).encode() + b"\0" + oct(info.st_mode).encode() + b"\0")
            if stat.S_ISLNK(info.st_mode):
                digest.update(os.readlink(item).encode())
            elif stat.S_ISREG(info.st_mode):
                digest.update(item.read_bytes())
            digest.update(b"\1")
    return digest.hexdigest()


def generated_files(root):
    """Relative names of every file under .claude-plugin/ and plugins/ of a checkout."""
    return sorted(str(path.relative_to(root)) for folder in (".claude-plugin", "plugins")
                  for path in (root / folder).rglob("*")
                  if (path.is_file() or path.is_symlink()) and "__pycache__" not in path.parts and path.suffix != ".pyc")


class Scratch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude = self.base / "claude"
        self.env = self.environment(self.claude, "state")

    def environment(self, claude, state, **extra):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("ROUTER_", "CLAUDE_", "CODEX_", "XDG_", "HARNESS_")) and key != "ROUTES_JSON"}
        env.update(HOME=str(self.base / "home"), CLAUDE_HOME=str(claude), CODEX_HOME=str(self.base / "codex"),
                   XDG_CONFIG_HOME=str(self.base / "config"), XDG_STATE_HOME=str(self.base / state),
                   ROUTER_LOCAL="off", PYTHONDONTWRITEBYTECODE="1", **extra)
        return env

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def enable(self, *plugins, settings=None):
        settings = settings or self.claude / "settings.json"
        value = json.loads(settings.read_text()) if settings.exists() else {}
        value["enabledPlugins"] = {f"{name}@router": True for name in plugins}
        self.write_json(settings, value)

    def cache(self, plugin, version, *agents, root=None):
        folder = (root or self.claude / "plugins") / "cache/router" / plugin / version
        (folder / "agents").mkdir(parents=True, exist_ok=True)
        for name in agents:
            (folder / "agents" / f"{name}.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        return folder

    def install(self, env=None, *options):
        result = subprocess.run(["bash", str(ROOT / "install.sh"), "--host", "claude", *options],
                                env=env or self.env, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result


# Fix 1: generated trees, checked for drift.
class Generated(Scratch):
    def copy_repo(self, name="repo"):
        copy = self.base / name
        shutil.copytree(ROOT, copy, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        return copy

    def gen(self, repo, *args):
        return subprocess.run([sys.executable, str(repo / "scripts/gen_plugin.py"), *args], env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def assert_drift(self, repo, relative, kind):
        before = tree_hash(repo)
        result = self.gen(repo, "--check")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"gen_plugin: drift at {relative} ({kind}); run python3 scripts/gen_plugin.py\n"))
        self.assertEqual(tree_hash(repo), before, "--check wrote to the checkout")

    def test_checkout_has_no_drift(self):
        result = self.gen(ROOT, "--check")
        count = len(generated_files(ROOT))
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, f"gen_plugin: {count} generated files match\n", ""))

    def test_drift_in_one_copied_role(self):
        repo = self.copy_repo()
        role = repo / "plugins/shared-roles/agents/judge.md"
        role.write_bytes(role.read_bytes() + b"\n")
        self.assert_drift(repo, "plugins/shared-roles/agents/judge.md", "changed")

    def test_one_extra_file(self):
        repo = self.copy_repo()
        (repo / "plugins/router/agents/extra.md").write_text("---\nname: extra\n---\n")
        self.assert_drift(repo, "plugins/router/agents/extra.md", "extra")

    def test_extra_empty_directories_are_drift(self):
        for relative in ("plugins/router/agents/emptied", "plugins/extra", ".claude-plugin/hollow"):
            with self.subTest(relative=relative):
                repo = self.copy_repo(relative.replace("/", "-"))
                (repo / relative).mkdir(parents=True)
                self.assert_drift(repo, relative, "extra")
                self.assertEqual(self.gen(repo).returncode, 0)
                self.assertFalse((repo / relative).exists())
                self.assertEqual(self.gen(repo, "--check").returncode, 0)

    def test_changed_version(self):
        repo = self.copy_repo()
        (repo / "VERSION").write_text("9.9.9\n")
        self.assert_drift(repo, "plugins/router/.claude-plugin/plugin.json", "changed")

    def test_missing_file_and_symlink(self):
        repo = self.copy_repo()
        (repo / "plugins/router/hooks/hooks.json").unlink()
        self.assert_drift(repo, "plugins/router/hooks/hooks.json", "missing")
        repo2 = self.base / "repo2"
        shutil.copytree(ROOT, repo2, symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        role = repo2 / "plugins/shared-roles/agents/worker.md"
        role.unlink()
        role.symlink_to(repo2 / "agents/worker.md")
        result = self.gen(repo2, "--check")
        self.assertEqual((result.returncode, result.stderr),
                         (1, "gen_plugin: drift at plugins/shared-roles/agents/worker.md (symlink); "
                             "run python3 scripts/gen_plugin.py\n"))

    def test_generation_repairs_drift_and_is_idempotent(self):
        repo = self.copy_repo()
        (repo / "plugins/router/agents/extra.md").write_text("x")
        role = repo / "plugins/shared-roles/agents/judge.md"
        role.write_bytes(b"edited")
        count = len(generated_files(ROOT))
        first = self.gen(repo)
        self.assertEqual((first.returncode, first.stdout, first.stderr),
                         (0, f"gen_plugin: {count} generated files (1 written, 1 removed)\n", ""))
        self.assertEqual(role.read_bytes(), (repo / "agents/judge.md").read_bytes())
        self.assertFalse((repo / "plugins/router/agents/extra.md").exists())
        again = self.gen(repo)
        self.assertEqual(again.stdout, f"gen_plugin: {count} generated files (0 written, 0 removed)\n")
        self.assertEqual(self.gen(repo, "--check").returncode, 0)

    def test_marketplace_and_plugin_manifests(self):
        marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
        self.assertEqual(marketplace, {"name": "router", "owner": OWNER, "plugins": [
            {"name": "router", "source": "./plugins/router"},
            {"name": "shared-roles", "source": "./plugins/shared-roles"}]})
        router = json.loads((ROOT / "plugins/router/.claude-plugin/plugin.json").read_text())
        self.assertEqual(set(router), {"name", "version", "description", "author", "homepage", "repository", "license"})
        self.assertEqual((router["name"], router["version"], router["author"], router["homepage"],
                          router["repository"], router["license"]),
                         ("router", VERSION, OWNER, REPOSITORY, REPOSITORY, "MIT"))
        shared = json.loads((ROOT / "plugins/shared-roles/.claude-plugin/plugin.json").read_text())
        self.assertEqual(set(shared), {"name", "version", "description", "author", "license"})
        self.assertEqual((shared["name"], shared["version"], shared["author"], shared["license"]),
                         ("shared-roles", VERSION, OWNER, "MIT"))

    def test_tier_set_is_exactly_the_eighteen_variants(self):
        routes = json.loads((ROOT / "hooks/router/routes.json").read_text())
        tiers = {spec["agent"] for levels in routes["tiers"].values() for spec in levels.values()}
        found = {path.name for path in (ROOT / "plugins/router/agents").iterdir()}
        self.assertEqual(len(found), 18)
        self.assertEqual(found, {f"{name}.md" for name in tiers})
        self.assertEqual(found, {path.name for path in (ROOT / "agents").glob("*.md")} - {f"{n}.md" for n in BASE_ROLES})
        for name in found:
            self.assertEqual((ROOT / "plugins/router/agents" / name).read_bytes(), (ROOT / "agents" / name).read_bytes())

    def test_shared_roles_equal_agents_and_pins(self):
        folder = ROOT / "plugins/shared-roles/agents"
        self.assertEqual(sorted(path.name for path in folder.iterdir()), sorted(f"{n}.md" for n in BASE_ROLES))
        pins = dict(reversed(line.split("  ", 1)) for line in (ROOT / "agents/SHARED.sha256").read_text().splitlines())
        for name in BASE_ROLES:
            data = (folder / f"{name}.md").read_bytes()
            self.assertEqual(data, (ROOT / "agents" / f"{name}.md").read_bytes(), name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), pins[f"{name}.md"], name)

    def test_shared_roles_tree_has_nothing_router_specific(self):
        files = generated_files(ROOT)
        shared = [name for name in files if name.startswith("plugins/shared-roles/")]
        self.assertEqual(sorted(shared), sorted(["plugins/shared-roles/.claude-plugin/plugin.json"] +
                                                [f"plugins/shared-roles/agents/{n}.md" for n in BASE_ROLES]))
        for name in shared:
            self.assertNotIn(b"router", (ROOT / name).read_bytes().lower(), name)

    def test_router_tree_is_copies_of_the_sources(self):
        hooks = sorted(path.name for path in (ROOT / "hooks/router").iterdir()
                       if path.is_file() and path.suffix != ".pyc" and path.name != "codex_spawn_guard.py")
        tiers = sorted(path.name for path in (ROOT / "plugins/router/agents").iterdir())
        expected = (["plugins/router/.claude-plugin/plugin.json", "plugins/router/bin/router",
                     "plugins/router/hooks/hooks.json", "plugins/router/skills/dispatch/SKILL.md"]
                    + [f"plugins/router/hooks/router/{name}" for name in hooks]
                    + [f"plugins/router/agents/{name}" for name in tiers])
        self.assertEqual(sorted(name for name in generated_files(ROOT) if name.startswith("plugins/router/")),
                         sorted(expected))
        for name in expected:
            path = ROOT / name
            self.assertFalse(path.is_symlink(), name)
            self.assertTrue(stat.S_ISREG(path.lstat().st_mode), name)
        sources = {"plugins/router/bin/router": "bin/router",
                   "plugins/router/skills/dispatch/SKILL.md": "skills/dispatch/SKILL.md",
                   **{f"plugins/router/hooks/router/{name}": f"hooks/router/{name}" for name in hooks}}
        for name, source in sources.items():
            self.assertEqual((ROOT / name).read_bytes(), (ROOT / source).read_bytes(), name)
            self.assertEqual(bool((ROOT / name).stat().st_mode & 0o100), bool((ROOT / source).stat().st_mode & 0o100), name)
        self.assertTrue((ROOT / "plugins/router/bin/router").stat().st_mode & 0o100)


# Fix 2: every plugin hook stands down when a script install exists.
class StandDown(Scratch):
    def hooks(self):
        return json.loads((ROOT / "plugins/router/hooks/hooks.json").read_text())

    def entries(self, value, pattern):
        rows = []
        for event, blocks in value["hooks"].items():
            for block in blocks:
                for hook in block["hooks"]:
                    self.assertEqual(set(hook), {"type", "command"})
                    self.assertEqual(hook["type"], "command")
                    match = pattern.fullmatch(hook["command"])
                    self.assertIsNotNone(match, (event, hook["command"]))
                    rows.append((event, block.get("matcher"), match.group(1)))
        return rows

    def test_every_command_is_guarded_and_matches_the_script_install(self):
        plugin = self.entries(self.hooks(), GUARD)
        script = self.entries(json.loads((ROOT / "examples/settings.example.json").read_text()), SCRIPT_INSTALL)
        self.assertEqual(plugin, script)
        self.assertEqual(set(self.hooks()), {"hooks"})

    def plugin_copy(self):
        folder = self.base / "cache-copy/router/router" / VERSION
        shutil.copytree(ROOT / "plugins/router", folder)
        return folder

    def payload(self):
        return json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "plugin-session",
                           "cwd": str(self.base), "tool_input": {"subagent_type": "sweeper", "prompt": BRIEF}})

    def run_plugin(self, folder, claude, state):
        command = self.hooks()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(GUARD.fullmatch(command).group(1), "spawn_guard.py")
        env = self.environment(claude, state, CLAUDE_PLUGIN_ROOT=str(folder))
        return subprocess.run(["sh", "-c", command], input=self.payload(), env=env, capture_output=True,
                              text=True, timeout=30)

    def test_manifest_present_stands_down_and_absent_runs_like_the_script_install(self):
        folder = self.plugin_copy()
        installed = self.base / "installed"
        install_env = self.environment(installed, "state-installed")
        self.install(install_env)
        scripted = subprocess.run([sys.executable, str(installed / "hooks/router/spawn_guard.py")],
                                  input=self.payload(), env=install_env, capture_output=True, text=True, timeout=30)
        self.assertEqual(scripted.returncode, 0, scripted.stderr)
        output = json.loads(scripted.stdout)["hookSpecificOutput"]
        self.assertEqual(output["updatedInput"], {"subagent_type": "sweeper-light", "model": "haiku", "prompt": BRIEF})
        # The installed home has a manifest: the plugin command must not start Python at all.
        standing = self.run_plugin(folder, installed, "state-standing")
        self.assertEqual((standing.returncode, standing.stdout, standing.stderr), (0, "", ""))
        self.assertFalse((self.base / "state-standing").exists(), "the stood-down hook wrote state")
        # A plugin-only home: the hook runs from the copy and decides as the script install did.
        plugged = self.run_plugin(folder, self.base / "plugin-only", "state-plugin")
        self.assertEqual((plugged.returncode, plugged.stdout, plugged.stderr),
                         (scripted.returncode, scripted.stdout, scripted.stderr))
        self.assertTrue((self.base / "state-plugin").is_dir(), "the plugin hook did not run")

    def test_plugin_copy_reads_its_own_routes(self):
        folder = self.plugin_copy()
        routes_path = folder / "hooks/router/routes.json"
        routes = json.loads(routes_path.read_text())
        routes["tiers"]["sweeper"]["light"]["agent"] = "sweeper-light-copy"
        routes_path.write_text(json.dumps(routes))
        checkout = (ROOT / "hooks/router/routes.json").read_bytes()
        plugged = self.run_plugin(folder, self.base / "plugin-only", "state-plugin")
        self.assertEqual(plugged.returncode, 0, plugged.stderr)
        updated = json.loads(plugged.stdout)["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(updated["subagent_type"], "sweeper-light-copy")
        self.assertEqual((ROOT / "hooks/router/routes.json").read_bytes(), checkout)


# Fix 3: <plugin>:<role> resolves like <role>; plugin-only tiers are emitted with router:.
class Prefixes(Scratch):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = self.base / "project"
        self.project.mkdir()
        self.routes = common.load_routes()
        self.modes = {key: common.effective_mode(self.routes["router"]["modes"].get(
            key, "enforce" if key in ("brief", "rerun") else None)) for key in common.mode_keys(self.routes)}

    def emitted(self, role="sweeper", cwd=None):
        cwd = str(self.project) if cwd is None else cwd
        result = common.decide_spawn({"subagent_type": role, "prompt": BRIEF}, self.routes, self.modes, [], cwd=cwd)
        return result["updated"]["subagent_type"] if result["updated"] else result["run_type"]

    def settings(self, name, value, layer="user"):
        path = (self.claude if layer == "user" else self.project / ".claude") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value if isinstance(value, bytes) else
                         value.encode() if isinstance(value, str) else json.dumps(value).encode())

    def agent_file(self, folder, name="sweeper-light"):
        (folder / "agents").mkdir(parents=True, exist_ok=True)
        (folder / "agents" / f"{name}.md").write_text(f"---\nname: {name}\n---\n")

    def test_each_known_prefix_resolves_like_the_bare_role(self):
        for prefix in ("router", "harness", "shared-roles", "Router", "SHARED-ROLES", "Harness"):
            with self.subTest(prefix=prefix):
                self.assertEqual(common.canonical_role(f"{prefix}:builder", self.routes), "builder")
                self.assertEqual(common.canonical_role(f" {prefix}:Explore ", self.routes), "researcher")
                tier = common.classify(f"{prefix}:Builder-Std", self.routes)
                self.assertEqual((tier["base"], tier["tier"], tier["class"]), ("builder", "std", "exec"))
                self.assertEqual(common.classify(f"{prefix}:judge", self.routes)["base"], "judge")
                problem = common.brief_problem("no fields here", f"{prefix}:builder", self.routes)
                self.assertIsNotNone(problem)
                self.assertEqual(problem, common.brief_problem("no fields here", "builder", self.routes))

    def test_unknown_prefix_stays_unknown(self):
        for name in ("acme:builder", "routerx:builder", ":builder", "router:", "toolkit:judge-std"):
            with self.subTest(name=name):
                self.assertEqual(common.canonical_role(name, self.routes), name)
                self.assertIsNone(common.classify(name, self.routes))
        result = common.decide_spawn({"subagent_type": "acme:builder", "prompt": BRIEF}, self.routes, self.modes, [])
        self.assertEqual((result["rule"], result["class"]), ("inject-unlisted", None))

    def test_tier_only_in_enabled_plugin_is_emitted_with_the_prefix(self):
        self.enable("router")
        self.assertEqual(self.emitted(), "router:sweeper-light")
        self.assertEqual(self.emitted("router:sweeper"), "router:sweeper-light")
        result = common.decide_spawn({"subagent_type": "router:sweeper-light", "prompt": BRIEF},
                                     self.routes, self.modes, [], cwd=str(self.project))
        self.assertEqual((result["decision"], result["rule"]), ("rewrite", "redirect"))
        self.assertEqual(result["updated"]["subagent_type"], "router:sweeper-light")

    def test_cached_plugin_alone_keeps_the_bare_name(self):
        self.cache("router", VERSION, "sweeper-light")
        self.assertEqual(self.emitted(), "sweeper-light")
        self.enable("shared-roles")
        self.assertEqual(self.emitted(), "sweeper-light")
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"router@router": False}})
        self.assertEqual(self.emitted(), "sweeper-light")

    def test_user_agent_file_or_other_plugin_keeps_the_bare_name(self):
        self.assertEqual(self.emitted(), "sweeper-light")
        self.enable("shared-roles")
        self.cache("other", VERSION, "sweeper-light")
        self.assertEqual(self.emitted(), "sweeper-light")
        self.enable("router")
        self.assertEqual(self.emitted(), "router:sweeper-light")
        self.agent_file(self.claude)
        self.assertEqual(self.emitted(), "sweeper-light")
        self.assertEqual(self.emitted("router:sweeper"), "sweeper-light")

    def test_project_agent_file_keeps_the_bare_name(self):
        self.enable("router")
        self.assertEqual(self.emitted(), "router:sweeper-light")
        self.agent_file(self.project / ".claude")
        self.assertEqual(self.emitted(), "sweeper-light")
        self.assertEqual(self.emitted("router:sweeper"), "sweeper-light")
        self.assertEqual(self.emitted(cwd=str(self.project / "sub")), "sweeper-light")
        self.assertEqual(self.emitted(cwd=str(self.base)), "router:sweeper-light")

    def test_later_settings_layer_wins(self):
        self.enable("router")
        self.assertEqual(self.emitted(), "router:sweeper-light")
        self.settings("settings.local.json", {"enabledPlugins": {"router@router": False}}, "project")
        self.assertEqual(self.emitted(), "sweeper-light")
        self.settings("settings.local.json", {"model": "x"}, "project")
        self.assertEqual(self.emitted(), "router:sweeper-light")
        self.settings("settings.json", {"enabledPlugins": {"router@router": False}}, "project")
        self.assertEqual(self.emitted(), "sweeper-light")
        self.settings("settings.local.json", {"enabledPlugins": {"router@router": True}}, "project")
        self.assertEqual(self.emitted(), "router:sweeper-light")
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"router@router": False}})
        self.settings("settings.json", {}, "project")
        self.settings("settings.local.json", {}, "project")
        self.assertEqual(self.emitted(), "sweeper-light")
        self.settings("settings.json", {"enabledPlugins": {"router@router": True}}, "project")
        self.assertEqual(self.emitted(), "router:sweeper-light")

    def test_project_settings_come_from_the_walked_project_root(self):
        sub = self.project / "sub"
        sub.mkdir()
        self.enable("router")
        self.settings("settings.local.json", {"enabledPlugins": {"router@router": False}}, "project")
        self.assertEqual(self.emitted(cwd=str(sub)), "sweeper-light")
        self.assertEqual(self.emitted(cwd=str(sub / "deeper")), "sweeper-light")
        self.write_json(self.claude / "settings.json", {})
        self.settings("settings.local.json", {}, "project")
        self.settings("settings.json", {"enabledPlugins": {"router@router": True}}, "project")
        self.assertEqual(self.emitted(cwd=str(sub)), "router:sweeper-light")

    def test_claude_project_dir_names_the_project_root(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.enable("router")
        self.settings("settings.local.json", {"enabledPlugins": {"router@router": False}}, "project")
        self.assertEqual(self.emitted(cwd=str(outside)), "router:sweeper-light")
        with mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=str(self.project)):
            self.assertEqual(self.emitted(cwd=str(outside)), "sweeper-light")
            self.settings("settings.local.json", {}, "project")
            self.assertEqual(self.emitted(cwd=str(outside)), "router:sweeper-light")
            self.agent_file(self.project / ".claude")
            self.assertEqual(self.emitted(cwd=str(outside)), "sweeper-light")

    def test_missing_claude_project_dir_falls_back_without_a_traceback(self):
        sub = self.project / "sub"
        sub.mkdir()
        self.enable("router")
        self.settings("settings.local.json", {"enabledPlugins": {"router@router": False}}, "project")
        for value in (str(self.base / "missing"), str(self.project / ".claude" / "settings.local.json"), ""):
            with self.subTest(value=value):
                env = dict(self.env, CLAUDE_PROJECT_DIR=value)
                self.assertEqual(self.hook_output(sub, str(sub), env=env), "sweeper-light")
                with mock.patch.dict(os.environ, CLAUDE_PROJECT_DIR=value):
                    self.assertEqual(self.emitted(cwd=str(sub)), "sweeper-light")

    def test_relative_claude_project_dir_is_ignored(self):
        outside = self.base / "outside"
        outside.mkdir()
        relative = self.base / "relative-project"
        self.write_json(relative / ".claude/settings.local.json",
                        {"enabledPlugins": {"router@router": False}})
        self.enable("router")
        self.assertEqual(self.hook_output(self.base, str(outside)), "router:sweeper-light")
        env = dict(self.env, CLAUDE_PROJECT_DIR="relative-project")
        self.assertEqual(self.hook_output(self.base, str(outside), env=env), "router:sweeper-light")

    def test_default_user_claude_home_is_never_project_settings(self):
        home = self.base / "home"
        (home / "work").mkdir(parents=True)
        other = self.base / "other-claude"
        self.write_json(other / "settings.json", {"enabledPlugins": {"router@router": True}})
        self.write_json(home / ".claude/settings.local.json",
                        {"enabledPlugins": {"router@router": False}})
        env = dict(self.env, CLAUDE_HOME=str(other))
        self.assertEqual(self.hook_output(home / "work", str(home / "work"), env=env),
                         "router:sweeper-light")

    def test_the_user_claude_home_is_not_a_project_root(self):
        home = self.base / "home"
        self.write_json(home / ".claude" / "settings.json", {"enabledPlugins": {"router@router": True}})
        self.write_json(home / ".claude" / "settings.local.json", {"enabledPlugins": {"router@router": False}})
        (home / "work").mkdir()
        with mock.patch.dict(os.environ, CLAUDE_HOME=str(home / ".claude")):
            self.assertEqual(self.emitted(cwd=str(home / "work")), "router:sweeper-light")

    def test_malformed_settings_in_each_layer_keep_the_bare_name(self):
        layers = (("settings.json", "user"), ("settings.json", "project"), ("settings.local.json", "project"))
        bad = ("{", "[]", '{"enabledPlugins": []}', '{"enabledPlugins": {"router@router": "yes"}}', b"\xff\xfe")
        for name, layer in layers:
            for text in bad:
                with self.subTest(layer=layer, name=name, text=text):
                    shutil.rmtree(self.claude, ignore_errors=True)
                    shutil.rmtree(self.project / ".claude", ignore_errors=True)
                    self.settings(name, text, layer)
                    self.assertEqual(self.emitted(), "sweeper-light")
        shutil.rmtree(self.claude, ignore_errors=True)
        (self.claude / "settings.json").mkdir(parents=True)
        self.assertEqual(self.emitted(), "sweeper-light")

    def test_malformed_layer_is_ignored_and_the_others_still_count(self):
        self.enable("router")
        self.settings("settings.local.json", "{", "project")
        self.assertEqual(self.emitted(), "router:sweeper-light")
        self.settings("settings.json", '{"enabledPlugins": {"router@router": "yes"}}', "project")
        self.assertEqual(self.emitted(), "router:sweeper-light")

    def test_spawn_hook_emits_the_prefixed_name(self):
        self.enable("router")
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s",
                              "cwd": str(self.project), "tool_input": {"subagent_type": "sweeper", "prompt": BRIEF}})

        def run():
            result = subprocess.run([sys.executable, str(ROOT / "hooks/router/spawn_guard.py")], input=payload,
                                    env=self.env, cwd=self.project, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(run(), {"subagent_type": "router:sweeper-light", "model": "haiku", "prompt": BRIEF})
        self.agent_file(self.project / ".claude")
        self.assertEqual(run(), {"subagent_type": "sweeper-light", "model": "haiku", "prompt": BRIEF})

    def hook_output(self, process_cwd, payload_cwd, present=True, env=None):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "session_id": "s",
                   "tool_input": {"subagent_type": "sweeper", "prompt": BRIEF}}
        if present:
            payload["cwd"] = payload_cwd
        result = subprocess.run([sys.executable, str(ROOT / "hooks/router/spawn_guard.py")], input=json.dumps(payload),
                                env=env or self.env, cwd=process_cwd, capture_output=True, text=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["subagent_type"]

    def test_spawn_hook_uses_the_payload_cwd_not_the_process_cwd(self):
        self.enable("router")
        self.agent_file(self.project / ".claude")
        empty = self.base / "empty"
        empty.mkdir()
        self.assertEqual(self.hook_output(empty, str(self.project)), "sweeper-light")
        self.assertEqual(self.hook_output(self.project, str(empty)), "router:sweeper-light")

    def test_spawn_hook_falls_back_to_the_process_cwd(self):
        self.enable("router")
        self.agent_file(self.project / ".claude")
        for value in (None, 7, "", ["x"]):
            with self.subTest(cwd=value):
                self.assertEqual(self.hook_output(self.project, value), "sweeper-light")
        self.assertEqual(self.hook_output(self.project, None, present=False), "sweeper-light")


# Fix 4: install.sh --status reports plugin state, read-only.
class Status(Scratch):
    def status(self, *options):
        return subprocess.run(["bash", str(ROOT / "install.sh"), "--status", *options], env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)

    def lines(self, *options):
        before = tree_hash(self.base)
        result = self.status(*options)
        self.assertEqual(tree_hash(self.base), before, "--status wrote")
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)
        return result.stdout.splitlines()

    def test_not_enabled(self):
        self.assertEqual(self.lines(), ["plugin: router not enabled, not cached",
                                        "plugin: shared-roles not enabled, not cached"])
        self.assertFalse(self.claude.exists())

    def test_enabled_cached_stale_and_script_install(self):
        self.enable("router", "shared-roles")
        self.cache("router", VERSION)
        self.cache("shared-roles", "0.2.0")
        self.assertEqual(self.lines(), [f"plugin: router enabled, cached {VERSION} (current)",
                                        f"plugin: shared-roles enabled, cached 0.2.0 (stale, checkout {VERSION})"])
        self.install()
        self.enable("router", "shared-roles")
        self.assertEqual(self.lines(), [
            f"plugin: router enabled, cached {VERSION} (current); stands down: script install present",
            f"plugin: shared-roles enabled, cached 0.2.0 (stale, checkout {VERSION})",
            f"plugin: roles in both {self.claude / 'agents'} and shared-roles: " + ", ".join(BASE_ROLES)
            + " (doubled descriptions cost tokens every turn)"])

    def test_malformed_settings_give_one_line(self):
        settings = self.claude / "settings.json"
        settings.parent.mkdir(parents=True)
        for text, error in (("{", "is not valid JSON"), ("[]", "is not a JSON object"),
                            ('{"enabledPlugins": [1]}', "enabledPlugins is not an object"),
                            ('{"enabledPlugins": {"router@router": "yes"}}',
                             'enabledPlugins["router@router"] is not true or false')):
            with self.subTest(text=text):
                settings.write_text(text)
                self.assertEqual(self.lines(), [f"plugin: {settings} {error}",
                                                "plugin: router enabled unknown, not cached",
                                                "plugin: shared-roles enabled unknown, not cached"])

    def test_combined_options_are_one_line_errors(self):
        for option in ("--uninstall", "--purge", "--with-defaults", "--dry-run"):
            with self.subTest(option=option):
                before = tree_hash(self.base)
                result = self.status(option)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", "router install: --status cannot be combined with other options\n"))
                self.assertEqual(tree_hash(self.base), before)


if __name__ == "__main__":
    unittest.main()
