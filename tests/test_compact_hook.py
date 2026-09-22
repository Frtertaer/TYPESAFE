#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "scripts" / "install.py"
HOOK_PATH = ROOT / "skills" / "jev-consult" / "scripts" / "compact_hook.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INSTALL = load(INSTALL_PATH, "jev_install")
HOOK = load(HOOK_PATH, "jev_compact_hook")


class HookTests(unittest.TestCase):
    def setUp(self):
        # Keep test runs out of the real ~/.cache spill dir.
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    def test_small_result_unchanged(self):
        out = HOOK.handle({"hook_event_name": "PostToolUse", "tool_response": "ok"})
        self.assertEqual(out, {})

    def test_error_not_abridged(self):
        fat = "x" * 40000
        out = HOOK.handle({"hook_event_name": "PostToolUse", "tool_response": fat, "is_error": True})
        self.assertEqual(out, {})

    def test_fat_result_replaced(self):
        fat = "HEAD" + ("n" * 40000) + "TAIL"
        out = HOOK.handle({"hook_event_name": "PostToolUse", "tool_response": fat})
        replaced = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("HEAD", replaced)
        self.assertIn("TAIL", replaced)
        self.assertIn("omitted", replaced)
        self.assertLess(len(replaced), len(fat))

    def test_fat_result_spills_to_disk_and_marker_names_file(self):
        fat = "HEAD" + ("n" * 40000) + "TAIL"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": tmp}):
                out = HOOK.handle(
                    {"hook_event_name": "PostToolUse", "tool_response": fat}
                )
            files = list(Path(tmp).glob("*.txt"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), fat)
            replaced = out["hookSpecificOutput"]["updatedToolOutput"]
            self.assertIn("full output saved:", replaced)
            self.assertIn(str(files[0]), replaced)
            self.assertLess(len(replaced), len(fat))

    def test_grok_toolresult_shape_kept(self):
        fat = "HEAD" + ("n" * 40000) + "TAIL"
        original = {"type": "Bash", "output_for_prompt": fat, "exit_code": 0}
        out = HOOK.handle({"hook_event_name": "PostToolUse", "toolResult": original})
        replaced = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(replaced["type"], "Bash")
        self.assertEqual(replaced["exit_code"], 0)
        self.assertLess(len(replaced["output_for_prompt"]), len(fat))

    def test_truncated_flag_skips(self):
        out = HOOK.handle(
            {
                "hook_event_name": "PostToolUse",
                "toolResultTruncated": True,
                "toolResult": "x" * 40000,
            }
        )
        self.assertEqual(out, {})


class InstallHookTests(unittest.TestCase):
    def test_claude_hook_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PostToolUse": [
                                {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "keep-me"}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            script = Path(tmp) / "jev-consult" / "scripts" / "compact_hook.py"
            INSTALL.upsert_claude_hook(settings, script, dry_run=False)
            INSTALL.upsert_claude_hook(settings, script, dry_run=False)
            data = json.loads(settings.read_text(encoding="utf-8"))
            posts = data["hooks"]["PostToolUse"]
            self.assertEqual(len(posts), 2)
            self.assertEqual(posts[0]["matcher"], "Write|Edit")
            self.assertIn("compact_hook.py", json.dumps(posts[1]))

    def test_enable_plugin_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled:\n    - disk-cleanup\n", encoding="utf-8")
            INSTALL.enable_hermes_plugin(config, "jev-compact", dry_run=False)
            INSTALL.enable_hermes_plugin(config, "jev-compact", dry_run=False)
            text = config.read_text(encoding="utf-8")
            self.assertEqual(text.count("- jev-compact"), 1)
            INSTALL.disable_hermes_plugin(config, "jev-compact", dry_run=False)
            self.assertNotIn("jev-compact", config.read_text(encoding="utf-8"))


class HookE2ETests(unittest.TestCase):
    """Subprocess e2e: stdin payload -> stdout JSON, no Jev key needed."""

    def _run(self, stdin_text: str, home: str | None = None, spill: str | None = None):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        if home:
            env["USERPROFILE"] = home
            env["HOME"] = home
            env["HERMES_HOME"] = str(Path(home) / ".hermes")
        if spill:
            env["JEV_CONSULT_SPILL"] = spill
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_empty_and_bad_stdin(self) -> None:
        self.assertEqual(json.loads(self._run("")), {})
        self.assertEqual(json.loads(self._run("[1,2]")), {})

    def test_non_post_event_noop(self) -> None:
        out = json.loads(self._run(json.dumps({"hook_event_name": "UserPromptSubmit", "toolResult": "x"})))
        self.assertEqual(out, {})

    def test_fat_toolresult_abridged_and_spilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            spill = Path(tmp) / "spill"
            home.mkdir()
            fat = "A" * 40000
            out = json.loads(
                self._run(
                    json.dumps(
                        {
                            "hook_event_name": "PostToolUse",
                            "toolResult": {"output": fat},
                        }
                    ),
                    home=str(home),
                    spill=str(spill),
                )
            )
            node = out["hookSpecificOutput"]["updatedToolOutput"]
            self.assertLess(len(node["output"]), len(fat))
            self.assertIn("omitted", node["output"])
            self.assertTrue(any(spill.iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
