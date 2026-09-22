#!/usr/bin/env python
"""Guard: .pre-commit-config.yaml local hooks point at scripts that exist.

The config is optional (only used by devs who `pre-commit install`), but a
renamed script would silently break it — check every `entry` resolves.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".pre-commit-config.yaml"


class PreCommitConfigTests(unittest.TestCase):
    def test_config_exists(self) -> None:
        self.assertTrue(CONFIG.is_file())

    def test_every_entry_script_exists(self) -> None:
        text = CONFIG.read_text(encoding="utf-8")
        entries = re.findall(r"entry:\s*python\s+(\S+)", text)
        self.assertGreaterEqual(len(entries), 2)
        missing = [e for e in entries if not (ROOT / e).is_file()]
        self.assertEqual(missing, [])

    def test_every_args_path_exists(self) -> None:
        text = CONFIG.read_text(encoding="utf-8")
        arg_paths = re.findall(r"args:\s*\[([^\]]*)\]", text)
        referenced = []
        for arglist in arg_paths:
            for token in arglist.split(","):
                token = token.strip().strip("'\"")
                if token and not token.startswith("-") and "/" in token:
                    referenced.append(token)
        self.assertGreaterEqual(len(referenced), 2)
        missing = [a for a in referenced if not (ROOT / a).is_file()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
