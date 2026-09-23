#!/usr/bin/env python
"""Guard: .pre-commit-config.yaml local hooks point at scripts that exist.

The config is optional (only used by devs who `pre-commit install`), but a
renamed script would silently break it — check every `entry` resolves.
"""
import re
import subprocess
import sys
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

    def _hook_blocks(self):
        text = CONFIG.read_text(encoding="utf-8")
        for block in re.split(r"\n\s+- id:", text)[1:]:
            hid = re.search(r"^\s*(\S+)", block)
            entry = re.search(r"entry:\s*python\s+(\S+)", block)
            args = re.search(r"args:\s*\[([^\]]*)\]", block)
            files = re.search(r"files:\s*(\S+)", block)
            yield {
                "id": hid.group(1) if hid else "?",
                "entry": entry.group(1) if entry else "",
                "args": [
                    t.strip().strip("'\"")
                    for t in (args.group(1).split(",") if args else [])
                    if t.strip()
                ],
                "files": files.group(1) if files else "",
            }

    def test_hooks_run_clean(self) -> None:
        """Each local hook's entry+args exits 0 (always_run or files match)."""
        ran = 0
        for hook in self._hook_blocks():
            cmd = [sys.executable, hook["entry"], *hook["args"]]
            if hook["files"]:
                # pre-commit appends matching filenames — emulate it
                pat = re.compile(hook["files"].strip("'\""))
                matched = [
                    str(p.relative_to(ROOT)).replace("\\", "/")
                    for p in sorted(ROOT.rglob("*.json"))
                    if pat.search(str(p.relative_to(ROOT)).replace("\\", "/"))
                ]
                self.assertTrue(matched, "%s files regex matched nothing" % hook["id"])
                cmd += matched
            proc = subprocess.run(
                cmd, cwd=ROOT, capture_output=True, text=True, timeout=60
            )
            ran += 1
            with self.subTest(hook=hook["id"]):
                self.assertEqual(
                    proc.returncode,
                    0,
                    "hook %s exited %d: %s"
                    % (hook["id"], proc.returncode, proc.stderr[:300]),
                )
        self.assertGreaterEqual(ran, 3)


if __name__ == "__main__":
    unittest.main()
