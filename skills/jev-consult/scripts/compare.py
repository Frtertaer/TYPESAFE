#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Score canned unguarded vs guarded traces. Live uses Jev Noul; default is offline."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _watch  # noqa: E402


def load_jev():
    path = HERE / "jev.py"
    spec = importlib.util.spec_from_file_location("jev_consult_jev", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def cases_path() -> Path:
    return HERE.parent / "examples" / "compare-cases.json"


def load_cases(path: Path | str | None = None) -> dict[str, Any]:
    try:
        if path is not None and str(path) == "-":
            data = json.loads(sys.stdin.read())
        else:
            data = json.loads((Path(path) if path else cases_path()).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit("cannot read cases: %s" % exc)
    except ValueError as exc:
        raise SystemExit("cases file is not JSON: %s" % exc)
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


def _write_verdict(path: str, tick: dict[str, Any]) -> None:
    """Slim verdict JSON {verdict, cases, failures}; failures is a count or a list."""
    failures = tick.get("failures")
    n = len(failures) if isinstance(failures, list) else int(failures or 0)
    payload = {
        "verdict": "PASS" if n == 0 else "FAIL",
        "cases": tick.get("cases"),
        "failures": failures if isinstance(failures, list) else n,
    }
    if "new_failures" in tick:
        payload["new_failures"] = tick["new_failures"]
    if "elapsed_s" in tick:
        payload["elapsed_s"] = tick["elapsed_s"]
    return _watch.write_verdict(path, payload)


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
        try:
            jev = load_jev()
            policy = jev.load_policy()
            for index, case in enumerate(cases):
                before = score_live(jev, policy, case, case.get("before") or {})
                after = score_live(jev, policy, case, case.get("after") or {})
                rows[index]["before"]["noul"] = before.get("noul")
                rows[index]["after"]["noul"] = after.get("noul")
                rows[index]["model"] = after.get("model") or before.get("model")
        except Exception as exc:
            live_error = str(exc) or exc.__class__.__name__
    return {
        "goal": blob.get("goal"),
        "live": live,
        "error": live_error,
        "rows": rows,
    }


def _row_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id") or "?"): row for row in rows if isinstance(row, dict)}


def _failing_ids(rows: list[dict[str, Any]], live: bool) -> set[str]:
    return {
        f.split(":", 1)[0]
        for f in strict_failures(rows, live)
    }


def diff_baseline(
    baseline_rows: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    live: bool,
) -> dict[str, Any]:
    """Per-case diff vs a saved baseline: strict-gate flips, noul drops,
    called_jev flips, added/removed cases."""
    before = _row_map(baseline_rows)
    after = _row_map(rows)
    was_failing = _failing_ids(list(before.values()), live)
    now_failing = _failing_ids(list(after.values()), live)
    regressions: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    changed: list[str] = []
    unchanged = 0
    for cid, row in after.items():
        old = before.get(cid)
        if old is None:
            continue  # added
        if cid in now_failing and cid not in was_failing:
            regressions.append({"id": cid, "why": "strict_failure"})
            continue
        if cid in was_failing and cid not in now_failing:
            improved.append({"id": cid, "why": "strict_pass"})
            continue
        delta = ""
        if live:
            old_noul = (old.get("after") or {}).get("noul")
            new_noul = (row.get("after") or {}).get("noul")
            if isinstance(old_noul, (int, float)) and isinstance(new_noul, (int, float)):
                delta = round(float(new_noul) - float(old_noul), 4)
                if delta <= -0.1:
                    regressions.append(
                        {"id": cid, "why": "noul_drop", "delta": delta}
                    )
                    continue
                if delta >= 0.1:
                    improved.append(
                        {"id": cid, "why": "noul_gain", "delta": delta}
                    )
                    continue
        old_after = old.get("after") or {}
        new_after = row.get("after") or {}
        if (
            old_after.get("last_pick") != new_after.get("last_pick")
            or old_after.get("step") != new_after.get("step")
            or old.get("defect") != row.get("defect")
        ):
            changed.append(cid)
            continue
        unchanged += 1
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    return {
        "regressions": regressions,
        "improved": improved,
        "changed": sorted(changed),
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "counts": {
            "regressions": len(regressions),
            "improved": len(improved),
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
            "unchanged": unchanged,
        },
    }


def env_report(args) -> dict:
    """Resolved compare.py environment. Values only — never secrets."""
    policy = os.environ.get("JEV_POLICY", "").strip()
    try:
        watch_secs = float(os.environ.get("JEV_COMPARE_WATCH_SECS", "") or 0)
    except ValueError:
        watch_secs = 0.0
    cases = args.cases or str(cases_path())
    return {
        "cases": str(cases),
        "cases_exists": cases != "-" and Path(cases).is_file(),
        "only": args.only,
        "live": bool(args.live),
        "strict": bool(args.strict),
        "policy": policy if policy else "default",
        "watch_max": _watch.cap("JEV_COMPARE_WATCH_MAX", None),
        "watch_secs": watch_secs,
        "watch_quiet": _watch.quiet("JEV_COMPARE_WATCH_QUIET", False),
    }


def _atomic_write(path, text):
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


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    if _watch.maybe_version(sys.argv[1:] if argv is None else argv):
        return 0
    parser = argparse.ArgumentParser(
        description="Compare unguarded vs Jev-guarded traces on sticky prompts."
    )
    parser.add_argument("--live", action="store_true", help="Call Jev Noul for each side")
    parser.add_argument("--failing", action="store_true", help="Show only cases whose guarded (after) side fails the strict gate")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the result payload (e.g. failures); unknown key exits 2")
    parser.add_argument("--md", action="store_true", help="Print rows as a Markdown table")
    parser.add_argument("--out", metavar="PATH", default="", help="Also write the result JSON to PATH")
    parser.add_argument("--report", metavar="PATH", default="", help="Write a markdown compare report (verdict + per-case table) to PATH; with --json writes the report object instead")
    parser.add_argument("--verdict", metavar="PATH", default="", help="Write a slim {verdict, cases, failures} JSON to PATH (in --watch mode refreshed every tick)")
    parser.add_argument("--cases", default=os.environ.get("JEV_COMPARE_CASES", "") or None, help="Path to compare-cases.json ('-' reads cases JSON from stdin; needs a file for --watch/--diff)")
    parser.add_argument("--schema", action="store_true", help="Print the compare-cases.json key contract and exit (--json emits the object)")
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
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick with failures.")
    parser.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    parser.add_argument("--baseline", metavar="PATH", default="", help="Write the current rows to PATH as a baseline file for a later --diff")
    parser.add_argument("--diff", metavar="PATH", default="", help="Load a --baseline file and add a diff block (regressions/improved/changed/added/removed) to the result payload; regressions also join the --strict failure list ('-' reads the baseline JSON from stdin; needs a file --cases, no --watch)")
    parser.add_argument("--trend", metavar="DIR", default="", help="Diff the current rows against every *.json baseline in DIR; adds a trend list ({file,ts,regressions,improved,changed,added,removed,unchanged} sorted by ts) to the payload and one stderr line per baseline")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the offline scorer on two synthetic cases (one pass, one deliberate strict-gate fail); exit 1 when the failure is not detected (--json emits the checks).",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="Print the resolved env config JSON ({cases, cases_exists, only, live, strict, policy, watch_max, watch_secs, watch_quiet}) and exit (--jq KEY prints one field, rc 2 on unknown; --out PATH also writes it).",
    )
    args = parser.parse_args(argv)
    if getattr(args, "schema", False):
        rows = {
            "goal": {"required": False, "type": "string, shared task description"},
            "cases": {"required": True, "type": "list[case]"},
            "case.id": {"required": True, "type": "string, unique"},
            "case.prompt": {"required": True, "type": "string, sticky prompt text"},
            "case.defect": {"required": False, "type": "string label (e.g. drift)"},
            "case.before": {"required": True, "type": "object, unguarded outcome fields"},
            "case.after": {"required": True, "type": "object, guarded outcome fields"},
            "case.score": {"required": False, "type": "number, hand-tuned weight"},
        }
        if args.as_json:
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        else:
            for key in rows:
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, rows[key]["type"], "required" if rows[key]["required"] else "optional")
                )
        return 0
    if getattr(args, "env", False):
        report = env_report(args)
        if getattr(args, "jq", ""):
            node = report
            found = True
            for part in args.jq.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    found = False
                    break
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (env has: %s)\n"
                    % (args.jq, ", ".join(sorted(report)))
                )
                return 2
            sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
            return 0
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if getattr(args, "out", ""):
            try:
                _atomic_write(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0
    if args.self_test:
        fixture = [
            {
                "id": "st_good",
                "defect": "none",
                "prompt": "self-test prompt",
                "before": {"called_jev": False, "current_step": "drifted"},
                "after": {
                    "called_jev": True,
                    "last_pick": "return_to_plan",
                    "current_step": "on plan",
                },
            },
            {
                "id": "st_bad",
                "defect": "drift",
                "prompt": "self-test prompt",
                "before": {"called_jev": False, "current_step": "drifted"},
                "after": {"called_jev": False, "current_step": "still drifted"},
            },
        ]
        checks: dict[str, bool] = {}
        try:
            rows = [row_offline(case) for case in fixture]
            checks["rows"] = [r.get("id") for r in rows] == [
                "st_good",
                "st_bad",
            ]
            failures = strict_failures(rows, live=False)
            checks["strict_detection"] = failures == [
                "st_bad: guarded side did not call Jev"
            ]
            checks["render"] = "st_bad" in format_table(
                rows, False
            ) and "st_bad" in format_md(rows, False)
        except Exception:
            checks = {"raised": False}
        ok = bool(checks) and all(checks.values())
        payload = {"self_test": "ok" if ok else "FAIL", "checks": checks}
        if args.as_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        else:
            sys.stdout.write(
                "self-test: %s %s\n"
                % (
                    payload["self_test"],
                    " ".join(
                        "%s=%s" % (k, "ok" if v else "FAIL")
                        for k, v in sorted(checks.items())
                    ),
                )
            )
        return 0 if ok else 1
    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    if args.cases == "-" and (args.watch or args.diff):
        sys.stderr.write("--cases - (stdin) supports neither --watch nor --diff\n")
        return 2
    if args.diff == "-" and (args.watch or args.cases == "-"):
        sys.stderr.write(
            "--diff - (stdin) needs a file --cases and no --watch\n"
        )
        return 2
    if args.watch and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_COMPARE_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_COMPARE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        verdict_ok = True
        prev_failures: set[str] = set()
        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            cur = run(
                live=args.live,
                as_json=args.as_json,
                path=args.cases or None,
                only=only,
            )
            failing = strict_failures(cur["rows"], args.live)
            new_failures = sorted(set(failing) - prev_failures)
            prev_failures = set(failing)
            tick = {
                "ts": int(_time.time()),
                "cases": len(cur["rows"]),
                "failures": len(failing),
                "new_failures": new_failures,
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, getattr(args, "jq", ""), args.out, quiet=_watch.quiet("JEV_COMPARE_WATCH_QUIET", args.quiet), bad=bool(tick["failures"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d cases=%d failures=%d\n"
                % (ticks, tick["cases"], tick["failures"])
            )
            if args.verdict and verdict_ok and not _write_verdict(args.verdict, tick):
                verdict_ok = False  # warn once, stop retrying
            if args.fail_fast and failing:
                break
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(args.watch)
        if args.verdict and verdict_ok:
            _write_verdict(args.verdict, tick)
        return 0 if tick["failures"] == 0 else 1
    result = run(
        live=args.live,
        as_json=args.as_json,
        path=args.cases or None,
        only=only,
    )
    if args.baseline:
        try:
            _atomic_write(
                Path(args.baseline),
                json.dumps(
                    {
                        "ts": int(time.time()),
                        "live": args.live,
                        "rows": result["rows"],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.baseline, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.baseline)
    if args.diff:
        try:
            raw = json.loads(
                sys.stdin.read()
                if args.diff == "-"
                else Path(args.diff).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            sys.stderr.write("cannot read baseline %s: %s\n" % (args.diff, exc))
            return 2
        base_rows = raw.get("rows") if isinstance(raw, dict) else raw
        if not isinstance(base_rows, list):
            sys.stderr.write("baseline %s has no rows list\n" % args.diff)
            return 2
        result["diff"] = diff_baseline(base_rows, result["rows"], args.live)
        for entry in result["diff"]["regressions"]:
            sys.stderr.write(
                "regression: %s (%s)\n" % (entry["id"], entry["why"])
            )
    if getattr(args, "trend", ""):
        trend_dir = Path(args.trend)
        if not trend_dir.is_dir():
            sys.stderr.write("trend dir %s missing\n" % trend_dir)
            return 2
        trend = []
        for path in sorted(trend_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            base_rows = raw.get("rows") if isinstance(raw, dict) else raw
            if not isinstance(base_rows, list):
                continue
            diff = diff_baseline(base_rows, result["rows"], args.live)
            counts = diff.get("counts") or {}
            trend.append(
                {
                    "file": path.name,
                    "ts": raw.get("ts") if isinstance(raw, dict) else None,
                    "regressions": counts.get("regressions", 0),
                    "improved": counts.get("improved", 0),
                    "changed": counts.get("changed", 0),
                    "added": counts.get("added", 0),
                    "removed": counts.get("removed", 0),
                    "unchanged": counts.get("unchanged", 0),
                }
            )
        trend.sort(key=lambda row: (row["ts"] is None, row["ts"], row["file"]))
        result["trend"] = trend
        for row in trend:
            sys.stderr.write(
                "trend: %s regressions=%d improved=%d changed=%d added=%d removed=%d\n"
                % (
                    row["file"],
                    row["regressions"],
                    row["improved"],
                    row["changed"],
                    row["added"],
                    row["removed"],
                )
            )
    if args.failing:
        failing_ids = {
            f.split(":", 1)[0]
            for f in strict_failures(result["rows"], args.live)
        }
        result["rows"] = [
            r for r in result["rows"] if str(r.get("id") or "?") in failing_ids
        ]
    if args.out:
        out_path = Path(args.out)
        try:
            _atomic_write(
                out_path,
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %s\n" % out_path)
    if args.verdict:
        _write_verdict(
            args.verdict,
            {
                "cases": len(result["rows"]),
                "failures": strict_failures(result["rows"], args.live),
            },
        )
    if args.report:
        failures = strict_failures(result["rows"], args.live)
        if args.as_json:
            report_obj = {
                "verdict": "PASS" if not failures else "FAIL",
                "goal": result.get("goal"),
                "live": args.live,
                "cases": len(result["rows"]),
                "failures": failures,
                "rows": result["rows"],
            }
            text = json.dumps(report_obj, indent=2, ensure_ascii=False) + "\n"
        else:
            text = (
                "# compare report\n\n"
                + "verdict: **%s**\n\n" % ("PASS" if not failures else "FAIL")
                + "- cases: %d\n" % len(result["rows"])
                + "- failures: %d\n\n" % len(failures)
                + "".join("- %s\n" % f for f in failures)
                + "\n"
                + format_md(result["rows"], live=args.live)
            )
        try:
            _atomic_write(Path(args.report), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.report, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.report)
    if getattr(args, "jq", ""):
        node = result
        found = True
        for part in args.jq.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                found = False
                break
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (args.jq, ", ".join(sorted(result)) if isinstance(result, dict) else "")
            )
            return 2
        sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
    elif args.as_json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif args.md:
        sys.stdout.write(format_md(result["rows"], live=args.live))
    else:
        sys.stdout.write(format_table(result["rows"], live=args.live))
    if result.get("error"):
        sys.stderr.write("live scoring failed: %s\n" % result["error"])
    if args.strict:
        failures = strict_failures(result["rows"], args.live)
        for entry in (result.get("diff") or {}).get("regressions", []):
            failures.append(
                "%s: regressed vs baseline (%s)" % (entry["id"], entry["why"])
            )
        for failure in failures:
            sys.stderr.write("strict: %s\n" % failure)
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
