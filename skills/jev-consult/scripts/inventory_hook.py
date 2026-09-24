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
import socket
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402
from inventory import (  # noqa: E402
    atomic_write_text,
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
    hook_dedupe_ttl_seconds,
    hook_max_payload_bytes,
    hook_max_prompt_chars,
    hook_note_limit,
    hook_jev_retries,
    hook_jev_timeout_seconds,
    name_df,
    picker_request,
    read_sidecar,
    resolve_picker,
    scan_cached,
    score_item,
    shortlist,
    sidecar_age_seconds,
    sidecar_fresh,
    sidecar_items,
    sidecar_ttl_seconds,
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


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _read_stdin() -> str:
    """stdin as text, decoded utf-8-sig: strips a UTF-8 BOM and avoids
    mojibake when the console codepage isn't utf-8 (Windows cp1252)."""
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


def _avg_score(items: list[dict], pool: list[dict], text: str) -> float | None:
    """Mean IDF score of `items` under the prompt query; None when no query."""
    query = tokens(text)
    if not query or not items:
        return None
    try:
        df = name_df(pool or items, query)
        vals = [score_item(item, query, df) for item in items]
    except Exception:
        return None
    if not vals:
        return None
    return round(sum(vals) / len(vals), 3)


def last_decision_age() -> float | None:
    """Seconds since the last recorded decision, or None if none yet."""
    ts = LAST_DECISION.get("ts") if isinstance(LAST_DECISION, dict) else None
    return (time.time() - ts) if isinstance(ts, (int, float)) else None


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
    return os.environ.get("JEV_HOOK_PROMPT", "").strip()


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
    except SystemExit as err:
        msg = str(err.code or "").lower()
        status = "timeout" if ("timed out" in msg or "timeout" in msg) else "error"
        return {"status": status, "winner": None}
    except Exception as err:
        status = "timeout" if isinstance(err, (TimeoutError, socket.timeout)) else "error"
        return {"status": status, "winner": None}
    picker = resolve_picker(picked, decision, policy)
    try:
        need = float((decision.get("picks") or {}).get("need_skill"))
    except (TypeError, ValueError, AttributeError):
        need = None
    picker["need"] = need
    picker["probabilities"] = (decision.get("probabilities") or {}).get("load_tools") or {}
    picker["latency_ms"] = latency_ms
    picker["question"] = "load_tools"
    return picker


def _status_reason(status: str, winner_name: str | None = None, via: str = "") -> str:
    """One-line human explanation of a jev_status for the routing log."""
    if status == "dedupe":
        return "same prompt inside a fresh sidecar; picks reused without a Jev call"
    if status == "winner":
        if via == "env":
            return "pick forced by JEV_HOOK_WINNER"
        if via == "explicit":
            return "prompt named the item explicitly ($name)"
        if via == "dedupe":
            return "winner carried over from the fresh dedupe sidecar"
        return "jev picked %s from the shortlist" % (winner_name or "an item")
    if status == "none":
        return "jev answered none for the shortlist"
    if status == "idf":
        return "jev unavailable or no ask made; IDF shortlist only"
    if status == "empty":
        return "jev returned an empty answer"
    if status == "error":
        return "pick chooser raised; fell back to the IDF shortlist"
    if status == "budget":
        return "hook budget hit after shortlisting; Jev call skipped"
    if status == "skip":
        return "event skipped by hook config"
    return "status=%s" % status


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
    no_writes: bool = False,
) -> dict:
    global LAST_DECISION
    LAST_DECISION = None
    if _env_on("JEV_HOOK_OFF"):
        return {}
    t0 = time.monotonic()
    event = event_name(payload)
    allowed = allowed_events()
    if event and event not in allowed:
        return {}
    max_age = hook_max_age()
    if max_age > 0:
        ts = payload_ts(payload)
        if ts is not None and time.time() - ts > max_age:
            return {}
    prompt = extract_prompt(payload) or os.environ.get("JEV_HOOK_PROMPT", "").strip()
    if not prompt:
        return {}
    note_tag = os.environ.get("JEV_HOOK_NOTE", "").strip()[:120] or None
    prompt_cap = hook_max_prompt_chars()
    prompt_truncated = bool(prompt_cap) and len(prompt) > prompt_cap
    if prompt_truncated:
        prompt = prompt[:prompt_cap]
    harness = (
        harness
        or os.environ.get("JEV_HOOK_HARNESS", "").strip()
        or detect_harness(Path(__file__))
    )
    cwd = extract_cwd(payload)
    deduped = None
    stale_match = False
    sidecar_age_s = None
    if cwd is not None:
        prior = read_sidecar(cwd / SIDECAR_NAME)
        age = sidecar_age_seconds(prior)
        if age is not None:
            sidecar_age_s = int(age)
        norm = lambda s: " ".join(str(s or "").split())[:500].lower()
        if prior and norm(prior.get("task")) == norm(prompt):
            dedupe_ttl = hook_dedupe_ttl_seconds()
            within_ttl = (
                dedupe_ttl <= 0
                or (age is not None and age <= dedupe_ttl)
            )
            if sidecar_fresh(prior) and within_ttl:
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
            "reason": _status_reason(extra["jev_status"], (winner_out or {}).get("name"), "dedupe"),
            "question": "dedupe",
            "winner": {"kind": winner_out.get("kind"), "name": winner_out.get("name")}
            if winner_out
            else None,
            "sidecar_age_s": sidecar_age_s,
            "shortlist_score_avg": _avg_score(picked, items or picked, prompt),
        }
        if note_tag:
            LAST_DECISION["note"] = note_tag
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
        picker = {
            "status": "winner",
            "winner": explicit_winner,
            "question": "env" if env_winner else "explicit",
        }
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
    if note_tag:
        extra["note"] = note_tag
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
        "prompt_truncated": prompt_truncated,
        "n_catalog": len(catalog),
        "shortlist_n": len(picked),
        "shortlist": [item.get("id") for item in picked],
        "explicit": explicit_winner is not None,
        "jev_status": extra["jev_status"],
        "reason": _status_reason(extra["jev_status"], (winner or {}).get("name"), str(picker.get("question") or "")),
        "question": picker.get("question"),
        "need": picker.get("need"),
        "probabilities": picker.get("probabilities") or {},
        "shortlist_score_avg": _avg_score(picked, catalog, prompt),
        "winner": {"kind": winner.get("kind"), "name": winner.get("name")}
        if winner
        else None,
        "strong_pick": bool(picker.get("strong")),
        "latency_ms": picker.get("latency_ms"),
        "budget_ms": int(hook_budget_seconds() * 1000),
        "over_budget": (
            isinstance(picker.get("latency_ms"), (int, float))
            and picker["latency_ms"] > hook_budget_seconds() * 1000
        ),
        "stale_sidecar": stale_match,
        "sidecar_age_s": sidecar_age_s,
    }
    if note_tag:
        LAST_DECISION["note"] = note_tag
    append_decision(LAST_DECISION)
    if note:
        extra["note_sha"] = hashlib.sha256(note.encode("utf-8")).hexdigest()[:12]
    env_no_sidecar = _env_on("JEV_HOOK_NOSIDECAR")
    env_no_miss = _env_on("JEV_HOOK_NOMISS")
    if cwd is not None and not env_no_sidecar:
        sidecar_path = cwd / (".jev-tools.dry.json" if no_writes else SIDECAR_NAME)
        try:
            write_sidecar(sidecar_path, harness, prompt, picked, extra)
        except OSError:
            pass
        miss_path = cwd / (".jev-tools-miss.dry.json" if no_writes else MISS_NAME)
        try:
            if picked or env_no_miss:
                clear_miss(miss_path)
            elif tokens(prompt):
                miss_extra = {"note": note_tag} if note_tag else {}
                if stale_match and sidecar_age_s is not None:
                    miss_extra["stale_sidecar_age_s"] = sidecar_age_s
                write_miss(miss_path, harness, prompt, miss_extra or None)
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


def _silence_reason(payload: dict) -> str:
    """Why handle() emitted no context — mirrors its early returns in order."""
    if _env_on("JEV_HOOK_OFF"):
        return "hook disabled (JEV_HOOK_OFF)"
    event = event_name(payload) if isinstance(payload, dict) else None
    if event and event not in allowed_events():
        return "skipped: event %r not in allowed set" % event
    max_age = hook_max_age()
    if isinstance(payload, dict) and max_age > 0:
        ts = payload_ts(payload)
        if ts is not None and time.time() - ts > max_age:
            return "skipped: payload older than JEV_HOOK_MAX_AGE"
    prompt = (extract_prompt(payload) if isinstance(payload, dict) else "") or os.environ.get(
        "JEV_HOOK_PROMPT", ""
    ).strip()
    if not prompt:
        return "skipped: no prompt in payload"
    if isinstance(LAST_DECISION, dict):
        return "no context emitted (jev_status=%s)" % LAST_DECISION.get("jev_status")
    return "no context emitted"


def _debug_enabled(argv: list[str]) -> bool:
    import os

    if "--debug" in argv:
        return True
    return _env_on("JEV_HOOK_DEBUG")


def allowed_events() -> set[str]:
    raw = os.environ.get("JEV_HOOK_EVENTS", "").strip()
    if raw:
        allowed = {part.strip() for part in raw.split(",") if part.strip()}
    else:
        allowed = {"UserPromptSubmit", "pre_llm_call"}
    skip = os.environ.get("JEV_HOOK_SKIP_EVENTS", "").strip()
    if skip:
        allowed -= {part.strip() for part in skip.split(",") if part.strip()}
    return allowed


def _float_or_zero(env_name: str) -> float:
    try:
        return max(float(os.environ.get(env_name, "") or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def env_report() -> dict:
    """Resolved hook configuration: effective values for every JEV_HOOK_* knob.
    Values only — never secrets."""
    onoff = ("JEV_HOOK_OFF", "JEV_HOOK_NOSIDECAR", "JEV_HOOK_NOMISS", "JEV_HOOK_DEBUG")
    strings = (
        "JEV_HOOK_HARNESS",
        "JEV_HOOK_CWD",
        "JEV_HOOK_PROMPT",
        "JEV_HOOK_EVENT",
        "JEV_HOOK_EVENTS",
        "JEV_HOOK_SKIP_EVENTS",
        "JEV_HOOK_WINNER",
        "JEV_HOOK_DEBUG_FILE",
    )
    report = {
        "events": sorted(allowed_events()),
        "limit": hook_limit(),
        "note_limit": hook_note_limit(),
        "jev_timeout_seconds": hook_jev_timeout_seconds(),
        "jev_retries": hook_jev_retries(),
        "budget_seconds": hook_budget_seconds(),
        "max_age_seconds": hook_max_age(),
        "max_prompt_chars": hook_max_prompt_chars(),
        "max_payload_bytes": hook_max_payload_bytes(),
        "dedupe_ttl_seconds": hook_dedupe_ttl_seconds(),
        "ttl_seconds": sidecar_ttl_seconds(),
        "watch_max": _watch.cap("JEV_HOOK_WATCH_MAX", None),
        "watch_secs": _float_or_zero("JEV_HOOK_WATCH_SECS"),
        "watch_quiet": _watch.quiet("JEV_HOOK_WATCH_QUIET", False),
        "watch_dedupe": _env_on("JEV_HOOK_WATCH_DEDUPE"),
    }
    for name in onoff:
        report[name.lower()] = os.environ.get(name, "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
    for name in strings:
        report[name.lower()] = bool(os.environ.get(name, "").strip())
    env_policy = os.environ.get("JEV_POLICY", "").strip()
    report["policy"] = env_policy or "default"
    cwd = os.environ.get("JEV_HOOK_CWD", "").strip() or "."
    base = Path(cwd)
    report["sidecar_present"] = (base / SIDECAR_NAME).is_file()
    report["miss_present"] = (base / MISS_NAME).is_file()
    return report


USAGE = 'Usage: python inventory_hook.py [--env|--events|--help] [--dry-run] [--verbose]\n       [--debug] [--file PATH] [--out PATH] [--jq KEY] [--json|--jsonl]\n       [--watch S [--max-ticks N] [--watch-max S] [--fail-fast] [--quiet] [--dedupe]\n       [--verdict PATH]] [--self-test]\n\nReads one hook JSON event from stdin (or --file), shortlists installed items\nagainst the prompt by IDF, asks Jev for at most one pick, writes the sidecar\n.jev-tools.json / miss marker, and prints the hook payload JSON ({} when it\nhas nothing to add — the hook never exits non-zero on a bad event).\n\n  --env      print the resolved JEV_HOOK_* config JSON and exit\n             (limit/ttl/dedupe/budget/payload caps/events/policy source,\n             sidecar+miss presence; --jq KEY prints one value, rc 2 unknown)\n  --events   print allowed hook event names and exit\n             (--json array, --jsonl/--csv/--md rows)\n  --dry-run  resolve the pick writing sidecar/miss as .jev-tools.dry.json /\n             .jev-tools-miss.dry.json instead of the live names\n  --simulate TEXT  run the hook on a synthetic UserPromptSubmit event with TEXT as the prompt and the process cwd (implies --dry-run; "-" reads TEXT from stdin)\n  --verbose  print the one-line reason when the payload would be {}\n  --debug    echo the LAST_DECISION record to stderr\n  --file P   read the event JSON from PATH instead of stdin\n  --out P    also write the emitted payload JSON to PATH (fail-open)\n  --jq KEY   print one dotted-path field of the emitted payload (rc 2 unknown)\n  --version  print the pack policy version and exit\n  --self-test  run the emit machinery on synthetic payloads in a temp dir\n             (no Jev); prints self-test ok|FAIL per check, rc 0/1\n  --watch S  re-run against the file/stdin every S seconds, tick JSON per pass\n  --dedupe   with --watch: skip emitting a tick identical to the previous\n             (ts/elapsed_s ignored; JEV_HOOK_WATCH_DEDUPE presets)\n  --unchanged-max N  with --watch: stop after N consecutive identical ticks\n  --verdict P  write a slim {verdict, ticks, winner, winner_stability, keys} JSON ("-" prints it to stderr instead of a file)\n  --schema   print the emitted payload key contract and exit (--json emits\n             the object); empty payload {} when the hook has nothing to add\n'


def _self_test() -> int:
    """Exercise handle()'s emit paths on synthetic payloads; no Jev calls."""
    checks: dict = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            item = {
                "id": "selftest-item",
                "kind": "skill",
                "name": "selftest-item",
                "description": "self-test item",
                "path": "",
            }
            old_log = os.environ.get("JEV_CONSULT_LOG")
            os.environ["JEV_CONSULT_LOG"] = str(tmp_path / "decisions.jsonl")
            try:
                checks["empty_event"] = handle({}) == {}
                checks["bad_event"] = (
                    handle({"hook_event_name": "Bogus", "prompt": "x"}) == {}
                )
                winner_out = handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "please run selftest-item on this repo",
                        "cwd": str(tmp_path),
                    },
                    items=[item],
                    harness="hermes",
                    no_writes=True,
                )
                checks["explicit_winner"] = (
                    isinstance(winner_out, dict)
                    and bool(winner_out.get("context"))
                    and (tmp_path / ".jev-tools.dry.json").is_file()
                )
                miss_out = handle(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "unrelated task with no matching items",
                        "cwd": str(tmp_path),
                    },
                    items=[],
                    harness="hermes",
                    pick_fn=lambda *a, **kw: {"status": "none", "winner": None},
                    no_writes=True,
                )
                checks["miss_written"] = (
                    isinstance(miss_out, dict)
                    and bool(miss_out.get("context"))
                    and (tmp_path / ".jev-tools-miss.dry.json").is_file()
                )
            finally:
                if old_log is None:
                    os.environ.pop("JEV_CONSULT_LOG", None)
                else:
                    os.environ["JEV_CONSULT_LOG"] = old_log
    except Exception:
        checks = {"raised": False}
    ok = bool(checks) and all(checks.values())
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


# Emitted-payload contract (--schema): {} means the hook adds nothing; the
# non-empty shape depends on the harness.
PAYLOAD_SCHEMA = {
    "context": {"required": False, "type": "string injected note (hermes pre_llm_call)"},
    "hookSpecificOutput": {"required": False, "type": "object (claude-code/codex UserPromptSubmit)"},
    "hookSpecificOutput.hookEventName": {"required": True, "type": "string, always UserPromptSubmit"},
    "hookSpecificOutput.additionalContext": {"required": True, "type": "string injected note"},
}


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    argv = sys.argv[1:] if argv is None else argv
    if _watch.maybe_version(list(argv)):
        return 0
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(USAGE)
        return 0
    no_writes = False
    if "--dry-run" in argv:
        no_writes = True
        argv = [a for a in argv if a != "--dry-run"]
    simulated_raw = ""
    if "--simulate" in argv:
        idx = argv.index("--simulate")
        task = argv[idx + 1] if idx + 1 < len(argv) else ""
        del argv[idx : idx + 2]
        task = _watch.text_arg(task)
        no_writes = True
        simulated_raw = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": task,
                "cwd": str(Path.cwd()),
            }
        )
    if "--events" in argv:
        names = sorted(allowed_events())
        if "--jsonl" in argv:
            for name in names:
                sys.stdout.write(json.dumps({"event": name}) + "\n")
        elif "--csv" in argv:
            _watch.csv_table([{"event": name} for name in names], ["event"])
        elif "--md" in argv:
            _watch.md_table([{"event": name} for name in names], ["event"])
        elif "--json" in argv:
            sys.stdout.write(json.dumps(names) + "\n")
        else:
            for name in names:
                sys.stdout.write(name + "\n")
        return 0
    if "--self-test" in argv:
        return _self_test()
    if "--schema" in argv:
        if "--json" in argv:
            sys.stdout.write(json.dumps(PAYLOAD_SCHEMA, indent=2) + "\n")
        else:
            for key, row in PAYLOAD_SCHEMA.items():
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, row["type"], "required" if row["required"] else "optional")
                )
        return 0
    if "--env" in argv:
        report = env_report()
        if "--jq" in argv:
            idx = argv.index("--jq")
            if idx + 1 < len(argv):
                key = argv[idx + 1]
                if key in report:
                    sys.stdout.write(json.dumps(report[key]) + "\n")
                    return 0
                sys.stderr.write(
                    "bad --jq key %r (env has: %s)\n"
                    % (key, ", ".join(sorted(report)))
                )
                return 2
            sys.stderr.write("--jq needs a KEY value\n")
            return 2
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if "--out" in argv:
            idx = argv.index("--out")
            if idx + 1 < len(argv):
                try:
                    atomic_write_text(Path(argv[idx + 1]), text)
                except OSError:
                    pass  # fail-open: still print to stdout
        sys.stdout.write(text)
        return 0
    raw = ""
    file_path = ""
    if "--file" in argv:
        idx = argv.index("--file")
        if idx + 1 < len(argv):
            file_path = argv[idx + 1]
            try:
                raw = Path(file_path).read_text(encoding="utf-8")
                raw = raw.lstrip("﻿")
            except OSError:
                sys.stdout.write("{}\n")
                return 0
            del argv[idx : idx + 2]
        else:
            sys.stdout.write("{}\n")
            return 0
    watch_seconds = 0.0
    if "--watch" in argv:
        idx = argv.index("--watch")
        if idx + 1 < len(argv):
            try:
                watch_seconds = float(argv[idx + 1])
            except ValueError:
                watch_seconds = 0.0
    if watch_seconds > 0:
        watch_out = ""
        if "--out" in argv:
            idx = argv.index("--out")
            if idx + 1 < len(argv):
                watch_out = argv[idx + 1]
        max_ticks_arg = 0
        if "--max-ticks" in argv:
            idx = argv.index("--max-ticks")
            if idx + 1 < len(argv):
                try:
                    max_ticks_arg = int(argv[idx + 1])
                except ValueError:
                    max_ticks_arg = 0
        max_ticks = _watch.cap("JEV_HOOK_WATCH_MAX", max_ticks_arg)
        quiet = "--quiet" in argv
        watch_max_arg = 0.0
        if "--watch-max" in argv:
            idx = argv.index("--watch-max")
            if idx + 1 < len(argv):
                try:
                    watch_max_arg = float(argv[idx + 1])
                except ValueError:
                    watch_max_arg = 0.0
        dead = _watch.deadline("JEV_HOOK_WATCH_SECS", watch_max_arg)
        fail_fast = "--fail-fast" in argv
        hook_jq = ""
        if "--jq" in argv:
            idx = argv.index("--jq")
            if idx + 1 < len(argv):
                hook_jq = argv[idx + 1]
        verdict_path = ""
        if "--verdict" in argv:
            idx = argv.index("--verdict")
            if idx + 1 < len(argv):
                verdict_path = argv[idx + 1]
        dedupe = "--dedupe" in argv or _env_on("JEV_HOOK_WATCH_DEDUPE")
        unchanged_max = 0
        if "--unchanged-max" in argv:
            idx = argv.index("--unchanged-max")
            if idx + 1 < len(argv):
                try:
                    unchanged_max = int(argv[idx + 1])
                except ValueError:
                    unchanged_max = 0
        ticks = 0
        dupes = 0
        unchanged = 0
        prev_tick: dict | None = None
        tick: dict = {}
        verdict_ok = True
        prev_winner: str | None = None
        winners_seen: set = set()
        winner_changes = 0
        watch_t0 = time.time()

        def _write_verdict() -> bool:
            return _watch.write_verdict(
                verdict_path,
                {
                    "verdict": "pass" if tick.get("winner") else "fail",
                    "ticks": ticks,
                    "winner": tick.get("winner"),
                    "winner_stability": len(winners_seen),
                    "winner_changes": winner_changes,
                    "winner_flap_rate": (
                        round(winner_changes / ticks, 3) if ticks else None
                    ),
                    "keys": tick.get("keys", []),
                    "keys_count": len(tick.get("keys") or []),
                    "dupes": dupes,
                    "elapsed_s": round(time.time() - watch_t0, 2),
                },
                stream=sys.stderr,
            )

        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
            tick = {"ts": int(time.time())}
            try:
                if file_path:
                    raw = Path(file_path).read_text(encoding="utf-8")
                else:
                    raw = _read_stdin()
                raw = raw.lstrip("﻿")
                payload_cap = hook_max_payload_bytes()
                if payload_cap and len(raw.encode("utf-8", "ignore")) > payload_cap:
                    raw = ""
                payload = json.loads(raw) if raw.strip() else {}
                out = (
                    handle(payload, no_writes=True)
                    if (isinstance(payload, dict) and no_writes)
                    else handle(payload)
                    if isinstance(payload, dict)
                    else {}
                )
            except Exception:
                out = {}
            tick["keys"] = sorted(out.keys()) if isinstance(out, dict) else []
            tick["winner"] = (
                ((LAST_DECISION or {}).get("winner") or {}).get("name") or None
            )
            tick["winner_changed"] = (
                prev_winner is not None and tick["winner"] != prev_winner
            )
            prev_winner = tick["winner"]
            if tick["winner_changed"]:
                winner_changes += 1
            if tick["winner"]:
                winners_seen.add(tick["winner"])
            tick["elapsed_s"] = round(time.time() - watch_t0, 2)
            same = _watch.same_tick(prev_tick, tick)
            if same:
                unchanged += 1
            else:
                unchanged = 0
            if dedupe and same:
                dupes += 1
            else:
                _watch.emit_or_jq(tick, hook_jq, watch_out, quiet=_watch.quiet("JEV_HOOK_WATCH_QUIET", quiet), bad=not tick["winner"])
            prev_tick = tick
            ticks += 1
            sys.stderr.write(
                "watch tick=%d winner=%s keys=%s\n"
                % (ticks, tick["winner"] or "-", ",".join(tick["keys"]) or "-")
            )
            if verdict_path and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if fail_fast and not tick["winner"]:
                break
            if unchanged_max and unchanged >= unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            time.sleep(watch_seconds)
        if verdict_path and verdict_ok and not _write_verdict():
            return 1
        return 0 if tick["winner"] else 1
    if not raw:
        raw = simulated_raw or _read_stdin()
    payload_cap = hook_max_payload_bytes()
    if payload_cap and len(raw.encode("utf-8", "ignore")) > payload_cap:
        sys.stdout.write("{}\n")
        return 0
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
        out = (
            handle(payload, no_writes=True) if no_writes else handle(payload)
        )
    except Exception:
        out = {}
    if "--verdict" in argv:
        idx = argv.index("--verdict")
        if idx + 1 < len(argv):
            winner = ((LAST_DECISION or {}).get("winner") or {}).get("name") or None
            _watch.write_verdict(
                argv[idx + 1],
                {
                    "verdict": "pass" if winner else "fail",
                    "ticks": 1,
                    "winner": winner,
                    "keys": sorted(out.keys()) if isinstance(out, dict) else [],
                },
                stream=sys.stderr,
            )
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 < len(argv):
            try:
                atomic_write_text(Path(argv[idx + 1]), json.dumps(out) + "\n")
            except OSError:
                pass
    if "--jq" in argv:
        idx = argv.index("--jq")
        if idx + 1 >= len(argv):
            sys.stderr.write("--jq needs a KEY value\n")
            return 2
        node, found = _watch.dig(out, argv[idx + 1])
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (argv[idx + 1], ", ".join(sorted(out)) if isinstance(out, dict) else "")
            )
            return 2
        sys.stdout.write(json.dumps(node) + "\n")
        return 0
    sys.stdout.write(json.dumps(out) + "\n")
    if "--json" in argv and LAST_DECISION is not None:
        sys.stderr.write(json.dumps(LAST_DECISION, sort_keys=True) + "\n")
    debug_file = os.environ.get("JEV_HOOK_DEBUG_FILE", "").strip()
    if (_debug_enabled(argv) or debug_file) and LAST_DECISION is not None:
        parts = {
            "jev_status": LAST_DECISION.get("jev_status"),
            "winner": (LAST_DECISION.get("winner") or {}).get("name"),
            "question": LAST_DECISION.get("question"),
            "dedupe": LAST_DECISION.get("dedupe"),
            "shortlist": len(LAST_DECISION.get("shortlist") or []),
            "latency_ms": LAST_DECISION.get("latency_ms"),
            "over_budget": LAST_DECISION.get("over_budget"),
            "sidecar_age_s": LAST_DECISION.get("sidecar_age_s"),
            "score_avg": LAST_DECISION.get("shortlist_score_avg"),
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
    if "--verbose" in argv and not out:
        sys.stderr.write("verbose: %s\n" % _silence_reason(payload))
    return 0


if __name__ == "__main__":
    _watch.exit_safely(main())
