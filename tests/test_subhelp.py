#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-subcommand --help rc0 sweep.

Subparser names are mined from each script's add_parser(...) calls;
every subcommand must accept --help/-h with rc 0. Scripts whose
subcommands require global flags first (progress needs --repo/--db)
get them in REQUIRED_PREFIX."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

ADD_PARSER_RE = re.compile(r'add_parser\(\s*["\']([a-z0-9_-]+)["\']')

# global flags some CLIs need before the subcommand, as a script-relative
# template rendered against the temp dir
REQUIRED_PREFIX = {
    "trace": ["--file", "{tmp}/trace.json"],
    "progress": ["--repo", "{tmp}", "--db", "{tmp}/p.db"],
}


def subcommands(path: Path) -> list:
    names = ADD_PARSER_RE.findall(path.read_text(encoding="utf-8"))
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def run(argv: list, cwd: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.setdefault("TYPESAFE_API_KEY", "test-key")
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
        env=env,
    )


class SubHelpTests(unittest.TestCase):
    def test_every_subcommand_accepts_help(self) -> None:
        scanned = 0
        with tempfile.TemporaryDirectory() as tmp:
            for path in sorted(SCRIPTS.glob("*.py")):
                name = path.stem
                subs = subcommands(path)
                if not subs:
                    continue
                prefix = [p.format(tmp=tmp) for p in REQUIRED_PREFIX.get(name, [])]
                for sub in subs:
                    scanned += 1
                    with self.subTest(script=name, sub=sub):
                        proc = run(
                            [sys.executable, str(path), *prefix, sub, "--help"],
                            tmp,
                        )
                        self.assertEqual(
                            proc.returncode, 0,
                            "%s %s --help rc=%s: %s"
                            % (name, sub, proc.returncode, proc.stderr[:200]),
                        )
        self.assertGreater(scanned, 10, "expected subcommands to be found")


if __name__ == "__main__":
    unittest.main()
