"""The shared capability catalog is static evidence, not a host probe."""
import builtins
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks/router"))

IDS = ("role_discovery", "model_pins_inheritance", "hook_payload_fields",
       "trusted_hook_lifecycle", "tool_rewrites", "completion_events",
       "tool_visibility", "read_only_enforcement", "usage_formats")


class CapabilityTests(unittest.TestCase):
    def test_matrix_and_versions(self):
        import capabilities
        for package in ("router", "harness"):
            for host, reference in (("claude", "2.1.296"), ("codex", "0.156.1")):
                for value, source, matched in ((None, "unavailable", False),
                                               (reference, "caller-supplied", True),
                                               ("999.0.0", "caller-supplied", False),
                                               ("1.2.3\nunsafe", "invalid", False),
                                               ("1" * 33 + ".0.0", "invalid", False),
                                               (123, "invalid", False)):
                    report = capabilities.describe(host, value, package)
                    self.assertEqual(set(report), {"schema_version", "package", "host", "host_version",
                                                  "version_source", "reference_version", "reference_match",
                                                  "host_verified", "features"})
                    self.assertEqual((report["schema_version"], report["package"], report["host"]),
                                     (1, package, host))
                    self.assertEqual((report["version_source"], report["reference_version"],
                                      report["reference_match"], report["host_verified"]),
                                     (source, reference, matched, False))
                    self.assertEqual(report["host_version"], value if source == "caller-supplied" else None)
                    self.assertEqual(tuple(f["id"] for f in report["features"]), IDS)
                    self.assertTrue(all(set(f) == {"id", "adapter_support", "evidence", "host_verified",
                                                    "detail", "action", "fields"} and not f["host_verified"]
                                        for f in report["features"]))
                    rows = capabilities.render_rows(report)
                    self.assertEqual([name for name, _ in rows],
                                     ["capability version", *("capability " + item for item in IDS)])
                    self.assertTrue(all("unverified" in detail for _, detail in rows))

    def test_source_supported_matrix_and_fields(self):
        import capabilities
        matrix = {(p, h): {f["id"]: f for f in capabilities.describe(h, package=p)["features"]}
                  for p in ("router", "harness") for h in ("claude", "codex")}
        self.assertEqual(matrix["router", "claude"]["tool_rewrites"]["adapter_support"], "fixture-supported")
        self.assertEqual(matrix["router", "codex"]["tool_rewrites"]["adapter_support"], "not-used")
        self.assertEqual(matrix["router", "codex"]["completion_events"]["adapter_support"], "unavailable")
        self.assertIn("tool_input.task_name", matrix["router", "codex"]["hook_payload_fields"]["fields"])
        self.assertNotIn("tool_input.message", matrix["router", "codex"]["hook_payload_fields"]["fields"])
        self.assertIn("tool_response", matrix["harness", "claude"]["hook_payload_fields"]["fields"])
        for features in matrix.values():
            for name in ("trusted_hook_lifecycle", "tool_visibility", "read_only_enforcement"):
                self.assertEqual((features[name]["adapter_support"], features[name]["evidence"]),
                                 ("unavailable", "unverified"))

    def test_independent_and_no_external_operations(self):
        import capabilities
        with mock.patch.object(builtins, "open", side_effect=AssertionError("file access")), \
             mock.patch.object(os, "getenv", side_effect=AssertionError("environment")), \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("process")), \
             mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            first = capabilities.describe("claude")
            second = capabilities.describe("claude")
            first["features"][0]["fields"].append("private")
            self.assertNotIn("private", second["features"][0]["fields"])
            self.assertEqual(len(capabilities.render_rows(second)), 10)
        for kwargs in ({"host": "other"}, {"host": "claude", "package": "other"}):
            with self.assertRaisesRegex(ValueError, "^invalid host or package$"):
                capabilities.describe(**kwargs)
        with self.assertRaisesRegex(ValueError, "^invalid host or package$"):
            capabilities.describe([])


if __name__ == "__main__":
    unittest.main()
