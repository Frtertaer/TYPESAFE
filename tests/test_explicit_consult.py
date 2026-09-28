#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Explicit-consult bypass: detection dictionary, route marker, floor bypass.

When the prompt matches policy.json `explicit_consult_tokens` (per-locale
phrase lists — "should i", "посоветуй", "soll ich", "est-ce que je
devrais"), inventory_hook routes to Jev even with an
empty IDF shortlist (the jev-consult item joins the candidates) and a pick
suppressed only by the confidence floor surfaces as the winner instead of
escalating. Records carry `route: "explicit_consult"` so the bypass stays
out of the router metrics (acceptance excludes it, stats show it)."""
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

import compare
import decisions
import inventory
import inventory_hook

CONSULT = {
    "id": "skill_jev_consult",
    "kind": "skill",
    "name": "jev-consult",
    "description": "Jev decides; you inspect and implement",
}
PICKED = [
    {"id": "skill:alpha", "kind": "skill", "name": "alpha", "description": "Alpha skill"},
    {"id": "skill:beta", "kind": "skill", "name": "beta", "description": "Beta skill"},
]

_MISSING = object()


def fake_jev(answers=None, decide_ret=None):
    """Stub jev module for pick_with_jev (mirrors test_schema_v2's)."""

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


def floor_escalate(load="skill:alpha", top=0.4, need=0.9):
    """decide() payload: a pick suppressed only by the confidence floor."""
    return {
        "action": "escalate",
        "reasons": ["low_confidence"],
        "picks": {"load_tools": load, "need_skill": need},
        "probabilities": {"load_tools": {load: top}},
    }


class DetectConsultTests(unittest.TestCase):
    """explicit_consult matches policy phrases, en/ru/de/es/fr, whole-prompt only."""

    def test_policy_dict_shape(self):
        tokens = inventory.explicit_consult_tokens()
        for lang in ("en", "ru", "de", "es", "fr"):
            self.assertIn(lang, tokens)
        self.assertIn("should i", tokens["en"])
        self.assertIn("посоветуй", tokens["ru"])
        self.assertIn("soll ich", tokens["de"])
        self.assertIn("qué me recomiendas", tokens["es"])
        self.assertIn("est-ce que je devrais", tokens["fr"])

    def test_en_phrases(self):
        for prompt in (
            "should i keep this cache",
            "Should we migrate to postgres now",
            "is it worth rewriting this",
            "pick between these two libraries",
            "what would you do here",
            "can you recommend an approach",
            "please advise on the layout",
            "consult jev before I start",
            "help me decide between the two",
        ):
            self.assertIsNotNone(
                inventory.explicit_consult(prompt), prompt
            )

    def test_ru_phrases(self):
        for prompt in (
            "стоит ли удалять мёртвый код",
            "посоветуй как лучше сделать",
            "посоветуйте, что выбрать",
            "посоветуешь, какой подход",
            "что выбрать для кеша",
            "имеет смысл переписать",
            "оставить или изменить модуль",
        ):
            self.assertIsNotNone(
                inventory.explicit_consult(prompt), prompt
            )

    def test_de_phrases(self):
        for prompt in (
            "soll ich den cache behalten",
            "was hältst du von diesem ansatz",
            "lohnt es sich das umzuschreiben",
            "welche variante passt hier besser",
            "hilf mir entscheiden zwischen den beiden",
            "behalten oder ändern wir das modul",
            "eine zweite meinung wäre hilfreich",
        ):
            self.assertIsNotNone(
                inventory.explicit_consult(prompt), prompt
            )

    def test_es_phrases(self):
        for prompt in (
            "debería cambiar esta función",
            "lo dejo o lo borro",
            "qué me recomiendas aquí",
            "vale la pena migrar ahora",
            "ayúdame a decidir entre estas dos",
            "cuál es mejor para este caso",
            "que harias con este modulo",  # unaccented variant
        ):
            self.assertIsNotNone(
                inventory.explicit_consult(prompt), prompt
            )

    def test_fr_phrases(self):
        for prompt in (
            "est-ce que je devrais garder ce cache",
            "devrais-je le réécrire",
            "tu me conseilles quoi ici",
            "qu'en penses-tu pour cette approche",
            "lequel choisir pour le cache",
            "ça vaut le coup de migrer",
            "un deuxième avis serait utile",
            "aide moi a decider entre les deux",  # unaccented variant
        ):
            self.assertIsNotNone(
                inventory.explicit_consult(prompt), prompt
            )

    def test_returns_matched_phrase(self):
        self.assertEqual(
            inventory.explicit_consult("hmm, should I keep it"), "should i"
        )

    def test_false_positive_negatives(self):
        for prompt in (
            "the consultant said so",
            "recommended: update the docs",
            "decided to keep it yesterday",
            "the decision was already made",
            "explain how consult routing works",
            "what does the jev-consult skill do",
            "let me consult the docs first",
            "how do i fix the failing test",
            "решить конфликт в ветке",  # 'решить' is deliberately unlisted
            "advise the user to restart",  # 'advise me' only
            "pick the first line item",  # 'pick one'/'pick between' only
            "ich soll das dokument noch lesen",  # de: 'ich soll' is not 'soll ich'
            "das sollte klappen",  # de: 'sollte ich'/'sollten wir' only
            "wir entscheiden das morgen",  # de: 'hilf mir entscheiden' only
            "behalte die änderungen im branch",  # de: 'behalten oder ändern' only
            "dejé el archivo abierto",  # es: 'lo dejo o' only
            "él debería revisarlo mañana",  # es: 'debería cambiar' only
            "ella me aconsejó esperar",  # es: 'me aconsejas' only
            "vale, la pena ya pasó",  # es: comma breaks 'vale la pena'
            "me lo recomendó el equipo",  # es: 'me recomiendas' only
            "je devrais finir cette partie",  # fr: 'est-ce que je devrais'/'devrais-je' only
            "il m'a conseillé d'attendre",  # fr: 'tu me conseilles' only
            "la deuxième option gagne",  # fr: 'deuxième avis' only
            "vous choisirez la couleur",  # fr: 'lequel choisir' only
            "ça vaut mieux ainsi",  # fr: 'ça vaut le coup' only
            "garde le fichier pour moi",  # fr: 'je garde ou' only
        ):
            self.assertIsNone(
                inventory.explicit_consult(prompt), prompt
            )

    def test_empty_and_missing_inputs(self):
        self.assertIsNone(inventory.explicit_consult(""))
        self.assertIsNone(inventory.explicit_consult(None))

    def test_missing_key_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(json.dumps({"version": 3}), encoding="utf-8")
            with patch.dict(os.environ, {"JEV_POLICY": str(policy)}):
                self.assertEqual(inventory.explicit_consult_tokens(), {})
                self.assertIsNone(inventory.explicit_consult("should i keep this"))

    def test_bad_shape_fails_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "policy.json"
            policy.write_text(
                json.dumps({"explicit_consult_tokens": "should i"}),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"JEV_POLICY": str(policy)}):
                self.assertEqual(inventory.explicit_consult_tokens(), {})


class ConsultItemTests(unittest.TestCase):
    def test_finds_by_name_and_id(self):
        self.assertIs(inventory.consult_item([CONSULT, *PICKED]), CONSULT)
        slug = {"id": "skill_jev_consult", "kind": "skill", "name": "other"}
        self.assertIs(inventory.consult_item([slug]), slug)

    def test_absent(self):
        self.assertIsNone(inventory.consult_item(PICKED))
        self.assertIsNone(inventory.consult_item([]))


class ResolvePickerConsultTests(unittest.TestCase):
    """The consult flag recovers picks suppressed only by the floor."""

    def test_floor_suppressed_pick_surfaces_as_winner(self):
        out = inventory.resolve_picker(
            PICKED, floor_escalate("skill:alpha", top=0.4), consult=True
        )
        self.assertEqual(out["status"], "winner")
        self.assertEqual(out["winner"]["id"], "skill:alpha")
        self.assertIsNone(out["escalate_reason"])
        self.assertFalse(out.get("strong", False))  # 0.4 < strong_pick

    def test_floor_suppressed_none_pick_stays_none(self):
        out = inventory.resolve_picker(
            PICKED, floor_escalate("none", top=0.4), consult=True
        )
        self.assertEqual(out["status"], "none")
        self.assertEqual(out["escalate_reason"], "none_pick")

    def test_floor_suppressed_unknown_pick_stays_none(self):
        out = inventory.resolve_picker(
            PICKED, floor_escalate("skill:missing", top=0.4), consult=True
        )
        self.assertEqual(out["status"], "none")
        self.assertEqual(out["escalate_reason"], "none_pick")

    def test_model_escalate_still_escalates(self):
        decision = floor_escalate("skill:alpha")
        decision["reasons"] = ["low_confidence", "malformed_answer"]
        out = inventory.resolve_picker(PICKED, decision, consult=True)
        self.assertEqual(out["status"], "escalate")
        self.assertEqual(out["escalate_reason"], "confidence_floor")

    def test_model_only_escalate_still_escalates(self):
        decision = floor_escalate("skill:alpha")
        decision["reasons"] = ["uncertain_noul"]
        out = inventory.resolve_picker(PICKED, decision, consult=True)
        self.assertEqual(out["status"], "escalate")
        self.assertEqual(out["escalate_reason"], "model_escalate")

    def test_without_consult_escalate_stands(self):
        out = inventory.resolve_picker(PICKED, floor_escalate("skill:alpha"))
        self.assertEqual(out["status"], "escalate")
        self.assertEqual(out["escalate_reason"], "confidence_floor")

    def test_strong_flag_on_floor_survivor(self):
        out = inventory.resolve_picker(
            PICKED, floor_escalate("skill:alpha", top=0.9), consult=True
        )
        self.assertEqual(out["status"], "winner")
        self.assertTrue(out["strong"])


class PickWithJevConsultTests(unittest.TestCase):
    def setUp(self):
        self._had_jev = sys.modules.get("jev", _MISSING)
        self.addCleanup(self._restore_jev)

    def _restore_jev(self):
        if self._had_jev is _MISSING:
            sys.modules.pop("jev", None)
        else:
            sys.modules["jev"] = self._had_jev

    def test_consult_route_tagged_and_floor_bypassed(self):
        sys.modules["jev"] = fake_jev(
            answers={
                "load_tools": {"confidence": 0.3},
                "need_skill": {"noul": 0.9},
            },
            decide_ret=floor_escalate("skill:alpha"),
        )
        out = inventory_hook.pick_with_jev("t", "hermes", PICKED, consult=True)
        self.assertEqual(out["status"], "winner")
        self.assertEqual(out["route"], "explicit_consult")
        self.assertEqual(out["winner"]["id"], "skill:alpha")

    def test_non_consult_unchanged(self):
        sys.modules["jev"] = fake_jev(
            answers={"load_tools": {"confidence": 0.3}},
            decide_ret=floor_escalate("skill:alpha"),
        )
        out = inventory_hook.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "escalate")
        self.assertNotIn("route", out)


class HandleConsultTests(unittest.TestCase):
    """handle() routes explicit-consult asks through the bypass."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_path = Path(self._tmp.name) / "decisions.jsonl"
        self._env = patch.dict(
            os.environ, {"JEV_CONSULT_LOG": str(self.log_path)}
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self._had_jev = sys.modules.get("jev", _MISSING)
        self.addCleanup(self._restore_jev)

    def _restore_jev(self):
        if self._had_jev is _MISSING:
            sys.modules.pop("jev", None)
        else:
            sys.modules["jev"] = self._had_jev

    def _handle(self, prompt, pick_fn, items, harness="hermes"):
        return inventory_hook.handle(
            {
                "hook_event_name": "pre_llm_call",
                "prompt": prompt,
                "cwd": self._tmp.name,
            },
            items=items,
            harness=harness,
            pick_fn=pick_fn,
        )

    def test_consult_routes_with_empty_shortlist(self):
        # prompt mentions nothing the catalog carries — without the bypass
        # the shortlist is empty and no Jev call happens at all.
        seen = []

        def pick(task, harness, picked):
            seen.append([item["id"] for item in picked])
            return {"status": "winner", "winner": picked[0]}

        self._handle("should i keep this thing", pick, items=[CONSULT])
        self.assertEqual(seen, [["skill_jev_consult"]])
        rec = inventory_hook.LAST_DECISION
        self.assertEqual(rec["route"], "explicit_consult")
        self.assertEqual(rec["jev_status"], "winner")

    def test_consult_joins_existing_shortlist(self):
        seen = []

        def pick(task, harness, picked):
            seen.append([item["id"] for item in picked])
            return {"status": "winner", "winner": picked[0]}

        self._handle("should i keep this, alpha is relevant", pick,
                     items=[CONSULT] + PICKED)
        # consult target first, IDF picks follow
        self.assertEqual(seen[0][0], "skill_jev_consult")
        self.assertIn("skill:alpha", seen[0])
        self.assertEqual(inventory_hook.LAST_DECISION["route"], "explicit_consult")

    def test_no_consult_target_means_idf_stands(self):
        seen = []
        self._handle(
            "should i keep this thing",
            lambda t, h, p: seen.append(p) or {"status": "none", "winner": None},
            items=PICKED,
        )
        # no jev-consult in the catalog -> prompt not in the shortlist
        # either -> nothing picked; route is still tagged.
        self.assertEqual(seen, [])
        rec = inventory_hook.LAST_DECISION
        self.assertEqual(rec["route"], "explicit_consult")
        self.assertEqual(rec["escalate_reason"], "no_candidates")

    def test_normal_prompt_gets_no_route(self):
        self._handle(
            "alpha task",
            lambda t, h, p: {"status": "none", "winner": None},
            items=PICKED,
        )
        self.assertIsNone(inventory_hook.LAST_DECISION["route"])

    def test_explicit_name_still_wins_over_consult(self):
        def pick(*a):
            self.fail("explicit mention must not reach the chooser")

        self._handle("use $alpha — should i keep this", pick, items=PICKED)
        rec = inventory_hook.LAST_DECISION
        self.assertEqual(rec["jev_status"], "winner")
        self.assertTrue(rec["explicit"])
        self.assertIsNone(rec["route"])

    def test_fail_open_statuses_keep_route_tag(self):
        # Jev unreachable -> same statuses as before, still tagged.
        self._handle(
            "should i keep this",
            lambda t, h, p: {"status": "error", "winner": None},
            items=[CONSULT],
        )
        rec = inventory_hook.LAST_DECISION
        self.assertEqual(rec["jev_status"], "error")
        self.assertEqual(rec["route"], "explicit_consult")

    def test_route_persists_via_append_decision(self):
        self._handle(
            "should i keep this",
            lambda t, h, p: {"status": "winner", "winner": CONSULT},
            items=[CONSULT],
        )
        entries, bad = decisions.load_entries(self.log_path)
        self.assertEqual(bad, 0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["route"], "explicit_consult")

    def test_route_survives_dedupe_replay(self):
        def pick(task, harness, picked):
            return {"status": "winner", "winner": CONSULT, "question": "load_tools"}

        self._handle("should i keep this", pick, items=[CONSULT])
        sidecar = json.loads(
            (Path(self._tmp.name) / ".jev-tools.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sidecar.get("route"), "explicit_consult")
        self._handle("should i keep this", lambda *a: self.fail("no re-pick"),
                     items=[CONSULT])
        rec = inventory_hook.LAST_DECISION
        self.assertTrue(rec["dedupe"])
        self.assertEqual(rec["route"], "explicit_consult")


class DecisionsRouteReportTests(unittest.TestCase):
    """route=explicit_consult shows in stats/report and is excluded from
    acceptance pick rates so the bypass can't inflate them."""

    def _records(self):
        return [
            {
                "ts": 1000.0,
                "schema": 2,
                "harness": "hermes",
                "prompt_sha": "aa11bb22cc33",
                "prompt_head": "alpha task",
                "jev_status": "winner",
                "question": "load_tools",
                "winner": {"kind": "skill", "name": "alpha"},
            },
            {
                "ts": 1001.0,
                "schema": 2,
                "harness": "hermes",
                "prompt_sha": "bb22cc33dd44",
                "prompt_head": "should i keep this",
                "jev_status": "winner",
                "question": "load_tools",
                "route": "explicit_consult",
                "winner": {"kind": "skill", "name": "jev-consult"},
            },
        ]

    def test_summarize_by_route(self):
        stats = decisions.summarize(self._records())
        self.assertEqual(stats["by_route"], {"explicit_consult": 1})
        out = decisions.format_stats(stats)
        self.assertIn("route: explicit_consult=1", out)

    def test_acceptance_excludes_consult_route(self):
        acc = decisions.acceptance_report(self._records())
        # without exclusion pick_rate would read 2/2 = 1.0
        self.assertEqual(acc["routing_entries"], 1)
        self.assertEqual(acc["picks"], 1)
        self.assertEqual(acc["pick_rate"], 1.0)
        self.assertEqual(
            acc["explicit_consult"], {"entries": 1, "picks": 1}
        )
        out = decisions.format_acceptance(acc)
        self.assertIn("explicit_consult: 1 routed (1 picks)", out)

    def test_consult_miss_not_counted_as_miss(self):
        recs = [
            {
                "ts": 1000.0,
                "schema": 2,
                "harness": "hermes",
                "prompt_sha": "bb22cc33dd44",
                "prompt_head": "should i keep this",
                "jev_status": "none",
                "route": "explicit_consult",
            }
        ]
        self.assertFalse(any(decisions.is_miss_entry(r) for r in recs))
        acc = decisions.acceptance_report(recs)
        self.assertEqual(acc["misses"], 0)
        self.assertEqual(acc["explicit_consult"]["entries"], 1)

    def test_consult_records_not_calibrate_eligible(self):
        rec = {
            "schema": 2,
            "jev_status": "winner",
            "route": "explicit_consult",
            "probabilities": {"skill:alpha": 0.4},
            "shortlist": ["skill:alpha"],
        }
        self.assertFalse(decisions._calibrate_eligible(rec))

    def test_v1_record_unchanged(self):
        rec = {"ts": 1.0, "jev_status": "idf", "prompt_sha": "x"}
        self.assertEqual(decisions.record_schema(rec), 1)
        rec["route"] = "explicit_consult"
        self.assertEqual(decisions.record_schema(rec), 1)

    def test_schema_row_declared(self):
        self.assertIn("route", decisions.ENTRY_SCHEMA_ROWS)
        self.assertFalse(decisions.ENTRY_SCHEMA_ROWS["route"]["required"])


class CompareConsultGateTests(unittest.TestCase):
    def test_corpus_negatives_declared(self):
        blob = compare.load_cases()
        marked = [c for c in blob["cases"] if c.get("explicit_consult") is False]
        self.assertGreaterEqual(len(marked), 3)
        for case in marked:
            self.assertFalse(case["expect_call"])

    def test_observed_route_labels(self):
        pool = json.loads(
            (ROOT / "skills" / "jev-consult" / "examples" / "compare-cases.json")
            .read_text(encoding="utf-8")
        )["routing_pool"]
        self.assertEqual(
            compare.observed_route({"prompt": "should i keep this"}, pool),
            "explicit_consult",
        )
        self.assertEqual(
            compare.observed_route({"prompt": "fix the typo"}, pool), "none"
        )
        self.assertEqual(
            compare.observed_route({"prompt": "dep-audit please"}, pool),
            "explicit",
        )

    def test_strict_failures_flags_consult_false_accept(self):
        rows = [
            {
                "id": "neg",
                "expect_call": False,
                "explicit_consult": False,
                "observed_route": "explicit_consult",
                "after": {"called_jev": False},
            }
        ]
        failures = compare.strict_failures(rows, live=False)
        self.assertTrue(any("explicit_consult" in f for f in failures))

    def test_strict_failures_flags_missed_bypass(self):
        rows = [
            {
                "id": "pos",
                "explicit_consult": True,
                "observed_route": "idf",
                "after": {"called_jev": True},
            }
        ]
        failures = compare.strict_failures(rows, live=False)
        self.assertTrue(any("missed bypass" in f for f in failures))

    def test_load_cases_rejects_nonbool_explicit_consult(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(
                {"cases": [{"id": "c", "prompt": "p", "explicit_consult": "false"}]},
                fh,
            )
        self.addCleanup(os.unlink, fh.name)
        with self.assertRaises(SystemExit):
            compare.load_cases(fh.name)


if __name__ == "__main__":
    unittest.main()
