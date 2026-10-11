#!/usr/bin/env python3
"""Shipped Python must run on Python 3.9, the python3 of a stock macOS.

Every shipped Python file and every Python heredoc in a shipped script is parsed with the 3.9
grammar. Constructs that parse on 3.9 but fail when they run are flagged too: a `|` union in an
annotation without `from __future__ import annotations`, a `|` union passed to isinstance or
issubclass, and a short list of 3.10+ standard-library names. Nothing here calls a model or the
network.
"""
import ast
from pathlib import Path
import re
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
FLOOR = (3, 9)
HEREDOC = re.compile(r"<<'PY'\n(.*?)\nPY\n", re.S)
NEWER_NAMES = {  # module: names added after 3.9
    "itertools": {"pairwise", "batched"},
    "datetime": {"UTC"},
    "contextlib": {"chdir"},
    "enum": {"StrEnum"},
    "hashlib": {"file_digest"},
    "typing": {"TypeAlias", "ParamSpec", "Concatenate", "TypeGuard", "Self", "Never", "LiteralString",
               "Required", "NotRequired", "Unpack", "assert_never", "reveal_type", "override"},
}
NEWER_MODULES = {"tomllib"}
NEWER_KEYWORDS = {"zip": {"strict"}, "dataclass": {"slots", "kw_only"}, "rmtree": {"onexc"},
                  "TemporaryDirectory": {"ignore_cleanup_errors"}}


def shipped_sources():
    """(label, text) for each shipped Python file and each Python heredoc of a shipped script."""
    files = [path for folder in ("hooks", "scripts", "plugins") for path in (ROOT / folder).rglob("*.py")]
    for folder in [ROOT / "bin", *(ROOT / "plugins").glob("*/bin")]:
        files += [path for path in folder.iterdir()
                  if path.is_file() and (path.read_bytes().split(b"\n", 1)[0].startswith(b"#!/usr/bin/env")
                                         and b"python3" in path.read_bytes().split(b"\n", 1)[0]
                                         or b"\n" in path.read_bytes())]  # sh/Python polyglot via pyrun.sh
    sources = [(path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")) for path in sorted(files)]
    for script in [ROOT / "install.sh", *sorted((ROOT / "scripts").glob("*.sh"))]:
        for index, match in enumerate(HEREDOC.finditer(script.read_text(encoding="utf-8"))):
            sources.append((f"{script.relative_to(ROOT).as_posix()} heredoc {index + 1}", match.group(1)))
    return sources


def _union(node):
    return node is not None and any(isinstance(item, ast.BinOp) and isinstance(item.op, ast.BitOr)
                                    for item in ast.walk(node))


def _guarded_import(tree):
    """Import nodes inside a try that catches ImportError (an optional dependency)."""
    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                handler.type is None or "ImportError" in ast.dump(handler.type) for handler in node.handlers):
            guarded.update(id(item) for statement in node.body for item in ast.walk(statement))
    return guarded


def problems(label, text):
    """Lines that would fail on Python 3.9, as 'label:line: reason'."""
    try:
        tree = ast.parse(text, filename=label, feature_version=FLOOR)
    except SyntaxError as exc:
        return [f"{label}:{exc.lineno}: not Python 3.9 syntax: {exc.msg}"]
    found = []
    future = any(isinstance(node, ast.ImportFrom) and node.module == "__future__"
                 and any(alias.name == "annotations" for alias in node.names) for node in tree.body)
    guarded = _guarded_import(tree)
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            annotations = [node.returns] + [item.annotation for item in (
                *arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                *(item for item in (arguments.vararg, arguments.kwarg) if item is not None))]
        elif isinstance(node, ast.AnnAssign):
            annotations = [node.annotation]
        if not future and any(_union(item) for item in annotations):
            found.append(f"{label}:{node.lineno}: '|' in an annotation needs from __future__ import annotations")
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if name in ("isinstance", "issubclass") and len(node.args) > 1 and _union(node.args[1]):
                found.append(f"{label}:{node.lineno}: '|' union in {name} needs Python 3.10")
            for keyword in node.keywords:
                if keyword.arg in NEWER_KEYWORDS.get(name, ()):
                    found.append(f"{label}:{node.lineno}: {name}({keyword.arg}=) needs Python 3.10 or newer")
        if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in guarded:
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module]
            for module in modules:
                if module in NEWER_MODULES:
                    found.append(f"{label}:{node.lineno}: module {module} needs Python 3.11")
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in NEWER_NAMES.get(node.module, ()):
                        found.append(f"{label}:{node.lineno}: {node.module}.{alias.name} needs Python 3.10 or newer")
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.attr in NEWER_NAMES.get(node.value.id, ())):
            found.append(f"{label}:{node.lineno}: {node.value.id}.{node.attr} needs Python 3.10 or newer")
    return found


class ShippedSourcesRunOn39(unittest.TestCase):
    def test_every_shipped_source_is_found(self):
        labels = [label for label, _ in shipped_sources()]
        for expected in ("hooks/router/common.py", "bin/router", "scripts/gen_plugin.py",
                         "plugins/router/hooks/router/common.py", "install.sh heredoc 1"):
            self.assertIn(expected, labels)

    def test_no_shipped_source_needs_newer_python(self):
        found = [line for label, text in shipped_sources() for line in problems(label, text)]
        self.assertEqual(found, [], "\n".join(found))

    def test_installer_doctor_and_readme_state_the_39_floor(self):
        def lines(path, needle):
            return [line.strip() for line in (ROOT / path).read_text(encoding="utf-8").splitlines() if needle in line]
        self.assertEqual(lines("install.sh", "version_info"), ["if sys.version_info < (3, 9):"])
        self.assertEqual(lines("install.sh", " or newer"),
                         ['sys.exit("router install: missing requirement: python3 3.9 or newer")'])
        self.assertEqual(lines("hooks/router/cmd_doctor.py", "(3, 1"), [])
        self.assertEqual(len(lines("hooks/router/cmd_doctor.py", "version >= (3, 9)")), 2)
        self.assertEqual(lines("hooks/router/cmd_doctor.py", " or newer"),
                         ['f"{version_text} at {executable}, need 3.9 or newer")'])
        requirements = [line for line in (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
                        if line.startswith("Requirements:")]
        self.assertEqual(len(requirements), 1, requirements)
        self.assertIn("Python 3.9 or newer", requirements[0])


class CheckerFlagsWhat39CannotRun(unittest.TestCase):
    def flagged(self, text):
        return problems("sample.py", text)

    def test_union_annotation_without_future_import_is_flagged(self):
        for text in ("def f(x: int | None): pass\n", "def f() -> 'str' | None: pass\n",
                     "def f(*items: int | str): pass\n", "value: dict | None = None\n",
                     "class A:\n    def f(self, *, key: str | None = None): pass\n"):
            self.assertEqual(len(self.flagged(text)), 1, text)
        self.assertIn("sample.py:1: '|' in an annotation needs from __future__ import annotations",
                      self.flagged("def f(x: int | None): pass\n"))

    def test_future_import_makes_union_annotations_safe(self):
        text = "from __future__ import annotations\ndef f(x: int | None) -> str | None: pass\n"
        self.assertEqual(self.flagged(text), [])
        self.assertEqual(self.flagged("value = 1 | 2\nflags = a | b\n"), [])

    def test_union_in_isinstance_is_flagged_even_with_future_import(self):
        text = "from __future__ import annotations\nok = isinstance(1, int | str)\n"
        self.assertEqual(self.flagged(text), ["sample.py:2: '|' union in isinstance needs Python 3.10"])
        self.assertEqual(self.flagged("ok = isinstance(1, (int, str))\n"), [])

    def test_newer_syntax_and_library_names_are_flagged(self):
        self.assertIn("not Python 3.9 syntax", self.flagged("match x:\n    case 1:\n        pass\n")[0])
        for text in ("import tomllib\n", "from itertools import pairwise\n", "import datetime\nnow = datetime.UTC\n",
                     "pairs = zip(a, b, strict=True)\n", "from typing import TypeAlias\n"):
            self.assertEqual(len(self.flagged(text)), 1, text)
        optional = "try:\n    import tomllib\nexcept ImportError:\n    tomllib = None\n"
        self.assertEqual(self.flagged(optional), [])


if __name__ == "__main__":
    unittest.main()
