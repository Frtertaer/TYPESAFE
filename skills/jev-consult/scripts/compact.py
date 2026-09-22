#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LIVE_FAT abridge of the current fat tool result. Session-history drop is opt-in.

Default path: compact_hook / Hermes transform_tool_result cut walls of
text above LIVE_FAT. That is not Tamara retention and not a watchdog.

`compact.py --history` still has the Python port of
https://github.com/tamaratran/fast-jev-compaction (MIT). Hermes eval
(Teknium, PR discussion 2026-09-20) did not adopt that retention rule:
it drops old tool calls, breaks cache, and loses to production summary.
Do not run --history as the default. No second-LLM summary. No resync.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402

STATE_CONTEXT = (
    "A coding assistant conversation is being compacted to free context. "
    "`history` is the whole conversation so far, oldest first; tool outputs "
    "are replaced by a short `result` note and long texts may be abridged. "
    "Each question asks whether one tool call, or the full output of that "
    "call, still needs to stay in the history verbatim. Whatever is not kept "
    "is deleted permanently, but the assistant can always re-run a tool or "
    "re-read a file."
)
INPUT_CHARS = (1000, 200, 60)
TEXT_HEAD = 400
TEXT_TAIL = 150
KEEP_THRESHOLD = 0.5
PRESERVE_RECENT = 6
MAX_STATE_TOKENS = 25000
MAX_REQUEST_TOKENS = 30000
TRUNCATE_HEAD_CHARS = 300
MIN_REDUCTION = 0.25
MAX_CALLS_PER_BATCH = 16
PATH_KEYS = ("file_path", "path", "filename", "target")
TOKEN_PIECES = re.compile(r"[A-Za-z]+|\d+|[^ \t\n\r\f\vA-Za-z\d]")
DUMP = {"separators": (",", ":"), "ensure_ascii": False}


def _dumps(value: Any) -> str:
    return json.dumps(value, **DUMP)


def estimate_tokens(text: str) -> int:
    tokens = 0.0
    for piece in TOKEN_PIECES.findall(text):
        first = ord(piece[0])
        if 48 <= first <= 57:
            tokens += len(piece) / 2.0
        elif (65 <= first <= 90) or (97 <= first <= 122):
            tokens += 1 + (len(piece) - 1) // 6
        else:
            tokens += 0.9
    return math.ceil(tokens)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def abridge(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail + 40:
        return text
    omitted = len(text) - head - tail
    return "%s\n[… %s chars omitted …]\n%s" % (text[:head], omitted, text[-tail:])


# Live mutate: only walls of text. Normal file reads stay intact.
LIVE_FAT = 32000
LIVE_HEAD = 6000
LIVE_TAIL = 2000
SPILL_MAX_FILES = 200
SPILL_MAX_BYTES = 256 * 1024 * 1024


def spill_dir_default() -> Path | None:
    override = os.environ.get("JEV_CONSULT_SPILL", "").strip()
    if override == "0":
        return None
    if override:
        return Path(override)
    return Path.home() / ".cache" / "jev-consult" / "spill"


def spill(text: str, spill_dir: Path | None = None) -> Path | None:
    """Content-addressed copy of the omitted payload; None when disabled/failed."""
    target = spill_dir if spill_dir is not None else spill_dir_default()
    if target is None:
        return None
    try:
        target.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        path = target / (digest + ".txt")
        if path.exists():
            path.touch()
        else:
            tmp = target / (digest + ".tmp.%d" % os.getpid())
            try:
                tmp.write_text(text, encoding="utf-8")
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, path)
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        entries: list[tuple[float, int, Path]] = []
        total_size = 0
        for candidate in target.iterdir():
            try:
                info = candidate.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            entries.append((info.st_mtime, info.st_size, candidate))
            total_size += info.st_size
        if len(entries) > SPILL_MAX_FILES or total_size > SPILL_MAX_BYTES:
            entries.sort(key=lambda entry: entry[0])
            count = len(entries)
            for _mtime, size, old in entries:
                if count <= SPILL_MAX_FILES and total_size <= SPILL_MAX_BYTES:
                    break
                if old == path:
                    continue
                try:
                    old.unlink()
                except OSError:
                    continue
                count -= 1
                total_size -= size
        return path
    except OSError:
        return None


def abridge_live(
    text: str, is_error: bool = False, spill_dir: Path | None = None
) -> str | None:
    if is_error or not isinstance(text, str) or not text:
        return None
    if len(text) <= LIVE_FAT:
        return None
    path = spill(text, spill_dir)
    omitted = len(text) - LIVE_HEAD - LIVE_TAIL
    if path is not None:
        return "%s\n[… %s chars omitted; full output saved: %s …]\n%s" % (
            text[:LIVE_HEAD],
            omitted,
            path,
            text[-LIVE_TAIL:],
        )
    return abridge(text, LIVE_HEAD, LIVE_TAIL)


def is_pinned(
    index: int, total: int, preserve_recent: int, keep_first: int = 0
) -> bool:
    return index == 0 or index < keep_first or index >= total - preserve_recent


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def map_role(role: Any) -> str:
    name = str(role or "user").lower()
    if name in ("assistant", "model", "ai"):
        return "assistant"
    if name in ("tool", "function", "tool_result"):
        return "tool"
    if name == "system":
        return "system"
    return "user"


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float, bool)):
        return str(content)
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return content_text(content.get("content"))
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    skip = ("tool_use", "tool_result", "tool_call", "function_call")
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        item = _as_dict(block)
        if item.get("type") in skip:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
            continue
        nested = item.get("content")
        if isinstance(nested, str) and nested and item.get("type") in (None, "text", "output_text"):
            parts.append(nested)
    return "\n".join(parts)


def parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    return {}


def error_flag(item: dict[str, Any]) -> bool:
    if item.get("isError") or item.get("is_error"):
        return True
    status = item.get("status")
    return isinstance(status, str) and status.lower() == "error"


def collect_tool_uses(item: dict[str, Any]) -> list[Any]:
    uses: list[Any] = []
    for key in ("toolUses", "tool_uses"):
        value = item.get(key)
        if isinstance(value, list):
            uses.extend(value)
    calls = item.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            call = _as_dict(call)
            fn = _as_dict(call.get("function"))
            uses.append(
                {
                    "tool_use_id": call.get("id") or call.get("tool_use_id") or "",
                    "tool": fn.get("name") or call.get("name") or call.get("tool") or "tool",
                    "input": parse_arguments(
                        fn.get("arguments") if fn else call.get("arguments") or call.get("input")
                    ),
                }
            )
    fn = item.get("function_call")
    if isinstance(fn, dict):
        uses.append(
            {
                "tool_use_id": str(item.get("tool_call_id") or fn.get("name") or "function_call"),
                "tool": fn.get("name") or "tool",
                "input": parse_arguments(fn.get("arguments")),
            }
        )
    if str(item.get("type") or "") in ("custom_tool_call", "function_call") and item.get("name"):
        uses.append(
            {
                "tool_use_id": str(item.get("call_id") or item.get("id") or ""),
                "tool": item.get("name") or "tool",
                "input": parse_arguments(item.get("input") or item.get("arguments")),
            }
        )
    content = item.get("content")
    if isinstance(content, list):
        for block in content:
            block = _as_dict(block)
            if block.get("type") not in ("tool_use", "tool_call", "function_call"):
                continue
            uses.append(
                {
                    "tool_use_id": block.get("id") or block.get("tool_use_id") or "",
                    "tool": block.get("name") or block.get("tool") or "tool",
                    "input": block.get("input")
                    if isinstance(block.get("input"), dict)
                    else parse_arguments(block.get("arguments")),
                }
            )
    return uses


def collect_tool_results(item: dict[str, Any]) -> list[Any]:
    results: list[Any] = []
    for key in ("toolResults", "tool_results"):
        value = item.get(key)
        if isinstance(value, list):
            results.extend(value)
    role = str(item.get("role") or item.get("type") or "").lower()
    if role in ("tool", "function", "tool_result", "custom_tool_call_output"):
        uid = (
            item.get("tool_call_id")
            or item.get("tool_use_id")
            or item.get("call_id")
            or item.get("id")
            or item.get("name")
            or ""
        )
        text = item.get("text") or content_text(item.get("content"))
        if not text:
            extra = item.get("output")
            if extra is None:
                extra = item.get("result")
            text = extra if isinstance(extra, str) else content_text(extra)
        results.append({"tool_use_id": str(uid), "text": text or "", "isError": error_flag(item)})
    content = item.get("content")
    if isinstance(content, list):
        for block in content:
            block = _as_dict(block)
            if block.get("type") != "tool_result":
                continue
            body = block.get("content")
            text = body if isinstance(body, str) else content_text(body)
            if not text:
                text = str(block.get("text") or "")
            results.append(
                {
                    "tool_use_id": str(block.get("tool_use_id") or block.get("id") or ""),
                    "text": text,
                    "isError": error_flag(block),
                }
            )
    return results


def normalize_message(raw: Any) -> dict[str, Any]:
    item = _as_dict(raw)
    uses = collect_tool_uses(item)
    results = collect_tool_results(item)
    role_raw = str(item.get("role") or item.get("type") or "").lower()
    text = item.get("text")
    if not isinstance(text, str) or not text:
        text = content_text(item.get("content"))
    if role_raw in ("tool", "function", "tool_result", "custom_tool_call_output"):
        text = ""
    message = {
        "role": map_role(item.get("role") or item.get("type")),
        "text": text or "",
        "toolUses": [normalize_tool_use(tool) for tool in uses],
    }
    have = set()
    for result in results:
        uid = str(_as_dict(result).get("tool_use_id") or _as_dict(result).get("id") or "")
        if uid:
            have.add(uid)
    for tool, normalized in zip(uses, message["toolUses"]):
        uid = normalized["tool_use_id"]
        if not uid or uid in have:
            continue
        extra = ""
        if isinstance(tool, dict):
            extra = str(tool.get("text") or tool.get("result") or "")
        if extra:
            results.append({"tool_use_id": uid, "text": extra, "isError": bool(normalized.get("isError"))})
            have.add(uid)
    if results:
        cleaned = []
        for result in results:
            normalized = normalize_tool_result(result)
            if not normalized.get("tool_use_id") and not normalized.get("text"):
                continue
            cleaned.append(normalized)
        if cleaned:
            message["toolResults"] = cleaned
    return message


SKIP_SESSION_TYPES = {
    "queue-operation",
    "attachment",
    "progress",
    "system",
    "session_meta",
    "event_msg",
    "turn_context",
    "file_history_snapshot",
    "compact_boundary",
    "reasoning",
}


def session_records_to_messages(item: Any) -> list[Any]:
    if not isinstance(item, dict):
        return [item] if item is not None else []
    if item.get("role"):
        return [item]
    kind = str(item.get("type") or "")
    if kind in SKIP_SESSION_TYPES:
        return []
    nested = item.get("message")
    if kind in ("user", "assistant") and isinstance(nested, dict):
        return [nested]
    payload = item.get("payload")
    if kind == "response_item" and isinstance(payload, dict):
        return session_records_to_messages(payload)
    if kind in ("custom_tool_call", "function_call"):
        raw_input = item.get("input")
        if raw_input is None:
            raw_input = item.get("arguments")
        arguments = raw_input if isinstance(raw_input, str) else json.dumps(raw_input or {}, ensure_ascii=False)
        return [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": item.get("call_id") or item.get("id") or "",
                        "type": "function",
                        "function": {
                            "name": item.get("name") or "tool",
                            "arguments": arguments,
                        },
                    }
                ],
            }
        ]
    if kind in ("custom_tool_call_output", "function_call_output"):
        output = item.get("output")
        return [
            {
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "",
                "content": output if isinstance(output, str) else content_text(output),
            }
        ]
    if kind == "message":
        return [item]
    if kind in ("user", "assistant", "tool", "tool_result", "function"):
        return [item]
    if kind:
        return []
    return [item]


def flatten_session_records(items: list[Any]) -> list[Any]:
    out: list[Any] = []
    for item in items:
        out.extend(session_records_to_messages(item))
    return out


def extract_messages(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return flatten_session_records(payload)
    if not isinstance(payload, dict):
        raise SystemExit("transcript JSON must be a list or an object with messages")
    request = payload.get("request")
    if isinstance(request, dict):
        body = request.get("body")
        if isinstance(body, str) and body.strip():
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                body = None
        if isinstance(body, (list, dict)):
            try:
                return extract_messages(body)
            except SystemExit:
                pass
        nested = extract_messages(request) if request.get("messages") or request.get("transcript") else []
        if nested:
            return nested
    for key in ("messages", "transcript", "items", "history", "input"):
        value = payload.get(key)
        if isinstance(value, list):
            return flatten_session_records(value)
        if isinstance(value, dict) and isinstance(value.get("messages"), list):
            return flatten_session_records(value["messages"])
    raise SystemExit("transcript JSON must be a list or {\"messages\": [...]}")


def parse_transcript(raw: str) -> list[Any]:
    text = raw.strip()
    if not text:
        raise SystemExit("empty transcript")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows: list[Any] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        payload = rows
    return extract_messages(payload)


def normalize_tool_use(raw: Any) -> dict[str, Any]:
    item = _as_dict(raw)
    raw_input = item.get("input")
    if not isinstance(raw_input, dict):
        raw_input = parse_arguments(item.get("arguments") if raw_input is None else raw_input)
    tool = {
        "tool_use_id": str(
            item.get("tool_use_id") or item.get("id") or item.get("call_id") or item.get("tool_call_id") or ""
        ),
        "tool": str(item.get("tool") or item.get("name") or "tool"),
        "input": raw_input,
    }
    if "text" in item:
        tool["text"] = str(item.get("text") or "")
    if error_flag(item):
        tool["isError"] = True
    return tool


def normalize_tool_result(raw: Any) -> dict[str, Any]:
    item = _as_dict(raw)
    text = item.get("text")
    if not isinstance(text, str) or not text:
        text = content_text(item.get("content"))
        if not text:
            extra = item.get("output")
            if extra is None:
                extra = item.get("result")
            text = extra if isinstance(extra, str) else content_text(extra)
    result = {
        "tool_use_id": str(
            item.get("tool_use_id") or item.get("tool_call_id") or item.get("call_id") or item.get("id") or ""
        ),
        "text": text or "",
    }
    if error_flag(item):
        result["isError"] = True
    return result


@dataclass
class ToolCall:
    id: str
    tool_use_id: str
    tool: str
    input: dict[str, Any]
    call_index: int
    result_index: int
    result_chars: int
    is_error: bool
    pinned: bool


def collect_tool_calls(
    messages: list[dict[str, Any]],
    preserve_recent: int,
    keep_first: int = 0,
) -> list[ToolCall]:
    results: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, message in enumerate(messages):
        for result in message.get("toolResults") or []:
            results[result["tool_use_id"]] = (index, result)
    calls: list[ToolCall] = []
    total = len(messages)
    for call_index, message in enumerate(messages):
        for tool in message.get("toolUses") or []:
            found = results.get(tool["tool_use_id"])
            if not found:
                continue
            result_index, result = found
            calls.append(
                ToolCall(
                    id="t%s" % (len(calls) + 1),
                    tool_use_id=tool["tool_use_id"],
                    tool=tool["tool"],
                    input=tool.get("input") if isinstance(tool.get("input"), dict) else {},
                    call_index=call_index,
                    result_index=result_index,
                    result_chars=len(result.get("text") or ""),
                    is_error=bool(result.get("isError")),
                    pinned=is_pinned(call_index, total, preserve_recent, keep_first)
                    or is_pinned(result_index, total, preserve_recent, keep_first),
                )
            )
    return calls


def input_paths(inp: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key in PATH_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            found.append(value.strip())
    return found


def trace_needles(trace: dict[str, Any] | None) -> list[str]:
    if not isinstance(trace, dict):
        return []
    needles: list[str] = []
    for key in ("plan", "current_step", "last_error", "last_pick"):
        value = trace.get(key)
        if isinstance(value, str) and value.strip():
            needles.append(value.strip())
    inspected = trace.get("inspected") or []
    if isinstance(inspected, list):
        for item in inspected:
            if isinstance(item, str) and item.strip():
                needles.append(item.strip())
            elif isinstance(item, dict):
                needles.extend(input_paths(item))
    return needles


def pin_errors_and_trace(calls: list[ToolCall], trace: dict[str, Any] | None) -> None:
    needles = trace_needles(trace)
    for call in calls:
        if call.pinned:
            continue
        if call.is_error:
            call.pinned = True
            continue
        paths = input_paths(call.input)
        if not paths or not needles:
            continue
        for path in paths:
            for needle in needles:
                if path in needle or needle in path:
                    call.pinned = True
                    break
            if call.pinned:
                break


def input_text(inp: dict[str, Any], limit: int) -> str:
    try:
        dumped = _dumps(inp)
    except TypeError:
        dumped = "[unserializable input]"
    return truncate(dumped, limit)


def result_note(call: ToolCall) -> str:
    kind = "error" if call.is_error else "ok"
    return "%s, %s chars (omitted)" % (kind, call.result_chars)


def compact_call(call: ToolCall) -> str:
    parts = []
    for key, value in call.input.items():
        text = value if isinstance(value, str) else input_text({key: value}, 200)
        parts.append("%s=%s" % (key, re.sub(r"\s+", " ", str(text))))
    joined = " ".join(parts)
    kind = "error" if call.is_error else "ok"
    return "%s %s %s → %s %sch" % (
        call.id,
        call.tool,
        truncate(joined, INPUT_CHARS[2]),
        kind,
        call.result_chars,
    )


def calls_by_message(calls: Iterable[ToolCall]) -> dict[int, list[ToolCall]]:
    grouped: dict[int, list[ToolCall]] = {}
    for call in calls:
        grouped.setdefault(call.call_index, []).append(call)
    return grouped


def history_entries(
    messages: list[dict[str, Any]],
    calls: list[ToolCall],
    input_chars: int,
) -> list[dict[str, Any]]:
    by_message = calls_by_message(calls)
    entries: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        tool_calls = [
            {
                "id": call.id,
                "tool": call.tool,
                "input": input_text(call.input, input_chars),
                "result": result_note(call),
            }
            for call in by_message.get(index, [])
        ]
        if not (message.get("text") or "").strip() and not tool_calls:
            continue
        entry: dict[str, Any] = {
            "i": index,
            "role": message.get("role") or "user",
            "text": message.get("text") or "",
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        entries.append(entry)
    return entries


def merge_call_runs(
    history: list[dict[str, Any]],
    pinned: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []

    def foldable(entry: dict[str, Any]) -> bool:
        calls = entry.get("tool_calls")
        return (
            not pinned(entry)
            and not (entry.get("text") or "")
            and isinstance(calls, list)
            and calls
            and isinstance(calls[0], str)
        )

    for entry in history:
        previous = merged[-1] if merged else None
        if (
            previous
            and foldable(previous)
            and foldable(entry)
            and previous.get("role") == entry.get("role")
        ):
            previous["tool_calls"] = list(previous.get("tool_calls") or []) + list(
                entry.get("tool_calls") or []
            )
            continue
        merged.append(dict(entry))
    return merged


def goal_from_messages(messages: list[dict[str, Any]]) -> str:
    texts = [
        truncate(message.get("text") or "", 500)
        for message in messages
        if message.get("role") == "user"
        and (message.get("text") or "").strip()
        and not (message.get("toolResults") or [])
    ]
    return "\n".join(texts[-3:])


def entry_tokens(entry: dict[str, Any]) -> int:
    return estimate_tokens(_dumps(entry)) + 1


def fit_state(
    messages: list[dict[str, Any]],
    calls: list[ToolCall],
    options: dict[str, Any],
) -> dict[str, Any]:
    goal = options.get("goal") or goal_from_messages(messages)
    max_tokens = int(options.get("max_state_tokens") or MAX_STATE_TOKENS)
    preserve_raw = options.get("preserve_recent")
    preserve = int(preserve_raw) if preserve_raw is not None else PRESERVE_RECENT
    keep_first = int(options.get("keep_first") or 0)
    keep_pattern = options.get("keep_text") or ""
    keep_re = None
    if keep_pattern:
        try:
            keep_re = re.compile(keep_pattern)
        except re.error:
            keep_re = None

    def state_of(history: list[dict[str, Any]]) -> dict[str, Any]:
        return {"context": STATE_CONTEXT, "goal": goal, "history": history}

    base_tokens = estimate_tokens(_dumps(state_of([])))
    history: list[dict[str, Any]] = []
    per_entry: list[int] = []
    tokens = 0

    def rebuild(limit: int) -> None:
        nonlocal history, per_entry, tokens
        history = history_entries(messages, calls, limit)
        per_entry = [entry_tokens(entry) for entry in history]
        tokens = base_tokens + sum(per_entry)

    def fits() -> bool:
        return tokens <= max_tokens

    def shrink(index: int, change: Callable[[dict[str, Any]], None]) -> None:
        nonlocal tokens
        entry = history[index]
        change(entry)
        now = entry_tokens(entry)
        tokens += now - (per_entry[index] if index < len(per_entry) else 0)
        if index < len(per_entry):
            per_entry[index] = now

    def fitted(stage: str, hist: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        used = hist if hist is not None else history
        return {"state": state_of(used), "tokens": tokens, "stage": stage}

    rebuild(INPUT_CHARS[0])
    if fits():
        return fitted("full")
    for limit in INPUT_CHARS[1:]:
        rebuild(limit)
        if fits():
            return fitted("inputs<=%s" % limit)

    def pinned(entry: dict[str, Any]) -> bool:
        if keep_re is not None and keep_re.search(str(entry.get("text") or "")):
            return True
        return is_pinned(int(entry["i"]), len(messages), preserve, keep_first)

    indices = list(range(len(history)))
    order = [i for i in indices if not pinned(history[i])] + [
        i for i in indices if pinned(history[i])
    ]
    for index in order:
        entry = history[index]
        if len(entry.get("text") or "") <= TEXT_HEAD + TEXT_TAIL + 40:
            continue
        shrink(index, lambda e: e.__setitem__("text", abridge(e["text"], TEXT_HEAD, TEXT_TAIL)))
        if fits():
            return fitted("texts abridged")
    for index in order:
        entry = history[index]
        if pinned(entry) or not (entry.get("text") or ""):
            continue
        original = len((messages[entry["i"]].get("text") if entry["i"] < len(messages) else "") or entry.get("text") or "")
        shrink(index, lambda e, n=original: e.__setitem__("text", "[… %s chars omitted …]" % n))
        if fits():
            return fitted("old messages collapsed")
    by_message = calls_by_message(calls)
    for index in order:
        entry = history[index]
        own = by_message.get(entry["i"])
        if pinned(entry) or not own:
            continue
        shrink(index, lambda e, own=own: e.__setitem__("tool_calls", [compact_call(c) for c in own]))
        if fits():
            return fitted("old calls compacted")
    left: set[int] = set()
    for index in order:
        entry = history[index]
        if pinned(entry) or entry.get("tool_calls"):
            continue
        left.add(index)
        tokens -= per_entry[index] if index < len(per_entry) else 0
        if fits():
            return fitted(
                "old messages left out",
                [item for i, item in enumerate(history) if i not in left],
            )
    history = merge_call_runs([item for i, item in enumerate(history) if i not in left], pinned)
    per_entry = [entry_tokens(entry) for entry in history]
    tokens = base_tokens + sum(per_entry)
    if fits():
        return fitted("old calls merged")
    raise RuntimeError(
        "history too large for Jev (~%s tokens after truncation, limit %s)"
        % (tokens, max_tokens)
    )


def questions_for(call: ToolCall) -> dict[str, Any]:
    return {
        "call_%s" % call.id: {
            "type": "noul",
            "instructions": (
                "Should the %s call %s stay in the history? Knowing it was "
                "made, with this input, still matters for the ongoing task. "
                "The result is a separate question."
            )
            % (call.tool, call.id),
        },
        "result_%s" % call.id: {
            "type": "noul",
            "instructions": (
                "Should the full output of %s call %s stay verbatim? Its "
                "contents are still needed and re-running the tool would not do."
            )
            % (call.tool, call.id),
        },
    }


def batch_calls(
    calls: list[ToolCall],
    state_tokens: int,
    max_request_tokens: int = MAX_REQUEST_TOKENS,
) -> list[list[ToolCall]]:
    budget = max_request_tokens - state_tokens
    batches: list[list[ToolCall]] = []
    current: list[ToolCall] = []
    current_tokens = 0
    for call in calls:
        tokens = estimate_tokens(_dumps(questions_for(call)))
        over_budget = current and current_tokens + tokens > budget
        over_count = current and len(current) >= MAX_CALLS_PER_BATCH
        if over_budget or over_count:
            batches.append(current)
            current = []
            current_tokens = 0
        if not current and tokens > budget:
            raise RuntimeError(
                "state leaves no room for questions (~%s of %s tokens)"
                % (state_tokens, max_request_tokens)
            )
        current.append(call)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


def noul_answer(answers: dict[str, Any], name: str) -> float:
    answer = answers.get(name) or {}
    if not isinstance(answer, dict) or not isinstance(answer.get("noul"), (int, float)):
        raise RuntimeError("Invalid Jev answer for %s" % name)
    return float(answer["noul"])


def decide_call(
    call: ToolCall,
    answer: dict[str, float],
    keep_threshold: float,
) -> dict[str, Any]:
    base = {
        "id": call.id,
        "tool": call.tool,
        "keepCall": answer["keepCall"],
        "keepResult": answer["keepResult"],
    }
    if call.pinned:
        return {**base, "action": "keep", "reason": "pinned"}
    if answer["keepResult"] >= keep_threshold:
        return {**base, "action": "keep", "reason": "kept"}
    if answer["keepCall"] >= keep_threshold:
        return {**base, "action": "drop_result", "reason": "result_dropped"}
    return {**base, "action": "drop_call", "reason": "call_dropped"}


def truncated_result_text(
    text: str, is_error: bool, head_chars: int, spill_enabled: bool = True
) -> str:
    if len(text) <= head_chars + 120:
        return text
    head = "%s\n" % text[:head_chars] if head_chars > 0 else ""
    extra = " (error)" if is_error else ""
    saved = spill(text) if spill_enabled else None
    if saved is not None:
        extra += "; full output saved: %s" % saved
    return "%s[fast-jev-compaction truncated %s chars of this tool result%s; re-run the tool if needed]" % (
        head,
        len(text) - head_chars,
        extra,
    )


def apply_decisions(
    messages: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    calls: list[ToolCall],
    head_chars: int,
    spill_enabled: bool = True,
) -> list[dict[str, Any]]:
    by_id = {call.id: call for call in calls}
    actions: dict[str, str] = {}
    for decision in decisions:
        call = by_id.get(decision["id"])
        if call and decision["action"] != "keep":
            actions[call.tool_use_id] = decision["action"]
    kept: list[dict[str, Any]] = []
    for message in messages:
        uses = message.get("toolUses") or []
        results = message.get("toolResults") or []
        touched = any(tool["tool_use_id"] in actions for tool in uses) or any(
            result["tool_use_id"] in actions for result in results
        )
        if not touched:
            kept.append(message)
            continue

        def map_use(tool: dict[str, Any]) -> dict[str, Any] | None:
            action = actions.get(tool["tool_use_id"])
            if action == "drop_call":
                return None
            if action != "drop_result":
                return tool
            text = truncated_result_text(
                tool.get("text") or "",
                bool(tool.get("isError")),
                head_chars,
                spill_enabled,
            )
            if (tool.get("text") or "") == text:
                return tool
            copy = {
                "tool_use_id": tool["tool_use_id"],
                "tool": tool["tool"],
                "input": tool.get("input") or {},
                "text": text,
            }
            if tool.get("isError"):
                copy["isError"] = True
            return copy

        def map_result(result: dict[str, Any]) -> dict[str, Any] | None:
            action = actions.get(result["tool_use_id"])
            if action == "drop_call":
                return None
            if action != "drop_result":
                return result
            text = truncated_result_text(
                result.get("text") or "",
                bool(result.get("isError")),
                head_chars,
                spill_enabled,
            )
            if text == (result.get("text") or ""):
                return result
            copy = {"tool_use_id": result["tool_use_id"], "text": text}
            if "isError" in result:
                copy["isError"] = result.get("isError")
            return copy

        new_uses = [item for item in (map_use(tool) for tool in uses) if item is not None]
        new_results = [item for item in (map_result(result) for result in results) if item is not None]
        if not (message.get("text") or "").strip() and not new_uses and not new_results:
            continue
        rebuilt: dict[str, Any] = {
            "role": message.get("role") or "user",
            "text": message.get("text") or "",
            "toolUses": new_uses,
        }
        if new_results:
            rebuilt["toolResults"] = new_results
        kept.append(rebuilt)
    return kept


def message_chars(message: dict[str, Any]) -> int:
    total = len(message.get("text") or "")
    for tool in message.get("toolUses") or []:
        try:
            total += len(_dumps(tool.get("input") or {}))
        except TypeError:
            total += 20
    for result in message.get("toolResults") or []:
        total += len(result.get("text") or "")
    return total


def reduction_ratio(result: dict[str, Any]) -> float:
    before = result["stats"]["charsBefore"]
    after = result["stats"]["charsAfter"]
    if before == 0:
        return 0.0
    return (before - after) / before


def _count(decisions: list[dict[str, Any]], reason: str) -> int:
    return sum(1 for item in decisions if item.get("reason") == reason)


Asker = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def compact(
    messages: list[dict[str, Any]],
    asker: Asker,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.time()
    opts = options or {}
    if not isinstance(messages, list):
        messages = extract_messages(messages)
    else:
        messages = flatten_session_records(messages)
    preserve_raw = opts.get("preserve_recent")
    preserve = int(preserve_raw) if preserve_raw is not None else PRESERVE_RECENT
    keep_first = int(opts.get("keep_first") or 0)
    keep_threshold = float(opts.get("keep_threshold") if opts.get("keep_threshold") is not None else KEEP_THRESHOLD)
    head_chars = int(opts.get("truncate_head_chars") if opts.get("truncate_head_chars") is not None else TRUNCATE_HEAD_CHARS)
    messages = [normalize_message(item) for item in messages]
    calls = collect_tool_calls(messages, preserve, keep_first)
    pin_errors_and_trace(calls, opts.get("trace"))
    keep_re = None
    if opts.get("keep_text"):
        try:
            keep_re = re.compile(str(opts["keep_text"]))
        except re.error:
            keep_re = None
    if keep_re is not None:
        for call in calls:
            if keep_re.search(call.tool) or keep_re.search(_dumps(call.input)):
                call.pinned = True
    candidates = [call for call in calls if not call.pinned]
    chars_before = sum(message_chars(message) for message in messages)
    fitted = {"tokens": 0, "stage": ""}
    batches: list[list[ToolCall]] = []
    answers: dict[str, dict[str, float]] = {}
    if candidates:
        fitted_state = fit_state(
            messages,
            calls,
            {
                "goal": opts.get("goal"),
                "max_state_tokens": opts.get("max_state_tokens") or MAX_STATE_TOKENS,
                "preserve_recent": preserve,
                "keep_text": opts.get("keep_text"),
            },
        )
        fitted = fitted_state
        batches = batch_calls(
            candidates,
            fitted_state["tokens"],
            int(opts.get("max_request_tokens") or MAX_REQUEST_TOKENS),
        )
        for batch in batches:
            questions: dict[str, Any] = {}
            for call in batch:
                questions.update(questions_for(call))
            payload = asker(fitted_state["state"], questions)
            raw_answers = payload.get("answers") if isinstance(payload, dict) else {}
            if not isinstance(raw_answers, dict):
                raise RuntimeError("Jev response is missing answers")
            for call in batch:
                answers[call.id] = {
                    "keepCall": noul_answer(raw_answers, "call_%s" % call.id),
                    "keepResult": noul_answer(raw_answers, "result_%s" % call.id),
                }
    decisions = [
        decide_call(
            call,
            answers.get(call.id) or {"keepCall": 1.0, "keepResult": 1.0},
            keep_threshold,
        )
        for call in calls
    ]
    kept = apply_decisions(
        messages, decisions, calls, head_chars,
        spill_enabled=not opts.get("no_spill"),
    )
    return {
        "messages": kept,
        "decisions": decisions,
        "stats": {
            "messagesBefore": len(messages),
            "messagesAfter": len(kept),
            "charsBefore": chars_before,
            "charsAfter": sum(message_chars(message) for message in kept),
            "calls": len(calls),
            "kept": _count(decisions, "kept"),
            "resultsDropped": _count(decisions, "result_dropped"),
            "callsDropped": _count(decisions, "call_dropped"),
            "pinned": _count(decisions, "pinned"),
            "stateTokens": fitted.get("tokens") or 0,
            "stateStage": fitted.get("stage") or "",
            "requests": len(batches),
            "ms": int((time.time() - started) * 1000),
            "fallback": False,
        },
    }


def compact_or_keep(
    messages: list[dict[str, Any]],
    asker: Asker,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    opts = dict(options or {})
    min_reduction = float(opts.pop("min_reduction") if "min_reduction" in opts else MIN_REDUCTION)
    original = [normalize_message(item) for item in messages]
    result = compact(original, asker, opts)
    result["stats"]["reduction"] = reduction_ratio(result)
    if min_reduction > 0 and result["stats"]["reduction"] < min_reduction:
        result["messages"] = original
        result["stats"]["charsAfter"] = result["stats"]["charsBefore"]
        result["stats"]["messagesAfter"] = len(original)
        result["stats"]["fallback"] = True
    return result


def load_jev():
    path = Path(__file__).resolve().parent / "jev.py"
    spec = importlib.util.spec_from_file_location("jev_consult_jev", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def jev_asker(state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
    jev = load_jev()
    policy = jev.load_policy(str(Path(__file__).resolve().parent.parent / "policy.json"))
    return jev.post_systemone(state, questions, policy)


def load_trace(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    return data if isinstance(data, dict) else None


def cmd_compact(args: argparse.Namespace) -> int:
    if args.file == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(args.file).read_text(encoding="utf-8")
    messages = parse_transcript(raw)
    options = {
        "goal": args.goal,
        "keep_threshold": args.keep_threshold,
        "preserve_recent": args.preserve_recent,
        "keep_first": args.keep_first,
        "truncate_head_chars": args.truncate_head_chars,
        "min_reduction": args.min_reduction,
        "keep_text": args.keep_text or os.environ.get("JEV_KEEP_TEXT", ""),
        "trace": load_trace(args.trace),
        "no_spill": bool(getattr(args, "dry_run", False) or getattr(args, "check", False)),
    }
    asker: Asker
    if args.fake:
        def asker(state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
            answers = {
                name: {"type": "noul", "noul": 0.1}
                for name in questions
            }
            return {"answers": answers}
    else:
        asker = jev_asker
    if getattr(args, "watch", None):
        import time as _time

        max_ticks = _watch.cap("JEV_COMPACT_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_COMPACT_WATCH_SECS", getattr(args, "watch_max", 0.0))
        cur: dict[str, Any] = {}
        verdict_ok = True

        def _write_verdict() -> bool:
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "fallback" if cur.get("stats", {}).get("fallback") else "ok",
                    "ticks": ticks,
                    "reduction": cur.get("stats", {}).get("reduction"),
                    "fallback": bool(cur.get("stats", {}).get("fallback")),
                },
            )

        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
            if args.file != "-":
                try:
                    messages = parse_transcript(Path(args.file).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
            cur = compact_or_keep(messages, asker, options)
            stats = cur.get("stats") or {}
            tick = {
                "ts": int(_time.time()),
                "messagesBefore": stats.get("messagesBefore"),
                "messagesAfter": stats.get("messagesAfter"),
                "charsBefore": stats.get("charsBefore"),
                "charsAfter": stats.get("charsAfter"),
                "reduction": stats.get("reduction"),
                "fallback": bool(stats.get("fallback")),
            }
            _watch.emit(tick, getattr(args, "out", "") or None, quiet=_watch.quiet("JEV_COMPACT_WATCH_QUIET", args.quiet), bad=tick["fallback"])
            ticks += 1
            if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if getattr(args, "fail_fast", False) and tick["fallback"]:
                break
            _time.sleep(args.watch)
        if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
            return 1
        return 1 if cur.get("stats", {}).get("fallback") else 0
    result = compact_or_keep(messages, asker, options)
    if getattr(args, "verdict", ""):
        stats = result.get("stats") if isinstance(result, dict) else {}
        stats = stats if isinstance(stats, dict) else {}
        if not _watch.write_verdict(
            args.verdict,
            {
                "verdict": "fallback" if stats.get("fallback") else "ok",
                "ticks": 1,
                "reduction": stats.get("reduction"),
                "fallback": bool(stats.get("fallback")),
            },
        ):
            return 1
    if getattr(args, "check", False):
        stats = result.get("stats") or {}
        ratio = stats.get("reduction", reduction_ratio(result))
        ok = not stats.get("fallback")
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps(
                    {
                        "check": "ok" if ok else "FAIL",
                        "reduction": ratio,
                        "min_reduction": args.min_reduction,
                    }
                )
                + "\n"
            )
        elif ok:
            sys.stdout.write("check: ok reduction %.3f\n" % ratio)
        else:
            sys.stdout.write(
                "check: FAIL reduction %.3f below --min-reduction %s\n"
                % (ratio, args.min_reduction)
            )
        return 0 if ok else 1
    if getattr(args, "dry_run", False):
        result["messages"] = messages
        stats = result.setdefault("stats", {})
        stats["messagesAfter"] = stats.get("messagesBefore")
        stats["charsAfter"] = stats.get("charsBefore")
        stats["dry_run"] = True
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps(
                    {"dry_run": True, "stats": stats}, indent=2, ensure_ascii=False
                )
                + "\n"
            )
            return 0
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    report_path = getattr(args, "report", "") or ""
    if report_path:
        stats = result.get("stats") if isinstance(result, dict) else {}
        try:
            Path(report_path).write_text(
                json.dumps(stats or {}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            sys.stderr.write("cannot write report %s: %s\n" % (report_path, exc))
            return 1
    if getattr(args, "stats_json", False):
        stats = result.get("stats") if isinstance(result, dict) else {}
        sys.stderr.write(json.dumps(stats or {}, ensure_ascii=False) + "\n")
    if args.stats:
        stats = result.get("stats") if isinstance(result, dict) else None
        stats = stats if isinstance(stats, dict) else {}
        before = stats.get("charsBefore")
        after = stats.get("charsAfter")
        pct = ""
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before:
            pct = " (%.0f%%)" % (100.0 * after / before)
        sys.stderr.write(
            "stats: kept=%s messages %s->%s chars %s->%s%s\n"
            % (
                stats.get("kept"),
                stats.get("messagesBefore"),
                stats.get("messagesAfter"),
                before,
                after,
                pct,
            )
        )
    return 0


def list_spill(directory: Path | None = None) -> list[tuple[Path, int, float]]:
    """Return (path, size_bytes, mtime) for regular files in the spill dir."""
    target = directory if directory is not None else spill_dir_default()
    if target is None or not target.is_dir():
        return []
    rows: list[tuple[Path, int, float]] = []
    for candidate in sorted(target.iterdir()):
        try:
            info = candidate.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        rows.append((candidate, info.st_size, info.st_mtime))
    return rows


def prune_spill(
    directory: Path | None = None,
    older_than: float = 0.0,
    now: float | None = None,
) -> list[Path]:
    """Unlink spill files older than `older_than` seconds. Returns removed paths."""
    target = directory if directory is not None else spill_dir_default()
    if target is None or not target.is_dir():
        return []
    cutoff = (time.time() if now is None else float(now)) - float(older_than)
    removed: list[Path] = []
    for candidate in sorted(target.iterdir()):
        try:
            info = candidate.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
            continue
        try:
            candidate.unlink()
        except OSError:
            continue
        removed.append(candidate)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="LIVE_FAT is the default. Session-history drop needs --history."
    )
    parser.add_argument("file", nargs="?", help="Transcript JSON/JSONL (list, {messages}, OpenAI/Claude/Hermes). '-' = stdin.")
    parser.add_argument("-o", "--output", help="Write result JSON here instead of stdout.")
    parser.add_argument("--trace", help="Optional .jev-trace.json; matching file_path stays.")
    parser.add_argument("--goal", default="")
    try:
        env_keep = float(os.environ.get("JEV_KEEP_THRESHOLD", "") or KEEP_THRESHOLD)
    except ValueError:
        env_keep = KEEP_THRESHOLD
    parser.add_argument("--keep-threshold", type=float, default=env_keep)
    try:
        env_preserve = int(os.environ.get("JEV_PRESERVE_RECENT", "") or PRESERVE_RECENT)
    except ValueError:
        env_preserve = PRESERVE_RECENT
    parser.add_argument("--preserve-recent", type=int, default=max(0, env_preserve))
    try:
        env_first = int(os.environ.get("JEV_KEEP_FIRST", "") or 0)
    except ValueError:
        env_first = 0
    parser.add_argument("--keep-first", type=int, default=max(0, env_first), help="Always keep the first N messages pinned")
    try:
        env_head = int(os.environ.get("JEV_TRUNCATE_HEAD", "") or TRUNCATE_HEAD_CHARS)
    except ValueError:
        env_head = TRUNCATE_HEAD_CHARS
    parser.add_argument("--truncate-head-chars", type=int, default=max(0, env_head))
    try:
        env_min = float(os.environ.get("JEV_MIN_REDUCTION", "") or MIN_REDUCTION)
    except ValueError:
        env_min = MIN_REDUCTION
    parser.add_argument("--min-reduction", type=float, default=env_min)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute decisions and stats but emit the original messages unchanged.",
    )
    parser.add_argument(
        "--keep-text",
        default="",
        help="Pin messages/tool calls whose text or input matches this regex (never dropped).",
    )
    parser.add_argument(
        "--history",       
        action="store_true",
        help="Opt-in Tamara session drop. Not the default (Hermes eval did not adopt it).",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Do not call Jev; drop every non-pinned result (for tests).",
    )
    parser.add_argument(
        "--watch",
        type=float,
        metavar="SECONDS",
        help="Re-read the transcript file and re-run compaction every S seconds, emitting a stats tick per pass (JEV_COMPACT_WATCH_MAX caps ticks).",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick that fell back to the original transcript")
    parser.add_argument("--verdict", default="", metavar="PATH", help="Write a slim {verdict: ok|fallback, ticks, reduction, fallback} JSON to PATH — refreshed every --watch tick; without --watch a one-shot probe after the run.")
    parser.add_argument(
        "--prune-spill",
        type=float,
        metavar="SECONDS",
        help="Unlink spill files older than SECONDS in the spill dir (or --spill-dir) and exit.",
    )
    parser.add_argument("--spill-dir", help="Override spill directory for --prune-spill/--list-spill.")
    parser.add_argument(
        "--out",
        default="",
        metavar="PATH",
        help="With --list-spill/--prune-spill: write the listing to PATH instead of stdout.",
    )
    parser.add_argument(
        "--list-spill",
        action="store_true",
        help="List spill files (name, bytes, mtime) and exit.",
    )
    parser.add_argument(
        "--verify-spill",
        metavar="FILE",
        default="",
        help="Verify 'full output saved: PATH' references in FILE exist (rc 1 on missing).",
    )
    parser.add_argument(
        "--orphan-spill",
        metavar="FILE",
        default="",
        help="List spill files not referenced by FILE (uses --spill-dir for the dir).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit --verify-spill/--orphan-spill results as JSON.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Dry check: exit 1 when the transcript would compact below the --min-reduction gate; prints only a check line.",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print a one-line compaction summary to stderr after the result.",
    )
    parser.add_argument(
        "--stats-json",
        action="store_true",
        help="Print the stats dict as JSON to stderr after the result.",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        default="",
        help="Write the stats dict as JSON to PATH after the run.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the jev-consult policy version and exit.",
    )
    parser.add_argument(
        "--dir",
        metavar="DIR",
        help="Compact every *.json/*.jsonl transcript in DIR; one JSON line per file on stdout.",
    )
    args = parser.parse_args(argv)
    if args.version:
        policy_path = Path(__file__).resolve().parent.parent / "policy.json"
        try:
            version = json.loads(policy_path.read_text(encoding="utf-8")).get("version", "?")
        except (OSError, ValueError):
            version = "?"
        sys.stdout.write("jev-consult (policy v%s)\n" % version)
        return 0
    if args.verify_spill:
        try:
            text = Path(args.verify_spill).read_text(encoding="utf-8")
        except OSError as exc:
            sys.stderr.write("--verify-spill failed: %s\n" % exc)
            return 2
        refs = re.findall(r"full output saved: (\S+?)\]", text)
        missing = [ref for ref in refs if not Path(ref).is_file()]
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "refs": refs,
                        "missing": missing,
                        "ok": not missing,
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            for ref in refs:
                sys.stdout.write(
                    "%s %s\n" % ("ok" if Path(ref).is_file() else "missing", ref)
                )
            sys.stdout.write("%d refs, %d missing\n" % (len(refs), len(missing)))
        return 1 if missing else 0
    if args.orphan_spill:
        try:
            text = Path(args.orphan_spill).read_text(encoding="utf-8")
        except OSError as exc:
            sys.stderr.write("--orphan-spill failed: %s\n" % exc)
            return 2
        referenced = set(re.findall(r"full output saved: (\S+?)\]", text))
        directory = Path(args.spill_dir) if args.spill_dir else None
        orphans = [
            path for path, _size, _mtime in list_spill(directory)
            if str(path) not in referenced
        ]
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {"orphans": [str(path) for path in orphans], "count": len(orphans)},
                    indent=2,
                )
                + "\n"
            )
        else:
            for path in orphans:
                sys.stdout.write("orphan: %s\n" % path)
            sys.stdout.write("%d orphans\n" % len(orphans))
        return 0
    if args.list_spill:
        directory = Path(args.spill_dir) if args.spill_dir else None
        rows = list_spill(directory)
        if args.json:
            text = json.dumps(
                {
                    "files": [
                        {
                            "path": str(path),
                            "size": size,
                            "mtime": int(mtime),
                        }
                        for path, size, mtime in rows
                    ],
                    "count": len(rows),
                },
                indent=2,
            ) + "\n"
        else:
            text = "".join(
                "%s %d %d\n" % (path, size, int(mtime)) for path, size, mtime in rows
            ) + "%d spill files\n" % len(rows)
        if args.out:
            try:
                Path(args.out).write_text(text, encoding="utf-8")
            except OSError as exc:
                sys.stderr.write("--out failed: %s\n" % exc)
                return 1
            sys.stderr.write("wrote %s\n" % args.out)
        else:
            sys.stdout.write(text)
        return 0
    if args.prune_spill is not None:
        directory = Path(args.spill_dir) if args.spill_dir else None
        removed = prune_spill(directory, args.prune_spill)
        if args.json:
            text = json.dumps(
                {"pruned": [str(path) for path in removed], "count": len(removed)},
                indent=2,
            ) + "\n"
        else:
            text = "".join("pruned: %s\n" % path for path in removed) + (
                "pruned %d spill files\n" % len(removed)
            )
        if args.out:
            try:
                Path(args.out).write_text(text, encoding="utf-8")
            except OSError as exc:
                sys.stderr.write("--out failed: %s\n" % exc)
                return 1
            sys.stderr.write("wrote %s\n" % args.out)
        else:
            sys.stdout.write(text)
        return 0
    if args.dir:
        batch = Path(args.dir)
        if not batch.is_dir():
            sys.stderr.write("--dir: no such directory %s\n" % batch)
            return 2
        if not args.history:
            sys.stderr.write("--dir requires --history (same opt-in as single-file mode)\n")
            return 2
        files = sorted(
            [p for p in batch.iterdir() if p.is_file() and p.suffix in (".json", ".jsonl")]
        )
        total_in = 0
        total_out = 0
        n_ok = 0
        rows: list[dict] = []
        for p in files:
            row = {"file": p.name}
            try:
                messages = parse_transcript(p.read_text(encoding="utf-8"))
                options = {
                    "goal": args.goal,
                    "keep_threshold": args.keep_threshold,
                    "preserve_recent": args.preserve_recent,
                    "keep_first": args.keep_first,
                    "truncate_head_chars": args.truncate_head_chars,
                    "min_reduction": args.min_reduction,
                    "keep_text": args.keep_text or os.environ.get("JEV_KEEP_TEXT", ""),
                    "trace": load_trace(args.trace),
                }
                asker = (
                    (lambda s, q: {"answers": {n: {"type": "noul", "noul": 0.1} for n in q}})
                    if args.fake
                    else jev_asker
                )
                result = compact_or_keep(messages, asker, options)
                stats = result.get("stats") if isinstance(result, dict) else None
                stats = stats if isinstance(stats, dict) else {}
                row["ok"] = True
                row["kept"] = bool(stats.get("kept"))
                row["messages_in"] = len(messages)
                row["messages_out"] = stats.get("messagesAfter")
                row["chars_in"] = stats.get("charsBefore")
                row["chars_out"] = stats.get("charsAfter")
                n_ok += 1
                if isinstance(row["chars_in"], (int, float)):
                    total_in += row["chars_in"]
                if isinstance(row["chars_out"], (int, float)):
                    total_out += row["chars_out"]
            except (Exception, SystemExit) as exc:
                row["ok"] = False
                row["error"] = str(exc)[:200]
            if getattr(args, "json", False):
                rows.append(row)
            else:
                sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps(
                    {
                        "files": rows,
                        "count": len(files),
                        "ok": n_ok,
                        "chars_in": total_in,
                        "chars_out": total_out,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        else:
            sys.stdout.write(
                "batch: %d file(s), %d ok, %d -> %d chars\n" % (len(files), n_ok, total_in, total_out)
            )
        return 0
    if args.file is None:
        parser.error("file is required unless --prune-spill or --dir is given")
    if not args.history:
        sys.stderr.write(
            "history drop is not the default (Hermes eval: do not adopt Tamara retention). "
            "LIVE_FAT via compact_hook / transform_tool_result. Pass --history to opt in.\n"
        )
        return 2
    return cmd_compact(args)


if __name__ == "__main__":
    sys.exit(main())
