#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""drift_calibrate.py - turn a live-eval drift streak into a calibration PR plan.

Reads the artifacts the weekly workflow produces -- eval-live.json (the
compare --live --out payload carrying the ``drift`` block with per-case
streaks), eval-history.jsonl, and the eval decisions log -- and decides
what the workflow should do:

- no case at/above ``streak_fail_weeks``        -> action ``skip``
- an open ``calibration`` PR already exists     -> action ``dedupe``
- too few replayable decisions entries          -> action ``comment``
  (fail-open: the CI log is often thin; comment on the drift issue)
- calibrate finds no verdict-backed flips       -> action ``comment``
- flips >= ``calibrate_min_flips``              -> action ``open_pr``:
  writes policy.json (patched copy), policy.diff and pr-body.md into
  --out-dir for the workflow to branch/commit/push/open as a PR.

A "verdict-backed flip" is a calibrate-eligible routing record carrying a
recorded ``outcome`` verdict whose replayed outcome changes between the
current and recommended (confidence_floor, strong_pick) thresholds.

The script never edits the real policy.json and never calls git or gh --
side effects stay in the workflow, which keeps this tool offline-testable.

Usage:
  python drift_calibrate.py --eval eval-live.json [--history eval-history.jsonl]
      [--decisions decisions.jsonl] [--policy policy.json] [--open-prs prs.json]
      [--out-dir calibration] [--branch NAME] [--label NAME] [--run-url URL]
      [--min-entries N] [--min-flips N] [--dry-run]
      [--json] [--jq KEY] [--schema] [--env] [--verdict PATH] [--self-test]
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _watch  # noqa: E402
import decisions  # noqa: E402
import inventory  # noqa: E402

PLAN_SCHEMA_ROWS = {
    "action": {"type": "skip|dedupe|comment|open_pr", "required": True},
    "reason": {"type": "string", "required": True},
    "generated_ts": {"type": "int, epoch seconds", "required": True},
    "run_url": {"type": "string", "required": True},
    "fail_weeks": {"type": "int", "required": True},
    "streak_cases": {"type": "{case: runs}", "required": True},
    "min_entries": {"type": "int", "required": True},
    "min_flips": {"type": "int", "required": True},
    "decisions": {"type": "{path, entries, eligible}", "required": True},
    "calibrate": {"type": "decisions.calibrate report | null", "required": True},
    "flips": {"type": "int, replay outcome changes", "required": True},
    "verdicted_flips": {"type": "int, flips on outcome-bearing records", "required": True},
    "existing_prs": {"type": "list of open calibration PRs", "required": True},
    "pr": {"type": "{title, branch, label, body, files} | null", "required": True},
    "comment": {"type": "{body} | null", "required": True},
    "files": {"type": "list of written paths", "required": True},
    "dry_run": {"type": "bool", "required": True},
}

DEFAULT_BRANCH = "calibration/drift"
DEFAULT_LABEL = "calibration"


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _read_json(path: str):
    """Parsed JSON object/list, or None on missing/malformed input."""
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


def _int_arg(value, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _policy_int(policy: dict, key: str, default: int) -> int:
    v = policy.get(key)
    if isinstance(v, dict):
        v = v.get("default", default)
    if not _num(v):
        return default
    return max(0, int(v))


def _streak_map(eval_payload, history: list[dict]) -> dict:
    """{case: consecutive-runs-below-gate} from the eval payload's drift
    block, falling back to the newest history record's streaks."""
    streaks: dict = {}
    drift = (eval_payload or {}).get("drift") or {}
    for cid, n in (drift.get("streaks") or {}).items():
        if _num(n):
            streaks[str(cid)] = max(streaks.get(str(cid), 0), int(n))
    latest = next((r for r in reversed(history) if isinstance(r, dict)), {})
    for cid, n in (latest.get("streaks") or {}).items():
        if _num(n):
            streaks[str(cid)] = max(streaks.get(str(cid), 0), int(n))
    return streaks


def _replay_flips(eligible: list[dict], report: dict) -> tuple[int, int, list[dict]]:
    """(flips, verdicted_flips, detail): entries whose replayed outcome
    changes between the current and recommended thresholds. A flip is
    verdict-backed when the record carries an `outcome` verdict."""
    cur = report.get("current") or {}
    rec = report.get("recommended") or {}
    flips = 0
    verdicted = 0
    detail = []
    for item in eligible:
        before = decisions._replay(
            item, cur.get("confidence_floor", 0.55), cur.get("strong_pick", 0.85)
        )
        after = decisions._replay(
            item,
            rec.get("confidence_floor") if rec.get("confidence_floor") is not None else cur.get("confidence_floor", 0.55),
            rec.get("strong_pick") if rec.get("strong_pick") is not None else cur.get("strong_pick", 0.85),
        )
        if before == after:
            continue
        flips += 1
        has_verdict = bool(str(item.get("outcome") or "").strip())
        if has_verdict:
            verdicted += 1
        if len(detail) < 25:
            detail.append(
                {
                    "prompt_sha": str(item.get("prompt_sha") or "?"),
                    "prompt_head": str(item.get("prompt_head") or "")[:60],
                    "outcome": str(item.get("outcome") or ""),
                    "from": before,
                    "to": after,
                }
            )
    return flips, verdicted, detail


def _pr_body(plan: dict, report: dict, diff: str) -> str:
    cur = report.get("current") or {}
    rec = report.get("recommended") or {}
    lines = [
        "## Drift-driven calibration proposal",
        "",
        "The live-eval drift watch reports a case stuck below its noul "
        "gate for the `streak_fail_weeks` streak. Replaying the eval "
        "decisions log under the calibrated thresholds produces "
        "**%d verdict-backed flips** (%d total), so this PR carries the "
        "recommended `policy.json` change for a human to merge."
        % (plan["verdicted_flips"], plan["flips"]),
        "",
        "**Never auto-applied** -- merging is a human decision. The patched "
        "`skills/jev-consult/policy.json` on this branch is the full file; "
        "the diff below is the effective change.",
        "",
        "### Streak cases at the fail gate",
        "",
        "| case | runs below gate |",
        "| --- | --- |",
    ]
    for cid, n in sorted(
        plan["streak_cases"].items(), key=lambda kv: (-kv[1], kv[0])
    ):
        lines.append("| `%s` | %d |" % (cid, n))
    lines += [
        "",
        "### Threshold change (calibrate report)",
        "",
        "| key | current | recommended |",
        "| --- | --- | --- |",
    ]
    for key in ("confidence_floor", "strong_pick", "tight_gap", "noul_yes"):
        rv = rec.get(key)
        if rv is None:
            continue
        lines.append(
            "| `%s` | %s | %s |"
            % (key, cur.get(key), rv)
        )
    lines += [
        "",
        "cost: current %.4f -> recommended %s  |  replayable entries: %d "
        "(skipped %d, health-excluded %d)"
        % (
            (report.get("cost") or {}).get("current") or 0.0,
            (report.get("cost") or {}).get("recommended"),
            report.get("entries") or 0,
            report.get("skipped") or 0,
            report.get("health_excluded") or 0,
        ),
        "",
        "### Flips",
        "",
        "| prompt | verdict | outcome now -> recommended |",
        "| --- | --- | --- |",
    ]
    for row in plan["flips_detail"]:
        lines.append(
            "| `%s` %s | %s | %s -> %s |"
            % (
                row["prompt_sha"],
                row["prompt_head"],
                row["outcome"] or "-",
                row["from"],
                row["to"],
            )
        )
    lines += ["", "### policy.json diff", "", "```diff", diff.rstrip(), "```", ""]
    if plan.get("run_url"):
        lines.append("_eval run: %s_" % plan["run_url"])
    lines.append(
        "_composed by `drift_calibrate.py` in "
        "`.github/workflows/live-eval.yml`; gates: "
        "`streak_fail_weeks`, `calibrate_min_entries`, `calibrate_min_flips`_"
    )
    return "\n".join(lines) + "\n"


def _comment_body(plan: dict) -> str:
    lines = [
        "### drift -> calibration check",
        "",
        "Streak cases at the fail gate: %s."
        % (
            ", ".join(
                "`%s` (%d)" % (c, n)
                for c, n in sorted(
                    plan["streak_cases"].items(), key=lambda kv: (-kv[1], kv[0])
                )
            )
            or "none"
        ),
        "",
        "No calibration PR opened: %s." % plan["reason"],
        "",
    ]
    dec = plan.get("decisions") or {}
    cal = plan.get("calibrate")
    lines.append(
        "- replayable decisions entries: %d (need >= %d)"
        % (dec.get("eligible") or 0, plan.get("min_entries") or 0)
    )
    if cal:
        lines.append(
            "- verdict-backed flips: %d (need >= %d)"
            % (plan.get("verdicted_flips") or 0, plan.get("min_flips") or 0)
        )
    if plan.get("run_url"):
        lines.append("- eval run: %s" % plan["run_url"])
    lines.append(
        "_composed by `drift_calibrate.py` -- when a verdict-backed log "
        "reaches `calibrate_min_entries`, the next streak will open a "
        "`calibration` PR._"
    )
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str, files: list) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        inventory.atomic_write_text(path, text)
    except OSError as exc:
        sys.stderr.write("cannot write %s: %s\n" % (path, exc))
        return False
    files.append(str(path))
    return True


def build_plan(args, policy: dict, policy_path: Path) -> dict:
    eval_payload = _read_json(args.eval)
    history = (
        decisions.load_history(Path(args.history)) if args.history else []
    )
    fail_weeks = _policy_int(
        (eval_payload or {}).get("drift") or {},
        "fail_weeks",
        _policy_int(policy, "streak_fail_weeks", 0),
    )
    streaks = _streak_map(eval_payload, history)
    streak_cases = {
        cid: n for cid, n in streaks.items() if fail_weeks and n >= fail_weeks
    }
    open_prs = _read_json(args.open_prs) or []
    if not isinstance(open_prs, list):
        open_prs = []
    open_prs = [p for p in open_prs if isinstance(p, dict)]

    min_entries = (
        _int_arg(args.min_entries, 0)
        or _policy_int(policy, "calibrate_min_entries", 10)
    )
    min_flips = (
        _int_arg(args.min_flips, 0)
        or _policy_int(policy, "calibrate_min_flips", 1)
    )

    plan = {
        "action": "skip",
        "reason": "",
        "generated_ts": int(time.time()),
        "run_url": args.run_url or "",
        "fail_weeks": fail_weeks,
        "streak_cases": streak_cases,
        "min_entries": min_entries,
        "min_flips": min_flips,
        "decisions": {"path": "", "entries": 0, "eligible": 0},
        "calibrate": None,
        "flips": 0,
        "verdicted_flips": 0,
        "flips_detail": [],
        "existing_prs": [
            {
                "number": p.get("number"),
                "title": str(p.get("title") or ""),
                "url": str(p.get("url") or ""),
            }
            for p in open_prs
        ],
        "pr": None,
        "comment": None,
        "files": [],
        "dry_run": bool(args.dry_run),
    }

    if not fail_weeks:
        plan["reason"] = "streak_fail_weeks is 0 (drift fail gate disabled)"
        return plan
    if not streak_cases:
        plan["reason"] = "no case at or above the fail streak"
        return plan
    if open_prs:
        plan["action"] = "dedupe"
        plan["reason"] = "open %s PR already exists: %s" % (
            args.label,
            ", ".join(
                p.get("url") or "#%s" % p.get("number") for p in open_prs
            ),
        )
        return plan

    log_path = (
        Path(args.decisions)
        if args.decisions
        else inventory.decisions_log_path()
    )
    entries, bad = ([], 0)
    if log_path is not None:
        entries, bad = decisions.load_entries(log_path)
    eligible = [e for e in entries if decisions._calibrate_eligible(e)]
    plan["decisions"] = {
        "path": str(log_path) if log_path else "",
        "entries": len(entries),
        "eligible": len(eligible),
        "bad_lines": bad,
    }
    if len(eligible) < min_entries:
        plan["action"] = "comment"
        plan["reason"] = (
            "insufficient data: %d replayable entries < %d"
            % (len(eligible), min_entries)
        )
        plan["comment"] = {"body": _comment_body(plan)}
        return plan

    eval_rows = (
        (eval_payload or {}).get("rows")
        if isinstance(eval_payload, dict)
        else None
    )
    eval_pairs = (
        decisions._eval_noul_pairs(eval_rows)
        if isinstance(eval_rows, list)
        else None
    )
    report = decisions.calibrate(
        entries, policy=policy, health_filter=True, eval_pairs=eval_pairs
    )
    plan["calibrate"] = report
    flips, verdicted, detail = _replay_flips(eligible, report)
    plan["flips"] = flips
    plan["verdicted_flips"] = verdicted
    plan["flips_detail"] = detail
    if verdicted < min_flips:
        plan["action"] = "comment"
        plan["reason"] = (
            "no verdict-backed flips: %d verdicted (< %d) of %d total"
            % (verdicted, min_flips, flips)
        )
        plan["comment"] = {"body": _comment_body(plan)}
        return plan

    old_text, new_text = decisions._calibrate_policy_patch(
        policy_path, report["recommended"]
    )
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(policy_path),
            tofile=str(policy_path) + " (calibrated)",
        )
    )
    plan["action"] = "open_pr"
    plan["reason"] = "%d verdict-backed flips >= %d" % (verdicted, min_flips)
    plan["pr"] = {
        "title": "calibration: drift-backed policy thresholds (%d flips)"
        % verdicted,
        "branch": args.branch or DEFAULT_BRANCH,
        "label": args.label or DEFAULT_LABEL,
        "body": _pr_body(plan, report, diff),
        "policy_patched": new_text,
        "diff": diff,
    }
    return plan


def _env_report(args) -> dict:
    return {
        "eval": args.eval or None,
        "history": args.history or None,
        "decisions": args.decisions or None,
        "policy": str(args.policy or decisions._policy_path()),
        "open_prs": args.open_prs or None,
        "out_dir": args.out_dir,
        "branch": args.branch or DEFAULT_BRANCH,
        "label": args.label or DEFAULT_LABEL,
        "min_entries": args.min_entries,
        "min_flips": args.min_flips,
        "dry_run": bool(args.dry_run),
    }


def _self_test() -> int:
    """Synthetic streak-hit fixtures -> open_pr plan; empty fixtures ->
    skip/comment. Asserts the real policy file is never touched."""
    import tempfile

    checks = {}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pol = dict(inventory._policy_dict())
        pol["streak_fail_weeks"] = 3
        pol["calibrate_min_entries"] = 5
        pol["calibrate_min_flips"] = 1
        pol_path = tmp / "policy.json"
        inventory.atomic_write_text(
            pol_path, json.dumps(pol, indent=2) + "\n"
        )
        before_policy = pol_path.read_bytes()

        eval_path = tmp / "eval-live.json"
        inventory.atomic_write_text(
            eval_path,
            json.dumps(
                {
                    "drift": {
                        "streaks": {"noul-budget": 3},
                        "flags": [],
                        "fails": ["noul-budget"],
                        "warn_weeks": 2,
                        "fail_weeks": 3,
                    },
                    "rows": [],
                }
            ),
        )
        log = tmp / "decisions.jsonl"
        log_lines = []
        for i in range(8):
            log_lines.append(
                json.dumps(
                    {
                        "ts": 1700000000.0 + i,
                        "schema": 2,
                        "harness": "hermes",
                        "prompt_sha": "selftest%04d" % i,
                        "prompt_head": "pick a skill %d" % i,
                        "jev_status": "escalate",
                        "probabilities": {"demo": 0.9, "other": 0.1},
                        "pick_confidence": 0.2,
                        "strong_pick": True,
                        "shortlist": ["demo", "other"],
                        "outcome": "applied",
                    }
                )
            )
        inventory.atomic_write_text(log, "\n".join(log_lines) + "\n")
        out_dir = tmp / "cal"
        rc = main(
            [
                "--eval",
                str(eval_path),
                "--decisions",
                str(log),
                "--policy",
                str(pol_path),
                "--out-dir",
                str(out_dir),
            ]
        )
        plan_file = out_dir / "plan.json"
        checks["rc0"] = rc == 0
        checks["plan_written"] = plan_file.is_file()
        if plan_file.is_file():
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
            checks["action_open_pr"] = plan["action"] == "open_pr"
            checks["flips"] = plan["verdicted_flips"] > 0
            checks["body"] = (out_dir / "pr-body.md").is_file()
            checks["patched_policy"] = (out_dir / "policy.json").is_file()
            checks["policy_untouched"] = pol_path.read_bytes() == before_policy
        rc2 = main(
            [
                "--eval",
                str(eval_path),
                "--decisions",
                str(tmp / "missing.jsonl"),
                "--policy",
                str(pol_path),
                "--out-dir",
                str(tmp / "cal2"),
            ]
        )
        plan2 = json.loads((tmp / "cal2" / "plan.json").read_text())
        checks["thin_comment"] = plan2["action"] == "comment"
        checks["rc2_0"] = rc2 == 0
    ok = all(checks.values())
    sys.stdout.write(
        "self-test: %s %s\n"
        % (
            "ok" if ok else "FAIL",
            " ".join(
                "%s=%s" % (k, "ok" if v else "FAIL")
                for k, v in sorted(checks.items())
            ),
        )
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if _watch.maybe_version(argv):
        return 0
    parser = argparse.ArgumentParser(
        prog="drift_calibrate.py",
        description="Compose a calibration-PR plan from live-eval drift "
        "streaks; writes plan/body/policy artifacts into --out-dir, never "
        "touches git or the real policy.json.",
    )
    parser.add_argument(
        "--eval",
        metavar="PATH",
        default="",
        help="eval-live.json compare payload (drift.streaks + rows)",
    )
    parser.add_argument(
        "--history",
        metavar="PATH",
        default="",
        help="eval-history.jsonl (streak fallback source)",
    )
    parser.add_argument(
        "--decisions",
        metavar="PATH",
        default="",
        help="decisions.jsonl to calibrate on (default: JEV_CONSULT_LOG or "
        "the per-user cache log)",
    )
    parser.add_argument(
        "--policy",
        metavar="PATH",
        default="",
        help="policy.json to calibrate against (default: JEV_POLICY or "
        "the bundled file)",
    )
    parser.add_argument(
        "--open-prs",
        metavar="PATH",
        default="",
        help="JSON list of open calibration PRs for dedupe "
        "(gh pr list --json number,title,url)",
    )
    parser.add_argument(
        "--out-dir",
        metavar="DIR",
        default="calibration",
        help="directory for plan.json / pr-body.md / comment.md / "
        "policy.json / policy.diff",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="also write the plan JSON (or the --env report) to PATH",
    )
    parser.add_argument(
        "--branch",
        metavar="NAME",
        default="",
        help="PR branch name recorded in the plan "
        "(default %s)" % DEFAULT_BRANCH,
    )
    parser.add_argument(
        "--label",
        metavar="NAME",
        default="",
        help="PR label recorded in the plan (default %s)" % DEFAULT_LABEL,
    )
    parser.add_argument(
        "--run-url",
        metavar="URL",
        default="",
        help="CI run link embedded in the PR body / issue comment",
    )
    parser.add_argument(
        "--min-entries",
        metavar="N",
        type=int,
        default=0,
        help="replayable-entry floor for a PR (policy calibrate_min_entries)",
    )
    parser.add_argument(
        "--min-flips",
        metavar="N",
        type=int,
        default=0,
        help="verdict-backed-flip floor for a PR (policy calibrate_min_flips)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute the plan and print it without writing --out-dir files",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the plan JSON to stdout",
    )
    parser.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="print one dotted-path field of the plan (rc 2 on unknown)",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="print the plan key contract and exit (--json emits the object)",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="print the resolved inputs/config JSON",
    )
    parser.add_argument(
        "--verdict",
        metavar="PATH",
        default="",
        help="write a slim {verdict: ok, action, verdicted_flips} JSON",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the synthetic-fixture checks",
    )
    args = parser.parse_args(argv)

    if args.schema:
        if args.json:
            sys.stdout.write(json.dumps(PLAN_SCHEMA_ROWS, indent=2) + "\n")
        else:
            for key in PLAN_SCHEMA_ROWS:
                meta = PLAN_SCHEMA_ROWS[key]
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (
                        key,
                        meta["type"],
                        "required" if meta["required"] else "optional",
                    )
                )
        return 0
    if args.self_test:
        return _self_test()
    if args.env:
        report = _env_report(args)
        if args.jq:
            val, ok = _watch.dig(report, args.jq)
            if not ok:
                sys.stderr.write(
                    "bad --jq key %r (env has: %s)\n"
                    % (args.jq, ", ".join(sorted(report)))
                )
                return 2
            sys.stdout.write(json.dumps(val) + "\n")
            return 0
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if args.out and args.out != "-":
            try:
                inventory.atomic_write_text(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0

    policy_path = (
        Path(args.policy) if args.policy else decisions._policy_path()
    )
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        if not isinstance(policy, dict):
            policy = {}
    except (OSError, ValueError):
        policy = {}
    plan = build_plan(args, policy, policy_path)

    out_dir = Path(args.out_dir)
    files = plan["files"]
    if not args.dry_run:
        if not _write(
            out_dir / "plan.json", json.dumps(plan, indent=2) + "\n", files
        ):
            return 1
        if plan["action"] == "open_pr" and plan["pr"]:
            if not _write(
                out_dir / "policy.json", plan["pr"]["policy_patched"], files
            ):
                return 1
            if not _write(out_dir / "policy.diff", plan["pr"]["diff"], files):
                return 1
            if not _write(out_dir / "pr-body.md", plan["pr"]["body"], files):
                return 1
        if plan["comment"] is not None:
            if not _write(
                out_dir / "comment.md", plan["comment"]["body"], files
            ):
                return 1
    if args.jq:
        val, ok = _watch.dig(plan, args.jq)
        if not ok:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (args.jq, ", ".join(sorted(plan)))
            )
            return 2
        sys.stdout.write(json.dumps(val) + "\n")
    elif args.json or args.dry_run:
        sys.stdout.write(json.dumps(plan, indent=2) + "\n")
    else:
        sys.stdout.write(
            "%s: %s\n" % (plan["action"], plan["reason"])
        )
    if args.out and args.out != "-":
        try:
            inventory.atomic_write_text(
                Path(args.out), json.dumps(plan, indent=2) + "\n"
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
    if args.verdict:
        _watch.write_verdict(
            args.verdict,
            {
                "verdict": "ok",
                "action": plan["action"],
                "streak_cases": len(plan["streak_cases"]),
                "verdicted_flips": plan["verdicted_flips"],
            },
        )
    return 0


if __name__ == "__main__":
    raise _watch.exit_safely(main())
