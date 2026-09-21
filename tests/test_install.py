#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import inspect
import json
import tempfile
import unittest
from pathlib import Path

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
            script = Path(tmp) / "inventory_hook.py"
            other = Path(tmp) / "compact_hook.py"
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


def sys_exe_slash() -> str:
    import sys

    return sys.executable.replace("\\", "/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
