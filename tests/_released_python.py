"""Interpreter shim for Router's 0.1 and 0.2 installer fixtures."""
import atexit
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from _platform import python_shim


def released_env(env, cache):
    """Prepend a python3 shim whose interpreter actually meets the releases' 3.10 floor."""
    if not cache:
        found = sys.executable if sys.version_info >= (3, 10) else None
        if found is None:
            search = os.environ.get("PATH", os.defpath)
            names = [f"python3.{minor}" for minor in range(20, 9, -1)] + ["python3", "python", "py"]
            for name in names:
                candidate = shutil.which(name, path=search)
                if not candidate:
                    continue
                args = [candidate] + (["-3"] if name == "py" else [])
                try:
                    result = subprocess.run(args + ["-c", "import sys; print(sys.executable) if sys.version_info >= "
                                                    "(3, 10) else sys.exit(1)"],
                                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if result.returncode == 0 and result.stdout.strip():
                    found = result.stdout.strip()
                    break
        if found is None:
            raise unittest.SkipTest("the 0.1 and 0.2 release installers need Python 3.10 or newer; none is on PATH")
        folder = Path(tempfile.mkdtemp(prefix="router-released-python-"))
        atexit.register(shutil.rmtree, folder, True)
        python_shim(folder, found)
        cache.append(str(folder))
    return dict(env, PATH=cache[0] + os.pathsep + env.get("PATH", os.defpath))
