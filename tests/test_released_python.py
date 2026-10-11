#!/usr/bin/env python3
"""The settings-rule fixture must launch old releases with a verified Python 3.10+."""
from pathlib import Path
import importlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import _released_python as helper


class ReleasedPython(unittest.TestCase):
    def test_all_legacy_call_sites_probe_before_selecting_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            def which(name, path):
                return {"python3.10": "old.exe", "python": "new.exe"}.get(name)

            def probe(args, **kwargs):
                self.assertIn("sys.version_info >= (3, 10)", args[-1])
                return subprocess.CompletedProcess(args, 1 if args[0] == "old.exe" else 0,
                                                   "" if args[0] == "old.exe" else "new.exe\n", "")

            for name in ("test_install", "test_upgrade", "test_install_modes", "test_docs",
                         "test_doctor", "test_settings_rule"):
                with self.subTest(module=name):
                    module = importlib.import_module(name)
                    with patch.object(helper.sys, "version_info", (3, 9)), \
                            patch.object(module, "RELEASED_PYTHON", []), \
                            patch.object(helper, "python_shim") as writer, \
                            patch.object(helper.tempfile, "mkdtemp", return_value=str(Path(directory) / "shim")), \
                            patch.object(helper.atexit, "register"), \
                            patch.object(helper.shutil, "which", side_effect=which), \
                            patch.object(helper.subprocess, "run", side_effect=probe):
                        env = module.released_env({"PATH": "original"})
                        self.assertEqual(env["PATH"], str(Path(directory) / "shim") + helper.os.pathsep + "original")
                        writer.assert_called_once_with(Path(directory) / "shim", "new.exe")


if __name__ == "__main__":
    unittest.main()
