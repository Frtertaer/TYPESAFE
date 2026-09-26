from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import progress as progress_cli
import progress_core as progress
from progress_core import ProgressError
from test_progress import FakeEvidence, answer, picker, plan, policy

MODIFY = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n-old behavior\n+verified behavior\n"
MODIFY_REFORMATTED = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n-  old   behavior\n+    verified   behavior\n"
RENAMED = (
    "diff --git a/core.py b/core.py\ndeleted file mode 100644\n--- a/core.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old behavior\n"
    "diff --git a/renamed.py b/renamed.py\nnew file mode 100644\n--- /dev/null\n+++ b/renamed.py\n@@ -0,0 +1,1 @@\n+verified behavior\n"
)


def run_cli(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = progress_cli.main(argv)
    return code, out.getvalue()


def picker_with_usage(choice, input_tokens=120, output_tokens=30, **kwargs):
    base = picker(choice, **kwargs)
    def ask(state, questions, snapshot):
        response = base.side_effect(state, questions, snapshot)
        response["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        return response
    return Mock(side_effect=ask)


def harmful_policy():
    doc = policy()
    doc["progress"]["harmful"] = True
    doc["progress"]["points"]["harmful"] = -2
    doc["templates"]["contribution"]["criteria"]["harmful"] = (
        "Verified change that breaks scope or agreed behavior of other items"
    )
    return doc


class SemanticCreditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = FakeEvidence()
        self.ledger = progress.Ledger(self.root / "progress.sqlite3", self.root, evidence=self.evidence)
        self.ledger.initialize(plan(), policy())
        self.evidence.advance()

    def assess(self, item="item_0", level="major", **kwargs):
        return self.ledger.assess("reliability", item, "Verified outcome", asker=picker(level), **kwargs)

    def test_reformatted_line_keeps_credit(self):
        self.evidence.diff_text = MODIFY
        self.assertEqual(self.assess()["points"], 3)
        self.evidence.advance(3)
        self.evidence.diff_text = MODIFY_REFORMATTED
        result = self.assess()
        self.assertEqual(result["points"], 3)
        self.assertTrue(result["cached"])
        events = self.ledger.history("reliability")["events"]
        self.assertFalse(any(e["kind"] == "invalidate" for e in events))

    def test_rename_delete_create_keeps_credit(self):
        self.evidence.diff_text = MODIFY
        self.assertEqual(self.assess()["points"], 3)
        self.evidence.advance(3)
        self.evidence.diff_text = RENAMED
        result = self.assess()
        self.assertEqual(result["points"], 3)
        events = self.ledger.history("reliability")["events"]
        self.assertFalse(any(e["kind"] == "invalidate" for e in events))

    def test_reorder_within_context_still_revokes(self):
        self.evidence.diff_text = (
            "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,2 +1,2 @@\n-foo\n+bar\n foo\n"
        )
        self.assertEqual(self.assess()["points"], 3)
        self.evidence.advance(3)
        self.evidence.diff_text = (
            "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,2 +1,2 @@\n foo\n-foo\n+bar\n"
        )
        result = self.assess()
        self.assertEqual(result["points"], 0)
        self.assertEqual(result["action"], "repair_required")

    def test_reformatted_section_recopy_suppresses_content_lines(self):
        block = "".join("+shared line %d\n" % n for n in range(4))
        original = progress._diff_line_hashes(
            "diff --git a/core_0.py b/core_0.py\nnew file mode 100644\n--- /dev/null\n+++ b/core_0.py\n@@ -0,0 +1,4 @@\n" + block
        )
        credit = progress._earned_credit(original, progress._credited_union([]), [])
        prior = [(credit["sections"][path], credit["sections_norm"][path])
                 for path in credit["sections"]]
        reformatted = "".join("+  shared  line %d\n" % n for n in range(4))
        copied = progress._diff_line_hashes(
            "diff --git a/core_1.py b/core_1.py\nnew file mode 100644\n--- /dev/null\n+++ b/core_1.py\n@@ -0,0 +1,4 @@\n" + reformatted
        )
        earned = progress._earned_credit(copied, progress._credited_union([]), prior)
        self.assertEqual(earned["added"], [])
        self.assertNotIn("core_1.py", earned.get("sections", {}))

    def test_old_credit_shape_still_validates(self):
        raw = progress._diff_line_hashes(MODIFY)
        credit = progress._earned_credit(raw, progress._credited_union([]), [])
        stripped = {k: v for k, v in credit.items() if k in ("added", "removed", "ops", "sections")}
        self.assertTrue(progress._credit_ok(stripped))
        self.assertTrue(progress._credited_retained(stripped, MODIFY))
        rebuilt = progress._align_credit_shape(stripped, credit)
        self.assertEqual(stripped, rebuilt)
        self.assertFalse(progress._credited_retained(stripped, MODIFY_REFORMATTED))

    def test_same_mode_change_on_other_path_does_not_retain(self):
        credited = progress._diff_line_hashes(
            "diff --git a/core_0.py b/core_0.py\nold mode 100644\nnew mode 100755\n"
        )
        credit = progress._earned_credit(credited, progress._credited_union([]), [])
        current = "diff --git a/core_1.py b/core_1.py\nold mode 100644\nnew mode 100755\n"
        self.assertFalse(progress._credited_retained(credit, current))

    def test_move_outside_item_scope_keeps_credit(self):
        credited = progress._diff_line_hashes('["core.py"]\n' + MODIFY)
        credit = progress._earned_credit(credited, progress._credited_union([]), [])
        scoped = (
            '["core.py"]\n'
            "diff --git a/core.py b/core.py\ndeleted file mode 100644\n"
            "--- a/core.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-old behavior\n"
        )
        wide = "null\n" + scoped.split("\n", 1)[1] + (
            "diff --git a/moved.py b/moved.py\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/moved.py\n@@ -0,0 +1,1 @@\n+verified behavior\n"
        )
        self.assertTrue(progress._credited_retained(credit, scoped, wide))
        self.assertFalse(progress._credited_retained(credit, scoped))

    def test_delete_elsewhere_does_not_retain_removed_credit(self):
        credited = progress._diff_line_hashes(
            '["x.py"]\n'
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,1 @@\n keep\n-obsolete\n"
        )
        credit = progress._earned_credit(credited, progress._credited_union([]), [])
        # x.py reverted to baseline (no diff block); y.py deleted with the same line
        current = (
            "diff --git a/y.py b/y.py\ndeleted file mode 100644\n"
            "--- a/y.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-obsolete\n"
        )
        self.assertFalse(progress._credited_retained(credit, current))

    def test_credit_ok_rejects_malformed_norms(self):
        raw = progress._diff_line_hashes(MODIFY)
        credit = progress._earned_credit(raw, progress._credited_union([]), [])
        self.assertTrue(progress._credit_ok(credit))
        bad = dict(credit)
        bad["added_norm"] = ["not", "a", "dict"]
        self.assertFalse(progress._credit_ok(bad))
        bad = dict(credit)
        bad["added_norm"] = {digest: {"n": "zz", "p": "core_0.py", "f": info["f"]}
                             for digest, info in credit["added_norm"].items()}
        self.assertFalse(progress._credit_ok(bad))


class HarmfulPolicyTests(unittest.TestCase):
    def test_harmful_requires_opt_in(self):
        doc = harmful_policy()
        del doc["progress"]["harmful"]
        with self.assertRaises(ProgressError) as cm:
            progress.validate_progress_policy(doc)
        self.assertEqual(cm.exception.code, "INVALID_POLICY")

    def test_harmful_weight_must_be_negative(self):
        doc = harmful_policy()
        doc["progress"]["points"]["harmful"] = 0
        with self.assertRaises(ProgressError) as cm:
            progress.validate_progress_policy(doc)
        self.assertEqual(cm.exception.code, "INVALID_POLICY")

    def test_harmful_flag_must_be_bool(self):
        doc = harmful_policy()
        doc["progress"]["harmful"] = "yes"
        with self.assertRaises(ProgressError) as cm:
            progress.validate_progress_policy(doc)
        self.assertEqual(cm.exception.code, "INVALID_POLICY")

    def test_harmful_opt_in_accepted(self):
        progress.validate_progress_policy(harmful_policy())


class HarmfulLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = FakeEvidence()
        self.ledger = progress.Ledger(self.root / "progress.sqlite3", self.root, evidence=self.evidence)
        self.ledger.initialize(plan(), harmful_policy())
        self.evidence.advance()

    def test_harmful_pick_records_negative_points(self):
        self.evidence.diff_text = MODIFY
        result = self.ledger.assess("reliability", "item_0", "Verified outcome", asker=picker("harmful"))
        self.assertEqual(result["points"], -2)
        status = self.ledger.status("reliability")
        self.assertEqual(status["points"], -2)
        self.assertEqual(status["harmful_items"], ["item_0"])
        self.assertNotIn("item_0", status["awarded_items"])

    def test_positive_score_lifts_penalty(self):
        self.evidence.diff_text = MODIFY
        self.assertEqual(
            self.ledger.assess("reliability", "item_0", "Verified outcome", asker=picker("harmful"))["points"],
            -2,
        )
        self.evidence.advance(3)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n-old\n+new fix\n"
        result = self.ledger.assess("reliability", "item_0", "Verified fix", asker=picker("major"))
        self.assertEqual(result["points"], 3)
        self.assertEqual(self.ledger.status("reliability")["harmful_items"], [])


class TokenBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = FakeEvidence()
        self.ledger = progress.Ledger(self.root / "progress.sqlite3", self.root, evidence=self.evidence)
        self.policy = policy()
        self.policy["progress"]["max_tokens"] = 300
        self.ledger.initialize(plan(), self.policy)
        self.evidence.advance()

    def test_usage_recorded_and_summed(self):
        self.evidence.diff_text = MODIFY
        self.ledger.assess(
            "reliability", "item_0", "Verified outcome",
            asker=picker_with_usage("major"),
        )
        events = self.ledger.history("reliability")["events"]
        usage = [e["data"].get("usage") for e in events if e["kind"] == "assessment"]
        self.assertEqual(usage[-1], {"input_tokens": 120, "output_tokens": 30})
        status = self.ledger.status("reliability")
        self.assertEqual(status["tokens_used"], 150)
        self.assertEqual(status["token_limit"], 300)

    def test_token_budget_exhaustion(self):
        self.evidence.diff_text = MODIFY
        self.ledger.assess("reliability", "item_0", "s", asker=picker_with_usage("major", input_tokens=250))
        self.evidence.advance(3)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        result = self.ledger.assess("reliability", "item_1", "s", asker=picker_with_usage("major"))
        self.assertEqual(result["action"], "budget_exhausted")
        self.assertEqual(result["reason"], "token_budget")

    def test_over_budget_review_allowed_on_tokens_with_finish_approval(self):
        self.evidence.diff_text = MODIFY
        self.ledger.assess("reliability", "item_0", "s", asker=picker_with_usage("major", input_tokens=250))
        self.evidence.advance(3)
        self.evidence.diff_text = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
        self.ledger.assess("reliability", "item_1", "s", asker=picker_with_usage("major"))
        result = self.ledger.review(
            "reliability", "Tokens exhausted", "test-reviewer",
            approve_finish=True, asker=picker("finish", acceptance={"item_0": "met", "item_1": "met"}),
        )
        review_events = [
            e["data"] for e in self.ledger.history("reliability")["events"] if e["kind"] == "review"
        ]
        self.assertTrue(review_events[-1].get("over_budget"))
        self.assertEqual(result["action"], "budget_exhausted")

    def test_max_tokens_must_be_positive_int(self):
        for bad in (0, -5, 1.5, "300"):
            doc = policy()
            doc["progress"]["max_tokens"] = bad
            with self.assertRaises(ProgressError):
                progress.validate_progress_policy(doc)


class ReportAllTests(unittest.TestCase):
    def _ledger(self):
        ledger = Mock()
        ledger.stage_ids.return_value = ["alpha", "beta"]
        ledger.status.side_effect = lambda stage: {
            "stage_id": stage, "action": "continue", "reason": "ok",
            "points": 3 if stage == "alpha" else -1,
            "review_at": 12, "assessment_count": 1, "model_attempts": 1,
            "tokens_used": 100, "awarded_items": ["i0"] if stage == "alpha" else [],
            "blocked_items": ["i1"] if stage == "beta" else [],
        }
        ledger.history.side_effect = lambda stage: {
            "stage": {"plan": {"goal": "g"}},
            "events": [{"kind": "assessment", "sequence": 1,
                        "data": {"item_id": "i0", "level": "major" if stage == "alpha" else "harmful",
                                 "points": 3 if stage == "alpha" else -1}}],
        }
        return ledger

    def test_report_all_aggregates(self):
        ledger = self._ledger()
        with patch.object(progress_cli, "Ledger", return_value=ledger):
            code, out = run_cli(["report", "--all", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["totals"]["stages"], 2)
        self.assertEqual(payload["totals"]["points"], 2)
        self.assertEqual(payload["totals"]["tokens_used"], 200)
        self.assertEqual(payload["categories"]["major"], {"count": 1, "points": 3})
        self.assertEqual(payload["categories"]["harmful"], {"count": 1, "points": -1})
        self.assertEqual([row["stage"] for row in payload["stages"]], ["alpha", "beta"])

    def test_report_all_markdown_lists_stages(self):
        ledger = self._ledger()
        with patch.object(progress_cli, "Ledger", return_value=ledger):
            code, out = run_cli(["report", "--all"])
        self.assertEqual(code, 0)
        self.assertIn("alpha", out)
        self.assertIn("beta", out)
        self.assertIn("major", out)

    def test_report_without_stage_or_all_errors(self):
        ledger = self._ledger()
        with patch.object(progress_cli, "Ledger", return_value=ledger):
            code, out = run_cli(["report"])
        self.assertEqual(code, 1)
        self.assertIn("--all", out)

    def test_report_all_rejects_watch(self):
        ledger = self._ledger()
        with patch.object(progress_cli, "Ledger", return_value=ledger):
            code, out = run_cli(["report", "--all", "--watch", "1"])
        self.assertEqual(code, 1)
        self.assertIn("--all", out)


class CalibrateTests(unittest.TestCase):
    CASES = SCRIPTS.parent / "examples" / "progress-cases.json"

    def test_golden_cases_pass_offline(self):
        code, out = run_cli(["calibrate", str(self.CASES)])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["calibrate"]["cases"], len(payload["calibrate"]["results"]))
        self.assertTrue(all(row["ok"] for row in payload["calibrate"]["results"]))

    def test_drift_fails(self):
        cases = json.loads(self.CASES.read_text())
        cases["cases"][0]["expect"] = "major" if cases["cases"][0]["expect"] != "major" else "zero"
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(cases, handle)
            path = handle.name
        self.addCleanup(Path(path).unlink)
        code, out = run_cli(["calibrate", path])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertFalse(all(row["ok"] for row in payload["calibrate"]["results"]))

    def test_stdin_cases(self):
        import sys as _sys
        text = self.CASES.read_text()
        with patch.object(_sys, "stdin", io.StringIO(text)):
            code, out = run_cli(["calibrate", "-"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

    def test_invalid_cases_document(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('{"cases": []}')
            path = handle.name
        self.addCleanup(Path(path).unlink)
        code, out = run_cli(["calibrate", path])
        self.assertEqual(code, 2)
        self.assertIn("INVALID_INPUT", out)


ADD_LINE = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,2 @@\n keep\n+verified behavior\n"
ADD_LINE_MOVED = "diff --git a/core.py b/core.py\n--- a/core.py\n+++ b/core.py\n@@ -1,1 +1,4 @@\n keep\n+verified behavior\n+  verified   behavior\n+unrelated new\n"
TWO_FILE_SAME_LINE = (
    "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n keep\n+same call\n"
    "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,2 @@\n keep\n+same call\n"
)


class ReviewFixRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = FakeEvidence()
        self.ledger = progress.Ledger(self.root / "progress.sqlite3", self.root, evidence=self.evidence)
        self.ledger.initialize(plan(), policy())
        self.evidence.advance()

    def assess(self, item="item_0", level="major", **kwargs):
        return self.ledger.assess("reliability", item, "Verified outcome", asker=picker(level), **kwargs)

    def test_usage_extra_keys_are_dropped(self):
        base = picker("major")

        def ask(state, questions, snapshot):
            response = base.side_effect(state, questions, snapshot)
            response["usage"] = {
                "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
            }
            return response

        self.evidence.diff_text = MODIFY
        self.ledger.assess(
            "reliability", "item_0", "Verified outcome", asker=Mock(side_effect=ask)
        )
        events = self.ledger.history("reliability")["events"]
        usage = [e["data"].get("usage") for e in events if e["kind"] == "assessment"]
        self.assertEqual(usage[-1], {"input_tokens": 10, "output_tokens": 5})

    def test_reformatted_readdition_earns_nothing_new(self):
        shared = plan()
        shared["items"][0]["paths"] = ["shared.py"]
        shared["items"][1]["paths"] = ["shared.py"]
        ledger = progress.Ledger(
            self.root / "shared.sqlite3", self.root, evidence=self.evidence
        )
        ledger.initialize(shared, policy())
        self.evidence.advance(7)
        self.evidence.diff_text = ADD_LINE
        ledger.assess(
            "reliability", "item_0", "Verified outcome", asker=picker("major")
        )
        self.evidence.advance(9)
        self.evidence.diff_text = ADD_LINE_MOVED
        result = ledger.assess(
            "reliability", "item_1", "Verified outcome", asker=picker("major")
        )
        event = ledger.history("reliability")["events"][-1]
        self.assertEqual(len(event["data"]["credit"]["added"]), 1)
        self.assertEqual(result["points"], 6)

    def test_same_line_in_other_path_still_earns(self):
        self.evidence.diff_text = ADD_LINE
        self.assess(item="item_0")
        self.evidence.advance(3)
        self.assess(item="item_1")
        event = self.ledger.history("reliability")["events"][-1]
        self.assertTrue(event["data"]["credit"]["added"])

    def test_indexed_norm_binds_path(self):
        raw = progress._diff_line_hashes(TWO_FILE_SAME_LINE)
        infos = list(raw["added_norm"].values())
        self.assertEqual(len({info["n"] for info in infos}), 2)
        self.assertEqual(len({info["f"] for info in infos}), 1)

    def test_normalize_preserves_string_literal_whitespace(self):
        self.assertNotEqual(
            progress._normalize_line('x = "a  b"'),
            progress._normalize_line('x = "a b"'),
        )
        self.assertEqual(
            progress._normalize_line('  x   =  "a  b"  '),
            'x = "a  b"',
        )
        self.assertEqual(progress._normalize_line("  foo(1,   2)"), "foo(1, 2)")

    def test_continue_review_with_penalties_replays(self):
        ledger = progress.Ledger(
            self.root / "harmful.sqlite3", self.root, evidence=self.evidence
        )
        ledger.initialize(plan(), harmful_policy())
        self.evidence.advance(9)
        self.evidence.diff_text = MODIFY
        ledger.assess(
            "reliability", "item_0", "Harmful outcome", asker=picker("harmful")
        )
        for n in range(1, 6):
            self.evidence.advance(9 + n)
            result = ledger.assess(
                "reliability", "item_%d" % n, "Verified outcome", asker=picker("major")
            )
        self.assertEqual(result["action"], "review_required")
        ledger.review(
            "reliability", "Evidence reviewed", "test-reviewer",
            asker=picker("continue"),
        )
        status = ledger.status("reliability")
        self.assertEqual(status["review_at"], 25)

    def test_report_watch_requires_stage_or_all(self):
        report_tests = ReportAllTests()
        ledger = report_tests._ledger()
        with patch.object(progress_cli, "Ledger", return_value=ledger):
            code, out = run_cli(["report", "--watch", "1"])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload["error"]["code"], "INVALID_INPUT")


if __name__ == "__main__":
    unittest.main()
