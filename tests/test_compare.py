#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
COMPARE = SCRIPTS / "compare.py"
sys.path.insert(0, str(SCRIPTS))

import compare  # noqa: E402

CASES = {
    "goal": "test goal",
    "cases": [
        {
            "id": "drift_case",
            "defect": "drift",
            "score": "on_track",
            "prompt": "stick to the plan",
            "before": {
                "plan": "do A",
                "current_step": "doing B instead",
                "called_jev": False,
                "invented": "user wants B",
            },
            "after": {
                "plan": "do A",
                "current_step": "do A",
                "called_jev": True,
                "last_pick": "return_to_plan",
            },
        },
        {
            "id": "stall_case",
            "defect": "stall",
            "prompt": "what next",
            "before": {"current_step": "idle", "called_jev": False},
            "after": {"current_step": "next step", "called_jev": True, "last_pick": "narrow"},
        },
    ],
}


class FakeJev:
    """Stand-in for the jev module: canned answers, no network."""

    def __init__(self, answers):
        self._answers = answers
        self.posts = []

    def post_systemone(self, state, questions, policy, **kwargs):
        self.posts.append((state, questions))
        return {"answers": self._answers, "model": "fake-0"}

    @staticmethod
    def decide(answers, policy, irreversible=False):
        return {"action": "proceed", "picks": {}}


class LoadCasesTest(unittest.TestCase):
    def test_bundled_cases_file_loads(self) -> None:
        blob = compare.load_cases()
        self.assertIsInstance(blob["cases"], list)
        self.assertTrue(blob["cases"])

    def test_rejects_non_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps([1, 2]), encoding="utf-8")
            with self.assertRaises(SystemExit):
                compare.load_cases(path)

    def test_rejects_missing_cases_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps({"goal": "x"}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                compare.load_cases(path)


class HelpersTest(unittest.TestCase):
    def test_side_state_strips_bookkeeping(self) -> None:
        side = {
            "plan": "p",
            "current_step": "s",
            "called_jev": True,
            "invented": "x",
            "last_pick": "y",
        }
        out = compare.side_state(side)
        self.assertEqual(out, {"plan": "p", "current_step": "s"})

    def test_build_request_uses_score_template(self) -> None:
        policy = {"templates": {"on_track": {"type": "noul", "instructions": "on track?"}}}
        req = compare.build_request(policy, {"score": "on_track"}, {"plan": "p", "called_jev": True})
        self.assertEqual(req["state"], {"plan": "p"})
        self.assertEqual(req["questions"]["on_track"]["type"], "noul")

    def test_build_request_defaults_to_on_track(self) -> None:
        policy = {"templates": {"on_track": {"type": "noul", "instructions": "on track?"}}}
        req = compare.build_request(policy, {}, {})
        self.assertIn("on_track", req["questions"])

    def test_build_request_missing_template_exits(self) -> None:
        with self.assertRaises(SystemExit):
            compare.build_request({"templates": {}}, {"score": "nope"}, {})

    def test_noul_value_variants(self) -> None:
        self.assertEqual(compare.noul_value({"q": {"noul": 0.9}}, "q"), 0.9)
        self.assertEqual(compare.noul_value({"q": {"value": 0.4}}, "q"), 0.4)
        self.assertIsNone(compare.noul_value({"q": {"noul": "high"}}, "q"))
        self.assertIsNone(compare.noul_value({"q": "nope"}, "q"))
        self.assertIsNone(compare.noul_value({}, "q"))


class RowAndTableTest(unittest.TestCase):
    def test_row_offline_shape(self) -> None:
        row = compare.row_offline(CASES["cases"][0])
        self.assertEqual(row["id"], "drift_case")
        self.assertEqual(row["before"]["called_jev"], False)
        self.assertEqual(row["before"]["invented"], "user wants B")
        self.assertEqual(row["after"]["called_jev"], True)
        self.assertEqual(row["after"]["last_pick"], "return_to_plan")
        self.assertNotIn("noul", row["before"])

    def test_format_table_offline(self) -> None:
        rows = [compare.row_offline(c) for c in CASES["cases"]]
        table = compare.format_table(rows, live=False)
        self.assertIn("before_jev", table)
        self.assertIn("drift_case", table)
        self.assertIn("return_to_plan", table)
        self.assertIn("No watchdog", table)

    def test_format_table_live_marks_noul(self) -> None:
        rows = [compare.row_offline(c) for c in CASES["cases"]]
        rows[0]["before"]["noul"] = 0.12
        rows[0]["after"]["noul"] = 0.93
        table = compare.format_table(rows, live=True)
        self.assertIn("before_noul", table)
        self.assertIn("0.12", table)
        self.assertIn("0.93", table)
        self.assertIn("-", table)  # second case has no noul


class RunTest(unittest.TestCase):
    def write_cases(self, tmp: str) -> Path:
        path = Path(tmp) / "cases.json"
        path.write_text(json.dumps(CASES), encoding="utf-8")
        return path

    def test_run_offline_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = compare.run(live=False, as_json=False, path=self.write_cases(tmp))
        self.assertFalse(result["live"])
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["goal"], "test goal")

    def test_score_live_with_fake_jev(self) -> None:
        fake = FakeJev({"on_track": {"noul": 0.91, "type": "noul"}})
        policy = {"templates": {"on_track": {"type": "noul", "instructions": "on track?"}}}
        case = CASES["cases"][0]
        out = compare.score_live(fake, policy, case, case["before"])
        self.assertEqual(out["noul"], 0.91)
        self.assertEqual(out["model"], "fake-0")
        self.assertEqual(out["action"], "proceed")
        state, questions = fake.posts[0]
        self.assertNotIn("called_jev", state)
        self.assertIn("on_track", questions)

    def test_score_live_bad_answers(self) -> None:
        fake = FakeJev("not a dict inside")
        fake._answers = None
        policy = {"templates": {"on_track": {"type": "noul", "instructions": "on track?"}}}
        out = compare.score_live(fake, policy, {"score": "on_track"}, {})
        self.assertIsNone(out["noul"])


class CliTest(unittest.TestCase):
    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(COMPARE), *argv],
            capture_output=True,
            text=True,
        )

    def test_cli_offline_table(self) -> None:
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("goal:", proc.stdout)
        self.assertIn("No watchdog", proc.stdout)

    def test_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(CASES), encoding="utf-8")
            proc = self.run_cli("--json", "--cases", str(path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertFalse(payload["live"])
        self.assertEqual(len(payload["rows"]), 2)

    def test_cli_only_filters_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(CASES), encoding="utf-8")
            proc = self.run_cli(
                "--json", "--cases", str(path), "--only", "drift_case"
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual([r["id"] for r in payload["rows"]], ["drift_case"])

    def test_cli_only_unknown_id_yields_no_rows(self) -> None:
        proc = self.run_cli("--json", "--only", "nope")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["rows"], [])

    def test_env_cases_and_only(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text(json.dumps(CASES), encoding="utf-8")
            env = dict(
                os.environ,
                JEV_COMPARE_CASES=str(path),
                JEV_COMPARE_ONLY="drift_case",
            )
            proc = subprocess.run(
                [sys.executable, str(COMPARE), "--json"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual([r["id"] for r in payload["rows"]], ["drift_case"])

    def test_cli_bad_cases_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text("{}", encoding="utf-8")
            proc = self.run_cli("--cases", str(path))
        self.assertNotEqual(proc.returncode, 0)


class StrictGateTest(unittest.TestCase):
    def run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "compare", *argv],
            capture_output=True,
            text=True,
            cwd=str(ROOT / "skills" / "jev-consult" / "scripts"),
        )

    def test_strict_failures_offline(self) -> None:
        good = [{"id": "a", "after": {"called_jev": True}}]
        bad = [{"id": "b", "after": {"called_jev": False}}]
        self.assertEqual(compare.strict_failures(good, False), [])
        self.assertEqual(len(compare.strict_failures(bad, False)), 1)
        self.assertIn("did not call Jev", compare.strict_failures(bad, False)[0])

    def test_strict_failures_live_noul(self) -> None:
        rows = [
            {"id": "ok", "after": {"called_jev": True, "noul": 0.9}},
            {"id": "low", "after": {"called_jev": True, "noul": 0.4}},
            {"id": "none", "after": {"called_jev": True}},  # no noul: skip check
        ]
        failures = compare.strict_failures(rows, True)
        self.assertEqual(len(failures), 1)
        self.assertIn("low", failures[0])

    def test_cli_strict_rc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps(CASES), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "skills" / "jev-consult" / "scripts" / "compare.py"),
                 "--strict", "--cases", str(good)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            bad_cases = json.loads(json.dumps(CASES))
            bad_cases["cases"][0]["after"]["called_jev"] = False
            bad = Path(tmp) / "bad.json"
            bad.write_text(json.dumps(bad_cases), encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "skills" / "jev-consult" / "scripts" / "compare.py"),
                 "--strict", "--cases", str(bad)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn("strict:", proc.stderr)


if __name__ == "__main__":
    unittest.main()
