#!/usr/bin/env python
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"

ENV_RE = re.compile(r'"JEV_[A-Z0-9_]+"')
DOC_ENV_RE = re.compile(r"JEV_[A-Z0-9_*]+")
# prose fragments like "all JEV_HOOK_* knobs" are prefixes, not env vars
DOC_SKIP_EXACT = {"JEV_HOOK_"}


def code_envs() -> set[str]:
    out: set[str] = set()
    for path in SCRIPTS.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        out.update(m.group(0)[1:-1] for m in ENV_RE.finditer(text))
    return out


def doc_envs_and_patterns() -> tuple[set[str], list[re.Pattern[str]]]:
    text = SKILL_MD.read_text(encoding="utf-8")
    exact: set[str] = set()
    patterns: list[re.Pattern[str]] = []
    for token in DOC_ENV_RE.findall(text):
        if token in DOC_SKIP_EXACT:
            continue
        if "*" in token:
            patterns.append(re.compile("^" + token.replace("*", "[A-Z0-9_]+") + "$"))
        else:
            exact.add(token)
    return exact, patterns


class EnvDocParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.code = code_envs()
        self.doc_exact, self.doc_patterns = doc_envs_and_patterns()

    def test_sanity_code_and_doc_have_envs(self) -> None:
        self.assertGreater(len(self.code), 50)
        self.assertGreater(len(self.doc_exact), 50)
        self.assertIn("JEV_HOOK_WATCH_QUIET", self.code)
        self.assertIn("TYPESAFE_API_KEY", SKILL_MD.read_text(encoding="utf-8"))

    def test_every_code_env_is_documented(self) -> None:
        undocumented = sorted(
            name
            for name in self.code
            if name not in self.doc_exact
            and not any(p.match(name) for p in self.doc_patterns)
        )
        self.assertEqual(undocumented, [])

    def test_every_doc_env_exists_in_code(self) -> None:
        phantoms = sorted(name for name in self.doc_exact if name not in self.code)
        self.assertEqual(phantoms, [])

    def test_watch_env_triplets_documented_for_each_loop(self) -> None:
        """Every script with JEV_X_WATCH_MAX also wires JEV_X_WATCH_SECS and
        JEV_X_WATCH_QUIET in the same file."""
        by_file: dict[str, set[str]] = {}
        for path in SCRIPTS.glob("*.py"):
            names = set(m.group(0)[1:-1] for m in ENV_RE.finditer(path.read_text(encoding="utf-8")))
            by_file[path.name] = names
        missing = []
        for fname, names in by_file.items():
            for name in names:
                m = re.match(r"^JEV_(.+)_WATCH_MAX$", name)
                if not m:
                    continue
                stem = m.group(1)
                for suffix in ("SECS", "QUIET"):
                    want = f"JEV_{stem}_WATCH_{suffix}"
                    if want not in names:
                        missing.append(f"{fname}:{want}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
