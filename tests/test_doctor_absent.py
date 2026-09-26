#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Absent-harness handling in skills/jev-consult/scripts/doctor.py.

A machine without a harness installed (no ~/.hermes, ~/.claude, ...) is
not an install failure: doctor emits one skipped `presence` row per
absent agent, keeps the top-level verdict driven by real failures, and
lists the absent names under payload["absent"].
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "doctor_absent", ROOT / "skills" / "jev-consult" / "scripts" / "doctor.py"
)
DOC = importlib.util.module_from_spec(SPEC)
sys.modules["doctor_absent"] = DOC
SPEC.loader.exec_module(DOC)


def run_main(argv, env_extra=None, cwd=None):
    buf = io.StringIO()
    env = {"TYPESAFE_API_KEY": "", "JEV_CONSULT_LOG": "0"}
    if env_extra:
        env.update(env_extra)
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
    try:
        with patch.dict(os.environ, env), patch.object(sys, "stdout", buf):
            rc = DOC.main(argv)
    finally:
        os.chdir(old)
    text = buf.getvalue()
    try:
        return rc, json.loads(text), text
    except ValueError:
        return rc, None, text


def presence_of(out, agent):
    for c in out["checks"]:
        if c["check"] == "presence" and c["agent"] == agent:
            return c
    return None


def agent_checks(out, agent):
    return [c for c in out["checks"] if c["agent"] == agent]


class AbsentHarnessTests(unittest.TestCase):
    def test_absent_hermes_is_skipped_not_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"  # never created
            home.mkdir()
            rc, out, _ = run_main(
                [
                    "--agents",
                    "hermes",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                ],
                env_extra={"TYPESAFE_API_KEY": "sekret-AAAA-BBBB-CCCC-DDDD"},
            )
        self.assertIsNotNone(out)
        self.assertEqual(out["absent"], ["hermes"])
        presence = presence_of(out, "hermes")
        self.assertIsNotNone(presence)
        self.assertTrue(presence["ok"])
        self.assertTrue(presence["skipped"])
        self.assertIn("not installed", presence["detail"])
        # the absence replaced the whole per-harness check list
        self.assertEqual(agent_checks(out, "hermes"), [presence])
        # nothing failed: a box without hermes is a valid box
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])

    def test_empty_home_marks_every_agent_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            home.mkdir()
            rc, out, _ = run_main(
                ["--home", str(home), "--hermes-home", str(hermes)]
            )
        for agent in (
            "claude-code", "codex", "cursor", "gemini", "grok", "hermes"
        ):
            presence = presence_of(out, agent)
            self.assertIsNotNone(presence, agent)
            self.assertTrue(presence["skipped"], agent)
            self.assertEqual(agent_checks(out, agent), [presence], agent)
        self.assertEqual(
            out["absent"],
            ["claude-code", "codex", "cursor", "gemini", "grok", "hermes"],
        )
        # still a failure overall: the shared api_key check fails
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        failed = [c for c in out["checks"] if not c["ok"]]
        self.assertTrue(all(c["agent"] == "*" for c in failed))

    def test_present_harness_runs_real_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            home.mkdir()
            hermes.mkdir()  # installed but unconfigured -> real FAILs
            rc, out, _ = run_main(
                [
                    "--agents",
                    "hermes",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                ],
                env_extra={"TYPESAFE_API_KEY": "sekret-AAAA-BBBB-CCCC-DDDD"},
            )
        presence = presence_of(out, "hermes")
        self.assertIsNotNone(presence)
        self.assertTrue(presence["ok"])
        self.assertNotIn("skipped", presence)
        self.assertEqual(out["absent"], [])
        others = [
            c for c in agent_checks(out, "hermes") if c["check"] != "presence"
        ]
        self.assertTrue(others)
        self.assertTrue(any(not c["ok"] for c in others))
        self.assertEqual(rc, 1)

    def test_mixed_box_reports_only_missing_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            (home / ".claude").mkdir(parents=True)
            (home / ".codex").mkdir(parents=True)
            rc, out, _ = run_main(
                [
                    "--agents",
                    "claude-code,codex,hermes",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                ],
                env_extra={"TYPESAFE_API_KEY": "sekret-AAAA-BBBB-CCCC-DDDD"},
            )
        self.assertEqual(out["absent"], ["hermes"])
        self.assertTrue(presence_of(out, "hermes")["skipped"])
        for agent in ("claude-code", "codex"):
            self.assertNotIn("skipped", presence_of(out, agent))
            self.assertTrue(
                any(
                    c["check"] != "presence"
                    for c in agent_checks(out, agent)
                ),
                agent,
            )

    def test_only_filter_keeps_presence_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            rc, out, _ = run_main(
                [
                    "--agents",
                    "claude-code",
                    "--home",
                    str(home),
                    "--only",
                    "presence",
                ]
            )
        self.assertEqual(
            [c["check"] for c in out["checks"]], ["presence"]
        )
        self.assertTrue(out["checks"][0]["skipped"])
        self.assertEqual(out["absent"], ["claude-code"])

    def test_schema_documents_skipped_and_absent(self) -> None:
        rc, out, text = run_main(["--schema"])
        self.assertEqual(rc, 0)
        self.assertIn("check.skipped", text)
        self.assertIn("absent: list[string]", text)

    def test_markdown_and_csv_render_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            argv = ["--agents", "grok", "--home", str(home), "--only", "presence"]
            for flag, needle in (("--md", "skipped"), ("--csv", "skipped")):
                rc, _, text = run_main(argv + [flag])
                self.assertEqual(rc, 0, flag)
                self.assertIn(needle, text, flag)

    def test_verdict_counts_absent_as_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            verdict = Path(tmp) / "verdict.json"
            home.mkdir()
            rc, out, _ = run_main(
                [
                    "--agents",
                    "grok",
                    "--home",
                    str(home),
                    "--verdict",
                    str(verdict),
                ],
                env_extra={"TYPESAFE_API_KEY": "sekret-AAAA-BBBB-CCCC-DDDD"},
            )
            self.assertEqual(rc, 0)
            slim = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertTrue(slim["agents"]["grok"])
        self.assertEqual(slim["failed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
