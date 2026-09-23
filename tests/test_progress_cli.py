from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
PROGRESS_PATH = SCRIPTS / "progress.py"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))

import progress
from progress_core import ProgressError
from test_progress import plan, policy


def run_cli(argv):
    out = io.StringIO()
    with redirect_stdout(out):
        code = progress.main(argv)
    return code, out.getvalue()


class ParserTests(unittest.TestCase):
    def test_subcommands_exist(self) -> None:
        parser = progress.build_parser()
        for argv in (
            ["init", "plan.json"],
            ["status", "s"],
            ["history", "s"],
            ["assess", "s", "i", "--summary", "x"],
            ["invalidate", "s", "i", "--reason", "r"],
            ["restore", "s", "i", "--reason", "r", "--reviewer", "v"],
            ["review", "s", "--reason", "r", "--reviewer", "v"],
            ["lint", "plan.json"],
            ["report", "s"],
        ):
            args = parser.parse_args(argv)
            self.assertTrue(args.command)

    def test_missing_command_rejected(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stderr(err):
            progress.main([])

    def test_missing_required_arguments_rejected(self) -> None:
        for argv in (
            ["status"],
            ["history"],
            ["init"],
            ["assess", "s", "i"],
            ["invalidate", "s", "i"],
            ["restore", "s", "i", "--reason", "r"],
            ["review", "s", "--reason", "r"],
        ):
            err = io.StringIO()
            with self.assertRaises(SystemExit), redirect_stderr(err):
                progress.main(list(argv))


class DispatchTests(unittest.TestCase):
    def _ledger(self):
        ledger = Mock()
        ledger.initialize.return_value = {"ok": True}
        ledger.assess.return_value = {"ok": True}
        ledger.status.return_value = {"ok": True}
        ledger.history.return_value = {"ok": True}
        ledger.invalidate.return_value = {"ok": True}
        ledger.restore.return_value = {"ok": True}
        ledger.review.return_value = {"ok": True}
        return ledger

    def test_status_dispatch_and_default_db(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger) as cls:
            code, out = run_cli(["status", "sample"])
        self.assertEqual(code, 0)
        repo = Path(".").resolve()
        cls.assert_called_once_with(repo / ".devin" / "progress.sqlite3", repo)
        ledger.status.assert_called_once_with("sample")

    def test_init_dispatch_reads_plan_and_default_policy(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger), patch.object(
            progress, "read_json", side_effect=[{"plan": 1}, {"policy": 2}]
        ) as read:
            code, _out = run_cli(["init", "plan.json"])
        self.assertEqual(code, 0)
        ledger.initialize.assert_called_once_with({"plan": 1}, {"policy": 2})
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args_list[0].args[0], Path("plan.json"))
        default_policy = read.call_args_list[1].args[0]
        self.assertEqual(default_policy.name, "policy.json")

    def test_assess_dispatch_with_retry_flag(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, _out = run_cli(
                ["assess", "s", "i", "--summary", "done", "--retry-unavailable"]
            )
        self.assertEqual(code, 0)
        ledger.assess.assert_called_once_with("s", "i", "done", retry_unavailable=True)

    def test_history_dispatch(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, _out = run_cli(["history", "s"])
        self.assertEqual(code, 0)
        ledger.history.assert_called_once_with("s")

    def test_invalidate_dispatch(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, _out = run_cli(["invalidate", "s", "i", "--reason", "r"])
        self.assertEqual(code, 0)
        ledger.invalidate.assert_called_once_with("s", "i", "r")

    def test_restore_dispatch(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, _out = run_cli(["restore", "s", "i", "--reason", "r", "--reviewer", "v"])
        self.assertEqual(code, 0)
        ledger.restore.assert_called_once_with("s", "i", "r", "v")

    def test_review_dispatch_with_approve_finish(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, _out = run_cli(
                ["review", "s", "--reason", "r", "--reviewer", "v", "--approve-finish"]
            )
        self.assertEqual(code, 0)
        ledger.review.assert_called_once_with("s", "r", "v", approve_finish=True)

    def test_explicit_db_and_repo_paths(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            db = Path(tmp) / "custom.sqlite3"
            repo.mkdir()
            with patch.object(progress, "Ledger", return_value=ledger) as cls:
                code, _out = run_cli(
                    ["--repo", str(repo), "--db", str(db), "status", "s"]
                )
        self.assertEqual(code, 0)
        cls.assert_called_once_with(db.resolve(), repo.resolve())


class OneshotVerdictTests(unittest.TestCase):
    def _ledger(self):
        ledger = Mock()
        ledger.status.return_value = {
            "stage_id": "s",
            "action": "continue",
            "reason": "open",
            "points": 3,
            "review_at": 10,
            "assessment_count": 0,
            "assessment_limit": 5,
            "model_attempts": 0,
            "model_attempt_limit": 10,
            "awarded_items": [],
            "blocked_items": [],
        }
        ledger.history.return_value = {
            "stage": {"plan": {"goal": "g", "items": []}},
            "events": [{"kind": "init"}, {"kind": "note"}],
        }
        return ledger

    def test_status_verdict_without_watch(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, _out = run_cli(["status", "s", "--verdict", str(verdict)])
            self.assertEqual(code, 0)
            doc = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertEqual(doc["verdict"], "active")
        self.assertEqual(doc["ticks"], 1)
        self.assertEqual(doc["action"], "continue")
        self.assertEqual(doc["points"], 3)
        self.assertIn("ts", doc)

    def test_status_verdict_resolved(self) -> None:
        ledger = self._ledger()
        ledger.status.return_value["action"] = "finish"
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, _out = run_cli(["status", "s", "--verdict", str(verdict)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(verdict.read_text(encoding="utf-8"))["verdict"], "resolved")

    def test_history_verdict_without_watch(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, _out = run_cli(["history", "s", "--verdict", str(verdict)])
            self.assertEqual(code, 0)
            doc = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertEqual(doc["verdict"], "steady")
        self.assertEqual(doc["events"], 2)
        self.assertIsNone(doc["delta"])

    def test_report_verdict_without_watch(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, _out = run_cli(["report", "s", "--verdict", str(verdict)])
            self.assertEqual(code, 0)
            doc = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertEqual(doc["verdict"], "steady")
        self.assertEqual(doc["ticks"], 1)
        self.assertGreater(doc["chars"], 0)
        self.assertIsNone(doc["delta"])


class OneshotOutTests(unittest.TestCase):
    def _ledger(self):
        ledger = Mock()
        ledger.status.return_value = {
            "stage_id": "s",
            "action": "continue",
            "reason": "open",
            "points": 3,
            "review_at": 10,
            "assessment_count": 0,
            "assessment_limit": 5,
            "model_attempts": 0,
            "model_attempt_limit": 10,
            "awarded_items": [],
            "blocked_items": [],
        }
        ledger.history.return_value = {
            "stage": {"plan": {"goal": "g", "items": []}},
            "events": [{"kind": "init"}, {"kind": "note"}],
        }
        return ledger

    def test_status_out_writes_payload(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "status.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, out = run_cli(["status", "s", "--out", str(target)])
            self.assertEqual(code, 0)
            receipt = json.loads(out)
            self.assertEqual(receipt["wrote"], str(target))
            saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved, ledger.status.return_value)

    def test_history_out_writes_payload(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "history.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, out = run_cli(["history", "s", "--out", str(target)])
            self.assertEqual(code, 0)
            receipt = json.loads(out)
            self.assertEqual(receipt["wrote"], str(target))
            saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(saved, ledger.history.return_value)

    def test_out_receipt_jq_digs_wrapper(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "status.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, out = run_cli(
                    ["status", "s", "--out", str(target), "--jq", "wrote"]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), str(target))

    def test_out_unwritable_path_rc1(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            missing_dir = Path(tmp) / "gone" / "status.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                buf_err = io.StringIO()
                with redirect_stderr(buf_err):
                    code, _out = run_cli(["status", "s", "--out", str(missing_dir)])
            self.assertEqual(code, 1)
            self.assertIn("cannot write", buf_err.getvalue())


class MutatingVerdictTests(unittest.TestCase):
    def test_self_test_verdict_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            code, _out = run_cli(["self-test", "--verdict", str(verdict)])
            self.assertEqual(code, 0)
            doc = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertEqual(doc["verdict"], "ok")
        self.assertEqual(doc["command"], "self-test")
        self.assertEqual(doc["stage"], "self_test")

    def test_review_verdict_error_on_missing_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            code, _out = run_cli(
                [
                    "--repo",
                    tmp,
                    "review",
                    "s",
                    "--reason",
                    "r",
                    "--reviewer",
                    "rv",
                    "--verdict",
                    str(verdict),
                ]
            )
            self.assertEqual(code, 2)
            doc = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertEqual(doc["verdict"], "error")
        self.assertEqual(doc["command"], "review")
        self.assertIn("code", doc)

    def test_status_verdict_not_double_written(self) -> None:
        ledger = Mock()
        ledger.status.return_value = {
            "stage_id": "s",
            "action": "continue",
            "reason": "open",
            "points": 3,
            "review_at": 10,
            "assessment_count": 0,
            "assessment_limit": 5,
            "model_attempts": 0,
            "model_attempt_limit": 10,
            "awarded_items": [],
            "blocked_items": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, _out = run_cli(["status", "s", "--verdict", str(verdict)])
            self.assertEqual(code, 0)
            doc = json.loads(verdict.read_text(encoding="utf-8"))
        self.assertEqual(doc["verdict"], "active")
        self.assertNotIn("command", doc)  # branch verdict, not the tail one


class OutputTests(unittest.TestCase):
    def test_status_result_is_frozen_json(self) -> None:
        frozen = {"stage_id": "sample", "points": 2, "action": "review_required"}
        ledger = Mock()
        ledger.status.return_value = frozen
        with patch.object(progress, "Ledger", return_value=ledger):
            code, out = run_cli(["status", "sample"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), frozen)

    def test_progress_error_is_json_not_traceback(self) -> None:
        ledger = Mock()
        ledger.status.side_effect = ProgressError("STORE_MISSING", "Initialize first")
        with patch.object(progress, "Ledger", return_value=ledger):
            code, out = run_cli(["status", "sample"])
        self.assertEqual(code, 2)
        document = json.loads(out)
        self.assertEqual(
            document,
            {"error": {"code": "STORE_MISSING", "message": "Initialize first"}},
        )
        self.assertNotIn("Traceback", out)


class SubprocessTests(unittest.TestCase):
    def test_help_names_subcommands(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(PROGRESS_PATH), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("review", proc.stdout)
        self.assertIn("assess", proc.stdout)

    def test_init_status_history_in_real_git_repo(self) -> None:
        git_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="test",
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="test",
            GIT_COMMITTER_EMAIL="test@example.com",
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "repo"
            repo.mkdir()
            init = subprocess.run(
                ["git", "init", "-q", str(repo)],
                capture_output=True,
                text=True,
                env=git_env,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            (repo / "check.py").write_text("print('ok')\n", encoding="utf-8")
            for step in (["add", "check.py"], ["commit", "-qm", "fixture"]):
                proc = subprocess.run(
                    ["git", "-C", str(repo)] + step,
                    capture_output=True,
                    text=True,
                    env=git_env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
            plan_path = base / "plan.json"
            plan_path.write_text(json.dumps(plan(1)), encoding="utf-8")
            policy_path = base / "policy.json"
            policy_path.write_text(json.dumps(policy()), encoding="utf-8")
            db = base / "progress.sqlite3"
            cli = [
                sys.executable,
                str(PROGRESS_PATH),
                "--repo",
                str(repo),
                "--db",
                str(db),
                "--policy",
                str(policy_path),
            ]
            proc = subprocess.run(
                cli + ["init", str(plan_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            proc = subprocess.run(
                cli + ["status", "reliability"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["points"], 0)
            proc = subprocess.run(
                cli + ["history", "reliability"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            document = json.loads(proc.stdout)
            self.assertEqual(document["events"], [])
            self.assertEqual(document["stage"]["plan"]["id"], "reliability")
            proc = subprocess.run(
                cli + ["report", "reliability"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("# Progress: reliability", proc.stdout)
            self.assertIn("| `item_0` | open |", proc.stdout)
            out_path = base / "report.md"
            proc = subprocess.run(
                cli + ["report", "reliability", "--out", str(out_path)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            wrote = json.loads(proc.stdout)
            self.assertEqual(wrote["wrote"], str(out_path))
            self.assertIn("# Progress: reliability", out_path.read_text(encoding="utf-8"))
            proc = subprocess.run(
                cli + ["report", "reliability", "--jq", "points"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(json.loads(proc.stdout), 0)
            proc = subprocess.run(
                cli + ["report", "missing_stage"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(json.loads(proc.stdout)["error"]["code"], "STAGE_MISSING")


class LintTests(unittest.TestCase):
    def _write_pair(self, base: Path) -> tuple[Path, Path]:
        plan_path = base / "plan.json"
        plan_path.write_text(json.dumps(plan(1)), encoding="utf-8")
        policy_path = base / "policy.json"
        policy_path.write_text(json.dumps(policy()), encoding="utf-8")
        return plan_path, policy_path

    def test_lint_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path, policy_path = self._write_pair(Path(tmp))
            code, out = run_cli(
                ["--policy", str(policy_path), "lint", str(plan_path)]
            )
            self.assertEqual(code, 0)
            doc = json.loads(out)
            self.assertEqual(doc["lint"], "ok")
            self.assertEqual(doc["items"], 1)

    def test_lint_invalid_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path, policy_path = self._write_pair(Path(tmp))
            broken = plan(1)
            broken.pop("goal")
            plan_path.write_text(json.dumps(broken), encoding="utf-8")
            code, out = run_cli(
                ["--policy", str(policy_path), "lint", str(plan_path)]
            )
            self.assertEqual(code, 1)
            doc = json.loads(out)
            self.assertEqual(doc["lint"], "invalid")
            self.assertTrue(doc["code"])

    def test_lint_invalid_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path, policy_path = self._write_pair(Path(tmp))
            bad = policy()
            bad["endpoint"] = "http://example.invalid"
            policy_path.write_text(json.dumps(bad), encoding="utf-8")
            code, out = run_cli(
                ["--policy", str(policy_path), "lint", str(plan_path)]
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["lint"], "invalid")

    def test_lint_unreadable_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, policy_path = self._write_pair(Path(tmp))
            code, out = run_cli(
                ["--policy", str(policy_path), "lint", str(Path(tmp) / "missing.json")]
            )
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["lint"], "invalid")

    def test_lint_writes_no_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            plan_path, policy_path = self._write_pair(base)
            code, _ = run_cli(
                [
                    "--repo", str(base),
                    "--db", str(base / "progress.sqlite3"),
                    "--policy", str(policy_path),
                    "lint", str(plan_path),
                ]
            )
            self.assertEqual(code, 0)
            self.assertFalse((base / "progress.sqlite3").exists())
            self.assertFalse((base / ".devin").exists())


class JqTests(unittest.TestCase):
    def test_self_test_jq_prints_field(self) -> None:
        code, out = run_cli(["self-test", "--jq", "self_test"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), '"ok"')

    def test_lint_jq_prints_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path, policy_path = LintTests()._write_pair(Path(tmp))
            code, out = run_cli(
                ["--policy", str(policy_path), "lint", str(plan_path), "--jq", "items"]
            )
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "1")

    def test_jq_bad_key_rc2(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code, out = run_cli(["self-test", "--jq", "bogus"])
        self.assertEqual(code, 2)
        self.assertIn("bad --jq key", err.getvalue())
        self.assertEqual(out, "")

    def test_jq_digs_nested(self) -> None:
        code, out = run_cli(["self-test", "--jq", "stage"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), '"self_test"')


class SchemaTests(unittest.TestCase):
    def test_schema_prints_plan_contract(self) -> None:
        code, out = run_cli(["--schema"])
        self.assertEqual(code, 0)
        for line in out.splitlines():
            self.assertRegex(line, r"^[A-Za-z0-9_.-]+: .+ \((required|optional)\)$")

    def test_schema_json_object_shape(self) -> None:
        code, out = run_cli(["--schema", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertTrue(data)
        for row in data.values():
            self.assertEqual(sorted(row), ["required", "type"])
            self.assertIs(type(row["required"]), bool)
            self.assertIs(type(row["type"]), str)

    def test_schema_required_top_keys_match_validate_plan(self) -> None:
        from progress_core import validate_plan

        _, out = run_cli(["--schema", "--json"])
        data = json.loads(out)
        required_top = {
            key for key, row in data.items() if row["required"] and "." not in key
        }
        self.assertEqual(required_top, {"id", "goal", "checks", "required_checks", "items"})
        for key in sorted(required_top):
            broken = plan(1)
            broken.pop(key, None)
            with self.assertRaises(ProgressError, msg="plan missing %r accepted" % key):
                validate_plan(broken, policy())

    def test_schema_subprocess_matches_module(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(PROGRESS_PATH), "--schema", "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), progress.PLAN_SCHEMA_ROWS)


class ReportJsonTests(unittest.TestCase):
    def _ledger(self):
        ledger = Mock()
        ledger.status.return_value = {
            "stage_id": "s",
            "action": "continue",
            "reason": "open",
            "points": 3,
            "review_at": 10,
            "assessment_count": 1,
            "assessment_limit": 5,
            "model_attempts": 2,
            "model_attempt_limit": 10,
            "awarded_items": ["i1"],
            "blocked_items": ["i2"],
            "failed_checks": ["pytest"],
            "next_direction": "d1",
        }
        ledger.history.return_value = {
            "stage": {"plan": {"goal": "g", "items": []}},
            "events": [{"kind": "init"}],
        }
        return ledger

    def test_report_json_emits_structured(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, out = run_cli(["report", "s", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["stage"], "s")
        self.assertEqual(payload["goal"], "g")
        self.assertEqual(payload["points"], 3)
        self.assertEqual(payload["awarded_items"], ["i1"])
        self.assertEqual(payload["blocked_items"], ["i2"])
        self.assertEqual(payload["failed_checks"], ["pytest"])
        self.assertEqual(payload["next_direction"], "d1")
        self.assertEqual(payload["events"], 1)
        self.assertNotIn("# Progress", out)

    def test_report_json_out_writes_json(self) -> None:
        ledger = self._ledger()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.json"
            with patch.object(progress, "Ledger", return_value=ledger):
                code, out = run_cli(["report", "s", "--json", "--out", str(target)])
            self.assertEqual(code, 0)
            self.assertIn("wrote", json.loads(out))
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["stage"], "s")

    def test_report_json_jq_digs_structured(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, out = run_cli(["report", "s", "--json", "--jq", "action"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), "continue")

    def test_report_default_still_markdown(self) -> None:
        ledger = self._ledger()
        with patch.object(progress, "Ledger", return_value=ledger):
            code, out = run_cli(["report", "s"])
        self.assertEqual(code, 0)
        self.assertIn("# Progress: s", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
