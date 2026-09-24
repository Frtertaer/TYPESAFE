from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from io import StringIO
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import progress as progress_cli
import progress_core as progress


def policy():
    return json.loads((SCRIPTS.parent / "policy.json").read_text(encoding="utf-8"))


def plan(count=6):
    return {
        "id": "reliability",
        "goal": "Verify the agreed reliability outcomes",
        "checks": {"suite": ["{python}", "-c", "print('verified')"]},
        "required_checks": ["suite"],
        "items": [
            {"id": "item_%d" % n, "description": "Agreed outcome %d" % n, "checks": ["suite"], "paths": ["core_%d.py" % n]}
            for n in range(count)
        ],
        "directions": {"usability": "Improve the agreed usability workflow"},
        "platform": "any",
    }


def answer(question, choice, confidence=0.95, probabilities=None):
    keys = question["criteria"]
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": probabilities or {key: float(key == choice) for key in keys},
    }


def picker(choice, confidence=0.95, acceptance=None):
    def ask(state, questions, snapshot):
        answers = {}
        for key, question in questions.items():
            selected = choice
            if key.startswith("accept_"):
                selected = (acceptance or {}).get(key[len("accept_"):], "met")
            answers[key] = answer(question, selected, confidence)
        return {"model": "test-model", "answers": answers}
    return Mock(side_effect=ask)


class FakeEvidence:
    def __init__(self):
        self.revision = "a" * 40
        self.tree = "1" * 40
        self.exit_code = 0
        self.per_check = {}
        self.changed_during_check = False
        self.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n-old behavior\n+verified behavior\n"
        self.platform = sys.platform
        self.check_runs = 0
        self.config = {}

    def snapshot(self, settings):
        return {"revision": self.revision, "tree": self.tree, "platform": self.platform, "config": dict(self.config)}

    def checks(self, definitions, names, settings, revision=None):
        self.check_runs += 1
        if self.changed_during_check:
            self.tree = "f" * 40
        return [
            {
                "id": name,
                "exit_code": self.per_check.get(name, self.exit_code),
                "timed_out": False,
                "output_sha256": hashlib.sha256(b"verified").hexdigest(),
                "output_bytes": 8,
            }
            for name in names
        ]

    def diff(self, baseline, revision, settings, paths=None):
        if not self.diff_text:
            return ""
        text = self.diff_text
        if paths:
            text = text.replace("core.py", Path(paths[0]).name)
        return json.dumps(paths) + "\n" + text

    def descendant(self, baseline, revision, settings):
        return True

    def advance(self, number=2):
        self.revision = ("%040x" % number)
        self.tree = ("%040x" % (number + 100))


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "progress.sqlite3"
        self.evidence = FakeEvidence()
        self.ledger = progress.Ledger(self.path, self.root, evidence=self.evidence)
        self.policy = policy()
        self.spec = plan()
        self.ledger.initialize(self.spec, self.policy)
        self.evidence.advance()

    def assess(self, item="item_0", level="material", **kwargs):
        return self.ledger.assess("reliability", item, "Verified outcome", asker=picker(level), **kwargs)

    def review(self, choice="continue", **kwargs):
        return self.ledger.review(
            "reliability", "Evidence reviewed", "test-reviewer", asker=picker(choice), **kwargs
        )

    def test_default_scale_and_initial_checkpoint(self):
        state = self.ledger.status("reliability")
        self.assertEqual(self.policy["progress"]["points"], {"zero": 0, "small": 1, "material": 2, "major": 3})
        self.assertEqual(state["points"], 0)
        self.assertEqual(state["review_at"], 12)
        self.assertEqual(state["action"], "continue")

    def test_positive_grade_and_evidence_are_recorded(self):
        result = self.assess()
        self.assertEqual(result["points"], 2)
        self.assertEqual(result["assessment_count"], 1)
        event = self.ledger.history("reliability")["events"][-1]
        self.assertEqual(event["kind"], "assessment")
        self.assertEqual(event["data"]["level"], "material")
        self.assertEqual(event["data"]["revision"], self.evidence.revision)
        self.assertEqual(len(event["data"]["evidence_sha256"]), 64)
        self.assertNotIn("diff", event["data"])

    def test_same_item_tree_is_cached_without_calling_jev(self):
        ask = picker("material")
        self.ledger.assess("reliability", "item_0", "First summary", asker=ask)
        cached = self.ledger.assess("reliability", "item_0", "A more persuasive summary", asker=ask)
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["points"], 2)
        self.assertEqual(cached["assessment_count"], 1)
        self.assertEqual(ask.call_count, 1)

    def test_new_commit_same_tree_does_not_rerate_zero(self):
        ask = picker("zero")
        self.ledger.assess("reliability", "item_0", "First", asker=ask)
        self.evidence.revision = "c" * 40
        result = self.ledger.assess("reliability", "item_0", "Again", asker=ask)
        self.assertTrue(result["cached"])
        self.assertEqual(ask.call_count, 1)

    def test_splitting_commits_cannot_credit_one_item_twice(self):
        self.assess(level="small")
        self.evidence.advance(3)
        ask = picker("major")
        result = self.ledger.assess("reliability", "item_0", "Another commit", asker=ask)
        self.assertEqual(result["points"], 1)
        self.assertEqual(result["assessment_count"], 1)
        ask.assert_not_called()

    def test_unknown_item_cannot_be_used_to_farm_points(self):
        with self.assertRaises(progress.ProgressError) as cm:
            self.assess("invented_item")
        self.assertEqual(cm.exception.code, "UNKNOWN_ITEM")
        self.assertEqual(self.ledger.status("reliability")["assessment_count"], 0)

    def test_zero_and_unscored_remain_distinct(self):
        self.assess("item_0", "zero")
        self.assess("item_1", "none")
        events = self.ledger.history("reliability")["events"]
        results = [entry["data"] for entry in events if entry["kind"] == "assessment"]
        self.assertEqual([r["status"] for r in results], ["scored", "unscored"])
        self.assertEqual(results[0]["level"], "zero")
        self.assertIsNone(results[1]["level"])
        self.assertEqual(self.ledger.status("reliability")["points"], 0)

    def test_low_confidence_earns_no_points(self):
        result = self.ledger.assess("reliability", "item_0", "Verified", asker=picker("major", 0.6))
        self.assertEqual(result["points"], 0)
        event = self.ledger.history("reliability")["events"][-1]["data"]
        self.assertEqual(event["status"], "unscored")
        self.assertEqual(event["reason"], "uncertain")

    def test_ambiguous_and_nonmaximal_choices_are_not_credited(self):
        for probabilities in (
            {"zero": 0.0, "small": 0.0, "material": 0.49, "major": 0.51, "none": 0.0},
            {"zero": 0.0, "small": 0.0, "material": 0.9, "major": 0.1, "none": 0.0},
        ):
            with self.subTest(probabilities=probabilities):
                question = self.policy["templates"]["contribution"]
                parsed = {"answers": {"contribution": answer(question, "major", probabilities=probabilities)}}
                result = progress.interpret_choice(parsed, "contribution", question, self.policy)
                self.assertIsNone(result["choice"])

    def test_api_failure_is_unscored_without_persisting_exception_text(self):
        secret = "private-error-detail-should-not-appear"
        bad = Mock(side_effect=SystemExit(secret))
        result = self.ledger.assess("reliability", "item_0", "Verified", asker=bad)
        self.assertEqual(result["points"], 0)
        history = json.dumps(self.ledger.history("reliability"))
        self.assertNotIn(secret, history)
        self.assertIn("jev_unavailable", history)

    def test_unavailable_request_can_be_explicitly_retried_without_double_credit(self):
        self.ledger.assess("reliability", "item_0", "Verified", asker=Mock(side_effect=OSError("offline")))
        result = self.ledger.assess("reliability", "item_0", "Verified", retry_unavailable=True, asker=picker("material"))
        self.assertEqual(result["points"], 2)
        self.assertEqual(result["assessment_count"], 2)
        self.assertEqual(result["model_attempts"], 2)
        self.assertEqual(len(self.ledger.history("reliability")["events"]), 2)

    def test_retry_cannot_shop_for_a_better_grade(self):
        self.assess("item_0", "zero")
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.assess("reliability", "item_0", "Try for major", retry_unavailable=True, asker=picker("major"))
        self.assertEqual(cm.exception.code, "RETRY_NOT_ALLOWED")
        self.assertEqual(self.ledger.status("reliability")["assessment_count"], 1)

    def test_identical_patch_cannot_be_credited_under_another_item(self):
        self.evidence.diff = lambda *args, **kwargs: "diff --git a/x.py b/x.py\nindex 1..2 100644\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+same\n"
        self.assess("item_0", "major")
        ask = picker("major")
        result = self.ledger.assess("reliability", "item_1", "Same change, new label", asker=ask)
        self.assertEqual(result["points"], 3)
        self.assertEqual(self.ledger.history("reliability")["events"][-1]["data"]["reason"], "duplicate_change")
        ask.assert_not_called()

    def test_malformed_model_answer_is_unscored(self):
        bad = Mock(return_value={"answers": {"contribution": {"type": "score", "score": 100000}}})
        result = self.ledger.assess("reliability", "item_0", "Verified", asker=bad)
        self.assertEqual(result["points"], 0)
        self.assertEqual(self.ledger.history("reliability")["events"][-1]["data"]["status"], "unscored")

    def test_failed_verification_never_calls_jev_or_awards_points(self):
        self.evidence.exit_code = 1
        ask = picker("major")
        result = self.ledger.assess("reliability", "item_0", "It is excellent", asker=ask)
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")
        ask.assert_not_called()

    def test_changed_tree_during_verification_cannot_be_credited(self):
        self.evidence.changed_during_check = True
        ask = picker("major")
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.assess("reliability", "item_0", "Verified", asker=ask)
        self.assertEqual(cm.exception.code, "WORKTREE_CHANGED")
        self.assertEqual(self.ledger.status("reliability")["points"], 0)
        ask.assert_not_called()

    def test_empty_diff_is_mechanical_zero(self):
        self.evidence.diff_text = ""
        ask = picker("major")
        result = self.ledger.assess("reliability", "item_0", "A big improvement", asker=ask)
        self.assertEqual(result["points"], 0)
        self.assertEqual(self.ledger.history("reliability")["events"][-1]["data"]["level"], "zero")
        ask.assert_not_called()

    def test_large_evidence_is_unscored_not_silently_truncated(self):
        self.evidence.diff_text = "diff --git a/big.py b/big.py\nnew file mode 100644\n" + "x" * self.policy["progress"]["max_evidence_chars"]
        ask = picker("major")
        self.ledger.assess("reliability", "item_0", "Verified", asker=ask)
        event = self.ledger.history("reliability")["events"][-1]["data"]
        self.assertEqual(event["reason"], "evidence_too_large")
        self.assertEqual(event["status"], "unscored")
        ask.assert_not_called()

    def test_checkpoint_requires_review_and_never_finishes(self):
        for n in range(4):
            result = self.assess("item_%d" % n, "major")
        self.assertEqual(result["points"], 12)
        self.assertEqual(result["action"], "review_required")
        self.assertEqual(result["reason"], "checkpoint")
        with self.assertRaises(progress.ProgressError) as cm:
            self.assess("item_4")
        self.assertEqual(cm.exception.code, "REVIEW_REQUIRED")
        continued = self.review()
        self.assertEqual(continued["review_at"], 24)
        self.assertEqual(continued["assessment_count"], 4)
        self.assertEqual(continued["action"], "continue")

    def test_stall_triggers_review_but_keeps_unscored_distinct(self):
        for n in range(3):
            result = self.assess("item_%d" % n, "zero")
        self.assertEqual(result["action"], "review_required")
        self.assertEqual(result["reason"], "stalled")
        self.review()
        result = self.assess("item_3", "material")
        self.assertEqual(result["action"], "continue")

    def test_budget_survives_continue_reviews(self):
        small_policy = policy()
        small_policy["progress"]["max_assessments"] = 2
        spec = plan()
        spec["id"] = "bounded"
        self.ledger.initialize(spec, small_policy)
        self.evidence.advance(5)
        self.ledger.assess("bounded", "item_0", "Verified", asker=picker("small"))
        last = self.ledger.assess("bounded", "item_1", "Verified", asker=picker("small"))
        self.assertEqual(last["action"], "budget_exhausted")
        continued = self.ledger.review("bounded", "Review", "reviewer", asker=picker("continue"))
        self.assertEqual(continued["action"], "budget_exhausted")
        with self.assertRaises(progress.ProgressError):
            self.ledger.assess("bounded", "item_2", "More", asker=picker("major"))

    def test_human_finish_review_survives_exhausted_model_budget(self):
        tiny = policy()
        tiny["progress"]["max_model_calls"] = 1
        spec = plan(1)
        spec["id"] = "capped"
        self.ledger.initialize(spec, tiny)
        self.evidence.advance(5)
        self.ledger.assess("capped", "item_0", "Verified", asker=picker("small"))
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.review("capped", "Continue", "reviewer", asker=picker("continue"))
        self.assertEqual(cm.exception.code, "BUDGET_EXHAUSTED")
        finished = self.ledger.review("capped", "Human approved finish", "reviewer", approve_finish=True, asker=picker("finish"))
        self.assertEqual(finished["action"], "finished")

    def test_revocation_and_restoration_preserve_history_without_extra_credit(self):
        self.assess(level="major")
        invalid = self.ledger.invalidate("reliability", "item_0", "Outcome was reverted")
        self.assertEqual(invalid["points"], 0)
        self.assertEqual(invalid["action"], "repair_required")
        self.evidence.advance(4)
        ask = picker("major")
        with self.assertRaises(progress.ProgressError):
            self.ledger.assess("reliability", "item_0", "Fixed our own regression", asker=ask)
        ask.assert_not_called()
        restored = self.ledger.restore("reliability", "item_0", "Original result restored", "reviewer")
        self.assertEqual(restored["points"], 3)
        self.assertEqual(restored["assessment_count"], 1)
        kinds = [e["kind"] for e in self.ledger.history("reliability")["events"]]
        self.assertEqual(kinds, ["assessment", "invalidate", "restore"])

    def test_failed_restoration_cannot_clear_revocation(self):
        self.assess()
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        self.evidence.exit_code = 1
        with self.assertRaises(progress.ProgressError):
            self.ledger.restore("reliability", "item_0", "Claimed repair", "reviewer")
        self.assertEqual(self.ledger.status("reliability")["points"], 0)

    def test_finish_before_checkpoint_requires_fresh_checks_and_approval(self):
        for n in range(len(self.spec["items"])):
            self.assess("item_%d" % n, level="small")
        result = self.review("finish")
        self.assertNotEqual(result["action"], "finished")
        finished = self.review("finish", approve_finish=True)
        self.assertEqual(finished["action"], "finished")
        self.assertEqual(finished["points"], 6)
        with self.assertRaises(progress.ProgressError):
            self.assess()

    def test_finish_rejects_items_that_were_never_assessed(self):
        for n in range(len(self.spec["items"]) - 1):
            self.assess("item_%d" % n, level="small")
        result = self.review("finish", approve_finish=True)
        self.assertNotEqual(result["action"], "finished")
        self.assertEqual(result["reason"], "items_not_assessed")

    def test_model_cannot_finish_over_failed_checks(self):
        self.evidence.exit_code = 1
        result = self.review("finish", approve_finish=True)
        self.assertNotEqual(result["action"], "finished")
        self.assertEqual(result["action"], "repair_required")

    def test_revoked_item_blocks_finish_even_when_checks_pass(self):
        self.assess()
        self.ledger.invalidate("reliability", "item_0", "Independent review found a regression")
        result = self.review("finish", approve_finish=True)
        self.assertEqual(result["action"], "repair_required")

    def test_only_preapproved_direction_can_be_selected(self):
        pivot = self.review("pivot_usability")
        self.assertEqual(pivot["action"], "pivoted")
        self.assertEqual(pivot["next_direction"], "usability")

    def test_pivot_can_leave_unmet_items_but_not_failed_required_checks(self):
        spec = plan(1)
        spec["id"] = "pivotable"
        spec["checks"]["feature"] = ["{python}", "-c", "print('feature')"]
        spec["items"][0]["checks"] = ["feature"]
        self.ledger.initialize(spec, self.policy)
        self.evidence.per_check["feature"] = 1
        result = self.ledger.review("pivotable", "Authorized direction change", "reviewer", asker=picker("pivot_usability"))
        self.assertEqual(result["action"], "pivoted")
        self.assertNotEqual(result["action"], "finished")
        self.assertIn("feature", result["failed_checks"])

    def test_invented_direction_cannot_be_selected(self):
        result = self.review("pivot_unapproved")
        self.assertNotEqual(result["action"], "pivoted")

    def test_policy_snapshot_is_frozen(self):
        self.policy["progress"]["points"]["major"] = 100
        self.policy["progress"]["review_points"] = 1
        result = self.assess(level="major")
        self.assertEqual(result["points"], 3)
        self.assertEqual(result["review_at"], 12)
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.initialize(self.spec, self.policy)
        self.assertEqual(cm.exception.code, "STAGE_EXISTS")

    def test_concurrent_same_outcome_gets_one_award(self):
        def run(_):
            ledger = progress.Ledger(self.path, self.root, evidence=self.evidence)
            return ledger.assess("reliability", "item_0", "Verified", asker=picker("major"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(4)))
        self.assertEqual([r["points"] for r in results], [3, 3, 3, 3])
        self.assertEqual(self.ledger.status("reliability")["assessment_count"], 1)

    def test_missing_stage_and_corrupt_store_fail_closed(self):
        with self.assertRaises(progress.ProgressError):
            self.ledger.status("missing")
        broken = self.root / "broken.sqlite3"
        broken.write_bytes(b"not a database")
        with self.assertRaises(progress.ProgressError):
            progress.Ledger(broken, self.root).status("reliability")
        self.assertEqual(broken.read_bytes(), b"not a database")

    def test_future_schema_is_not_modified(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA user_version = 999")
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "SCHEMA_VERSION")

    def test_cached_award_is_invalidated_after_full_revert(self):
        self.assess(level="major")
        self.evidence.advance(7)
        self.evidence.diff_text = ""
        ask = picker("major")
        result = self.ledger.assess("reliability", "item_0", "Entire scoped change reverted", asker=ask)
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")
        self.assertEqual(result["assessment_count"], 1)
        ask.assert_not_called()

    def test_review_detects_full_revert_before_finishing(self):
        self.assess(level="major")
        self.evidence.advance(7)
        self.evidence.diff_text = ""
        result = self.review("finish", approve_finish=True)
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")
        self.assertIn("item_0", result["blocked_items"])

    def test_partial_revert_revokes_credit_without_an_empty_scope(self):
        self.assess(level="major")
        self.evidence.advance(7)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n+unrelated edit\n"
        ask = picker("major")
        result = self.ledger.assess("reliability", "item_0", "Scoped edit that dropped the credited change", asker=ask)
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")
        self.assertIn("item_0", result["blocked_items"])
        ask.assert_not_called()
        self.assertEqual(
            self.ledger.history("reliability")["events"][-1]["data"]["reason"], "credited_change_reverted"
        )

    def test_review_detects_partial_revert_before_finishing(self):
        self.assess(level="major")
        self.evidence.advance(7)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n+unrelated edit\n"
        result = self.review("finish", approve_finish=True)
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")
        self.assertIn("item_0", result["blocked_items"])

    def test_kept_credit_plus_new_work_still_counts(self):
        self.assess(level="major")
        self.evidence.advance(7)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,2 +1,2 @@\n-old behavior\n+verified behavior\n+unrelated edit\n"
        result = self.ledger.assess("reliability", "item_0", "Credited change retained plus more", asker=picker("major"))
        self.assertEqual(result["points"], 3)
        self.assertNotIn("item_0", result["blocked_items"])

    def test_restore_requires_the_credited_change_not_any_change(self):
        self.assess(level="major")
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n+different edit\n"
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.restore("reliability", "item_0", "Different content is not the credited outcome", "reviewer")
        self.assertEqual(cm.exception.code, "RESTORE_MISMATCH")
        self.assertEqual(self.ledger.status("reliability")["points"], 0)

    def _insert_event(self, kind, event):
        with closing(sqlite3.connect(self.path)) as db:
            previous = db.execute(
                "SELECT seal FROM events WHERE stage_id = 'reliability' ORDER BY sequence DESC LIMIT 1"
            ).fetchone()[0]
            sequence = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events").fetchone()[0]
            seal = progress.fingerprint(
                {"previous": previous, "stage_id": "reliability", "kind": kind, "document": event,
                 "sequence": sequence, "dedupe": None}
            )
            db.execute(
                "INSERT INTO events(sequence, stage_id, kind, document, dedupe_key, seal) VALUES (?, ?, ?, ?, NULL, ?)",
                (sequence, "reliability", kind, json.dumps(event), seal),
            )
            head = db.execute("SELECT seal FROM heads WHERE stage_id = 'reliability'").fetchone()[0]
            db.execute(
                "UPDATE heads SET seal = ? WHERE stage_id = 'reliability'",
                (progress.fingerprint({"stage": "reliability", "previous": head, "seal": seal}),),
            )
            db.commit()

    def _review_document(self):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT document FROM events WHERE kind = 'review' ORDER BY sequence DESC LIMIT 1").fetchone()
        return json.loads(row[0])

    def test_cached_award_return_detects_concurrent_change(self):
        self.assess(level="major")
        self.evidence.advance(7)
        original_diff = self.evidence.diff

        def drift(*args, **kwargs):
            text = original_diff(*args, **kwargs)
            self.evidence.tree = "9" * 40
            return text

        self.evidence.diff = drift
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.assess("reliability", "item_0", "Tree moved during retained-credit check", asker=picker("major"))
        self.assertEqual(cm.exception.code, "WORKTREE_CHANGED")

    def test_inserted_applied_finish_cannot_close_the_stage(self):
        self.review("continue")
        event = self._review_document()
        event["choice"] = "finish"
        event["applied"] = True
        self._insert_event("review", event)
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_inserted_finish_with_unmet_items_is_rejected(self):
        self.review("continue")
        event = self._review_document()
        event["choice"] = "finish"
        event["applied"] = True
        event["approve_finish"] = True
        event["item_acceptance"]["item_0"]["status"] = "unmet"
        self._insert_event("review", event)
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_inserted_invalidate_without_an_award_is_rejected(self):
        self.assess()
        self._insert_event("invalidate", {"item_id": "item_1", "reason": "forged", "recorded_at": time.time()})
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_inserted_restore_without_invalidation_is_rejected(self):
        self.assess(level="major")
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        self.ledger.restore("reliability", "item_0", "Original outcome returned", "reviewer")
        document = None
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT document FROM events WHERE kind = 'restore'").fetchone()
            document = json.loads(row[0])
        self._insert_event("restore", document)
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_overlapping_scope_lines_cannot_be_credited_twice(self):
        self.evidence.diff_text = "diff --git a/shared.py b/shared.py\n--- a/shared.py\n+++ b/shared.py\n@@ -1 +1 @@\n-old\n+shared fix\n"
        self.assess("item_0", "major")
        ask = picker("major")
        result = self.ledger.assess("reliability", "item_1", "Same lines inside another scope", asker=ask)
        self.assertEqual(result["points"], 3)
        self.assertEqual(self.ledger.history("reliability")["events"][-1]["data"]["reason"], "already_credited")
        ask.assert_not_called()

    def test_deleted_tail_event_breaks_the_chain_anchor(self):
        self.assess()
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER events_no_delete")
            db.execute("DELETE FROM events WHERE sequence = (SELECT MAX(sequence) FROM events)")
            db.commit()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_invalidated_credit_can_be_re_earned_by_another_item(self):
        self.evidence.diff_text = "diff --git a/shared.py b/shared.py\n--- a/shared.py\n+++ b/shared.py\n@@ -1 +1 @@\n-old\n+shared fix\n"
        self.assess("item_0", "major")
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        self.evidence.advance(4)
        result = self.ledger.assess("reliability", "item_1", "Re-added the reverted lines", asker=picker("small"))
        self.assertEqual(result["points"], 1)
        self.assertEqual(result["awarded_items"], ["item_1"])

    def test_binary_change_credit_does_not_survive_replacement(self):
        self.evidence.diff_text = "diff --git a/blob.bin b/blob.bin\nindex aaa111..bbb222 100644\nBinary files a/blob.bin and b/blob.bin differ\n"
        self.assess(level="major")
        self.evidence.advance(7)
        self.evidence.diff_text = "diff --git a/blob.bin b/blob.bin\nindex ccc333..ddd444 100644\nBinary files a/blob.bin and b/blob.bin differ\n"
        result = self.ledger.assess("reliability", "item_0", "Binary replaced", asker=picker("major"))
        self.assertEqual(result["points"], 0)
        self.assertIn("item_0", result["blocked_items"])

    def test_restore_evidence_is_replayable(self):
        self.assess(level="major")
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        self.ledger.restore("reliability", "item_0", "Original outcome returned", "reviewer")
        event = self.ledger.history("reliability")["events"][-1]
        self.assertEqual(event["kind"], "restore")
        replay = self.ledger.evidence("reliability", event["sequence"])
        self.assertEqual(replay["sha256"], event["data"]["evidence_sha256"])
        self.assertEqual(replay["state"]["credit"], event["data"]["evidence_replay"]["metadata"]["credit"])

    def test_edited_event_kind_breaks_the_seal_chain(self):
        self.assess()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET kind = 'restore' WHERE kind = 'assessment'")
            db.commit()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_reordered_history_breaks_the_seal_chain(self):
        self.assess()
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET sequence = 0 WHERE kind = 'invalidate'")
            db.commit()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_edited_document_breaks_the_event_seal(self):
        self.assess()
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT sequence, document FROM events WHERE kind = 'assessment'").fetchone()
            event = json.loads(row[1])
            event["reason"] = "rewritten history"
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET document = ? WHERE sequence = ?", (json.dumps(event), row[0]))
            db.commit()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_malformed_check_records_fail_closed(self):
        self.evidence.checks = lambda definitions, names, settings, revision=None: [{"id": name, "exit_code": 0, "timed_out": False} for name in names]
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.assess("reliability", "item_0", "Verified", asker=picker("major"))
        self.assertEqual(cm.exception.code, "INVALID_EVIDENCE")
        self.assertEqual(self.ledger.status("reliability")["assessment_count"], 0)

    def test_replay_is_bound_to_the_event_top_level_fields(self):
        self.assess()
        event = self.ledger.history("reliability")["events"][-1]
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("DROP TRIGGER events_no_update")
            data = event["data"]
            data["revision"] = "b" * 40
            db.execute("UPDATE events SET document = ? WHERE sequence = ?", (json.dumps(data), event["sequence"]))
            db.commit()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.evidence("reliability", event["sequence"])
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_restore_cannot_credit_a_still_reverted_scope(self):
        self.assess(level="major")
        self.ledger.invalidate("reliability", "item_0", "Reverted")
        self.evidence.diff_text = ""
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.restore("reliability", "item_0", "Checks alone pass", "reviewer")
        self.assertEqual(cm.exception.code, "NO_CURRENT_CHANGE")
        self.assertEqual(self.ledger.status("reliability")["points"], 0)

    def test_model_attempt_budget_also_bounds_reviews(self):
        limited = policy()
        limited["progress"]["max_model_calls"] = 1
        spec = plan()
        spec["id"] = "model_bound"
        self.ledger.initialize(spec, limited)
        self.evidence.advance(6)
        result = self.ledger.assess("model_bound", "item_0", "Verified", asker=picker("small"))
        self.assertEqual(result["model_attempts"], 1)
        self.assertEqual(result["action"], "budget_exhausted")
        ask = picker("continue")
        with self.assertRaises(progress.ProgressError):
            self.ledger.review("model_bound", "Try another request", "reviewer", asker=ask)
        ask.assert_not_called()

    def test_platform_requirement_is_checked_before_commands(self):
        spec = plan()
        spec["id"] = "windows_only"
        spec["platform"] = "win32"
        self.evidence.platform = "linux"
        count = self.evidence.check_runs
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.initialize(spec, self.policy)
        self.assertEqual(cm.exception.code, "PLATFORM_MISMATCH")
        self.assertEqual(self.evidence.check_runs, count)

    def test_each_attempt_has_its_own_id_and_a_stable_assessment_key(self):
        failed = self.ledger.assess("reliability", "item_0", "Verified", asker=Mock(side_effect=OSError("offline")))
        success = self.ledger.assess("reliability", "item_0", "Verified", retry_unavailable=True, asker=picker("material"))
        rows = [event["data"] for event in self.ledger.history("reliability")["events"]]
        self.assertNotEqual(failed["assessment_id"], success["assessment_id"])
        self.assertEqual(rows[0]["assessment_key"], rows[1]["assessment_key"])
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertEqual(len({row["id"] for row in rows}), 2)

    def test_unmet_or_unknown_item_blocks_finish_independently_of_points(self):
        for verdict in ("unmet", "none"):
            with self.subTest(verdict=verdict):
                ask = picker("finish", acceptance={"item_0": verdict})
                result = self.ledger.review("reliability", "Acceptance review", "reviewer", approve_finish=True, asker=ask)
                self.assertNotEqual(result["action"], "finished")
                self.assertEqual(result["reason"], "item_acceptance_required")
                last = self.ledger.history("reliability")["events"][-1]["data"]
                self.assertEqual(last["item_acceptance"]["item_0"]["status"], "unmet" if verdict == "unmet" else "unknown")

    def test_review_requires_all_item_answers_in_one_model_call(self):
        for n in range(len(self.spec["items"])):
            self.assess("item_%d" % n, level="small")
        def incomplete(state, questions, snapshot):
            return {"answers": {"progress_review": answer(questions["progress_review"], "finish")}}
        result = self.ledger.review("reliability", "Review", "reviewer", approve_finish=True, asker=Mock(side_effect=incomplete))
        self.assertNotEqual(result["action"], "finished")
        self.assertEqual(result["model_attempts"], 7)
        request = picker("finish")
        result = self.ledger.review("reliability", "Complete evidence", "reviewer", approve_finish=True, asker=request)
        self.assertEqual(result["action"], "finished")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(request.call_args.args[1]), len(self.spec["items"]) + 1)

    def test_evidence_replay_reconstructs_exact_input_without_repeating_checks(self):
        ask = picker("material")
        self.ledger.assess("reliability", "item_0", "Exact original summary", asker=ask)
        event = self.ledger.history("reliability")["events"][-1]
        runs = self.evidence.check_runs
        replay = self.ledger.evidence("reliability", event["sequence"])
        self.assertEqual(replay["state"], ask.call_args.args[0])
        self.assertEqual(replay["sha256"], event["data"]["evidence_sha256"])
        self.assertEqual(progress.fingerprint(replay["state"]), replay["sha256"])
        self.assertEqual(replay["questions"], ask.call_args.args[1])
        self.assertEqual(self.evidence.check_runs, runs)
        metadata = event["data"]["evidence_replay"]["metadata"]
        self.assertEqual(metadata["coder_summary"], "Exact original summary")
        self.assertNotIn("diff", metadata["candidate"])

    def test_evidence_replay_detects_drift_instead_of_inventing_input(self):
        self.assess()
        event = self.ledger.history("reliability")["events"][-1]
        self.evidence.diff_text = "different serialized diff"
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.evidence("reliability", event["sequence"])
        self.assertEqual(cm.exception.code, "EVIDENCE_MISMATCH")

    def test_sensitive_policy_is_rejected(self):
        bad = policy()
        bad["templates"]["contribution"]["instructions"] = "Send ghp_" + "a" * 40
        spec = plan(1)
        spec["id"] = "leaky"
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.initialize(spec, bad)
        self.assertEqual(cm.exception.code, "INVALID_POLICY")

    def test_sensitive_evidence_metadata_is_withheld(self):
        sensitive = "ghp_" + "a" * 40
        self.evidence.diff_text = sensitive
        ask = picker("major")
        self.ledger.assess("reliability", "item_0", "Verify", asker=ask)
        event = self.ledger.history("reliability")["events"][-1]
        self.assertIsNone(event["data"]["evidence_replay"])
        self.assertNotIn(sensitive, json.dumps(event))
        ask.assert_not_called()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.evidence("reliability", event["sequence"])
        self.assertEqual(cm.exception.code, "EVIDENCE_UNAVAILABLE")

    def test_database_rollback_to_a_valid_earlier_copy_is_rejected(self):
        self.assess(level="major")
        backup = self.path.read_bytes()
        self.ledger.invalidate("reliability", "item_0", "Revoked by reviewer")
        self.path.write_bytes(backup)
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_missing_chain_anchor_is_rejected(self):
        self.assess()
        self.ledger._anchor_path().unlink()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_stage_baseline_cannot_be_swapped_for_an_ancestor(self):
        self.assess()
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT document FROM stages WHERE id = 'reliability'").fetchone()
            stage = json.loads(row[0])
            stage["baseline"]["revision"] = "0" * 40
            db.execute("DROP TRIGGER stages_no_update")
            db.execute("UPDATE stages SET document = ? WHERE id = 'reliability'", (json.dumps(stage),))
            db.commit()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")

    def test_credited_lines_reproduced_at_another_position_are_reverted(self):
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,2 +1,2 @@\n-foo\n+bar\n foo\n"
        self.assess(level="major")
        self.evidence.advance(3)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,2 +1,2 @@\n foo\n-foo\n+bar\n"
        result = self.ledger.assess("reliability", "item_0", "Same lines at another position", asker=picker("major"))
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")

    def test_identical_multiline_file_addition_is_credited_once(self):
        block = "".join("+shared line %d\n" % index for index in range(4))
        self.evidence.diff_text = "diff --git a/core.py b/core.py\nnew file mode 100644\n--- /dev/null\n+++ b/core.py\n@@ -0,0 +1,4 @@\n" + block
        self.assess(item="item_0", level="major")
        self.evidence.advance(3)
        self.ledger.assess("reliability", "item_1", "Copied file under another item", asker=picker("major"))
        event = self.ledger.history("reliability")["events"][-1]
        self.assertEqual(event["data"]["credit"]["added"], [])
        self.assertTrue(event["data"]["credit"]["ops"])

    def test_submodule_pointer_change_is_trackable(self):
        self.evidence.diff_text = (
            "diff --git a/vendor/lib b/vendor/lib\nindex 0123456789abcdef0123456789abcdef01234567..89abcdef0123456789abcdef0123456789abcd 160000\n"
            "Submodule vendor/lib 0123456..89abcde\n"
        )
        ask = picker("small")
        result = self.ledger.assess("reliability", "item_0", "Submodule bump", asker=ask)
        self.assertEqual(result["points"], 1)
        event = self.ledger.history("reliability")["events"][-1]
        self.assertTrue(event["data"]["credit"]["ops"])

    def test_git_config_drift_is_rejected(self):
        self.assess()
        self.evidence.config = {"core.autocrlf": "true"}
        self.evidence.advance(3)
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.assess("reliability", "item_0", "Drifted config", asker=picker("major"))
        self.assertEqual(cm.exception.code, "CONFIG_DRIFT")

    def test_relocation_into_different_context_revokes_credit(self):
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,3 +1,3 @@\n alpha\n-work\n+patched\n omega\n"
        self.assess(level="major")
        self.evidence.advance(3)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,3 +1,3 @@\n gamma\n-work\n+patched\n delta\n"
        result = self.ledger.assess("reliability", "item_0", "Relocated hunk", asker=picker("major"))
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")

    def test_unicode_separators_do_not_split_diff_lines(self):
        parsed = progress._diff_line_hashes(
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+al omega\n"
        )
        other = progress._diff_line_hashes(
            "diff --git a/f.py b/f.py\n--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n+al\n"
        )
        self.assertEqual(len(parsed["added"]), 1)
        self.assertNotEqual(parsed["added"], other["added"])

    def test_over_budget_finish_bypass_is_single_use(self):
        candidate = policy()
        candidate["progress"]["max_model_calls"] = 2
        spec = plan(1)
        spec["id"] = "tight_budget"
        self.ledger.initialize(spec, candidate)
        self.ledger.review("tight_budget", "Look", "reviewer", asker=picker("continue"))
        self.ledger.review("tight_budget", "Again", "reviewer", asker=picker("continue"))
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.review("tight_budget", "No approval", "reviewer", asker=picker("continue"))
        self.assertEqual(cm.exception.code, "BUDGET_EXHAUSTED")
        result = self.ledger.review("tight_budget", "Human approved", "reviewer", approve_finish=True, asker=picker("continue"))
        self.assertTrue(result["action"] != "finished")
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.review("tight_budget", "Second bypass", "reviewer", approve_finish=True, asker=picker("continue"))
        self.assertEqual(cm.exception.code, "BUDGET_EXHAUSTED")

    def test_corrupt_event_cannot_silently_change_point_totals(self):
        self.assess(level="major")
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT sequence, document FROM events WHERE kind = 'assessment'").fetchone()
            event = json.loads(row[1])
            event["points"] = True
            db.execute("DROP TRIGGER events_no_update")
            db.execute("UPDATE events SET document = ? WHERE sequence = ?", (json.dumps(event), row[0]))
            db.commit()
        with self.assertRaises(progress.ProgressError) as cm:
            self.ledger.status("reliability")
        self.assertEqual(cm.exception.code, "STORE_INVALID")


class ProgressPolicyTests(unittest.TestCase):
    def test_real_progress_policy_is_valid(self):
        self.assertEqual(progress.lint_progress(policy()), [])

    def test_invalid_thresholds_and_points_are_rejected(self):
        for key, value in (
            ("review_points", 0), ("max_assessments", True), ("stall_limit", -1),
            ("confidence_floor", float("nan")), ("choice_gap", 1.1),
            ("command_timeout_seconds", float("inf")), ("max_evidence_chars", 0),
        ):
            with self.subTest(key=key):
                candidate = policy()
                candidate["progress"][key] = value
                self.assertTrue(progress.lint_progress(candidate))
        for value in (True, -1, 2.5):
            candidate = policy()
            candidate["progress"]["points"]["major"] = value
            self.assertTrue(progress.lint_progress(candidate))

    def test_category_map_must_match_template_and_keep_zero_distinct(self):
        for mutate in (
            lambda p: p["progress"]["points"].pop("material"),
            lambda p: p["progress"]["points"].update(none=0),
            lambda p: p["progress"]["points"].update(zero=1),
            lambda p: p["templates"]["contribution"]["criteria"].pop("none"),
            lambda p: p["progress"].update(review_pionts=100),
        ):
            candidate = policy()
            mutate(candidate)
            self.assertTrue(progress.lint_progress(candidate))

    def test_scale_and_checkpoint_can_change_for_a_new_stage(self):
        candidate = policy()
        candidate["progress"]["points"]["critical"] = 4
        candidate["templates"]["contribution"]["criteria"]["critical"] = "A verified critical agreed outcome"
        candidate["progress"]["review_points"] = 100
        self.assertEqual(progress.lint_progress(candidate), [])

    def test_invalid_plan_rejected_before_commands_run(self):
        for change in (
            lambda p: p.update(items=[]),
            lambda p: p["items"].append(copy.deepcopy(p["items"][0])),
            lambda p: p.update(required_checks=["missing"]),
            lambda p: p["checks"].update(suite="python -m unittest"),
            lambda p: p["items"][0].update(checks=[]),
            lambda p: p["items"][0].update(paths=["../outside"]),
            lambda p: p["items"][0].update(paths=[":(exclude)secret"]),
            lambda p: p["items"][0].update(paths=[]),
        ):
            with self.subTest(change=change):
                candidate = plan()
                change(candidate)
                with self.assertRaises(progress.ProgressError):
                    progress.validate_plan(candidate, policy())

    def test_progress_endpoint_cannot_redirect_credentials_to_another_service(self):
        for endpoint in ("http://api.typesafe.ai/v1/systemone", "https://example.invalid/v1/systemone", "https://api.typesafe.ai.evil.invalid/v1/systemone"):
            candidate = policy()
            candidate["endpoint"] = endpoint
            self.assertTrue(progress.lint_progress(candidate))

    def test_whitespace_in_identifiers_is_rejected(self):
        for location in ("stage", "item", "check", "direction"):
            with self.subTest(location=location):
                candidate = plan()
                if location == "stage":
                    candidate["id"] = " padded "
                elif location == "item":
                    candidate["items"][0]["id"] = " item_0 "
                elif location == "check":
                    candidate["checks"][" padded "] = candidate["checks"].pop("suite")
                    candidate["required_checks"] = [" padded "]
                    for item in candidate["items"]:
                        item["checks"] = [" padded "]
                else:
                    candidate["directions"] = {" padded ": "Next goal"}
                with self.assertRaises(progress.ProgressError):
                    progress.validate_plan(candidate, policy())

    def test_json_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            for text in ('{"id":"a","id":"b"}', '{"value":NaN}'):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(progress.ProgressError):
                    progress.read_json(path)

    def test_read_json_stdin(self):
        with patch("sys.stdin", StringIO('{"a": 1}')):
            self.assertEqual(progress.read_json(Path("-")), {"a": 1})
        with patch("sys.stdin", StringIO("{bad")):
            with self.assertRaises(progress.ProgressError) as ctx:
                progress.read_json(Path("-"))
        self.assertEqual(ctx.exception.code, "INVALID_JSON")


class GitEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, GIT_AUTHOR_NAME="Progress Test", GIT_AUTHOR_EMAIL="progress@example.invalid", GIT_COMMITTER_NAME="Progress Test", GIT_COMMITTER_EMAIL="progress@example.invalid")
        self.git("init", "--quiet")
        (self.root / "check.py").write_text("print('baseline')\n", encoding="utf-8")
        self.git("add", "check.py")
        self.git("commit", "--quiet", "-m", "baseline")
        self.db = self.root / ".devin" / "progress.sqlite3"
        self.collector = progress.GitEvidence(self.root, self.db)
        self.settings = policy()["progress"]

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], env=self.env, check=True, capture_output=True, text=True).stdout.strip()

    def test_real_snapshot_and_command_output_fingerprint(self):
        snapshot = self.collector.snapshot(self.settings)
        self.assertEqual(snapshot["revision"], self.git("rev-parse", "HEAD"))
        self.assertEqual(snapshot["tree"], self.git("rev-parse", "HEAD^{tree}"))
        checks = self.collector.checks({"unit": ["{python}", "check.py"]}, ["unit"], self.settings, self.git("rev-parse", "HEAD"))
        self.assertEqual(checks[0]["exit_code"], 0)
        self.assertEqual(len(checks[0]["output_sha256"]), 64)
        self.assertNotIn("output", checks[0])

    def test_uncommitted_and_untracked_source_are_rejected(self):
        (self.root / "untracked.py").write_text("pass\n", encoding="utf-8")
        with self.assertRaises(progress.ProgressError) as cm:
            self.collector.snapshot(self.settings)
        self.assertEqual(cm.exception.code, "WORKTREE_DIRTY")

    def test_runtime_database_does_not_make_tree_dirty(self):
        self.db.parent.mkdir()
        self.db.write_bytes(b"runtime state")
        self.assertEqual(self.collector.snapshot(self.settings)["revision"], self.git("rev-parse", "HEAD"))

    def test_command_timeout_is_failed_evidence(self):
        settings = dict(self.settings, command_timeout_seconds=0.1)
        checks = self.collector.checks({"slow": ["{python}", "-c", "import time; time.sleep(5)"]}, ["slow"], settings, self.git("rev-parse", "HEAD"))
        self.assertTrue(checks[0]["timed_out"])
        self.assertNotEqual(checks[0]["exit_code"], 0)

    def test_checks_cannot_read_process_secrets(self):
        key = "test-only-secret-value"
        os.environ["TYPESAFE_API_KEY"] = key
        self.addCleanup(os.environ.pop, "TYPESAFE_API_KEY")
        checks = self.collector.checks(
            {"unit": ["{python}", "-c", "import os, sys; sys.exit(0 if os.environ.get('TYPESAFE_API_KEY') is None else 1)"]},
            ["unit"], self.settings, self.git("rev-parse", "HEAD"),
        )
        self.assertEqual(checks[0]["exit_code"], 0)

    def test_real_ledger_can_initialize_without_a_key(self):
        ledger = progress.Ledger(self.db, self.root)
        result = ledger.initialize(plan(1), policy())
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["stage_id"], "reliability")
        self.assertEqual(result["action"], "continue")

    def test_real_git_fix_verified_before_grade_and_revert_revokes_it(self):
        baseline = "def increment(n):\n    return n - 1\n"
        (self.root / "calc.py").write_text(baseline, encoding="utf-8")
        (self.root / "check.py").write_text("from calc import increment\nassert increment(3) == 4\n", encoding="utf-8")
        self.git("add", "calc.py", "check.py")
        self.git("commit", "--quiet", "-m", "reproducible baseline defect")
        spec = plan(1)
        spec["checks"]["suite"] = ["{python}", "-B", "check.py"]
        spec["items"][0]["paths"] = ["calc.py", "check.py"]
        ledger = progress.Ledger(self.db, self.root)
        ledger.initialize(spec, policy())
        (self.root / "calc.py").write_text("def increment(n):\n    return n + 1\n", encoding="utf-8")
        self.git("add", "calc.py")
        self.git("commit", "--quiet", "-m", "fix the agreed defect")
        ask = picker("material")
        result = ledger.assess("reliability", "item_0", "Fix integer increment", asker=ask)
        evidence = ask.call_args.args[0]
        self.assertEqual(evidence["baseline"]["checks"][0]["exit_code"], 1)
        self.assertEqual(evidence["candidate"]["checks"][0]["exit_code"], 0)
        self.assertEqual(evidence["candidate"]["revision"], self.git("rev-parse", "HEAD"))
        self.assertIn("+    return n + 1", evidence["candidate"]["diff"])
        self.assertEqual(result["points"], 2)
        (self.root / "calc.py").write_text(baseline, encoding="utf-8")
        self.git("add", "calc.py")
        self.git("commit", "--quiet", "-m", "revert the contribution")
        reverted = ledger.assess("reliability", "item_0", "Reverted outcome", asker=ask)
        self.assertEqual(reverted["points"], 0)
        self.assertEqual(reverted["action"], "repair_required")
        self.assertEqual(ask.call_count, 1)

    def test_real_changes_outside_item_scope_earn_no_credit(self):
        spec = plan(1)
        spec["items"][0]["paths"] = ["check.py"]
        ledger = progress.Ledger(self.db, self.root)
        ledger.initialize(spec, policy())
        (self.root / "unrelated.txt").write_text("Unrelated change\n", encoding="utf-8")
        self.git("add", "unrelated.txt")
        self.git("commit", "--quiet", "-m", "unrelated change")
        ask = picker("major")
        result = ledger.assess("reliability", "item_0", "Claimed contribution to the agreed item", asker=ask)
        self.assertEqual(result["points"], 0)
        self.assertEqual(ledger.history("reliability")["events"][-1]["data"]["reason"], "unchanged_tree")
        ask.assert_not_called()


def _watch_args(**over):
    args = SimpleNamespace(
        stage="reliability", watch=0.01, max_ticks=0, watch_max=10.0,
        quiet=False, fail_fast=False, out="", verdict="", jq="",
    )
    for key, value in over.items():
        setattr(args, key, value)
    return args


class _ResolvedLedger:
    def status(self, _stage):
        return {
            "stage_id": "reliability", "action": "finished", "reason": "finished",
            "points": 12, "review_at": 12, "assessment_count": 6,
            "model_attempts": 6, "awarded_items": ["i"], "blocked_items": [],
        }


class StatusWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = progress.Ledger(
            self.root / "progress.sqlite3", self.root, evidence=FakeEvidence()
        )
        self.ledger.initialize(plan(), policy())

    def _run(self, args):
        out, err = StringIO(), StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = progress_cli._status_watch(self.ledger, args)
        return rc, out.getvalue(), err.getvalue()

    def test_watch_emits_ticks_and_rc1_while_continue(self):
        verdict = self.root / "verdict.json"
        args = _watch_args(max_ticks=2, verdict=str(verdict))
        rc, out, err = self._run(args)
        self.assertEqual(rc, 1)  # capped while still "continue" = unresolved
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        tick = json.loads(lines[0])
        self.assertEqual(tick["action"], "continue")
        self.assertEqual(tick["stage"], "reliability")
        self.assertIn("points", tick)
        self.assertIn("watch tick=1 action=continue", err)
        data = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "active")
        self.assertEqual(data["ticks"], 2)

    def test_watch_fail_fast_on_missing_stage(self):
        args = _watch_args(stage="nope", fail_fast=True)
        rc, out, _err = self._run(args)
        self.assertEqual(rc, 1)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(json.loads(out.strip())["action"], "error")

    def test_watch_resolved_tick_exits_zero(self):
        ledger = _ResolvedLedger()
        out, err = StringIO(), StringIO()
        args = _watch_args(fail_fast=True, verdict=str(self.root / "v.json"))
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = progress_cli._status_watch(ledger, args)
        self.assertEqual(rc, 0)
        data = json.loads((self.root / "v.json").read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "resolved")
        self.assertEqual(data["action"], "finished")

    def test_watch_max_env_caps_ticks(self):
        with patch.dict(os.environ, {"JEV_PROGRESS_WATCH_MAX": "1"}):
            rc, out, _err = self._run(_watch_args())
        self.assertEqual(rc, 1)
        self.assertEqual(len(out.strip().splitlines()), 1)

    def test_watch_jq_prints_named_field(self):
        _rc, out, _err = self._run(_watch_args(max_ticks=1, jq="action"))
        self.assertEqual(json.loads(out.strip()), "continue")

    def test_watch_out_appends_ticks(self):
        target = self.root / "ticks.jsonl"
        self._run(_watch_args(max_ticks=2, out=str(target)))
        self.assertEqual(len(target.read_text(encoding="utf-8").strip().splitlines()), 2)


class HistoryWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = progress.Ledger(
            self.root / "progress.sqlite3", self.root, evidence=FakeEvidence()
        )
        self.ledger.initialize(plan(), policy())

    def _run(self, args):
        out, err = StringIO(), StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = progress_cli._history_watch(self.ledger, args)
        return rc, out.getvalue(), err.getvalue()

    def _score_one(self):
        self.ledger.assess(
            "reliability", "item_0", "Verified outcome", asker=picker("material")
        )

    def test_history_watch_counts_events(self):
        self._score_one()
        args = _watch_args(max_ticks=2, verdict=str(self.root / "v.json"))
        rc, out, err = self._run(args)
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        tick = json.loads(lines[0])
        self.assertEqual(tick["events"], 1)
        self.assertIsNone(tick["delta"])
        self.assertEqual(json.loads(lines[1])["delta"], 0)
        self.assertIn("watch tick=1 events=1", err)
        data = json.loads((self.root / "v.json").read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "steady")

    def test_history_watch_missing_stage_errors(self):
        args = _watch_args(stage="nope", max_ticks=2)
        rc, out, _err = self._run(args)
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.strip().splitlines()[0])["events"], 0)

    def test_history_watch_fail_fast_on_change(self):
        original = self.ledger.history
        calls = {"n": 0}

        def growing(stage):
            result = original(stage)
            calls["n"] += 1
            if calls["n"] > 1:
                result = dict(result)
                result["events"] = result["events"] * calls["n"]
            return result

        self._score_one()
        with patch.object(self.ledger, "history", side_effect=growing):
            rc, out, _err = self._run(_watch_args(fail_fast=True, verdict=str(self.root / "v.json")))
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)  # tick1 baseline, tick2 delta=1 -> stop
        self.assertEqual(json.loads(lines[1])["delta"], 1)
        data = json.loads((self.root / "v.json").read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "changed")

    def test_history_watch_quiet_mutes_steady_ticks(self):
        self._score_one()
        with patch.dict(os.environ, {"JEV_PROGRESS_WATCH_QUIET": "1"}):
            _rc, out, _err = self._run(_watch_args(max_ticks=2))
        self.assertEqual(out.strip(), "")  # steady ticks are quieted

    def test_history_watch_empty_exits_one(self):
        rc, out, _err = self._run(_watch_args(max_ticks=2))
        self.assertEqual(rc, 1)  # init logs no events; still empty at cap
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["events"], 0)


class ReportWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = progress.Ledger(
            self.root / "progress.sqlite3", self.root, evidence=FakeEvidence()
        )
        self.ledger.initialize(plan(), policy())

    def _run(self, args):
        out, err = StringIO(), StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = progress_cli._report_watch(self.ledger, args)
        return rc, out.getvalue(), err.getvalue()

    def test_report_watch_emits_sha_ticks(self):
        args = _watch_args(max_ticks=2, verdict=str(self.root / "v.json"))
        rc, out, err = self._run(args)
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        tick = json.loads(lines[0])
        self.assertEqual(tick["stage"], "reliability")
        self.assertGreater(tick["chars"], 0)
        self.assertIsNone(tick["delta"])
        self.assertEqual(json.loads(lines[1])["delta"], 0)
        self.assertEqual(json.loads(lines[1])["sha"], tick["sha"])
        self.assertIn("watch tick=1 chars=", err)
        data = json.loads((self.root / "v.json").read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "steady")

    def test_report_watch_missing_stage_exits_one(self):
        rc, out, _err = self._run(_watch_args(stage="nope", max_ticks=2))
        self.assertEqual(rc, 1)
        tick = json.loads(out.strip().splitlines()[0])
        self.assertEqual(tick["chars"], 0)
        self.assertIn("error", tick)

    def test_report_watch_fail_fast_on_content_change(self):
        original = self.ledger.status
        calls = {"n": 0}

        def shifting(stage):
            result = original(stage)
            calls["n"] += 1
            if calls["n"] > 1:
                result = dict(result)
                result["points"] = result["points"] + calls["n"]
            return result

        with patch.object(self.ledger, "status", side_effect=shifting):
            rc, out, _err = self._run(
                _watch_args(fail_fast=True, verdict=str(self.root / "v.json"))
            )
        self.assertEqual(rc, 0)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)  # tick1 baseline, tick2 sha changed -> stop
        self.assertEqual(json.loads(lines[1])["delta"], 1)
        data = json.loads((self.root / "v.json").read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "changed")

    def test_report_watch_quiet_mutes_steady_ticks(self):
        with patch.dict(os.environ, {"JEV_PROGRESS_WATCH_QUIET": "1"}):
            _rc, out, _err = self._run(_watch_args(max_ticks=2))
        self.assertEqual(out.strip(), "")

    def test_report_watch_jq_prints_named_field(self):
        _rc, out, _err = self._run(_watch_args(max_ticks=1, jq="chars"))
        self.assertGreater(json.loads(out.strip()), 0)



class HistoryKeysTests(unittest.TestCase):
    """`progress.py history --jsonl --keys a,b` projects event rows."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "progress.sqlite3"
        ledger = progress.Ledger(self.db, self.root, evidence=FakeEvidence())
        ledger.initialize(plan(), policy())
        ledger.assess(
            "reliability", "item_0", "Verified outcome", asker=picker("material")
        )

    def _main(self, args):
        out, err = StringIO(), StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            rc = progress_cli.main(
                ["--repo", str(self.root), "--db", str(self.db)] + args
            )
        return rc, out.getvalue(), err.getvalue()

    def test_history_jsonl_keys_projects_rows(self):
        rc, out, _err = self._main(
            ["history", "reliability", "--jsonl", "--keys", "kind,seq"]
        )
        self.assertEqual(rc, 0)
        rows = [json.loads(l) for l in out.splitlines() if l.strip()]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row) - {"kind", "seq"}, set())

    def test_history_jsonl_keys_empty_list_rc2(self):
        rc, _out, err = self._main(
            ["history", "reliability", "--jsonl", "--keys", " ,"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("--keys names no fields", err)

    def test_history_csv_and_md_emit_event_tables(self):
        import csv as _csv

        rc, out, _err = self._main(["history", "reliability", "--csv"])
        self.assertEqual(rc, 0)
        rows = list(_csv.reader(StringIO(out)))
        self.assertEqual(rows[0], ["sequence", "kind", "data", "seal"])
        self.assertGreater(len(rows), 1)
        rc, out, _err = self._main(
            ["history", "reliability", "--csv", "--keys", "kind,sequence"]
        )
        self.assertEqual(rc, 0)
        rows = list(_csv.reader(StringIO(out)))
        self.assertEqual(rows[0], ["kind", "sequence"])
        self.assertTrue(all(len(r) == 2 for r in rows[1:]))
        rc, out, _err = self._main(["history", "reliability", "--md"])
        self.assertEqual(rc, 0)
        self.assertIn("| sequence | kind | data | seal |", out)
        rc, _out, err = self._main(
            ["history", "reliability", "--csv", "--keys", " ,"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("--keys names no fields", err)


class StdinPlanTests(unittest.TestCase):
    """`progress.py lint -` / `init -` read the plan JSON from stdin."""

    PLAN = (SCRIPTS.parent / "examples" / "progress-plan.json").read_text(encoding="utf-8")

    def _lint(self, stdin_text: str):
        out = StringIO()
        with patch("sys.stdin", StringIO(stdin_text)):
            from contextlib import redirect_stdout
            with redirect_stdout(out):
                rc = progress_cli.main(["lint", "-"])
        return rc, out.getvalue()

    def test_lint_stdin_plan_ok(self):
        rc, out = self._lint(self.PLAN)
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["lint"], "ok")
        self.assertEqual(payload["stage"], "reliability")

    def test_lint_stdin_bad_json(self):
        rc, out = self._lint("{bad")
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out)["code"], "INVALID_JSON")

    def test_init_stdin_bad_json_errors(self):
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sys.stdin", StringIO("{bad")):
                from contextlib import redirect_stdout
                with redirect_stdout(out):
                    rc = progress_cli.main(["--repo", tmp, "init", "-"])
        self.assertNotEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "INVALID_JSON")


if __name__ == "__main__":
    unittest.main()
