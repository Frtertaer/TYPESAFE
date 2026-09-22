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


if __name__ == "__main__":
    unittest.main(verbosity=2)
