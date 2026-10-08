#!/usr/bin/env python3
"""Check the delegation contract against the route table and hook decisions.

These exercise the public routing contract with repository fixtures and temporary
state. They need no host installation.
"""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "hooks/router"
sys.path.insert(0, str(HOOKS))


def import_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def contract_errors(routes):
    """The public delegation promises, independent of the hook implementation."""
    errors = []
    types = routes["router"]["types"]
    tiers = routes["tiers"]
    expected = {"sweeper": "sweep", "builder": "exec", "builder-in-place": "exec",
                "judge": "judge", "researcher": "research", "planner": "research",
                "worker": "worker"}
    for name, kind in expected.items():
        if types.get(name, {}).get("class") != kind:
            errors.append(f"{name} class differs from the contract")
    if tiers["sweeper"]["light"]["model"] != "haiku":
        errors.append("sweep default must use haiku")
    for name in ("builder", "builder-in-place"):
        for tier in ("light", "std"):
            if tiers[name][tier]["model"] != "sonnet":
                errors.append(f"{name} {tier} must use sonnet")
        if tiers[name]["up"]["model"] != "opus":
            errors.append(f"{name} up must use opus")
    if any(spec["model"] != "opus" for spec in tiers["judge"].values()):
        errors.append("every judge tier must use opus")
    if routes["router"]["modes"].get("ladder") != "enforce":
        errors.append("ladder must enforce by default")
    if routes["router"]["up_codes"] != ["risk", "security", "novel", "cross-cutting", "ladder"]:
        errors.append("up codes differ from the contract")
    return errors


class ContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="router-contract-test-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        env = patch.dict(os.environ, HOME=str(self.tmp / "home"),
                         CLAUDE_HOME=str(self.tmp / "claude"), ROUTER_HOME=str(self.tmp / "router"),
                         ROUTER_STATE=str(self.tmp / "state"), ROUTER_OFF_FILE=str(self.tmp / "OFF"),
                         ROUTER_OFF="0", ROUTES_JSON=str(HOOKS / "routes.json"))
        env.start()
        self.addCleanup(env.stop)
        self.common = import_module("contract_common", HOOKS / "common.py")
        self.spawn = import_module("contract_spawn", HOOKS / "spawn_guard.py")
        self.routes = self.common.load_routes()
        self.modes = self.routes["router"]["modes"]

    def decide(self, agent, prompt="Find a bounded answer", prior=None, **fields):
        tool_input = dict(subagent_type=agent, prompt=prompt, **fields)
        result = self.spawn.decide(tool_input, self.routes, self.modes, prior or [])
        self.assertNotEqual(result["decision"], "block", result)
        return result["updated"] or tool_input

    def test_public_delegation_contract(self):
        self.assertEqual(contract_errors(self.routes), [])

    def test_contract_rejects_drift_with_specific_diagnostics(self):
        mutations = [
            (lambda r: r["tiers"]["sweeper"]["light"].update(model="sonnet"), "sweep"),
            (lambda r: r["tiers"]["builder"]["std"].update(model="haiku"), "builder std"),
            (lambda r: r["tiers"]["judge"]["up"].update(model="sonnet"), "judge"),
            (lambda r: r["router"]["modes"].update(ladder="shadow"), "ladder"),
            (lambda r: r["router"]["up_codes"].append("unknown"), "up codes"),
        ]
        for mutate, needle in mutations:
            with self.subTest(needle=needle):
                changed = copy.deepcopy(self.routes)
                mutate(changed)
                errors = contract_errors(changed)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(needle, errors[0])

    def test_declared_agent_names_cover_the_public_seats_and_tiers(self):
        expected = {f"{seat}-{tier}" for seat in
                    ("sweeper", "builder", "builder-in-place", "judge")
                    for tier in ("light", "std", "up")}
        expected.update(f"{base}-{tier}" for base in ("researcher", "planner", "worker")
                        for tier in ("std", "up"))
        actual = [spec["agent"] for levels in self.routes["tiers"].values() for spec in levels.values()]
        self.assertEqual(set(actual), expected)
        self.assertEqual(len(actual), len(expected), "duplicate tier agent names")

    def test_default_models_match_the_contract(self):
        for agent, model in (("sweeper", "haiku"), ("builder", "sonnet"),
                             ("builder-in-place", "sonnet"), ("judge", "opus"),
                             ("researcher", "sonnet"), ("planner", "sonnet"),
                             ("worker", "sonnet")):
            with self.subTest(agent=agent):
                updated = self.decide(agent)
                self.assertEqual(updated["model"], model)
                if agent == "builder":
                    self.assertEqual(updated.get("isolation"), "worktree")
                else:
                    self.assertNotIn("isolation", updated)

    def test_variants_match_route_table_models_and_judges_are_fresh(self):
        for base, levels in self.routes["tiers"].items():
            for tier, spec in levels.items():
                with self.subTest(agent=spec["agent"]):
                    prompt = "route: up ladder\nFind an answer" if tier == "up" else "Find an answer"
                    prior = [{"tier": "std"}, {"tier": "std"}] if (
                        tier == "up" and self.routes["router"]["types"][base]["class"] == "exec"
                    ) else []
                    updated = self.decide(spec["agent"], prompt, prior=prior, resume="previous-context")
                    self.assertEqual(updated["model"], spec["model"])
                    self.assertEqual(updated["subagent_type"], spec["agent"])
                    if base == "judge":
                        self.assertNotIn("resume", updated)
                    if base == "builder":
                        self.assertEqual(updated.get("isolation"), "worktree")
                    else:
                        self.assertNotIn("isolation", updated)

    def test_exec_brief_ladder_overrides_early_escalation(self):
        prompt = "route: up risk\nTASK Repair parser\nFILES src/parser.py"
        request = {"subagent_type": "builder-up", "model": "opus", "prompt": prompt}
        for prior in ([], [{"round": 1}]):
            result = self.spawn.decide(request, self.routes, self.modes, prior)
            self.assertEqual(result["decision"], "rewrite")
            self.assertEqual(result["updated"]["model"], "sonnet")
            self.assertEqual(result["updated"]["isolation"], "worktree")
        prior = [{"round": 1}, {"round": 2}]
        third = self.spawn.decide(request, self.routes, self.modes, prior)
        self.assertEqual(third["decision"], "block")
        self.assertIn("route: up ladder", third["message"])
        request["prompt"] = prompt.replace("route: up risk", "route: up ladder")
        third = self.spawn.decide(request, self.routes, self.modes, prior)
        self.assertEqual(third["updated"]["model"], "opus")
        fourth = self.spawn.decide(request, self.routes, self.modes, prior + [{"round": 3}])
        self.assertEqual(fourth["decision"], "block")
        self.assertIn("planning", fourth["message"])
        self.assertIn("owner", fourth["message"])

    def test_return_contract_is_present_for_every_routed_class(self):
        caps = self.routes["context"]["caps"]
        for entry in self.routes["router"]["types"].values():
            self.assertIsInstance(caps[entry["class"]], int)
            self.assertGreater(caps[entry["class"]], 0)
        self.assertEqual(self.routes["context"]["modes"]["stop_cap_seats"], "enforce")
        self.assertEqual(self.routes["context"]["modes"]["start_note"], "enforce")

    def test_decisions_do_not_mutate_caller_data_or_write_state(self):
        original_routes = copy.deepcopy(self.routes)
        request = {"subagent_type": "judge", "prompt": "Check result", "model": "haiku",
                   "resume": "previous-context", "description": "Review", "metadata": {"key": "value"}}
        original_request = copy.deepcopy(request)
        result = self.spawn.decide(request, self.routes, self.modes, [])
        self.assertEqual(result["updated"]["model"], "opus")
        self.assertNotIn("resume", result["updated"])
        self.assertEqual(result["updated"]["metadata"], {"key": "value"})
        self.assertEqual(request, original_request)
        self.assertEqual(self.routes, original_routes)
        self.assertFalse((self.tmp / "state").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
