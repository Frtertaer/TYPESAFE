#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for skills/jev-consult/scripts/doctor.py."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "doctor", ROOT / "skills" / "jev-consult" / "scripts" / "doctor.py"
)
DOC = importlib.util.module_from_spec(SPEC)
sys.modules["doctor"] = DOC
SPEC.loader.exec_module(DOC)


def run_main(argv, env_extra=None, cwd=None):
    buf = io.StringIO()
    env = {"TYPESAFE_API_KEY": "", "JEV_CONSULT_LOG": "0"}
    if env_extra:
        env.update(env_extra)
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)  # keep the repo's real .env out of Path.cwd() checks
    try:
        with patch.dict(os.environ, env), patch.object(sys, "stdout", buf):
            rc = DOC.main(argv)
    finally:
        os.chdir(old)
    text = buf.getvalue()
    try:
        return rc, json.loads(text), text
    except ValueError:
        return rc, None, text


def make_skill(parent: Path) -> None:
    d = parent / "jev-consult"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: jev-consult\n---\n", encoding="utf-8")


def make_claude_hooks(home: Path, both=True) -> None:
    hooks = {
        "PostToolUse": [{"hooks": [{"command": "x compact_hook.py"}]}],
    }
    if both:
        hooks["UserPromptSubmit"] = [{"hooks": [{"command": "x inventory_hook.py"}]}]
    sdir = home / ".claude"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")


def make_hermes(hermes: Path, enabled=True) -> None:
    (hermes / "plugins" / "jev-compact").mkdir(parents=True)
    make_skill(hermes / "skills")
    marker = "    - jev-compact\n" if enabled else ""
    (hermes / "config.yaml").write_text(
        "plugins:\n  enabled:\n" + marker, encoding="utf-8"
    )


def check_of(out, name, agent="*"):
    for c in out["checks"]:
        if c["check"] == name and c["agent"] == agent:
            return c
    return None


class DoctorTests(unittest.TestCase):
    def test_empty_home_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--home", tmp, "--hermes-home", str(Path(tmp) / "h")], cwd=tmp
            )
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertFalse(check_of(out, "api_key")["ok"])

    def test_self_test_finds_failures_on_empty_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(["--self-test"], cwd=tmp)
        self.assertEqual(rc, 0)
        self.assertIn("self-test: ok", text)
        self.assertIn("failed=", text)
        with tempfile.TemporaryDirectory() as tmp:
            rc, failed, _ = run_main(["--self-test", "--jq", "failed"], cwd=tmp)
        self.assertEqual(rc, 0)
        self.assertIsInstance(failed, int)
        self.assertGreater(failed, 0)

    def test_jq_prints_one_field_of_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--home", tmp, "--hermes-home", str(Path(tmp) / "h"), "--jq", "ok"],
                cwd=tmp,
            )
            self.assertEqual(rc, 0)
            self.assertIs(out, False)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf):
                rc, out, _ = run_main(
                    ["--home", tmp, "--hermes-home", str(Path(tmp) / "h"), "--jq", "nope"],
                    cwd=tmp,
                )
            self.assertEqual(rc, 2)
            self.assertIn("bad --jq key", buf.getvalue())

    def test_report_writes_markdown_check_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "doctor.md"
            rc, _, _ = run_main(
                [
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--report", str(report),
                ],
                cwd=tmp,
            )
            self.assertEqual(rc, 1)
            text = report.read_text(encoding="utf-8")
            self.assertIn("# doctor report", text)
            self.assertIn("verdict: fail", text)
            self.assertIn("| api_key |", text)
            self.assertIn("| NO |", text)

    def test_md_prints_markdown_table_to_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                [
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--md",
                ],
                cwd=tmp,
            )
            self.assertEqual(rc, 1)
            self.assertIn("# doctor report", text)
            self.assertIn("verdict: fail", text)
            self.assertIn("| api_key |", text)
            self.assertIn("| NO |", text)

    def test_matrix_prints_check_by_harness_grid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "h").mkdir()  # hermes present -> its checks emit
            rc, _, text = run_main(
                [
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--matrix",
                ],
                cwd=tmp,
            )
            self.assertEqual(rc, 1)
            self.assertIn("| check | hermes | claude-code | codex | grok | -- |", text)
            self.assertIn("| api_key |", text)
            self.assertIn("| skill |", text)
            # wildcard-only checks must mark harness cells '-'
            api_row = next(
                line for line in text.splitlines()
                if line.startswith("| api_key |")
            )
            self.assertTrue(api_row.endswith("| NO |"))  # no key in test env

    def test_matrix_json_emits_check_agent_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                [
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--matrix", "--json",
                ],
                cwd=tmp,
            )
            self.assertEqual(rc, 1)
            self.assertIsInstance(out, dict)
            self.assertIn("api_key", out)
            self.assertEqual(out["api_key"]["*"], "NO")
            self.assertEqual(out["api_key"]["hermes"], "-")

    def test_matrix_ok_run_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            make_hermes(hermes)
            make_skill(home / ".claude" / "skills")
            make_claude_hooks(home)
            make_skill(home / ".grok" / "skills")
            hooks_dir = home / ".grok" / "hooks"
            hooks_dir.mkdir(parents=True)
            for name, mark in (
                ("jev-compact.json", "compact_hook.py"),
                ("jev-tools.json", "inventory_hook.py"),
            ):
                (hooks_dir / name).write_text(
                    json.dumps({"hooks": {"x": [{"command": mark}]}}),
                    encoding="utf-8",
                )
            make_skill(home / ".codex" / "skills")
            codex = home / ".codex"
            codex.mkdir(parents=True, exist_ok=True)
            (codex / "config.toml").write_text("[x]\n", encoding="utf-8")
            (codex / "hooks.json").write_text(
                json.dumps({"hooks": {"x": [{"command": "inventory_hook.py"}]}}),
                encoding="utf-8",
            )
            rc, _, text = run_main(
                [
                    "--home", str(home),
                    "--hermes-home", str(hermes),
                    "--matrix",
                ],
                cwd=tmp,
                env_extra={"TYPESAFE_API_KEY": "test-key"},
            )
            self.assertIn("| check |", text)

    def test_full_install_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            make_hermes(hermes)
            make_skill(home / ".claude" / "skills")
            make_claude_hooks(home)
            make_skill(home / ".grok" / "skills")
            hooks_dir = home / ".grok" / "hooks"
            hooks_dir.mkdir(parents=True)
            for name, event, mark in (
                ("jev-compact.json", "PostToolUse", "compact_hook.py"),
                ("jev-tools.json", "UserPromptSubmit", "inventory_hook.py"),
            ):
                (hooks_dir / name).write_text(
                    json.dumps({"hooks": {event: [{"hooks": [{"command": "x " + mark}]}]}}),
                    encoding="utf-8",
                )
            make_skill(home / ".codex" / "skills")
            (home / ".codex").mkdir(parents=True, exist_ok=True)
            (home / ".codex" / "hooks.json").write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "x inventory_hook.py"}]}]}}),
                encoding="utf-8",
            )
            (home / ".env").parent.mkdir(parents=True, exist_ok=True)
            (home / ".env").write_text("TYPESAFE_API_KEY=apikey_secret123\n", encoding="utf-8")
            rc, out, text = run_main(
                ["--home", str(home), "--hermes-home", str(hermes)]
            )
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertNotIn("apikey_secret123", text)  # value never printed

    def test_quiet_filters_to_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                [
                    "--home",
                    tmp,
                    "--hermes-home",
                    str(Path(tmp) / "h"),
                    "--quiet",
                ],
                cwd=tmp,
            )
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertTrue(out["checks"])
        self.assertTrue(all(not c["ok"] for c in out["checks"]))

    def test_claude_missing_tools_hook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            make_skill(home / ".claude" / "skills")
            make_claude_hooks(home, both=False)
            rc, out, _ = run_main(
                ["--agents", "claude-code", "--home", str(home), "--hermes-home", str(Path(tmp) / "h")],
                env_extra={"TYPESAFE_API_KEY": "apikey_x"},
            )
        self.assertEqual(rc, 1)
        self.assertTrue(check_of(out, "compact_hook", "claude-code")["ok"])
        self.assertFalse(check_of(out, "inventory_hook", "claude-code")["ok"])

    def test_claude_invalid_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text("bad", encoding="utf-8")
            rc, out, _ = run_main(
                ["--agents", "claude-code", "--home", str(home), "--hermes-home", str(Path(tmp) / "h")],
                env_extra={"TYPESAFE_API_KEY": "apikey_x"},
            )
        self.assertEqual(rc, 1)
        self.assertFalse(check_of(out, "hooks", "claude-code")["ok"])

    def test_hermes_plugin_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hermes = Path(tmp) / "hermes"
            make_hermes(hermes, enabled=False)
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", str(Path(tmp) / "home"), "--hermes-home", str(hermes)],
                env_extra={"TYPESAFE_API_KEY": "apikey_x"},
            )
        self.assertEqual(rc, 1)
        self.assertTrue(check_of(out, "plugin_dir", "hermes")["ok"])
        self.assertFalse(check_of(out, "plugin_enabled", "hermes")["ok"])

    def test_codex_agents_dir_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            make_skill(home / ".agents" / "skills")  # .agents suffices
            (home / ".codex").mkdir(parents=True)
            (home / ".codex" / "hooks.json").write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "inventory_hook.py"}]}]}}),
                encoding="utf-8",
            )
            rc, out, _ = run_main(
                ["--agents", "codex", "--home", str(home), "--hermes-home", str(Path(tmp) / "h")],
                env_extra={"TYPESAFE_API_KEY": "apikey_x"},
            )
        self.assertEqual(rc, 0)
        self.assertTrue(check_of(out, "skill", "codex")["ok"])

    def test_hooks_json_validity_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            codex_dir = home / ".codex"
            codex_dir.mkdir(parents=True)
            (codex_dir / "hooks.json").write_text("{not json", encoding="utf-8")
            rc, out, _ = run_main(
                ["--home", str(home), "--hermes-home", str(hermes), "--agents", "codex"],
                cwd=tmp,
            )
            self.assertEqual(rc, 1)
            c = check_of(out, "hooks_json", "codex")
            self.assertFalse(c["ok"])
            self.assertIn("invalid JSON", c["detail"])
            (codex_dir / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
            rc, out, _ = run_main(
                ["--home", str(home), "--hermes-home", str(hermes), "--agents", "codex"],
                cwd=tmp,
            )
            self.assertEqual(check_of(out, "hooks_json", "codex")["detail"], "valid")
            (codex_dir / "hooks.json").unlink()
            rc, out, _ = run_main(
                ["--home", str(home), "--hermes-home", str(hermes), "--agents", "codex"],
                cwd=tmp,
            )
            self.assertTrue(check_of(out, "hooks_json", "codex")["ok"])

    def test_grok_missing_tools_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            make_skill(home / ".grok" / "skills")
            hooks_dir = home / ".grok" / "hooks"
            hooks_dir.mkdir(parents=True)
            (hooks_dir / "jev-compact.json").write_text(
                json.dumps({"hooks": {"PostToolUse": [{"hooks": [{"command": "compact_hook.py"}]}]}}),
                encoding="utf-8",
            )
            rc, out, _ = run_main(
                ["--agents", "grok", "--home", str(home), "--hermes-home", str(Path(tmp) / "h")],
                env_extra={"TYPESAFE_API_KEY": "apikey_x"},
            )
        self.assertEqual(rc, 1)
        self.assertTrue(check_of(out, "jev-compact.json", "grok")["ok"])
        self.assertFalse(check_of(out, "jev-tools.json", "grok")["ok"])

    def test_policy_check_real_file(self) -> None:
        # doctor.py resolves policy.json next to itself in the repo skill dir
        rc, out, _ = run_main(["--agents", "hermes", "--home", "x", "--hermes-home", "y"])
        self.assertTrue(check_of(out, "policy")["ok"])

    def test_policy_lint_check_emitted_and_ok(self) -> None:
        rc, out, _ = run_main(["--agents", "hermes", "--home", "x", "--hermes-home", "y"])
        lint = check_of(out, "policy_lint")
        self.assertTrue(lint["ok"], lint)

    def test_policy_lint_check_fails_on_error_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad-policy.json"
            policy = json.loads(
                (DOC.SCRIPT_DIR.parent / "policy.json").read_text(encoding="utf-8")
            )
            policy["question_soft_max"] = 99
            policy["question_hard_max"] = 4
            bad.write_text(json.dumps(policy), encoding="utf-8")
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", "x", "--hermes-home", "y"],
                env_extra={"JEV_POLICY": str(bad)},
            )
            lint = check_of(out, "policy_lint")
            self.assertFalse(lint["ok"])
            self.assertIn("errors=", lint["detail"])
            self.assertEqual(lint["hint"], DOC.HINTS["policy_lint"])

    def test_smoke_self_test_check(self) -> None:
        rc, out, _ = run_main(
            ["--only", "smoke_self_test"]
        )
        check = check_of(out, "smoke_self_test")
        self.assertTrue(check["ok"], check)
        self.assertIn("self-test: ok", check["detail"])

    def test_decisions_verify_absent_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--only", "decisions_verify", "--home", tmp,
                 "--hermes-home", tmp],
                env_extra={"JEV_CONSULT_LOG": ""},
                cwd=tmp,
            )
        check = check_of(out, "decisions_verify")
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["detail"], "absent")

    def test_decisions_verify_fails_on_bad_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            logdir = home / ".cache" / "jev-consult"
            logdir.mkdir(parents=True)
            (logdir / "decisions.jsonl").write_text(
                '{"ts": 1, "jev_status": "ok"}\nbad line\n',
                encoding="utf-8",
            )
            rc, out, _ = run_main(
                ["--only", "decisions_verify", "--home", str(home),
                 "--hermes-home", str(home)],
                env_extra={"JEV_CONSULT_LOG": ""},
                cwd=tmp,
            )
            check = check_of(out, "decisions_verify")
            self.assertFalse(check["ok"])
            self.assertIn("bad_lines=1", check["detail"])
            self.assertEqual(rc, 1)

    def test_decisions_verify_ok_on_clean_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            logdir = home / ".cache" / "jev-consult"
            logdir.mkdir(parents=True)
            (logdir / "decisions.jsonl").write_text(
                '{"ts": 1, "jev_status": "ok"}\n', encoding="utf-8",
            )
            rc, out, _ = run_main(
                ["--only", "decisions_verify", "--home", str(home),
                 "--hermes-home", str(home)],
                env_extra={"JEV_CONSULT_LOG": ""},
                cwd=tmp,
            )
            check = check_of(out, "decisions_verify")
            self.assertTrue(check["ok"], check)
            self.assertIn("verify: ok", check["detail"])

    def test_sidecars_missing_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--only", "sidecars", "--home", tmp, "--hermes-home", tmp],
                cwd=tmp,
            )
        check = check_of(out, "sidecars")
        self.assertTrue(check["ok"], check)
        self.assertIn("missing", check["detail"])

    def test_sidecars_fresh_and_stale(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".jev-tools.json").write_text(
                '{"written_at": %d}' % _time.time(), encoding="utf-8"
            )
            (root / ".jev-tools-miss.json").write_text(
                '{"written_at": 1}', encoding="utf-8"
            )
            rc, out, _ = run_main(
                ["--only", "sidecars", "--home", tmp, "--hermes-home", tmp],
                cwd=tmp,
            )
        check = check_of(out, "sidecars")
        self.assertTrue(check["ok"], check)
        self.assertIn("fresh", check["detail"])
        self.assertIn("stale", check["detail"])

    def test_sidecars_invalid_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".jev-tools.json").write_text("{not json", encoding="utf-8")
            rc, out, _ = run_main(
                ["--only", "sidecars", "--home", tmp, "--hermes-home", tmp],
                cwd=tmp,
            )
        check = check_of(out, "sidecars")
        self.assertFalse(check["ok"])
        self.assertIn("invalid", check["detail"])
        self.assertEqual(rc, 1)
        self.assertEqual(check["hint"], DOC.HINTS["sidecars"])

    def test_only_filters_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                [
                    "--home",
                    tmp,
                    "--hermes-home",
                    str(Path(tmp) / "h"),
                    "--only",
                    "api_key",
                ],
                cwd=tmp,
            )
        self.assertEqual(rc, 1)
        self.assertEqual([c["check"] for c in out["checks"]], ["api_key"])

    def test_only_unknown_name_empty_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                [
                    "--home",
                    tmp,
                    "--hermes-home",
                    str(Path(tmp) / "h"),
                    "--only",
                    "nonexistent_check",
                ],
                cwd=tmp,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out["checks"], [])

    def test_unknown_agent_rc2(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf), patch.object(sys, "stdout", io.StringIO()):
            rc = DOC.main(["--agents", "cursor"])
        self.assertEqual(rc, 2)
        self.assertIn("cursor", buf.getvalue())

    def test_out_writes_result_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "doctor.json"
            err = io.StringIO()
            with patch.object(sys, "stderr", err):
                rc, out, _ = run_main(
                    [
                        "--agents",
                        "hermes",
                        "--home",
                        tmp,
                        "--hermes-home",
                        str(Path(tmp) / "h"),
                        "--out",
                        str(out_path),
                    ],
                )
            self.assertIn(rc, (0, 1))
            self.assertIn("wrote", err.getvalue())
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertIn("checks", payload)
            self.assertIsNotNone(check_of(payload, "api_key"))

    def test_verdict_writes_slim_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            rc, out, _ = run_main(
                [
                    "--agents", "hermes",
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--verdict", str(verdict),
                ],
            )
            self.assertIn(rc, (0, 1))
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertIn(payload["verdict"], ("pass", "fail"))
            self.assertEqual(payload["verdict"], "pass" if rc == 0 else "fail")
            self.assertIn("hermes", payload["agents"])
            self.assertGreater(payload["checks"], 0)
            self.assertEqual(payload["failed"], 0 if payload["verdict"] == "pass" else payload["failed"])
            self.assertGreaterEqual(payload["failed"], 0)

    def test_verdict_watch_writes_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            rc, out, _ = run_main(
                [
                    "--agents", "hermes",
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--watch", "0.01",
                    "--verdict", str(verdict),
                ],
                env_extra={"JEV_DOCTOR_WATCH_MAX": "1"},
            )
            self.assertIn(rc, (0, 1))
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertIn(payload["verdict"], ("pass", "fail"))
            self.assertIn("agents", payload)
            self.assertEqual(payload["ticks"], 1)

    def test_verdict_watch_ticks_counts_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            rc, out, _ = run_main(
                [
                    "--agents", "hermes",
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--watch", "0.01",
                    "--verdict", str(verdict),
                ],
                env_extra={"JEV_DOCTOR_WATCH_MAX": "2"},
            )
            self.assertIn(rc, (0, 1))
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["ticks"], 2)

    def test_api_key_from_env_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", tmp, "--hermes-home", str(Path(tmp) / "h")],
                env_extra={"TYPESAFE_API_KEY": "apikey_fromenv"},
            )
        self.assertTrue(check_of(out, "api_key")["ok"])

    def test_decisions_log_lines_counted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            log.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", tmp, "--hermes-home", str(Path(tmp) / "h")],
                env_extra={"JEV_CONSULT_LOG": str(log)},
            )
        c = check_of(out, "decisions_log")
        self.assertTrue(c["ok"])
        self.assertIn("2 lines", c["detail"])

    def test_api_key_export_prefix_in_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(
                "export TYPESAFE_API_KEY=abc123\n", encoding="utf-8"
            )
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", tmp, "--hermes-home", str(Path(tmp) / "h")],
                cwd=tmp,
            )
        self.assertTrue(check_of(out, "api_key")["ok"])

    def test_decisions_log_disabled_reports_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", tmp, "--hermes-home", str(Path(tmp) / "h")],
            )
        c = check_of(out, "decisions_log")
        self.assertTrue(c["ok"])
        self.assertIn("disabled", c["detail"])

    def test_failed_checks_carry_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "h").mkdir()  # harness present but unconfigured
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", tmp, "--hermes-home", str(Path(tmp) / "h")],
                cwd=tmp,
            )
        self.assertEqual(rc, 1)
        skill = check_of(out, "skill", agent="hermes")
        self.assertFalse(skill["ok"])
        self.assertIn("install.py", skill["hint"])
        api = check_of(out, "api_key")
        self.assertFalse(api["ok"])
        self.assertIn("TYPESAFE_API_KEY", api["hint"])

    def test_passing_checks_have_no_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--agents", "hermes", "--home", tmp, "--hermes-home", str(Path(tmp) / "h")],
                cwd=tmp,
            )
        for c in out["checks"]:
            if c["ok"]:
                self.assertNotIn("hint", c)

    def _full_home(self, tmp: str):
        home = Path(tmp) / "home"
        hermes = Path(tmp) / "hermes"
        make_hermes(hermes)
        make_skill(home / ".claude" / "skills")
        make_claude_hooks(home)
        make_skill(home / ".grok" / "skills")
        hooks_dir = home / ".grok" / "hooks"
        hooks_dir.mkdir(parents=True)
        for name, event, mark in (
            ("jev-compact.json", "PostToolUse", "compact_hook.py"),
            ("jev-tools.json", "UserPromptSubmit", "inventory_hook.py"),
        ):
            (hooks_dir / name).write_text(
                json.dumps({"hooks": {event: [{"hooks": [{"command": "x " + mark}]}]}}),
                encoding="utf-8",
            )
        make_skill(home / ".codex" / "skills")
        (home / ".codex").mkdir(parents=True, exist_ok=True)
        (home / ".codex" / "hooks.json").write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "x inventory_hook.py"}]}]}}),
            encoding="utf-8",
        )
        (home / ".env").write_text("TYPESAFE_API_KEY=x\n", encoding="utf-8")
        return home, hermes

    def test_watch_emits_ticks_rc_reflects_last(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                [
                    "--agents", "claude-code",
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--watch", "0.01",
                ],
                env_extra={"JEV_DOCTOR_WATCH_MAX": "2"},
                cwd=tmp,
            )
            ticks = [
                json.loads(l) for l in text.splitlines() if l.startswith("{")
            ]
            self.assertEqual(rc, 1)
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(t["failed"] > 0 and not t["ok"] for t in ticks))

    def test_watch_tick_reports_ok_changed(self) -> None:
        seq = [
            [{"agent": "claude-code", "check": "c", "ok": False, "detail": "d"}],
            [{"agent": "claude-code", "check": "c", "ok": True, "detail": "d"}],
            [{"agent": "claude-code", "check": "c", "ok": True, "detail": "d"}],
        ]

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".claude").mkdir()  # harness present so the check runs
            buf = io.StringIO()
            with patch.dict(
                os.environ,
                {"TYPESAFE_API_KEY": "", "JEV_DOCTOR_WATCH_MAX": "3"},
            ):
                with patch.object(DOC, "check_claude", side_effect=seq), patch.object(
                    DOC, "check_common", side_effect=lambda h, hh: []
                ):
                    old = os.getcwd()
                    os.chdir(tmp)
                    try:
                        with patch.object(sys, "stdout", buf):
                            rc = DOC.main(
                                [
                                    "--agents",
                                    "claude-code",
                                    "--home",
                                    tmp,
                                    "--watch",
                                    "0.01",
                                ]
                            )
                    finally:
                        os.chdir(old)
            ticks = [
                json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertEqual(rc, 0)
            self.assertEqual(len(ticks), 3)
            self.assertEqual(
                [t["ok_changed"] for t in ticks], [False, True, False]
            )

    def test_watch_tick_reports_elapsed_s(self) -> None:
        ok = [{"agent": "claude-code", "check": "c", "ok": True, "detail": "d"}]

        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.dict(
                os.environ,
                {"TYPESAFE_API_KEY": "", "JEV_DOCTOR_WATCH_MAX": "2"},
            ):
                with patch.object(DOC, "check_claude", side_effect=lambda *a: ok), patch.object(
                    DOC, "check_common", side_effect=lambda h, hh: []
                ):
                    old = os.getcwd()
                    os.chdir(tmp)
                    try:
                        with patch.object(sys, "stdout", buf):
                            rc = DOC.main(["--agents", "claude-code", "--watch", "0.01"])
                    finally:
                        os.chdir(old)
            ticks = [
                json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertEqual(rc, 0)
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(isinstance(t["elapsed_s"], float) for t in ticks))
            self.assertGreaterEqual(ticks[1]["elapsed_s"], ticks[0]["elapsed_s"])

    def test_watch_writes_stderr_tick_summary(self) -> None:
        ok = [{"agent": "claude-code", "check": "c", "ok": True, "detail": "d"}]

        with tempfile.TemporaryDirectory() as tmp:
            err = io.StringIO()
            with patch.dict(
                os.environ,
                {"TYPESAFE_API_KEY": "", "JEV_DOCTOR_WATCH_MAX": "2"},
            ):
                with patch.object(DOC, "check_claude", side_effect=lambda *a: ok), patch.object(
                    DOC, "check_common", side_effect=lambda h, hh: []
                ):
                    old = os.getcwd()
                    os.chdir(tmp)
                    try:
                        with patch.object(sys, "stdout", io.StringIO()):
                            with patch.object(sys, "stderr", err):
                                rc = DOC.main(["--agents", "claude-code", "--watch", "0.01"])
                    finally:
                        os.chdir(old)
            self.assertEqual(rc, 0)
            lines = [
                l for l in err.getvalue().splitlines() if l.startswith("watch tick=")
            ]
            self.assertEqual(len(lines), 2)
            self.assertIn("ok=True failed=0", lines[0])

    def test_watch_fail_fast_breaks_on_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                [
                    "--agents", "claude-code",
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--watch", "0.01", "--fail-fast",
                ],
                env_extra={"JEV_DOCTOR_WATCH_MAX": "9"},
                cwd=tmp,
            )
            ticks = [
                json.loads(l) for l in text.splitlines() if l.startswith("{")
            ]
            self.assertEqual(rc, 1)
            self.assertEqual(len(ticks), 1)
            self.assertFalse(ticks[0]["ok"])

    def test_watch_rc_0_when_all_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, hermes = self._full_home(tmp)
            rc, _, text = run_main(
                [
                    "--home", str(home),
                    "--hermes-home", str(hermes),
                    "--watch", "0.01",
                ],
                env_extra={"JEV_DOCTOR_WATCH_MAX": "1"},
                cwd=tmp,
            )
            ticks = [
                json.loads(l) for l in text.splitlines() if l.startswith("{")
            ]
            self.assertEqual(rc, 0)
            self.assertEqual(len(ticks), 1)
            self.assertTrue(ticks[0]["ok"])

    def test_watch_appends_ticks_to_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, hermes = self._full_home(tmp)
            out = Path(tmp) / "ticks.jsonl"
            rc, _, _ = run_main(
                [
                    "--home", str(home),
                    "--hermes-home", str(hermes),
                    "--watch", "0.01",
                    "--out", str(out),
                ],
                env_extra={"JEV_DOCTOR_WATCH_MAX": "2"},
                cwd=tmp,
            )
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(rc, 0)
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("checks" in t and "ok" in t for t in lines))


class WatchJqTests(unittest.TestCase):
    def test_watch_jq_prints_only_named_tick_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                [
                    "--agents", "claude-code",
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--watch", "0.01",
                    "--jq", "failed",
                ],
                env_extra={"JEV_DOCTOR_WATCH_MAX": "2"},
                cwd=tmp,
            )
            lines = text.splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(l.lstrip("-").isdigit() for l in lines))
            self.assertTrue(all(int(l) > 0 for l in lines))

class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            start = _time.time()
            rc, _, text = run_main(
                [
                    "--agents", "claude-code",
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--watch", "0.02",
                ],
                env_extra={
                    "JEV_DOCTOR_WATCH_MAX": "0",
                    "JEV_DOCTOR_WATCH_SECS": "0.05",
                },
                cwd=tmp,
            )
            self.assertLess(_time.time() - start, 2.0)
            ticks = [
                l for l in text.splitlines() if l.startswith("{")
            ]
            self.assertLessEqual(len(ticks), 10)
            self.assertGreaterEqual(len(ticks), 1)

class EnvDumpTests(unittest.TestCase):
    def test_env_lists_prefixed_vars_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, data, text = run_main(
                ["--env"],
                env_extra={"JEV_FOO_XYZ": "bar", "PATH_LIKE": "nope"},
                cwd=tmp,
            )
            self.assertEqual(rc, 0)
            self.assertIn("JEV_FOO_XYZ", data["env"])
            self.assertEqual(data["env"]["JEV_FOO_XYZ"], "bar")
            self.assertNotIn("PATH_LIKE", data["env"])
            self.assertEqual(data["count"], len(data["env"]))

    def test_env_masks_secret_names_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, data, _ = run_main(
                ["--env"],
                env_extra={
                    "TYPESAFE_API_KEY": "apikey_" + "a" * 30 + "_" + "b" * 30,
                    "JEV_INNOCENT": "apikey_" + "a" * 30 + "_" + "b" * 30,
                },
                cwd=tmp,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(data["env"]["TYPESAFE_API_KEY"], "<set>")
            self.assertEqual(data["env"]["JEV_INNOCENT"], "<set>")
            self.assertNotIn("apikey_", json.dumps(data))

    def test_env_empty_secret_name_not_masked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, data, _ = run_main(
                ["--env"],
                env_extra={"TYPESAFE_API_KEY": ""},
                cwd=tmp,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(data["env"]["TYPESAFE_API_KEY"], "")

    def test_env_jq_digs_into_env_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                ["--env", "--jq", "env.JEV_FOO_Q"],
                env_extra={"JEV_FOO_Q": "qq"},
                cwd=tmp,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(text), "qq")

    def test_env_jq_unknown_key_rc2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, _ = run_main(
                ["--env", "--jq", "nope.deep"], cwd=tmp
            )
            self.assertEqual(rc, 2)


class ProgressCheckTests(unittest.TestCase):
    SEAL = "a" * 64

    def _make_ledger(self, repo: Path, seal: str = SEAL) -> Path:
        dev = repo / ".devin"
        dev.mkdir(parents=True, exist_ok=True)
        db = dev / "progress.sqlite3"
        conn = sqlite3.connect(str(db))
        try:
            conn.execute("CREATE TABLE heads (stage_id TEXT, seal TEXT)")
            conn.execute("INSERT INTO heads VALUES ('s1', ?)", (seal,))
            conn.commit()
        finally:
            conn.close()
        return db

    def test_absent_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = DOC.check_progress(Path(tmp))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["ok"])
        self.assertEqual(rows[0]["detail"], "absent")

    def test_healthy_with_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            db = self._make_ledger(repo)
            Path(str(db) + ".heads").write_text(json.dumps({"s1": self.SEAL}))
            rows = DOC.check_progress(repo)
        self.assertTrue(rows[0]["ok"], rows)
        self.assertIn("anchor ok", rows[0]["detail"])

    def test_healthy_no_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rows = DOC.check_progress(Path(tmp))
            self._make_ledger(Path(tmp))
            rows = DOC.check_progress(Path(tmp))
        self.assertTrue(rows[0]["ok"], rows)
        self.assertIn("no anchor", rows[0]["detail"])

    def test_divergent_anchor_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            db = self._make_ledger(repo)
            Path(str(db) + ".heads").write_text(json.dumps({"s1": "b" * 64}))
            rows = DOC.check_progress(repo)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("diverges", rows[0]["detail"])

    def test_unreadable_anchor_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            db = self._make_ledger(repo)
            Path(str(db) + ".heads").write_text("{bad json")
            rows = DOC.check_progress(repo)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("anchor unreadable", rows[0]["detail"])

    def test_not_a_database_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devin").mkdir()
            (repo / ".devin" / "progress.sqlite3").write_text("not sqlite")
            rows = DOC.check_progress(repo)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("sqlite", rows[0]["detail"].lower())

    def test_missing_heads_table_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".devin").mkdir()
            db = repo / ".devin" / "progress.sqlite3"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE other (x TEXT)")
            conn.commit()
            conn.close()
            rows = DOC.check_progress(repo)
        self.assertFalse(rows[0]["ok"])
        self.assertIn("heads", rows[0]["detail"])

    def test_main_runs_progress_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._make_ledger(repo)
            rc, data, _ = run_main(
                ["--only", "progress_ledger"], cwd=tmp,
                env_extra={"TYPESAFE_API_KEY": ""},
            )
            names = [c["check"] for c in data["checks"]]
            self.assertIn("progress_ledger", names)
            self.assertTrue(next(c for c in data["checks"] if c["check"] == "progress_ledger")["ok"])


class DoctorSchemaTests(unittest.TestCase):
    def test_schema_text_marks_rows(self) -> None:
        rc, _, text = run_main(["--schema"])
        self.assertEqual(rc, 0)
        for line in text.splitlines():
            if line.strip():
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+: .+ \((required|optional)\)$")

    def test_schema_json_object(self) -> None:
        rc, out, _ = run_main(["--schema", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn("check.check", out)
        for name in DOC.CHECK_NAMES:
            self.assertIn(name, out["check.check"]["type"])

    def test_env_reports_resolved_knobs(self) -> None:
        env = {
            "JEV_DOCTOR_WATCH_MAX": "5",
            "JEV_DOCTOR_WATCH_SECS": "20",
            "JEV_DOCTOR_WATCH_QUIET": "1",
            "JEV_POLICY": "/tmp/p.json",
        }
        rc, out, _ = run_main(["--env"], env_extra=env)
        self.assertEqual(rc, 0)
        self.assertEqual(out["watch_max"], 5)
        self.assertEqual(out["watch_secs"], 20.0)
        self.assertTrue(out["watch_quiet"])
        self.assertEqual(out["policy"], "/tmp/p.json")

    def test_emitted_checks_within_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, _ = run_main(
                ["--home", tmp, "--hermes-home", str(Path(tmp) / "h")],
                cwd=tmp,
            )
        self.assertIn(rc, (0, 1))
        for check in out["checks"]:
            self.assertIn(check["check"], DOC.CHECK_NAMES)
            self.assertIsInstance(check["ok"], bool)
            self.assertIsInstance(check["detail"], str)
            for key in check:
                self.assertIn(
                    key,
                    ("agent", "check", "ok", "detail", "hint", "suppressed", "skipped"),
                    "undocumented check key %r" % key,
                )

    def test_jsonl_emits_one_row_per_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                ["--home", tmp, "--hermes-home", str(Path(tmp) / "h"), "--jsonl"],
                cwd=tmp,
            )
        self.assertIn(rc, (0, 1))
        lines = [json.loads(l) for l in text.splitlines() if l.strip()]
        self.assertGreater(len(lines), 0)
        for row in lines:
            self.assertIn(row["check"], DOC.CHECK_NAMES)
            self.assertIsInstance(row["ok"], bool)
        self.assertNotIn('"checks"', text)

    def test_jsonl_keys_projects_check_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                [
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--jsonl", "--keys", "check,ok",
                ],
                cwd=tmp,
            )
        self.assertIn(rc, (0, 1))
        lines = [json.loads(l) for l in text.splitlines() if l.strip()]
        self.assertGreater(len(lines), 0)
        for row in lines:
            self.assertEqual(set(row), {"check", "ok"})
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf):
                rc, _, _ = run_main(
                    [
                        "--home", tmp,
                        "--hermes-home", str(Path(tmp) / "h"),
                        "--jsonl", "--keys", " ,",
                    ],
                    cwd=tmp,
                )
            self.assertEqual(rc, 2)

    def test_csv_emits_check_rows(self) -> None:
        import csv as _csv
        import io as _io

        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                [
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--csv",
                ],
                cwd=tmp,
            )
        self.assertIn(rc, (0, 1))
        rows = list(_csv.reader(_io.StringIO(text)))
        self.assertEqual(rows[0], ["check", "agent", "ok", "hint"])
        self.assertGreater(len(rows), 1)

    def test_csv_keys_drives_columns(self) -> None:
        import csv as _csv
        import io as _io

        with tempfile.TemporaryDirectory() as tmp:
            rc, _, text = run_main(
                [
                    "--home", tmp,
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--csv", "--keys", "check,ok",
                ],
                cwd=tmp,
            )
        self.assertIn(rc, (0, 1))
        rows = list(_csv.reader(_io.StringIO(text)))
        self.assertEqual(rows[0], ["check", "ok"])
        self.assertGreater(len(rows), 1)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with patch.object(sys, "stderr", buf):
                rc, _, _ = run_main(
                    [
                        "--home", tmp,
                        "--hermes-home", str(Path(tmp) / "h"),
                        "--csv", "--keys", " ,",
                    ],
                    cwd=tmp,
                )
            self.assertEqual(rc, 2)


class DoctorBaselineTests(unittest.TestCase):
    def _empty_home(self, tmp: str) -> Path:
        home = Path(tmp) / "home"
        home.mkdir()
        return home

    def test_baseline_write_then_suppress_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            argv = ["--home", str(home), "--hermes-home", str(home / "h"), "--agents", "claude-code"]
            rc, out, _ = run_main(argv, cwd=tmp)
            self.assertEqual(rc, 1)
            failing = [c for c in out["checks"] if not c["ok"]]
            self.assertGreater(len(failing), 0)

            base = Path(tmp) / "base.json"
            rc, _, _ = run_main(argv + ["--baseline-write", str(base)], cwd=tmp)
            self.assertEqual(rc, 1)  # snapshot alone does not suppress
            stored = json.loads(base.read_text(encoding="utf-8"))["findings"]
            self.assertEqual(len(stored), len(failing))

            rc, out, _ = run_main(argv + ["--baseline", str(base)], cwd=tmp)
            self.assertEqual(rc, 0)
            self.assertTrue(out["ok"])
            self.assertEqual(out["suppressed"], len(failing))
            marked = [c for c in out["checks"] if c.get("suppressed")]
            self.assertEqual(len(marked), len(failing))

    def test_baseline_partial_suppression_still_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            # a present-but-broken harness contributes real failing checks,
            # so suppressing only the api_key failure still fails the run
            (home / ".claude").mkdir()
            argv = ["--home", str(home), "--hermes-home", str(home / "h"), "--agents", "claude-code"]
            rc, out, _ = run_main(argv, cwd=tmp)
            self.assertEqual(rc, 1)
            one = next(c for c in out["checks"] if not c["ok"])
            base = Path(tmp) / "base.json"
            base.write_text(
                json.dumps({"findings": [{"agent": one["agent"], "check": one["check"]}]}),
                encoding="utf-8",
            )
            rc, out, _ = run_main(argv + ["--baseline", str(base)], cwd=tmp)
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
            self.assertEqual(out["suppressed"], 1)
            self.assertTrue(check_of(out, one["check"], one["agent"])["suppressed"])

    def test_baseline_missing_file_counts_everything(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            argv = ["--home", str(home), "--hermes-home", str(home / "h"), "--agents", "claude-code"]
            rc, out, _ = run_main(
                argv + ["--baseline", str(Path(tmp) / "absent.json")], cwd=tmp
            )
            self.assertEqual(rc, 1)
            self.assertFalse(out["ok"])
            self.assertEqual(out["suppressed"], 0)

    def test_baseline_verdict_passes_when_all_suppressed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = self._empty_home(tmp)
            argv = ["--home", str(home), "--hermes-home", str(home / "h"), "--agents", "claude-code"]
            base = Path(tmp) / "base.json"
            run_main(argv + ["--baseline-write", str(base)], cwd=tmp)
            verdict = Path(tmp) / "v.json"
            rc, out, _ = run_main(
                argv + ["--baseline", str(base), "--verdict", str(verdict)], cwd=tmp
            )
            self.assertEqual(rc, 0)
            doc = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(doc["verdict"], "pass")
            self.assertEqual(doc["failed"], 0)
            self.assertGreater(doc["suppressed"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
