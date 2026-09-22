#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
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
COMPACT_PATH = ROOT / "skills" / "jev-consult" / "scripts" / "compact.py"


def load():
    spec = importlib.util.spec_from_file_location("jev_compact", COMPACT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


C = load()


def msg(role, text="", uses=None, results=None):
    item = {"role": role, "text": text, "toolUses": uses or []}
    if results:
        item["toolResults"] = results
    return item


def use(uid, tool="Read", inp=None, text="", error=False):
    item = {"tool_use_id": uid, "tool": tool, "input": inp or {}}
    if text:
        item["text"] = text
    if error:
        item["isError"] = True
    return item


def result(uid, text, error=False):
    item = {"tool_use_id": uid, "text": text}
    if error:
        item["isError"] = True
    return item


def drop_asker(_state, questions):
    return {"answers": {name: {"type": "noul", "noul": 0.05} for name in questions}}


def keep_asker(_state, questions):
    return {"answers": {name: {"type": "noul", "noul": 0.95} for name in questions}}


class CompactTests(unittest.TestCase):
    def setUp(self):
        # Keep test runs out of the real ~/.cache spill dir.
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    def test_estimate_tokens_digits(self):
        self.assertEqual(C.estimate_tokens("12345"), 3)

    def test_is_pinned(self):
        self.assertTrue(C.is_pinned(0, 10, 3))
        self.assertTrue(C.is_pinned(9, 10, 3))
        self.assertFalse(C.is_pinned(5, 10, 3))
        self.assertTrue(C.is_pinned(1, 10, 3, keep_first=2))
        self.assertFalse(C.is_pinned(2, 10, 3, keep_first=2))

    def test_collect_tool_calls_keep_first_pins_head(self):
        messages = [
            msg("user", "Fix the failing test. Never edit src/generated."),
            msg("assistant", "", [use("tool-1", "Read", {"file_path": "src/a.ts"})]),
            msg("user", "", results=[result("tool-1", "file contents here")]),
            msg("assistant", "", [use("tool-2", "Read", {"file_path": "src/b.ts"})]),
            msg("user", "", results=[result("tool-2", "other contents")]),
            msg("assistant", "done"),
        ]
        calls = C.collect_tool_calls(messages, preserve_recent=1, keep_first=3)
        pinned = {c.tool_use_id: c.pinned for c in calls}
        self.assertEqual(pinned, {"tool-1": True, "tool-2": False})

    def test_collect_pairs_and_renumbers(self):
        messages = [
            msg("user", "Fix the failing test. Never edit src/generated."),
            msg("assistant", "", [use("tool-1", "Read", {"file_path": "src/a.ts"})]),
            msg("user", "", results=[result("tool-1", "file contents here")]),
            msg("assistant", "done"),
        ]
        calls = C.collect_tool_calls(messages, preserve_recent=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].id, "t1")
        self.assertEqual(calls[0].tool_use_id, "tool-1")
        self.assertEqual(calls[0].tool, "Read")
        self.assertFalse(calls[0].pinned)

    def test_decide_keep_drop(self):
        call = C.ToolCall(
            id="t1",
            tool_use_id="x",
            tool="Read",
            input={},
            call_index=1,
            result_index=2,
            result_chars=10,
            is_error=False,
            pinned=False,
        )
        kept = C.decide_call(call, {"keepCall": 0.9, "keepResult": 0.8}, 0.5)
        self.assertEqual(kept["action"], "keep")
        dropped_result = C.decide_call(call, {"keepCall": 0.9, "keepResult": 0.1}, 0.5)
        self.assertEqual(dropped_result["action"], "drop_result")
        dropped_call = C.decide_call(call, {"keepCall": 0.1, "keepResult": 0.1}, 0.5)
        self.assertEqual(dropped_call["action"], "drop_call")
        call.pinned = True
        pinned = C.decide_call(call, {"keepCall": 0.1, "keepResult": 0.1}, 0.5)
        self.assertEqual(pinned["reason"], "pinned")

    def test_apply_drop_call_removes_pair(self):
        messages = [
            msg("user", "goal"),
            msg("assistant", "", [use("u1", "Read", {"file_path": "a.ts"})]),
            msg("user", "", results=[result("u1", "lots of file text " * 20)]),
            msg("assistant", "ok"),
        ]
        calls = C.collect_tool_calls(messages, 1)
        decisions = [
            {
                "id": "t1",
                "tool": "Read",
                "keepCall": 0.1,
                "keepResult": 0.1,
                "action": "drop_call",
                "reason": "call_dropped",
            }
        ]
        kept = C.apply_decisions(messages, decisions, calls, 300)
        texts = [item.get("text") for item in kept]
        self.assertIn("goal", texts)
        self.assertIn("ok", texts)
        self.assertTrue(all(not (item.get("toolUses") or item.get("toolResults")) for item in kept))

    def test_error_result_stays_when_asker_drops(self):
        messages = [
            msg("user", "fix tests"),
            msg("assistant", "", [use("e1", "Bash", {"command": "pytest"})]),
            msg("user", "", results=[result("e1", "FAILED tests/test_a.py", error=True)]),
            msg("assistant", "looking"),
            msg("user", "continue"),
            msg("assistant", "still"),
            msg("user", "and"),
            msg("assistant", "more"),
            msg("user", "again"),
        ]
        out = C.compact(messages, drop_asker, {"preserve_recent": 2})
        reasons = {item["id"]: item["reason"] for item in out["decisions"]}
        self.assertEqual(reasons.get("t1"), "pinned")
        blob = json.dumps(out["messages"])
        self.assertIn("FAILED tests/test_a.py", blob)

    def test_trace_path_stays(self):
        messages = [
            msg("user", "JWT in src/auth.ts"),
            msg("assistant", "", [use("r1", "Read", {"file_path": "src/auth.ts"})]),
            msg("user", "", results=[result("r1", "export function sign() {}" * 40)]),
            msg("assistant", "ok"),
            msg("user", "next"),
            msg("assistant", "x"),
            msg("user", "y"),
            msg("assistant", "z"),
            msg("user", "w"),
        ]
        out = C.compact(
            messages,
            drop_asker,
            {
                "preserve_recent": 2,
                "trace": {"plan": "JWT auth", "inspected": ["src/auth.ts"], "current_step": "read auth"},
            },
        )
        self.assertEqual(out["decisions"][0]["reason"], "pinned")
        self.assertIn("export function sign()", json.dumps(out["messages"]))

    def test_stale_read_drops(self):
        messages = [
            msg("user", "task"),
            msg("assistant", "", [use("r1", "Read", {"file_path": "old.ts"})]),
            msg("user", "", results=[result("r1", "AAAA" * 200)]),
            msg("assistant", "mid"),
            msg("user", "go"),
            msg("assistant", "x"),
            msg("user", "y"),
            msg("assistant", "z"),
            msg("user", "now"),
        ]
        out = C.compact(messages, drop_asker, {"preserve_recent": 2, "min_reduction": 0})
        self.assertEqual(out["decisions"][0]["action"], "drop_call")
        self.assertNotIn("AAAA", json.dumps(out["messages"]))

    def test_fallback_keeps_original_when_little_saved(self):
        messages = [
            msg("user", "short"),
            msg("assistant", "reply"),
        ]
        out = C.compact_or_keep(messages, drop_asker, {"min_reduction": 0.25})
        self.assertTrue(out["stats"]["fallback"])
        self.assertEqual(len(out["messages"]), 2)

    def test_snake_case_alias(self):
        raw = [
            {"role": "user", "text": "hi", "tool_uses": []},
            {
                "role": "assistant",
                "text": "",
                "tool_uses": [{"tool_use_id": "a", "tool": "Read", "input": {"file_path": "x"}}],
            },
            {
                "role": "user",
                "text": "",
                "tool_results": [{"tool_use_id": "a", "text": "body"}],
            },
            {"role": "assistant", "text": "ok", "tool_uses": []},
        ]
        calls = C.collect_tool_calls([C.normalize_message(item) for item in raw], 1)
        self.assertEqual(calls[0].tool_use_id, "a")

    def test_reduction_ratio(self):
        ratio = C.reduction_ratio({"stats": {"charsBefore": 100, "charsAfter": 40}})
        self.assertAlmostEqual(ratio, 0.6)

    def test_batch_caps_at_sixteen(self):
        calls = [
            C.ToolCall(
                id="t%s" % i,
                tool_use_id="u%s" % i,
                tool="Read",
                input={"file_path": "f%s" % i},
                call_index=i,
                result_index=i,
                result_chars=10,
                is_error=False,
                pinned=False,
            )
            for i in range(20)
        ]
        batches = C.batch_calls(calls, state_tokens=100, max_request_tokens=30000)
        self.assertEqual(len(batches[0]), 16)
        self.assertEqual(sum(len(batch) for batch in batches), 20)

    def test_openai_ingest_pairs_and_keeps_user_text(self):
        raw = [
            {"role": "user", "content": "Fix the failing test. Never edit src/generated."},
            {
                "role": "assistant",
                "content": "I'll read it",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": "{\"file_path\":\"src/generated.ts\"}",
                        },
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "export const x = 1\n" * 40},
            {"role": "assistant", "content": "ok"},
        ]
        result = C.compact(raw, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        texts = [item["text"] for item in result["messages"]]
        self.assertIn("Fix the failing test. Never edit src/generated.", texts)
        self.assertIn("ok", texts)
        self.assertNotIn("export const x = 1", "\n".join(texts))

    def test_claude_content_blocks(self):
        raw = [
            {"role": "user", "content": [{"type": "text", "text": "Keep this goal"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "reading"},
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Read",
                        "input": {"file_path": "a.ts"},
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_1", "content": "NOISE " * 80}
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        result = C.compact(raw, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        blob = json.dumps(result["messages"])
        self.assertIn("Keep this goal", blob)
        self.assertIn("done", blob)
        self.assertNotIn("NOISE", blob)

    def test_hermes_function_role_and_jsonl(self):
        lines = "\n".join(
            [
                json.dumps({"role": "user", "content": "Do not invent a library"}),
                json.dumps(
                    {
                        "role": "assistant",
                        "function_call": {
                            "name": "Read",
                            "arguments": "{\"path\":\"jwt.ts\"}",
                        },
                    }
                ),
                json.dumps(
                    {"role": "function", "name": "Read", "content": "secret body " * 50}
                ),
                json.dumps({"role": "assistant", "content": "next"}),
            ]
        )
        messages = C.parse_transcript(lines)
        result = C.compact(messages, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        blob = json.dumps(result["messages"])
        self.assertIn("Do not invent a library", blob)
        self.assertNotIn("secret body", blob)

    def test_nested_transcript_wrapper(self):
        payload = {
            "transcript": {
                "messages": [
                    {"role": "user", "content": "goal"},
                    {"role": "assistant", "content": "ok"},
                ]
            }
        }
        messages = C.extract_messages(payload)
        self.assertEqual(messages[0]["content"], "goal")

    def test_hermes_request_body_dump(self):
        payload = {
            "session_id": "sess",
            "request": {
                "method": "POST",
                "body": {
                    "model": "x",
                    "messages": [
                        {"role": "user", "content": "Keep this goal"},
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "Read",
                                        "arguments": "{\"file_path\":\"a.ts\"}",
                                    },
                                }
                            ],
                        },
                        {"role": "tool", "tool_call_id": "call_1", "content": "NOISE " * 80},
                        {"role": "assistant", "content": "done"},
                    ],
                },
            },
        }
        result = C.compact(payload, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        blob = json.dumps(result["messages"])
        self.assertIn("Keep this goal", blob)
        self.assertIn("done", blob)
        self.assertNotIn("NOISE", blob)

    def test_claude_jsonl_message_envelope(self):
        raw = [
            {"type": "queue-operation", "operation": "dequeue"},
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": "Keep this goal"}]},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Read",
                            "input": {"file_path": "a.ts"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "NOISE " * 80}
                    ],
                },
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            },
        ]
        result = C.compact(raw, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        blob = json.dumps(result["messages"])
        self.assertIn("Keep this goal", blob)
        self.assertIn("done", blob)
        self.assertNotIn("NOISE", blob)

    def test_codex_response_item(self):
        raw = [
            {"type": "session_meta", "payload": {"id": "s"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Keep this goal"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "c1",
                    "name": "Read",
                    "input": {"path": "a.ts"},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "c1",
                    "output": "NOISE " * 80,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
        ]
        result = C.compact(raw, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        blob = json.dumps(result["messages"])
        self.assertIn("Keep this goal", blob)
        self.assertIn("done", blob)
        self.assertNotIn("NOISE", blob)

    def test_grok_type_and_tool_result(self):
        raw = [
            {"type": "user", "content": "Keep this goal"},
            {
                "type": "assistant",
                "content": "reading",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "Read", "arguments": "{\"path\":\"a.ts\"}"},
                    }
                ],
            },
            {"type": "tool_result", "tool_call_id": "c1", "content": "NOISE " * 80},
            {"type": "assistant", "content": "done"},
        ]
        result = C.compact(raw, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        blob = json.dumps(result["messages"])
        self.assertIn("Keep this goal", blob)
        self.assertIn("done", blob)
        self.assertNotIn("NOISE", blob)

    def test_hermes_responses_input_function_call(self):
        payload = {
            "request": {
                "body": {
                    "model": "x",
                    "input": [
                        {"role": "user", "content": "Keep this goal"},
                        {
                            "type": "function_call",
                            "call_id": "c1",
                            "name": "Read",
                            "arguments": "{\"path\":\"a.ts\"}",
                        },
                        {
                            "type": "function_call_output",
                            "call_id": "c1",
                            "output": "NOISE " * 80,
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        },
                    ],
                }
            }
        }
        result = C.compact(payload, drop_asker, {"preserve_recent": 1, "min_reduction": 0})
        blob = json.dumps(result["messages"])
        self.assertIn("Keep this goal", blob)
        self.assertIn("done", blob)
        self.assertNotIn("NOISE", blob)

    def test_abridge_live_skips_small_and_errors(self):
        self.assertIsNone(C.abridge_live("short"))
        self.assertIsNone(C.abridge_live("x" * (C.LIVE_FAT + 10), is_error=True))
        cut = C.abridge_live("x" * (C.LIVE_FAT + 50))
        self.assertIsNotNone(cut)
        self.assertLess(len(cut), C.LIVE_FAT)

    def test_abridge_live_spills_full_text_and_names_file(self):
        text = "y" * (C.LIVE_FAT + 100)
        with tempfile.TemporaryDirectory() as tmp:
            cut = C.abridge_live(text, spill_dir=Path(tmp))
            files = list(Path(tmp).glob("*.txt"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), text)
            self.assertIn(str(files[0]), cut)
        self.assertIn("full output saved:", cut)
        self.assertLess(len(cut), C.LIVE_FAT)

    def test_spill_dedupes_identical_payloads(self):
        text = "z" * (C.LIVE_FAT + 10)
        with tempfile.TemporaryDirectory() as tmp:
            first = C.spill(text, Path(tmp))
            second = C.spill(text, Path(tmp))
            files = list(Path(tmp).glob("*.txt"))
        self.assertEqual(first, second)
        self.assertEqual(len(files), 1)

    def test_spill_env_path_override_and_disable(self):
        text = "w" * (C.LIVE_FAT + 10)
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": tmp}):
                path = C.spill(text)
                cut = C.abridge_live(text)
            self.assertIsNotNone(path)
            self.assertEqual(path.parent, Path(tmp))
            self.assertIn(str(path), cut)
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"}):
                plain = C.abridge_live(text)
            self.assertNotIn("full output saved", plain)
            self.assertIn("chars omitted", plain)
            self.assertEqual(len(list(Path(tmp).glob("*.txt"))), 1)

    def test_spill_caps_at_max_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            for i in range(205):
                old = folder / ("old%03d.txt" % i)
                old.write_text("x%d" % i, encoding="utf-8")
                os.utime(old, (1000 + i, 1000 + i))
            newest = C.spill("brand new payload", folder)
            files = list(folder.glob("*.txt"))
            self.assertEqual(len(files), C.SPILL_MAX_FILES)
            self.assertIn(newest, files)

    def test_spill_dedupe_touch_survives_cap(self):
        text = "dedupe me " * 4000
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            target = folder / (digest + ".txt")
            target.write_text(text, encoding="utf-8")
            os.utime(target, (100, 100))
            for i in range(200):
                stale = folder / ("stale%03d.txt" % i)
                stale.write_text("x%d" % i, encoding="utf-8")
                os.utime(stale, (200 + i, 200 + i))
            got = C.spill(text, folder)
            files = list(folder.glob("*.txt"))
            self.assertEqual(got, target)
            self.assertTrue(target.exists())
            self.assertEqual(len(files), C.SPILL_MAX_FILES)

    def test_spill_max_bytes_evicts_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            stale = []
            for i in range(3):
                old = folder / ("big%d.txt" % i)
                old.write_text("x" * 60000, encoding="utf-8")
                os.utime(old, (1000 + i, 1000 + i))
                stale.append(old)
            with patch.object(C, "SPILL_MAX_BYTES", 100_000):
                got = C.spill("n" * 40000, folder)
            files = list(folder.glob("*.txt"))
            self.assertIn(got, files)
            self.assertIn(stale[2], files)
            self.assertNotIn(stale[0], files)
            self.assertNotIn(stale[1], files)
            total = sum(p.stat().st_size for p in files)
            self.assertLessEqual(total, 100_000)

    def test_spill_tolerates_file_vanishing_mid_pass(self):
        text = "v" * (C.LIVE_FAT + 10)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            ghost = folder / "ghost.txt"
            ghost.write_text("x", encoding="utf-8")
            real_stat = Path.stat

            def flaky(self, *args, **kwargs):
                if self.parent == folder and self.name == "ghost.txt":
                    raise FileNotFoundError("vanished")
                return real_stat(self, *args, **kwargs)

            with patch.object(Path, "stat", flaky):
                got = C.spill(text, folder)
            self.assertIsNotNone(got)
            self.assertEqual(got.read_text(encoding="utf-8"), text)

    def test_spill_unwritable_dir_falls_back_to_plain_marker(self):
        text = "q" * (C.LIVE_FAT + 10)
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            cut = C.abridge_live(text, spill_dir=blocker / "sub")
        self.assertIsNotNone(cut)
        self.assertNotIn("full output saved", cut)
        self.assertIn("chars omitted", cut)

    def test_truncated_result_text_names_spill_file(self):
        text = "r" * 5000
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": tmp}):
                out = C.truncated_result_text(text, False, 100)
            files = list(Path(tmp).glob("*.txt"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), text)
            self.assertIn(str(files[0]), out)
        self.assertIn("full output saved:", out)

    def test_history_drop_is_not_default(self):
        self.assertEqual(C.main(["missing.json"]), 2)


def _call(**kw):
    base = dict(
        id="t1",
        tool_use_id="u1",
        tool="Read",
        input={},
        call_index=0,
        result_index=1,
        result_chars=10,
        is_error=False,
        pinned=False,
    )
    base.update(kw)
    return C.ToolCall(**base)


class InternalsTests(unittest.TestCase):
    def setUp(self):
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    # --- merge_call_runs -------------------------------------------------
    def test_merge_call_runs_folds_adjacent_same_role(self):
        history = [
            {"i": 1, "role": "assistant", "tool_calls": ["t1 Read a.py -> ok 10ch"]},
            {"i": 2, "role": "assistant", "tool_calls": ["t2 Read b.py -> ok 9ch"]},
            {"i": 3, "role": "user", "text": "next"},
        ]
        out = C.merge_call_runs(history, lambda e: False)
        self.assertEqual(len(out), 2)
        self.assertEqual(len(out[0]["tool_calls"]), 2)
        self.assertEqual(out[1]["role"], "user")

    def test_merge_call_runs_stops_on_text_pinned_role(self):
        base = {"i": 1, "role": "assistant", "tool_calls": ["t1 Read a.py -> ok 10ch"]}
        cases = [
            # second entry carries text -> not foldable
            [{"i": 2, "role": "assistant", "text": "note", "tool_calls": ["t2 x"]}],
            # different role -> no merge
            [{"i": 2, "role": "user", "tool_calls": ["t2 x"]}],
            # calls not a list of str (foldable requires calls[0] str)
            [{"i": 2, "role": "assistant", "tool_calls": [{"id": "t2"}]}],
        ]
        for extra in cases:
            out = C.merge_call_runs([dict(base)] + extra, lambda e: False)
            self.assertEqual(len(out), 2, extra)
        pinned = C.merge_call_runs(
            [dict(base), {"i": 2, "role": "assistant", "tool_calls": ["t2 x"]}],
            lambda e: e["i"] == 1,
        )
        self.assertEqual(len(pinned), 2)

    # --- trace_needles / pin_errors_and_trace ----------------------------
    def test_trace_needles_sources(self):
        self.assertEqual(C.trace_needles(None), [])
        self.assertEqual(C.trace_needles("x"), [])
        trace = {
            "plan": "fix auth",
            "current_step": "edit src/auth.py",
            "last_error": "",
            "last_pick": "return_to_plan",
            "inspected": ["src/main.py", {"file_path": "src/x.py"}, 42, ""],
        }
        needles = C.trace_needles(trace)
        self.assertIn("fix auth", needles)
        self.assertIn("edit src/auth.py", needles)
        self.assertIn("return_to_plan", needles)
        self.assertIn("src/main.py", needles)
        self.assertIn("src/x.py", needles)

    def test_pin_errors_and_trace(self):
        calls = [
            _call(is_error=True),
            _call(id="t2", input={"file_path": "src/auth.py"}),
            _call(id="t3", input={"file_path": "src/other.py"}),
            _call(id="t4", input={"cmd": "ls"}),
            _call(id="t5", pinned=True),
        ]
        C.pin_errors_and_trace(calls, {"current_step": "editing src/auth.py"})
        self.assertTrue(all(c.pinned for c in calls[:2]))
        self.assertFalse(calls[2].pinned)
        self.assertFalse(calls[3].pinned)
        self.assertTrue(calls[4].pinned)

    def test_pin_errors_needle_inside_path(self):
        calls = [_call(input={"file_path": "src/pkg/sub/auth.py"})]
        C.pin_errors_and_trace(calls, {"plan": "src/auth.py"})
        self.assertFalse(calls[0].pinned)
        C.pin_errors_and_trace(calls, {"plan": "auth.py"})
        self.assertTrue(calls[0].pinned)

    # --- goal_from_messages ----------------------------------------------
    def test_goal_from_messages_last_three_user_texts(self):
        messages = [
            msg("user", "first"),
            msg("assistant", "ack"),
            msg("user", "second"),
            msg("user", "", results=[result("u1", "out")]),  # tool carrier, not user text
            msg("user", "third"),
            msg("user", "fourth"),
        ]
        goal = C.goal_from_messages(messages)
        self.assertNotIn("first", goal)
        self.assertEqual(goal, "second\nthird\nfourth")

    # --- questions_for ----------------------------------------------------
    def test_questions_for_shape(self):
        call = _call(id="t7", tool="Bash")
        qs = C.questions_for(call)
        self.assertEqual(set(qs), {"call_t7", "result_t7"})
        self.assertEqual(qs["call_t7"]["type"], "noul")
        self.assertIn("Bash", qs["call_t7"]["instructions"])
        self.assertIn("t7", qs["call_t7"]["instructions"])

    # --- calls_by_message / history_entries -------------------------------
    def test_calls_by_message_groups(self):
        grouped = C.calls_by_message([_call(call_index=0), _call(id="t2", call_index=2), _call(id="t3", call_index=0)])
        self.assertEqual(sorted(grouped), [0, 2])
        self.assertEqual(len(grouped[0]), 2)

    def test_history_entries_skips_empty_and_attaches_calls(self):
        messages = [
            msg("user", "hello"),
            msg("assistant", "", uses=[use("u1")]),
            msg("assistant", ""),  # empty, no calls -> skipped
            msg("tool", "result delivered", results=[result("u1", "x" * 42)]),
        ]
        call = _call(call_index=1, result_index=3, result_chars=42, is_error=True)
        entries = C.history_entries(messages, [call], input_chars=100)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["i"], 0)
        self.assertEqual(entries[1]["tool_calls"][0]["result"], "error, 42 chars (omitted)")
        self.assertNotIn("tool_calls", entries[2])

    # --- small formatters --------------------------------------------------
    def test_input_text_unserializable(self):
        self.assertEqual(C.input_text({"x": object()}, 50), "[unserializable input]")

    def test_result_note_and_compact_call(self):
        ok = _call(tool="Edit", input={"file_path": "a.py", "old": "x\ny"}, result_chars=7)
        self.assertEqual(C.result_note(ok), "ok, 7 chars (omitted)")
        err = _call(is_error=True, result_chars=5)
        self.assertEqual(C.result_note(err), "error, 5 chars (omitted)")
        line = C.compact_call(ok)
        self.assertIn("t1", line)
        self.assertIn("Edit", line)
        self.assertIn("file_path=a.py", line)
        self.assertNotIn("\n", line)
        self.assertTrue(line.endswith("ok 7ch"))

    # --- fit_state ----------------------------------------------------------
    def test_fit_state_full_when_small(self):
        messages = [msg("user", "tiny task")]
        out = C.fit_state(messages, [], {"goal": "g"})
        self.assertEqual(out["stage"], "full")
        self.assertEqual(out["state"]["goal"], "g")
        self.assertEqual(out["state"]["history"][0]["text"], "tiny task")

    def test_fit_state_goal_fallback_to_messages(self):
        out = C.fit_state([msg("user", "the real goal")], [], {})
        self.assertEqual(out["state"]["goal"], "the real goal")

    def test_fit_state_progresses_through_stages(self):
        # PRESERVE_RECENT pins index 0 and the last 6, so the bulky text
        # must sit in the shrinkable middle (i = 1..5 of 12 messages).
        big = "x" * 40000
        messages = (
            [msg("user", "plan")]
            + [msg("assistant", big) for _ in range(5)]
            + [msg("user", "recent %d" % i) for i in range(6)]
        )
        out = C.fit_state(messages, [], {"max_state_tokens": 400})
        self.assertNotEqual(out["stage"], "full")
        self.assertLessEqual(out["tokens"], 400)

    def test_fit_state_raises_when_unfittable(self):
        messages = [
            msg("user", "pinned huge " + "z" * 20000),
            msg("user", "tail"),
        ]
        with self.assertRaises(RuntimeError):
            C.fit_state(messages, [], {"max_state_tokens": 50})

    # --- load_trace / jev_asker ---------------------------------------------
    def test_load_trace(self):
        self.assertIsNone(C.load_trace(None))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(json.dumps({"plan": "x"}), encoding="utf-8")
            self.assertEqual(C.load_trace(str(path)), {"plan": "x"})
            path.write_text("[1,2]", encoding="utf-8")
            self.assertIsNone(C.load_trace(str(path)))

    def test_jev_asker_uses_pack_policy(self):
        seen = {}

        class FakeJev:
            @staticmethod
            def load_policy(path):
                seen["policy_path"] = path
                return {"fake": True}

            @staticmethod
            def post_systemone(state, questions, policy, **kw):
                seen["policy"] = policy
                return {"answers": {}}

        with patch.object(C, "load_jev", return_value=FakeJev):
            C.jev_asker({"a": 1}, {"q": {}})
        self.assertTrue(str(seen["policy_path"]).endswith("policy.json"))
        self.assertEqual(seen["policy"], {"fake": True})


class NormalizeTests(unittest.TestCase):
    def test_map_role(self):
        self.assertEqual(C.map_role("assistant"), "assistant")
        self.assertEqual(C.map_role("model"), "assistant")
        self.assertEqual(C.map_role("AI"), "assistant")
        self.assertEqual(C.map_role("tool"), "tool")
        self.assertEqual(C.map_role("function"), "tool")
        self.assertEqual(C.map_role("tool_result"), "tool")
        self.assertEqual(C.map_role("system"), "system")
        self.assertEqual(C.map_role("human"), "user")
        self.assertEqual(C.map_role(None), "user")

    def test_content_text(self):
        self.assertEqual(C.content_text(None), "")
        self.assertEqual(C.content_text("hi"), "hi")
        self.assertEqual(C.content_text(42), "42")
        self.assertEqual(C.content_text({"text": "x"}), "x")
        self.assertEqual(C.content_text({"content": {"text": "y"}}), "y")
        self.assertEqual(
            C.content_text(
                [
                    "a",
                    {"type": "tool_use", "name": "t"},
                    {"type": "text", "text": "b"},
                    {"type": "text", "content": "nested"},
                ]
            ),
            "a\nb\nnested",
        )
        self.assertEqual(C.content_text(set()), "")

    def test_parse_arguments(self):
        self.assertEqual(C.parse_arguments({"a": 1}), {"a": 1})
        self.assertEqual(C.parse_arguments('{"a": 1}'), {"a": 1})
        self.assertEqual(C.parse_arguments("not json"), {"_raw": "not json"})
        self.assertEqual(C.parse_arguments("[1,2]"), {"_raw": [1, 2]})
        self.assertEqual(C.parse_arguments(""), {})
        self.assertEqual(C.parse_arguments(None), {})

    def test_error_flag(self):
        self.assertTrue(C.error_flag({"isError": True}))
        self.assertTrue(C.error_flag({"is_error": True}))
        self.assertTrue(C.error_flag({"status": "ERROR"}))
        self.assertFalse(C.error_flag({"status": "ok"}))
        self.assertFalse(C.error_flag({}))

    def test_collect_tool_uses_variants(self):
        item = {
            "toolUses": [{"tool_use_id": "a", "tool": "t1"}],
            "tool_calls": [
                {
                    "id": "b",
                    "function": {"name": "t2", "arguments": '{"x": 1}'},
                }
            ],
        }
        uses = C.collect_tool_uses(item)
        self.assertEqual(len(uses), 2)
        self.assertEqual(uses[1]["tool"], "t2")
        self.assertEqual(uses[1]["input"], {"x": 1})

    def test_collect_tool_uses_function_call(self):
        uses = C.collect_tool_uses(
            {"function_call": {"name": "f", "arguments": "{}"}, "tool_call_id": "c1"}
        )
        self.assertEqual(uses[0]["tool_use_id"], "c1")

    def test_collect_tool_results_variants(self):
        item = {
            "toolResults": [{"tool_use_id": "a", "text": "r1"}],
            "role": "tool",
            "tool_call_id": "b",
            "output": "r2",
        }
        results = C.collect_tool_results(item)
        texts = {r["tool_use_id"]: r["text"] for r in results}
        self.assertEqual(texts, {"a": "r1", "b": "r2"})

    def test_collect_tool_results_content_blocks(self):
        item = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "u1", "content": "done"},
                {"type": "text", "text": "hi"},
            ],
        }
        results = C.collect_tool_results(item)
        self.assertEqual(results[0]["tool_use_id"], "u1")
        self.assertEqual(results[0]["text"], "done")

    def test_normalize_tool_use(self):
        tool = C.normalize_tool_use(
            {"call_id": "c", "name": "exec", "arguments": '{"cmd": "ls"}', "text": "t"}
        )
        self.assertEqual(tool["tool_use_id"], "c")
        self.assertEqual(tool["tool"], "exec")
        self.assertEqual(tool["input"], {"cmd": "ls"})
        self.assertEqual(tool["text"], "t")
        self.assertNotIn("isError", tool)
        err = C.normalize_tool_use({"id": "x", "isError": True})
        self.assertTrue(err["isError"])
        self.assertEqual(
            C.normalize_tool_use({"tool_use_id": "p", "input": {"a": 1}})["input"],
            {"a": 1},
        )

    def test_normalize_tool_result(self):
        res = C.normalize_tool_result({"tool_call_id": "c", "text": "out"})
        self.assertEqual(res["tool_use_id"], "c")
        self.assertEqual(res["text"], "out")
        res2 = C.normalize_tool_result({"id": "x", "result": "from-result"})
        self.assertEqual(res2["text"], "from-result")
        res3 = C.normalize_tool_result({"call_id": "y", "output": "o", "is_error": True})
        self.assertTrue(res3["isError"])

    def test_normalize_message_uses_results(self):
        raw = {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "function": {"name": "exec", "arguments": "{}"}}
            ],
            "text": "calling",
        }
        msg = C.normalize_message(raw)
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["toolUses"][0]["tool_use_id"], "c1")

    def test_normalize_message_tool_role_clears_text(self):
        raw = {"role": "tool", "tool_call_id": "c", "text": "output text"}
        msg = C.normalize_message(raw)
        self.assertEqual(msg["role"], "tool")
        self.assertEqual(msg["text"], "")
        self.assertEqual(msg["toolResults"][0]["text"], "output text")

    def test_session_records_to_messages(self):
        self.assertEqual(C.session_records_to_messages({"role": "user", "text": "hi"}), [{"role": "user", "text": "hi"}])
        self.assertEqual(C.session_records_to_messages({"type": "progress"}), [])
        self.assertEqual(
            C.session_records_to_messages(
                {"type": "assistant", "message": {"role": "assistant", "text": "m"}}
            ),
            [{"role": "assistant", "text": "m"}],
        )
        calls = C.session_records_to_messages(
            {"type": "custom_tool_call", "call_id": "c", "name": "exec", "input": {"a": 1}}
        )
        self.assertEqual(calls[0]["role"], "assistant")
        self.assertEqual(calls[0]["tool_calls"][0]["function"]["name"], "exec")
        self.assertIn('"a": 1', calls[0]["tool_calls"][0]["function"]["arguments"])
        out = C.session_records_to_messages(
            {"type": "custom_tool_call_output", "call_id": "c", "output": "ok"}
        )
        self.assertEqual(out[0]["role"], "tool")
        nested = C.session_records_to_messages(
            {"type": "response_item", "payload": {"role": "user", "text": "deep"}}
        )
        self.assertEqual(nested[0]["text"], "deep")
        self.assertEqual(C.session_records_to_messages("raw string"), ["raw string"])

    def test_extract_messages_shapes(self):
        self.assertEqual(C.extract_messages([{"role": "user"}]), [{"role": "user"}])
        self.assertEqual(C.extract_messages({"messages": [{"role": "user"}]}), [{"role": "user"}])
        nested = C.extract_messages({"request": {"body": '[{"role": "user"}]'}})
        self.assertEqual(nested, [{"role": "user"}])
        nested2 = C.extract_messages({"request": {"messages": [{"role": "user"}]}})
        self.assertEqual(nested2, [{"role": "user"}])
        with self.assertRaises(SystemExit):
            C.extract_messages({"other": 1})
        with self.assertRaises(SystemExit):
            C.extract_messages("string")

    def test_parse_transcript_json_and_jsonl(self):
        msgs = C.parse_transcript('[{"role": "user", "text": "a"}]')
        self.assertEqual(len(msgs), 1)
        lines = '{"role": "user", "text": "a"}\n{"role": "assistant", "text": "b"}\n'
        self.assertEqual(len(C.parse_transcript(lines)), 2)
        with self.assertRaises(SystemExit):
            C.parse_transcript("   ")

    def test_input_paths(self):
        inp = {"file_path": "a.py", "path": " b.txt ", "other": "x", "target": ""}
        self.assertEqual(C.input_paths(inp), ["a.py", "b.txt"])

    def test_spill_dir_default(self):
        with patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"}):
            self.assertIsNone(C.spill_dir_default())
        with patch.dict(os.environ, {"JEV_CONSULT_SPILL": "/tmp/spill-x"}):
            self.assertEqual(C.spill_dir_default(), Path("/tmp/spill-x"))
        with patch.dict(os.environ, {"JEV_CONSULT_SPILL": ""}):
            self.assertTrue(str(C.spill_dir_default()).endswith("spill"))

    def test_noul_answer(self):
        self.assertEqual(C.noul_answer({"keep": {"noul": 0.7}}, "keep"), 0.7)
        with self.assertRaises(RuntimeError):
            C.noul_answer({}, "keep")
        with self.assertRaises(RuntimeError):
            C.noul_answer({"keep": "bad"}, "keep")
        with self.assertRaises(RuntimeError):
            C.noul_answer({"keep": {"noul": "x"}}, "keep")

    def test_decide_call_branches(self):
        call = C.ToolCall("id1", "u1", "exec", {}, 0, 0, 10, False, True)
        d = C.decide_call(call, {"keepCall": 0.0, "keepResult": 0.0}, 0.5)
        self.assertEqual(d["reason"], "pinned")
        call2 = C.ToolCall("id2", "u2", "exec", {}, 0, 0, 10, False, False)
        self.assertEqual(
            C.decide_call(call2, {"keepCall": 0.1, "keepResult": 0.9}, 0.5)["action"], "keep"
        )
        self.assertEqual(
            C.decide_call(call2, {"keepCall": 0.9, "keepResult": 0.1}, 0.5)["action"],
            "drop_result",
        )
        self.assertEqual(
            C.decide_call(call2, {"keepCall": 0.1, "keepResult": 0.1}, 0.5)["action"],
            "drop_call",
        )

    def test_message_chars(self):
        msg = {"text": "abcd", "toolUses": [{"input": {"a": 1}}], "toolResults": [{"text": "xyz"}]}
        self.assertEqual(C.message_chars(msg), 4 + len('{"a":1}') + 3)

    def test_count(self):
        decisions = [{"reason": "pinned"}, {"reason": "kept"}, {"reason": "pinned"}]
        self.assertEqual(C._count(decisions, "pinned"), 2)


class CompactCliTests(unittest.TestCase):
    """main()/cmd_compact end-to-end with the built-in --fake asker (no Jev)."""

    def _transcript(self) -> list:
        return [
            {"role": "user", "content": "read the file"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {"file_path": "/x"}}],
            },
            {"role": "tool", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "z" * 400}]},
            {"role": "assistant", "content": "done"},
        ]

    def test_requires_history_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            with patch.object(sys, "stderr", io.StringIO()) as err:
                rc = C.main([str(f), "--fake"])
            self.assertEqual(rc, 2)
            self.assertIn("--history", err.getvalue())

    def test_fake_compact_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main([str(f), "--history", "--fake", "--min-reduction", "0"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
        self.assertIn("messages", out)
        self.assertEqual(out["stats"]["calls"], 1)
        self.assertIn(out["decisions"][0]["action"], ("drop_result", "drop_call", "keep", "kept"))

    def test_jq_prints_one_field_of_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main([str(f), "--history", "--fake", "--min-reduction", "0", "--jq", "stats.calls"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), 1)
            with patch.object(sys, "stderr", io.StringIO()) as err:
                rc = C.main([str(f), "--history", "--fake", "--min-reduction", "0", "--jq", "nope.x"])
            self.assertEqual(rc, 2)
            self.assertIn("bad --jq key", err.getvalue())

    def test_explain_prints_decision_lines_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            err = io.StringIO()
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", err
            ):
                rc = C.main(
                    [str(f), "--history", "--fake", "--min-reduction", "0", "--explain"]
                )
            self.assertEqual(rc, 0)
            lines = [l for l in err.getvalue().splitlines() if l.startswith("explain:")]
            self.assertEqual(len(lines), 1)
            self.assertIn("action=", lines[0])
            self.assertIn("reason=", lines[0])
            self.assertIn("keepCall=", lines[0])

    def test_output_file_and_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_f = Path(tmp) / "out.json"
            stdin = io.StringIO(json.dumps(self._transcript()))
            with patch.object(sys, "stdin", stdin), patch.object(sys, "stdout", io.StringIO()):
                rc = C.main(["-", "--history", "--fake", "-o", str(out_f), "--min-reduction", "0"])
            self.assertEqual(rc, 0)
            out = json.loads(out_f.read_text(encoding="utf-8"))
            self.assertIn("stats", out)

    def test_min_reduction_fallback_restores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main([str(f), "--history", "--fake", "--min-reduction", "0.99"])
            out = json.loads(buf.getvalue())
        self.assertTrue(out["stats"]["fallback"])
        self.assertEqual(out["stats"]["messagesAfter"], out["stats"]["messagesBefore"])

    def test_apply_decisions_no_spill_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            spill_dir = Path(tmp) / "spill"
            long_text = "x" * 1000
            messages = [
                msg("user", "goal"),
                msg("assistant", "", [use("u1", "Read", {"file_path": "a.ts"})]),
                msg("user", "", results=[result("u1", long_text)]),
                msg("assistant", "ok"),
            ]
            calls = C.collect_tool_calls(messages, 0)
            decisions = [
                {
                    "id": calls[0].id,
                    "tool": "Read",
                    "keepCall": 0.9,
                    "keepResult": 0.1,
                    "action": "drop_result",
                    "reason": "result_dropped",
                }
            ]
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": str(spill_dir)}):
                kept = C.apply_decisions(
                    messages, decisions, calls, 300, spill_enabled=False
                )
                blob = json.dumps(kept)
                self.assertIn("fast-jev-compaction truncated", blob)
                self.assertNotIn("full output saved", blob)
                self.assertFalse(spill_dir.exists())
                # control: enabled writes the spill file
                kept2 = C.apply_decisions(
                    messages, decisions, calls, 300, spill_enabled=True
                )
                self.assertIn("full output saved", json.dumps(kept2))
                self.assertTrue(spill_dir.exists() and any(spill_dir.iterdir()))

    def test_dry_run_json_stats_only_and_no_spill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            spill_dir = Path(tmp) / "spill"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf), patch.dict(
                os.environ, {"JEV_CONSULT_SPILL": str(spill_dir)}
            ):
                rc = C.main(
                    [str(f), "--history", "--fake", "--min-reduction", "0",
                     "--dry-run", "--json"]
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["dry_run"])
            self.assertIn("stats", out)
            self.assertNotIn("messages", out)
            stats = out["stats"]
            self.assertTrue(stats["dry_run"])
            self.assertEqual(stats["messagesAfter"], stats["messagesBefore"])
            # dry-run never writes spill payloads
            self.assertFalse(spill_dir.exists() and any(spill_dir.iterdir()))

    def test_dry_run_without_json_still_emits_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main(
                    [str(f), "--history", "--fake", "--min-reduction", "0",
                     "--dry-run"]
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertIn("messages", out)
            self.assertTrue(out["stats"]["dry_run"])

    def test_watch_jq_prints_only_the_named_tick_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf), patch.object(
                    sys, "stderr", io.StringIO()
                ):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0",
                         "--watch", "0.01", "--jq", "fallback"]
                    )
        self.assertEqual(rc, 0)
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertEqual(lines, ["false", "false"])

    def test_watch_emits_stats_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0",
                         "--watch", "0.01"]
                    )
        self.assertEqual(rc, 0)
        ticks = [
            json.loads(l) for l in buf.getvalue().splitlines() if l.startswith("{")
        ]
        self.assertEqual(len(ticks), 2)
        self.assertTrue(all("charsBefore" in t and "reduction" in t for t in ticks))
        self.assertTrue(all(t["fallback"] is False for t in ticks))

    def test_watch_writes_stderr_tick_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            err = io.StringIO()
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    with patch.object(sys, "stderr", err):
                        rc = C.main(
                            [str(f), "--history", "--fake", "--min-reduction", "0",
                             "--watch", "0.01"]
                        )
        self.assertEqual(rc, 0)
        lines = [l for l in err.getvalue().splitlines() if l.startswith("watch tick=")]
        self.assertEqual(len(lines), 2)
        self.assertIn("fallback=False", lines[0])

    def test_watch_rc_1_on_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0.99",
                         "--watch", "0.01"]
                    )
        self.assertEqual(rc, 1)

    def test_watch_fail_fast_breaks_on_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "5"}):
                with patch.object(sys, "stdout", buf):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0.99",
                         "--watch", "0.01", "--fail-fast"]
                    )
            self.assertEqual(rc, 1)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertTrue(ticks[0]["fallback"])

    def test_watch_fail_fast_keeps_running_when_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0",
                         "--watch", "0.01", "--fail-fast"]
                    )
            self.assertEqual(rc, 0)
            ticks = [
                json.loads(l)
                for l in buf.getvalue().splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)

    def test_watch_tick_reports_elapsed_s(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", buf):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0",
                         "--watch", "0.01"]
                    )
            self.assertEqual(rc, 0)
            tick = json.loads(
                next(l for l in buf.getvalue().splitlines() if l.startswith("{"))
            )
            self.assertIsInstance(tick["elapsed_s"], float)
            self.assertGreaterEqual(tick["elapsed_s"], 0.0)

    def test_watch_appends_ticks_to_out_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            out = Path(tmp) / "ticks.jsonl"
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "2"}):
                with patch.object(sys, "stdout", buf):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0",
                         "--watch", "0.01", "--out", str(out)]
                    )
            self.assertEqual(rc, 0)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("reduction" in t for t in lines))

    def test_watch_verdict_writes_final_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0",
                         "--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "ok")
            self.assertEqual(payload["ticks"], 1)
            self.assertFalse(payload["fallback"])
            self.assertIn("reduction", payload)

    def test_watch_verdict_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            with patch.dict(os.environ, {"JEV_COMPACT_WATCH_MAX": "1"}):
                with patch.object(sys, "stdout", io.StringIO()):
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0.99",
                         "--watch", "0.01", "--verdict", str(verdict)]
                    )
            self.assertEqual(rc, 1)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "fallback")
            self.assertTrue(payload["fallback"])

    def test_nonwatch_verdict_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            with patch.object(sys, "stdout", io.StringIO()):
                rc = C.main(
                    [str(f), "--history", "--fake", "--min-reduction", "0",
                     "--verdict", str(verdict)]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "ok")
            self.assertEqual(payload["ticks"], 1)
            self.assertFalse(payload["fallback"])
            self.assertIn("reduction", payload)

    def test_nonwatch_verdict_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            verdict = Path(tmp) / "v.json"
            with patch.object(sys, "stdout", io.StringIO()):
                rc = C.main(
                    [str(f), "--history", "--fake", "--min-reduction", "0.99",
                     "--verdict", str(verdict)]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "fallback")
            self.assertTrue(payload["fallback"])

    def test_check_exits_1_below_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main(
                    [str(f), "--history", "--fake", "--min-reduction", "0.99", "--check"]
                )
            self.assertEqual(rc, 1)
            self.assertIn("check: FAIL", buf.getvalue())
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main(
                    [str(f), "--history", "--fake", "--min-reduction", "0", "--check"]
                )
            self.assertEqual(rc, 0)
            self.assertIn("check: ok", buf.getvalue())
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main(
                    [
                        str(f),
                        "--history",
                        "--fake",
                        "--min-reduction",
                        "0.99",
                        "--check",
                        "--json",
                    ]
                )
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["check"], "FAIL")
            self.assertEqual(out["min_reduction"], 0.99)

    def test_trace_file_loads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(self._transcript()), encoding="utf-8")
            tr = Path(tmp) / ".jev-trace.json"
            tr.write_text('{"plan": "x"}', encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main([str(f), "--history", "--fake", "--trace", str(tr), "--min-reduction", "0"])
            self.assertEqual(rc, 0)
            json.loads(buf.getvalue())

    def test_compact_or_keep_zero_min_reduction(self) -> None:
        asker = lambda state, questions: {"answers": {n: {"type": "noul", "noul": 0.9} for n in questions}}
        result = C.compact_or_keep(self._transcript(), asker, {"min_reduction": 0})
        self.assertFalse(result["stats"]["fallback"])

    def test_parse_transcript_empty_exits(self) -> None:
        with self.assertRaises(SystemExit):
            C.parse_transcript("")


class BatchDirTests(unittest.TestCase):
    def _write_transcript(self, directory: Path, name: str) -> Path:
        p = directory / name
        p.write_text(
            json.dumps([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]),
            encoding="utf-8",
        )
        return p

    def test_dir_requires_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write_transcript(Path(tmp), "a.json")
            with patch.object(sys, "stderr", io.StringIO()) as err:
                rc = C.main(["--dir", tmp])
            self.assertEqual(rc, 2)
            self.assertIn("--history", err.getvalue())

    def test_dir_missing_rc2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys, "stderr", io.StringIO()):
                rc = C.main(["--dir", str(Path(tmp) / "nope"), "--history", "--fake"])
            self.assertEqual(rc, 2)

    def test_dir_processes_json_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write_transcript(d, "a.json")
            self._write_transcript(d, "b.json")
            (d / "skip.txt").write_text("not a transcript", encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main(["--dir", str(d), "--history", "--fake", "--min-reduction", "0"])
            self.assertEqual(rc, 0)
            lines = buf.getvalue().strip().splitlines()
            rows = [json.loads(l) for l in lines[:-1]]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(r["ok"] for r in rows))
            self.assertEqual({r["file"] for r in rows}, {"a.json", "b.json"})
            self.assertTrue(lines[-1].startswith("batch: 2 file(s), 2 ok,"))

    def test_dir_per_file_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write_transcript(d, "good.json")
            (d / "bad.json").write_text("{{{", encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main(["--dir", str(d), "--history", "--fake", "--min-reduction", "0"])
            self.assertEqual(rc, 0)
            rows = [json.loads(l) for l in buf.getvalue().strip().splitlines()[:-1]]
            by_file = {r["file"]: r for r in rows}
            self.assertTrue(by_file["good.json"]["ok"])
            self.assertFalse(by_file["bad.json"]["ok"])

    def test_dir_json_emits_single_result_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self._write_transcript(d, "a.json")
            self._write_transcript(d, "b.json")
            (d / "bad.json").write_text("{{{", encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = C.main(
                    ["--dir", str(d), "--history", "--fake", "--min-reduction", "0", "--json"]
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue().strip())
            self.assertEqual(out["count"], 3)
            self.assertEqual(out["ok"], 2)
            self.assertEqual(len(out["files"]), 3)
            self.assertIn("chars_in", out)
            self.assertIn("chars_out", out)


class StatsFlagTests(unittest.TestCase):
    def _transcript_file(self, tmp: str) -> Path:
        path = Path(tmp) / "t.json"
        path.write_text(
            json.dumps(
                [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
            ),
            encoding="utf-8",
        )
        return path

    def test_stats_prints_summary_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._transcript_file(tmp)
            err = io.StringIO()
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", err
            ):
                rc = C.main(
                    [str(path), "--history", "--fake", "--stats", "--min-reduction", "0"]
                )
            self.assertEqual(rc, 0)
            self.assertIn("stats: kept=", err.getvalue())
            self.assertIn("chars", err.getvalue())

    def test_stats_off_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._transcript_file(tmp)
            err = io.StringIO()
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", err
            ):
                rc = C.main([str(path), "--history", "--fake", "--min-reduction", "0"])
            self.assertEqual(rc, 0)
            self.assertNotIn("stats:", err.getvalue())

    def test_stats_json_prints_stats_dict_to_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._transcript_file(tmp)
            err = io.StringIO()
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", err
            ):
                rc = C.main(
                    [
                        str(path),
                        "--history",
                        "--fake",
                        "--stats-json",
                        "--min-reduction",
                        "0",
                    ]
                )
            self.assertEqual(rc, 0)
            stats = json.loads(err.getvalue())
            self.assertIn("charsBefore", stats)
            self.assertIn("kept", stats)

    def test_report_writes_stats_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._transcript_file(tmp)
            report = Path(tmp) / "report.json"
            with patch.object(sys, "stdout", io.StringIO()):
                rc = C.main(
                    [
                        str(path),
                        "--history",
                        "--fake",
                        "--min-reduction",
                        "0",
                        "--report",
                        str(report),
                    ]
                )
            self.assertEqual(rc, 0)
            stats = json.loads(report.read_text(encoding="utf-8"))
            self.assertIn("charsBefore", stats)
            self.assertIn("kept", stats)
            # unwritable report path -> rc 1, no crash
            bad = Path(tmp) / "no-dir" / "r.json"
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", io.StringIO()
            ):
                rc = C.main(
                    [
                        str(path),
                        "--history",
                        "--fake",
                        "--min-reduction",
                        "0",
                        "--report",
                        str(bad),
                    ]
                )
            self.assertEqual(rc, 1)


class PruneSpillTests(unittest.TestCase):
    def _spill_dir(self, tmp: str) -> Path:
        target = Path(tmp) / "spill"
        target.mkdir()
        return target

    def test_prunes_only_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self._spill_dir(tmp)
            old = target / "old.txt"
            new = target / "new.txt"
            old.write_text("x", encoding="utf-8")
            new.write_text("y", encoding="utf-8")
            old_ts = os.stat(old).st_mtime - 7200
            os.utime(old, (old_ts, old_ts))
            removed = C.prune_spill(target, older_than=3600.0)
            self.assertEqual(removed, [old])
            self.assertTrue(new.is_file())

    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(C.prune_spill(Path("no-such-spill-dir"), 60.0), [])

    def test_cli_prune_spill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = self._spill_dir(tmp)
            stale = target / "stale.txt"
            stale.write_text("x", encoding="utf-8")
            old_ts = os.stat(stale).st_mtime - 7200
            os.utime(stale, (old_ts, old_ts))
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(["--prune-spill", "3600", "--spill-dir", str(target)])
            self.assertEqual(rc, 0)
            self.assertIn("pruned 1 spill files", buf.getvalue())
            self.assertFalse(stale.exists())


class VersionFlagTests(unittest.TestCase):
    def test_version_prints_policy_version(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = C.main(["--version"])
        self.assertEqual(rc, 0)
        self.assertRegex(buf.getvalue().strip(), r"^jev-consult \(policy v\d+\)$")


class ListSpillTests(unittest.TestCase):
    def test_lists_files_with_size_and_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "spill"
            target.mkdir()
            a = target / "a.txt"
            a.write_text("x" * 7, encoding="utf-8")
            (target / "sub").mkdir()
            rows = C.list_spill(target)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0], a)
            self.assertEqual(rows[0][1], 7)
            self.assertGreater(rows[0][2], 0)

    def test_missing_dir_returns_empty(self) -> None:
        self.assertEqual(C.list_spill(Path("no-such-spill-dir")), [])

    def test_cli_list_spill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "spill"
            target.mkdir()
            f = target / "f.txt"
            f.write_text("y" * 5, encoding="utf-8")
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(["--list-spill", "--spill-dir", str(target)])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("%s 5 " % f, out)
            self.assertIn("1 spill files", out)

    def test_cli_verify_spill_checks_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "a.txt"
            existing.write_text("x", encoding="utf-8")
            doc = Path(tmp) / "out.txt"
            doc.write_text(
                "head [fast-jev-compaction truncated 9 chars of this tool result "
                "(error); full output saved: %s]\n"
                "and [truncated 1; full output saved: %s]\n" % (existing, Path(tmp) / "gone.txt"),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(["--verify-spill", str(doc)])
            self.assertEqual(rc, 1)
            out = buf.getvalue()
            self.assertIn("ok %s" % existing, out)
            self.assertIn("missing %s" % (Path(tmp) / "gone.txt"), out)
            self.assertIn("2 refs, 1 missing", out)
            doc.write_text("no refs here", encoding="utf-8")
            with patch("sys.stdout", buf):
                rc = C.main(["--verify-spill", str(doc)])
            self.assertEqual(rc, 0)
            err = io.StringIO()
            with patch("sys.stderr", err):
                rc = C.main(["--verify-spill", str(Path(tmp) / "nope.txt")])
            self.assertEqual(rc, 2)

    def test_cli_orphan_spill_lists_unreferenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "spill"
            target.mkdir()
            used = target / "used.txt"
            free = target / "free.txt"
            used.write_text("u", encoding="utf-8")
            free.write_text("f", encoding="utf-8")
            doc = Path(tmp) / "out.txt"
            doc.write_text(
                "[truncated 1; full output saved: %s]" % used, encoding="utf-8"
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(
                    ["--orphan-spill", str(doc), "--spill-dir", str(target)]
                )
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("orphan: %s" % free, out)
            self.assertNotIn("orphan: %s" % used, out)
            self.assertIn("1 orphans", out)
            err = io.StringIO()
            with patch("sys.stderr", err):
                rc = C.main(["--orphan-spill", str(Path(tmp) / "nope.txt")])
            self.assertEqual(rc, 2)

    def test_cli_verify_spill_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "a.txt"
            existing.write_text("x", encoding="utf-8")
            gone = Path(tmp) / "gone.txt"
            doc = Path(tmp) / "out.txt"
            doc.write_text(
                "[full output saved: %s] [full output saved: %s]"
                % (existing, gone),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(["--verify-spill", str(doc), "--json"])
            self.assertEqual(rc, 1)
            payload = json.loads(buf.getvalue())
            self.assertEqual(len(payload["refs"]), 2)
            self.assertEqual(payload["missing"], [str(gone)])
            self.assertFalse(payload["ok"])

    def test_cli_orphan_spill_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "spill"
            target.mkdir()
            used = target / "used.txt"
            free = target / "free.txt"
            used.write_text("u", encoding="utf-8")
            free.write_text("f", encoding="utf-8")
            doc = Path(tmp) / "out.txt"
            doc.write_text(
                "[full output saved: %s]" % used, encoding="utf-8"
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(
                    ["--orphan-spill", str(doc), "--spill-dir", str(target), "--json"]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["orphans"], [str(free)])

    def test_cli_list_spill_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "spill"
            target.mkdir()
            (target / "a.txt").write_text("x" * 3, encoding="utf-8")
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(
                    ["--list-spill", "--spill-dir", str(target), "--json"]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["files"][0]["size"], 3)

    def test_cli_prune_spill_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "spill"
            target.mkdir()
            stale = target / "a.txt"
            stale.write_text("x" * 3, encoding="utf-8")
            import os as _os
            import time as _time

            old = _time.time() - 10
            _os.utime(stale, (old, old))
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = C.main(
                    [
                        "--prune-spill", "5",
                        "--spill-dir", str(target),
                        "--json",
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["count"], 1)
            self.assertFalse((target / "a.txt").exists())

    def test_cli_list_spill_out_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "spill"
            target.mkdir()
            (target / "f.txt").write_text("y" * 5, encoding="utf-8")
            out_path = Path(tmp) / "list.txt"
            err = io.StringIO()
            with patch("sys.stderr", err):
                rc = C.main(
                    [
                        "--list-spill",
                        "--spill-dir",
                        str(target),
                        "--out",
                        str(out_path),
                    ]
                )
            self.assertEqual(rc, 0)
            text = out_path.read_text(encoding="utf-8")
            self.assertIn("f.txt 5 ", text)
            self.assertIn("1 spill files", text)
            self.assertIn("wrote", err.getvalue())


class KeepTextTests(unittest.TestCase):
    def setUp(self):
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    def _messages(self):
        messages = [msg("user", "compress this session")]
        messages.append(
            msg("assistant", "", [use("k1", "SeekTool", {"file_path": "src/a.ts"})])
        )
        messages.append(msg("user", "", results=[result("k1", "blob " * 500)]))
        for i in range(12):
            messages.append(msg("assistant" if i % 2 else "user", "filler %d" % i))
        return messages

    def _kept_ids(self, out):
        ids = set()
        for item in out["messages"]:
            for tool in item.get("toolUses") or []:
                ids.add(tool["tool_use_id"])
        return ids

    def test_keep_text_pins_matching_tool(self):
        messages = self._messages()
        options = {"min_reduction": 0, "keep_text": "SeekTool"}
        out = C.compact_or_keep(messages, drop_asker, options)
        self.assertIn("k1", self._kept_ids(out))
        self.assertEqual(out["stats"]["callsDropped"], 0)

    def test_keep_text_matches_input_text(self):
        messages = self._messages()
        options = {"min_reduction": 0, "keep_text": "a\\.ts"}
        out = C.compact_or_keep(messages, drop_asker, options)
        self.assertIn("k1", self._kept_ids(out))

    def test_without_keep_text_call_drops(self):
        messages = self._messages()
        out = C.compact_or_keep(messages, drop_asker, {"min_reduction": 0})
        self.assertNotIn("k1", self._kept_ids(out))

    def test_bad_regex_falls_back_to_positional(self):
        messages = self._messages()
        options = {"min_reduction": 0, "keep_text": "["}
        out = C.compact_or_keep(messages, drop_asker, options)
        self.assertNotIn("k1", self._kept_ids(out))


class KeepTextEnvTests(unittest.TestCase):
    def setUp(self):
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    def test_env_keep_text_pins_matching_tool(self):
        import io
        import json as _json
        from contextlib import redirect_stdout

        messages = [{"role": "user", "content": "compress"}]
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "k1",
                        "name": "SeekTool",
                        "input": {"file_path": "src/a.ts"},
                    }
                ],
            }
        )
        messages.append(
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "k1", "content": "blob " * 500}]}
        )
        for i in range(12):
            messages.append({"role": "assistant" if i % 2 else "user", "content": "filler %d" % i})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(_json.dumps(messages), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_KEEP_TEXT": "SeekTool"}):
                with redirect_stdout(buf):
                    rc = C.main(
                        [str(path), "--history", "--fake", "--min-reduction", "0"]
                    )
            self.assertEqual(rc, 0)
            out = _json.loads(buf.getvalue())
            ids = {
                t["tool_use_id"]
                for m in out["messages"]
                for t in (m.get("toolUses") or [])
            }
            self.assertIn("k1", ids)


class PreserveRecentEnvTests(unittest.TestCase):
    def setUp(self):
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    def _run(self, path, env):
        import io
        import json as _json
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with patch.dict(os.environ, env):
            with redirect_stdout(buf):
                rc = C.main(
                    [str(path), "--history", "--fake", "--min-reduction", "0"]
                )
        self.assertEqual(rc, 0)
        return _json.loads(buf.getvalue())

    def test_keep_threshold_env_default(self):
        import json as _json

        messages = [{"role": "user", "content": "compress"}]
        for i in range(4):
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t%d" % i,
                            "name": "Read",
                            "input": {"file_path": "f%d.py" % i},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t%d" % i,
                            "content": "blob " * 200,
                        }
                    ],
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(_json.dumps(messages), encoding="utf-8")
            bogus = self._run(path, {"JEV_KEEP_THRESHOLD": "bogus"})
            self.assertGreater(bogus["stats"]["messagesBefore"], 0)

    def test_truncate_head_env_default(self):
        import json as _json

        messages = [{"role": "user", "content": "compress"}]
        for i in range(4):
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t%d" % i,
                            "name": "Read",
                            "input": {"file_path": "f%d.py" % i},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t%d" % i,
                            "content": "blob " * 200,
                        }
                    ],
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(_json.dumps(messages), encoding="utf-8")
            bogus = self._run(path, {"JEV_TRUNCATE_HEAD": "bogus"})
            self.assertGreater(bogus["stats"]["messagesBefore"], 0)

    def test_min_reduction_env_default(self):
        import json as _json

        messages = [{"role": "user", "content": "compress"}]
        for i in range(4):
            messages.append({"role": "user", "content": "filler %d" % i})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(_json.dumps(messages), encoding="utf-8")
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with patch.dict(os.environ, {"JEV_MIN_REDUCTION": "bogus"}):
                with redirect_stdout(buf):
                    rc = C.main([str(path), "--history", "--fake"])
            self.assertEqual(rc, 0)
            self.assertIn("stats", json.loads(buf.getvalue()))

    def test_env_zero_pins_only_first(self):
        import json as _json

        messages = [{"role": "user", "content": "compress"}]
        for i in range(10):
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t%d" % i,
                            "name": "Read",
                            "input": {"file_path": "f%d.py" % i},
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t%d" % i,
                            "content": "blob " * 200,
                        }
                    ],
                }
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(_json.dumps(messages), encoding="utf-8")
            zero = self._run(path, {"JEV_PRESERVE_RECENT": "0"})
            default = self._run(path, {"JEV_PRESERVE_RECENT": ""})
        self.assertLess(len(zero["messages"]), len(default["messages"]))
        self.assertEqual(
            zero["stats"]["preserve_recent"] if "preserve_recent" in zero["stats"] else 0, 0
        )


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    def test_dry_run_returns_original_messages(self):
        import io
        import json as _json
        from contextlib import redirect_stdout

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.json"
            path.write_text(_json.dumps(messages), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = C.main(
                    [
                        str(path),
                        "--history",
                        "--fake",
                        "--min-reduction",
                        "0",
                        "--dry-run",
                    ]
                )
            self.assertEqual(rc, 0)
            out = _json.loads(buf.getvalue())
            self.assertTrue(out["stats"]["dry_run"])
            self.assertEqual(len(out["messages"]), len(messages))


class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import time as _time

        transcript = [
            {"role": "user", "content": "read the file"},
            {"role": "assistant", "content": "done"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.json"
            f.write_text(json.dumps(transcript), encoding="utf-8")
            buf = io.StringIO()
            with patch.dict(
                os.environ,
                {"JEV_COMPACT_WATCH_MAX": "0", "JEV_COMPACT_WATCH_SECS": "0.05"},
            ):
                with patch.object(sys, "stdout", buf):
                    start = _time.time()
                    rc = C.main(
                        [str(f), "--history", "--fake", "--min-reduction", "0",
                         "--watch", "0.02"]
                    )
            self.assertEqual(rc, 0)
            self.assertLess(_time.time() - start, 2.0)
            ticks = [
                l for l in buf.getvalue().splitlines() if l.startswith("{")
            ]
            self.assertLessEqual(len(ticks), 10)
            self.assertGreaterEqual(len(ticks), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
