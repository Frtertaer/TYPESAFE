#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""_watch.py - shared plumbing for the --watch loops across the pack.

cap() parses the per-script JEV_*_WATCH_MAX tick cap (0/invalid = uncapped);
emit() prints one tick JSON line to stdout and appends it to the optional
--out file, fail-open so a bad path never kills the loop.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def cap(env_name: str, override=None) -> int:
    """Tick cap: --max-ticks N wins, else the JEV_*_WATCH_MAX env (0 = uncapped).

    A non-empty env value that is not a parseable integer warns on stderr and
    falls back to uncapped — a typo must never silently disable the cap."""
    if override:
        try:
            return max(int(override), 0)
        except (TypeError, ValueError):
            return 0
    raw = os.environ.get(env_name, "")
    if raw:
        try:
            return max(int(raw), 0)
        except ValueError:
            sys.stderr.write("bad %s %r (want int ticks); uncapped\n" % (env_name, raw))
    return 0


def deadline(env_name: str, override=None) -> float:
    """Epoch deadline: --watch-max S wins, else the JEV_*_WATCH_SECS env
    (0/invalid/missing = no deadline)."""
    src = override if override else os.environ.get(env_name, "0")
    try:
        s = float(src or 0)
    except (TypeError, ValueError):
        s = 0.0
    return time.time() + s if s > 0 else 0.0


def quiet(env_name: str, flag: bool = False) -> bool:
    """Effective --quiet: the flag wins, else the JEV_*_WATCH_QUIET env
    (1/true/yes/on count as set). Lets CI preset quiet ticks globally."""
    if flag:
        return True
    return os.environ.get(env_name, "").strip().lower() in ("1", "true", "yes", "on")


def emit(tick: dict, out_path=None, quiet: bool = False, bad=None) -> None:
    """Print one JSONL tick (and append to out_path, fail-open).

    A ``ts`` epoch field is injected when the caller did not set one.
    quiet suppresses stdout for clean ticks: with quiet=True a tick reaches
    stdout only when `bad` is truthy; --out always gets every tick."""
    if "ts" not in tick:
        tick = dict(tick, ts=int(time.time()))
    line = json.dumps(tick) + "\n"
    if not (quiet and not bad):
        sys.stdout.write(line)
        sys.stdout.flush()
    if out_path:
        try:
            with Path(out_path).open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass


def emit_or_jq(tick: dict, jq: str, out_path=None, quiet: bool = False, bad=None) -> None:
    """Emit a tick, or print just the named field(s) when jq is set.

    jq is a comma list of dotted paths; each prints one JSON line (null on a
    miss). In jq mode the tick JSON is not emitted and out_path is not written,
    matching decisions.py's --jq watch behavior."""
    if jq:
        for field in [f.strip() for f in jq.split(",") if f.strip()]:
            cur = tick
            for part in field.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            sys.stdout.write(json.dumps(cur) + "\n")
        sys.stdout.flush()
        return
    emit(tick, out_path, quiet=quiet, bad=bad)


def policy_version() -> str:
    """policy.json "version" as a string; '?' when unreadable or missing."""
    try:
        data = json.loads(
            (Path(__file__).resolve().parent.parent / "policy.json").read_text(
                encoding="utf-8"
            )
        )
        return str(data.get("version", "?"))
    except (OSError, ValueError):
        return "?"


def maybe_version(argv: list, out=None) -> bool:
    """--version short-circuit for every script: print the pack version and
    return True when the flag is anywhere in argv (works for argparse and
    hand-rolled argv parsers alike)."""
    if "--version" not in argv:
        return False
    (out or sys.stdout).write("jev-consult (policy v%s)\n" % policy_version())
    return True


def write_verdict(path: str, payload: dict) -> bool:
    """Write a slim verdict JSON to path; False (with stderr note) on failure.

    A ``ts`` epoch field is injected when the caller did not set one. Writes a
    sibling ``<name>.tmp`` file first and ``os.replace``s it over the target
    so readers never see a half-written payload."""
    if "ts" not in payload:
        payload = dict(payload, ts=int(time.time()))
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        sys.stderr.write("--verdict failed: %s\n" % exc)
        return False
    return True
