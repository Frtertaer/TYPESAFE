#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Score canned unguarded vs guarded traces. Live uses Jev Noul; default is offline."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def load_jev():
    path = HERE / "jev.py"
    spec = importlib.util.spec_from_file_location("jev_consult_jev", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def cases_path() -> Path:
    return HERE.parent / "examples" / "compare-cases.json"


def load_cases(path: Path | None = None) -> dict[str, Any]:
    data = json.loads((path or cases_path()).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise SystemExit("compare-cases.json must have a cases list")
    return data


def side_state(side: dict[str, Any]) -> dict[str, Any]:
    skip = {"called_jev", "invented", "last_pick"}
    return {key: value for key, value in side.items() if key not in skip}


def build_request(policy: dict[str, Any], case: dict[str, Any], side: dict[str, Any]) -> dict[str, Any]:
    qid = str(case.get("score") or "on_track")
    templates = policy.get("templates") or {}
    question = templates.get(qid)
    if not isinstance(question, dict):
        raise SystemExit("policy is missing template %s" % qid)
    return {"state": side_state(side), "questions": {qid: dict(question)}}


def noul_value(answers: dict[str, Any], qid: str) -> float | None:
    item = answers.get(qid) or {}
    if not isinstance(item, dict):
        return None
    raw = item.get("noul")
    if raw is None:
        raw = item.get("value")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def score_live(jev, policy: dict[str, Any], case: dict[str, Any], side: dict[str, Any]) -> dict[str, Any]:
    request = build_request(policy, case, side)
    parsed = jev.post_systemone(request["state"], request["questions"], policy)
    answers = parsed.get("answers") or {}
    qid = str(case.get("score") or "on_track")
    decided = jev.decide(answers if isinstance(answers, dict) else {}, policy)
    return {
        "noul": noul_value(answers if isinstance(answers, dict) else {}, qid),
        "model": parsed.get("model"),
        "action": decided.get("action"),
    }


def row_offline(case: dict[str, Any]) -> dict[str, Any]:
    before = case.get("before") or {}
    after = case.get("after") or {}
    return {
        "id": case.get("id"),
        "defect": case.get("defect"),
        "score": case.get("score"),
        "prompt": case.get("prompt"),
        "before": {
            "called_jev": bool(before.get("called_jev")),
            "invented": before.get("invented"),
            "step": before.get("current_step"),
        },
        "after": {
            "called_jev": bool(after.get("called_jev")),
            "last_pick": after.get("last_pick"),
            "step": after.get("current_step"),
        },
    }


def format_table(rows: list[dict[str, Any]], live: bool) -> str:
    lines = [
        "goal: cut stall / unknown / forget / drift — not IQ",
        "",
    ]
    header = (
        "case        defect              called_jev  before_noul  after_noul"
        if live
        else "case        defect              before_jev  after_jev  after_pick"
    )
    lines.append(header)
    for row in rows:
        cid = str(row.get("id") or "")[:11]
        defect = str(row.get("defect") or "")[:18]
        before = row.get("before") or {}
        after = row.get("after") or {}
        if live:
            bn = before.get("noul")
            an = after.get("noul")
            lines.append(
                "%-11s %-18s %-10s %-12s %s"
                % (
                    cid,
                    defect,
                    "yes" if after.get("called_jev") else "no",
                    "-" if bn is None else "%.2f" % bn,
                    "-" if an is None else "%.2f" % an,
                )
            )
        else:
            lines.append(
                "%-11s %-18s %-10s %-9s %s"
                % (
                    cid,
                    defect,
                    "no" if not before.get("called_jev") else "yes",
                    "yes" if after.get("called_jev") else "no",
                    after.get("last_pick") or "-",
                )
            )
    lines.append("")
    lines.append("No watchdog: if the coder skips the skill, the after-path does not run.")
    return "\n".join(lines) + "\n"


def format_md(rows: list[dict[str, Any]], live: bool) -> str:
    if live:
        header = ["case", "defect", "called_jev", "before_noul", "after_noul"]
    else:
        header = ["case", "defect", "before_jev", "after_jev", "after_pick"]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    for row in rows:
        before = row.get("before") or {}
        after = row.get("after") or {}
        if live:
            bn = before.get("noul")
            an = after.get("noul")
            cells = [
                str(row.get("id") or ""),
                str(row.get("defect") or ""),
                "yes" if after.get("called_jev") else "no",
                "-" if bn is None else "%.2f" % bn,
                "-" if an is None else "%.2f" % an,
            ]
        else:
            cells = [
                str(row.get("id") or ""),
                str(row.get("defect") or ""),
                "yes" if before.get("called_jev") else "no",
                "yes" if after.get("called_jev") else "no",
                str(after.get("last_pick") or "-"),
            ]
        cells = [c.replace("|", "\\|").replace("\n", " ") for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def strict_failures(rows: list[dict[str, Any]], live: bool) -> list[str]:
    """CI gate: the guarded (after) side must have called Jev and, when live,
    scored at least noul_yes on the case's question."""
    failures: list[str] = []
    noul_yes = 0.7
    try:
        jev = load_jev()
        policy = jev.load_policy()
        noul_yes = float(policy.get("noul_yes", 0.7))
    except Exception:
        pass
    for row in rows:
        cid = str(row.get("id") or "?")
        after = row.get("after") or {}
        if not after.get("called_jev"):
            failures.append("%s: guarded side did not call Jev" % cid)
            continue
        if live:
            an = after.get("noul")
            if an is not None and an < noul_yes:
                failures.append("%s: after noul %.2f < %.2f" % (cid, an, noul_yes))
    return failures


def run(
    live: bool,
    as_json: bool,
    path: Path | None = None,
    only: set[str] | None = None,
) -> dict[str, Any]:
    blob = load_cases(path)
    cases = list(blob["cases"])
    if only:
        cases = [case for case in cases if str(case.get("id") or "") in only]
    rows = [row_offline(case) for case in cases]
    live_error = ""
    if live:
        jev = load_jev()
        policy = jev.load_policy()
        for index, case in enumerate(cases):
            before = score_live(jev, policy, case, case.get("before") or {})
            after = score_live(jev, policy, case, case.get("after") or {})
            rows[index]["before"]["noul"] = before.get("noul")
            rows[index]["after"]["noul"] = after.get("noul")
            rows[index]["model"] = after.get("model") or before.get("model")
    return {
        "goal": blob.get("goal"),
        "live": live,
        "error": live_error,
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare unguarded vs Jev-guarded traces on sticky prompts."
    )
    parser.add_argument("--live", action="store_true", help="Call Jev Noul for each side")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--md", action="store_true", help="Print rows as a Markdown table")
    parser.add_argument("--out", metavar="PATH", default="", help="Also write the result JSON to PATH")
    parser.add_argument("--cases", default=os.environ.get("JEV_COMPARE_CASES", "") or None, help="Path to compare-cases.json")
    parser.add_argument(
        "--only",
        default=os.environ.get("JEV_COMPARE_ONLY", ""),
        help="Comma-separated case ids to run (default: all; JEV_COMPARE_ONLY presets).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when any case's guarded side skipped Jev (or scored below noul_yes with --live).",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-run the cases every S seconds, printing a {ts,cases,failures} JSON tick (JEV_COMPARE_WATCH_MAX caps ticks).",
    )
    args = parser.parse_args(argv)
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    if args.watch and args.watch > 0:
        import time as _time

        try:
            max_ticks = int(os.environ.get("JEV_COMPARE_WATCH_MAX", "") or 0)
        except ValueError:
            max_ticks = 0
        ticks = 0
        while max_ticks <= 0 or ticks < max_ticks:
            cur = run(
                live=args.live,
                as_json=args.as_json,
                path=Path(args.cases) if args.cases else None,
                only=only,
            )
            tick = {
                "ts": int(_time.time()),
                "cases": len(cur["rows"]),
                "failures": len(strict_failures(cur["rows"], args.live)),
            }
            sys.stdout.write(json.dumps(tick) + "\n")
            sys.stdout.flush()
            ticks += 1
            _time.sleep(args.watch)
        return 0
    result = run(
        live=args.live,
        as_json=args.as_json,
        path=Path(args.cases) if args.cases else None,
        only=only,
    )
    if args.out:
        out_path = Path(args.out)
        try:
            out_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %s\n" % out_path)
    if args.as_json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif args.md:
        sys.stdout.write(format_md(result["rows"], live=args.live))
    else:
        sys.stdout.write(format_table(result["rows"], live=args.live))
    if args.strict:
        failures = strict_failures(result["rows"], args.live)
        for failure in failures:
            sys.stderr.write("strict: %s\n" % failure)
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
