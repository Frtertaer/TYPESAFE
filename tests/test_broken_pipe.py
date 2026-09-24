#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Hooks keep the fail-open rc0 contract even when the consumer closed
their stdout (harness died): _watch.exit_safely redirects stdout to
devnull so interpreter shutdown never dies with rc 120."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

HOOKS = ["inventory_hook.py", "compact_hook.py"]


def run_closed_stdout(script: str, tmp: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["JEV_CONSULT_LOG"] = "0"
    env.setdefault("TYPESAFE_API_KEY", "test-key")
    env["JEV_HOOK_CWD"] = tmp
    env["JEV_HOOK_PROMPT"] = "deploy proxy"
    # stdout -> a pipe whose read end is already closed: the child's first
    # flush hits EPIPE/EINVAL (deterministic — no race with the write).
    r, w = os.pipe()
    os.close(r)
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS / script)],
        stdin=subprocess.PIPE, stdout=w, stderr=subprocess.PIPE,
        text=True, env=env, cwd=tmp,
    )
    os.close(w)
    _, err = proc.communicate(input="{}", timeout=120)
    proc._stderr_text = err  # type: ignore[attr-defined]
    return proc


EXEMPT = {"_watch.py"}  # skill_scanner guards its _watch import for standalone use


class BrokenPipeTests(unittest.TestCase):
    def test_entrypoints_route_through_exit_safely(self) -> None:
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in EXEMPT:
                continue
            src = path.read_text(encoding="utf-8")
            if 'if __name__ == "__main__":' in src \
                    and "_watch.exit_safely(" not in src:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_hooks_exit_0_when_stdout_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for script in HOOKS:
                with self.subTest(script=script):
                    proc = run_closed_stdout(script, tmp)
                    self.assertEqual(
                        proc.returncode, 0,
                        "%s rc=%d stderr=%s" % (
                            script, proc.returncode,
                            getattr(proc, "_stderr_text", "")[:300]),
                    )


if __name__ == "__main__":
    unittest.main()
