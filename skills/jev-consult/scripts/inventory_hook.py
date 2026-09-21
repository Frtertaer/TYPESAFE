#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Prompt-hook shortlist of already-installed skills/plugins/MCP.

IDF first, then one Jev Choice+Noul (need_skill) with a short timeout.
Fail open to the IDF list. Does not install anything.
Empty shortlist with task tokens writes .jev-tools-miss.json for peer_fill.py.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from inventory import (  # noqa: E402
    HOOK_LIMIT,
    MISS_NAME,
    SIDECAR_NAME,
    append_decision,
    clear_miss,
    detect_harness,
    explicit_mentions,
    format_miss_note,
    format_note,
    format_winner_note,
    picker_request,
    resolve_picker,
    scan_cached,
    shortlist,
    tokens,
    write_miss,
    write_sidecar,
)

FILL_SCRIPT = _SCRIPTS / "peer_fill.py"
HOOK_JEV_TIMEOUT = 8.0


def _redact_prompt(prompt: str) -> str:
    try:
        import jev as jev_mod

        return jev_mod.redact(prompt)
    except Exception:
        return prompt


def extract_prompt(payload: dict) -> str:
    for key in ("prompt", "user_message", "userMessage"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    history = payload.get("conversation_history") or payload.get("messages")
    if isinstance(history, list):
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("type") or "").lower()
            if role not in {"user", "human"}:
                continue
            content = item.get("content") or item.get("text") or item.get("message")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def extract_cwd(payload: dict) -> Path | None:
    for key in ("cwd", "cwd_path", "workspace"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value)
            if path.is_dir():
                return path
    return None


def event_name(payload: dict) -> str:
    return str(payload.get("hook_event_name") or payload.get("event") or "").strip()


def pick_with_jev(
    task: str,
    harness: str,
    picked: list[dict],
    timeout: float = HOOK_JEV_TIMEOUT,
) -> dict:
    """One Jev call. Never prints keys. Fail-open on missing key, timeout, or errors."""
    if not picked:
        return {"status": "empty", "winner": None}
    try:
        import jev as jev_mod
    except Exception:
        return {"status": "error", "winner": None}
    try:
        jev_mod.load_api_key()
    except SystemExit:
        return {"status": "skip", "winner": None}
    request = picker_request(task, harness, picked)
    try:
        policy = jev_mod.load_policy()
        start = time.perf_counter()
        result = jev_mod.post_systemone(
            request["state"],
            request["questions"],
            policy,
            timeout=timeout,
            retries=0,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        answers = result.get("answers") or {}
        if not isinstance(answers, dict):
            return {"status": "error", "winner": None}
        decision = jev_mod.decide(answers, policy, irreversible=False)
    except SystemExit:
        return {"status": "error", "winner": None}
    except Exception:
        return {"status": "error", "winner": None}
    picker = resolve_picker(picked, decision, policy)
    try:
        need = float((decision.get("picks") or {}).get("need_skill"))
    except (TypeError, ValueError, AttributeError):
        need = None
    picker["need"] = need
    picker["probabilities"] = (decision.get("probabilities") or {}).get("load_tools") or {}
    picker["latency_ms"] = latency_ms
    return picker


def _note_for_picker(picked: list[dict], picker: dict) -> str:
    status = str(picker.get("status") or "")
    winner = picker.get("winner")
    if status == "winner" and isinstance(winner, dict) and winner.get("name"):
        return format_winner_note(winner)
    if status == "none":
        return ""
    return format_note(picked)


def handle(
    payload: dict,
    items: list[dict] | None = None,
    harness: str | None = None,
    pick_fn=None,
) -> dict:
    event = event_name(payload)
    if event and event not in {"UserPromptSubmit", "pre_llm_call"}:
        return {}
    prompt = extract_prompt(payload)
    if not prompt:
        return {}
    harness = harness or detect_harness(Path(__file__))
    catalog = items if items is not None else scan_cached(harness)
    hits = explicit_mentions(prompt, catalog)
    explicit_winner = hits[0] if len(hits) == 1 else None
    picker = {"status": "idf", "winner": None}
    if explicit_winner is not None:
        picked = [explicit_winner]
        picker = {"status": "winner", "winner": explicit_winner}
    else:
        picked = shortlist(catalog, prompt, HOOK_LIMIT, [hit["name"] for hit in hits])
        if picked:
            chooser = pick_fn if pick_fn is not None else pick_with_jev
            try:
                got = chooser(prompt, harness, picked)
                if isinstance(got, dict):
                    picker = got
            except Exception:
                picker = {"status": "error", "winner": None}
    extra = {"jev_status": str(picker.get("status") or "idf")}
    if explicit_winner is not None:
        extra["explicit"] = True
    if picker.get("strong"):
        extra["strong_pick"] = True
    winner = picker.get("winner") if isinstance(picker.get("winner"), dict) else None
    if extra["jev_status"] == "winner" and winner and winner.get("name"):
        extra["jev_pick"] = {"kind": winner.get("kind"), "name": winner.get("name")}
    note = _note_for_picker(picked, picker)
    append_decision(
        {
            "ts": time.time(),
            "harness": harness,
            "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
            "prompt_head": _redact_prompt(prompt[:240])[:160],
            "n_catalog": len(catalog),
            "shortlist": [item.get("id") for item in picked],
            "explicit": explicit_winner is not None,
            "jev_status": extra["jev_status"],
            "need": picker.get("need"),
            "probabilities": picker.get("probabilities") or {},
            "winner": {"kind": winner.get("kind"), "name": winner.get("name")}
            if winner
            else None,
            "strong_pick": bool(picker.get("strong")),
            "latency_ms": picker.get("latency_ms"),
        }
    )
    cwd = extract_cwd(payload)
    if cwd is not None:
        try:
            write_sidecar(cwd / SIDECAR_NAME, harness, prompt, picked, extra)
        except OSError:
            pass
        miss_path = cwd / MISS_NAME
        try:
            if picked:
                clear_miss(miss_path)
            elif tokens(prompt):
                write_miss(miss_path, harness, prompt)
                note = format_miss_note(FILL_SCRIPT)
            else:
                clear_miss(miss_path)
        except OSError:
            pass
    elif not picked and tokens(prompt):
        note = format_miss_note(FILL_SCRIPT)
    if not note:
        return {}
    if event == "pre_llm_call" or harness == "hermes":
        return {"context": note}
    if harness == "grok":
        return {}
    if event == "UserPromptSubmit" or harness in {"claude-code", "codex"}:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": note,
            }
        }
    return {}


def main() -> int:
    raw = sys.stdin.read()
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
    try:
        out = handle(payload)
    except Exception:
        out = {}
    sys.stdout.write(json.dumps(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
