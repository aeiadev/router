#!/usr/bin/env python3
"""Windows may expose fchmod without supporting private POSIX permission bits."""
import importlib.util
import os
from pathlib import Path, PureWindowsPath
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ROUTER = (ROOT / "hooks/router").is_dir()
SOURCE = ROOT / ("hooks/router/compat.py" if ROUTER else "hooks/checkpoint/compat.py")


class WindowsPermissions(unittest.TestCase):
    def test_windows_fchmod_is_not_a_posix_permission_capability(self):
        spec = importlib.util.spec_from_file_location("permission_compat", SOURCE)
        compat = importlib.util.module_from_spec(spec)
        with patch.object(os, "name", "nt"), patch.object(os, "fchmod", create=True) as chmod:
            spec.loader.exec_module(compat)
            if ROUTER:
                self.assertFalse(compat.CAN_FCHMOD)
            compat.set_private(123, 0o600)
            chmod.assert_not_called()

    def test_incomplete_unc_state_paths_fall_back_on_every_python_version(self):
        spec = importlib.util.spec_from_file_location("state_compat", SOURCE)
        compat = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compat)
        with patch.object(os, "name", "nt"), patch.object(compat, "Path", PureWindowsPath):
            for value in ("//", "//server", "//server/", "//server//share"):
                with self.subTest(value=value):
                    self.assertFalse(compat.state_is_absolute(value))
            for value in ("//server/share", "//server/share/folder", "C:/state"):
                with self.subTest(value=value):
                    self.assertTrue(compat.state_is_absolute(value))


if __name__ == "__main__":
    unittest.main()
