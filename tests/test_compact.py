#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import importlib.util
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
