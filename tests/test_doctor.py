#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for skills/jev-consult/scripts/doctor.py."""
from __future__ import annotations

import importlib.util
import io
import json
import os
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

    def test_failed_checks_carry_hints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
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
                            rc = DOC.main(["--agents", "claude-code", "--watch", "0.01"])
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

if __name__ == "__main__":
    unittest.main(verbosity=2)
