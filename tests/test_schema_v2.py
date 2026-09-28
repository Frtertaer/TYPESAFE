#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Schema-v2 routing records + core_skill_tokens IDF aliases.

The routing record gained `schema`, `pick_confidence`, `need_skill_score` and
`escalate_reason`; readers keep handling v1 records (tagged schema_v1). The
policy.json `core_skill_tokens` list gives an installed item extra name-tokens
so decision-type prompts surface jev-consult without a literal name token.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import decisions
import inventory
import inventory_hook
import jev


PICKED = [
    {"id": "skill:alpha", "kind": "skill", "name": "alpha", "description": "Alpha skill"},
    {"id": "skill:beta", "kind": "skill", "name": "beta", "description": "Beta skill"},
]

_MISSING = object()


def fake_jev(answers=None, decide_ret=None):
    """Stub jev module for pick_with_jev (mirrors test_inventory_hook's)."""

    class M:
        @staticmethod
        def load_api_key():
            return "k"

        @staticmethod
        def load_policy(path=None):
            return {}

        @staticmethod
        def post_systemone(state, questions, policy, **kwargs):
            return {"answers": answers or {}, "model": "fake-0"}

        @staticmethod
        def decide(ans, policy, irreversible=False):
            return decide_ret or {"action": "proceed", "picks": {}, "probabilities": {}}

    return M


def v1_record(**over):
    """A schema-v1 routing record: every pre-v2 required key, no v2 keys."""
    rec = {
        "ts": 1000.0,
        "harness": "hermes",
        "prompt_sha": "aa11bb22cc33",
        "prompt_head": "v1 prompt",
        "prompt_tail": "v1 prompt",
        "prompt_len": 9,
        "prompt_truncated": False,
        "n_catalog": 2,
        "shortlist_n": 1,
        "shortlist": ["skill:alpha"],
        "explicit": False,
        "jev_status": "winner",
        "reason": "jev picked alpha",
        "question": "load_tools",
        "need": 0.9,
        "probabilities": {"skill:alpha": 0.9, "none": 0.1},
        "shortlist_score_avg": 5.0,
        "winner": {"kind": "skill", "name": "alpha"},
        "strong_pick": True,
        "latency_ms": 50,
        "budget_ms": 12000,
        "over_budget": False,
        "stale_sidecar": False,
        "sidecar_age_s": None,
    }
    rec.update(over)
    return rec


def v2_record(**over):
    """A schema-v2 routing record (v1 fields plus the new outcome fields)."""
    rec = v1_record()
    rec.update(
        {
            "schema": 2,
            "need_skill_score": 0.9,
            "pick_confidence": 0.9,
            "escalate_reason": None,
        }
    )
    rec.update(over)
    return rec


class DecideReasonsTests(unittest.TestCase):
    """jev.decide reports machine-readable `reasons` for every escalate path."""

    def test_proceed_reasons_empty(self):
        out = jev.decide(
            {
                "load_tools": {
                    "type": "choice",
                    "choice": "x",
                    "confidence": 0.9,
                    "probabilities": {"x": 0.9, "y": 0.1},
                }
            },
            {},
        )
        self.assertEqual(out["action"], "proceed")
        self.assertEqual(out["reasons"], [])

    def test_low_confidence_reason(self):
        out = jev.decide(
            {
                "q": {
                    "type": "choice",
                    "choice": "x",
                    "confidence": 0.3,
                    "probabilities": {"x": 1.0},
                }
            },
            {},
        )
        self.assertEqual(out["action"], "escalate")
        self.assertIn("low_confidence", out["reasons"])

    def test_malformed_reasons(self):
        out = jev.decide(
            {
                "a": "not-a-dict",
                "b": {"type": "choice"},
                "c": {"type": "noul", "noul": "bad"},
                "d": {"type": "score", "score": "bad"},
                "e": {"type": "wat"},
            },
            {},
        )
        self.assertEqual(out["action"], "escalate")
        for code in (
            "malformed_answer",
            "malformed_choice",
            "malformed_noul",
            "malformed_score",
            "unknown_type",
        ):
            self.assertIn(code, out["reasons"])

    def test_score_low_confidence_reason(self):
        out = jev.decide(
            {"q": {"type": "score", "score": 5, "confidence": 0.1}}, {}
        )
        self.assertIn("low_score_confidence", out["reasons"])

    def test_tight_gap_only_escalates_irreversible(self):
        answers = {
            "q": {
                "type": "choice",
                "choice": "x",
                "confidence": 0.9,
                "probabilities": {"x": 0.51, "y": 0.49},
            }
        }
        soft = jev.decide(answers, {}, irreversible=False)
        hard = jev.decide(answers, {}, irreversible=True)
        self.assertIn("tight_gap", soft["reasons"])
        self.assertEqual(soft["action"], "proceed")
        self.assertEqual(hard["action"], "escalate")


class ResolvePickerEscalateReasonTests(unittest.TestCase):
    """Every resolve_picker return tags the escalate_reason enum."""

    def test_decide_level_confidence_floor(self):
        for reasons in (["low_confidence"], ["low_score_confidence"],
                        ["malformed_choice", "low_confidence"]):
            out = inventory.resolve_picker(
                PICKED, {"action": "escalate", "reasons": reasons, "picks": {}}
            )
            self.assertEqual(out["escalate_reason"], "confidence_floor")

    def test_other_escalate_is_model_escalate(self):
        for decision in (
            {"action": "escalate", "reasons": ["tight_gap"], "picks": {}},
            {"action": "escalate", "reasons": "oops", "picks": {}},
            {"action": "escalate", "picks": {}},
        ):
            out = inventory.resolve_picker(PICKED, decision)
            self.assertEqual(out["status"], "escalate")
            self.assertEqual(out["escalate_reason"], "model_escalate")

    def test_non_dict_decision_is_model_escalate(self):
        self.assertEqual(
            inventory.resolve_picker(PICKED, None)["escalate_reason"],
            "model_escalate",
        )

    def test_none_pick(self):
        out = inventory.resolve_picker(
            PICKED,
            {"action": "proceed", "picks": {"load_tools": "none", "need_skill": 0.9}},
        )
        self.assertEqual(out["status"], "none")
        self.assertEqual(out["escalate_reason"], "none_pick")

    def test_unresolvable_pick_is_none_pick(self):
        out = inventory.resolve_picker(
            PICKED,
            {"action": "proceed", "picks": {"load_tools": "skill:ghost", "need_skill": 0.9}},
        )
        self.assertEqual(out["status"], "none")
        self.assertEqual(out["escalate_reason"], "none_pick")

    def test_need_gate_removed_pick_wins(self):
        # The need_skill gate is retired: a resolvable pick wins regardless
        # of the noul, which is still carried as telemetry only.
        low = inventory.resolve_picker(
            PICKED,
            {"action": "proceed", "picks": {"load_tools": "skill:alpha", "need_skill": 0.2}},
        )
        self.assertEqual(low["status"], "winner")
        self.assertIsNone(low["escalate_reason"])
        unsure = inventory.resolve_picker(
            PICKED,
            {"action": "proceed", "picks": {"load_tools": "skill:alpha", "need_skill": 0.5}},
        )
        self.assertEqual(unsure["status"], "winner")
        self.assertIsNone(unsure["escalate_reason"])

    def test_malformed_need_is_model_escalate(self):
        out = inventory.resolve_picker(
            PICKED,
            {"action": "proceed", "picks": {"load_tools": "skill:alpha", "need_skill": "bad"}},
        )
        self.assertEqual(out["escalate_reason"], "model_escalate")

    def test_winner_reason_none(self):
        for need in (0.8, 0.95):
            out = inventory.resolve_picker(
                PICKED,
                {
                    "action": "proceed",
                    "picks": {"load_tools": "skill:alpha", "need_skill": need},
                    "probabilities": {"load_tools": {"skill:alpha": 0.6}},
                },
            )
            self.assertEqual(out["status"], "winner")
            self.assertIsNone(out["escalate_reason"])


class PickWithJevSchemaV2Tests(unittest.TestCase):
    """pick_with_jev records the conf_floor operand and the raw need score."""

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

    def test_winner_records_pick_confidence_and_need(self):
        sys.modules["jev"] = fake_jev(
            answers={
                "load_tools": {"confidence": 0.9},
                "need_skill": {"noul": 0.9},
            },
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "skill:alpha", "need_skill": 0.9},
                "probabilities": {"load_tools": {"skill:alpha": 0.9}},
            },
        )
        out = inventory_hook.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "winner")
        self.assertEqual(out["pick_confidence"], 0.9)
        self.assertEqual(out["need_skill_score"], 0.9)
        self.assertIsNone(out["escalate_reason"])

    def test_confidence_floor_preempt_recorded(self):
        sys.modules["jev"] = fake_jev(
            answers={"load_tools": {"confidence": 0.3}},
            decide_ret={
                "action": "escalate",
                "reasons": ["low_confidence"],
                "picks": {},
                "probabilities": {},
            },
        )
        out = inventory_hook.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "escalate")
        self.assertEqual(out["escalate_reason"], "confidence_floor")
        self.assertEqual(out["pick_confidence"], 0.3)

    def test_low_need_pick_wins_and_need_is_recorded(self):
        # need_skill no longer gates the pick; it is still logged.
        sys.modules["jev"] = fake_jev(
            answers={
                "load_tools": {"confidence": 0.9},
                "need_skill": {"noul": 0.4},
            },
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "skill:alpha", "need_skill": 0.4},
                "probabilities": {},
            },
        )
        out = inventory_hook.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "winner")
        self.assertIsNone(out["escalate_reason"])
        self.assertEqual(out["need_skill_score"], 0.4)

    def test_missing_confidence_is_none(self):
        sys.modules["jev"] = fake_jev(
            answers={"load_tools": {}, "need_skill": {"noul": 0.9}},
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "skill:alpha", "need_skill": 0.9},
                "probabilities": {},
            },
        )
        out = inventory_hook.pick_with_jev("t", "hermes", PICKED)
        self.assertIsNone(out["pick_confidence"])


class HandleRecordV2Tests(unittest.TestCase):
    """inventory_hook writes the schema-v2 fields on every routing record."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "decisions.jsonl"
        self._env = patch.dict(
            os.environ, {"JEV_CONSULT_LOG": str(self.log_path)}
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def _handle(self, prompt, pick_fn, items=PICKED, harness="hermes"):
        return inventory_hook.handle(
            {"hook_event_name": "pre_llm_call", "prompt": prompt, "cwd": self._tmp.name},
            items=items,
            harness=harness,
            pick_fn=pick_fn,
        )

    def test_winner_record_is_v2(self):
        self._handle(
            "alpha task",
            lambda *a: {
                "status": "winner",
                "winner": PICKED[0],
                "question": "load_tools",
                "need": 0.9,
                "probabilities": {"skill:alpha": 0.9},
                "pick_confidence": 0.9,
                "latency_ms": 7,
            },
        )
        rec = inventory_hook.LAST_DECISION
        self.assertEqual(rec["schema"], 2)
        self.assertEqual(rec["pick_confidence"], 0.9)
        self.assertEqual(rec["need_skill_score"], 0.9)
        self.assertIsNone(rec["escalate_reason"])
        self.assertEqual(decisions.record_schema(rec), 2)

    def test_none_record_gets_none_pick(self):
        self._handle(
            "alpha task",
            lambda *a: {"status": "none", "winner": None, "need": 0.2},
        )
        rec = inventory_hook.LAST_DECISION
        self.assertEqual(rec["schema"], 2)
        self.assertEqual(rec["escalate_reason"], "none_pick")
        self.assertEqual(rec["need_skill_score"], 0.2)

    def test_escalate_record_reasons(self):
        self._handle(
            "alpha task",
            lambda *a: {
                "status": "escalate",
                "winner": None,
                "escalate_reason": "confidence_floor",
                "pick_confidence": 0.3,
            },
        )
        self.assertEqual(inventory_hook.LAST_DECISION["escalate_reason"], "confidence_floor")
        # A chooser that never sets escalate_reason still gets one derived.
        self._handle(
            "alpha other",
            lambda *a: {"status": "escalate", "winner": None},
        )
        self.assertEqual(inventory_hook.LAST_DECISION["escalate_reason"], "model_escalate")

    def test_empty_shortlist_no_candidates(self):
        self._handle("nothing matches", lambda *a: None, items=[])
        rec = inventory_hook.LAST_DECISION
        self.assertEqual(rec["schema"], 2)
        self.assertEqual(rec["escalate_reason"], "no_candidates")

    def test_dedupe_record_carries_v2_fields(self):
        def pick(*a):
            return {
                "status": "winner",
                "winner": PICKED[0],
                "question": "load_tools",
                "need": 0.8,
                "probabilities": {"skill:alpha": 0.8},
                "pick_confidence": 0.8,
                "latency_ms": 5,
            }

        self._handle("alpha task", pick)
        first = inventory_hook.LAST_DECISION
        self.assertFalse(first.get("dedupe") is True)
        sidecar = json.loads(
            (Path(self._tmp.name) / ".jev-tools.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sidecar.get("pick_confidence"), 0.8)
        self.assertEqual(sidecar.get("need_skill_score"), 0.8)
        self._handle("alpha task", lambda *a: self.fail("dedupe must not re-pick"))
        rec = inventory_hook.LAST_DECISION
        self.assertTrue(rec["dedupe"])
        self.assertEqual(rec["schema"], 2)
        self.assertEqual(rec["pick_confidence"], 0.8)
        self.assertEqual(rec["need_skill_score"], 0.8)
        self.assertEqual(rec["jev_status"], "winner")

    def test_record_round_trips_through_append_decision(self):
        self._handle(
            "alpha task",
            lambda *a: {
                "status": "winner",
                "winner": PICKED[0],
                "question": "load_tools",
                "need": 0.9,
                "probabilities": {"skill:alpha": 0.9},
                "pick_confidence": 0.9,
                "latency_ms": 3,
            },
        )
        entries, bad = decisions.load_entries(self.log_path)
        self.assertEqual(bad, 0)
        self.assertEqual(len(entries), 1)
        rec = entries[0]
        self.assertEqual(rec["schema"], 2)
        self.assertEqual(rec["pick_confidence"], 0.9)
        self.assertEqual(rec["need_skill_score"], 0.9)
        self.assertIn("escalate_reason", rec)


class ReaderParityTests(unittest.TestCase):
    """Mixed v1+v2 logs: every reader handles both without breaking."""

    def _log(self, entries):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "decisions.jsonl"
        lines = [json.dumps(e) for e in entries]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def _mixed(self):
        return [
            v1_record(ts=1000.0, jev_status="winner"),
            v1_record(
                ts=1001.0,
                jev_status="escalate",
                winner=None,
                need=0.5,
                probabilities={"skill:alpha": 0.4, "none": 0.6},
            ),
            v2_record(ts=1002.0, jev_status="winner"),
            v2_record(
                ts=1003.0,
                jev_status="escalate",
                winner=None,
                need=0.5,
                need_skill_score=0.5,
                pick_confidence=0.3,
                probabilities={"skill:alpha": 0.4, "none": 0.6},
                escalate_reason="confidence_floor",
            ),
            v2_record(
                ts=1004.0,
                jev_status="escalate",
                winner=None,
                need=0.5,
                need_skill_score=0.5,
                pick_confidence=0.9,
                probabilities={"skill:alpha": 0.4, "none": 0.6},
                escalate_reason="need_gate",
            ),
        ]

    def test_verify_counts_v1_without_failing(self):
        path = self._log(self._mixed())
        report = decisions.verify_log(path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["schema_v1"], 2)
        self.assertEqual(report["entries"], 5)

    def test_summarize_by_schema_and_reasons(self):
        stats = decisions.summarize(self._mixed())
        self.assertEqual(stats["by_schema"], {"schema_v1": 2, "schema_v2": 3})
        self.assertEqual(
            stats["by_escalate_reason"], {"confidence_floor": 1, "need_gate": 1}
        )
        text = decisions.format_stats(stats)
        self.assertIn("schema_v1=2", text)
        self.assertIn("schema_v2=3", text)
        self.assertIn("confidence_floor=1", text)

    def test_report_markdown_sections(self):
        import subprocess

        path = self._log(self._mixed())
        report = path.parent / "report.md"
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(path),
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = report.read_text(encoding="utf-8")
        self.assertIn("## by schema", text)
        self.assertIn("| schema_v1 | 2 |", text)
        self.assertIn("| schema_v2 | 3 |", text)
        self.assertIn("## escalate reasons", text)
        self.assertIn("| confidence_floor | 1 |", text)
        self.assertIn("| need_gate | 1 |", text)

    def test_calibrate_uses_recorded_pick_confidence(self):
        report = decisions.calibrate(self._mixed())
        self.assertEqual(report["entries"], 5)
        self.assertEqual(report["pick_confidence_records"], 3)
        # The confidence_floor preempt is replayed against the recorded 0.3 —
        # under the default floor it stays an escalate, while the same record
        # without the field falls back to the top probability (0.6 -> none).
        entry = self._mixed()[3]
        replayed = decisions._replay(dict(entry), 0.55, 0.85)
        self.assertEqual(replayed, "escalate")
        v1_entry = dict(entry)
        del v1_entry["pick_confidence"]
        self.assertEqual(
            decisions._replay(v1_entry, 0.55, 0.85), "none"
        )
        text = decisions.format_calibrate(report)
        self.assertIn("pick_confidence recorded on 3/5 entries", text)

    def test_calibrate_v1_falls_back_to_top_prob(self):
        report = decisions.calibrate([v1_record(), v1_record(ts=1001.0)])
        self.assertEqual(report["pick_confidence_records"], 0)
        self.assertEqual(report["entries"], 2)

    def test_acceptance_escalate_reasons(self):
        data = decisions.acceptance_report(self._mixed())
        self.assertEqual(
            data["escalate_reasons"], {"confidence_floor": 1, "need_gate": 1}
        )
        text = decisions.format_acceptance(data)
        self.assertIn("confidence_floor=1", text)

    def test_harness_health_carries_reasons(self):
        rows = decisions.harness_health(self._mixed())
        reasons = {}
        for row in rows:
            reasons.update(row.get("escalate_reasons") or {})
        self.assertEqual(
            reasons, {"confidence_floor": 1, "need_gate": 1}
        )
        text = decisions.format_health(rows)
        self.assertIn("confidence_floor=1", text)

    def test_schema_contract_marks_v2(self):
        required = {
            key for key, meta in decisions.ENTRY_SCHEMA_ROWS.items() if meta["required"]
        }
        self.assertIn("schema", required)
        self.assertIn("pick_confidence", required)
        self.assertIn("need_skill_score", required)
        self.assertIn("escalate_reason", required)
        self.assertEqual(inventory.ENTRY_SCHEMA_VERSION, 2)


class AppendDecisionRoundTripTests(unittest.TestCase):
    def test_new_fields_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            rec = v2_record(
                pick_confidence=0.42,
                need_skill_score=0.37,
                escalate_reason="confidence_floor",
            )
            self.assertTrue(inventory.append_decision(rec, path=path))
            entries, bad = decisions.load_entries(path)
        self.assertEqual(bad, 0)
        self.assertEqual(entries[0]["pick_confidence"], 0.42)
        self.assertEqual(entries[0]["need_skill_score"], 0.37)
        self.assertEqual(entries[0]["escalate_reason"], "confidence_floor")
        self.assertEqual(decisions.record_schema(entries[0]), 2)

    def test_record_schema_detection(self):
        self.assertEqual(decisions.record_schema(v1_record()), 1)
        self.assertEqual(decisions.record_schema(v2_record()), 2)
        # Any v2 key marks the record v2 even without an explicit marker.
        rec = v1_record()
        rec["pick_confidence"] = 0.5
        self.assertEqual(decisions.record_schema(rec), 2)


class CoreSkillTokensTests(unittest.TestCase):
    """Decision-type prompts surface jev-consult via policy.json aliases."""

    ITEMS = [
        {
            "id": "skill:jev-consult",
            "kind": "skill",
            "name": "jev-consult",
            "description": "Jev decides; you inspect and implement.",
        },
        {
            "id": "skill:readme-fixer",
            "kind": "skill",
            "name": "readme-fixer",
            "description": "Fix README files",
        },
    ]

    def _names(self, prompt, limit=5):
        return [
            item["name"]
            for item in inventory.shortlist(self.ITEMS, prompt, limit, [])
        ]

    def test_decision_prompt_surfaces_jev_consult(self):
        names = self._names("should I rewrite inventory.py or profile first?")
        self.assertIn("jev-consult", names)

    def test_decision_prompt_variants(self):
        for prompt in (
            "which library should I use for parsing toml",
            "keep this parser or rewrite it",
            "is this refactor risky",
            "is a dict good enough here",
            "approach for splitting the hook",
        ):
            self.assertIn("jev-consult", self._names(prompt), prompt)

    def test_mechanical_prompt_does_not_surface(self):
        names = self._names("fix the typo in README badge")
        self.assertNotIn("jev-consult", names)
        self.assertIn("readme-fixer", names)

    def test_observed_miss_phrases_surface(self):
        # Verbatim dogfood r2/r3 prompts that missed the jev-consult
        # shortlist (idf with empty shortlist, or a non-jev shortlist).
        for prompt in (
            # r2
            "I'm torn between keeping the log append-only and moving it into a real database.",
            "Two paths on the table: patch the migration runner or start fresh.",
            "Debating whether the Windows installer stays batch or becomes Python.",
            "Torn on the sidecar TTL: long at four hours or short at fifteen minutes?",
            "Can't settle on pinned or floating dependency ranges.",
            "On the fence about dropping Python 3.8 support this cycle.",
            # r3
            "torn between storing session state in redis or postgres",
            "on the fence about moving the scheduled jobs into a separate process",
            "hold or fold the custom auth middleware?",
            "debating if the scheduler belongs inside the api process",
        ):
            self.assertIn("jev-consult", self._names(prompt), prompt)

    def test_overbroad_constituents_stay_quiet(self):
        # "can" and "table" appear inside observed misses but were left
        # un-aliased: alone they would surface jev-consult on mechanical
        # prompts.
        for prompt in (
            "Can you run the unittest suite and paste the failures.",
            "Query the postgres table for duplicate emails.",
        ):
            self.assertNotIn("jev-consult", self._names(prompt), prompt)

    def test_policy_override_without_key_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"JEV_POLICY": str(policy)}):
                self.assertEqual(inventory.core_skill_tokens(), {})
                names = self._names("should I rewrite inventory.py or profile first?")
                self.assertNotIn("jev-consult", names)

    def test_policy_override_alias_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(
                json.dumps(
                    {"core_skill_tokens": {"jev-consult": ["zz bespoke token"]}}
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"JEV_POLICY": str(policy)}):
                names = self._names("zz bespoke token task")
                self.assertIn("jev-consult", names)

    def test_aliases_land_in_name_df(self):
        df = inventory.name_df(self.ITEMS, {"rewrite"})
        self.assertEqual(df["rewrite"], 1)


if __name__ == "__main__":
    unittest.main()
