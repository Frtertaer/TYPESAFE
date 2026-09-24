#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Pin .pre-commit-config.yaml stays runnable: every entry's script
exists, smoke --only names resolve to real steps, and literal path
args exist."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pre-commit-config.yaml"
SMOKE = ROOT / "skills" / "jev-consult" / "scripts" / "smoke.py"


class PrecommitPinsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.src = CONFIG.read_text(encoding="utf-8")

    def test_every_entry_script_exists(self) -> None:
        entries = re.findall(r"entry: python (\S+)", self.src)
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertTrue((ROOT / entry).is_file(), entry)

    def test_literal_path_args_exist(self) -> None:
        for arg in re.findall(r"args: \[[^\]]*\]|args: \[(.*?)\]", self.src):
            for token in arg.split(","):
                token = token.strip().strip('"').strip("'")
                if token and "/" in token and not token.startswith("-"):
                    with self.subTest(path=token):
                        self.assertTrue(
                            (ROOT / token).exists(),
                            "args path missing: %s" % token,
                        )

    def test_smoke_only_names_resolve(self) -> None:
        """--only args (comma-free by convention here) must name real steps."""
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--list", "--json"],
            capture_output=True, text=True, timeout=60, env=env,
            cwd=str(ROOT),
        )
        listed = proc.stdout
        for m in re.finditer(r"--only, ([a-z_,]+)", self.src):
            for name in m.group(1).split(","):
                with self.subTest(step=name):
                    self.assertIn(name, listed)

    def test_no_comma_inside_quoted_arg(self) -> None:
        """pre-commit arg lists split naively in some harnesses — never
        embed a comma inside one quoted arg value."""
        for m in re.finditer(r"args:\s*\[([^\]]*)\]", self.src):
            for token in m.group(1).split(","):
                token = token.strip()
                if token.startswith(('\"', "'")) or token.endswith(('\"', "'")):
                    self.assertNotIn(
                        ",", token.strip("\"'"),
                        "comma inside a quoted arg: %s" % token,
                    )

    def test_smoke_quick_passes(self) -> None:
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "self_test", "--quiet"],
            capture_output=True, text=True, timeout=120, env=env,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:200])
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
