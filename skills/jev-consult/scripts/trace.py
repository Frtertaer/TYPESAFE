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
}


def default_path() -> Path:
    env = os.environ.get("JEV_TRACE", "").strip()
    return Path(env) if env else Path(".jev-trace.json")


def empty() -> dict[str, Any]:
    data = dict(EMPTY)
    data["inspected"] = []
    data["history"] = []
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
    data = empty()
    data["plan"] = args.plan
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
    age_seconds = None
    if exists:
        try:
            age_seconds = max(0.0, round(time.time() - path.stat().st_mtime, 3))
        except OSError:
            age_seconds = None
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
    save(data, path)
    emit({"path": str(path), "trace": data})
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
    init.add_argument("--plan", required=True)
    init.add_argument("--step", default="")
    init.set_defaults(func=cmd_init)
    show = sub.add_parser("show", help="Print the trace (empty object if missing)")
    show.set_defaults(func=cmd_show)
    setter = sub.add_parser("set", help="Update fields")
    setter.add_argument("--plan")
    setter.add_argument("--step")
    setter.add_argument("--unknown")
    setter.add_argument("--error")
    setter.add_argument("--attempt", type=int)
    setter.set_defaults(func=cmd_set)
    bump_cmd = sub.add_parser("bump", help="Increment attempt_count")
    bump_cmd.add_argument("--error", default="")
    bump_cmd.set_defaults(func=cmd_bump)
    rec = sub.add_parser("record", help="Store a Jev pick")
    rec.add_argument("--pick", required=True)
    rec.add_argument("--kind", default="")
    rec.add_argument("--step", default="")
    rec.set_defaults(func=cmd_record)
    state_cmd = sub.add_parser(
        "state", help="Emit trace as a bare state dict (scaffold --state input)"
    )
    state_cmd.add_argument("--out", help="Write JSON here instead of stdout")
    state_cmd.set_defaults(func=cmd_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
