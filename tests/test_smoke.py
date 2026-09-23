#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for skills/jev-consult/scripts/smoke.py."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "skills" / "jev-consult" / "scripts" / "smoke.py"
SPEC = importlib.util.spec_from_file_location("jev_smoke", SMOKE)
MOD = importlib.util.module_from_spec(SPEC)
sys.modules["jev_smoke"] = MOD
SPEC.loader.exec_module(MOD)


class SmokeTests(unittest.TestCase):
    def test_full_smoke_green(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE)], capture_output=True, text=True, timeout=120
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["steps"]), 23)
        names = {s["name"] for s in out["steps"]}
        self.assertEqual(
            names,
            {
                "policy",
                "policy_lint",
                "jev_scaffold_lint",
                "inventory",
                "compact_fake",
                "decisions",
                "trace",
                "skill_lint",
                "question_lint",
                "compare",
                "apply_fill",
                "hook",
                "doctor_json",
                "trigger_lint",
                "trigger_eval",
                "compact_hook",
                "jev_decide",
                "peer_fill_status",
                "catalog_fill",
                "perf",
                "ask_verdict",
                "install",
                "self_test",
            },
        )

    def test_self_test_detects_synthetic_failure(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--self-test", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["self_test"], "ok")
        self.assertEqual(out["failed"], ["selftest_bad"])
        self.assertEqual(len(out["steps"]), 2)

        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--self-test"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("self-test: ok", proc.stdout)

    def test_only_runs_subset(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy,trace"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual({s["name"] for s in out["steps"]}, {"policy", "trace"})

    def test_jobs_runs_subset_in_step_order(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "trace,policy,decisions",
             "--jobs", "3"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertTrue(out["ok"])
        self.assertEqual(
            [s["name"] for s in out["steps"]],
            ["policy", "decisions", "trace"],
        )

    def test_jobs_env_presets_workers(self) -> None:
        env = dict(os.environ, JEV_SMOKE_JOBS="2")
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy,trace"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_only_env_presets_steps(self) -> None:
        env = dict(os.environ, JEV_SMOKE_ONLY="policy,trace")
        proc = subprocess.run(
            [sys.executable, str(SMOKE)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        out = json.loads(proc.stdout)
        self.assertEqual({s["name"] for s in out["steps"]}, {"policy", "trace"})
        env = dict(os.environ, JEV_SMOKE_ONLY="trace")
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        out = json.loads(proc.stdout)
        self.assertEqual({s["name"] for s in out["steps"]}, {"policy"})

    def test_repeat_runs_step_multiple_times(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy", "--repeat", "2"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual([s["name"] for s in out["steps"]], ["policy"])
        self.assertTrue(all(s["ok"] for s in out["steps"]))

    def test_repeat_env_presets_attempts(self) -> None:
        env = dict(os.environ, JEV_SMOKE_REPEAT="2")
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_only_unknown_step_rc2(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "bogus"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unknown step", proc.stderr)

    def test_out_writes_results_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "smoke.json"
            proc = subprocess.run(
                [sys.executable, str(SMOKE), "--only", "policy", "--out", str(out_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("wrote", proc.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual({s["name"] for s in payload["steps"]}, {"policy"})

    def test_jq_prints_one_field_of_result(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy", "--jq", "ok"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), True)
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy", "--jq", "nope"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("bad --jq key", proc.stderr)

    def test_watch_jq_prints_only_the_named_tick_field(self) -> None:
        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "2"}), patch.object(
            MOD, "_run", return_value=(0, "ok")
        ):
            import io

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf), patch.object(
                sys, "stderr", io.StringIO()
            ):
                rc = MOD.main(["--watch", "0.001", "--only", "policy", "--jq", "ok,failed"])
        self.assertEqual(rc, 0)
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "true")
        self.assertEqual(json.loads(lines[1]), [])

    def test_report_writes_markdown_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "smoke.md"
            proc = subprocess.run(
                [sys.executable, str(SMOKE), "--only", "policy",
                 "--report", str(report)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("wrote", proc.stderr)
            text = report.read_text(encoding="utf-8")
            self.assertIn("verdict: **PASS**", text)
            self.assertIn("- steps: 1", text)
            self.assertIn("| policy | yes |", text)

    def test_verdict_writes_slim_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            proc = subprocess.run(
                [sys.executable, str(SMOKE), "--only", "policy",
                 "--verdict", str(verdict)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "PASS")
            self.assertEqual(payload["steps"], 1)
            self.assertEqual(payload["failed"], [])

    def test_verdict_watch_writes_final_state(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            env = dict(os.environ, JEV_SMOKE_WATCH_MAX="1")
            proc = subprocess.run(
                [sys.executable, str(SMOKE), "--only", "policy",
                 "--watch", "0.01", "--verdict", str(verdict)],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "PASS")

    def test_only_doctor_json_uses_emitted_name(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "doctor_json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = json.loads(proc.stdout)
        self.assertEqual({s["name"] for s in out["steps"]}, {"doctor_json"})

    def test_list_prints_step_names(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--list"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        names = set(proc.stdout.strip().splitlines())
        self.assertEqual(len(names), 23)
        self.assertIn("doctor_json", names)
        self.assertIn("hook", names)
        self.assertIn("perf", names)

    def test_list_json_emits_array(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--list", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        names = json.loads(proc.stdout.strip())
        self.assertEqual(len(names), 23)
        self.assertIn("doctor_json", names)
        self.assertIn("perf", names)
        self.assertEqual(names, sorted(names))

    def test_coverage_reports_every_script_covered(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--coverage", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["uncovered"], [])
        self.assertEqual(payload["steps"], 23)
        self.assertIn("inventory_hook.py", payload["covered"])
        self.assertIn("smoke.py", payload["step_scripts"]["self_test"])
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--coverage"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("coverage:", proc.stdout)
        self.assertNotIn("uncovered:", proc.stdout)

    def test_self_test_step_passes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            row = MOD.step_self_test(Path(tmp))
        self.assertTrue(row["ok"], row)
        self.assertEqual(row["name"], "self_test")

    def test_fail_fast_stops_after_first_failure(self) -> None:
        def boom(tmp):
            raise RuntimeError("explode")

        with patch.object(MOD, "_run", return_value=(1, "nope")), patch.object(
            MOD, "step_policy", side_effect=boom
        ):
            import io

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = MOD.main(["--fail-fast"])
        self.assertEqual(rc, 1)
        out = json.loads(buf.getvalue())
        self.assertEqual(len(out["steps"]), 1)
        self.assertEqual(out["steps"][0]["name"], "step")
        self.assertIn("explode", out["steps"][0]["detail"])

    def test_step_failure_marks_not_ok(self) -> None:
        def boom(tmp):
            raise RuntimeError("explode")

        with patch.object(MOD, "_run", return_value=(1, "nope")), patch.object(
            MOD, "step_policy", side_effect=boom
        ):
            import io

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = MOD.main([])
        self.assertEqual(rc, 1)
        out = json.loads(buf.getvalue())
        self.assertFalse(out["ok"])
        self.assertIn("explode", out["steps"][0]["detail"])

    def test_timeout_flag_sets_step_timeout(self) -> None:
        def boom(tmp):
            raise RuntimeError("explode")

        with patch.object(MOD, "_run", return_value=(1, "nope")), patch.object(
            MOD, "step_policy", side_effect=boom
        ):
            import io

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = MOD.main(["--timeout", "7", "--fail-fast"])
        self.assertEqual(rc, 1)
        self.assertEqual(MOD.STEP_TIMEOUT, 7.0)
        MOD.STEP_TIMEOUT = 60.0

    def test_timeout_env_sets_step_timeout(self) -> None:
        import os

        def boom(tmp):
            raise RuntimeError("explode")

        with patch.dict(os.environ, {"JEV_SMOKE_TIMEOUT": "9"}):
            with patch.object(MOD, "_run", return_value=(1, "nope")), patch.object(
                MOD, "step_policy", side_effect=boom
            ):
                import io

                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(["--fail-fast"])
        self.assertEqual(rc, 1)
        self.assertEqual(MOD.STEP_TIMEOUT, 9.0)
        MOD.STEP_TIMEOUT = 60.0

    def test_watch_emits_ticks(self) -> None:
        def boom(tmp):
            raise RuntimeError("explode")

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "2"}):
            with patch.object(MOD, "_run", return_value=(1, "nope")), patch.object(
                MOD, "step_policy", side_effect=boom
            ):
                import io

                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(["--watch", "0.001", "--only", "policy"])
        self.assertEqual(rc, 1)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 2)
        self.assertTrue(all(t["ok"] is False for t in ticks))
        self.assertTrue(all(t["failed"] for t in ticks))

    def test_junit_writes_xml(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        def bad_step(tmp):
            return {"name": "compact", "ok": False, "detail": "broke <x>"}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "j.xml"
            with patch.object(MOD, "step_policy", side_effect=ok_step), patch.object(
                MOD, "step_compact_fake", side_effect=bad_step
            ):
                import io

                with patch.object(sys, "stdout", io.StringIO()):
                    rc = MOD.main(["--only", "policy,compact_fake", "--junit", str(out)])
            self.assertEqual(rc, 1)
            import xml.etree.ElementTree as ET

            root = ET.fromstring(out.read_text(encoding="utf-8"))
            self.assertEqual(root.tag, "testsuite")
            self.assertEqual(root.get("tests"), "2")
            self.assertEqual(root.get("failures"), "1")
            cases = root.findall("testcase")
            self.assertEqual([c.get("name") for c in cases], ["policy", "compact"])
            self.assertEqual(
                [c.get("classname") for c in cases],
                ["jev-consult.smoke"] * 2,
            )
            failure = cases[1].find("failure")
            self.assertIsNotNone(failure)
            self.assertIn("broke <x>", failure.text)

    def test_junit_write_is_atomic(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "j.xml"
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                import io

                with patch.object(sys, "stdout", io.StringIO()):
                    rc = MOD.main(["--only", "policy", "--junit", str(out)])
            self.assertEqual(rc, 0)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["j.xml"])

    def test_junit_watch_writes_final_pass(self) -> None:
        import xml.etree.ElementTree as ET

        calls = []

        def ok_step(tmp):
            calls.append(1)
            return {"name": "policy", "ok": True, "detail": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "j.xml"
            ticks = Path(tmp) / "ticks.jsonl"
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                import io

                with patch.object(sys, "stdout", io.StringIO()), patch.object(
                    sys, "stderr", io.StringIO()
                ):
                    rc = MOD.main(
                        [
                            "--only",
                            "policy",
                            "--watch",
                            "0.01",
                            "--max-ticks",
                            "2",
                            "--quiet",
                            "--out",
                            str(ticks),
                            "--junit",
                            str(out),
                        ]
                    )
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 2, "expected exactly two watch ticks")
            root = ET.fromstring(out.read_text(encoding="utf-8"))
            self.assertEqual(root.tag, "testsuite")
            self.assertEqual(root.get("tests"), "1")
            self.assertEqual(root.get("failures"), "0")
            self.assertEqual(root.find("testcase").get("name"), "policy")
            lines = [x for x in ticks.read_text(encoding="utf-8").splitlines() if x.strip()]
            self.assertEqual(len(lines), 2, "watch emitted %d ticks" % len(lines))

    def test_junit_escapes_quotes_in_names(self) -> None:
        import xml.etree.ElementTree as ET

        xml_text = MOD.junit_xml(
            [{"name": 'say "hi" <now>', "ok": False, "detail": 'bad "quote"'}]
        )
        root = ET.fromstring(xml_text)
        case = root.find("testcase")
        self.assertEqual(case.get("name"), 'say "hi" <now>')
        self.assertEqual(case.find("failure").text, 'bad "quote"')

    def test_junit_none_when_flag_absent(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                import io

                with patch.object(sys, "stdout", io.StringIO()):
                    rc = MOD.main(["--only", "policy"])
            self.assertEqual(rc, 0)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_watch_writes_stderr_tick_summary(self) -> None:
        env = dict(os.environ, JEV_SMOKE_WATCH_MAX="2")
        proc = subprocess.run(
            [sys.executable, str(SMOKE), "--only", "policy",
             "--watch", "0.05"],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [
            l for l in proc.stderr.splitlines() if l.startswith("watch tick=")
        ]
        self.assertEqual(len(lines), 2)
        self.assertIn("ok=True", lines[0])

    def test_watch_tick_reports_elapsed_s(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "2"}):
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                import io

                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(["--watch", "0.001", "--only", "policy"])
        self.assertEqual(rc, 0)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 2)
        self.assertTrue(all(isinstance(t["elapsed_s"], float) for t in ticks))
        self.assertGreaterEqual(ticks[1]["elapsed_s"], ticks[0]["elapsed_s"])

    def test_watch_fail_fast_breaks_on_first_failing_tick(self) -> None:
        def boom(tmp):
            raise RuntimeError("explode")

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "9"}):
            with patch.object(MOD, "step_policy", side_effect=boom):
                import io

                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(
                        ["--watch", "0.001", "--only", "policy", "--fail-fast"]
                    )
        self.assertEqual(rc, 1)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 1)

    def test_watch_rc_0_when_steps_pass(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "1"}):
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                import io

                with patch.object(sys, "stdout", io.StringIO()):
                    rc = MOD.main(["--watch", "0.001", "--only", "policy"])
        self.assertEqual(rc, 0)

    def test_watch_appends_ticks_to_out_file(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ticks.jsonl"
            with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "2"}):
                with patch.object(MOD, "step_policy", side_effect=ok_step):
                    with patch.object(sys, "stdout", __import__("io").StringIO()):
                        rc = MOD.main(
                            ["--watch", "0.001", "--only", "policy",
                             "--out", str(out)]
                        )
            self.assertEqual(rc, 0)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(t["ok"] is True for t in lines))

    def test_max_ticks_flag_overrides_env_cap(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        import io

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "9"}):
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(
                        ["--watch", "0.001", "--only", "policy",
                         "--max-ticks", "3"]
                    )
        self.assertEqual(rc, 0)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 3)

    def test_watch_max_seconds_bounds_loop(self) -> None:
        import io
        import time

        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "0"}):
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                with patch("time.sleep"):
                    buf = io.StringIO()
                    start = time.time()
                    with patch.object(sys, "stdout", buf):
                        rc = MOD.main(
                            ["--watch", "0.02", "--only", "policy",
                             "--watch-max", "0.06"]
                        )
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - start, 2.0)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertTrue(len(ticks) >= 1)

    def test_watch_quiet_suppresses_ok_ticks(self) -> None:
        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        import io

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "3"}):
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(
                        ["--watch", "0.001", "--only", "policy", "--quiet"]
                    )
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

        def bad_step(tmp):
            return {"name": "policy", "ok": False, "detail": "boom"}

        with patch.dict(os.environ, {"JEV_SMOKE_WATCH_MAX": "2"}):
            with patch.object(MOD, "step_policy", side_effect=bad_step):
                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(
                        ["--watch", "0.001", "--only", "policy", "--quiet"]
                    )
        self.assertEqual(rc, 1)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 2)

    def test_policy_step_real(self) -> None:
        from pathlib import Path as P
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            step = MOD.step_policy(P(tmp))
        self.assertTrue(step["ok"], step)


class WatchQuietEnvTests(unittest.TestCase):
    def test_watch_quiet_env_presets_quiet(self) -> None:
        import io

        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with patch.dict(
            os.environ,
            {"JEV_SMOKE_WATCH_MAX": "2", "JEV_SMOKE_WATCH_QUIET": "1"},
        ):
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(["--watch", "0.001", "--only", "policy"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "")

class WatchDeadlineEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import io
        import time

        def ok_step(tmp):
            return {"name": "policy", "ok": True, "detail": "fake"}

        with patch.dict(
            os.environ,
            {"JEV_SMOKE_WATCH_MAX": "0", "JEV_SMOKE_WATCH_SECS": "0.05"},
        ):
            with patch.object(MOD, "step_policy", side_effect=ok_step):
                buf = io.StringIO()
                start = time.time()
                with patch.object(sys, "stdout", buf):
                    rc = MOD.main(["--watch", "0.02", "--only", "policy"])
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - start, 2.0)
        ticks = [l for l in buf.getvalue().splitlines() if l.startswith("{")]
        self.assertLessEqual(len(ticks), 10)
        self.assertGreaterEqual(len(ticks), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
