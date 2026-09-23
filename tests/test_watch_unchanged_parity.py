#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""--unchanged-max parity across every pack watch loop."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import apply_fill  # noqa: E402
import catalog_fill  # noqa: E402
import compare  # noqa: E402
import compact  # noqa: E402
import decisions  # noqa: E402
import doctor  # noqa: E402
import inventory  # noqa: E402
import inventory_hook  # noqa: E402
import jev  # noqa: E402
import peer_fill  # noqa: E402
import policy_lint  # noqa: E402
import progress  # noqa: E402
import progress_core  # noqa: E402
try:
    import test_progress as _tp  # discover -s tests layout
except ImportError:  # pragma: no cover - `python -m unittest tests.x` layout
    import tests.test_progress as _tp
import question_lint  # noqa: E402
import skill_lint  # noqa: E402
import smoke  # noqa: E402
import trigger_eval  # noqa: E402
import trigger_lint  # noqa: E402

MSG = "consecutive identical ticks"


def _stderr_ticks(err: str) -> int:
    return len([l for l in err.splitlines() if l.startswith("watch tick=")])


class DecisionsUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            log.write_text(
                json.dumps({"ts": 1, "jev_status": "ok", "harness": "h"}) + "\n",
                encoding="utf-8",
            )
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = decisions.main(
                    [
                        "--file", str(log),
                        "--watch", "0.02",
                        "--unchanged-max", "2",
                        "--max-ticks", "60",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err.getvalue())
            self.assertLess(_stderr_ticks(err.getvalue()), 60)


class ProgressUnchangedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = progress_core.Ledger(
            self.root / "progress.sqlite3", self.root, evidence=_tp.FakeEvidence()
        )
        self.ledger.initialize(_tp.plan(), _tp.policy())

    def _args(self, **over):
        args = SimpleNamespace(
            stage="reliability", watch=0.01, max_ticks=60, watch_max=0.0,
            quiet=False, fail_fast=False, out="", verdict="", jq="",
            unchanged_max=2,
        )
        for k, v in over.items():
            setattr(args, k, v)
        return args

    def test_status_watch_stops_on_identical_ticks(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = progress._status_watch(self.ledger, self._args())
        self.assertEqual(rc, 1)  # still "continue" when it stopped
        self.assertIn(MSG, err.getvalue())

    def test_history_watch_stops_on_identical_ticks(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = progress._history_watch(self.ledger, self._args())
        self.assertEqual(rc, 1)  # fresh ledger has an empty history
        self.assertIn(MSG, err.getvalue())

    def test_report_watch_stops_on_identical_ticks(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = progress._report_watch(self.ledger, self._args())
        self.assertEqual(rc, 0)
        self.assertIn(MSG, err.getvalue())


class SmokeUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        err = io.StringIO()
        with patch.object(smoke, "step_policy", side_effect=ok_step):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = smoke.main(
                    [
                        "--watch", "0.01",
                        "--only", "policy",
                        "--unchanged-max", "2",
                        "--max-ticks", "60",
                    ]
                )
        self.assertEqual(rc, 0)
        self.assertIn(MSG, err.getvalue())


class InventoryUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = inventory.main(
                    [
                        "--home", tmp,
                        "--hermes-home", str(Path(tmp) / "h"),
                        "--watch", "0.02",
                        "--unchanged-max", "2",
                        "--max-ticks", "60",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err.getvalue())


class CompactUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        transcript = [
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": "done"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(transcript), encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = compact.main(
                    [
                        str(f), "--history", "--fake", "--min-reduction", "0",
                        "--watch", "0.02",
                        "--unchanged-max", "2",
                        "--max-ticks", "60",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err.getvalue())


class DoctorUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        stable = [{"agent": "claude-code", "check": "c", "ok": True, "detail": "d"}]
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}):
                with patch.object(doctor, "check_claude", return_value=stable), patch.object(
                    doctor, "check_common", side_effect=lambda h, hh: []
                ):
                    old = __import__("os").getcwd()
                    __import__("os").chdir(tmp)
                    try:
                        with redirect_stdout(io.StringIO()), redirect_stderr(err):
                            rc = doctor.main(
                                [
                                    "--agents", "claude-code",
                                    "--home", tmp,
                                    "--hermes-home", str(Path(tmp) / "h"),
                                    "--watch", "0.01",
                                    "--unchanged-max", "2",
                                    "--max-ticks", "60",
                                ]
                            )
                    finally:
                        __import__("os").chdir(old)
        self.assertEqual(rc, 0)
        self.assertIn(MSG, err.getvalue())


class LintUnchangedTests(unittest.TestCase):
    def _run_watch(self, module, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = module.main(argv)
        return rc, err.getvalue()

    def test_question_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(
                json.dumps(
                    {
                        "state": {"task": "x"},
                        "questions": {
                            "q": {
                                "type": "noul",
                                "instructions": "done?",
                                "criteria": {"true": "y", "false": "n"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rc, err = self._run_watch(
                question_lint,
                [
                    str(req), "--watch", "0.02",
                    "--unchanged-max", "2", "--max-ticks", "60",
                ],
            )
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err)

    def test_skill_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "good-skill"
            skill_dir.mkdir()
            md = skill_dir / "SKILL.md"
            md.write_text(
                "---\nname: good-skill\ndescription: does a thing\n---\n\nbody\n",
                encoding="utf-8",
            )
            rc, err = self._run_watch(
                skill_lint,
                [
                    str(md), "--watch", "0.02",
                    "--unchanged-max", "2", "--max-ticks", "60",
                ],
            )
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err)

    def test_policy_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pol = Path(tmp) / "policy.json"
            pol.write_text(
                Path(SCRIPTS.parent / "policy.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            rc, err = self._run_watch(
                policy_lint,
                [
                    str(pol), "--watch", "0.02",
                    "--unchanged-max", "2", "--max-ticks", "60",
                ],
            )
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err)

    def test_lint_bad_unchanged_max_rc2(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = policy_lint.main(["whatever.json", "--unchanged-max", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("--unchanged-max", err.getvalue())


class TriggerEvalUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = trigger_eval.main(
                [
                    "--watch", "0.01",
                    "--unchanged-max", "2",
                    "--max-ticks", "60",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn(MSG, err.getvalue())


class TriggerLintUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cases = Path(tmp) / "cases.json"
            cases.write_text(
                json.dumps({"cases": [{"id": "a", "should_trigger": True}]}),
                encoding="utf-8",
            )
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = trigger_lint.main(
                    [
                        str(cases), "--watch", "0.02",
                        "--unchanged-max", "2", "--max-ticks", "60",
                    ]
                )
            self.assertEqual(rc, 1)  # minimal case trips T003; stop is still early
            self.assertIn(MSG, err.getvalue())


class CompareUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        cases = {
            "goal": "g",
            "cases": [
                {
                    "id": "c1",
                    "defect": "drift",
                    "score": "on_track",
                    "prompt": "p",
                    "before": {"plan": "A"},
                    "after": {"plan": "A"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "cases.json"
            f.write_text(json.dumps(cases), encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = compare.main(
                    [
                        "--cases", str(f),
                        "--watch", "0.02",
                        "--unchanged-max", "2",
                        "--max-ticks", "60",
                    ]
                )
            self.assertIn(MSG, err.getvalue())


class FillUnchangedTests(unittest.TestCase):
    def test_apply_fill_unchanged_max_ignores_miss_age(self) -> None:
        import os
        import time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".jev-tools-miss.json").write_text(
                json.dumps(
                    {"task": "jwt", "harness": "hermes", "written_at": time.time()}
                ),
                encoding="utf-8",
            )
            err = io.StringIO()
            argv = [
                "apply_fill.py",
                "--watch", "0.02",
                "--cwd", str(cwd),
                "--unchanged-max", "2",
                "--max-ticks", "60",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(err):
                rc = apply_fill.main()
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err.getvalue())

    def test_peer_fill_unchanged_max(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".jev-tools-miss.json").write_text(
                json.dumps({"task": "jwt flow", "written_at": int(time.time())}),
                encoding="utf-8",
            )
            err = io.StringIO()
            argv = [
                "peer_fill.py",
                "--cwd", str(cwd),
                "--watch", "0.02",
                "--unchanged-max", "2",
                "--max-ticks", "60",
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(err):
                rc = peer_fill.main()
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err.getvalue())

    def test_catalog_fill_unchanged_max_ignores_cache_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            argv = [
                "catalog_fill.py",
                "--task", "jwt",
                "--cwd", tmp,
                "--watch", "0.02",
                "--unchanged-max", "2",
                "--max-ticks", "60",
            ]
            with patch.object(
                catalog_fill, "search_hits", return_value=[]
            ), patch.object(
                catalog_fill, "read_catalog_cache", return_value=None
            ), patch.object(
                sys, "argv", argv
            ), redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = catalog_fill.main()
            self.assertEqual(rc, 0)
            self.assertIn(MSG, err.getvalue())


class InventoryHookUnchangedTests(unittest.TestCase):
    def test_unchanged_max_stops_early(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "anything",
                        "cwd": tmp,
                    }
                ),
                encoding="utf-8",
            )
            err = io.StringIO()
            env = {"TYPESAFE_API_KEY": "", "JEV_CONSULT_LOG": "0"}
            with patch.dict(os.environ, env), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(err):
                rc = inventory_hook.main(
                    [
                        "--file", str(event),
                        "--watch", "0.02",
                        "--unchanged-max", "2",
                        "--max-ticks", "60",
                    ]
                )
            self.assertIn(MSG, err.getvalue())


class JevPingUnchangedTests(unittest.TestCase):
    def test_unchanged_max_ignores_ms(self) -> None:
        err = io.StringIO()
        with patch.object(
            jev, "_ping_once", return_value={"model": "m", "noul": "x", "ms": 12}
        ), redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = jev.main(
                [
                    "ping", "--watch", "0.01",
                    "--unchanged-max", "2", "--max-ticks", "60",
                ]
            )
        self.assertEqual(rc, 0)
        self.assertIn(MSG, err.getvalue())


if __name__ == "__main__":
    unittest.main()
