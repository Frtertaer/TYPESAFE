#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PostToolUse stdin hook: abridge fat tool results. No Jev. No watchdog.

Reads one JSON event from stdin. If the tool result is longer than LIVE_FAT
and is not an error, writes hookSpecificOutput.updatedToolOutput. Otherwise
prints {} and exits 0. Never exits non-zero — fail open.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import compact as C  # noqa: E402


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text_of(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for key in (
            "output_for_prompt",
            "output",
            "content",
            "text",
            "result",
            "stdout",
        ):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
        return C.content_text(value)
    return C.content_text(value)


def extract_result(payload: dict[str, Any]) -> tuple[str, bool, Any]:
    if payload.get("toolResultTruncated"):
        return "", False, None
    original = None
    for key in ("toolResult", "tool_result", "tool_response", "toolResponse", "result"):
        if key in payload and payload.get(key) is not None:
            original = payload.get(key)
            break
    text = _text_of(original)
    is_error = C.error_flag(payload) or C.error_flag(_as_dict(original))
    if payload.get("is_error") or payload.get("isError"):
        is_error = True
    return text, is_error, original


def replace_payload(original: Any, abridged: str) -> Any:
    if isinstance(original, dict):
        copy = dict(original)
        for key in ("output_for_prompt", "output", "content", "text", "result", "stdout"):
            if isinstance(copy.get(key), str) and copy.get(key):
                copy[key] = abridged
                return copy
        copy["output_for_prompt"] = abridged
        return copy
    return abridged


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    if event and event not in ("PostToolUse", "post_tool_use"):
        return {}
    text, is_error, original = extract_result(payload)
    abridged = C.abridge_live(text, is_error=is_error)
    if abridged is None:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": replace_payload(original, abridged),
        }
    }


USAGE = 'Usage: python compact_hook.py [--help]\n\nReads one PostToolUse JSON event from stdin. When the tool result is longer\nthan the live-fat threshold and is not an error, emits\nhookSpecificOutput.updatedToolOutput with the abridged text; otherwise prints\n{} and exits 0. Never exits non-zero — fail open.\n'


def _read_stdin() -> str:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is not None:
        try:
            return stream.read().decode("utf-8-sig", "replace")
        except (OSError, ValueError):
            pass
    try:
        return sys.stdin.read().lstrip("﻿")
    except (OSError, UnicodeError):
        return ""


def main() -> int:
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        sys.stdout.write(USAGE)
        return 0
    raw = _read_stdin()
    if not raw.strip():
        sys.stdout.write("{}\n")
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        sys.stdout.write("{}\n")
        return 0
    if not isinstance(payload, dict):
        sys.stdout.write("{}\n")
        return 0
    sys.stdout.write(json.dumps(handle(payload), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
