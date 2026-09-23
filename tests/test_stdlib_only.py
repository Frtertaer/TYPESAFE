"""The jev-consult pack is stdlib-only — guard every scripts/*.py import."""

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def script_files() -> list[Path]:
    return sorted(SCRIPTS.glob("*.py"))


def local_module_names() -> set:
    return {p.stem for p in script_files()}


def imported_top_modules(path: Path) -> set:
    tree = ast.parse(path.read_bytes(), filename=str(path))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


class StdlibOnlyTests(unittest.TestCase):
    def test_scripts_dir_nonempty(self) -> None:
        self.assertGreater(len(script_files()), 10)

    def test_every_import_is_stdlib_or_sibling(self) -> None:
        stdlib = set(sys.stdlib_module_names)
        local = local_module_names()
        bad = {}
        for path in script_files():
            foreign = imported_top_modules(path) - stdlib - local
            foreign.discard("__future__")
            if foreign:
                bad[path.name] = sorted(foreign)
        self.assertEqual(bad, {})

    def test_no_third_party_installer_shellouts(self) -> None:
        banned = ("pip install", "pip3 install", "uv pip", "poetry add", "npm install")
        exec_marks = ("subprocess", "os.system", "Popen", "check_output", "check_call")
        hits = {}
        for path in script_files():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not any(mark in line for mark in exec_marks):
                    continue
                for token in banned:
                    if token in line:
                        hits.setdefault(path.name, []).append(token)
        self.assertEqual(hits, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
