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
import os
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from inventory import (  # noqa: E402
    hook_limit,
    MISS_NAME,
    SIDECAR_NAME,
    append_decision,
    clear_miss,
    detect_harness,
    explicit_mentions,
    format_miss_note,
    format_note,
    format_winner_note,
    hook_budget_seconds,
    hook_note_limit,
    hook_jev_retries,
    hook_jev_timeout_seconds,
    picker_request,
    read_sidecar,
    resolve_picker,
    scan_cached,
    shortlist,
    sidecar_fresh,
    sidecar_items,
    tokens,
    write_miss,
    write_sidecar,
)

FILL_SCRIPT = _SCRIPTS / "peer_fill.py"
HOOK_JEV_TIMEOUT = hook_jev_timeout_seconds()

LAST_DECISION: dict | None = None


def _redact_prompt(prompt: str) -> str:
    try:
        import jev as jev_mod

        return jev_mod.redact(prompt)
    except Exception:
        return prompt


def _content_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [
            str(block.get("text") or "")
            for block in value
            if isinstance(block, dict)
        ]
        return " ".join(part for part in parts if part)
    return ""


def extract_prompt(payload: dict) -> str:
    for key in ("prompt", "user_message", "userMessage"):
        text = _content_text(payload.get(key))
        if text.strip():
            return text.strip()
    history = payload.get("conversation_history") or payload.get("messages")
    if isinstance(history, list):
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("type") or "").lower()
            if role not in {"user", "human"}:
                continue
            text = _content_text(
                item.get("content") or item.get("text") or item.get("message")
            )
            if text.strip():
                return text.strip()
    return ""


def extract_cwd(payload: dict) -> Path | None:
    for key in ("cwd", "cwd_path", "workspace"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            path = Path(value)
            if path.is_dir():
                return path
    env_cwd = os.environ.get("JEV_HOOK_CWD", "").strip()
    if env_cwd:
        path = Path(env_cwd)
        if path.is_dir():
            return path
    return None


def payload_ts(payload: dict) -> float | None:
    for key in ("timestamp", "ts", "time", "created_at"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            ts = float(value)
            return ts / 1000.0 if ts > 1e12 else ts
        if isinstance(value, str):
            text = value.strip()
            if not text:
                continue
            try:
                return float(text)
            except ValueError:
                try:
                    import datetime as _dt

                    parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
                    return parsed.timestamp()
                except ValueError:
                    continue
    return None


def hook_max_age() -> float:
    """Max prompt age in seconds before the hook skips it (0 = off)."""
    try:
        env = float(os.environ.get("JEV_HOOK_MAX_AGE", "") or -1)
        if env >= 0:
            return env
    except ValueError:
        pass
    return 0.0


def event_name(payload: dict) -> str:
    override = os.environ.get("JEV_HOOK_EVENT", "").strip()
    if override:
        return override
    return str(payload.get("hook_event_name") or payload.get("event") or "").strip()


def pick_with_jev(
    task: str,
    harness: str,
    picked: list[dict],
    timeout: float | None = None,
) -> dict:
    """One Jev call. Never prints keys. Fail-open on missing key, timeout, or errors."""
    if timeout is None:
        timeout = hook_jev_timeout_seconds()
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
            retries=hook_jev_retries(),
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
    note_limit = hook_note_limit()
    shown = picked[:note_limit] if note_limit > 0 else picked
    return format_note(shown)


def handle(
    payload: dict,
    items: list[dict] | None = None,
    harness: str | None = None,
    pick_fn=None,
) -> dict:
    global LAST_DECISION
    LAST_DECISION = None
    if os.environ.get("JEV_HOOK_OFF", "").strip() in {"1", "true", "yes"}:
        return {}
    t0 = time.monotonic()
    event = event_name(payload)
    if event and event not in {"UserPromptSubmit", "pre_llm_call"}:
        return {}
    max_age = hook_max_age()
    if max_age > 0:
        ts = payload_ts(payload)
        if ts is not None and time.time() - ts > max_age:
            return {}
    prompt = extract_prompt(payload) or os.environ.get("JEV_HOOK_PROMPT", "").strip()
    if not prompt:
        return {}
    harness = (
        harness
        or os.environ.get("JEV_HOOK_HARNESS", "").strip()
        or detect_harness(Path(__file__))
    )
    cwd = extract_cwd(payload)
    deduped = None
    stale_match = False
    if cwd is not None:
        prior = read_sidecar(cwd / SIDECAR_NAME)
        norm = lambda s: " ".join(str(s or "").split())[:500].lower()
        if prior and norm(prior.get("task")) == norm(prompt):
            if sidecar_fresh(prior):
                deduped = prior
            else:
                stale_match = True
    if deduped is not None:
        picked = sidecar_items(deduped)
        prior_pick = deduped.get("jev_pick")
        winner = None
        if isinstance(prior_pick, dict):
            winner = next(
                (
                    item
                    for item in picked
                    if item.get("name") == prior_pick.get("name")
                    and item.get("kind") == prior_pick.get("kind")
                ),
                None,
            )
        picker = {
            "status": "winner" if winner is not None else "dedupe",
            "winner": winner,
        }
        catalog: list[dict] = []
        explicit_winner = None
        extra = {
            "jev_status": "dedupe" if winner is None else "winner",
            "dedupe": True,
        }
        winner_out = winner if isinstance(winner, dict) else None
        if extra["jev_status"] == "winner" and winner_out and winner_out.get("name"):
            extra["jev_pick"] = {"kind": winner_out.get("kind"), "name": winner_out.get("name")}
        note = _note_for_picker(picked, picker)
        LAST_DECISION = {
            "ts": time.time(),
            "harness": harness,
            "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
            "prompt_head": _redact_prompt(prompt[:240])[:160],
            "n_catalog": 0,
            "shortlist": [item.get("id") for item in picked],
            "explicit": False,
            "dedupe": True,
            "jev_status": extra["jev_status"],
            "winner": {"kind": winner_out.get("kind"), "name": winner_out.get("name")}
            if winner_out
            else None,
        }
        append_decision(LAST_DECISION)
        if not note:
            return {}
        if event == "pre_llm_call" or harness == "hermes":
            return {"context": note}
        if harness == "grok":
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": note,
            }
        }
    catalog = items if items is not None else scan_cached(harness)
    hits = explicit_mentions(prompt, catalog)
    env_winner = os.environ.get("JEV_HOOK_WINNER", "").strip()
    if env_winner:
        lowered = env_winner.lower()
        forced = [
            item
            for item in catalog
            if str(item.get("name") or "").lower() == lowered
            or str(item.get("id") or "").lower() == lowered
        ]
        if forced:
            hits = forced[:1]
    explicit_winner = hits[0] if len(hits) == 1 else None
    picker = {"status": "idf", "winner": None}
    if explicit_winner is not None:
        picked = [explicit_winner]
        picker = {"status": "winner", "winner": explicit_winner}
    else:
        picked = shortlist(catalog, prompt, hook_limit(), [hit["name"] for hit in hits])
        if picked and time.monotonic() - t0 >= hook_budget_seconds():
            picker = {"status": "budget", "winner": None}
        elif picked:
            chooser = pick_fn if pick_fn is not None else pick_with_jev
            try:
                got = chooser(prompt, harness, picked)
                if isinstance(got, dict):
                    picker = got
            except Exception:
                picker = {"status": "error", "winner": None}
    extra = {"jev_status": str(picker.get("status") or "idf")}
    if stale_match:
        extra["stale_sidecar"] = True
    if explicit_winner is not None:
        extra["explicit"] = True
    if picker.get("strong"):
        extra["strong_pick"] = True
    winner = picker.get("winner") if isinstance(picker.get("winner"), dict) else None
    if extra["jev_status"] == "winner" and winner and winner.get("name"):
        extra["jev_pick"] = {"kind": winner.get("kind"), "name": winner.get("name")}
    note = _note_for_picker(picked, picker)
    LAST_DECISION = {
        "ts": time.time(),
        "harness": harness,
        "prompt_sha": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
        "prompt_head": _redact_prompt(prompt[:240])[:160],
        "prompt_tail": _redact_prompt(prompt[-80:]),
        "prompt_len": len(prompt),
        "n_catalog": len(catalog),
        "shortlist_n": len(picked),
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
        "stale_sidecar": stale_match,
    }
    append_decision(LAST_DECISION)
    no_sidecar = os.environ.get("JEV_HOOK_NOSIDECAR", "").strip() in {"1", "true", "yes"}
    if cwd is not None and not no_sidecar:
        try:
            write_sidecar(cwd / SIDECAR_NAME, harness, prompt, picked, extra)
        except OSError:
            pass
        no_miss = os.environ.get("JEV_HOOK_NOMISS", "").strip() in {"1", "true", "yes"}
        miss_path = cwd / MISS_NAME
        try:
            if picked or no_miss:
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


def _debug_enabled(argv: list[str]) -> bool:
    import os

    if "--debug" in argv:
        return True
    return os.environ.get("JEV_HOOK_DEBUG", "").strip().lower() in {"1", "true", "yes"}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
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
    debug_file = os.environ.get("JEV_HOOK_DEBUG_FILE", "").strip()
    if (_debug_enabled(argv) or debug_file) and LAST_DECISION is not None:
        parts = {
            "jev_status": LAST_DECISION.get("jev_status"),
            "winner": (LAST_DECISION.get("winner") or {}).get("name"),
            "dedupe": LAST_DECISION.get("dedupe"),
            "shortlist": len(LAST_DECISION.get("shortlist") or []),
            "latency_ms": LAST_DECISION.get("latency_ms"),
        }
        line = " ".join("%s=%s" % (k, v) for k, v in parts.items() if v is not None)
        if _debug_enabled(argv):
            sys.stderr.write(line + "\n")
        if debug_file:
            try:
                with open(debug_file, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
