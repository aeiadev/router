#!/usr/bin/env python3
"""Example overlays validate and change real hook behavior."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PRESETS = ROOT / "examples/presets"
sys.path.insert(0, str(ROOT / "hooks/router"))
import common

EXPECTED = {
    "careful": {"router": {"modes": {"risk": "enforce"}}, "context": {"modes": {"stop_cap_research": "enforce", "large_read": "enforce"}}},
    "medium": {"context": {"caps": {"research": 4000, "worker": 4000}, "modes": {"stop_cap_research": "enforce"}}},
    "small": {"context": {"caps": {"exec": 800, "judge": 800, "sweep": 1500, "research": 2500, "worker": 2500}, "chain": {"first": 4, "repeat": 8}, "large_read_bytes": 12000, "modes": {"stop_cap_research": "enforce", "large_read": "enforce"}}},
}

def contains(actual, expected):
    for key, value in expected.items():
        if isinstance(value, dict):
            contains(actual[key], value)
        else:
            assert actual[key] == value, (key, actual[key], value)

class PresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / "config"),
                        ROUTER_STATE=str(self.home / "state"), ROUTER_OFF_FILE=str(self.home / "OFF"),
                        ROUTER_LOCAL="off", PYTHONDONTWRITEBYTECODE="1")

    def cli(self, path):
        return subprocess.run([sys.executable, str(ROOT / "bin/router"), "config", "check"],
                              env=dict(self.env, ROUTER_LOCAL=str(path)), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)

    def test_all_presets_are_minimal_valid_overlays(self):
        self.assertEqual({p.name for p in PRESETS.iterdir()}, {name + ".local.json" for name in EXPECTED})
        for name, expected in EXPECTED.items():
            path = PRESETS / (name + ".local.json")
            data = path.read_bytes()
            self.assertLessEqual(len(data), 65536)
            self.assertIsInstance(json.loads(data), dict)
            self.assertEqual(json.loads(data), expected)
            result = self.cli(path)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, f"overlay ok: {path}\n", ""))
            with patch.dict(os.environ, dict(self.env, ROUTER_LOCAL=str(path))):
                routes, problem = common.load_routes_checked()
            self.assertIsNone(problem)
            contains(routes, expected)

    def test_invalid_chain_overlays(self):
        small = json.loads((PRESETS / "small.local.json").read_text())
        for key, value in (("first", 0), ("pressure", {"urgent": {"chain_first": 5}})):
            changed = json.loads(json.dumps(small))
            if key == "first":
                changed["context"]["chain"]["first"] = value
            else:
                changed["context"]["pressure"] = value
            path = self.home / (key + ".local.json")
            path.write_text(json.dumps(changed))
            result = self.cli(path)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, "")
            self.assertIsNotNone(re.fullmatch(r"overlay invalid: " + re.escape(str(path)) + r": .+\n", result.stderr))

    def test_small_changes_real_read_guard(self):
        big = self.home / "big.txt"
        big.write_bytes(b"x" * 13000)
        event = {"hook_event_name": "PreToolUse", "session_id": "preset-test", "tool_name": "Read",
                 "tool_input": {"file_path": str(big)}}
        results = []
        for local in ("off", str(PRESETS / "small.local.json")):
            env = dict(self.env, ROUTER_LOCAL=local, ROUTER_STATE=str(self.home / ("state-off" if local == "off" else "state-small")))
            results.append(subprocess.run([sys.executable, str(ROOT / "hooks/router/context_guard.py")],
                                          input=json.dumps(event), env=env, capture_output=True, text=True, timeout=10))
        self.assertEqual([r.returncode for r in results], [0, 2])
        self.assertIn("offset and limit", results[1].stderr)

if __name__ == "__main__":
    unittest.main()
