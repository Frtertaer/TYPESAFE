#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Durable plan/step file. Jev has no memory; this file is stuffed into each ask."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

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


def save(data: dict[str, Any], path: Path | None = None) -> Path:
    path = path or default_path()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


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
    entry: dict[str, Any] = {"pick": pick}
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
        entry = {"ts": now, "iso": iso, "text": note_text}
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
    limit = getattr(args, "limit", None)
    if isinstance(limit, int) and limit >= 0:
        history = history[-limit:] if limit else []
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
    try:
        path.unlink()
    except OSError as exc:
        emit({"path": str(path), "removed": False, "reason": "unlink failed: %s" % exc})
        return 1
    emit({"path": str(path), "removed": True, "age_seconds": round(age, 3)})
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
    since = getattr(args, "since", None)
    if since is not None:
        since_ts = _ts_arg(since)
        if since_ts is None:
            sys.stderr.write("bad --since: %s\n" % since)
            return 2
        notes = [
            n
            for n in notes
            if isinstance(n, dict) and isinstance(n.get("ts"), (int, float)) and n["ts"] >= since_ts
        ]
    want_harness = getattr(args, "harness", "") or ""
    if want_harness:
        notes = [n for n in notes if isinstance(n, dict) and n.get("harness") == want_harness]
    limit = getattr(args, "limit", None)
    if isinstance(limit, int) and limit >= 0:
        notes = notes[-limit:] if limit else []
    if getattr(args, "json", False):
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
            Path(out_path).write_text(out_text, encoding="utf-8")
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
    emit(out)
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    """Emit the trace as a bare state dict for `jev.py scaffold --state`."""
    path = Path(args.file) if args.file else default_path()
    data = load(path)
    state = {key: value for key, value in data.items() if _present(value)}
    if args.out:
        Path(args.out).write_text(
            json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
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
    state_cmd.set_defaults(func=cmd_state)
    stats_cmd = sub.add_parser("stats", help="Summary: counts, last pick, file age")
    stats_cmd.set_defaults(func=cmd_stats)
    notes_cmd = sub.add_parser("notes", help="List recorded notes (iso + text)")
    notes_cmd.add_argument("--json", action="store_true", help="Emit notes as a JSON array")
    notes_cmd.add_argument("--limit", type=int, help="Show only the last N notes")
    notes_cmd.add_argument("--prune", type=int, help="Rewrite the trace keeping only the last N notes")
    notes_cmd.add_argument("--since", default=None, help="Only notes with ts >= epoch seconds or ISO8601")
    notes_cmd.add_argument("--harness", default="", help="Only notes tagged with this harness")
    notes_cmd.add_argument("--out", default="", help="Write the notes output to PATH instead of stdout")
    notes_cmd.set_defaults(func=cmd_notes)
    hist_cmd = sub.add_parser("history", help="List recorded picks (--json for the array)")
    hist_cmd.add_argument("--json", action="store_true")
    hist_cmd.add_argument("--limit", type=int, help="Show only the last N picks")
    hist_cmd.set_defaults(func=cmd_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
