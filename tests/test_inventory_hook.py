#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INV = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory.py", "jev_inventory")
HOOK = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory_hook.py", "jev_inventory_hook")
INSTALL = load(ROOT / "scripts" / "install.py", "jev_install_tools")
FIXTURE = ROOT / "tests" / "fixtures" / "inventory-harness"


def skip_pick(*_args, **_kwargs):
    return {"status": "skip", "winner": None}


class InventoryHookTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep test runs out of the real ~/.cache decisions log.
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def test_claude_injects_jwt_and_writes_sidecar(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("jwt-auth", note)
            self.assertNotIn("ascii-art", note)
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["harness"], "claude-code")
        names = [row["name"] for row in sidecar["names"]]
        self.assertIn("jwt-auth", names)
        self.assertEqual(
            HOOK.LAST_DECISION["shortlist_n"],
            len(HOOK.LAST_DECISION["shortlist"]),
        )

    def test_note_limit_env_truncates_list(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_NOTE_LIMIT": "1"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "ascii-art plus authorized-scan for this task",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(note.count("- skill"), 1)
            self.assertGreater(HOOK.LAST_DECISION["shortlist_n"], 1)

    def test_last_decision_records_shortlist_score_avg(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            avg = HOOK.LAST_DECISION["shortlist_score_avg"]
            self.assertIsInstance(avg, float)
            self.assertGreater(avg, 0)

    def test_avg_score_none_for_empty_query(self) -> None:
        self.assertIsNone(HOOK._avg_score([{"name": "x"}], [], ""))

    def test_hook_limit_env_narrows_shortlist(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_LIMIT": "1"}):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "jwt scan",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertEqual(HOOK.LAST_DECISION.get("shortlist_n"), 1)
            tmp2 = Path(tmp) / "other"
            tmp2.mkdir()
            with patch.dict(os.environ, {"JEV_HOOK_LIMIT": "3"}):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "jwt scan",
                        "cwd": str(tmp2),
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertEqual(HOOK.LAST_DECISION.get("shortlist_n"), 2)

    def test_max_age_env_skips_stale_prompt(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_MAX_AGE": "30"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                        "timestamp": 1700000000,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertEqual(out, {})
            fresh = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertIn("jwt-auth", fresh["hookSpecificOutput"]["additionalContext"])

    def test_last_decision_age_helper(self) -> None:
        saved = HOOK.LAST_DECISION
        try:
            HOOK.LAST_DECISION = None
            self.assertIsNone(HOOK.last_decision_age())
            HOOK.LAST_DECISION = {"ts": 100.0}
            age = HOOK.last_decision_age()
            self.assertIsNotNone(age)
            self.assertGreater(age, 0)
        finally:
            HOOK.LAST_DECISION = saved

    def test_sidecar_records_note_sha(self) -> None:
        import hashlib
        import json

        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text())
            self.assertEqual(
                sidecar["note_sha"],
                hashlib.sha256(note.encode("utf-8")).hexdigest()[:12],
            )

    def test_prompt_env_supplies_missing_prompt(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ, {"JEV_HOOK_PROMPT": "Add JWT access tokens in Python"}
            ):
                out = HOOK.handle(
                    {"hook_event_name": "UserPromptSubmit", "cwd": tmp},
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertIn("jwt-auth", out["hookSpecificOutput"]["additionalContext"])

    def test_last_decision_records_prompt_len(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertEqual(
                HOOK.LAST_DECISION["prompt_len"],
                len("Add JWT access tokens in Python"),
            )
            self.assertEqual(
                HOOK.LAST_DECISION["prompt_tail"],
                "Add JWT access tokens in Python",
            )

    def test_hook_winner_env_forces_explicit_pick(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_WINNER": "ascii-art"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("ascii-art", note)
            self.assertTrue(HOOK.LAST_DECISION["explicit"])
            self.assertEqual(HOOK.LAST_DECISION["jev_status"], "winner")
            self.assertEqual(HOOK.LAST_DECISION["question"], "env")

    def test_question_marks_pick_source(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Add JWT access tokens in Python",
                "cwd": tmp,
            }
            HOOK.handle(payload, items=items, harness="claude-code", pick_fn=skip_pick)
            self.assertIsNone(HOOK.LAST_DECISION["question"])
            HOOK.handle(payload, items=items, harness="claude-code", pick_fn=skip_pick)
            self.assertEqual(HOOK.LAST_DECISION["question"], "dedupe")

    def test_last_decision_records_sidecar_age(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Add JWT access tokens in Python",
                "cwd": tmp,
            }
            HOOK.handle(payload, items=items, harness="claude-code", pick_fn=skip_pick)
            self.assertIsNone(HOOK.LAST_DECISION["sidecar_age_s"])
            sidecar_path = Path(tmp) / ".jev-tools.json"
            blob = json.loads(sidecar_path.read_text(encoding="utf-8"))
            blob["written_at"] = int(blob["written_at"]) - 42
            sidecar_path.write_text(json.dumps(blob), encoding="utf-8")
            HOOK.handle(payload, items=items, harness="claude-code", pick_fn=skip_pick)
            self.assertEqual(HOOK.LAST_DECISION["sidecar_age_s"], 42)
            self.assertEqual(HOOK.LAST_DECISION["question"], "dedupe")

    def test_stale_sidecar_triggers_repick(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        calls = []

        def counting_pick(*args, **kwargs):
            calls.append(1)
            return skip_pick(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Add JWT access tokens in Python",
                "cwd": tmp,
            }
            HOOK.handle(payload, items=items, harness="claude-code", pick_fn=counting_pick)
            self.assertEqual(len(calls), 1)
            self.assertFalse(HOOK.LAST_DECISION["stale_sidecar"])
            sidecar_path = Path(tmp) / ".jev-tools.json"
            blob = json.loads(sidecar_path.read_text(encoding="utf-8"))
            blob["written_at"] = int(blob["written_at"]) - (INV.sidecar_ttl_seconds() + 60)
            sidecar_path.write_text(json.dumps(blob), encoding="utf-8")
            HOOK.handle(payload, items=items, harness="claude-code", pick_fn=counting_pick)
            self.assertEqual(len(calls), 2)
            self.assertTrue(HOOK.LAST_DECISION["stale_sidecar"])
            self.assertNotEqual(HOOK.LAST_DECISION["question"], "dedupe")
            self.assertFalse(HOOK.LAST_DECISION.get("dedupe"))

    def test_watch_file_emits_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "event": "UserPromptSubmit",
                        "prompt": "jwt",
                        "cwd": str(cwd),
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(
                os.environ, {"JEV_HOOK_WATCH_MAX": "2", "JEV_HOOK_OFF": "1"}
            ):
                with patch("sys.stdout", buf):
                    rc = HOOK.main(
                        ["--file", str(payload), "--watch", "0.01"]
                    )
            self.assertEqual(rc, 1)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(t["keys"] == [] for t in ticks))
            self.assertTrue(all(t["winner"] is None for t in ticks))

    def test_watch_fail_fast_breaks_on_winnerless_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text(
                '{"event": "UserPromptSubmit", "prompt": "p"}', encoding="utf-8"
            )
            buf = io.StringIO()
            with patch.dict(
                os.environ, {"JEV_HOOK_WATCH_MAX": "9", "JEV_HOOK_OFF": "1"}
            ):
                with patch("sys.stdout", buf):
                    rc = HOOK.main(
                        ["--file", str(payload), "--watch", "0.01", "--fail-fast"]
                    )
            self.assertEqual(rc, 1)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertIsNone(ticks[0]["winner"])

    def test_watch_writes_stderr_tick_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text(
                '{"event": "UserPromptSubmit", "prompt": "p"}', encoding="utf-8"
            )
            err = io.StringIO()
            with patch.dict(
                os.environ, {"JEV_HOOK_WATCH_MAX": "2", "JEV_HOOK_OFF": "1"}
            ):
                with patch("sys.stdout", io.StringIO()):
                    with patch("sys.stderr", err):
                        rc = HOOK.main(
                            ["--file", str(payload), "--watch", "0.01"]
                        )
            self.assertEqual(rc, 1)
            lines = [
                l for l in err.getvalue().splitlines() if l.startswith("watch tick=")
            ]
            self.assertEqual(len(lines), 2)
            self.assertIn("winner=-", lines[0])

    def test_watch_tick_reports_winner_changed(self) -> None:
        calls = []

        def fake_handle(payload):
            calls.append(1)
            HOOK.LAST_DECISION = {"winner": {"name": "w%d" % len(calls)}}
            return {"note": "x"}

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text('{"event": "UserPromptSubmit", "prompt": "p"}',
                               encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_HOOK_WATCH_MAX": "2"}):
                with patch.object(HOOK, "handle", side_effect=fake_handle):
                    with patch("sys.stdout", buf):
                        rc = HOOK.main(["--file", str(payload), "--watch", "0.01"])
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertFalse(ticks[0]["winner_changed"])
            self.assertTrue(ticks[1]["winner_changed"])

    def test_watch_verdict_reports_winner_stability(self) -> None:
        winners = iter(["a-tool", "b-tool"])

        def fake_handle(payload):
            HOOK.LAST_DECISION = {"winner": {"name": next(winners)}}
            return {"note": "x"}

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text('{"event": "UserPromptSubmit", "prompt": "p"}',
                               encoding="utf-8")
            verdict = Path(tmp) / "verdict.json"
            with patch.dict(os.environ, {"JEV_HOOK_WATCH_MAX": "2"}):
                with patch.object(HOOK, "handle", side_effect=fake_handle):
                    with patch("sys.stdout", io.StringIO()):
                        rc = HOOK.main(
                            [
                                "--file", str(payload),
                                "--watch", "0.01",
                                "--verdict", str(verdict),
                            ]
                        )
            self.assertEqual(rc, 0)
            out = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(out["winner_stability"], 2)
            self.assertEqual(out["winner"], "b-tool")
            self.assertEqual(out["winner_changes"], 1)
            self.assertEqual(out["winner_flap_rate"], 0.5)

    def test_watch_verdict_flap_rate_zero_when_stable(self) -> None:
        def fake_handle(payload):
            HOOK.LAST_DECISION = {"winner": {"name": "same"}}
            return {"note": "x"}

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text('{"event": "UserPromptSubmit", "prompt": "p"}',
                               encoding="utf-8")
            verdict = Path(tmp) / "verdict.json"
            with patch.dict(os.environ, {"JEV_HOOK_WATCH_MAX": "2"}):
                with patch.object(HOOK, "handle", side_effect=fake_handle):
                    with patch("sys.stdout", io.StringIO()):
                        rc = HOOK.main(
                            [
                                "--file", str(payload),
                                "--watch", "0.01",
                                "--verdict", str(verdict),
                            ]
                        )
            self.assertEqual(rc, 0)
            out = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(out["winner_changes"], 0)
            self.assertEqual(out["winner_flap_rate"], 0.0)

    def test_watch_verdict_winner_stability_one_when_stable(self) -> None:
        def fake_handle(payload):
            HOOK.LAST_DECISION = {"winner": {"name": "same"}}
            return {"note": "x"}

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text('{"event": "UserPromptSubmit", "prompt": "p"}',
                               encoding="utf-8")
            verdict = Path(tmp) / "verdict.json"
            with patch.dict(os.environ, {"JEV_HOOK_WATCH_MAX": "2"}):
                with patch.object(HOOK, "handle", side_effect=fake_handle):
                    with patch("sys.stdout", io.StringIO()):
                        rc = HOOK.main(
                            [
                                "--file", str(payload),
                                "--watch", "0.01",
                                "--verdict", str(verdict),
                            ]
                        )
            self.assertEqual(rc, 0)
            out = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(out["winner_stability"], 1)

    def test_watch_tick_winner_changed_false_when_stable(self) -> None:
        def fake_handle(payload):
            HOOK.LAST_DECISION = {"winner": {"name": "same"}}
            return {"note": "x"}

        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text('{"event": "UserPromptSubmit", "prompt": "p"}',
                               encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_HOOK_WATCH_MAX": "2"}):
                with patch.object(HOOK, "handle", side_effect=fake_handle):
                    with patch("sys.stdout", buf):
                        rc = HOOK.main(["--file", str(payload), "--watch", "0.01"])
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual([t["winner_changed"] for t in ticks], [False, False])
            self.assertTrue(all(isinstance(t["elapsed_s"], float) for t in ticks))
            self.assertGreaterEqual(ticks[1]["elapsed_s"], ticks[0]["elapsed_s"])

    def test_watch_rc_0_when_last_tick_has_winner(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "event": "UserPromptSubmit",
                        "prompt": "jwt",
                        "cwd": str(cwd),
                        "harness": "claude-code",
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            env = {
                "JEV_HOOK_WATCH_MAX": "1",
                "JEV_HOOK_WINNER": "ascii-art",
                "JEV_HOOK_NOSIDECAR": "1",
                "JEV_HOOK_NOMISS": "1",
            }
            with patch.dict(os.environ, env):
                with patch.object(HOOK, "scan_cached", return_value=items):
                    with patch("sys.stdout", buf):
                        rc = HOOK.main(
                            ["--file", str(payload), "--watch", "0.01"]
                        )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(ticks[0]["winner"], "ascii-art")

    def test_watch_verdict_writes_final_state(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "event": "UserPromptSubmit",
                        "prompt": "jwt",
                        "cwd": str(cwd),
                        "harness": "claude-code",
                    }
                ),
                encoding="utf-8",
            )
            verdict = cwd / "v.json"
            env = {
                "JEV_HOOK_WATCH_MAX": "1",
                "JEV_HOOK_WINNER": "ascii-art",
                "JEV_HOOK_NOSIDECAR": "1",
                "JEV_HOOK_NOMISS": "1",
            }
            with patch.dict(os.environ, env):
                with patch.object(HOOK, "scan_cached", return_value=items):
                    with patch("sys.stdout", io.StringIO()):
                        rc = HOOK.main(
                            [
                                "--file", str(payload), "--watch", "0.01",
                                "--verdict", str(verdict),
                            ]
                        )
            self.assertEqual(rc, 0)
            out = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(out["verdict"], "pass")
            self.assertEqual(out["ticks"], 1)
            self.assertEqual(out["winner"], "ascii-art")
            self.assertIsInstance(out["keys"], list)
            self.assertEqual(out["keys_count"], len(out["keys"]))
            self.assertIn("elapsed_s", out)

    def test_watch_appends_ticks_to_out_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {"event": "UserPromptSubmit", "prompt": "jwt", "cwd": str(cwd)}
                ),
                encoding="utf-8",
            )
            out = cwd / "ticks.jsonl"
            env = {
                "JEV_HOOK_WATCH_MAX": "2",
                "JEV_HOOK_OFF": "1",
            }
            with patch.dict(os.environ, env):
                with patch("sys.stdout", io.StringIO()):
                    HOOK.main(
                        ["--file", str(payload), "--watch", "0.01",
                         "--out", str(out)]
                    )
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("winner" in t and "keys" in t for t in lines))

    def test_file_flag_reads_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text("{}\n", encoding="utf-8")
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--file", str(payload)])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "{}")

    def test_nonwatch_verdict_writes_decision(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "event": "UserPromptSubmit",
                        "prompt": "jwt",
                        "cwd": str(cwd),
                        "harness": "claude-code",
                    }
                ),
                encoding="utf-8",
            )
            verdict = cwd / "v.json"
            env = {
                "JEV_HOOK_WINNER": "ascii-art",
                "JEV_HOOK_NOSIDECAR": "1",
                "JEV_HOOK_NOMISS": "1",
            }
            with patch.dict(os.environ, env):
                with patch.object(HOOK, "scan_cached", return_value=items):
                    with patch("sys.stdout", io.StringIO()):
                        rc = HOOK.main(
                            ["--file", str(payload), "--verdict", str(verdict)]
                        )
            self.assertEqual(rc, 0)
            out = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(out["verdict"], "pass")
            self.assertEqual(out["winner"], "ascii-art")
            self.assertEqual(out["ticks"], 1)

    def test_nonwatch_verdict_fail_when_no_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {"event": "UserPromptSubmit", "prompt": "x", "cwd": str(cwd)}
                ),
                encoding="utf-8",
            )
            verdict = cwd / "v.json"
            with patch.dict(os.environ, {"JEV_HOOK_OFF": "1"}):
                with patch("sys.stdout", io.StringIO()):
                    rc = HOOK.main(
                        ["--file", str(payload), "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            out = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(out["verdict"], "fail")
            self.assertIsNone(out["winner"])

    def test_events_flag_lists_allowed_events(self) -> None:
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_HOOK_EVENTS": ""}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--events"])
        self.assertEqual(rc, 0)
        names = buf.getvalue().split()
        self.assertEqual(names, ["UserPromptSubmit", "pre_llm_call"])
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_HOOK_EVENTS": "b_event,a_event"}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--events"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().split(), ["a_event", "b_event"])
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_HOOK_EVENTS": ""}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--events", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            json.loads(buf.getvalue()), ["UserPromptSubmit", "pre_llm_call"]
        )
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_HOOK_EVENTS": ""}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--events", "--jsonl"])
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in buf.getvalue().splitlines()]
        self.assertEqual(
            rows, [{"event": "UserPromptSubmit"}, {"event": "pre_llm_call"}]
        )

    def test_env_flag_reports_resolved_config(self) -> None:
        buf = io.StringIO()
        with patch.dict(
            os.environ,
            {"JEV_HOOK_LIMIT": "3", "JEV_HOOK_OFF": "1", "JEV_HOOK_WINNER": "ascii-art"},
        ):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--env"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["limit"], 3)
        self.assertTrue(report["jev_hook_off"])
        self.assertTrue(report["jev_hook_winner"])
        self.assertFalse(report["jev_hook_nomiss"])
        self.assertEqual(report["dedupe_ttl_seconds"], 0.0)
        self.assertNotIn("api_key", buf.getvalue().lower())

    def test_env_jq_prints_one_value(self) -> None:
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_HOOK_LIMIT": "3"}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--env", "--jq", "limit"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), 3)

    def test_env_jq_bad_key_is_usage_error(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            rc = HOOK.main(["--env", "--jq", "nope"])
        self.assertEqual(rc, 2)
        self.assertEqual(buf.getvalue(), "")
        self.assertIn("bad --jq key", err.getvalue())

    def test_env_report_includes_cwd_sidecar_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".jev-tools.json").write_text("{}", encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_HOOK_CWD": tmp}):
                with patch("sys.stdout", buf):
                    rc = HOOK.main(["--env"])
            self.assertEqual(rc, 0)
            report = json.loads(buf.getvalue())
            self.assertTrue(report["sidecar_present"])
            self.assertFalse(report["miss_present"])

    def test_env_report_shows_policy_source(self) -> None:
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_POLICY": ""}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--env"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["policy"], "default")

        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_POLICY": "C:/x/policy-copy.json"}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--env"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["policy"], "C:/x/policy-copy.json")

    def test_env_out_writes_report_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "env.json"
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_HOOK_LIMIT": "3"}):
                with patch("sys.stdout", buf):
                    rc = HOOK.main(["--env", "--out", str(target)])
            self.assertEqual(rc, 0)
            report = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(report["limit"], 3)
            self.assertEqual(json.loads(buf.getvalue())["limit"], 3)

    def test_hook_skip_events_excludes_events(self) -> None:
        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_HOOK_SKIP_EVENTS": "pre_llm_call"}):
            with patch("sys.stdout", buf):
                rc = HOOK.main(["--events"])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().split(), ["UserPromptSubmit"])
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_SKIP_EVENTS": "UserPromptSubmit"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertEqual(out, {})
            self.assertIsNone(HOOK.LAST_DECISION)

    def test_over_budget_flag_when_pick_exceeds_budget(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)

        def slow_pick(*_a, **_kw):
            return {"status": "idf", "winner": None, "latency_ms": 99999}

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_BUDGET": "12"}):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=slow_pick,
                )
            self.assertEqual(HOOK.LAST_DECISION["budget_ms"], 12000)
            self.assertTrue(HOOK.LAST_DECISION["over_budget"])

    def test_hook_events_env_overrides_allowed_events(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_EVENTS": "pre_llm_call"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
                self.assertEqual(out, {})
            with patch.dict(os.environ, {"JEV_HOOK_EVENTS": "CustomEvent"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "CustomEvent",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
                self.assertIn("hookSpecificOutput", out)

    def test_dedupe_ttl_forces_repick(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        calls = []

        def counting_pick(*args, **kwargs):
            calls.append(1)
            return skip_pick(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Add JWT access tokens in Python",
                "cwd": tmp,
            }
            HOOK.handle(payload, items=items, harness="claude-code", pick_fn=counting_pick)
            self.assertEqual(len(calls), 1)
            sidecar_path = Path(tmp) / ".jev-tools.json"
            blob = json.loads(sidecar_path.read_text(encoding="utf-8"))
            blob["written_at"] = int(blob["written_at"]) - 120
            sidecar_path.write_text(json.dumps(blob), encoding="utf-8")
            # dedupe TTL off (0): fresh sidecar still dedupes the same prompt
            with patch.dict(os.environ, {"JEV_HOOK_DEDUPE_TTL": "0"}):
                HOOK.handle(payload, items=items, harness="claude-code", pick_fn=counting_pick)
            self.assertEqual(len(calls), 1)
            self.assertEqual(HOOK.LAST_DECISION["question"], "dedupe")
            # TTL 30 < age 120: repeat prompt re-runs the pick instead of deduping
            with patch.dict(os.environ, {"JEV_HOOK_DEDUPE_TTL": "30"}):
                HOOK.handle(payload, items=items, harness="claude-code", pick_fn=counting_pick)
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(HOOK.LAST_DECISION["question"], "dedupe")
            self.assertFalse(HOOK.LAST_DECISION.get("dedupe"))

    def test_dry_run_skips_sidecar_and_miss(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        saved = {
            k: os.environ.get(k)
            for k in ("JEV_HOOK_NOSIDECAR", "JEV_HOOK_NOMISS")
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                payload = Path(tmp) / "payload.json"
                payload.write_text(
                    json.dumps(
                        {
                            "hook_event_name": "UserPromptSubmit",
                            "prompt": "paint a mural today",
                            "cwd": tmp,
                        }
                    ),
                    encoding="utf-8",
                )
                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = HOOK.main(["--file", str(payload), "--dry-run"])
                self.assertEqual(rc, 0)
                self.assertFalse((Path(tmp) / ".jev-tools.json").exists())
                self.assertFalse((Path(tmp) / ".jev-tools-miss.json").exists())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_no_sidecar_env_skips_sidecar_write(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_NOSIDECAR": "1"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertIn("jwt-auth", out["hookSpecificOutput"]["additionalContext"])
            self.assertFalse((Path(tmp) / ".jev-tools.json").exists())

    def test_no_miss_env_skips_miss_write(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_NOMISS": "1"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "paint a mural today",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertFalse((Path(tmp) / ".jev-tools-miss.json").exists())
            self.assertTrue((Path(tmp) / ".jev-tools.json").exists())

    def test_hook_off_env_short_circuits(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_OFF": "1"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertEqual(out, {})
            self.assertIsNone(HOOK.LAST_DECISION)

    def test_env_harness_override_used_when_no_arg(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_HARNESS": "claude-code"}):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    pick_fn=skip_pick,
                )
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["harness"], "claude-code")
        self.assertIn("additionalContext", out.get("hookSpecificOutput", {}))

    def test_grok_writes_sidecar_without_additional_context(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="grok",
                pick_fn=skip_pick,
            )
            self.assertEqual(out, {})
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["harness"], "grok")
        self.assertIn("jwt-auth", [row["name"] for row in sidecar["names"]])

    def test_codex_injects_jwt_and_writes_sidecar(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="codex",
                pick_fn=skip_pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("jwt-auth", note)
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["harness"], "codex")
        self.assertIn("jwt-auth", [row["name"] for row in sidecar["names"]])

    def test_generic_tokens_do_not_inject(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        out = HOOK.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "skill plugin mcp tool",
            },
            items=items,
            harness="claude-code",
            pick_fn=skip_pick,
        )
        self.assertEqual(out, {})

    def test_post_tool_use_ignored(self) -> None:
        out = HOOK.handle({"hook_event_name": "PostToolUse", "prompt": "Add JWT access tokens in Python"})
        self.assertEqual(out, {})

    def test_jev_winner_injects_skill_relevance(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picked = INV.shortlist(items, "Add JWT access tokens in Python", INV.HOOK_LIMIT, [])
        jwt = next(item for item in picked if item["name"] == "jwt-auth")

        def pick(_task, _harness, _picked):
            return {"status": "winner", "winner": jwt}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertIn("<skill_relevance>", note)
        self.assertIn("jwt-auth", note)
        self.assertNotIn("ascii-art", note)
        self.assertEqual(sidecar["jev_status"], "winner")
        self.assertEqual(sidecar["jev_pick"]["name"], "jwt-auth")

    def test_jev_none_injects_nothing(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)

        def pick(_task, _harness, _picked):
            return {"status": "none", "winner": None}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(out, {})
        self.assertEqual(sidecar["jev_status"], "none")
        self.assertNotIn("jev_pick", sidecar)

    def test_jev_error_fail_open_idf(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)

        def pick(_task, _harness, _picked):
            return {"status": "error", "winner": None}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jwt-auth", note)
        self.assertIn("jev-consult auto tools", note)

    def test_grok_winner_stays_sidecar_only(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picked = INV.shortlist(items, "Add JWT access tokens in Python", INV.HOOK_LIMIT, [])
        jwt = next(item for item in picked if item["name"] == "jwt-auth")

        def pick(_task, _harness, _picked):
            return {"status": "winner", "winner": jwt}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="grok",
                pick_fn=pick,
            )
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(out, {})
        self.assertEqual(sidecar["jev_pick"]["name"], "jwt-auth")

    def test_cyrillic_prompt_does_not_write_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "добавь авторизацию",
                    "cwd": tmp,
                },
                items=[],
                harness="claude-code",
                pick_fn=skip_pick,
            )
            miss = Path(tmp) / ".jev-tools-miss.json"
            self.assertEqual(out, {})
            self.assertFalse(miss.is_file())

    def test_latin_empty_shortlist_writes_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=[],
                harness="claude-code",
                pick_fn=skip_pick,
            )
            miss = Path(tmp) / ".jev-tools-miss.json"
            self.assertTrue(miss.is_file())
        self.assertIn("from-miss", json.dumps(out))


    def test_explicit_single_mention_wins_without_jev(self) -> None:
        items = [
            {"kind": "skill", "name": "hyperframes", "id": "s_hf"},
            {"kind": "skill", "name": "hyperframes-cli", "id": "s_hfc"},
            {"kind": "skill", "name": "jwt-auth", "id": "s_jwt"},
        ]

        def boom(*_args, **_kwargs):
            self.fail("pick_with_jev must not be called for an explicit mention")

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "use $hyperframes-cli to render this clip",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=boom,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertIn("<skill_relevance>", note)
        self.assertIn("hyperframes-cli", note)
        self.assertEqual(sidecar.get("explicit"), True)
        self.assertEqual(sidecar["jev_pick"]["name"], "hyperframes-cli")

    def test_explicit_two_mentions_fall_back_to_jev(self) -> None:
        items = [
            {"kind": "skill", "name": "hyperframes", "id": "s_hf"},
            {"kind": "skill", "name": "hyperframes-cli", "id": "s_hfc"},
            {"kind": "skill", "name": "jev-consult", "id": "s_jc"},
        ]
        seen: list[list[str]] = []

        def pick(_task, _harness, picked):
            seen.append([item["name"] for item in picked])
            return {"status": "none", "winner": None}

        with tempfile.TemporaryDirectory() as tmp:
            HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "compare hyperframes-cli and jev-consult for this render",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(len(seen), 1)
        self.assertIn("hyperframes-cli", seen[0])
        self.assertIn("jev-consult", seen[0])
        self.assertNotIn("explicit", sidecar)


class DecisionLogTests(unittest.TestCase):
    def test_log_line_written_without_key_value(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        fake = "apikey_FAKEFAKEFAKEFAKEFAKE_FAKEFAKEFAKEFAKEFAKE"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            env = {"JEV_CONSULT_LOG": str(log), "TYPESAFE_API_KEY": fake}
            with patch.dict(os.environ, env):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            text = log.read_text(encoding="utf-8")
        self.assertNotIn(fake, text)
        entry = json.loads(text.strip().splitlines()[-1])
        for key in (
            "ts",
            "harness",
            "prompt_sha",
            "prompt_head",
            "n_catalog",
            "shortlist",
            "explicit",
            "jev_status",
            "need",
            "probabilities",
            "winner",
            "strong_pick",
            "latency_ms",
        ):
            self.assertIn(key, entry)
        self.assertEqual(entry["harness"], "claude-code")
        self.assertEqual(entry["jev_status"], "skip")
        self.assertEqual(entry["prompt_head"], "Add JWT access tokens in Python")

    def test_log_disabled_writes_nothing(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            with patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"}):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertFalse(log.exists())

    def test_log_records_jev_status_and_probabilities(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picker = {
            "status": "winner",
            "winner": {"kind": "skill", "name": "jwt-auth"},
            "need": 0.9,
            "probabilities": {"skill_jwt_auth": 0.9, "none": 0.1},
            "latency_ms": 12,
            "strong": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            with patch.dict(os.environ, {"JEV_CONSULT_LOG": str(log)}):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=lambda *_a, **_k: dict(picker),
                )
            entry = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
            sidecar = json.loads(
                (Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8")
            )
        self.assertEqual(entry["jev_status"], "winner")
        self.assertEqual(entry["winner"], {"kind": "skill", "name": "jwt-auth"})
        self.assertEqual(entry["need"], 0.9)
        self.assertEqual(entry["probabilities"]["skill_jwt_auth"], 0.9)
        self.assertEqual(entry["latency_ms"], 12)
        self.assertTrue(entry["strong_pick"])
        self.assertTrue(sidecar["strong_pick"])


class InventoryScanTests(unittest.TestCase):
    def test_plugin_yaml_is_scanned(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        names = {item["name"] for item in items}
        self.assertIn("demo-plug", names)

    def test_generic_shortlist_empty(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picked = INV.shortlist(items, "skill plugin mcp tool", 8, [])
        self.assertEqual(picked, [])


class InstallToolsHookTests(unittest.TestCase):
    def test_claude_user_prompt_hook_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            INSTALL.upsert_claude_event(settings, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            INSTALL.upsert_claude_event(settings, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            data = json.loads(settings.read_text(encoding="utf-8"))
            entries = data["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(entries), 1)
            self.assertIn("inventory_hook.py", json.dumps(entries))

    def test_codex_hooks_json_idempotent_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            hooks.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {"hooks": [{"type": "command", "command": "echo keep"}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            data = json.loads(hooks.read_text(encoding="utf-8"))
            self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)
            blob = json.dumps(data)
            self.assertIn("inventory_hook.py", blob)
            self.assertIn("commandWindows", blob)
            self.assertIn("echo keep", blob)
            INSTALL.strip_codex_event(hooks, "UserPromptSubmit", "inventory_hook.py", False)
            kept = json.loads(hooks.read_text(encoding="utf-8"))
            self.assertNotIn("UserPromptSubmit", kept["hooks"])
            self.assertIn("PreToolUse", kept["hooks"])

    def test_codex_invalid_json_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            hooks.write_text("{not json", encoding="utf-8")
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            msg = INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            self.assertIn("invalid json", msg)
            self.assertEqual(hooks.read_text(encoding="utf-8"), "{not json")

    def test_codex_strip_removes_file_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            self.assertTrue(hooks.is_file())
            INSTALL.strip_codex_event(hooks, "UserPromptSubmit", "inventory_hook.py", False)
            self.assertFalse(hooks.exists())


_MISSING = object()

PICKED = [
    {"id": "skill:alpha", "kind": "skill", "name": "alpha", "description": "does alpha"},
    {"id": "skill:beta", "kind": "skill", "name": "beta", "description": "does beta"},
]


def fake_jev(answers=None, post_exc=None, key_exc=None, decide_exc=None, decide_ret=None):
    class M:
        @staticmethod
        def load_api_key():
            if key_exc is not None:
                raise key_exc
            return "k"

        @staticmethod
        def load_policy(path=None):
            return {}

        @staticmethod
        def post_systemone(state, questions, policy, **kwargs):
            if post_exc is not None:
                raise post_exc
            return {"answers": answers, "model": "fake-0"}

        @staticmethod
        def decide(ans, policy, irreversible=False):
            if decide_exc is not None:
                raise decide_exc
            if decide_ret is not None:
                return decide_ret
            return {"action": "proceed", "picks": {}, "probabilities": {}}

    return M


class PickWithJevTests(unittest.TestCase):
    """The real pick_with_jev fail-open branches (handle() tests inject pick_fn)."""

    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)
        self._had_jev = sys.modules.get("jev", _MISSING)
        self.addCleanup(self._restore_jev)

    def _restore_jev(self) -> None:
        if self._had_jev is _MISSING:
            sys.modules.pop("jev", None)
        else:
            sys.modules["jev"] = self._had_jev

    def test_empty_shortlist_never_imports_jev(self) -> None:
        sys.modules["jev"] = None  # would raise ImportError if reached
        out = HOOK.pick_with_jev("task", "hermes", [])
        self.assertEqual(out, {"status": "empty", "winner": None})

    def test_jev_import_failure_is_error(self) -> None:
        sys.modules["jev"] = None
        out = HOOK.pick_with_jev("task", "hermes", PICKED)
        self.assertEqual(out, {"status": "error", "winner": None})

    def test_missing_api_key_is_skip(self) -> None:
        sys.modules["jev"] = fake_jev(key_exc=SystemExit(2))
        out = HOOK.pick_with_jev("task", "hermes", PICKED)
        self.assertEqual(out["status"], "skip")

    def test_post_exception_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(post_exc=RuntimeError("down"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_post_systemexit_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(post_exc=SystemExit(3))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_non_dict_answers_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(answers="nope")
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_decide_exception_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(decide_exc=ValueError("bad answers"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_decide_systemexit_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(decide_exc=SystemExit(1))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_escalate_decision_maps_to_escalate(self) -> None:
        sys.modules["jev"] = fake_jev(decide_ret={"action": "escalate", "picks": {}, "probabilities": {}})
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "escalate")

    def test_none_pick_maps_to_none(self) -> None:
        sys.modules["jev"] = fake_jev(
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "none", "need_skill": 0.8},
                "probabilities": {},
            }
        )
        out = HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "none")

    def test_post_timeout_message_maps_to_timeout(self) -> None:
        sys.modules["jev"] = fake_jev(post_exc=SystemExit("Jev network error: timed out"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "timeout")

    def test_post_timeout_error_maps_to_timeout(self) -> None:
        sys.modules["jev"] = fake_jev(post_exc=TimeoutError("timed out"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "timeout")

    def test_post_socket_timeout_maps_to_timeout(self) -> None:
        import socket

        sys.modules["jev"] = fake_jev(post_exc=socket.timeout("timed out"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "timeout")

    def test_post_http_systemexit_stays_error(self) -> None:
        sys.modules["jev"] = fake_jev(post_exc=SystemExit("Jev HTTP 500: nope"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_strong_winner_sets_fields(self) -> None:
        sys.modules["jev"] = fake_jev(
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "skill:beta", "need_skill": 0.9},
                "probabilities": {"load_tools": {"skill:beta": 0.95}},
            }
        )
        out = HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "winner")
        self.assertEqual(out["winner"]["name"], "beta")
        self.assertTrue(out["strong"])
        self.assertEqual(out["need"], 0.9)
        self.assertEqual(out["probabilities"], {"skill:beta": 0.95})
        self.assertIsInstance(out["latency_ms"], int)

    def test_timeout_defaults_to_policy_value(self) -> None:
        seen = {}

        class FakeJev:
            @staticmethod
            def load_api_key():
                return "k"

            @staticmethod
            def load_policy(path=None):
                return {}

            @staticmethod
            def post_systemone(state, questions, policy, **kwargs):
                seen.update(kwargs)
                return {"answers": {}, "model": "fake-0"}

            @staticmethod
            def decide(ans, policy, irreversible=False):
                return {"action": "proceed", "picks": {}, "probabilities": {}}

        sys.modules["jev"] = FakeJev
        HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(seen.get("timeout"), INV.hook_jev_timeout_seconds())

    def test_hook_retries_env_reaches_jev_call(self) -> None:
        seen = {}

        class FakeJev:
            @staticmethod
            def load_api_key():
                return "k"

            @staticmethod
            def load_policy(path=None):
                return {}

            @staticmethod
            def post_systemone(state, questions, policy, **kwargs):
                seen.update(kwargs)
                return {"answers": {}, "model": "fake-0"}

            @staticmethod
            def decide(ans, policy, irreversible=False):
                return {"action": "proceed", "picks": {}, "probabilities": {}}

        sys.modules["jev"] = FakeJev
        with patch.dict(os.environ, {"JEV_HOOK_RETRIES": "2"}):
            HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(seen.get("retries"), 2)

    def test_hook_timeout_env_overrides(self) -> None:
        seen = {}

        class FakeJev:
            @staticmethod
            def load_api_key():
                return "k"

            @staticmethod
            def load_policy(path=None):
                return {}

            @staticmethod
            def post_systemone(state, questions, policy, **kwargs):
                seen.update(kwargs)
                return {"answers": {}, "model": "fake-0"}

            @staticmethod
            def decide(ans, policy, irreversible=False):
                return {"action": "proceed", "picks": {}, "probabilities": {}}

        sys.modules["jev"] = FakeJev
        with patch.dict(os.environ, {"JEV_HOOK_TIMEOUT": "1.5"}):
            HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(seen.get("timeout"), 1.5)
        seen.clear()
        with patch.dict(os.environ, {"JEV_HOOK_TIMEOUT": "bogus"}):
            HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(seen.get("timeout"), INV.DEFAULT_HOOK_JEV_TIMEOUT_SECONDS)

    def test_retries_defaults_to_policy_value(self) -> None:
        seen = {}

        class FakeJev:
            @staticmethod
            def load_api_key():
                return "k"

            @staticmethod
            def load_policy(path=None):
                return {}

            @staticmethod
            def post_systemone(state, questions, policy, **kwargs):
                seen.update(kwargs)
                return {"answers": {}, "model": "fake-0"}

            @staticmethod
            def decide(ans, policy, irreversible=False):
                return {"action": "proceed", "picks": {}, "probabilities": {}}

        sys.modules["jev"] = FakeJev
        HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(seen.get("retries"), INV.hook_jev_retries())

    def test_non_numeric_need_still_attached_as_none(self) -> None:
        sys.modules["jev"] = fake_jev(
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "skill:beta", "need_skill": "high"},
                "probabilities": {"load_tools": {"skill:beta": 0.5}},
            }
        )
        out = HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertIsNone(out["need"])
        self.assertIn(out["status"], {"winner", "idf", "escalate"})


class MainLoopTests(unittest.TestCase):
    """stdin/stdout edge cases for the hook entrypoint."""

    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def run_main(self, stdin_text: str) -> str:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin_text)), redirect_stdout(buf):
            rc = HOOK.main()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_empty_stdin_prints_empty_object(self) -> None:
        self.assertEqual(self.run_main(""), "{}\n")

    def test_bad_json_prints_empty_object(self) -> None:
        self.assertEqual(self.run_main("{not json"), "{}\n")

    def test_non_dict_payload_prints_empty_object(self) -> None:
        self.assertEqual(self.run_main("[1, 2]"), "{}\n")

    def test_handle_exception_still_prints_object(self) -> None:
        with patch.object(HOOK, "handle", side_effect=RuntimeError("boom")):
            out = self.run_main('{"prompt": "x"}')
        self.assertEqual(json.loads(out), {})

    def test_debug_file_writes_last_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "hook-debug.log"
            decision = {
                "jev_status": "idf",
                "winner": {"name": "jwt-auth"},
                "dedupe": False,
                "shortlist": ["a", "b"],
                "latency_ms": 3,
            }
            with patch.object(HOOK, "LAST_DECISION", decision), patch.object(
                HOOK, "handle", lambda _p: {}
            ), patch.dict(os.environ, {"JEV_HOOK_DEBUG_FILE": str(log_path)}):
                self.run_main('{"prompt": "x", "cwd": "%s"}' % tmp.replace("\\", "\\\\"))
            text = log_path.read_text(encoding="utf-8")
            self.assertIn("jev_status=idf", text)
            self.assertIn("winner=jwt-auth", text)

    def run_main_verbose(self, stdin_text: str, extra_argv=None) -> tuple:
        import io
        from contextlib import redirect_stderr, redirect_stdout

        out_buf, err_buf = io.StringIO(), io.StringIO()
        argv = ["inventory_hook.py", "--verbose"] + list(extra_argv or [])
        with patch.object(sys, "argv", argv), patch(
            "sys.stdin", io.StringIO(stdin_text)
        ), redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = HOOK.main()
        self.assertEqual(rc, 0)
        return out_buf.getvalue(), err_buf.getvalue()

    def test_verbose_explains_hook_off(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_OFF": "1"}):
            out, err = self.run_main_verbose('{"prompt": "x", "event": "UserPromptSubmit"}')
        self.assertEqual(out.strip(), "{}")
        self.assertIn("JEV_HOOK_OFF", err)

    def test_verbose_explains_disallowed_event(self) -> None:
        out, err = self.run_main_verbose('{"prompt": "x", "event": "PostToolUse"}')
        self.assertEqual(out.strip(), "{}")
        self.assertIn("not in allowed set", err)

    def test_verbose_explains_missing_prompt(self) -> None:
        out, err = self.run_main_verbose('{"event": "UserPromptSubmit"}')
        self.assertEqual(out.strip(), "{}")
        self.assertIn("no prompt", err)

    def test_verbose_reports_jev_status_when_no_context(self) -> None:
        decision = {"jev_status": "none"}
        with patch.object(HOOK, "handle", lambda _p: {}), patch.object(
            HOOK, "LAST_DECISION", decision
        ):
            out, err = self.run_main_verbose('{"prompt": "x"}')
        self.assertEqual(out.strip(), "{}")
        self.assertIn("jev_status=none", err)

    def test_verbose_silent_when_context_emitted(self) -> None:
        with patch.object(HOOK, "handle", lambda _p: {"context": "note"}):
            out, err = self.run_main_verbose('{"prompt": "x"}')
        self.assertIn("context", out)
        self.assertEqual(err, "")

    def test_debug_file_bad_path_fails_open(self) -> None:
        decision = {"jev_status": "idf"}
        with patch.object(HOOK, "LAST_DECISION", decision), patch.object(
            HOOK, "handle", lambda _p: {}
        ), patch.dict(
            os.environ, {"JEV_HOOK_DEBUG_FILE": "N:\\no\\such\\dir\\x.log"}
        ):
            self.assertEqual(self.run_main('{"prompt": "x"}').strip(), "{}")


class HandleBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)
        self.items = INV.scan("hermes", hermes=FIXTURE)

    def test_extract_prompt_variants(self) -> None:
        self.assertEqual(HOOK.extract_prompt({"prompt": " p "}), "p")
        self.assertEqual(HOOK.extract_prompt({"user_message": "u"}), "u")
        blocks = [{"type": "text", "text": "block"}, {"type": "image"}]
        self.assertEqual(HOOK.extract_prompt({"prompt": blocks}), "block")
        self.assertEqual(
            HOOK.extract_prompt(
                {"messages": [{"role": "user", "content": [{"type": "text", "text": "deep"}, {"type": "text", "text": "dive"}]}]}
            ),
            "deep dive",
        )
        self.assertEqual(HOOK.extract_prompt({"userMessage": "m"}), "m")
        with patch.dict(os.environ, {"JEV_HOOK_PROMPT": " env prompt "}):
            self.assertEqual(HOOK.extract_prompt({"prompt": "p"}), "p")
            self.assertEqual(HOOK.extract_prompt({}), "env prompt")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_HOOK_CWD": tmp}):
                self.assertEqual(HOOK.extract_cwd({}), Path(tmp))
            with patch.dict(os.environ, {"JEV_HOOK_CWD": tmp}):
                self.assertEqual(HOOK.extract_cwd({"cwd": "/no/such/dir"}), Path(tmp))
            with patch.dict(os.environ, {"JEV_HOOK_CWD": "/no/such/dir"}):
                self.assertIsNone(HOOK.extract_cwd({}))
        with patch.dict(os.environ, {"JEV_HOOK_EVENT": "post_llm"}):
            self.assertEqual(HOOK.event_name({"event": "UserPromptSubmit"}), "post_llm")
        self.assertEqual(
            HOOK.extract_prompt(
                {
                    "conversation_history": [
                        {"role": "assistant", "content": "old"},
                        {"role": "user", "content": " latest "},
                    ]
                }
            ),
            "latest",
        )
        self.assertEqual(
            HOOK.extract_prompt({"messages": [{"role": "user", "text": "t1"}]}), "t1"
        )
        self.assertEqual(
            HOOK.extract_prompt({"messages": [{"role": "user", "message": "mm"}]}), "mm"
        )
        self.assertEqual(HOOK.extract_prompt({}), "")
        self.assertEqual(HOOK.extract_prompt({"prompt": "  "}), "")
        self.assertEqual(
            HOOK.extract_prompt({"conversation_history": [{"role": "assistant", "content": "a"}]}),
            "",
        )

    def test_extract_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(HOOK.extract_cwd({"cwd": tmp}), Path(tmp))
            self.assertEqual(HOOK.extract_cwd({"cwd_path": tmp}), Path(tmp))
            self.assertEqual(HOOK.extract_cwd({"workspace": tmp}), Path(tmp))
        self.assertIsNone(HOOK.extract_cwd({"cwd": "/no/such/dir-xyz"}))
        self.assertIsNone(HOOK.extract_cwd({}))

    def test_redact_prompt_wiring(self) -> None:
        class FakeJev:
            @staticmethod
            def redact(text):
                return text.replace("secret", "[R]")

        with patch.dict(sys.modules, {"jev": FakeJev}):
            self.assertEqual(HOOK._redact_prompt("a secret"), "a [R]")
        with patch.dict(sys.modules, {"jev": None}):
            self.assertEqual(HOOK._redact_prompt("a secret"), "a secret")

    def test_unknown_event_and_empty_prompt(self) -> None:
        self.assertEqual(
            HOOK.handle(
                {"hook_event_name": "PostToolUse", "prompt": "jwt"},
                items=self.items,
                harness="claude-code",
            ),
            {},
        )
        self.assertEqual(
            HOOK.handle({"prompt": "  "}, items=self.items, harness="claude-code"), {}
        )

    def test_explicit_single_mention_wins(self) -> None:
        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "run $jwt-auth now"},
            items=self.items,
            harness="claude-code",
            pick_fn=lambda *a, **k: self.fail("picker must not run on explicit hit"),
        )
        note = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jwt-auth", note)
        self.assertIn("skill_relevance", note)  # winner note, not the list note

    def test_pick_fn_exception_falls_back_to_note(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("down")

        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=boom,
        )
        note = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jwt-auth", note)

    def test_pick_fn_non_dict_keeps_idf(self) -> None:
        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=lambda *_a, **_k: "garbage",
        )
        self.assertIn("jwt-auth", out["hookSpecificOutput"]["additionalContext"])

    def test_miss_written_when_no_picks_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "zebra quokka xylophone",
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertTrue((Path(tmp) / INV.MISS_NAME).is_file())
            miss = json.loads((Path(tmp) / INV.MISS_NAME).read_text(encoding="utf-8"))
            self.assertEqual(miss["harness"], "claude-code")
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("peer_fill.py", note)

    def test_no_cwd_miss_note_still_returned(self) -> None:
        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "zebra quokka xylophone"},
            items=self.items,
            harness="claude-code",
            pick_fn=skip_pick,
        )
        self.assertIn("peer_fill.py", out["hookSpecificOutput"]["additionalContext"])

    def test_no_tokens_no_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "a a a",  # tokens() drops <3 chars
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertEqual(out, {})
            self.assertFalse((Path(tmp) / INV.MISS_NAME).exists())

    def test_picked_clears_existing_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            miss = Path(tmp) / INV.MISS_NAME
            miss.write_text('{"stale": true}', encoding="utf-8")
            HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT tokens please",
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertFalse(miss.exists())

    def test_hermes_returns_context(self) -> None:
        out = HOOK.handle(
            {"prompt": "Add JWT tokens please"},
            items=self.items,
            harness="hermes",
            pick_fn=skip_pick,
        )
        self.assertIn("context", out)
        self.assertIn("jwt-auth", out["context"])

    def test_pre_llm_call_returns_context(self) -> None:
        out = HOOK.handle(
            {"event": "pre_llm_call", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=skip_pick,
        )
        self.assertIn("context", out)

    def test_grok_emits_nothing(self) -> None:
        out = HOOK.handle(
            {"prompt": "Add JWT tokens please"},
            items=self.items,
            harness="grok",
            pick_fn=skip_pick,
        )
        self.assertEqual(out, {})

    def test_winner_pick_writes_sidecar_fields(self) -> None:
        def win(*_a, **_k):
            return {
                "status": "winner",
                "winner": {"kind": "skill", "name": "jwt-auth"},
                "strong": True,
                "need": 0.9,
                "probabilities": {"p_jwt_auth": 0.8},
                "latency_ms": 12,
            }

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT tokens please",
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=win,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("skill_relevance", note)
            sidecar = json.loads((Path(tmp) / INV.SIDECAR_NAME).read_text(encoding="utf-8"))
        self.assertTrue(sidecar["strong_pick"])
        self.assertEqual(sidecar["jev_pick"], {"kind": "skill", "name": "jwt-auth"})
        self.assertEqual(sidecar["jev_status"], "winner")

    def test_none_pick_returns_empty(self) -> None:
        def none_pick(*_a, **_k):
            return {"status": "none", "winner": None}

        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=none_pick,
        )
        self.assertEqual(out, {})


class BudgetGuardTests(unittest.TestCase):
    def test_budget_exceeded_skips_jev(self) -> None:
        calls = []

        def spy(prompt, harness, picked):
            calls.append(1)
            return {"status": "winner", "winner": picked[0]}

        payload = {"event": "UserPromptSubmit", "prompt": "jwt auth please"}
        with patch.object(HOOK, "hook_budget_seconds", return_value=0.0):
            out = HOOK.handle(
                payload,
                items=[{"kind": "skill", "name": "jwt-auth", "id": "skill_jwt_auth", "description": "jwt"}],
                harness="claude-code",
                pick_fn=spy,
            )
        self.assertEqual(calls, [])  # Jev never called
        # falls open to the IDF shortlist note
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jwt-auth", ctx)

    def test_budget_ok_calls_jev(self) -> None:
        calls = []

        def spy(prompt, harness, picked):
            calls.append(1)
            return {"status": "idf", "winner": None}

        payload = {"event": "UserPromptSubmit", "prompt": "jwt auth please"}
        with patch.object(HOOK, "hook_budget_seconds", return_value=999.0):
            HOOK.handle(
                payload,
                items=[{"kind": "skill", "name": "jwt-auth", "id": "skill_jwt_auth", "description": "jwt"}],
                harness="claude-code",
                pick_fn=spy,
            )
        self.assertEqual(calls, [1])

    def test_hook_budget_seconds_policy(self) -> None:
        self.assertEqual(INV.hook_budget_seconds(), 12.0)
        with patch.object(INV.json, "loads", side_effect=ValueError):
            self.assertEqual(INV.hook_budget_seconds(), INV.DEFAULT_HOOK_BUDGET_SECONDS)
        with patch.object(INV.json, "loads", return_value={"hook_budget_seconds": -5}):
            self.assertEqual(INV.hook_budget_seconds(), 0.0)

    def test_hook_jev_timeout_seconds_policy(self) -> None:
        self.assertEqual(INV.hook_jev_timeout_seconds(), 8.0)
        with patch.object(INV.json, "loads", side_effect=ValueError):
            self.assertEqual(INV.hook_jev_timeout_seconds(), INV.DEFAULT_HOOK_JEV_TIMEOUT_SECONDS)
        with patch.object(INV.json, "loads", return_value={"hook_jev_timeout_seconds": 2.5}):
            self.assertEqual(INV.hook_jev_timeout_seconds(), 2.5)

    def test_hook_jev_retries_policy(self) -> None:
        self.assertEqual(INV.hook_jev_retries(), 0)
        with patch.object(INV.json, "loads", side_effect=ValueError):
            self.assertEqual(INV.hook_jev_retries(), INV.DEFAULT_HOOK_JEV_RETRIES)
        with patch.object(INV.json, "loads", return_value={"hook_jev_retries": 3}):
            self.assertEqual(INV.hook_jev_retries(), 3)
        with patch.object(INV.json, "loads", return_value={"hook_jev_retries": -2}):
            self.assertEqual(INV.hook_jev_retries(), 0)


class InventoryInternalsTests(unittest.TestCase):
    def test_uniquify(self) -> None:
        items = [{"id": "a", "name": "x"}, {"id": "a", "name": "y"}, {"id": "b", "name": "z"}]
        out = INV.uniquify(items)
        ids = [i["id"] for i in out]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids[0], "a")
        self.assertTrue(ids[1].startswith("a_"))
        self.assertLessEqual(max(len(i) for i in ids), 48)

    def test_name_df_and_token_weight(self) -> None:
        items = [{"name": "jwt-auth"}, {"name": "jwt-verify"}, {"name": "other"}]
        df = INV.name_df(items, {"jwt", "zzz"})
        self.assertEqual(df, {"jwt": 2, "zzz": 0})
        self.assertEqual(INV.token_weight("jwt", df), 5)
        self.assertEqual(INV.token_weight("zzz", df), 0)
        self.assertEqual(INV.token_weight("hot", {"hot": 9}), 1)

    def test_score_item(self) -> None:
        item = {"name": "jwt-auth", "description": "token signing"}
        self.assertEqual(INV.score_item(item, set()), 0)
        self.assertEqual(INV.score_item(item, {"jwt"}), 3)  # name match, no df
        self.assertGreater(
            INV.score_item(item, {"jwt"}), INV.score_item({"name": "x", "description": "jwt"}, {"jwt"})
        )
        df = {"jwt": 50}
        self.assertEqual(INV.score_item(item, {"jwt"}, df), 1)  # common token cheapened

    def test_fm_scalar(self) -> None:
        self.assertEqual(INV._fm_scalar('"quoted"'), "quoted")
        self.assertEqual(INV._fm_scalar("'it''s'"), "it's")
        self.assertEqual(INV._fm_scalar("plain"), "plain")
        self.assertEqual(INV._fm_scalar('"bad'), "bad")

    def test_mcp_names_from_yaml(self) -> None:
        text = (
            "other: 1\n"
            "mcp_servers:\n"
            "  github: # comment\n"
            "    url: x\n"
            "  slack:\n"
            "next_key: 2\n"
        )
        self.assertEqual(INV.mcp_names_from_yaml(text), ["github", "slack"])
        self.assertEqual(INV.mcp_names_from_yaml("nothing: 1\n"), [])

    def test_mcp_names_from_json(self) -> None:
        self.assertEqual(INV.mcp_names_from_json("bad"), [])
        self.assertEqual(INV.mcp_names_from_json("[1]"), [])
        self.assertEqual(INV.mcp_names_from_json('{"mcpServers": {"a": {}}}'), ["a"])
        self.assertEqual(INV.mcp_names_from_json('{"mcp_servers": {"b": {}}}'), ["b"])
        self.assertEqual(INV.mcp_names_from_json('{"mcpServers": [1]}'), [])

    def test_mcp_names_from_toml(self) -> None:
        text = "[mcp_servers.github]\nurl = 1\n[mcp_servers.slack]\nx = 2\n[mcp_servers.github]\n"
        self.assertEqual(INV.mcp_names_from_toml(text), ["github", "slack"])

    def test_iter_mcp_suffixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "a.yaml").write_text("mcp_servers:\n  gh:\n", encoding="utf-8")
            (d / "b.json").write_text('{"mcpServers": {"j": {}}}', encoding="utf-8")
            (d / "c.toml").write_text("[mcp_servers.tt]\n", encoding="utf-8")
            items = INV.iter_mcp([d / "a.yaml", d / "b.json", d / "c.toml", d / "missing.json"])
        self.assertEqual({i["name"] for i in items}, {"gh", "j", "tt"})

    def test_iter_claude_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(INV.iter_claude_plugins([root]), [])
            manifest = root / "installed_plugins.json"
            manifest.write_text("bad", encoding="utf-8")
            self.assertEqual(INV.iter_claude_plugins([root]), [])
            manifest.write_text('{"plugins": {"nice@1.0": {}, "other@2": {}}}', encoding="utf-8")
            items = INV.iter_claude_plugins([root])
            self.assertEqual({i["name"] for i in items}, {"nice", "other"})

    def test_iter_plugin_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plug = root / "myp"
            plug.mkdir()
            (plug / "plugin.yaml").write_text("name: real-name\n", encoding="utf-8")
            noname = root / "direc"
            noname.mkdir()
            (noname / "plugin.yml").write_text("version: 1\n", encoding="utf-8")
            nodir = root / "nothing"
            nodir.mkdir()  # no manifest
            items = INV.iter_plugin_yaml([root, root])  # dup dir dedupes
            names = {i["name"] for i in items}
            self.assertEqual(names, {"real-name", "direc"})

    def test_walk_named_skips_cruft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok" / "x").mkdir(parents=True)
            (root / "ok" / "x" / "SKILL.md").write_text("---\nname: ok-skill\n---\n", encoding="utf-8")
            (root / "node_modules" / "junk").mkdir(parents=True)
            (root / "node_modules" / "junk" / "SKILL.md").write_text("---\nname: bad\n---\n", encoding="utf-8")
            items = INV.iter_skills([root])
            self.assertEqual([i["name"] for i in items], ["ok-skill"])

    def test_scan_cached(self) -> None:
        INV.clear_scan_cache()
        calls = []

        def fake_scan(harness, home=None, hermes=None):
            calls.append(harness)
            return [{"id": "x", "name": "x"}]

        with patch.object(INV, "scan", side_effect=fake_scan):
            first = INV.scan_cached("hermes")
            second = INV.scan_cached("hermes")
        self.assertIs(first, second)  # cached object
        self.assertEqual(len(calls), 1)
        INV.clear_scan_cache()

    def test_decisions_log_path(self) -> None:
        with patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"}):
            self.assertIsNone(INV.decisions_log_path())
        with patch.dict(os.environ, {"JEV_CONSULT_LOG": "C:/x/log.jsonl"}):
            self.assertEqual(INV.decisions_log_path(), Path("C:/x/log.jsonl"))

    def test_append_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "sub" / "decisions.jsonl"
            INV.append_decision({"a": 1}, log)
            INV.append_decision({"b": 2}, log)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1]), {"b": 2})
        with patch.object(INV, "decisions_log_path", return_value=None):
            INV.append_decision({"x": 1})  # no target: silent no-op

    def test_detect_harness(self) -> None:
        self.assertEqual(INV.detect_harness(Path("C:/u/.claude/skills/x/h.py")), "claude-code")
        self.assertEqual(INV.detect_harness(Path("C:/u/.grok/skills/x/h.py")), "grok")
        self.assertEqual(INV.detect_harness(Path("C:/u/.codex/skills/x/h.py")), "codex")
        self.assertEqual(INV.detect_harness(Path("C:/u/.agents/skills/x/h.py")), "codex")
        self.assertEqual(INV.detect_harness(Path("D:/Hermes/home/skills/x/h.py")), "hermes")
        self.assertEqual(INV.detect_harness(Path("C:/other/x.py")), "hermes")  # fallback

    def test_format_notes(self) -> None:
        self.assertEqual(INV.format_note([]), "")
        note = INV.format_note([{"kind": "skill", "name": "jwt-auth"}])
        self.assertIn("jwt-auth", note)
        self.assertIn("Never auto-install", note)
        miss = INV.format_miss_note(Path("C:/x/peer_fill.py"))
        self.assertIn("peer_fill.py", miss)
        self.assertIn("catalog_fill.py", miss)
        self.assertIn("apply_fill.py", miss)
        self.assertIn("Never --force", miss)

    def test_sidecar_items_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            picked = [
                {
                    "kind": "skill",
                    "name": "jwt-auth",
                    "id": "skill_jwt_auth",
                    "description": "sign tokens",
                    "path": "/p",
                },
                {"kind": "mcp", "name": "bare", "id": "mcp_bare"},  # no desc/path
            ]
            INV.write_sidecar(path, "codex", "task", picked, {"jev_status": "winner"})
            payload = INV.read_sidecar(path)
            rows = INV.sidecar_items(payload)
            self.assertEqual(rows[0]["id"], "skill_jwt_auth")
            self.assertEqual(rows[0]["description"], "sign tokens")
            self.assertEqual(rows[1], {"id": "mcp_bare", "kind": "mcp", "name": "bare"})
            self.assertEqual(INV.read_sidecar_items(path), rows)
            # names[] preserved for back-compat
            self.assertEqual(payload["names"], [{"kind": "skill", "name": "jwt-auth"}, {"kind": "mcp", "name": "bare"}])

    def test_sidecar_items_legacy_and_bad(self) -> None:
        legacy = {"names": [{"kind": "skill", "name": "a"}, {"kind": "mcp", "name": "b"}, "junk"]}
        self.assertEqual(
            INV.sidecar_items(legacy),
            [{"kind": "skill", "name": "a"}, {"kind": "mcp", "name": "b"}],
        )
        self.assertEqual(INV.sidecar_items({}), [])
        self.assertEqual(INV.sidecar_items({"items": ["x", {"name": "y"}]}), [{"name": "y"}])
        self.assertEqual(INV.sidecar_items("nope"), [])

    def test_policy_float(self) -> None:
        self.assertEqual(INV._policy_float({"x": 0.9}, "x", 0.5), 0.9)
        self.assertEqual(INV._policy_float({"x": "bad"}, "x", 0.5), 0.5)
        self.assertEqual(INV._policy_float(None, "x", 0.5), 0.5)
        self.assertEqual(INV._policy_float({}, "x", 0.5), 0.5)

    def test_stop_words_and_catalogs_from_policy(self) -> None:
        # real policy.json carries both keys
        self.assertIn("skill", INV.stop_words())
        self.assertEqual(len(INV.catalogs()), 4)
        # policy override
        with patch.object(INV, "_policy_dict", return_value={"stop_words": ["zztop"]}):
            self.assertEqual(INV.stop_words(), {"zztop"})
            self.assertEqual(INV.tokens("zztop keepme"), {"keepme"})
        with patch.object(
            INV,
            "_policy_dict",
            return_value={"catalogs": [{"name": "x", "url": "https://x"}, {"name": "y"}]},
        ):
            self.assertEqual(INV.catalogs(), (("x", "https://x"),))
        # missing/malformed keys fall back to module defaults
        with patch.object(INV, "_policy_dict", return_value={}):
            self.assertEqual(INV.stop_words(), INV.STOP)
            self.assertEqual(INV.catalogs(), INV.CATALOGS)
        with patch.object(INV, "_policy_dict", return_value={"stop_words": "nope", "catalogs": "nope"}):
            self.assertEqual(INV.stop_words(), INV.STOP)
            self.assertEqual(INV.catalogs(), INV.CATALOGS)


class HookE2ETests(unittest.TestCase):
    """main() over real stdin/stdout in a subprocess (no Jev key => fail-open)."""

    HOOK_PATH = ROOT / "skills" / "jev-consult" / "scripts" / "inventory_hook.py"

    def _run(
        self,
        stdin_text: str,
        cwd: str | None = None,
        home: str | None = None,
        env_extra: dict | None = None,
    ):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        if env_extra:
            env.update(env_extra)
        if home:
            env["USERPROFILE"] = home
            env["HOME"] = home
        proc = subprocess.run(
            [sys.executable, str(self.HOOK_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_jq_prints_one_field_of_emitted_payload(self) -> None:
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "p.json"
            f.write_text(
                json.dumps(
                    {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT", "cwd": tmp}
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(self.HOOK_PATH), "--file", str(f), "--jq", "context"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("jev-consult:", json.loads(proc.stdout))
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "p.json"
            f.write_text(
                json.dumps(
                    {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT", "cwd": tmp}
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(self.HOOK_PATH), "--file", str(f), "--jq", "nope.x"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("bad --jq key", proc.stderr)

    def test_empty_stdin(self) -> None:
        self.assertEqual(json.loads(self._run("")), {})

    def test_bad_json(self) -> None:
        self.assertEqual(json.loads(self._run("not json")), {})
        self.assertEqual(json.loads(self._run("[1,2]")), {})

    def test_valid_prompt_fail_open(self) -> None:
        # no key in env -> pick_with_jev fails open; still emits context note
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            skill = home / ".hermes" / "skills" / "jwt-stuff"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: jwt-stuff\ndescription: jwt\n---\n", encoding="utf-8"
            )
            cwd = Path(tmp) / "work"
            cwd.mkdir()
            out = json.loads(
                self._run(
                    json.dumps(
                        {
                            "event": "UserPromptSubmit",
                            "prompt": "jwt stuff please",
                            "cwd": str(cwd),
                        }
                    ),
                    home=str(home),
                )
            )
        # hermes path detect via __file__ -> default hermes; emits {"context": ...}
        if "hookSpecificOutput" in out:
            ctx = out["hookSpecificOutput"]["additionalContext"]
        else:
            ctx = out.get("context", "")
        self.assertIn("jwt-stuff", ctx)

    def test_unknown_event(self) -> None:
        out = json.loads(self._run(json.dumps({"event": "PostToolUse", "prompt": "x"})))
        self.assertEqual(out, {})

    def test_max_age_skips_stale_payload_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            stale = json.dumps(
                {
                    "event": "UserPromptSubmit",
                    "prompt": "jwt stuff",
                    "cwd": str(cwd),
                    "timestamp": 1700000000,
                }
            )
            out = json.loads(
                self._run(stale, env_extra={"JEV_HOOK_MAX_AGE": "30"})
            )
            self.assertEqual(out, {})
            fresh = json.dumps(
                {
                    "event": "UserPromptSubmit",
                    "prompt": "jwt stuff",
                    "cwd": str(cwd),
                    "timestamp": int(time.time()),
                }
            )
            out = json.loads(
                self._run(fresh, env_extra={"JEV_HOOK_MAX_AGE": "30"})
            )
            self.assertNotEqual(out, {})

    def test_json_flag_echoes_last_decision_to_stderr(self) -> None:
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            skill = home / ".hermes" / "skills" / "jwt-stuff"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: jwt-stuff\ndescription: jwt\n---\n", encoding="utf-8"
            )
            cwd = Path(tmp) / "work"
            cwd.mkdir()
            env["USERPROFILE"] = str(home)
            env["HOME"] = str(home)
            proc = subprocess.run(
                [sys.executable, str(self.HOOK_PATH), "--json"],
                input=json.dumps(
                    {
                        "event": "UserPromptSubmit",
                        "prompt": "jwt stuff please",
                        "cwd": str(cwd),
                    }
                ),
                capture_output=True,
                text=True,
                cwd=str(cwd),
                env=env,
                timeout=60,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        decision = json.loads(proc.stderr.strip().splitlines()[-1])
        self.assertIn("jev_status", decision)
        self.assertIn("prompt_sha", decision)
        self.assertIn("jwt", json.dumps(proc.stdout))

    def _run_script(self, script: Path, stdin_text: str, home: str, cwd: str):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        env["USERPROFILE"] = home
        env["HOME"] = home
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=stdin_text,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_grok_e2e_sidecar_only_no_stdout_context(self) -> None:
        # Hook copy under a .grok path detects harness=grok -> emits {}
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            home = tmp_p / "home"
            grok_hook = home / ".grok" / "skills" / "jev" / "hook.py"
            grok_hook.parent.mkdir(parents=True)
            shutil.copyfile(self.HOOK_PATH, grok_hook)
            shutil.copyfile(
                self.HOOK_PATH.parent / "inventory.py",
                grok_hook.parent / "inventory.py",
            )
            shutil.copyfile(
                self.HOOK_PATH.parent / "_watch.py",
                grok_hook.parent / "_watch.py",
            )
            skill = home / ".grok" / "skills" / "jwt-stuff"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: jwt-stuff\ndescription: jwt\n---\n", encoding="utf-8"
            )
            cwd = tmp_p / "work"
            cwd.mkdir()
            out = self._run_script(
                grok_hook,
                json.dumps(
                    {
                        "event": "UserPromptSubmit",
                        "prompt": "jwt stuff please",
                        "cwd": str(cwd),
                    }
                ),
                str(home),
                str(cwd),
            )
            self.assertEqual(json.loads(out), {})
            sidecar = cwd / INV.SIDECAR_NAME
            self.assertTrue(sidecar.is_file())
            data = INV.read_sidecar(sidecar)
            self.assertEqual(data["harness"], "grok")


class ScanMergeTests(unittest.TestCase):
    def _tree(self, tmp: str):
        home = Path(tmp) / "home"
        hermes = Path(tmp) / "hermes"
        # hermes skill
        s = hermes / "skills" / "token-writer"
        s.mkdir(parents=True)
        (s / "SKILL.md").write_text("---\nname: token-writer\ndescription: h\n---\n", encoding="utf-8")
        # claude plugin manifest + yaml plugin
        cplugins = home / ".claude" / "plugins"
        cplugins.mkdir(parents=True)
        (cplugins / "installed_plugins.json").write_text(
            '{"plugins": {"cplug@1": {}}}', encoding="utf-8"
        )
        yplug = cplugins / "yamlplug"
        yplug.mkdir()
        (yplug / "plugin.yaml").write_text("name: yamlplug\n", encoding="utf-8")
        # claude mcp json
        (home / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
        (home / ".claude.json").write_text('{"mcpServers": {"ghmcp": {}}}', encoding="utf-8")
        return home, hermes

    def test_scan_hermes_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, hermes = self._tree(tmp)
            items = INV.scan("hermes", home=home, hermes=hermes)
        self.assertEqual({i["kind"] for i in items}, {"skill"})
        self.assertEqual(items[0]["name"], "token-writer")

    def test_scan_claude_tree_merges_all_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, hermes = self._tree(tmp)
            items = INV.scan("claude-code", home=home, hermes=hermes)
        kinds = {i["kind"] for i in items}
        self.assertEqual(kinds, {"plugin", "mcp"})
        self.assertIn("cplug", {i["name"] for i in items})
        self.assertIn("yamlplug", {i["name"] for i in items})
        self.assertIn("ghmcp", {i["name"] for i in items})

    def test_scan_codex_two_skill_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            for part in (".codex", ".agents"):
                d = home / part / "skills" / ("s-%s" % part[1:])
                d.mkdir(parents=True)
                (d / "SKILL.md").write_text(
                    "---\nname: s-%s\n---\n" % part[1:], encoding="utf-8"
                )
            items = INV.scan("codex", home=home, hermes=Path(tmp) / "hermes")
        self.assertEqual({i["name"] for i in items}, {"s-codex", "s-agents"})

    def test_scan_dedupes_same_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp) / "hermes"
            for sub in ("a", "b"):
                d = hermes / "skills" / sub
                d.mkdir(parents=True)
                (d / "SKILL.md").write_text("---\nname: same-name\n---\n", encoding="utf-8")
            items = INV.scan("hermes", home=Path(tmp) / "home", hermes=hermes)
        ids = [i["id"] for i in items]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)  # uniquify split the collision

    def test_explicit_only_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "s"
            (skill / "agents").mkdir(parents=True)
            md = skill / "SKILL.md"
            md.write_text("---\nname: s\n---\n", encoding="utf-8")
            self.assertFalse(INV.explicit_only(md))
            (skill / "agents" / "openai.yaml").write_text(
                "allow_implicit_invocation: false\n", encoding="utf-8"
            )
            self.assertTrue(INV.explicit_only(md))
            (skill / "agents" / "openai.yaml").write_text(
                "allow_implicit_invocation: true\n", encoding="utf-8"
            )
            self.assertFalse(INV.explicit_only(md))
            (skill / "agents" / "openai.yaml").write_bytes(b"x" * 20001)
            self.assertFalse(INV.explicit_only(md))

    def test_parse_frontmatter_multiline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "sname"
            d.mkdir()
            (d / "SKILL.md").write_text(
                "---\nname: quoted-skill\ndescription: >\n  first line\n  second line\n---\n",
                encoding="utf-8",
            )
            meta = INV.parse_frontmatter(d / "SKILL.md")
            self.assertEqual(meta["name"], "quoted-skill")
            self.assertIn("first line", meta["description"])
            plain = d / "NOFM.md"
            plain.write_text("no frontmatter\n", encoding="utf-8")
            meta2 = INV.parse_frontmatter(plain)
            self.assertEqual(meta2["name"], "sname")  # falls back to dir name

    def test_picker_request_and_write_ask(self) -> None:
        picked = [{"kind": "skill", "name": "jwt-auth", "id": "skill_jwt_auth", "description": "d"}]
        req = INV.picker_request("task t", "claude-code", picked)
        self.assertEqual(set(req["questions"]), {"load_tools", "need_skill"})
        self.assertIn("none", req["questions"]["load_tools"]["criteria"])
        self.assertIn("skill_jwt_auth", req["questions"]["load_tools"]["criteria"])
        self.assertIn("untrusted", req["questions"]["load_tools"]["instructions"])
        self.assertEqual(req["state"]["harness"], "claude-code")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ask.json"
            INV.write_ask(out, "t", "hermes", picked)
            payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertIn("installed_enough", payload["questions"])

    def test_main_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, hermes = self._tree(tmp)
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = INV.main(
                    [
                        "--harness", "hermes", "--hermes-home", str(hermes),
                        "--home", str(home), "--task", "token writer",
                    ]
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["counts"]["skill"], 1)
            self.assertEqual(out["shortlist"][0]["id"], "skill_token_writer")

            ask = Path(tmp) / "ask.json"
            with patch.object(sys, "stdout", io.StringIO()):
                INV.main(
                    [
                        "--harness", "hermes", "--hermes-home", str(hermes), "--home", str(home),
                        "--task", "jwt", "--include", "token-writer", "--write-ask", str(ask),
                    ]
                )
            payload = json.loads(ask.read_text(encoding="utf-8"))
            self.assertIn("skill_token_writer", payload["questions"]["load_tools"]["criteria"])

    def test_main_catalogs_and_all_names(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            INV.main(["--catalogs"])
        self.assertIn("\t", buf.getvalue())  # name<TAB>url rows
        with tempfile.TemporaryDirectory() as tmp:
            home, hermes = self._tree(tmp)
            buf2 = io.StringIO()
            with patch.object(sys, "stdout", buf2):
                INV.main(
                    [
                        "--harness", "hermes", "--hermes-home", str(hermes),
                        "--home", str(home), "--all-names",
                    ]
                )
            self.assertIn("token-writer", buf2.getvalue())


class PruneSidecarsTests(unittest.TestCase):
    def _sidecar(self, path, written_at) -> None:
        path.write_text(
            json.dumps({"harness": "codex", "task": "t", "written_at": int(written_at)}),
            encoding="utf-8",
        )

    def test_prunes_stale_and_invalid_keeps_fresh(self) -> None:
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            now = _time.time()
            fresh = base / ".jev-tools.json"
            self._sidecar(fresh, now)
            stale = base / ".jev-tools-miss.json"
            self._sidecar(stale, now - 999999)
            invalid = base / "sub" / ".jev-tools-extra.json"
            invalid.parent.mkdir()
            invalid.write_text("not-json", encoding="utf-8")
            removed = INV.prune_stale_sidecars(base)
            self.assertEqual(set(removed), {stale, invalid})
            self.assertTrue(fresh.is_file())
            self.assertFalse(stale.exists())
            self.assertFalse(invalid.exists())

    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(
            INV.prune_stale_sidecars(Path("no-such-dir-xyz")), []
        )

    def test_cli_prune_sidecars(self) -> None:
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            stale = base / ".jev-tools.json"
            self._sidecar(stale, _time.time() - 999999)
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = INV.main(["--prune-sidecars", str(base)])
            self.assertEqual(rc, 0)
            self.assertIn("pruned 1 stale sidecars", buf.getvalue())
            self.assertFalse(stale.exists())


class DedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def _items(self):
        return INV.scan("hermes", hermes=FIXTURE)

    def _payload(self, prompt: str, cwd: str) -> dict:
        return {
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "cwd": cwd,
        }

    def test_same_prompt_reuses_sidecar_without_jev(self) -> None:
        calls = []

        def counting_pick(prompt, harness, picked):
            calls.append(prompt)
            return skip_pick(prompt, harness, picked)

        with tempfile.TemporaryDirectory() as tmp:
            prompt = "Add JWT access tokens in Python"
            out1 = HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
            self.assertTrue((Path(tmp) / ".jev-tools.json").is_file())
            out2 = HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
        self.assertEqual(len(calls), 1)
        note2 = out2["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            note2, out1["hookSpecificOutput"]["additionalContext"]
        )

    def test_different_prompt_does_not_dedupe(self) -> None:
        calls = []

        def counting_pick(prompt, harness, picked):
            calls.append(prompt)
            return skip_pick(prompt, harness, picked)

        with tempfile.TemporaryDirectory() as tmp:
            HOOK.handle(
                self._payload("Add JWT access tokens in Python", tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
            HOOK.handle(
                self._payload("Draw an ascii banner", tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
        self.assertEqual(len(calls), 2)

    def test_whitespace_variant_prompt_dedupes(self) -> None:
        calls = []

        def counting_pick(prompt, harness, picked):
            calls.append(prompt)
            return skip_pick(prompt, harness, picked)

        with tempfile.TemporaryDirectory() as tmp:
            HOOK.handle(
                self._payload("Add JWT access tokens in Python", tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
            HOOK.handle(
                self._payload("  Add   JWT access  tokens in\nPython  ", tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
        self.assertEqual(len(calls), 1)

    def test_case_variant_prompt_dedupes(self) -> None:
        calls = []

        def counting_pick(prompt, harness, picked):
            calls.append(prompt)
            return skip_pick(prompt, harness, picked)

        with tempfile.TemporaryDirectory() as tmp:
            HOOK.handle(
                self._payload("Add JWT access tokens in Python", tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
            HOOK.handle(
                self._payload("add jwt access tokens in python", tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
        self.assertEqual(len(calls), 1)

    def test_corrupt_sidecar_does_not_dedupe(self) -> None:
        calls = []

        def counting_pick(prompt, harness, picked):
            calls.append(prompt)
            return skip_pick(prompt, harness, picked)

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".jev-tools.json").write_text("{broken", encoding="utf-8")
            HOOK.handle(
                self._payload("Add JWT access tokens in Python", tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
        self.assertEqual(len(calls), 1)

    def test_stale_sidecar_does_not_dedupe(self) -> None:
        import time as _time
        calls = []

        def counting_pick(prompt, harness, picked):
            calls.append(prompt)
            return skip_pick(prompt, harness, picked)

        with tempfile.TemporaryDirectory() as tmp:
            prompt = "Add JWT access tokens in Python"
            HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
            sidecar = Path(tmp) / ".jev-tools.json"
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            data["written_at"] = int(_time.time() - 999999)
            sidecar.write_text(json.dumps(data), encoding="utf-8")
            HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=counting_pick,
            )
        self.assertEqual(len(calls), 2)

    def test_stale_sidecar_marks_decision(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            prompt = "Add JWT access tokens in Python"
            HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertFalse(HOOK.LAST_DECISION.get("stale_sidecar", False))
            sidecar = Path(tmp) / ".jev-tools.json"
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            data["written_at"] = int(_time.time() - 999999)
            sidecar.write_text(json.dumps(data), encoding="utf-8")
            HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertTrue(HOOK.LAST_DECISION.get("stale_sidecar"))
            sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertTrue(sidecar_data.get("stale_sidecar"))

    def test_dedupe_winner_note_replayed(self) -> None:
        def winner_pick(prompt, harness, picked):
            return {"status": "winner", "winner": picked[0]}

        with tempfile.TemporaryDirectory() as tmp:
            prompt = "Add JWT access tokens in Python"
            out1 = HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=winner_pick,
            )
            called = []

            def boom(prompt, harness, picked):
                called.append(prompt)
                return {"status": "none"}

            out2 = HOOK.handle(
                self._payload(prompt, tmp),
                items=self._items(),
                harness="claude-code",
                pick_fn=boom,
            )
        self.assertEqual(called, [])
        self.assertEqual(
            out2["hookSpecificOutput"]["additionalContext"],
            out1["hookSpecificOutput"]["additionalContext"],
        )
        self.assertIn("jwt-auth", out2["hookSpecificOutput"]["additionalContext"])


class CheckMissTests(unittest.TestCase):
    def test_check_miss_fresh_stale_missing(self) -> None:
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            miss = Path(tmp) / ".jev-tools-miss.json"
            miss.write_text(
                json.dumps({"harness": "grok", "task": "t", "empty": True,
                            "written_at": int(_time.time())}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = INV.main(["--check-miss", str(miss)])
            self.assertEqual(rc, 0)
            self.assertTrue(buf.getvalue().strip().startswith("fresh"))

            buf = io.StringIO()
            with patch("sys.stdout", buf):
                INV.main(["--check-miss", str(Path(tmp) / "absent.json")])
            self.assertEqual(buf.getvalue().strip(), "missing")

    def test_check_miss_default_name(self) -> None:
        # Bare --check-miss uses the default miss filename (cwd-relative).
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                with patch("sys.stdout", buf):
                    rc = INV.main(["--check-miss"])
            finally:
                os.chdir(cwd)
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "missing")


class MissDedupeTests(unittest.TestCase):
    def test_same_task_fresh_miss_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.MISS_NAME
            INV.write_miss(path, "codex", "task a")
            first = INV.read_sidecar(path)["written_at"]
            INV.write_miss(path, "codex", "task a")
            second = INV.read_sidecar(path)["written_at"]
            self.assertEqual(first, second)

    def test_different_task_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.MISS_NAME
            INV.write_miss(path, "codex", "task a")
            INV.write_miss(path, "codex", "task b")
            self.assertEqual(INV.read_sidecar(path)["task"], "task b")

    def test_stale_miss_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.MISS_NAME
            path.write_text(
                json.dumps({"harness": "codex", "task": "task a", "empty": True, "written_at": 1})
                + "\n",
                encoding="utf-8",
            )
            INV.write_miss(path, "codex", "task a")
            self.assertGreater(INV.read_sidecar(path)["written_at"], 1)


class ShowSidecarTests(unittest.TestCase):
    def test_show_fresh_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.SIDECAR_NAME
            INV.write_sidecar(path, "codex", "task", [{"id": "s", "kind": "skill", "name": "jwt", "description": "", "path": ""}])
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = INV.main(["--show", str(path)])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["status"], "fresh")
            self.assertEqual(data["payload"]["items"][0]["name"], "jwt")

    def test_show_missing(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = INV.main(["--show", "no-such-file.json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["status"], "missing")
        self.assertEqual(data["payload"], {})


class DebugFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def _run_main(self, payload: dict, argv: list) -> tuple:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), patch(
            "sys.stdout", out_buf
        ), patch("sys.stderr", err_buf), patch.object(
            HOOK, "pick_with_jev", return_value={"status": "skip", "winner": None}
        ):
            rc = HOOK.main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_debug_writes_kv_stderr(self) -> None:
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens"}
        rc, out, err = self._run_main(payload, ["--debug"])
        self.assertEqual(rc, 0)
        self.assertIn("jev_status=", err)
        self.assertIn("shortlist=", err)

    def test_out_flag_writes_payload_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "hook.json"
            payload = {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens"}
            rc, out, err = self._run_main(payload, ["--out", str(out_path)])
            self.assertEqual(rc, 0)
            written = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(written, json.loads(out))

    def test_no_debug_flag_silent_stderr(self) -> None:
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens"}
        rc, out, err = self._run_main(payload, [])
        self.assertEqual(rc, 0)
        self.assertEqual(err.strip(), "")

    def test_debug_env_var_enables(self) -> None:
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens"}
        with patch.dict(os.environ, {"JEV_HOOK_DEBUG": "1"}):
            rc, out, err = self._run_main(payload, [])
        self.assertIn("jev_status=", err)


class WatchJqTests(unittest.TestCase):
    def test_watch_jq_prints_only_named_tick_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {
                        "event": "UserPromptSubmit",
                        "prompt": "jwt",
                        "cwd": str(cwd),
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(
                os.environ, {"JEV_HOOK_WATCH_MAX": "2", "JEV_HOOK_OFF": "1"}
            ):
                with patch("sys.stdout", buf):
                    rc = HOOK.main(
                        ["--file", str(payload), "--watch", "0.01", "--jq", "winner"]
                    )
            self.assertEqual(rc, 1)
            self.assertEqual(buf.getvalue().splitlines(), ["null", "null"])

class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = cwd / "payload.json"
            payload.write_text(
                json.dumps(
                    {"event": "UserPromptSubmit", "prompt": "jwt", "cwd": str(cwd)}
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "JEV_HOOK_WATCH_MAX": "0",
                    "JEV_HOOK_WATCH_SECS": "0.05",
                    "JEV_HOOK_OFF": "1",
                },
            ):
                start = _time.time()
                with patch("sys.stdout", buf):
                    rc = HOOK.main(
                        ["--file", str(payload), "--watch", "0.02"]
                    )
            self.assertEqual(rc, 1)
            self.assertLess(_time.time() - start, 2.0)
            ticks = [
                l for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertLessEqual(len(ticks), 10)
            self.assertGreaterEqual(len(ticks), 1)

class MaxPromptCharsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def _handle(self, prompt: str, env: dict) -> dict:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, env):
                out = HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": prompt,
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
        return out

    def test_over_cap_truncates_before_pick(self) -> None:
        prompt = "jwt " + ("x" * 300)
        self._handle(prompt, {"JEV_HOOK_MAX_PROMPT": "50"})
        self.assertTrue(HOOK.LAST_DECISION["prompt_truncated"])
        self.assertEqual(HOOK.LAST_DECISION["prompt_len"], 50)

    def test_under_cap_untouched(self) -> None:
        prompt = "Add JWT access tokens in Python"
        self._handle(prompt, {"JEV_HOOK_MAX_PROMPT": "500"})
        self.assertFalse(HOOK.LAST_DECISION["prompt_truncated"])
        self.assertEqual(HOOK.LAST_DECISION["prompt_len"], len(prompt))

    def test_cap_zero_never_truncates(self) -> None:
        prompt = "jwt " + ("x" * 50000)
        self._handle(prompt, {"JEV_HOOK_MAX_PROMPT": "0"})
        self.assertFalse(HOOK.LAST_DECISION["prompt_truncated"])
        self.assertEqual(HOOK.LAST_DECISION["prompt_len"], len(prompt))

    def test_helper_env_and_policy_and_default(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_MAX_PROMPT": "123"}):
            self.assertEqual(INV.hook_max_prompt_chars(), 123)
        with patch.dict(os.environ, {"JEV_HOOK_MAX_PROMPT": "bogus"}):
            self.assertEqual(INV.hook_max_prompt_chars(), 20000)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_HOOK_MAX_PROMPT", None)
            self.assertEqual(INV.hook_max_prompt_chars(), 20000)

    def test_env_report_exposes_resolved_cap(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_MAX_PROMPT": "77"}):
            report = HOOK.env_report()
        self.assertEqual(report["max_prompt_chars"], 77)


class MaxPayloadBytesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def _run_stdin(self, raw: str, env: dict) -> tuple:
        import io

        buf = io.StringIO()
        with patch.dict(os.environ, env):
            with patch.object(sys, "stdin", io.StringIO(raw)):
                with patch.object(sys, "stdout", buf):
                    rc = HOOK.main([])
        return rc, buf.getvalue()

    def test_over_cap_emits_empty_object(self) -> None:
        raw = json.dumps({"prompt": "x" * 500, "cwd": "c:/"})
        rc, out = self._run_stdin(raw, {"JEV_HOOK_MAX_PAYLOAD": "64"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")

    def test_under_cap_processed(self) -> None:
        raw = json.dumps({"prompt": "hi", "cwd": "c:/nope"})
        rc, out = self._run_stdin(raw, {"JEV_HOOK_MAX_PAYLOAD": "4096"})
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())  # {} or a winner payload, either way reads

    def test_cap_zero_unlimited(self) -> None:
        raw = json.dumps({"prompt": "x" * 5000, "cwd": "c:/"})
        rc, out = self._run_stdin(raw, {"JEV_HOOK_MAX_PAYLOAD": "0"})
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())

    def test_helper_env_and_default(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_MAX_PAYLOAD": "123"}):
            self.assertEqual(INV.hook_max_payload_bytes(), 123)
        with patch.dict(os.environ, {"JEV_HOOK_MAX_PAYLOAD": "bogus"}):
            self.assertEqual(INV.hook_max_payload_bytes(), 1048576)
        os.environ.pop("JEV_HOOK_MAX_PAYLOAD", None)
        self.assertEqual(INV.hook_max_payload_bytes(), 1048576)

    def test_env_report_exposes_cap(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_MAX_PAYLOAD": "77"}):
            report = HOOK.env_report()
        self.assertEqual(report["max_payload_bytes"], 77)


class EnvReportMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    EXPECTED_KEYS = {
        "budget_seconds", "dedupe_ttl_seconds", "events", "jev_hook_cwd",
        "jev_hook_debug", "jev_hook_debug_file", "jev_hook_event",
        "jev_hook_events", "jev_hook_harness", "jev_hook_nomiss",
        "jev_hook_nosidecar", "jev_hook_off", "jev_hook_prompt",
        "jev_hook_skip_events", "jev_hook_winner", "jev_retries",
        "jev_timeout_seconds", "limit", "max_age_seconds", "max_payload_bytes",
        "max_prompt_chars", "miss_present", "note_limit", "sidecar_present",
        "ttl_seconds", "watch_max", "watch_secs", "watch_quiet",
    }

    def test_report_covers_every_knob(self) -> None:
        report = HOOK.env_report()
        self.assertTrue(self.EXPECTED_KEYS <= set(report.keys()))

    def test_ttl_seconds_honors_env(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_TTL": "9"}):
            self.assertEqual(HOOK.env_report()["ttl_seconds"], 9.0)

    def test_watch_knobs_honor_env(self) -> None:
        env = {
            "JEV_HOOK_WATCH_MAX": "3",
            "JEV_HOOK_WATCH_SECS": "7.5",
            "JEV_HOOK_WATCH_QUIET": "1",
        }
        with patch.dict(os.environ, env):
            report = HOOK.env_report()
        self.assertEqual(report["watch_max"], 3)
        self.assertEqual(report["watch_secs"], 7.5)
        self.assertTrue(report["watch_quiet"])


class StdinGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def _stdin(self, raw: str) -> tuple:
        import io

        buf = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(raw)):
            with patch.object(sys, "stdout", buf):
                rc = HOOK.main([])
        return rc, buf.getvalue()

    def test_empty_stdin_emits_empty_object(self) -> None:
        rc, out = self._stdin("")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")

    def test_garbage_stdin_emits_empty_object(self) -> None:
        rc, out = self._stdin("not json at all")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")

    def test_whitespace_stdin_emits_empty_object(self) -> None:
        rc, out = self._stdin("   \n\t  ")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")

    def test_null_bytes_stdin_fail_open(self) -> None:
        rc, out = self._stdin("\x00\x01\x02")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")

    def test_array_stdin_emits_empty_object(self) -> None:
        rc, out = self._stdin("[1,2,3]")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "{}")



class EnvOnMatrixTests(unittest.TestCase):
    TRUTHY = ["1", "true", "yes", "on", "TRUE", "On"]
    FALSY = ["0", "false", "no", "off", "", "2"]

    def test_env_on_accepts_documented_truthy_forms(self) -> None:
        for val in self.TRUTHY:
            for name in (
                "JEV_HOOK_OFF",
                "JEV_HOOK_NOSIDECAR",
                "JEV_HOOK_NOMISS",
                "JEV_HOOK_DEBUG",
            ):
                with patch.dict(os.environ, {name: val}):
                    self.assertTrue(HOOK._env_on(name), "%s=%r" % (name, val))

    def test_env_on_rejects_other_values(self) -> None:
        for val in self.FALSY:
            for name in (
                "JEV_HOOK_OFF",
                "JEV_HOOK_NOSIDECAR",
                "JEV_HOOK_NOMISS",
                "JEV_HOOK_DEBUG",
            ):
                with patch.dict(os.environ, {name: val}):
                    self.assertFalse(HOOK._env_on(name), "%s=%r" % (name, val))

    def test_hook_off_uppercase_disables(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_OFF": "TRUE"}):
            self.assertEqual(
                HOOK.handle(
                    {"hook_event_name": "UserPromptSubmit", "prompt": "jwt"},
                    items=[
                        {
                            "kind": "skill",
                            "name": "jwt-auth",
                            "id": "skill_jwt_auth",
                            "description": "jwt",
                        }
                    ],
                    harness="claude-code",
                ),
                {},
            )

    def test_debug_enabled_accepts_on(self) -> None:
        with patch.dict(os.environ, {"JEV_HOOK_DEBUG": "on"}):
            self.assertTrue(HOOK._debug_enabled([]))


class WrongTypedFieldTests(unittest.TestCase):
    BAD = [
        {"hook_event_name": 123, "prompt": "jwt"},
        {"hook_event_name": "UserPromptSubmit", "prompt": ["list"]},
        {"hook_event_name": "UserPromptSubmit", "prompt": 0},
        {"hook_event_name": "UserPromptSubmit", "prompt": {"x": 1}},
        {"hook_event_name": "UserPromptSubmit", "prompt": "jwt", "cwd": {"x": 1}},
        {"hook_event_name": "UserPromptSubmit", "prompt": "jwt", "cwd": ["/tmp"]},
        {"hook_event_name": "UserPromptSubmit", "prompt": "jwt", "timestamp": "not-a-number"},
        {"hook_event_name": None, "prompt": "jwt"},
        {},
    ]

    def test_wrong_typed_fields_never_raise(self) -> None:
        for payload in self.BAD:
            with self.subTest(payload=payload):
                try:
                    out = HOOK.handle(payload, items=[], harness="claude-code")
                except Exception as err:
                    self.fail("handle raised %r on %r" % (err, payload))
                self.assertIsInstance(out, dict)

    def test_wrong_typed_fields_write_nothing_to_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for payload in self.BAD:
                HOOK.handle(payload, items=[], harness="claude-code")
            leaked = [x.name for x in Path(tmp).iterdir()]
            self.assertEqual(leaked, [], "handle wrote into cwd: %s" % leaked)

if __name__ == "__main__":
    unittest.main(verbosity=2)
