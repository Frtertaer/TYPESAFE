#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Durable plan/step file. Jev has no memory; this file is stuffed into each ask."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402


def load_jev():
    path = _SCRIPTS / "jev.py"
    spec = importlib.util.spec_from_file_location("jev_consult_jev", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["jev_consult_jev"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def run_jev(ask_path: Path) -> dict | None:
    """Run `jev.py ask` on the request file; None on any failure."""
    script = _SCRIPTS / "jev.py"
    timeout = 90.0
    env_timeout = os.environ.get("JEV_FILL_TIMEOUT", "").strip()
    try:
        if env_timeout:
            timeout = max(1.0, float(env_timeout))
    except ValueError:
        pass
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "ask", str(ask_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


EMPTY: dict[str, Any] = {
    "plan": "",
    "current_step": "",
    "attempt_count": 0,
    "last_error": "",
    "unknown": "",
    "inspected": [],
    "last_pick": "",
    "history": [],
    "notes": [],
}


def default_path() -> Path:
    env = os.environ.get("JEV_TRACE", "").strip()
    return Path(env) if env else Path(".jev-trace.json")


def empty() -> dict[str, Any]:
    data = dict(EMPTY)
    data["inspected"] = []
    data["history"] = []
    data["notes"] = []
    return data


_STDIN_TRACE: dict | None = None
_STDIN_READ = False


def _stdin_raw() -> dict | None:
    """Parse the '--file -' trace JSON from stdin once (cached)."""
    global _STDIN_TRACE, _STDIN_READ
    if not _STDIN_READ:
        _STDIN_READ = True
        try:
            raw = json.loads(sys.stdin.read())
        except ValueError:
            raw = None
        _STDIN_TRACE = raw if isinstance(raw, dict) else None
    return _STDIN_TRACE


def _exists(path: Path) -> bool:
    """A '-' path always 'exists' — its stdin content may still be invalid."""
    return str(path) == "-" or path.is_file()


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or default_path()
    if str(path) == "-":
        raw = _stdin_raw()
        if raw is None:
            return empty()
    elif not path.is_file():
        return empty()
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty()
    if not isinstance(raw, dict):
        return empty()
    data = empty()
    for key in EMPTY:
        if key not in raw:
            continue
        data[key] = raw[key]
    try:
        data["attempt_count"] = int(data.get("attempt_count") or 0)
    except (TypeError, ValueError):
        data["attempt_count"] = 0
    if not isinstance(data.get("notes"), list):
        data["notes"] = []
    if not isinstance(data.get("inspected"), list):
        data["inspected"] = []
    if not isinstance(data.get("history"), list):
        data["history"] = []
    return data


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def save(data: dict[str, Any], path: Path | None = None) -> Path:
    path = path or default_path()
    if str(path) == "-":
        sys.stderr.write("--file - (stdin) is read-only\n")
        raise SystemExit(1)
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return path


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _dig(item: dict, key: str):
    node = item
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _ts_arg(raw: str) -> float | None:
    """Parse an epoch-seconds or ISO8601 timestamp argument. Empty -> 0."""
    text = (raw or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        import datetime as _dt

        parsed = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


def merge_state(state: Any, trace: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {"trace": dict(trace), "request": state}
    merged = dict(trace)
    for key, value in state.items():
        if _present(value):
            merged[key] = value
    return merged


def bump(trace: dict[str, Any], error: str = "") -> dict[str, Any]:
    data = dict(trace)
    data["attempt_count"] = int(data.get("attempt_count") or 0) + 1
    if error:
        data["last_error"] = error
    return data


def record(trace: dict[str, Any], pick: str, kind: str = "") -> dict[str, Any]:
    data = dict(trace)
    data["last_pick"] = pick
    history = list(data.get("history") or [])
    now = time.time()
    entry: dict[str, Any] = {
        "pick": pick,
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
    }
    if kind:
        entry["kind"] = kind
    history.append(entry)
    data["history"] = history[-20:]
    return data


def emit(payload: Any, jq: str = "") -> int:
    """Print payload JSON; with jq, print just that dotted field (rc 2 on miss).
    A comma list `a,b` digs every field and emits them as an object."""
    if jq:
        fields = [f.strip() for f in jq.split(",") if f.strip()]
        values: dict[str, Any] = {}
        missing = ""
        for field in fields:
            if isinstance(payload, dict):
                value, found = jq_lookup(payload, field)
            else:
                value, found = None, False
            if not found:
                missing = field
                break
            values[field] = value
        if missing or not fields:
            bad = missing or jq
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (
                    bad,
                    ", ".join(sorted(payload)) if isinstance(payload, dict) else "-",
                )
            )
            return 2
        out = values[fields[0]] if len(fields) == 1 else values
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 0
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    plan = args.plan if args.plan is not None else os.environ.get("JEV_TRACE_PLAN", "")
    if not plan.strip():
        sys.stderr.write("trace init requires --plan or JEV_TRACE_PLAN\n")
        return 2
    data = empty()
    data["plan"] = plan
    if args.step:
        data["current_step"] = args.step
    path = Path(args.file) if args.file else default_path()
    save(data, path)
    return emit({"path": str(path), "trace": data}, getattr(args, "jq", ""))


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    exists = _exists(path)
    if getattr(args, "jq", ""):
        fields = [f.strip() for f in args.jq.split(",") if f.strip()]
        values: dict[str, Any] = {}
        missing = ""
        for field in fields:
            value, found = _watch.dig(data, field)
            if not found:
                missing = field
                break
            values[field] = value
        if missing or not fields:
            bad = missing or args.jq
            sys.stderr.write(
                "bad --jq key %r (trace has: %s)\n"
                % (bad, ", ".join(sorted(data)) if isinstance(data, dict) else "-")
            )
            return 2
        out = values[fields[0]] if len(fields) == 1 else values
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 0
    key = getattr(args, "key", "")
    if key:
        value = data.get(key)
        if isinstance(value, (dict, list)):
            sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
        elif value is None:
            sys.stdout.write("\n")
        else:
            sys.stdout.write("%s\n" % value)
        return 0
    age_seconds = None
    if exists and str(path) != "-":
        try:
            age_seconds = max(0.0, round(time.time() - path.stat().st_mtime, 3))
        except OSError:
            age_seconds = None
    if getattr(args, "pretty", False):
        lines = [
            "file: %s%s" % (path, "" if exists else " (missing)"),
        ]
        if age_seconds is not None:
            lines.append("age: %ss" % age_seconds)
        for key in ("plan", "current_step", "attempt_count", "last_pick", "last_error"):
            value = data.get(key)
            if _present(value):
                lines.append("%s: %s" % (key, value))
        sys.stdout.write("\n".join(lines) + "\n")
        return 0
    out_path = getattr(args, "out", "") or ""
    if out_path:
        try:
            _atomic_write(
                Path(out_path),
                json.dumps({"path": str(path), "exists": exists, "age_seconds": age_seconds, "trace": data}, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %s\n" % out_path)
        return 0
    emit({"path": str(path), "exists": exists, "age_seconds": age_seconds, "trace": data})
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    if args.plan is not None:
        data["plan"] = args.plan
    if args.step is not None:
        data["current_step"] = args.step
    if args.unknown is not None:
        data["unknown"] = args.unknown
    if args.error is not None:
        data["last_error"] = args.error
    if args.attempt is not None:
        data["attempt_count"] = int(args.attempt)
    for pair in args.kv or []:
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key = key.strip()
        if key:
            data[key] = value.strip()
    if getattr(args, "dry_run", False):
        return emit(
            {"path": str(path), "trace": data, "dry_run": True},
            getattr(args, "jq", ""),
        )
    save(data, path)
    return emit({"path": str(path), "trace": data}, getattr(args, "jq", ""))


def cmd_bump(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else default_path()
    data = bump(load(path), error=args.error or "")
    save(data, path)
    return emit({"path": str(path), "trace": data}, getattr(args, "jq", ""))


def cmd_record(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else default_path()
    data = record(load(path), pick=args.pick, kind=args.kind or "")
    if args.step:
        data["current_step"] = args.step
    note_arg = args.note if args.note is not None else os.environ.get("JEV_TRACE_NOTE", "")
    note_text = sys.stdin.read().strip() if note_arg == "-" else note_arg
    if note_text:
        notes = data.get("notes")
        if not isinstance(notes, list):
            notes = []
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        entry = {
            "ts": now,
            "iso": iso,
            "text": note_text,
            "sha": hashlib.sha256(note_text.encode("utf-8")).hexdigest()[:12],
        }
        harness = args.harness or os.environ.get("JEV_TRACE_HARNESS", "")
        if harness:
            entry["harness"] = harness
        notes.append(entry)
        data["notes"] = notes[-50:]
    save(data, path)
    return emit({"path": str(path), "trace": data}, getattr(args, "jq", ""))


def cmd_suggest(args: argparse.Namespace) -> int:
    """Ask Jev for the next move using a policy template + the trace state,
    then record the pick into the trace history."""
    path = Path(args.file) if args.file else default_path()
    trace = load(path)
    question_name = args.template or "next_move"
    jev = load_jev()
    question: dict[str, Any] = {}
    if jev is not None:
        try:
            policy = jev.load_policy()
        except SystemExit:
            policy = {}
        template = (policy.get("templates") or {}).get(question_name) or {}
        if isinstance(template, dict):
            question = dict(template)
    if not isinstance(question.get("criteria"), dict) or not question["criteria"]:
        sys.stderr.write(
            "template %r has no criteria in policy.json\n" % question_name
        )
        return 2
    question.setdefault("type", "choice")
    question.setdefault("instructions", "Which move next?")
    state = {
        "plan": trace.get("plan"),
        "current_step": trace.get("current_step"),
        "attempt_count": trace.get("attempt_count"),
        "last_error": trace.get("last_error"),
        "last_pick": trace.get("last_pick"),
        "inspected": trace.get("inspected") or [],
    }
    task = _watch.text_arg(args.task)
    if task:
        state["task"] = task
    request = {"state": state, "questions": {question_name: question}}
    if args.out:
        try:
            _atomic_write(
                Path(args.out),
                json.dumps(request, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
    if args.dry_run:
        emit(request)
        return 0
    pick = args.pick
    confidence = None
    if not pick:
        ask_path = (
            Path(args.ask_file)
            if args.ask_file
            else path.with_name("jev-suggest.ask.json")
        )
        _atomic_write(
            ask_path, json.dumps(request, indent=2, ensure_ascii=False) + "\n"
        )
        resp = run_jev(ask_path)
        if resp is None:
            sys.stderr.write("jev ask failed; request saved to %s\n" % ask_path)
            return 1
        answers = resp.get("answers") if isinstance(resp, dict) else {}
        answer = answers.get(question_name) if isinstance(answers, dict) else {}
        if isinstance(answer, dict):
            pick = str(answer.get("choice") or "")
            confidence = answer.get("confidence")
    if not pick:
        sys.stderr.write("no pick\n")
        return 1
    data = record(trace, pick=pick, kind=args.kind or "suggest")
    save(data, path)
    payload: dict[str, Any] = {
        "path": str(path),
        "question": question_name,
        "pick": pick,
        "trace": data,
    }
    if confidence is not None:
        payload["confidence"] = confidence
    rc = emit_jq(payload, args.jq)
    if rc is not None:
        return rc
    emit(payload)
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """List recorded picks (trace.history), newest last."""
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    history = data.get("history")
    history = [h for h in history if isinstance(h, dict)] if isinstance(history, list) else []
    def _filtered(items: list) -> list | None:
        needle = (
            getattr(args, "grep", "") or os.environ.get("JEV_TRACE_HISTORY_GREP", "")
        ).strip().lower()
        if needle:
            items = [
                h
                for h in items
                if needle in str(h.get("pick") or "").lower()
                or needle in str(h.get("kind") or "").lower()
            ]
        want_kinds = {
            k.strip() for k in (getattr(args, "kind", "") or "").split(",") if k.strip()
        }
        if want_kinds:
            items = [h for h in items if str(h.get("kind") or "") in want_kinds]
        for bound, op in ((getattr(args, "since", None), ">="), (getattr(args, "before", None), "<=")):
            if bound is None:
                continue
            bound_ts = _ts_arg(bound)
            if bound_ts is None:
                sys.stderr.write("bad time bound: %s\n" % bound)
                return None
            items = [
                h
                for h in items
                if isinstance(h.get("ts"), (int, float))
                and not isinstance(h.get("ts"), bool)
                and (h["ts"] >= bound_ts if op == ">=" else h["ts"] <= bound_ts)
            ]
        if getattr(args, "uniq", False):
            seen_picks: set[tuple[str, str]] = set()
            deduped = []
            for h in items:
                key = (str(h.get("pick") or ""), str(h.get("kind") or ""))
                if key in seen_picks:
                    continue
                seen_picks.add(key)
                deduped.append(h)
            items = deduped
        first = getattr(args, "first", None)
        if isinstance(first, int) and first >= 0:
            items = items[:first]
        limit = getattr(args, "limit", None)
        if isinstance(limit, int) and limit >= 0:
            items = items[-limit:] if limit else []
        if getattr(args, "reverse", False):
            items = items[::-1]
        return items

    history = _filtered(history)
    if history is None:
        return 2

    if getattr(args, "count", False):
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"count": len(history)}, ensure_ascii=False) + "\n"
            )
        else:
            sys.stdout.write("%d\n" % len(history))
        return 0

    if getattr(args, "kinds", False):
        counts: dict[str, int] = {}
        for entry in history:
            kind = str(entry.get("kind") or "")
            counts[kind] = counts.get(kind, 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"kinds": dict(rows)}, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            for kind, n in rows:
                sys.stdout.write("%s %d\n" % (kind or "-", n))
        return 0

    if getattr(args, "rate", False):
        stamps = [
            float(h["ts"])
            for h in history
            if isinstance(h.get("ts"), (int, float)) and not isinstance(h.get("ts"), bool)
        ]
        per_day: dict[str, int] = {}
        for stamp in stamps:
            day = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
            per_day[day] = per_day.get(day, 0) + 1
        span_s = (max(stamps) - min(stamps)) if len(stamps) >= 2 else 0.0
        days = max(1, int(span_s // 86400) + 1) if stamps else 0
        data_out = {
            "count": len(history),
            "stamped": len(stamps),
            "first_ts": min(stamps) if stamps else None,
            "last_ts": max(stamps) if stamps else None,
            "span_s": round(span_s, 3),
            "days": days,
            "picks_per_day": round(len(stamps) / days, 3) if days else 0.0,
            "per_day": dict(sorted(per_day.items())),
        }
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"rate": data_out}, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            sys.stdout.write(
                "count %d\nstamped %d\nspan_s %s\ndays %d\npicks_per_day %s\n"
                % (
                    data_out["count"],
                    data_out["stamped"],
                    data_out["span_s"],
                    data_out["days"],
                    data_out["picks_per_day"],
                )
            )
            for day, n in sorted(per_day.items()):
                sys.stdout.write("%s %d\n" % (day, n))
        return 0

    if getattr(args, "gap", 0.0) and args.gap > 0:
        chronological = sorted(
            history,
            key=lambda h: h.get("ts")
            if isinstance(h.get("ts"), (int, float)) and not isinstance(h.get("ts"), bool)
            else 0,
        )
        gaps = []
        prev = None
        for i, entry in enumerate(chronological):
            ts = entry.get("ts")
            if not (isinstance(ts, (int, float)) and not isinstance(ts, bool)):
                continue
            if prev is not None and float(ts) - float(prev["ts"]) > args.gap:
                gaps.append(
                    {
                        "index": i,
                        "prev_ts": prev["ts"],
                        "ts": ts,
                        "gap_s": round(float(ts) - float(prev["ts"]), 3),
                        "prev_pick": prev.get("pick"),
                        "pick": entry.get("pick"),
                    }
                )
            prev = {"ts": ts, "pick": entry.get("pick")}
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"gaps": gaps}, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            for g in gaps:
                sys.stdout.write(
                    "%d %.3f %s -> %s\n"
                    % (g["index"], g["gap_s"], g["prev_pick"], g["pick"])
                )
        return 0

    if getattr(args, "watch", 0.0) and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_TRACE_WATCH_MAX", getattr(args, "max_ticks", 0))
        ticks = 0
        dead = _watch.deadline("JEV_TRACE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        tick: dict = {}
        verdict_ok = True
        prev_tick: dict | None = None
        unchanged = 0

        def _write_verdict() -> bool:
            picks = tick.get("picks")
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "picks" if picks else "empty",
                    "ticks": ticks,
                    "picks": picks,
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            fresh = load(path).get("history")
            fresh = (
                [h for h in fresh if isinstance(h, dict)]
                if isinstance(fresh, list)
                else []
            )
            filtered = _filtered(fresh)
            tick = {
                "ts": int(_time.time()),
                "picks": len(filtered) if filtered is not None else None,
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, getattr(args, "jq", ""), getattr(args, "out", "") or None, quiet=_watch.quiet("JEV_TRACE_WATCH_QUIET", getattr(args, "quiet", False)), bad=bool(tick["picks"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d picks=%s\n" % (ticks, tick["picks"])
            )
            if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "fail_fast", False) and not tick["picks"]:
                break
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(args.watch)
        if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
            return 1
        return 0
    if getattr(args, "verdict", "") and not _watch.write_verdict(
        args.verdict,
        {
            "verdict": "picks" if history else "empty",
            "ticks": 1,
            "picks": len(history),
        },
    ):
        return 1
    field = getattr(args, "field", "") or ""
    if field:
        values = [_dig(entry, field) for entry in history]
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"field": field, "values": values}, ensure_ascii=False, indent=2) + "\n"
            )
            return 0
        for value in values:
            sys.stdout.write(
                (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if value is not None else "null")
                + "\n"
            )
        return 0
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(history, ensure_ascii=False, indent=2) + "\n")
        return 0
    for entry in history:
        kind = str(entry.get("kind") or "")
        line = str(entry.get("pick") or "")
        sys.stdout.write("%s %s\n" % (kind, line) if kind else line + "\n")
    sys.stdout.write("%d pick(s)\n" % len(history))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Delete the trace file when its mtime is older than --older-than seconds."""
    path = Path(args.file) if args.file else default_path()
    jq = getattr(args, "jq", "")
    if str(path) == "-":
        return emit({"path": "-", "removed": False, "reason": "stdin is read-only"}, jq)
    if not path.is_file():
        return emit({"path": str(path), "removed": False, "reason": "missing"}, jq)
    try:
        path_mtime = path.stat().st_mtime
        age = time.time() - path_mtime
    except OSError as exc:
        return emit({"path": str(path), "removed": False, "reason": "stat failed: %s" % exc}, jq)
    if age < float(args.older_than):
        return emit({"path": str(path), "removed": False, "reason": "fresh", "age_seconds": round(age, 3)}, jq)
    if getattr(args, "dry_run", False):
        return emit({"path": str(path), "removed": False, "reason": "dry-run", "age_seconds": round(age, 3), "would_remove": True}, jq)
    try:
        # Re-check right before unlink: a save landing between stat() and
        # unlink() must not lose the fresh trace.
        if path.stat().st_mtime != path_mtime:
            return emit({"path": str(path), "removed": False, "reason": "changed"}, jq)
        path.unlink()
    except OSError as exc:
        rc = emit({"path": str(path), "removed": False, "reason": "unlink failed: %s" % exc}, jq)
        return rc or 1
    return emit({"path": str(path), "removed": True, "age_seconds": round(age, 3)}, jq)


def jq_lookup(obj, path: str):
    """Dotted-path dict traversal: returns (value, True) or (None, False)."""
    node = obj
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None, False
    return node, True


def emit_jq(payload: dict, jq: str) -> int | None:
    """When --jq is set, print just that dotted field and return an rc; else None.
    A comma list `a,b` digs every field and emits them as an object."""
    if not jq:
        return None
    fields = [f.strip() for f in jq.split(",") if f.strip()]
    values: dict[str, Any] = {}
    missing = ""
    for field in fields:
        value, found = jq_lookup(payload, field)
        if not found:
            missing = field
            break
        values[field] = value
    if missing or not fields:
        bad = missing or jq
        sys.stderr.write(
            "bad --jq key %r (payload has: %s)\n"
            % (bad, ", ".join(sorted(payload)))
        )
        return 2
    out = values[fields[0]] if len(fields) == 1 else values
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    """Resolved trace.py environment. Values only — never secrets."""
    path = Path(args.file) if args.file else default_path()
    policy = os.environ.get("JEV_POLICY", "").strip()
    try:
        watch_secs = float(os.environ.get("JEV_TRACE_WATCH_SECS", "") or 0)
    except ValueError:
        watch_secs = 0.0
    try:
        fill_timeout = float(os.environ.get("JEV_FILL_TIMEOUT", "") or 90)
    except ValueError:
        fill_timeout = 90.0
    report = {
        "file": str(path),
        "exists": _exists(path),
        "fill_timeout_seconds": fill_timeout,
        "plan_set": bool(os.environ.get("JEV_TRACE_PLAN", "").strip()),
        "policy": policy if policy else "default",
        "watch_max": _watch.cap("JEV_TRACE_WATCH_MAX", None),
        "watch_secs": watch_secs,
        "watch_quiet": _watch.quiet("JEV_TRACE_WATCH_QUIET", False),
    }
    if getattr(args, "jq", ""):
        fields = [f.strip() for f in args.jq.split(",") if f.strip()]
        values: dict[str, Any] = {}
        missing = ""
        for field in fields:
            node, found = _watch.dig(report, field)
            if not found:
                missing = field
                break
            values[field] = node
        if missing or not fields:
            bad = missing or args.jq
            sys.stderr.write(
                "bad --jq key %r (env has: %s)\n"
                % (bad, ", ".join(sorted(report)))
            )
            return 2
        out = values[fields[0]] if len(fields) == 1 else values
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        return 0
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text)
    if getattr(args, "out", ""):
        try:
            _atomic_write(Path(args.out), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    """Print the .jev-trace.json key contract (--json emits the object)."""
    rows = {
        "plan": {"required": True, "type": "string, human plan text"},
        "current_step": {"required": True, "type": "string"},
        "attempt_count": {"required": True, "type": "int, bump increments"},
        "last_error": {"required": True, "type": "string, last failure note"},
        "unknown": {"required": True, "type": "string, unknown-area note"},
        "inspected": {"required": True, "type": "list[string], files checked"},
        "last_pick": {"required": True, "type": "string, newest Jev pick"},
        "history": {"required": True, "type": "list[pick], last 20 records"},
        "notes": {"required": True, "type": "list[{iso, text}], last 50 notes"},
    }
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(rows, indent=2) + "\n")
    else:
        for key in rows:
            sys.stdout.write(
                "%s: %s (%s)\n"
                % (key, rows[key]["type"], "required" if rows[key]["required"] else "optional")
            )
    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".jev-trace.json"
        data = empty()
        data["plan"] = "self-test"
        save(data, path)
        save(record(load(path), "self-test-pick", kind="self_test"), path)
        back = load(path)
    history = back.get("history") or []
    ok = back.get("last_pick") == "self-test-pick" and any(
        isinstance(h, dict) and h.get("pick") == "self-test-pick" for h in history
    )
    rc = emit(
        {
            "self_test": "ok" if ok else "FAIL",
            "history": len(history),
            "last_pick": back.get("last_pick"),
        },
        getattr(args, "jq", ""),
    )
    if rc:
        return rc
    return 0 if ok else 1


def cmd_export(args: argparse.Namespace) -> int:
    """Dump the whole trace bundle (state, history, notes, counts) as JSON."""
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    since_ts = None
    before_ts = None
    since = getattr(args, "since", None)
    if since is not None:
        since_ts = _ts_arg(since)
        if since_ts is None:
            sys.stderr.write("bad --since: %s\n" % since)
            return 2
    before = getattr(args, "before", None)
    if before is not None:
        before_ts = _ts_arg(before)
        if before_ts is None:
            sys.stderr.write("bad --before: %s\n" % before)
            return 2
    if since_ts is not None or before_ts is not None:
        def _in_window(item) -> bool:
            ts = item.get("ts") if isinstance(item, dict) else None
            if not isinstance(ts, (int, float)):
                return False
            if since_ts is not None and ts < since_ts:
                return False
            if before_ts is not None and ts > before_ts:
                return False
            return True
        for key in ("history", "notes"):
            items = data.get(key)
            if isinstance(items, list):
                data[key] = [item for item in items if _in_window(item)]
    kinds_raw = getattr(args, "kinds", "") or ""
    if kinds_raw.strip():
        wanted = {k.strip() for k in kinds_raw.split(",") if k.strip()}
        hist = data.get("history")
        if isinstance(hist, list):
            data["history"] = [
                h
                for h in hist
                if isinstance(h, dict) and str(h.get("kind") or "") in wanted
            ]
    data["file"] = str(path)
    rc = emit_jq(data, getattr(args, "jq", ""))
    if rc is not None:
        return rc
    if getattr(args, "csv", False):
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["pick", "ts", "iso", "kind"])
        for h in data.get("history") or []:
            if isinstance(h, dict):
                writer.writerow(
                    [
                        h.get("pick") or "",
                        h.get("ts") or "",
                        h.get("iso") or "",
                        h.get("kind") or "",
                    ]
                )
        out_text = buf.getvalue()
    elif getattr(args, "md", False):
        def _cell(v) -> str:
            return str(v or "").replace("|", "\\|").replace("\n", " ")

        lines = ["# trace export", "", "file: %s" % data.get("file", ""), ""]
        history_rows = [h for h in data.get("history") or [] if isinstance(h, dict)]
        lines.append("## history (%d)" % len(history_rows))
        lines.append("")
        lines.append("| pick | ts | iso | kind |")
        lines.append("| --- | --- | --- | --- |")
        for h in history_rows:
            lines.append(
                "| %s | %s | %s | %s |"
                % (_cell(h.get("pick")), _cell(h.get("ts")), _cell(h.get("iso")), _cell(h.get("kind")))
            )
        note_rows = [n for n in data.get("notes") or [] if isinstance(n, dict)]
        lines.append("")
        lines.append("## notes (%d)" % len(note_rows))
        lines.append("")
        lines.append("| iso | harness | text | sha |")
        lines.append("| --- | --- | --- | --- |")
        for n in note_rows:
            lines.append(
                "| %s | %s | %s | %s |"
                % (_cell(n.get("iso")), _cell(n.get("harness")), _cell(n.get("text")), _cell(n.get("sha")))
            )
        out_text = "\n".join(lines) + "\n"
    else:
        out_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    out_path = getattr(args, "out", "") or ""
    if out_path:
        try:
            _atomic_write(Path(out_path), out_text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write(
            "wrote trace export%s to %s\n"
            % (
                " (csv)" if getattr(args, "csv", False) else (" (md)" if getattr(args, "md", False) else ""),
                out_path,
            )
        )
        return 0
    sys.stdout.write(out_text)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-check every note's stored `sha` against sha256(text)[:12].

    Integrity audit for the trace file — a rewritten/corrupted note whose
    sha was not recomputed shows up here. Missing file or unreadable JSON
    fail the audit (rc 1) rather than reporting a vacuous pass.
    """
    path = Path(args.file) if args.file else default_path()
    payload: dict = {"path": str(path), "ok": True, "notes": 0, "checked": 0, "skipped": 0, "bad": []}
    if str(path) == "-":
        raw = _stdin_raw()
        missing = False
    elif not path.is_file():
        raw = None
        missing = True
    else:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        missing = False
    if missing:
        payload["ok"] = False
        payload["missing"] = True
    elif not isinstance(raw, dict):
        payload["ok"] = False
        payload["error"] = "unreadable"
    else:
        notes = raw.get("notes")
        notes = notes if isinstance(notes, list) else []
        payload["notes"] = len(notes)
        for i, note in enumerate(notes, 1):
            if not isinstance(note, dict) or not note.get("sha"):
                payload["skipped"] += 1
                continue
            computed = hashlib.sha256(str(note.get("text") or "").encode("utf-8")).hexdigest()[:12]
            if computed != note["sha"]:
                payload["bad"].append({"index": i, "stored": note["sha"], "computed": computed})
            else:
                payload["checked"] += 1
        if payload["bad"] and getattr(args, "fix", False):
            if str(path) == "-":
                payload["error"] = "--fix needs a writable file"
            else:
                for row in payload["bad"]:
                    notes[row["index"] - 1]["sha"] = row["computed"]
                try:
                    save(raw, path)
                    payload["fixed"] = len(payload["bad"])
                    payload["checked"] = payload["notes"] - payload["skipped"]
                    payload["bad"] = []
                except OSError as exc:
                    payload["error"] = "fix failed: %s" % exc
        payload["ok"] = not payload["bad"] and "error" not in payload
    if getattr(args, "verdict", ""):
        _watch.write_verdict(
            args.verdict,
            {
                "verdict": "ok" if payload["ok"] else "fail",
                "notes": payload["notes"],
                "checked": payload["checked"],
                "bad": len(payload["bad"]),
            },
        )
    rc = emit_jq(payload, getattr(args, "jq", ""))
    if rc is not None:
        return rc
    out_path = getattr(args, "out", "") or ""
    if out_path:
        try:
            _atomic_write(Path(out_path), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    elif payload["ok"]:
        sys.stdout.write(
            "verify: ok notes=%d checked=%d skipped=%d%s\n"
            % (
                payload["notes"],
                payload["checked"],
                payload["skipped"],
                " fixed=%d" % payload["fixed"] if payload.get("fixed") else "",
            )
        )
    else:
        detail = payload.get("error") or ("missing" if payload.get("missing") else "bad=%d" % len(payload["bad"]))
        sys.stdout.write("verify: FAIL %s\n" % detail)
        for row in payload["bad"][:10]:
            sys.stdout.write("note %d: stored %s != computed %s\n" % (row["index"], row["stored"], row["computed"]))
    return 0 if payload["ok"] else 1


def cmd_notes(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    notes = data.get("notes")
    notes = notes if isinstance(notes, list) else []
    prune = getattr(args, "prune", None)
    if isinstance(prune, int) and prune >= 0:
        data["notes"] = notes[-prune:] if prune else []
        try:
            save(data, path)
            sys.stderr.write("notes pruned to %d\n" % len(data["notes"]))
        except OSError as exc:
            sys.stderr.write("prune failed: %s\n" % exc)
            return 1
        notes = data["notes"]
    edit = getattr(args, "edit", None)
    if edit:
        spec = str(edit[0]).strip()
        lo = hi = 0
        if "-" in spec.lstrip("-"):
            parts = spec.split("-", 1)
            try:
                lo, hi = int(parts[0]), int(parts[1])
            except (TypeError, ValueError):
                sys.stderr.write("bad --edit range: %s\n" % edit[0])
                return 2
            if lo > hi:
                lo, hi = hi, lo
        else:
            try:
                lo = hi = int(spec)
            except (TypeError, ValueError):
                sys.stderr.write("bad --edit index: %s\n" % edit[0])
                return 2
        if (
            lo < 1
            or hi > len(notes)
            or any(not isinstance(notes[i - 1], dict) for i in range(lo, hi + 1))
        ):
            sys.stderr.write(
                "--edit %s out of range (%d notes)\n" % (spec, len(notes))
            )
            return 2
        new_sha = hashlib.sha256(edit[1].encode("utf-8")).hexdigest()[:12]
        for i in range(lo, hi + 1):
            notes[i - 1]["text"] = edit[1]
            notes[i - 1]["sha"] = new_sha
        try:
            save(data, path)
            sys.stderr.write(
                "note %d updated\n" % lo
                if lo == hi
                else "notes %d-%d updated\n" % (lo, hi)
            )
        except OSError as exc:
            sys.stderr.write("edit failed: %s\n" % exc)
            return 1
    context = getattr(args, "context", None)
    if context is not None:
        try:
            center = int(str(context).strip())
        except (TypeError, ValueError):
            sys.stderr.write("bad --context index: %s\n" % context)
            return 2
        around = getattr(args, "around", 2)
        if not isinstance(around, int) or around < 0:
            sys.stderr.write("bad --around: %s\n" % around)
            return 2
        if center < 1 or center > len(notes):
            sys.stderr.write(
                "--context %d out of range (%d notes)\n" % (center, len(notes))
            )
            return 2
        lo = max(0, center - 1 - around)
        hi = min(len(notes), center + around)
        window = notes[lo:hi]
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps(
                    {"index": center, "around": around, "notes": window},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        else:
            lines = []
            for offset, note in enumerate(window):
                if isinstance(note, dict):
                    stamp = str(note.get("iso") or int(note.get("ts") or 0))
                    mark = ">" if lo + offset + 1 == center else " "
                    lines.append(
                        "%s %d %s %s"
                        % (mark, lo + offset + 1, stamp, str(note.get("text") or ""))
                    )
            lines.append("context %d+%d-%d of %d note(s)" % (center, 0, around, len(notes)))
            sys.stdout.write("\n".join(lines) + "\n")
        return 0

    def _filtered(items: list) -> list | None:
        since = getattr(args, "since", None)
        if since is not None:
            since_ts = _ts_arg(since)
            if since_ts is None:
                sys.stderr.write("bad --since: %s\n" % since)
                return None
            items = [
                n
                for n in items
                if isinstance(n, dict) and isinstance(n.get("ts"), (int, float)) and n["ts"] >= since_ts
            ]
        before = getattr(args, "before", None)
        if before is not None:
            before_ts = _ts_arg(before)
            if before_ts is None:
                sys.stderr.write("bad --before: %s\n" % before)
                return None
            items = [
                n
                for n in items
                if isinstance(n, dict) and isinstance(n.get("ts"), (int, float)) and n["ts"] <= before_ts
            ]
        want_harness = getattr(args, "harness", "") or ""
        if want_harness:
            items = [n for n in items if isinstance(n, dict) and n.get("harness") == want_harness]
        needle = (getattr(args, "grep", "") or os.environ.get("JEV_TRACE_GREP", "")).strip().lower()
        if needle:
            items = [n for n in items if isinstance(n, dict) and needle in str(n.get("text") or "").lower()]
        if getattr(args, "uniq", False):
            seen_notes = set()
            deduped = []
            for n in items:
                if not isinstance(n, dict):
                    continue
                key = str(n.get("sha") or n.get("text") or "")
                if key in seen_notes:
                    continue
                seen_notes.add(key)
                deduped.append(n)
            items = deduped
        first = getattr(args, "first", None)
        if isinstance(first, int) and first >= 0:
            items = items[:first]
        limit = getattr(args, "limit", None)
        if isinstance(limit, int) and limit >= 0:
            items = items[-limit:] if limit else []
        if getattr(args, "reverse", False):
            items = items[::-1]
        return items

    notes = _filtered(notes)
    if notes is None:
        return 2

    if getattr(args, "count", False):
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"count": len(notes)}, ensure_ascii=False) + "\n"
            )
        else:
            sys.stdout.write("%d\n" % len(notes))
        return 0

    if getattr(args, "by_harness", False):
        counts: dict[str, int] = {}
        for note in notes:
            harness = str(note.get("harness") or "")
            counts[harness] = counts.get(harness, 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"by_harness": dict(rows)}, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            for harness, n in rows:
                sys.stdout.write("%s %d\n" % (harness or "-", n))
        return 0

    if getattr(args, "shas", False):
        shas = [str(n.get("sha") or "") for n in notes]
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps({"shas": shas}, ensure_ascii=False) + "\n")
        else:
            for sha in shas:
                sys.stdout.write("%s\n" % (sha or "-"))
        return 0

    if getattr(args, "rate", False):
        stamps = [
            float(n["ts"])
            for n in notes
            if isinstance(n.get("ts"), (int, float)) and not isinstance(n.get("ts"), bool)
        ]
        per_day: dict[str, int] = {}
        for stamp in stamps:
            day = datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
            per_day[day] = per_day.get(day, 0) + 1
        span_s = (max(stamps) - min(stamps)) if len(stamps) >= 2 else 0.0
        days = max(1, int(span_s // 86400) + 1) if stamps else 0
        data_out = {
            "count": len(notes),
            "stamped": len(stamps),
            "first_ts": min(stamps) if stamps else None,
            "last_ts": max(stamps) if stamps else None,
            "span_s": round(span_s, 3),
            "days": days,
            "notes_per_day": round(len(stamps) / days, 3) if days else 0.0,
            "per_day": dict(sorted(per_day.items())),
        }
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"rate": data_out}, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            sys.stdout.write(
                "count %d\nstamped %d\nspan_s %s\ndays %d\nnotes_per_day %s\n"
                % (
                    data_out["count"],
                    data_out["stamped"],
                    data_out["span_s"],
                    data_out["days"],
                    data_out["notes_per_day"],
                )
            )
            for day, n in sorted(per_day.items()):
                sys.stdout.write("%s %d\n" % (day, n))
        return 0

    if getattr(args, "gap", 0.0) and args.gap > 0:
        chronological = sorted(
            notes,
            key=lambda n: n.get("ts")
            if isinstance(n.get("ts"), (int, float)) and not isinstance(n.get("ts"), bool)
            else 0,
        )
        gaps = []
        prev = None
        for i, entry in enumerate(chronological):
            ts = entry.get("ts")
            if not (isinstance(ts, (int, float)) and not isinstance(ts, bool)):
                continue
            if prev is not None and float(ts) - float(prev["ts"]) > args.gap:
                gaps.append(
                    {
                        "index": i,
                        "prev_ts": prev["ts"],
                        "ts": ts,
                        "gap_s": round(float(ts) - float(prev["ts"]), 3),
                        "prev_text": prev.get("text"),
                        "text": entry.get("text"),
                    }
                )
            prev = {"ts": ts, "text": entry.get("text")}
        if getattr(args, "json", False):
            sys.stdout.write(
                json.dumps({"gaps": gaps}, ensure_ascii=False, indent=2) + "\n"
            )
        else:
            for g in gaps:
                sys.stdout.write(
                    "%d %.3f %s -> %s\n"
                    % (g["index"], g["gap_s"], g["prev_text"], g["text"])
                )
        return 0

    if getattr(args, "watch", 0.0) and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_TRACE_WATCH_MAX", getattr(args, "max_ticks", 0))
        ticks = 0
        dead = _watch.deadline("JEV_TRACE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        tick: dict = {}
        verdict_ok = True

        def _write_verdict() -> bool:
            notes_count = tick.get("notes")
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "notes" if notes_count else "empty",
                    "ticks": ticks,
                    "notes": notes_count,
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            fresh = load(path).get("notes")
            fresh = fresh if isinstance(fresh, list) else []
            filtered = _filtered(fresh)
            tick = {
                "ts": int(_time.time()),
                "notes": len(filtered) if filtered is not None else None,
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, getattr(args, "jq", ""), getattr(args, "out", "") or None, quiet=_watch.quiet("JEV_TRACE_WATCH_QUIET", getattr(args, "quiet", False)), bad=bool(tick["notes"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d notes=%s\n" % (ticks, tick["notes"])
            )
            if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "fail_fast", False) and not tick["notes"]:
                break
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(args.watch)
        if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
            return 1
        return 0
    if getattr(args, "verdict", "") and not _watch.write_verdict(
        args.verdict,
        {
            "verdict": "notes" if notes else "empty",
            "ticks": 1,
            "notes": len(notes),
        },
    ):
        return 1
    field = getattr(args, "field", "") or ""
    if field:
        values = [_dig(n, field) for n in notes if isinstance(n, dict)]
        if getattr(args, "json", False):
            out_text = json.dumps({"field": field, "values": values}, ensure_ascii=False, indent=2) + "\n"
        else:
            out_text = "".join(
                (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if v is not None else "null")
                + "\n"
                for v in values
            )
    elif getattr(args, "json", False):
        out_text = json.dumps(notes, ensure_ascii=False, indent=2) + "\n"
    else:
        lines = []
        for note in notes:
            if isinstance(note, dict):
                stamp = str(note.get("iso") or int(note.get("ts") or 0))
                text = str(note.get("text") or "")
                lines.append("%s %s" % (stamp, text))
        lines.append("%d note(s)" % len(notes))
        out_text = "\n".join(lines) + "\n"
    out_path = getattr(args, "out", "") or ""
    if out_path:
        try:
            _atomic_write(Path(out_path), out_text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d note(s) to %s\n" % (len(notes), out_path))
        return 0
    sys.stdout.write(out_text)
    return 0


def _diff_side(path: Path) -> tuple[dict, bool]:
    """Raw trace doc + exists flag; missing/corrupt/non-dict reads as empty()."""
    if str(path) == "-":
        raw = _stdin_raw()
        return (raw if raw is not None else empty()), raw is not None
    if not path.is_file():
        return empty(), False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty(), False
    return (raw if isinstance(raw, dict) else empty()), True


def _canon(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


def _trace_diff(a_path: Path, b_path: Path) -> dict:
    """Key-level diff of two trace docs: scalar changes + list item sets."""
    a, a_exists = _diff_side(a_path)
    b, b_exists = _diff_side(b_path)
    changed: dict[str, Any] = {}
    only_a: list[str] = []
    only_b: list[str] = []
    added: dict[str, list] = {}
    removed: dict[str, list] = {}
    same = 0
    for key in sorted(set(a) | set(b)):
        in_a, in_b = key in a, key in b
        if in_a and not in_b:
            only_a.append(key)
            continue
        if in_b and not in_a:
            only_b.append(key)
            continue
        va, vb = a[key], b[key]
        if va == vb:
            same += 1
        elif isinstance(va, list) and isinstance(vb, list):
            a_set = [_canon(x) for x in va]
            b_set = [_canon(x) for x in vb]
            add_items = [vb[i] for i in range(len(vb)) if b_set[i] not in a_set]
            rem_items = [va[i] for i in range(len(va)) if a_set[i] not in b_set]
            if add_items:
                added[key] = add_items
            if rem_items:
                removed[key] = rem_items
            if not add_items and not rem_items:
                changed[key] = {"a": va, "b": vb}
        else:
            changed[key] = {"a": va, "b": vb}
    return {
        "a": str(a_path),
        "b": str(b_path),
        "a_exists": a_exists,
        "b_exists": b_exists,
        "same": same,
        "changed": changed,
        "only_a": only_a,
        "only_b": only_b,
        "added": added,
        "removed": removed,
        "different": bool(changed or only_a or only_b or added or removed),
    }


def cmd_diff(args: argparse.Namespace) -> int:
    """Diff two trace files key-by-key (scalars changed, lists added/removed)."""
    payload = _trace_diff(Path(args.a), Path(args.b))
    rc = emit_jq(payload, getattr(args, "jq", ""))
    if rc is not None:
        return rc
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    out_path = getattr(args, "out", "") or ""
    if out_path:
        try:
            _atomic_write(Path(out_path), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %s\n" % out_path)
        return 0
    sys.stdout.write(text)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """One-shot summary: attempts, history/inspected counts, last pick, file age."""
    path = Path(args.file) if args.file else default_path()
    if getattr(args, "watch", 0.0) and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_TRACE_WATCH_MAX", getattr(args, "max_ticks", 0))
        ticks = 0
        dead = _watch.deadline("JEV_TRACE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        tick: dict = {}
        verdict_ok = True

        def _write_verdict() -> bool:
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "exists" if tick.get("exists") else "missing",
                    "ticks": ticks,
                    "attempt_count": tick.get("attempt_count", 0),
                    "history": tick.get("history", 0),
                    "inspected": tick.get("inspected", 0),
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            cur = load(path)
            tick = {
                "ts": int(_time.time()),
                "exists": _exists(path),
                "attempt_count": int(cur.get("attempt_count") or 0),
                "history": len(cur.get("history") or []),
                "inspected": len(cur.get("inspected") or []),
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, getattr(args, "jq", ""), getattr(args, "out", "") or None, quiet=_watch.quiet("JEV_TRACE_WATCH_QUIET", getattr(args, "quiet", False)), bad=not tick["exists"])
            ticks += 1
            sys.stderr.write(
                "watch tick=%d exists=%s attempt_count=%d\n"
                % (ticks, tick["exists"], tick["attempt_count"])
            )
            if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "fail_fast", False) and not tick["exists"]:
                break
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(args.watch)
        if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
            return 1
        return 0 if tick["exists"] else 1
    # stats/notes/history watch loops emit ticks through _watch.emit below
    data = load(path)
    history_list = data.get("history") or []
    notes_list = data.get("notes") or []
    out: dict[str, Any] = {
        "exists": _exists(path),
        "attempt_count": int(data.get("attempt_count") or 0),
        "history": len(history_list),
        "inspected": len(data.get("inspected") or []),
        "notes": len(notes_list),
        "notes_bytes": sum(len(json.dumps(n, ensure_ascii=False).encode("utf-8")) for n in notes_list if isinstance(n, dict)),
        "history_bytes": sum(len(json.dumps(h, ensure_ascii=False).encode("utf-8")) for h in history_list if isinstance(h, dict)),
        "last_pick": data.get("last_pick") or "",
        "has_error": bool(data.get("last_error")),
        "has_unknown": bool(data.get("unknown")),
    }
    if str(path) != "-" and path.is_file():
        try:
            out["age_seconds"] = int(time.time() - path.stat().st_mtime)
        except OSError:
            pass
    if getattr(args, "verdict", "") and not _watch.write_verdict(
        args.verdict,
        {
            "verdict": "exists" if out["exists"] else "missing",
            "ticks": 1,
            "attempt_count": out["attempt_count"],
            "history": out["history"],
            "inspected": out["inspected"],
        },
    ):
        return 1
    if getattr(args, "out", ""):
        try:
            _atomic_write(
                Path(args.out),
                json.dumps(out, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
        if not getattr(args, "jq", ""):
            return 0
    rc = emit_jq(out, getattr(args, "jq", ""))
    if rc is not None:
        return rc
    emit(out)
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """Emit the trace as a bare state dict for `jev.py scaffold --state`."""
    path = Path(args.file) if args.file else default_path()
    if getattr(args, "watch", 0.0) and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_TRACE_WATCH_MAX", getattr(args, "max_ticks", 0))
        ticks = 0
        dead = _watch.deadline("JEV_TRACE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        state: dict = {}
        verdict_ok = True
        prev_attempt: int | None = None
        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = _time.time()

        def _write_verdict() -> bool:
            nonempty = any(k != "attempt_count" for k in state)
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "ok" if nonempty else "empty",
                    "ticks": ticks,
                    "attempt_count": int(state.get("attempt_count") or 0),
                    "state": state,
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            data = load(path)
            state = {key: value for key, value in data.items() if _present(value)}
            cur_attempt = int(data.get("attempt_count") or 0)
            _watch.emit_or_jq(
                {
                    "ts": int(_time.time()),
                    "state": state,
                    "attempt_count": cur_attempt,
                    "attempt_count_delta": (
                        cur_attempt - prev_attempt
                        if prev_attempt is not None
                        else None
                    ),
                    "history": len(data.get("history") or []),
                    "inspected": len(data.get("inspected") or []),
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
                getattr(args, "jq", ""),
                getattr(args, "out", "") or None,
                quiet=_watch.quiet("JEV_TRACE_WATCH_QUIET", getattr(args, "quiet", False)),
                bad=bool(state),
            )
            prev_attempt = cur_attempt
            ticks += 1
            sys.stderr.write(
                "watch tick=%d state_keys=%d attempt_count=%d\n"
                % (ticks, len(state), cur_attempt)
            )
            if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            tick_cmp = {
                "state": state,
                "attempt_count": cur_attempt,
                "history": len(data.get("history") or []),
                "inspected": len(data.get("inspected") or []),
            }
            if _watch.same_tick(prev_tick, tick_cmp):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = tick_cmp
            if getattr(args, "fail_fast", False) and not any(
                k != "attempt_count" for k in state
            ):
                break
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(args.watch)
        if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
            return 1
        return 0 if any(k != "attempt_count" for k in state) else 1
    data = load(path)
    state = {key: value for key, value in data.items() if _present(value)}
    if getattr(args, "verdict", "") and not _watch.write_verdict(
        args.verdict,
        {
            "verdict": "ok" if any(k != "attempt_count" for k in state) else "empty",
            "ticks": 1,
            "attempt_count": int(state.get("attempt_count") or 0),
            "state": state,
        },
    ):
        return 1
    jq = getattr(args, "jq", "")
    if jq:
        fields = [f.strip() for f in jq.split(",") if f.strip()]
        values: dict = {}
        missing = ""
        for f in fields:
            value, found = _watch.dig(state, f)
            if not found:
                missing = f
                break
            values[f] = value
        if missing:
            sys.stderr.write(
                "bad --jq key %r (state has: %s)\n"
                % (missing, ", ".join(sorted(state)))
            )
            return 2
        if len(fields) == 1:
            sys.stdout.write(json.dumps(values[fields[0]], ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(json.dumps(values, ensure_ascii=False) + "\n")
        return 0
    if args.out:
        try:
            _atomic_write(
                Path(args.out),
                json.dumps(state, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
    else:
        emit(state)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Hold plan/step in a file because Jev and a new session forget."
    )
    parser.add_argument("--file", help="Trace JSON path (default JEV_TRACE or .jev-trace.json; '-' reads the trace JSON from stdin — read-only, no --watch)")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a trace from the human plan")
    init.add_argument("--plan", default=None, help="Plan text (default JEV_TRACE_PLAN env)")
    init.add_argument("--step", default="")
    init.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the emitted payload (rc 2 on unknown key)")
    init.set_defaults(func=cmd_init)
    show = sub.add_parser("show", help="Print the trace (empty object if missing)")
    show.add_argument("--pretty", action="store_true", help="Key fields as text lines.")
    show.add_argument("--key", default="", help="Print only this field's value")
    show.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the trace (rc 2 on unknown key; takes precedence over --key)")
    show.add_argument("--out", default="", help="Write the show JSON to PATH instead of stdout (ignored with --key/--pretty)")
    show.set_defaults(func=cmd_show)
    setter = sub.add_parser("set", help="Update fields")
    setter.add_argument("--plan")
    setter.add_argument("--step")
    setter.add_argument("--unknown")
    setter.add_argument("--error")
    setter.add_argument("--attempt", type=int)
    setter.add_argument(
        "--kv",
        action="append",
        metavar="KEY=VALUE",
        help="Set an arbitrary trace field (repeatable)",
    )
    setter.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the emitted payload (rc 2 on unknown key)")
    setter.add_argument("--dry-run", action="store_true", help="Emit the would-be trace without writing the file")
    setter.set_defaults(func=cmd_set)
    bump_cmd = sub.add_parser("bump", help="Increment attempt_count")
    bump_cmd.add_argument("--error", default="")
    bump_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the emitted payload (rc 2 on unknown key)")
    bump_cmd.set_defaults(func=cmd_bump)
    rec = sub.add_parser("record", help="Store a Jev pick")
    rec.add_argument("--pick", required=True)
    rec.add_argument("--kind", default="")
    rec.add_argument("--harness", default="", help="Tag the --note entry with this harness (default JEV_TRACE_HARNESS)")
    rec.add_argument("--step", default="")
    rec.add_argument("--note", default=None, help="Append a freeform note to trace.notes ('-' reads stdin; default JEV_TRACE_NOTE)")
    rec.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the emitted payload (rc 2 on unknown key)")
    rec.set_defaults(func=cmd_record)
    prune_cmd = sub.add_parser(
        "prune", help="Delete the trace file when older than --older-than seconds"
    )
    prune_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Report whether the file would be deleted without deleting it",
    )
    prune_cmd.add_argument(
        "--older-than",
        type=float,
        required=True,
        help="Age in seconds before the trace may be removed",
    )
    prune_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the emitted payload (rc 2 on unknown key)")
    prune_cmd.set_defaults(func=cmd_prune)
    state_cmd = sub.add_parser(
        "state", help="Emit trace as a bare state dict (scaffold --state input)"
    )
    state_cmd.add_argument("--out", help="Write JSON here instead of stdout")
    state_cmd.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-read the trace every S seconds and print a {ts,state,attempt_count,history,inspected} JSON tick",
    )
    state_cmd.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides JEV_TRACE_WATCH_MAX)")
    state_cmd.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    state_cmd.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    state_cmd.add_argument("--jq", metavar="KEY", default="", help="With --watch: print just the named tick field(s) per pass, comma list; without --watch: dig the state payload (comma list, rc 2 on unknown)")
    state_cmd.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick whose state is empty")
    state_cmd.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    state_cmd.add_argument("--verdict", metavar="PATH", default="", help="With --watch: write a slim {verdict: ok|empty, ticks, attempt_count, state} JSON to PATH, refreshed every tick")
    state_cmd.set_defaults(func=cmd_state)
    stats_cmd = sub.add_parser("stats", help="Summary: counts, last pick, file age")
    stats_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the stats payload (rc 2 on unknown key)")
    stats_cmd.add_argument("--out", default="", help="Write the stats JSON to PATH instead of stdout")
    stats_cmd.add_argument("--watch", metavar="S", type=float, default=0.0, help="Re-print a {ts,exists,attempt_count,history,inspected} tick every S seconds (JEV_TRACE_WATCH_MAX caps ticks)")
    stats_cmd.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides JEV_TRACE_WATCH_MAX)")
    stats_cmd.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    stats_cmd.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    stats_cmd.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick where the trace file is missing")
    stats_cmd.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    stats_cmd.add_argument("--verdict", metavar="PATH", default="", help="With --watch: write a slim {verdict: exists|missing, ticks, attempt_count, history, inspected} JSON to PATH, refreshed every tick")
    stats_cmd.set_defaults(func=cmd_stats)
    notes_cmd = sub.add_parser("notes", help="List recorded notes (iso + text)")
    notes_cmd.add_argument("--json", action="store_true", help="Emit notes as a JSON array")
    notes_cmd.add_argument("--limit", type=int, help="Show only the last N notes")
    notes_cmd.add_argument("--first", type=int, default=None, help="Show only the earliest N notes (applied before --limit/--reverse)")
    notes_cmd.add_argument("--prune", type=int, help="Rewrite the trace keeping only the last N notes")
    notes_cmd.add_argument("--edit", nargs=2, metavar=("I", "TEXT"), help="Rewrite note I (1-based, into the unfiltered list) — or every note in range I-J — with TEXT; keeps ts/iso/harness, recomputes sha; rc 2 out of range")
    notes_cmd.add_argument("--context", metavar="I", default=None, help="Print the notes surrounding index I (1-based, into the unfiltered list; other filters ignored)")
    notes_cmd.add_argument("--around", metavar="K", type=int, default=2, help="With --context: show K notes on each side (default 2)")
    notes_cmd.add_argument("--since", default=None, help="Only notes with ts >= epoch seconds or ISO8601")
    notes_cmd.add_argument("--before", default=None, help="Only notes with ts <= epoch seconds or ISO8601")
    notes_cmd.add_argument("--harness", default="", help="Only notes tagged with this harness")
    notes_cmd.add_argument("--out", default="", help="Write the notes output to PATH instead of stdout")
    notes_cmd.add_argument("--field", default="", help="Print only this field per note (a.b digs into nested objects)")
    notes_cmd.add_argument("--reverse", action="store_true", help="List notes newest-first")
    notes_cmd.add_argument("--grep", default="", help="Only notes whose text contains SUBSTR (case-insensitive; default JEV_TRACE_GREP)")
    notes_cmd.add_argument("--uniq", action="store_true", help="Dedupe notes by sha/text (first occurrence wins)")
    notes_cmd.add_argument("--by-harness", action="store_true", help="Print distinct note harnesses with counts, sorted desc (empty harness shown as '-')")
    notes_cmd.add_argument("--shas", action="store_true", help="Print just the sha of each filtered note, one per line (--json emits {shas})")
    notes_cmd.add_argument("--count", action="store_true", help="Print just the filtered note count (--json emits {count})")
    notes_cmd.add_argument("--rate", action="store_true", help="Print note-rate stats over the filtered notes: per-day UTC buckets plus notes_per_day")
    notes_cmd.add_argument("--gap", metavar="S", type=float, default=0.0, help="List consecutive-note gaps wider than S seconds ({index,gap_s,prev_text,text} rows; --json emits {gaps})")
    notes_cmd.add_argument("--watch", metavar="S", type=float, default=0.0, help="Re-print a {ts,notes} count tick every S seconds (JEV_TRACE_WATCH_MAX caps ticks)")
    notes_cmd.add_argument("--jq", metavar="KEY", default="", help="With --watch: print just the named tick field(s) per pass, comma list")
    notes_cmd.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides JEV_TRACE_WATCH_MAX)")
    notes_cmd.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    notes_cmd.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    notes_cmd.add_argument("--verdict", metavar="PATH", default="", help="With --watch: write a slim {verdict: notes|empty, ticks, notes} JSON to PATH, refreshed every tick")
    notes_cmd.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick with zero notes")
    notes_cmd.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    notes_cmd.set_defaults(func=cmd_notes)
    hist_cmd = sub.add_parser("history", help="List recorded picks (--json for the array)")
    hist_cmd.add_argument("--json", action="store_true")
    hist_cmd.add_argument("--limit", type=int, help="Show only the last N picks")
    hist_cmd.add_argument("--first", type=int, default=None, help="Show only the earliest N picks (applied before --limit/--reverse)")
    hist_cmd.add_argument("--reverse", action="store_true", help="List picks newest-first")
    hist_cmd.add_argument("--field", default="", help="Print only this field per pick (a.b digs into nested objects)")
    hist_cmd.add_argument("--kinds", action="store_true", help="Print distinct history kinds with counts, sorted desc (empty kind shown as '-')")
    hist_cmd.add_argument("--kind", default="", help="Only picks with exactly this kind (comma list for several)")
    hist_cmd.add_argument("--uniq", action="store_true", help="Dedupe picks by pick+kind (first occurrence wins; applied before --limit)")
    hist_cmd.add_argument("--count", action="store_true", help="Print just the filtered pick count (--json emits {count})")
    hist_cmd.add_argument("--rate", action="store_true", help="Print pick-rate stats over the filtered history: per-day UTC buckets plus picks_per_day")
    hist_cmd.add_argument("--since", default=None, help="Only picks with ts >= epoch seconds or ISO8601")
    hist_cmd.add_argument("--grep", default="", help="Only picks whose pick/kind contains SUBSTR (case-insensitive; default JEV_TRACE_HISTORY_GREP)")
    hist_cmd.add_argument("--before", default=None, help="Only picks with ts <= epoch seconds or ISO8601")
    hist_cmd.add_argument("--gap", metavar="S", type=float, default=0.0, help="List consecutive-pick gaps wider than S seconds ({index,gap_s,prev_pick,pick} rows; --json emits {gaps})")
    hist_cmd.add_argument("--watch", metavar="S", type=float, default=0.0, help="Re-print a {ts,picks} count tick every S seconds (JEV_TRACE_WATCH_MAX caps ticks)")
    hist_cmd.add_argument("--jq", metavar="KEY", default="", help="With --watch: print just the named tick field(s) per pass, comma list")
    hist_cmd.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides JEV_TRACE_WATCH_MAX)")
    hist_cmd.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    hist_cmd.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    hist_cmd.add_argument("--out", default="", help="With --watch: append each tick line to PATH (fail-open)")
    hist_cmd.add_argument("--verdict", metavar="PATH", default="", help="With --watch: write a slim {verdict: picks|empty, ticks, picks} JSON to PATH, refreshed every tick")
    hist_cmd.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick with zero picks")
    hist_cmd.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    hist_cmd.set_defaults(func=cmd_history)
    sug = sub.add_parser(
        "suggest",
        help="Ask Jev for the next move (policy template + trace state) and record the pick",
    )
    sug.add_argument("--template", default="next_move", help="policy.json templates key (default next_move)")
    sug.add_argument("--task", default="", help="Task text folded into the ask state; '-' reads it from stdin")
    sug.add_argument("--ask-file", default="", help="Write the ask request JSON to PATH (default <trace>.jev-suggest.ask.json)")
    sug.add_argument("--out", default="", help="Also write the ask request JSON to PATH")
    sug.add_argument("--dry-run", action="store_true", help="Print the ask request without calling Jev or recording")
    sug.add_argument("--pick", default="", help="Skip Jev; record this choice directly")
    sug.add_argument("--kind", default="suggest", help="Kind tag for the history entry (default suggest)")
    sug.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the result payload (rc 2 on unknown key)")
    diff_cmd = sub.add_parser(
        "diff",
        help="Diff two trace files key-by-key: scalars under changed, list items under added/removed, plus only_a/only_b keys and exists flags",
    )
    diff_cmd.add_argument("a", help="First trace JSON path")
    diff_cmd.add_argument("b", help="Second trace JSON path")
    diff_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the diff payload (rc 2 on unknown key)")
    diff_cmd.add_argument("--out", default="", help="Write the diff JSON to PATH instead of stdout")
    diff_cmd.set_defaults(func=cmd_diff)
    sug.set_defaults(func=cmd_suggest)
    export_cmd = sub.add_parser(
        "export", help="Dump the whole trace bundle as JSON"
    )
    export_cmd.add_argument(
        "--out", default="", help="Write the export JSON to PATH instead of stdout"
    )
    export_cmd.add_argument(
        "--csv",
        action="store_true",
        help="Emit the (filtered) history list as CSV rows — pick,ts,iso,kind — instead of the JSON bundle",
    )
    export_cmd.add_argument("--since", default=None, help="Only history/notes with ts >= epoch seconds or ISO8601")
    export_cmd.add_argument("--before", default=None, help="Only history/notes with ts <= epoch seconds or ISO8601")
    export_cmd.add_argument("--kinds", default="", help="Comma list of pick kinds to keep in exported history")
    export_cmd.add_argument("--md", action="store_true", help="Emit a markdown document (history + notes tables) instead of the JSON bundle")
    export_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the export payload (rc 2 on unknown key)")
    export_cmd.set_defaults(func=cmd_export)
    verify_cmd = sub.add_parser(
        "verify", help="Re-check every note's stored sha against sha256(text) — rc 1 on mismatch/missing/unreadable"
    )
    verify_cmd.add_argument("--json", action="store_true", help="Emit the verify report as JSON")
    verify_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the verify payload (rc 2 on unknown key)")
    verify_cmd.add_argument("--out", metavar="PATH", default="", help="Also write the verify payload JSON to PATH")
    verify_cmd.add_argument("--verdict", metavar="PATH", default="", help="Write a slim {verdict: ok|fail, notes, checked, bad} JSON to PATH")
    verify_cmd.add_argument("--fix", action="store_true", help="Rewrite mismatched note shas to the computed value in place (payload gains fixed)")
    verify_cmd.set_defaults(func=cmd_verify)
    schema_cmd = sub.add_parser(
        "schema", help="Print the .jev-trace.json key contract and exit"
    )
    schema_cmd.add_argument("--json", action="store_true", help="Emit the contract as JSON")
    schema_cmd.set_defaults(func=cmd_schema)
    env_cmd = sub.add_parser(
        "env",
        help="Print the resolved env config JSON",
        description="Print the resolved env config JSON (file, exists, fill_timeout_seconds, plan_set, policy, watch_max, watch_secs, watch_quiet) and exit",
    )
    env_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just one dotted-path field of the env report (rc 2 on unknown key)")
    env_cmd.add_argument("--out", metavar="PATH", default="", help="Also write the env report JSON to PATH (fail-open)")
    env_cmd.set_defaults(func=cmd_env)
    selftest = sub.add_parser(
        "self-test",
        help="Record+read a pick on a temp trace; exit 1 when it does not round-trip",
    )
    selftest.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the emitted payload (rc 2 on unknown key)")
    selftest.set_defaults(func=cmd_self_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    if _watch.maybe_version(sys.argv[1:] if argv is None else argv):
        return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "file", "") == "-" and getattr(args, "watch", 0):
        sys.stderr.write("--file - (stdin) supports no --watch (cannot re-read)")
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
