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

ROOT = Path(__file__).resolve().parent.parent


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INV = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory.py", "jev_inventory")
HOOK = load(ROOT / "skills" / "jev-consult" / "scripts" / "inventory_hook.py", "jev_inventory_hook")
INSTALL = load(ROOT / "scripts" / "install.py", "jev_install_tools")
FIXTURE = ROOT / "tests" / "fixtures" / "inventory-harness"


def skip_pick(*_args, **_kwargs):
    return {"status": "skip", "winner": None}


class InventoryHookTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep test runs out of the real ~/.cache decisions log.
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def test_claude_injects_jwt_and_writes_sidecar(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("jwt-auth", note)
            self.assertNotIn("ascii-art", note)
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["harness"], "claude-code")
        names = [row["name"] for row in sidecar["names"]]
        self.assertIn("jwt-auth", names)

    def test_grok_writes_sidecar_without_additional_context(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="grok",
                pick_fn=skip_pick,
            )
            self.assertEqual(out, {})
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["harness"], "grok")
        self.assertIn("jwt-auth", [row["name"] for row in sidecar["names"]])

    def test_codex_injects_jwt_and_writes_sidecar(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="codex",
                pick_fn=skip_pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("jwt-auth", note)
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["harness"], "codex")
        self.assertIn("jwt-auth", [row["name"] for row in sidecar["names"]])

    def test_generic_tokens_do_not_inject(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        out = HOOK.handle(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "skill plugin mcp tool",
            },
            items=items,
            harness="claude-code",
            pick_fn=skip_pick,
        )
        self.assertEqual(out, {})

    def test_post_tool_use_ignored(self) -> None:
        out = HOOK.handle({"hook_event_name": "PostToolUse", "prompt": "Add JWT access tokens in Python"})
        self.assertEqual(out, {})

    def test_jev_winner_injects_skill_relevance(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picked = INV.shortlist(items, "Add JWT access tokens in Python", INV.HOOK_LIMIT, [])
        jwt = next(item for item in picked if item["name"] == "jwt-auth")

        def pick(_task, _harness, _picked):
            return {"status": "winner", "winner": jwt}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertIn("<skill_relevance>", note)
        self.assertIn("jwt-auth", note)
        self.assertNotIn("ascii-art", note)
        self.assertEqual(sidecar["jev_status"], "winner")
        self.assertEqual(sidecar["jev_pick"]["name"], "jwt-auth")

    def test_jev_none_injects_nothing(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)

        def pick(_task, _harness, _picked):
            return {"status": "none", "winner": None}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(out, {})
        self.assertEqual(sidecar["jev_status"], "none")
        self.assertNotIn("jev_pick", sidecar)

    def test_jev_error_fail_open_idf(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)

        def pick(_task, _harness, _picked):
            return {"status": "error", "winner": None}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jwt-auth", note)
        self.assertIn("jev-consult auto tools", note)

    def test_grok_winner_stays_sidecar_only(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picked = INV.shortlist(items, "Add JWT access tokens in Python", INV.HOOK_LIMIT, [])
        jwt = next(item for item in picked if item["name"] == "jwt-auth")

        def pick(_task, _harness, _picked):
            return {"status": "winner", "winner": jwt}

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=items,
                harness="grok",
                pick_fn=pick,
            )
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(out, {})
        self.assertEqual(sidecar["jev_pick"]["name"], "jwt-auth")

    def test_cyrillic_prompt_does_not_write_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "добавь авторизацию",
                    "cwd": tmp,
                },
                items=[],
                harness="claude-code",
                pick_fn=skip_pick,
            )
            miss = Path(tmp) / ".jev-tools-miss.json"
            self.assertEqual(out, {})
            self.assertFalse(miss.is_file())

    def test_latin_empty_shortlist_writes_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": tmp,
                },
                items=[],
                harness="claude-code",
                pick_fn=skip_pick,
            )
            miss = Path(tmp) / ".jev-tools-miss.json"
            self.assertTrue(miss.is_file())
        self.assertIn("from-miss", json.dumps(out))


    def test_explicit_single_mention_wins_without_jev(self) -> None:
        items = [
            {"kind": "skill", "name": "hyperframes", "id": "s_hf"},
            {"kind": "skill", "name": "hyperframes-cli", "id": "s_hfc"},
            {"kind": "skill", "name": "jwt-auth", "id": "s_jwt"},
        ]

        def boom(*_args, **_kwargs):
            self.fail("pick_with_jev must not be called for an explicit mention")

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "use $hyperframes-cli to render this clip",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=boom,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertIn("<skill_relevance>", note)
        self.assertIn("hyperframes-cli", note)
        self.assertEqual(sidecar.get("explicit"), True)
        self.assertEqual(sidecar["jev_pick"]["name"], "hyperframes-cli")

    def test_explicit_two_mentions_fall_back_to_jev(self) -> None:
        items = [
            {"kind": "skill", "name": "hyperframes", "id": "s_hf"},
            {"kind": "skill", "name": "hyperframes-cli", "id": "s_hfc"},
            {"kind": "skill", "name": "jev-consult", "id": "s_jc"},
        ]
        seen: list[list[str]] = []

        def pick(_task, _harness, picked):
            seen.append([item["name"] for item in picked])
            return {"status": "none", "winner": None}

        with tempfile.TemporaryDirectory() as tmp:
            HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "compare hyperframes-cli and jev-consult for this render",
                    "cwd": tmp,
                },
                items=items,
                harness="claude-code",
                pick_fn=pick,
            )
            sidecar = json.loads((Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(len(seen), 1)
        self.assertIn("hyperframes-cli", seen[0])
        self.assertIn("jev-consult", seen[0])
        self.assertNotIn("explicit", sidecar)


class DecisionLogTests(unittest.TestCase):
    def test_log_line_written_without_key_value(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        fake = "apikey_FAKEFAKEFAKEFAKEFAKE_FAKEFAKEFAKEFAKEFAKE"
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            env = {"JEV_CONSULT_LOG": str(log), "TYPESAFE_API_KEY": fake}
            with patch.dict(os.environ, env):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            text = log.read_text(encoding="utf-8")
        self.assertNotIn(fake, text)
        entry = json.loads(text.strip().splitlines()[-1])
        for key in (
            "ts",
            "harness",
            "prompt_sha",
            "prompt_head",
            "n_catalog",
            "shortlist",
            "explicit",
            "jev_status",
            "need",
            "probabilities",
            "winner",
            "strong_pick",
            "latency_ms",
        ):
            self.assertIn(key, entry)
        self.assertEqual(entry["harness"], "claude-code")
        self.assertEqual(entry["jev_status"], "skip")
        self.assertEqual(entry["prompt_head"], "Add JWT access tokens in Python")

    def test_log_disabled_writes_nothing(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            with patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"}):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=skip_pick,
                )
            self.assertFalse(log.exists())

    def test_log_records_jev_status_and_probabilities(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picker = {
            "status": "winner",
            "winner": {"kind": "skill", "name": "jwt-auth"},
            "need": 0.9,
            "probabilities": {"skill_jwt_auth": 0.9, "none": 0.1},
            "latency_ms": 12,
            "strong": True,
        }
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            with patch.dict(os.environ, {"JEV_CONSULT_LOG": str(log)}):
                HOOK.handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "Add JWT access tokens in Python",
                        "cwd": tmp,
                    },
                    items=items,
                    harness="claude-code",
                    pick_fn=lambda *_a, **_k: dict(picker),
                )
            entry = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
            sidecar = json.loads(
                (Path(tmp) / ".jev-tools.json").read_text(encoding="utf-8")
            )
        self.assertEqual(entry["jev_status"], "winner")
        self.assertEqual(entry["winner"], {"kind": "skill", "name": "jwt-auth"})
        self.assertEqual(entry["need"], 0.9)
        self.assertEqual(entry["probabilities"]["skill_jwt_auth"], 0.9)
        self.assertEqual(entry["latency_ms"], 12)
        self.assertTrue(entry["strong_pick"])
        self.assertTrue(sidecar["strong_pick"])


class InventoryScanTests(unittest.TestCase):
    def test_plugin_yaml_is_scanned(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        names = {item["name"] for item in items}
        self.assertIn("demo-plug", names)

    def test_generic_shortlist_empty(self) -> None:
        items = INV.scan("hermes", hermes=FIXTURE)
        picked = INV.shortlist(items, "skill plugin mcp tool", 8, [])
        self.assertEqual(picked, [])


class InstallToolsHookTests(unittest.TestCase):
    def test_claude_user_prompt_hook_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            INSTALL.upsert_claude_event(settings, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            INSTALL.upsert_claude_event(settings, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            data = json.loads(settings.read_text(encoding="utf-8"))
            entries = data["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(entries), 1)
            self.assertIn("inventory_hook.py", json.dumps(entries))

    def test_codex_hooks_json_idempotent_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            hooks.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {"hooks": [{"type": "command", "command": "echo keep"}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            data = json.loads(hooks.read_text(encoding="utf-8"))
            self.assertEqual(len(data["hooks"]["UserPromptSubmit"]), 1)
            blob = json.dumps(data)
            self.assertIn("inventory_hook.py", blob)
            self.assertIn("commandWindows", blob)
            self.assertIn("echo keep", blob)
            INSTALL.strip_codex_event(hooks, "UserPromptSubmit", "inventory_hook.py", False)
            kept = json.loads(hooks.read_text(encoding="utf-8"))
            self.assertNotIn("UserPromptSubmit", kept["hooks"])
            self.assertIn("PreToolUse", kept["hooks"])

    def test_codex_invalid_json_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            hooks.write_text("{not json", encoding="utf-8")
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            msg = INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            self.assertIn("invalid json", msg)
            self.assertEqual(hooks.read_text(encoding="utf-8"), "{not json")

    def test_codex_strip_removes_file_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / "hooks.json"
            script = Path(tmp) / "inventory_hook.py"
            script.write_text("# hook\n", encoding="utf-8")
            INSTALL.upsert_codex_event(hooks, "UserPromptSubmit", script, "inventory_hook.py", 20, False)
            self.assertTrue(hooks.is_file())
            INSTALL.strip_codex_event(hooks, "UserPromptSubmit", "inventory_hook.py", False)
            self.assertFalse(hooks.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
