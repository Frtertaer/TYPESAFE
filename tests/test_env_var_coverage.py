#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Every JEV_*/TYPESAFE_* env literal in scripts/*.py must be exercised.

Coverage sources: any tests/test_*.py blob plus smoke.py (the pack's
own e2e harness). The pin scans script source for quoted env names and
fails listing the uncovered ones — add a test, or the pin stays red.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
TESTS = ROOT / "tests"
SMOKE = SCRIPTS / "smoke.py"
SELF = Path(__file__).name
HELPERS = {"_watch", "progress_core", "skill_scanner"}

ENV_RE = re.compile(r'["\']((JEV|TYPESAFE)_[A-Z0-9_]+)["\']')


def _covered_blob() -> str:
    # Unlike test_flag_coverage (a pure pin), this file's own tests invoke
    # the listed env vars via subprocess --env calls — so SELF counts.
    blob = "".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in TESTS.glob("test_*.py")
    )
    return blob + SMOKE.read_text(encoding="utf-8", errors="replace")


def _run(script: str, *args: str, env_add: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_add or {})
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


class EnvVarCoveragePinTests(unittest.TestCase):
    def test_every_env_var_referenced_by_some_test_or_smoke(self) -> None:
        blob = _covered_blob()
        missing: dict[str, list[str]] = {}
        for script in sorted(SCRIPTS.glob("*.py")):
            if script.stem.startswith("__") or script.stem in HELPERS:
                continue
            src = script.read_text(encoding="utf-8", errors="replace")
            uncovered = sorted(
                e for e in set(ENV_RE.findall(src)) if e[0] not in blob
            )
            if uncovered:
                missing[script.name] = [e[0] for e in uncovered]
        self.assertEqual(missing, {})


class WatchQuietEnvPresetTests(unittest.TestCase):
    """*_WATCH_QUIET env presets surface as watch_quiet in --env."""

    def _watch_quiet(self, script: str, env_var: str) -> bool:
        proc = _run(script, "--env", env_add={env_var: "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        return json.loads(proc.stdout).get("watch_quiet")

    def test_watch_quiet_env_presets(self) -> None:
        cases = [
            ("catalog_fill.py", "JEV_CATALOG_WATCH_QUIET"),
            ("compare.py", "JEV_COMPARE_WATCH_QUIET"),
            ("compact.py", "JEV_COMPACT_WATCH_QUIET"),
            ("question_lint.py", "JEV_QLINT_WATCH_QUIET"),
            ("skill_lint.py", "JEV_SLINT_WATCH_QUIET"),
            ("trigger_lint.py", "JEV_TLINT_WATCH_QUIET"),
        ]
        for script, env_var in cases:
            with self.subTest(env=env_var):
                self.assertIs(self._watch_quiet(script, env_var), True)

    def test_decisions_watch_quiet_listed_in_env(self) -> None:
        proc = _run("decisions.py", "--env", env_add={"JEV_DECISIONS_WATCH_QUIET": "1"})
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        report = json.loads(proc.stdout)
        self.assertIn("JEV_DECISIONS_WATCH_QUIET", report["env"])


class MiscEnvPresetTests(unittest.TestCase):
    def test_compact_keep_first_env_default(self) -> None:
        proc = _run("compact.py", "--env", env_add={"JEV_KEEP_FIRST": "3"})
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertEqual(json.loads(proc.stdout)["keep_first"], 3)

    def test_jev_ping_watch_secs_env(self) -> None:
        proc = _run("jev.py", "env", env_add={"JEV_PING_WATCH_SECS": "7"})
        if proc.returncode != 0:
            proc = _run("jev.py", "--env", env_add={"JEV_PING_WATCH_SECS": "7"})
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertEqual(json.loads(proc.stdout)["watch_secs"], 7.0)

    def test_progress_watch_secs_env(self) -> None:
        proc = _run("progress.py", "env", env_add={"JEV_PROGRESS_WATCH_SECS": "7"})
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertEqual(json.loads(proc.stdout)["watch_secs"], 7.0)

    def test_trace_harness_env_tags_note(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        try:
            import trace as tr
        finally:
            sys.path.remove(str(SCRIPTS))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.json"
            from unittest.mock import patch
            with patch.dict(
                os.environ, {"JEV_TRACE_HARNESS": "ci-bot"}
            ):
                from io import StringIO
                from contextlib import redirect_stdout

                buf = StringIO()
                with redirect_stdout(buf):
                    rc = tr.main(
                        ["--file", str(path), "record",
                         "--pick", "p1", "--note", "n1"]
                    )
                self.assertEqual(rc, 0, buf.getvalue())
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["notes"][0]["harness"], "ci-bot")


if __name__ == "__main__":
    unittest.main()
