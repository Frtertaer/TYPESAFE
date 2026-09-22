#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Durable plan/step file. Jev has no memory; this file is stuffed into each ask."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402

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


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or default_path()
    if not path.is_file():
        return empty()
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


def emit(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


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
    emit({"path": str(path), "trace": data})
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    exists = path.is_file()
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
    if exists:
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
    save(data, path)
    emit({"path": str(path), "trace": data})
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else default_path()
    data = bump(load(path), error=args.error or "")
    save(data, path)
    emit({"path": str(path), "trace": data})
    return 0


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
    emit({"path": str(path), "trace": data})
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
        limit = getattr(args, "limit", None)
        if isinstance(limit, int) and limit >= 0:
            items = items[-limit:] if limit else []
        if getattr(args, "reverse", False):
            items = items[::-1]
        return items

    history = _filtered(history)
    if history is None:
        return 2

    if getattr(args, "watch", 0.0) and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_TRACE_WATCH_MAX", getattr(args, "max_ticks", 0))
        ticks = 0
        dead = _watch.deadline("JEV_TRACE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        tick: dict = {}
        verdict_ok = True

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
            if getattr(args, "fail_fast", False) and not tick["picks"]:
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
    if not path.is_file():
        emit({"path": str(path), "removed": False, "reason": "missing"})
        return 0
    try:
        age = time.time() - path.stat().st_mtime
    except OSError as exc:
        emit({"path": str(path), "removed": False, "reason": "stat failed: %s" % exc})
        return 0
    if age < float(args.older_than):
        emit({"path": str(path), "removed": False, "reason": "fresh", "age_seconds": round(age, 3)})
        return 0
    if getattr(args, "dry_run", False):
        emit({"path": str(path), "removed": False, "reason": "dry-run", "age_seconds": round(age, 3), "would_remove": True})
        return 0
    try:
        path.unlink()
    except OSError as exc:
        emit({"path": str(path), "removed": False, "reason": "unlink failed: %s" % exc})
        return 1
    emit({"path": str(path), "removed": True, "age_seconds": round(age, 3)})
    return 0


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
    """When --jq is set, print just that dotted field and return an rc; else None."""
    if not jq:
        return None
    value, found = jq_lookup(payload, jq)
    if not found:
        sys.stderr.write(
            "bad --jq key %r (payload has: %s)\n"
            % (jq, ", ".join(sorted(payload)))
        )
        return 2
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Dump the whole trace bundle (state, history, notes, counts) as JSON."""
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    data["file"] = str(path)
    rc = emit_jq(data, getattr(args, "jq", ""))
    if rc is not None:
        return rc
    out_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    out_path = getattr(args, "out", "") or ""
    if out_path:
        try:
            _atomic_write(Path(out_path), out_text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote trace export to %s\n" % out_path)
        return 0
    sys.stdout.write(out_text)
    return 0


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
        limit = getattr(args, "limit", None)
        if isinstance(limit, int) and limit >= 0:
            items = items[-limit:] if limit else []
        if getattr(args, "reverse", False):
            items = items[::-1]
        return items

    notes = _filtered(notes)
    if notes is None:
        return 2

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
            if getattr(args, "fail_fast", False) and not tick["notes"]:
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

        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            cur = load(path)
            tick = {
                "ts": int(_time.time()),
                "exists": path.is_file(),
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
            if getattr(args, "fail_fast", False) and not tick["exists"]:
                break
            _time.sleep(args.watch)
        if getattr(args, "verdict", "") and verdict_ok and not _write_verdict():
            return 1
        return 0 if tick["exists"] else 1
    # stats/notes/history watch loops emit ticks through _watch.emit below
    data = load(path)
    out: dict[str, Any] = {
        "exists": path.is_file(),
        "attempt_count": int(data.get("attempt_count") or 0),
        "history": len(data.get("history") or []),
        "inspected": len(data.get("inspected") or []),
        "last_pick": data.get("last_pick") or "",
        "has_error": bool(data.get("last_error")),
        "has_unknown": bool(data.get("unknown")),
    }
    if path.is_file():
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
    rc = emit_jq(out, getattr(args, "jq", ""))
    if rc is not None:
        return rc
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
        return 0
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
            if getattr(args, "fail_fast", False) and not any(
                k != "attempt_count" for k in state
            ):
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
    parser.add_argument("--file", help="Trace JSON path (default JEV_TRACE or .jev-trace.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create a trace from the human plan")
    init.add_argument("--plan", default=None, help="Plan text (default JEV_TRACE_PLAN env)")
    init.add_argument("--step", default="")
    init.set_defaults(func=cmd_init)
    show = sub.add_parser("show", help="Print the trace (empty object if missing)")
    show.add_argument("--pretty", action="store_true", help="Key fields as text lines.")
    show.add_argument("--key", default="", help="Print only this field's value")
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
    setter.set_defaults(func=cmd_set)
    bump_cmd = sub.add_parser("bump", help="Increment attempt_count")
    bump_cmd.add_argument("--error", default="")
    bump_cmd.set_defaults(func=cmd_bump)
    rec = sub.add_parser("record", help="Store a Jev pick")
    rec.add_argument("--pick", required=True)
    rec.add_argument("--kind", default="")
    rec.add_argument("--harness", default="", help="Tag the --note entry with this harness (default JEV_TRACE_HARNESS)")
    rec.add_argument("--step", default="")
    rec.add_argument("--note", default=None, help="Append a freeform note to trace.notes ('-' reads stdin; default JEV_TRACE_NOTE)")
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
    state_cmd.add_argument("--jq", metavar="KEY", default="", help="With --watch: print just the named tick field(s) per pass, comma list")
    state_cmd.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick whose state is empty")
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
    stats_cmd.add_argument("--verdict", metavar="PATH", default="", help="With --watch: write a slim {verdict: exists|missing, ticks, attempt_count, history, inspected} JSON to PATH, refreshed every tick")
    stats_cmd.set_defaults(func=cmd_stats)
    notes_cmd = sub.add_parser("notes", help="List recorded notes (iso + text)")
    notes_cmd.add_argument("--json", action="store_true", help="Emit notes as a JSON array")
    notes_cmd.add_argument("--limit", type=int, help="Show only the last N notes")
    notes_cmd.add_argument("--prune", type=int, help="Rewrite the trace keeping only the last N notes")
    notes_cmd.add_argument("--since", default=None, help="Only notes with ts >= epoch seconds or ISO8601")
    notes_cmd.add_argument("--before", default=None, help="Only notes with ts <= epoch seconds or ISO8601")
    notes_cmd.add_argument("--harness", default="", help="Only notes tagged with this harness")
    notes_cmd.add_argument("--out", default="", help="Write the notes output to PATH instead of stdout")
    notes_cmd.add_argument("--field", default="", help="Print only this field per note (a.b digs into nested objects)")
    notes_cmd.add_argument("--reverse", action="store_true", help="List notes newest-first")
    notes_cmd.add_argument("--grep", default="", help="Only notes whose text contains SUBSTR (case-insensitive; default JEV_TRACE_GREP)")
    notes_cmd.add_argument("--uniq", action="store_true", help="Dedupe notes by sha/text (first occurrence wins)")
    notes_cmd.add_argument("--watch", metavar="S", type=float, default=0.0, help="Re-print a {ts,notes} count tick every S seconds (JEV_TRACE_WATCH_MAX caps ticks)")
    notes_cmd.add_argument("--jq", metavar="KEY", default="", help="With --watch: print just the named tick field(s) per pass, comma list")
    notes_cmd.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides JEV_TRACE_WATCH_MAX)")
    notes_cmd.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    notes_cmd.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    notes_cmd.add_argument("--verdict", metavar="PATH", default="", help="With --watch: write a slim {verdict: notes|empty, ticks, notes} JSON to PATH, refreshed every tick")
    notes_cmd.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick with zero notes")
    notes_cmd.set_defaults(func=cmd_notes)
    hist_cmd = sub.add_parser("history", help="List recorded picks (--json for the array)")
    hist_cmd.add_argument("--json", action="store_true")
    hist_cmd.add_argument("--limit", type=int, help="Show only the last N picks")
    hist_cmd.add_argument("--reverse", action="store_true", help="List picks newest-first")
    hist_cmd.add_argument("--field", default="", help="Print only this field per pick (a.b digs into nested objects)")
    hist_cmd.add_argument("--since", default=None, help="Only picks with ts >= epoch seconds or ISO8601")
    hist_cmd.add_argument("--grep", default="", help="Only picks whose pick/kind contains SUBSTR (case-insensitive; default JEV_TRACE_HISTORY_GREP)")
    hist_cmd.add_argument("--before", default=None, help="Only picks with ts <= epoch seconds or ISO8601")
    hist_cmd.add_argument("--watch", metavar="S", type=float, default=0.0, help="Re-print a {ts,picks} count tick every S seconds (JEV_TRACE_WATCH_MAX caps ticks)")
    hist_cmd.add_argument("--jq", metavar="KEY", default="", help="With --watch: print just the named tick field(s) per pass, comma list")
    hist_cmd.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides JEV_TRACE_WATCH_MAX)")
    hist_cmd.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    hist_cmd.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    hist_cmd.add_argument("--out", default="", help="With --watch: append each tick line to PATH (fail-open)")
    hist_cmd.add_argument("--verdict", metavar="PATH", default="", help="With --watch: write a slim {verdict: picks|empty, ticks, picks} JSON to PATH, refreshed every tick")
    hist_cmd.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick with zero picks")
    hist_cmd.set_defaults(func=cmd_history)
    export_cmd = sub.add_parser(
        "export", help="Dump the whole trace bundle as JSON"
    )
    export_cmd.add_argument(
        "--out", default="", help="Write the export JSON to PATH instead of stdout"
    )
    export_cmd.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the export payload (rc 2 on unknown key)")
    export_cmd.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
