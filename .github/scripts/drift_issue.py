#!/usr/bin/env python3
"""Compose the live-eval drift-watch issue body for GitHub Actions.

Reads the weekly eval artifacts -- compare.py's ``--out`` payload
(eval-live.json), its ``--verdict`` slim file, and an optional alerts
file (``alert:``-prefixed lines from ``decisions.py --acceptance-gate``)
-- and renders one markdown issue body when any drift signal fires:

- verdict FAIL (strict gate, diff regressions, or streak fails)
- a live-scoring error
- drift flags/fails (a case below its noul gate N runs in a row)
- miss/override-rate alerts

Nothing firing means empty output, so the workflow can gate its
``gh issue`` calls on the file being non-empty. The body never contains
secrets -- only run metadata and case scores.

Usage:
  python .github/scripts/drift_issue.py eval-live.json \
      [--verdict eval-verdict.json] [--alerts eval-alerts.txt] \
      [--run-url URL] [--out drift-issue.md] [--json]
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path


def _read_json(path: str):
    """Return the parsed JSON object, or None on missing/malformed input."""
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _read_alerts(path: str) -> list[str]:
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("alert:"):
            out.append(line[len("alert:") :].strip())
        elif line.startswith("alert "):
            out.append(line[len("alert ") :].strip())
    return out


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _fmt_noul(v) -> str:
    return "%.2f" % float(v) if _num(v) else "-"


def _iso(ts) -> str:
    if not _num(ts):
        return "?"
    return datetime.datetime.fromtimestamp(
        float(ts), tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%MZ")


def case_rows(payload: dict) -> list[str]:
    """Markdown rows: per-case before/after/baseline noul + gate."""
    rows = []
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "?")
        before = row.get("before") or {}
        after = row.get("after") or {}
        baseline = row.get("baseline") or {}
        rows.append(
            "| %s | %s | %s | %s |"
            % (
                cid,
                _fmt_noul(before.get("noul")),
                _fmt_noul(after.get("noul")),
                _fmt_noul(baseline.get("noul")),
            )
        )
    return rows


def diff_lines(diff: dict) -> list[str]:
    counts = diff.get("counts") or {}
    lines = [
        "- regressions: %d, improved: %d, changed: %d, added: %d, removed: %d"
        % (
            counts.get("regressions", 0),
            counts.get("improved", 0),
            counts.get("changed", 0),
            counts.get("added", 0),
            counts.get("removed", 0),
        )
    ]
    for entry in diff.get("regressions") or []:
        why = entry.get("why") or "?"
        delta = entry.get("delta")
        if _num(delta):
            suffix = " (%+.2f)" % float(delta)
        elif why != "strict_failure":
            suffix = " (%s)" % why
        else:
            suffix = ""
        lines.append(
            "- regression: `%s`%s" % (entry.get("id") or "?", suffix)
        )
    for cid in diff.get("removed") or []:
        lines.append("- removed case: `%s`" % cid)
    for cid in diff.get("added") or []:
        lines.append("- added case: `%s`" % cid)
    return lines


def build_issue(
    payload: dict,
    verdict: dict | None,
    alerts: list[str],
    run_url: str = "",
) -> str | None:
    """The issue body, or None when nothing fires."""
    error = str(payload.get("error") or "").strip()
    drift = payload.get("drift") or {}
    flags = [str(f) for f in drift.get("flags") or []]
    fails = [str(f) for f in drift.get("fails") or []]
    verdict_word = str((verdict or {}).get("verdict") or "")
    failures = [str(f) for f in (verdict or {}).get("failures") or []]
    if verdict_word not in ("PASS", "FAIL"):
        verdict_word = "FAIL" if (error or fails) else "PASS"

    fires = bool(
        error or verdict_word == "FAIL" or flags or fails or alerts
    )
    if not fires:
        return None

    lines = ["### live-eval drift watch", ""]
    if run_url:
        lines.append("**run**: %s" % run_url)
    lines.append("**verdict**: **%s**" % verdict_word)
    if error:
        lines.append("**error**: `%s`" % error)
    lines.append("")

    if failures:
        lines.append("#### failures")
        lines += ["- %s" % f for f in failures]
        lines.append("")

    if flags or fails:
        lines.append("#### drift streaks")
        for f in flags:
            marker = " (fail-gate)" if f in fails else ""
            lines.append("- %s%s" % (f, marker))
        lines.append("")

    if alerts:
        lines.append("#### acceptance alerts")
        lines += ["- %s" % a for a in alerts]
        lines.append("")

    rows = case_rows(payload)
    if rows:
        lines.append("#### per-case noul")
        lines.append("| case | before | after | baseline |")
        lines.append("| --- | --- | --- | --- |")
        lines += rows
        lines.append("")

    diff = payload.get("diff")
    if isinstance(diff, dict):
        lines.append("#### diff vs previous baseline")
        lines += diff_lines(diff)
        lines.append("")

    lines.append(
        "_posted by `.github/workflows/live-eval.yml`; "
        "streak thresholds: `streak_warn_weeks`/`streak_fail_weeks`, "
        "acceptance gates: `miss_rate_max`/`override_rate_max` "
        "(skills/jev-consult/policy.json)_"
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compose the drift-watch issue body from eval artifacts."
    )
    parser.add_argument(
        "eval_json",
        nargs="?",
        default="",
        help="compare.py --out payload (eval-live.json)",
    )
    parser.add_argument(
        "--verdict",
        metavar="PATH",
        default="",
        help="compare.py --verdict slim JSON (verdict + failures list)",
    )
    parser.add_argument(
        "--alerts",
        metavar="PATH",
        default="",
        help="text file with `alert:` lines (decisions.py --acceptance-gate)",
    )
    parser.add_argument(
        "--run-url", metavar="URL", default="", help="CI run link"
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="write the body to PATH instead of stdout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit {fired, body} JSON instead of the bare body",
    )
    args = parser.parse_args(argv)

    eval_path = Path(args.eval_json) if args.eval_json else None
    if eval_path is None or not eval_path.is_file():
        # no eval artifact at all (skipped run): nothing to post
        body = None
    else:
        try:
            payload = json.loads(
                eval_path.read_text(encoding="utf-8-sig")
            )
        except ValueError:
            payload = {
                "error": "eval payload unparseable: %s" % eval_path.name
            }
        if not isinstance(payload, dict):
            payload = {"error": "eval payload is not an object"}
        body = build_issue(
            payload,
            _read_json(args.verdict),
            _read_alerts(args.alerts),
            args.run_url,
        )

    if args.json:
        sys.stdout.write(
            json.dumps({"fired": body is not None, "body": body or ""})
            + "\n"
        )
        return 0
    if body is None:
        return 0
    if args.out:
        target = Path(args.out)
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
    else:
        sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
