#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "scripts" / "install.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


install = load(INSTALL_PATH, "install_jev")


class SnippetTests(unittest.TestCase):
    def test_snippets_guard_scaffold_and_decision_action(self) -> None:
        for blob in (install.SNIPPET, install.REPO_SNIPPET):
            self.assertIn("Jev decides", blob)
            self.assertIn("decision.action", blob)
            self.assertIn("scaffold", blob)
            self.assertIn("Hook never auto-installs", blob)
            self.assertIn("Never print `TYPESAFE_API_KEY`", blob)
            self.assertIn("Never npx", blob)
            self.assertIn("Never `claude plugin install`", blob)

    def test_repo_snippet_names_codex_hooks_json(self) -> None:
        self.assertIn("hooks.json", install.REPO_SNIPPET)
        self.assertIn("UserPromptSubmit", install.REPO_SNIPPET)
        self.assertIn("scaffold keep_vs_change", install.REPO_SNIPPET)
        self.assertIn("python scripts/install.py", install.REPO_SNIPPET)


class GitignoreTests(unittest.TestCase):
    def test_ignores_key_and_jev_scratch_json(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", text)
        self.assertIn(".jev-*.json", text)
        self.assertIn(".jev-*.txt", text)
        self.assertNotIn("policy.json", text)
        self.assertNotIn("hooks.json", text)


class CodexHookTests(unittest.TestCase):
    def test_live_hooks_codex_is_user_prompt_only(self) -> None:
        src = inspect.getsource(install.install_live_hooks)
        self.assertEqual(src.count("upsert_codex_event"), 1)
        self.assertIn('"UserPromptSubmit"', src)
        self.assertIn("codex live mutate: none (no PostToolUse)", src)
        self.assertNotIn('upsert_codex_event(\n                hooks,\n                "PostToolUse"', src)

    def test_upsert_writes_command_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            dry = install.upsert_codex_event(
                hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, True
            )
            self.assertFalse(hooks.exists())
            self.assertIn("upsert hook", dry)
            install.upsert_codex_event(
                hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False
            )
            data = json.loads(hooks.read_text(encoding="utf-8"))
            blob = json.dumps(data)
            self.assertIn("UserPromptSubmit", blob)
            self.assertIn("commandWindows", blob)
            self.assertIn("inventory_hook.py", blob)
            self.assertNotIn("PostToolUse", blob)


class GrokHookTests(unittest.TestCase):
    def test_hook_command_quotes_python_and_script(self) -> None:
        script = Path("D:/tmp/inventory_hook.py")
        cmd = install.grok_hook_command(script)
        self.assertTrue(cmd.startswith('"'))
        self.assertIn("inventory_hook.py", cmd)
        self.assertIn(sys_exe_slash(), cmd)

    def test_tools_hook_is_user_prompt_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jev-tools.json"
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            dry = install.write_grok_event(path, "UserPromptSubmit", script, 20, True)
            self.assertFalse(path.exists())
            self.assertIn("upsert hook", dry)
            install.write_grok_event(path, "UserPromptSubmit", script, 20, False)
            install.write_grok_event(path, "UserPromptSubmit", script, 20, False)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(data["hooks"]), ["UserPromptSubmit"])
            self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)
            self.assertIn("inventory_hook.py", json.dumps(data))

    def test_compact_hook_is_post_tool_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jev-compact.json"
            script = Path(tmp) / "compact_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            install.write_grok_hook(path, script, False)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(data["hooks"]), ["PostToolUse"])
            self.assertIn("compact_hook.py", json.dumps(data))


class ClaudeHookTests(unittest.TestCase):
    def test_user_prompt_strip_keeps_other_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            skill = Path(tmp) / "jev-consult"
            script = skill / "scripts" / "inventory_hook.py"
            other = skill / "scripts" / "compact_hook.py"
            script.parent.mkdir(parents=True)
            script.write_text("# hook\n", encoding="utf-8")
            other.write_text("# hook\n", encoding="utf-8")
            install.upsert_claude_hook(settings, other, False)
            install.upsert_claude_event(
                settings, "UserPromptSubmit", script, "inventory_hook.py", 20, False
            )
            install.strip_claude_event(settings, "UserPromptSubmit", "inventory_hook.py", False)
            data = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(data["hooks"].get("UserPromptSubmit") or [], [])
            self.assertIn("PostToolUse", data["hooks"])
            blob = json.dumps(data)
            self.assertIn("compact_hook.py", blob)
            self.assertNotIn("inventory_hook.py", json.dumps(data["hooks"].get("UserPromptSubmit", [])))


class CopyAndSnippetTests(unittest.TestCase):
    def test_copy_skill_records_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = install.copy_skill(install.skill_source(), Path(tmp) / "skills", False)
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertTrue((dest / "scripts" / "inventory_hook.py").is_file())
            source = (dest / ".jev-consult-source").read_text(encoding="utf-8")
            self.assertIn("skills", source.replace("\\", "/"))
            self.assertIn("jev-consult", source.replace("\\", "/"))

    def test_upsert_snippet_replaces_marker_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AGENTS.md"
            path.write_text("# keep\n", encoding="utf-8")
            install.upsert_snippet(path, False, install.REPO_SNIPPET)
            install.upsert_snippet(path, False, install.REPO_SNIPPET)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(install.MARKER_START), 1)
            self.assertIn("# keep", text)
            self.assertIn("decision.action", text)
            install.strip_snippet(path, False)
            stripped = path.read_text(encoding="utf-8")
            self.assertNotIn(install.MARKER_START, stripped)
            self.assertIn("# keep", stripped)

    def test_upsert_collapses_duplicate_marker_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "AGENTS.md"
            path.write_text(
                "pre\n" + install.REPO_SNIPPET + "mid\n" + install.REPO_SNIPPET + "post\n",
                encoding="utf-8",
            )
            install.upsert_snippet(path, False, install.REPO_SNIPPET)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(install.MARKER_START), 1)
            self.assertIn("pre", text)
            self.assertIn("mid", text)
            self.assertIn("post", text)
            install.strip_snippet(path, False)
            stripped = path.read_text(encoding="utf-8")
            self.assertNotIn(install.MARKER_START, stripped)
            self.assertIn("pre", stripped)
            self.assertIn("mid", stripped)
            self.assertIn("post", stripped)

    def test_claude_hook_dedup_keeps_foreign_same_named_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            script = Path(tmp) / "jev-consult" / "scripts" / "inventory_hook.py"
            foreign = {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/other/pack/inventory_hook.py",
                    }
                ]
            }
            settings.write_text(
                json.dumps({"hooks": {"UserPromptSubmit": [foreign]}}),
                encoding="utf-8",
            )
            install.upsert_claude_event(
                settings, "UserPromptSubmit", script, "inventory_hook.py", 20, False
            )
            entries = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(entries), 2)
            self.assertIn("/other/pack/inventory_hook.py", json.dumps(entries))
            install.strip_claude_event(settings, "UserPromptSubmit", "inventory_hook.py", False)
            entries = json.loads(settings.read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
            self.assertEqual(entries, [foreign])

    def test_claude_hook_invalid_json_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            script = Path(tmp) / "jev-consult" / "scripts" / "compact_hook.py"
            settings.write_text("not json {", encoding="utf-8")
            out = install.upsert_claude_hook(settings, script, False)
            self.assertTrue(out.startswith("invalid json"))
            self.assertEqual(settings.read_text(encoding="utf-8"), "not json {")
            out = install.strip_claude_hook(settings, False)
            self.assertTrue(out.startswith("invalid json"))
            settings.write_text("[1, 2]", encoding="utf-8")
            out = install.upsert_claude_hook(settings, script, False)
            self.assertTrue(out.startswith("invalid json"))
            self.assertEqual(settings.read_text(encoding="utf-8"), "[1, 2]")


class LiveSummaryTests(unittest.TestCase):
    """install.py --live forwards a doctor --live probe and the summary
    annotates rate-limited harnesses with the reported fallback."""

    def _payload(self, probes, fallback):
        return {
            "ok": False,
            "checks": [],
            "suppressed": 0,
            "absent": [],
            "live": {"probes": probes, "fallback": fallback},
        }

    def test_limited_harness_gets_quota_note_with_fallback(self) -> None:
        data = self._payload(
            {
                "codex": {"status": "limited", "detail": "usage limit"},
                "claude-code": {"status": "available", "detail": "answered"},
            },
            "claude-code",
        )
        text = install._doctor_summary(data, 1)
        self.assertIn(
            "codex: installed (currently rate-limited — same setup works in claude-code)",
            text,
        )
        self.assertNotIn("claude-code: installed (currently", text)

    def test_limited_without_fallback_has_no_recommendation(self) -> None:
        data = self._payload(
            {"grok": {"status": "limited", "detail": "429"}}, None
        )
        text = install._doctor_summary(data, 1)
        self.assertIn("grok: installed (currently rate-limited)", text)
        self.assertNotIn("same setup works", text)

    def test_no_live_key_keeps_summary_unchanged(self) -> None:
        data = {"ok": True, "checks": [], "suppressed": 0, "absent": []}
        self.assertEqual(install._doctor_summary(data, 0), "PASS")

    def test_run_doctor_live_forwards_flag(self) -> None:
        import subprocess as sp

        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return sp.CompletedProcess(cmd, 0, stdout='{"ok": true, "checks": [], "absent": []}', stderr="")

        with patch.object(install.subprocess, "run", fake_run):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = install.run_doctor(["codex"], live=True)
        self.assertEqual(rc, 0)
        self.assertIn("--live", seen["cmd"])
        self.assertIn("codex", seen["cmd"])

    def test_main_live_runs_doctor_after_install(self) -> None:
        import subprocess as sp

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return sp.CompletedProcess(cmd, 0, stdout='{"ok": true, "checks": [], "absent": []}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "USERPROFILE": tmp,
                "HOME": tmp,
                "HERMES_HOME": str(Path(tmp) / "hermes"),
                "TYPESAFE_API_KEY": "",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(
                install.subprocess, "run", fake_run
            ), redirect_stdout(io.StringIO()):
                rc = install.main(["--agents", "codex", "--live"])
        self.assertEqual(rc, 0)
        self.assertTrue(
            any("--live" in cmd for cmd in calls),
            "doctor --live never ran: %r" % calls,
        )


class InstallCoverageTests(unittest.TestCase):
    def test_parse_agents(self) -> None:
        self.assertEqual(install.parse_agents(None), list(install.ALLOWED))
        self.assertEqual(install.parse_agents(""), list(install.ALLOWED))
        self.assertEqual(install.parse_agents("claude-code"), ["claude-code"])
        self.assertEqual(install.parse_agents(" codex , grok "), ["codex", "grok"])
        with self.assertRaises(SystemExit):
            install.parse_agents("cursor")
        with self.assertRaises(SystemExit):
            install.parse_agents("claude-code,copilot")
        with self.assertRaises(SystemExit):
            install.parse_agents(",,")

    def test_targets_layout(self) -> None:
        home = Path("/home/u")
        hermes = Path("/hermes")
        mapping = install.targets(home, hermes)
        self.assertEqual(set(mapping), {"hermes", "claude-code", "codex", "grok"})
        self.assertEqual(mapping["codex"]["skills"], [home / ".codex" / "skills", home / ".agents" / "skills"])
        self.assertEqual(mapping["hermes"]["instructions"], [])
        self.assertIn(home / ".claude" / "CLAUDE.md", mapping["claude-code"]["instructions"])

    def test_env_file_has_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            self.assertFalse(install.env_file_has_key(path))
            path.write_text("# TYPESAFE_API_KEY=abc\nOTHER=1\n", encoding="utf-8")
            self.assertFalse(install.env_file_has_key(path))
            path.write_text("TYPESAFE_API_KEY=\n", encoding="utf-8")
            self.assertFalse(install.env_file_has_key(path))
            path.write_text('TYPESAFE_API_KEY="abc123"\n', encoding="utf-8")
            self.assertTrue(install.env_file_has_key(path))

    def test_key_is_set_and_report_never_prints_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_home.mkdir()
            with patch.object(install, "user_home", return_value=fake_home), patch.object(
                install, "hermes_home", return_value=Path(tmp) / "hermes"
            ), patch.object(install, "repo_root", return_value=Path(tmp) / "repo"), patch.dict(
                os.environ, {"TYPESAFE_API_KEY": ""}, clear=False
            ):
                os.environ.pop("TYPESAFE_API_KEY", None)
                self.assertFalse(install.key_is_set())
                (Path(tmp) / "repo").mkdir()
                (Path(tmp) / "repo" / ".env").write_text(
                    "TYPESAFE_API_KEY=secret123456\n", encoding="utf-8"
                )
                self.assertTrue(install.key_is_set())
                buf = io.StringIO()
                with redirect_stdout(buf):
                    install.report_key()
                self.assertIn("set", buf.getvalue())
                self.assertNotIn("secret123456", buf.getvalue())

    def test_hook_script_marks(self) -> None:
        skill = Path("/s/jev-consult")
        self.assertEqual(install.hook_script(skill), skill / "scripts" / "compact_hook.py")
        self.assertEqual(
            install.hook_script(skill, "inventory_hook.py"),
            skill / "scripts" / "inventory_hook.py",
        )

    def test_hermes_plugin_enable_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            self.assertIn("missing", install.enable_hermes_plugin(config, "jev-compact", False))
            config.write_text("server: {}\n", encoding="utf-8")
            self.assertIn("no plugins.enabled", install.enable_hermes_plugin(config, "jev-compact", False))
            config.write_text("plugins:\n  enabled:\n    - other\n", encoding="utf-8")
            self.assertIn("enable", install.enable_hermes_plugin(config, "jev-compact", True))
            self.assertNotIn("jev-compact", config.read_text(encoding="utf-8"))
            self.assertIn("enabled", install.enable_hermes_plugin(config, "jev-compact", False))
            text = config.read_text(encoding="utf-8")
            self.assertIn("    - jev-compact", text)
            self.assertIn("    - other", text)
            self.assertIn("already enabled", install.enable_hermes_plugin(config, "jev-compact", False))
            self.assertIn("disable", install.disable_hermes_plugin(config, "jev-compact", True))
            self.assertIn("jev-compact", config.read_text(encoding="utf-8"))  # dry run kept it
            self.assertIn("disabled", install.disable_hermes_plugin(config, "jev-compact", False))
            self.assertNotIn("jev-compact", config.read_text(encoding="utf-8"))
            self.assertIn("- other", config.read_text(encoding="utf-8"))
            self.assertIn("not enabled", install.disable_hermes_plugin(config, "jev-compact", False))

    def test_hermes_plugin_scoped_to_enabled_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text(
                "catalog:\n  - jev-compact\nplugins:\n  enabled:\n    - other\n",
                encoding="utf-8",
            )
            self.assertIn("enable", install.enable_hermes_plugin(config, "jev-compact", False))
            text = config.read_text(encoding="utf-8")
            self.assertIn("- jev-compact", text)
            self.assertIn("- other", text)
            config.write_text(
                "plugins:\n  enabled:\n    - jev-compact\n  - jev-compact\nother_key: []\n",
                encoding="utf-8",
            )
            self.assertIn("disabled", install.disable_hermes_plugin(config, "jev-compact", False))
            text = config.read_text(encoding="utf-8")
            self.assertNotIn("    - jev-compact", text)  # enabled entry removed
            self.assertIn("  - jev-compact", text)  # out-of-block entry survives

    def test_env_file_has_key_export_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("export TYPESAFE_API_KEY=abc123\n", encoding="utf-8")
            self.assertTrue(install.env_file_has_key(path))
            path.write_text("OTHER=1\n", encoding="utf-8")
            self.assertFalse(install.env_file_has_key(path))

    def test_strip_codex_event_branches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "hooks.json"
            self.assertIn("missing", install.strip_codex_event(path, "UserPromptSubmit", "inventory_hook.py", False))
            self.assertIn("missing", install.strip_codex_event(path, "UserPromptSubmit", "inventory_hook.py", True))
            path.write_text("not json", encoding="utf-8")
            self.assertIn("invalid json", install.strip_codex_event(path, "UserPromptSubmit", "inventory_hook.py", False))
            path.write_text('{"hooks": {}}', encoding="utf-8")
            self.assertIn("no UserPromptSubmit", install.strip_codex_event(path, "UserPromptSubmit", "inventory_hook.py", False))
            path.write_text('{"hooks": {"UserPromptSubmit": [{"hooks": [{"command": "other.py"}]}]}}', encoding="utf-8")
            self.assertIn("no marker", install.strip_codex_event(path, "UserPromptSubmit", "inventory_hook.py", False))
            # only our entry -> whole file removed
            ours = "/u/.codex/skills/jev-consult/scripts/inventory_hook.py"
            path.write_text(
                json.dumps(
                    {"hooks": {"UserPromptSubmit": [{"hooks": [{"command": ours}]}]}}
                ),
                encoding="utf-8",
            )
            self.assertIn("removed hook file", install.strip_codex_event(path, "UserPromptSubmit", "inventory_hook.py", False))
            self.assertFalse(path.exists())
            # mixed -> event kept with other entries
            path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "UserPromptSubmit": [
                                {"hooks": [{"command": ours}]},
                                {"hooks": [{"command": "keep.py"}]},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            self.assertIn("stripped hook", install.strip_codex_event(path, "UserPromptSubmit", "inventory_hook.py", False))
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(entries), 1)
            self.assertIn("keep.py", json.dumps(entries))

    def test_install_and_uninstall_dry_run_write_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            fake_hermes = Path(tmp) / "hermes"
            with patch.object(install, "user_home", return_value=fake_home), patch.object(
                install, "hermes_home", return_value=fake_hermes
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = install.install(["claude-code"], True)
                self.assertEqual(rc, 0)
                self.assertIn("claude-code skill ->", buf.getvalue())
                self.assertFalse(fake_home.exists())
                buf2 = io.StringIO()
                with redirect_stdout(buf2):
                    rc2 = install.uninstall(["claude-code"], True)
                self.assertEqual(rc2, 0)
                self.assertIn("claude-code remove", buf2.getvalue())
                self.assertFalse(fake_home.exists())

    def test_main_check_key(self) -> None:
        buf = io.StringIO()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fakekey"}), redirect_stdout(buf):
            rc = install.main(["--check-key"])
        self.assertEqual(rc, 0)
        self.assertIn("set", buf.getvalue())
        self.assertNotIn("fakekey", buf.getvalue())

    def test_main_refuses_blocked_agent(self) -> None:
        with self.assertRaises(SystemExit):
            install.main(["--agents", "windsurf"])

    def test_write_repo_instructions_dry_run(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            install.write_repo_instructions(True)
        for name in install.REPO_FILES:
            self.assertIn(name, buf.getvalue())


def sys_exe_slash() -> str:
    import sys

    return sys.executable.replace("\\", "/")


class IdempotentInstallTests(unittest.TestCase):
    def _snapshot(self, base: Path) -> dict:
        return {
            str(p.relative_to(base)): p.read_bytes()
            for p in sorted(base.rglob("*"))
            if p.is_file()
        }

    def test_second_install_is_byte_identical_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = {
                "USERPROFILE": tmp,
                "HOME": tmp,
                "HERMES_HOME": str(base / "hermes"),
            }
            repo_before = {
                n: (ROOT / n).read_bytes()
                for n in ("AGENTS.md", "CLAUDE.md", ".hermes.md")
            }
            with patch.dict(os.environ, env, clear=False):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    install.install(["claude-code"], False)
                first = self._snapshot(base)
                self.assertTrue(first, "install wrote nothing")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    install.install(["claude-code"], False)
                second = self._snapshot(base)
            self.assertEqual(first, second)
            for name, blob in repo_before.items():
                self.assertEqual((ROOT / name).read_bytes(), blob)

    def test_all_four_harnesses_install_to_tmp_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = {
                "USERPROFILE": tmp,
                "HOME": tmp,
                "HERMES_HOME": str(base / "hermes"),
                "TYPESAFE_API_KEY": "",
            }
            with patch.dict(os.environ, env, clear=False):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = install.install(
                        ["hermes", "claude-code", "codex", "grok"], False
                    )
            self.assertEqual(rc, 0)
            expected_skill_dirs = [
                base / "hermes" / "skills" / "jev-consult",
                base / ".claude" / "skills" / "jev-consult",
                base / ".codex" / "skills" / "jev-consult",
                base / ".agents" / "skills" / "jev-consult",
                base / ".grok" / "skills" / "jev-consult",
            ]
            for d in expected_skill_dirs:
                self.assertTrue(
                    (d / "SKILL.md").is_file(), "missing %s" % d
                )
            for doc in (
                base / ".claude" / "CLAUDE.md",
                base / ".codex" / "AGENTS.md",
                base / ".grok" / "AGENTS.md",
            ):
                self.assertTrue(doc.is_file(), "missing %s" % doc)
                self.assertIn("jev-consult", doc.read_text(encoding="utf-8"))

    def test_uninstall_all_four_leaves_tmp_home_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = {
                "USERPROFILE": tmp,
                "HOME": tmp,
                "HERMES_HOME": str(base / "hermes"),
                "TYPESAFE_API_KEY": "",
            }
            agents = ["hermes", "claude-code", "codex", "grok"]
            with patch.dict(os.environ, env, clear=False):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    install.install(agents, False)
                with redirect_stdout(io.StringIO()):
                    rc = install.uninstall(agents, False)
            self.assertEqual(rc, 0)
            for path in sorted(base.rglob("*")):
                if path.is_dir() and path.name == "jev-consult":
                    self.fail("leftover skill dir: %s" % path)
            for doc in (
                base / ".claude" / "CLAUDE.md",
                base / ".codex" / "AGENTS.md",
                base / ".grok" / "AGENTS.md",
            ):
                if doc.is_file():
                    self.assertNotIn(
                        "jev-consult", doc.read_text(encoding="utf-8"),
                        "snippet left in %s" % doc,
                    )

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = {"USERPROFILE": tmp, "HOME": tmp, "HERMES_HOME": str(base / "h")}
            with patch.dict(os.environ, env, clear=False):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    install.install(["claude-code"], True)
            self.assertEqual(self._snapshot(base), {})



class WrapperScriptTests(unittest.TestCase):
    """The documented one-command entrypoints actually run install.py."""

    def _env(self) -> dict:
        env = dict(os.environ)
        env["TYPESAFE_API_KEY"] = env.get("TYPESAFE_API_KEY") or "x"
        return env

    def test_install_cmd_check_key_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("install.cmd is Windows-only")
        import subprocess

        proc = subprocess.run(
            ["cmd", "/c", str(ROOT / "install.cmd"), "--check-key"],
            capture_output=True,
            text=True,
            env=self._env(),
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertIn("TYPESAFE_API_KEY", proc.stdout)
        self.assertNotIn("x" * 20, proc.stdout)  # never prints the key

    def test_install_sh_check_key_posix(self) -> None:
        import shutil
        import subprocess

        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh on this box")
        proc = subprocess.run(
            [sh, str(ROOT / "install.sh"), "--check-key"],
            capture_output=True,
            text=True,
            env=self._env(),
            cwd=str(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])
        self.assertIn("TYPESAFE_API_KEY", proc.stdout)

    def test_install_sh_syntax_clean(self) -> None:
        import shutil
        import subprocess

        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh on this box")
        proc = subprocess.run(
            [sh, "-n", str(ROOT / "install.sh")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[:300])

if __name__ == "__main__":
    unittest.main(verbosity=2)
