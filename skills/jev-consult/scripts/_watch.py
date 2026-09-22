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
    """Tick cap: --max-ticks N wins, else the JEV_*_WATCH_MAX env (0 = uncapped)."""
    if override:
        try:
            return max(int(override), 0)
        except (TypeError, ValueError):
            return 0
    try:
        return max(int(os.environ.get(env_name, "0") or 0), 0)
    except ValueError:
        return 0


def deadline(seconds) -> float:
    """Epoch deadline for --watch-max S (0/invalid/missing = no deadline)."""
    try:
        s = float(seconds or 0)
    except (TypeError, ValueError):
        s = 0.0
    return time.time() + s if s > 0 else 0.0


def emit(tick: dict, out_path=None, quiet: bool = False, bad=None) -> None:
    """Print one JSONL tick (and append to out_path, fail-open).

    quiet suppresses stdout for clean ticks: with quiet=True a tick reaches
    stdout only when `bad` is truthy; --out always gets every tick."""
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
