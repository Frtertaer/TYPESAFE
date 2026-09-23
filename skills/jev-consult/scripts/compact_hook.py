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

import _watch  # noqa: E402
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


LAST_SKIP: str = ""


def handle(payload: dict[str, Any]) -> dict[str, Any]:
    global LAST_SKIP
    LAST_SKIP = ""
    event = str(payload.get("hook_event_name") or payload.get("hookEventName") or "")
    if event and event not in ("PostToolUse", "post_tool_use"):
        LAST_SKIP = "not a PostToolUse event"
        return {}
    text, is_error, original = extract_result(payload)
    if not text:
        LAST_SKIP = (
            "toolResultTruncated"
            if payload.get("toolResultTruncated")
            else "no tool result text"
        )
        return {}
    if is_error:
        LAST_SKIP = "tool result is an error"
        return {}
    abridged = C.abridge_live(text, is_error=is_error)
    if abridged is None:
        LAST_SKIP = "below live-fat threshold"
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": replace_payload(original, abridged),
        }
    }


USAGE = 'Usage: python compact_hook.py [--help|--version|--verbose] [--file PATH] [--simulate TEXT] [--self-test]\n\nReads one PostToolUse JSON event from stdin (or --file). When the tool result\nis longer than the live-fat threshold and is not an error, emits\nhookSpecificOutput.updatedToolOutput with the abridged text; otherwise prints\n{} and exits 0. Never exits non-zero — fail open.\n--verbose prints the skip reason to stderr when the payload is {}.\n--simulate TEXT runs a synthetic PostToolUse event with TEXT as the tool\nresult — a quick probe of the live-fat decision without crafting JSON.\n--self-test runs handle() on synthetic payloads and exits 1 on failure.\n'


def _self_test() -> int:
    """Run handle() on synthetic PostToolUse payloads; print ok|FAIL per check."""
    checks = {}
    fat = "x" * (C.LIVE_FAT + 512)
    out = handle({"hook_event_name": "PostToolUse", "toolResult": fat})
    updated = out.get("hookSpecificOutput", {}).get("updatedToolOutput")
    checks["fat_abridged"] = (
        isinstance(updated, str)
        and 0 < len(updated) < len(fat)
        and LAST_SKIP == ""
    )
    out = handle({"hook_event_name": "PostToolUse", "toolResult": "tiny"})
    checks["thin_skip"] = out == {} and LAST_SKIP == "below live-fat threshold"
    out = handle(
        {
            "hook_event_name": "PostToolUse",
            "toolResult": {"is_error": True, "text": fat},
        }
    )
    checks["error_skip"] = out == {} and LAST_SKIP == "tool result is an error"
    out = handle({"hook_event_name": "PreToolUse", "toolResult": fat})
    checks["event_skip"] = out == {} and LAST_SKIP == "not a PostToolUse event"
    ok = all(checks.values())
    sys.stdout.write(
        "self-test: %s %s\n"
        % (
            "ok" if ok else "FAIL",
            " ".join(
                "%s=%s" % (k, "ok" if v else "FAIL")
                for k, v in sorted(checks.items())
            ),
        )
    )
    return 0 if ok else 1


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
    if _watch.maybe_version(sys.argv[1:]):
        return 0
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        sys.stdout.write(USAGE)
        return 0
    verbose = "--verbose" in sys.argv[1:]
    if "--self-test" in sys.argv[1:]:
        return _self_test()
    if "--simulate" in sys.argv[1:]:
        idx = sys.argv[1:].index("--simulate")
        text = sys.argv[1:][idx + 1] if idx + 1 < len(sys.argv[1:]) else ""
        out = handle({"hook_event_name": "PostToolUse", "toolResult": text})
        if verbose and not out and LAST_SKIP:
            sys.stderr.write("compact_hook: %s\n" % LAST_SKIP)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 0
    raw = ""
    if "--file" in sys.argv[1:]:
        idx = sys.argv[1:].index("--file")
        file_arg = sys.argv[1:][idx + 1] if idx + 1 < len(sys.argv[1:]) else ""
        if file_arg:
            try:
                raw = Path(file_arg).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                if verbose:
                    sys.stderr.write("compact_hook: unreadable --file %s\n" % file_arg)
                sys.stdout.write("{}\n")
                return 0
    else:
        raw = _read_stdin()
    if not raw.strip():
        if verbose:
            sys.stderr.write("compact_hook: empty stdin\n")
        sys.stdout.write("{}\n")
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        if verbose:
            sys.stderr.write("compact_hook: invalid JSON\n")
        sys.stdout.write("{}\n")
        return 0
    if not isinstance(payload, dict):
        if verbose:
            sys.stderr.write("compact_hook: payload is not an object\n")
        sys.stdout.write("{}\n")
        return 0
    try:
        out = handle(payload)
        text = json.dumps(out, ensure_ascii=False) + "\n"
    except Exception:
        sys.stdout.write("{}\n")
    else:
        if verbose and not out and LAST_SKIP:
            sys.stderr.write("compact_hook: %s\n" % LAST_SKIP)
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
