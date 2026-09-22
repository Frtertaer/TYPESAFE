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
from pathlib import Path


def cap(env_name: str) -> int:
    try:
        return max(int(os.environ.get(env_name, "0") or 0), 0)
    except ValueError:
        return 0


def emit(tick: dict, out_path=None) -> None:
    line = json.dumps(tick) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()
    if out_path:
        try:
            with Path(out_path).open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
