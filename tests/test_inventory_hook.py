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


_MISSING = object()

PICKED = [
    {"id": "skill:alpha", "kind": "skill", "name": "alpha", "description": "does alpha"},
    {"id": "skill:beta", "kind": "skill", "name": "beta", "description": "does beta"},
]


def fake_jev(answers=None, post_exc=None, key_exc=None, decide_exc=None, decide_ret=None):
    class M:
        @staticmethod
        def load_api_key():
            if key_exc is not None:
                raise key_exc
            return "k"

        @staticmethod
        def load_policy(path=None):
            return {}

        @staticmethod
        def post_systemone(state, questions, policy, **kwargs):
            if post_exc is not None:
                raise post_exc
            return {"answers": answers, "model": "fake-0"}

        @staticmethod
        def decide(ans, policy, irreversible=False):
            if decide_exc is not None:
                raise decide_exc
            if decide_ret is not None:
                return decide_ret
            return {"action": "proceed", "picks": {}, "probabilities": {}}

    return M


class PickWithJevTests(unittest.TestCase):
    """The real pick_with_jev fail-open branches (handle() tests inject pick_fn)."""

    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)
        self._had_jev = sys.modules.get("jev", _MISSING)
        self.addCleanup(self._restore_jev)

    def _restore_jev(self) -> None:
        if self._had_jev is _MISSING:
            sys.modules.pop("jev", None)
        else:
            sys.modules["jev"] = self._had_jev

    def test_empty_shortlist_never_imports_jev(self) -> None:
        sys.modules["jev"] = None  # would raise ImportError if reached
        out = HOOK.pick_with_jev("task", "hermes", [])
        self.assertEqual(out, {"status": "empty", "winner": None})

    def test_jev_import_failure_is_error(self) -> None:
        sys.modules["jev"] = None
        out = HOOK.pick_with_jev("task", "hermes", PICKED)
        self.assertEqual(out, {"status": "error", "winner": None})

    def test_missing_api_key_is_skip(self) -> None:
        sys.modules["jev"] = fake_jev(key_exc=SystemExit(2))
        out = HOOK.pick_with_jev("task", "hermes", PICKED)
        self.assertEqual(out["status"], "skip")

    def test_post_exception_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(post_exc=RuntimeError("down"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_post_systemexit_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(post_exc=SystemExit(3))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_non_dict_answers_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(answers="nope")
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_decide_exception_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(decide_exc=ValueError("bad answers"))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_decide_systemexit_is_error(self) -> None:
        sys.modules["jev"] = fake_jev(decide_exc=SystemExit(1))
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "error")

    def test_escalate_decision_maps_to_escalate(self) -> None:
        sys.modules["jev"] = fake_jev(decide_ret={"action": "escalate", "picks": {}, "probabilities": {}})
        self.assertEqual(HOOK.pick_with_jev("t", "hermes", PICKED)["status"], "escalate")

    def test_none_pick_maps_to_none(self) -> None:
        sys.modules["jev"] = fake_jev(
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "none", "need_skill": 0.8},
                "probabilities": {},
            }
        )
        out = HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "none")
        self.assertIsNone(out["winner"])

    def test_strong_winner_sets_fields(self) -> None:
        sys.modules["jev"] = fake_jev(
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "skill:beta", "need_skill": 0.9},
                "probabilities": {"load_tools": {"skill:beta": 0.95}},
            }
        )
        out = HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertEqual(out["status"], "winner")
        self.assertEqual(out["winner"]["name"], "beta")
        self.assertTrue(out["strong"])
        self.assertEqual(out["need"], 0.9)
        self.assertEqual(out["probabilities"], {"skill:beta": 0.95})
        self.assertIsInstance(out["latency_ms"], int)

    def test_non_numeric_need_still_attached_as_none(self) -> None:
        sys.modules["jev"] = fake_jev(
            decide_ret={
                "action": "proceed",
                "picks": {"load_tools": "skill:beta", "need_skill": "high"},
                "probabilities": {"load_tools": {"skill:beta": 0.5}},
            }
        )
        out = HOOK.pick_with_jev("t", "hermes", PICKED)
        self.assertIsNone(out["need"])
        self.assertIn(out["status"], {"winner", "idf", "escalate"})


class MainLoopTests(unittest.TestCase):
    """stdin/stdout edge cases for the hook entrypoint."""

    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)

    def run_main(self, stdin_text: str) -> str:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with patch("sys.stdin", io.StringIO(stdin_text)), redirect_stdout(buf):
            rc = HOOK.main()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_empty_stdin_prints_empty_object(self) -> None:
        self.assertEqual(self.run_main(""), "{}\n")

    def test_bad_json_prints_empty_object(self) -> None:
        self.assertEqual(self.run_main("{not json"), "{}\n")

    def test_non_dict_payload_prints_empty_object(self) -> None:
        self.assertEqual(self.run_main("[1, 2]"), "{}\n")

    def test_handle_exception_still_prints_object(self) -> None:
        with patch.object(HOOK, "handle", side_effect=RuntimeError("boom")):
            out = self.run_main('{"prompt": "x"}')
        self.assertEqual(json.loads(out), {})


class HandleBranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._log_env = patch.dict(os.environ, {"JEV_CONSULT_LOG": "0"})
        self._log_env.start()
        self.addCleanup(self._log_env.stop)
        self.items = INV.scan("hermes", hermes=FIXTURE)

    def test_extract_prompt_variants(self) -> None:
        self.assertEqual(HOOK.extract_prompt({"prompt": " p "}), "p")
        self.assertEqual(HOOK.extract_prompt({"user_message": "u"}), "u")
        self.assertEqual(HOOK.extract_prompt({"userMessage": "m"}), "m")
        self.assertEqual(
            HOOK.extract_prompt(
                {
                    "conversation_history": [
                        {"role": "assistant", "content": "old"},
                        {"role": "user", "content": " latest "},
                    ]
                }
            ),
            "latest",
        )
        self.assertEqual(
            HOOK.extract_prompt({"messages": [{"role": "user", "text": "t1"}]}), "t1"
        )
        self.assertEqual(
            HOOK.extract_prompt({"messages": [{"role": "user", "message": "mm"}]}), "mm"
        )
        self.assertEqual(HOOK.extract_prompt({}), "")
        self.assertEqual(HOOK.extract_prompt({"prompt": "  "}), "")
        self.assertEqual(
            HOOK.extract_prompt({"conversation_history": [{"role": "assistant", "content": "a"}]}),
            "",
        )

    def test_extract_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(HOOK.extract_cwd({"cwd": tmp}), Path(tmp))
            self.assertEqual(HOOK.extract_cwd({"cwd_path": tmp}), Path(tmp))
            self.assertEqual(HOOK.extract_cwd({"workspace": tmp}), Path(tmp))
        self.assertIsNone(HOOK.extract_cwd({"cwd": "/no/such/dir-xyz"}))
        self.assertIsNone(HOOK.extract_cwd({}))

    def test_redact_prompt_wiring(self) -> None:
        class FakeJev:
            @staticmethod
            def redact(text):
                return text.replace("secret", "[R]")

        with patch.dict(sys.modules, {"jev": FakeJev}):
            self.assertEqual(HOOK._redact_prompt("a secret"), "a [R]")
        with patch.dict(sys.modules, {"jev": None}):
            self.assertEqual(HOOK._redact_prompt("a secret"), "a secret")

    def test_unknown_event_and_empty_prompt(self) -> None:
        self.assertEqual(
            HOOK.handle(
                {"hook_event_name": "PostToolUse", "prompt": "jwt"},
                items=self.items,
                harness="claude-code",
            ),
            {},
        )
        self.assertEqual(
            HOOK.handle({"prompt": "  "}, items=self.items, harness="claude-code"), {}
        )

    def test_explicit_single_mention_wins(self) -> None:
        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "run $jwt-auth now"},
            items=self.items,
            harness="claude-code",
            pick_fn=lambda *a, **k: self.fail("picker must not run on explicit hit"),
        )
        note = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jwt-auth", note)
        self.assertIn("skill_relevance", note)  # winner note, not the list note

    def test_pick_fn_exception_falls_back_to_note(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("down")

        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=boom,
        )
        note = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jwt-auth", note)

    def test_pick_fn_non_dict_keeps_idf(self) -> None:
        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=lambda *_a, **_k: "garbage",
        )
        self.assertIn("jwt-auth", out["hookSpecificOutput"]["additionalContext"])

    def test_miss_written_when_no_picks_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "zebra quokka xylophone",
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertTrue((Path(tmp) / INV.MISS_NAME).is_file())
            miss = json.loads((Path(tmp) / INV.MISS_NAME).read_text(encoding="utf-8"))
            self.assertEqual(miss["harness"], "claude-code")
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("peer_fill.py", note)

    def test_no_cwd_miss_note_still_returned(self) -> None:
        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "zebra quokka xylophone"},
            items=self.items,
            harness="claude-code",
            pick_fn=skip_pick,
        )
        self.assertIn("peer_fill.py", out["hookSpecificOutput"]["additionalContext"])

    def test_no_tokens_no_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "a a a",  # tokens() drops <3 chars
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertEqual(out, {})
            self.assertFalse((Path(tmp) / INV.MISS_NAME).exists())

    def test_picked_clears_existing_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            miss = Path(tmp) / INV.MISS_NAME
            miss.write_text('{"stale": true}', encoding="utf-8")
            HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT tokens please",
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=skip_pick,
            )
            self.assertFalse(miss.exists())

    def test_hermes_returns_context(self) -> None:
        out = HOOK.handle(
            {"prompt": "Add JWT tokens please"},
            items=self.items,
            harness="hermes",
            pick_fn=skip_pick,
        )
        self.assertIn("context", out)
        self.assertIn("jwt-auth", out["context"])

    def test_pre_llm_call_returns_context(self) -> None:
        out = HOOK.handle(
            {"event": "pre_llm_call", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=skip_pick,
        )
        self.assertIn("context", out)

    def test_grok_emits_nothing(self) -> None:
        out = HOOK.handle(
            {"prompt": "Add JWT tokens please"},
            items=self.items,
            harness="grok",
            pick_fn=skip_pick,
        )
        self.assertEqual(out, {})

    def test_winner_pick_writes_sidecar_fields(self) -> None:
        def win(*_a, **_k):
            return {
                "status": "winner",
                "winner": {"kind": "skill", "name": "jwt-auth"},
                "strong": True,
                "need": 0.9,
                "probabilities": {"p_jwt_auth": 0.8},
                "latency_ms": 12,
            }

        with tempfile.TemporaryDirectory() as tmp:
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT tokens please",
                    "cwd": tmp,
                },
                items=self.items,
                harness="claude-code",
                pick_fn=win,
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("skill_relevance", note)
            sidecar = json.loads((Path(tmp) / INV.SIDECAR_NAME).read_text(encoding="utf-8"))
        self.assertTrue(sidecar["strong_pick"])
        self.assertEqual(sidecar["jev_pick"], {"kind": "skill", "name": "jwt-auth"})
        self.assertEqual(sidecar["jev_status"], "winner")

    def test_none_pick_returns_empty(self) -> None:
        def none_pick(*_a, **_k):
            return {"status": "none", "winner": None}

        out = HOOK.handle(
            {"hook_event_name": "UserPromptSubmit", "prompt": "Add JWT tokens please"},
            items=self.items,
            harness="claude-code",
            pick_fn=none_pick,
        )
        self.assertEqual(out, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
