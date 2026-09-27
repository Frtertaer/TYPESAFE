#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Opt-in ``--live`` CLI probing in skills/jev-consult/scripts/doctor.py.

Default doctor stays read-only and offline; --live runs each detected
harness's CLI once with a minimal prompt and classifies the answer as
available / limited (quota wording) / missing (no binary) / error. The
payload gains a live.fallback hint naming the first harness that
answered. Fake CLIs here are PATH stubs — no real harness needed.
"""
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
    "doctor_live", ROOT / "skills" / "jev-consult" / "scripts" / "doctor.py"
)
DOC = importlib.util.module_from_spec(SPEC)
sys.modules["doctor_live"] = DOC
SPEC.loader.exec_module(DOC)


def run_main(argv, env_extra=None, cwd=None):
    buf = io.StringIO()
    env = {"TYPESAFE_API_KEY": "", "JEV_CONSULT_LOG": "0"}
    if env_extra:
        env.update(env_extra)
    old = os.getcwd()
    if cwd:
        os.chdir(cwd)
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


def make_cli(directory: Path, name: str, body: str) -> Path:
    """Write a stub harness CLI under directory/."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        path = directory / (name + ".bat")
        path.write_text("@echo off\r\n" + body + "\r\n", encoding="utf-8")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
    return path


def live_checks(out, agent=None):
    rows = [
        c for c in out["checks"]
        if c["check"] == "live_probe" and (agent is None or c["agent"] == agent)
    ]
    return rows[0] if agent is not None and rows else (rows if agent is None else None)


class LiveProbeTests(unittest.TestCase):
    def test_default_run_has_no_live_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            rc, out, _ = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                ],
                cwd=tmp,
            )
        self.assertEqual(live_checks(out), [])
        self.assertNotIn("live", out)

    def test_missing_binary_fails_live_probe(self) -> None:
        """Detected home but no CLI on PATH: the harness cannot run, so
        the live check fails — only an absent home is skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            env = {"PATH": str(bindir)}
            rc, out, _ = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "5",
                ],
                env_extra=env,
                cwd=tmp,
            )
        row = live_checks(out, "codex")
        self.assertIsNotNone(row)
        self.assertFalse(row["ok"])
        self.assertNotIn("skipped", row)
        self.assertIn("codex", row["detail"])
        self.assertEqual(
            out["live"]["probes"]["codex"]["status"], "missing"
        )
        self.assertIsNone(out["live"]["fallback"])
        self.assertEqual(rc, 1)

    def test_successful_quota_wording_stays_available(self) -> None:
        """A good answer that merely mentions quota on stdout is not
        limited — quota classification reads stderr only on rc 0."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(bindir, "codex", "echo pong; quota remaining: 100\nexit /b 0")
            else:
                make_cli(bindir, "codex", "echo 'pong; quota remaining: 100'\nexit 0")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, _ = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertEqual(
            out["live"]["probes"]["codex"]["status"], "available"
        )
        self.assertTrue(live_checks(out, "codex")["ok"])

    def test_non_quota_wording_is_error_not_limited(self) -> None:
        """Bare 'exceeded'/'insufficient' outside quota phrasing is a
        plain CLI failure — 'context length exceeded' must not read as
        rate-limited."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            (home / ".grok").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(bindir, "codex", "echo context length exceeded >&2\nexit /b 1")
                make_cli(bindir, "grok", "echo insufficient permissions >&2\nexit /b 1")
            else:
                make_cli(bindir, "codex", "echo 'context length exceeded' >&2\nexit 1")
                make_cli(bindir, "grok", "echo 'insufficient permissions' >&2\nexit 1")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, _ = run_main(
                [
                    "--agents", "codex,grok",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertEqual(out["live"]["probes"]["codex"]["status"], "error")
        self.assertEqual(out["live"]["probes"]["grok"]["status"], "error")

    def test_qualified_quota_wording_is_limited(self) -> None:
        """'insufficient_quota'/'rate limit exceeded' keep classifying
        as limited after the bare-word match tightened."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            (home / ".grok").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(bindir, "codex", "echo error: insufficient_quota >&2\nexit /b 1")
                make_cli(bindir, "grok", "echo rate limit exceeded >&2\nexit /b 1")
            else:
                make_cli(bindir, "codex", "echo 'error: insufficient_quota' >&2\nexit 1")
                make_cli(bindir, "grok", "echo 'rate limit exceeded' >&2\nexit 1")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, _ = run_main(
                [
                    "--agents", "codex,grok",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertEqual(out["live"]["probes"]["codex"]["status"], "limited")
        self.assertEqual(out["live"]["probes"]["grok"]["status"], "limited")

    def test_invalid_live_timeout_reports_error(self) -> None:
        """Zero/negative/non-finite --live-timeout never reaches
        subprocess.run — the probe reports an invalid-timeout error."""
        for bad in ("0", "-3", "nan"):
            with tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "home"
                (home / ".codex").mkdir(parents=True)
                bindir = Path(tmp) / "bin"
                if os.name == "nt":
                    make_cli(bindir, "codex", "echo pong\nexit /b 0")
                else:
                    make_cli(bindir, "codex", "echo pong\nexit 0")
                env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
                rc, out, _ = run_main(
                    [
                        "--agents", "codex",
                        "--home", str(home),
                        "--hermes-home", str(Path(tmp) / "h"),
                        "--live", "--live-timeout", bad,
                    ],
                    env_extra=env,
                    cwd=tmp,
                )
            probe = out["live"]["probes"]["codex"]
            self.assertEqual(probe["status"], "error", bad)
            self.assertIn("invalid timeout", probe["detail"], bad)

    def test_probe_detail_redacts_secret_values(self) -> None:
        """A CLI that echoes a credential alongside quota wording must
        not leak it into doctor output."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(
                    bindir, "codex",
                    "echo usage limit; API_KEY=sk-secretvalue123456789 >&2\nexit /b 1",
                )
            else:
                make_cli(
                    bindir, "codex",
                    "echo 'usage limit; API_KEY=sk-secretvalue123456789' >&2\nexit 1",
                )
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, text = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertNotIn("sk-secretvalue123456789", text)
        self.assertIn("<redacted>", live_checks(out, "codex")["detail"])
        self.assertEqual(
            out["live"]["probes"]["codex"]["status"], "limited"
        )

    def test_fallback_skips_broken_install(self) -> None:
        """A CLI that answers but whose jev-consult setup fails doctor
        is not offered as the fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            (home / ".claude").mkdir(parents=True)  # no skills/jev-consult
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(bindir, "codex", "echo usage limit reached >&2\nexit /b 1")
                make_cli(bindir, "claude", "echo pong\nexit /b 0")
            else:
                make_cli(bindir, "codex", "echo 'usage limit reached' >&2\nexit 1")
                make_cli(bindir, "claude", "echo pong\nexit 0")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, _ = run_main(
                [
                    "--agents", "claude-code,codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        # claude answered but its skill check failed — no valid fallback
        self.assertEqual(
            out["live"]["probes"]["claude-code"]["status"], "available"
        )
        self.assertIsNone(out["live"]["fallback"])

    def test_limited_harness_fails_and_names_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            (home / ".claude").mkdir(parents=True)
            # fallback requires the shared prerequisites (api_key) to pass
            (home / ".env").write_text("TYPESAFE_API_KEY=test-key\n", encoding="utf-8")
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(bindir, "codex", "echo usage limit reached >&2\nexit /b 1")
                make_cli(bindir, "claude", "echo pong\nexit /b 0")
            else:
                make_cli(bindir, "codex", "echo 'usage limit reached' >&2\nexit 1")
                make_cli(bindir, "claude", "echo pong\nexit 0")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            # claude must pass its install checks to be a fallback
            (home / ".claude" / "skills" / "jev-consult").mkdir(parents=True)
            (home / ".claude" / "skills" / "jev-consult" / "SKILL.md").write_text("x")
            settings = home / ".claude" / "settings.json"
            settings.write_text(json.dumps({
                "hooks": {
                    "PostToolUse": [{"hooks": [{"command": "x compact_hook.py"}]}],
                    "UserPromptSubmit": [{"hooks": [{"command": "x inventory_hook.py"}]}],
                }
            }))
            rc, out, _ = run_main(
                [
                    "--agents", "claude-code,codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        codex = live_checks(out, "codex")
        claude = live_checks(out, "claude-code")
        self.assertIsNotNone(codex)
        self.assertFalse(codex["ok"])
        self.assertIn("limited", codex["detail"])
        self.assertIn("fallback", codex["hint"])
        self.assertIn("claude-code", codex["hint"])
        self.assertTrue(claude["ok"])
        self.assertEqual(out["live"]["fallback"], "claude-code")
        self.assertEqual(
            out["live"]["probes"]["codex"]["status"], "limited"
        )
        self.assertEqual(
            out["live"]["probes"]["claude-code"]["status"], "available"
        )
        # limited is a real failure: the run does not pass
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])

    def test_timeout_probe_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(
                    bindir, "codex",
                    "ping -n 4 127.0.0.1 >nul\nexit /b 0",
                )
            else:
                make_cli(bindir, "codex", "sleep 3\nexit 0")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, _ = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "0.5",
                ],
                env_extra=env,
                cwd=tmp,
            )
        row = live_checks(out, "codex")
        self.assertIsNotNone(row)
        self.assertFalse(row["ok"])
        self.assertIn("timed out", row["detail"])
        self.assertEqual(out["live"]["probes"]["codex"]["status"], "error")
        self.assertIsNone(out["live"]["fallback"])

    def test_absent_harness_is_never_probed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            env = {"PATH": str(bindir)}
            rc, out, _ = run_main(
                [
                    "--agents", "hermes,codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "5",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertIsNone(live_checks(out, "hermes"))
        self.assertIsNotNone(live_checks(out, "codex"))
        self.assertEqual(
            sorted(out["live"]["probes"]), ["codex"]
        )

    def test_windsurf_home_present_but_never_probed(self) -> None:
        """Windsurf has no prompt-bearing headless CLI — its binary opens
        the IDE, so --live must skip it even when the home dir exists."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codeium" / "windsurf").mkdir(parents=True)
            rc, out, _ = run_main(
                [
                    "--agents", "windsurf",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "5",
                ],
                cwd=tmp,
            )
        self.assertIsNone(live_checks(out, "windsurf"))
        self.assertNotIn("windsurf", out["live"]["probes"])

    def test_only_live_probe_runs_probes_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            env = {"PATH": str(bindir)}
            rc, out, _ = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--only", "live_probe",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertEqual(
            [c["check"] for c in out["checks"]], ["live_probe"]
        )

    def test_silent_success_is_not_available(self) -> None:
        """rc 0 with no stdout answer is not evidence the prompt ran —
        a wrapper can exit cleanly after ignoring it."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(bindir, "codex", "exit /b 0")
            else:
                make_cli(bindir, "codex", "exit 0")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, _ = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        probe = out["live"]["probes"]["codex"]
        self.assertEqual(probe["status"], "error")
        self.assertIn("no response", probe["detail"])
        self.assertFalse(live_checks(out, "codex")["ok"])
        self.assertIsNone(out["live"]["fallback"])

    def test_unrecognized_credential_shape_redacted(self) -> None:
        """A quota line carrying a bare long token (no known secret
        prefix, no KEY= assignment) must not reach doctor output."""
        token = "xMilk9" * 8 + "drop7"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(
                    bindir, "codex",
                    "echo usage limit hit, auth %s >&2\nexit /b 1" % token,
                )
            else:
                make_cli(
                    bindir, "codex",
                    "echo 'usage limit hit, auth %s' >&2\nexit 1" % token,
                )
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, text = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertNotIn(token, text)
        self.assertEqual(
            out["live"]["probes"]["codex"]["status"], "limited"
        )

    def test_fallback_blocked_when_shared_key_missing(self) -> None:
        """An answering CLI is no fallback when api_key fails — the same
        setup cannot call Jev there either."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            (home / ".claude").mkdir(parents=True)
            (home / ".claude" / "skills" / "jev-consult").mkdir(parents=True)
            (home / ".claude" / "skills" / "jev-consult" / "SKILL.md").write_text("x")
            (home / ".claude" / "settings.json").write_text(json.dumps({
                "hooks": {
                    "PostToolUse": [{"hooks": [{"command": "x compact_hook.py"}]}],
                    "UserPromptSubmit": [{"hooks": [{"command": "x inventory_hook.py"}]}],
                }
            }))
            bindir = Path(tmp) / "bin"
            if os.name == "nt":
                make_cli(bindir, "claude", "echo pong\nexit /b 0")
            else:
                make_cli(bindir, "claude", "echo pong\nexit 0")
            env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
            rc, out, _ = run_main(
                [
                    "--agents", "claude-code",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--live-timeout", "20",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertEqual(
            out["live"]["probes"]["claude-code"]["status"], "available"
        )
        self.assertIsNone(out["live"]["fallback"])

    def test_jq_reaches_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex").mkdir(parents=True)
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            env = {"PATH": str(bindir)}
            rc, _, text = run_main(
                [
                    "--agents", "codex",
                    "--home", str(home),
                    "--hermes-home", str(Path(tmp) / "h"),
                    "--live", "--jq", "live.fallback",
                ],
                env_extra=env,
                cwd=tmp,
            )
        self.assertEqual(rc, 0)
        self.assertEqual(text.strip(), "null")


if __name__ == "__main__":
    unittest.main(verbosity=2)
