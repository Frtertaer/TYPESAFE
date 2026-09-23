from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch
from inventory import atomic_write_text
from progress_core import Ledger, ProgressError, read_json, validate_plan, validate_progress_policy


def build_parser():
    parser = argparse.ArgumentParser(
        description="Evidence-backed contribution credits and stage reviews; not a background agent",
        epilog="--schema prints the init-plan key contract (no subcommand needed; --schema --json emits the object)",
    )
    parser.add_argument("--repo", default=".", help="Git repository root")
    parser.add_argument("--db", help="Runtime SQLite ledger (default: REPO/.devin/progress.sqlite3)")
    parser.add_argument("--policy", help="Policy file used only when initializing a new stage")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="Freeze an agreed plan and policy; run its trusted baseline checks")
    initialize.add_argument("plan", help="Reviewed stage plan JSON; commands in this file will execute")
    for name in ("status", "history"):
        command = commands.add_parser(name)
        command.add_argument("stage")
    for name in ("status", "history"):
        sub = commands.choices[name]
        sub.add_argument("--watch", metavar="S", type=float, default=0.0,
                         help="Re-read the stage every S seconds, printing one tick per pass (read-only; JEV_PROGRESS_WATCH_MAX caps ticks)")
        sub.add_argument("--max-ticks", metavar="N", type=int, default=0,
                         help="With --watch: stop after N ticks (overrides JEV_PROGRESS_WATCH_MAX)")
        sub.add_argument("--watch-max", metavar="S", type=float, default=0.0,
                         help="With --watch: stop after S elapsed seconds (JEV_PROGRESS_WATCH_SECS bounds without the flag)")
        sub.add_argument("--quiet", action="store_true",
                         help="With --watch: print only noteworthy ticks to stdout (--out still logs all; JEV_PROGRESS_WATCH_QUIET presets)")
        sub.add_argument("--fail-fast", action="store_true",
                         help="With --watch: status stops on the first non-'continue' tick; history stops on the first count change")
        sub.add_argument("--out", metavar="PATH", default="",
                         help="With --watch: append each tick line to PATH (fail-open)")
        sub.add_argument("--verdict", metavar="PATH", default="",
                         help="With --watch: write a slim verdict JSON to PATH, refreshed every tick")
    replay = commands.add_parser("evidence", help="Rebuild the exact Jev input recorded for an assessment or review event")
    replay.add_argument("stage")
    replay.add_argument("sequence", type=int, help="Event sequence number from history")
    assess = commands.add_parser("assess", help="Verify a committed outcome and request one Jev contribution grade")
    assess.add_argument("stage")
    assess.add_argument("item")
    assess.add_argument("--summary", required=True)
    assess.add_argument("--retry-unavailable", action="store_true", help="Explicit retry of an unavailable request only; never rerate a received grade")
    invalidate = commands.add_parser("invalidate", help="Revoke existing credit and retain the audit trail")
    invalidate.add_argument("stage")
    invalidate.add_argument("item")
    invalidate.add_argument("--reason", required=True)
    restore = commands.add_parser("restore", help="Reverify a reviewed restoration without awarding extra points")
    restore.add_argument("stage")
    restore.add_argument("item")
    restore.add_argument("--reason", required=True)
    restore.add_argument("--reviewer", required=True)
    review = commands.add_parser("review", help="Run fresh stage checks and ask Jev to continue, finish, or select a preauthorized pivot")
    review.add_argument("stage")
    review.add_argument("--reason", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--approve-finish", action="store_true", help="Record explicit completion approval; checks and Jev must still permit finishing")
    commands.add_parser("self-test", help="Initialize a scratch ledger with a stub evidence collector and read it back")
    lint_p = commands.add_parser("lint", help="Dry-validate a plan against a policy with the same checks as init; writes nothing")
    lint_p.add_argument("plan", help="Stage-plan JSON file")
    report = commands.add_parser("report", help="Print a markdown summary of a stage (plan, credits, events); --out writes it to a file")
    report.add_argument("stage")
    report.add_argument("--out", metavar="PATH", default="", help="Write the markdown to PATH instead of stdout (prints {wrote, bytes} JSON)")
    for name in commands.choices:
        commands.choices[name].add_argument(
            "--jq", metavar="KEY", default="",
            help="Print just this dotted-path field of the result payload (rc 2 on unknown key)",
        )
    return parser


def _report_md(summary: dict, hist: dict) -> str:
    """Markdown document for one stage: goal, action, per-item credit, events."""
    plan = hist["stage"]["plan"]
    awarded = set(summary["awarded_items"])
    blocked = set(summary["blocked_items"])
    lines = [
        "# Progress: %s" % summary["stage_id"],
        "",
        "- Goal: %s" % plan.get("goal", ""),
        "- Action: `%s` (%s)" % (summary["action"], summary["reason"]),
        "- Points: %s / %s" % (summary["points"], summary["review_at"]),
        "- Assessments: %s / %s; Jev calls: %s / %s" % (
            summary["assessment_count"], summary["assessment_limit"],
            summary["model_attempts"], summary["model_attempt_limit"],
        ),
    ]
    if summary.get("next_direction"):
        lines.append("- Next direction: `%s`" % summary["next_direction"])
    if summary.get("failed_checks"):
        lines.append("- Failed checks: %s" % ", ".join(summary["failed_checks"]))
    lines += ["", "| item | state |", "| --- | --- |"]
    for item in plan.get("items", []):
        state = "credited" if item["id"] in awarded else "blocked" if item["id"] in blocked else "open"
        lines.append("| `%s` | %s |" % (item["id"], state))
    lines += ["", "| seq | event | detail |", "| --- | --- | --- |"]
    for event in hist["events"]:
        data = event.get("data", {})
        detail = (
            data.get("summary") or data.get("choice") or data.get("status")
            or data.get("reason") or ""
        )
        lines.append("| %s | %s | %s |" % (event.get("sequence", "?"), event.get("kind", "?"), str(detail).replace("|", "\\|")))
    return "\n".join(lines)


def _status_tick(ledger, stage, t0):
    try:
        snap = ledger.status(stage)
        tick = {
            "stage": snap["stage_id"],
            "action": snap["action"],
            "reason": snap["reason"],
            "points": snap["points"],
            "review_at": snap["review_at"],
            "assessments": snap["assessment_count"],
            "model_attempts": snap["model_attempts"],
            "awarded": len(snap["awarded_items"]),
            "blocked": len(snap["blocked_items"]),
        }
    except ProgressError as exc:
        tick = {"stage": stage, "action": "error", "reason": exc.code}
    tick["elapsed_s"] = round(time.time() - t0, 2)
    return tick


def _status_watch(ledger, args):
    """Poll ledger.status on a loop; read-only. Exits 1 when the final tick
    is still 'continue' or errored (the stage did not resolve in time)."""
    max_ticks = _watch.cap("JEV_PROGRESS_WATCH_MAX", args.max_ticks)
    dead = _watch.deadline("JEV_PROGRESS_WATCH_SECS", args.watch_max)
    quiet = _watch.quiet("JEV_PROGRESS_WATCH_QUIET", args.quiet)
    ticks = 0
    tick: dict = {}
    verdict_ok = True
    t0 = time.time()

    def _write_verdict() -> bool:
        return _watch.write_verdict(
            args.verdict,
            {
                "verdict": "resolved" if tick.get("action") not in (None, "continue", "error") else "active",
                "ticks": ticks,
                "action": tick.get("action"),
                "points": tick.get("points"),
                "elapsed_s": round(time.time() - t0, 2),
            },
        )

    while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
        tick = _status_tick(ledger, args.stage, t0)
        ticks += 1
        _watch.emit_or_jq(
            tick, args.jq, args.out, quiet=quiet,
            bad=tick["action"] != "continue",
        )
        sys.stderr.write(
            "watch tick=%d action=%s points=%s\n"
            % (ticks, tick["action"], tick.get("points"))
        )
        if args.verdict and verdict_ok and not _write_verdict():
            verdict_ok = False
        if args.fail_fast and tick["action"] != "continue":
            break
        time.sleep(args.watch)
    if args.verdict and verdict_ok and not _write_verdict():
        return 1
    return 0 if tick.get("action") not in ("continue", "error") else 1


def _history_tick(ledger, stage, t0, prev_events):
    try:
        events = ledger.history(stage)["events"]
        tick = {
            "stage": stage,
            "events": len(events),
            "delta": None if prev_events is None else len(events) - prev_events,
            "last_kind": events[-1]["kind"] if events else None,
            "last_seq": events[-1].get("sequence") if events else None,
        }
    except ProgressError as exc:
        tick = {"stage": stage, "events": 0, "delta": None, "error": exc.code}
    tick["elapsed_s"] = round(time.time() - t0, 2)
    return tick


def _history_watch(ledger, args):
    """Poll ledger.history on a loop; read-only. Exits 1 when the final tick
    reports an empty history (no events ever landed)."""
    max_ticks = _watch.cap("JEV_PROGRESS_WATCH_MAX", args.max_ticks)
    dead = _watch.deadline("JEV_PROGRESS_WATCH_SECS", args.watch_max)
    quiet = _watch.quiet("JEV_PROGRESS_WATCH_QUIET", args.quiet)
    ticks = 0
    tick: dict = {}
    prev_events: int | None = None
    verdict_ok = True
    t0 = time.time()

    def _write_verdict() -> bool:
        return _watch.write_verdict(
            args.verdict,
            {
                "verdict": "changed" if tick.get("delta") else "steady",
                "ticks": ticks,
                "events": tick.get("events"),
                "delta": tick.get("delta"),
                "elapsed_s": round(time.time() - t0, 2),
            },
        )

    while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
        tick = _history_tick(ledger, args.stage, t0, prev_events)
        prev_events = tick["events"]
        ticks += 1
        _watch.emit_or_jq(
            tick, args.jq, args.out, quiet=quiet,
            bad=not tick["events"] or bool(tick.get("delta")),
        )
        sys.stderr.write(
            "watch tick=%d events=%s delta=%s\n"
            % (ticks, tick["events"], tick.get("delta"))
        )
        if args.verdict and verdict_ok and not _write_verdict():
            verdict_ok = False
        if args.fail_fast and (tick.get("delta") or "error" in tick):
            break
        time.sleep(args.watch)
    if args.verdict and verdict_ok and not _write_verdict():
        return 1
    return 0 if tick.get("events") else 1


def _emit_jq(payload, jq):
    """When --jq is set, print just that dotted field and return an rc; else None."""
    if not jq:
        return None
    value, found = _watch.dig(payload, jq)
    if not found:
        sys.stderr.write(
            "bad --jq key %r (payload has: %s)\n"
            % (jq, ", ".join(sorted(payload)))
        )
        return 2
    sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
    return 0


# The init plan contract, mirroring validate_plan in progress_core. --schema
# prints it like the other pack scripts: `key: type (required|optional)` or
# the {key: {required, type}} object with --json.
PLAN_SCHEMA_ROWS = {
    "id": {"required": True, "type": "string stage identifier"},
    "goal": {"required": True, "type": "string, agreed outcome statement"},
    "checks": {"required": True, "type": "object{name: argv[]}, bounded by progress.max_checks"},
    "required_checks": {"required": True, "type": "list[check name], run on every verify"},
    "items": {"required": True, "type": "list[item], bounded by progress.max_items"},
    "directions": {"required": False, "type": "object{id: label}, preauthorized pivots"},
    "platform": {"required": False, "type": "any|win32|linux|darwin, default any"},
    "item.id": {"required": True, "type": "string identifier, unique within items"},
    "item.description": {"required": True, "type": "string, agreed outcome"},
    "item.checks": {"required": True, "type": "list[check name] referencing plan.checks"},
    "item.paths": {"required": True, "type": "list[str] repo-relative literals, no .. or drives"},
}


class _StubEvidence:
    """Deterministic collector so `self-test` needs no real Git worktree."""

    def snapshot(self, settings):
        return {"revision": "0" * 40, "tree": "0" * 40, "platform": sys.platform, "config": {}}

    def descendant(self, baseline, revision, settings):
        return True

    def checks(self, definitions, names, settings, revision):
        return [
            {
                "id": name,
                "exit_code": 0,
                "timed_out": False,
                "output_sha256": hashlib.sha256(name.encode()).hexdigest(),
                "output_bytes": 0,
            }
            for name in names
        ]

    def diff(self, baseline, revision, settings, paths):
        return ""


def _self_test(args) -> dict:
    """Exercise initialize/status on a temporary ledger; never contacts Jev."""
    policy_path = Path(args.policy) if args.policy else Path(__file__).resolve().parent.parent / "policy.json"
    plan = {
        "id": "self_test",
        "goal": "Exercise initialize and status without touching a real ledger",
        "checks": {"smoke": ["{python}", "-c", "pass"]},
        "required_checks": ["smoke"],
        "items": [
            {
                "id": "boot",
                "description": "self-test item",
                "checks": ["smoke"],
                "paths": ["skills/jev-consult/scripts/progress.py"],
            }
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "progress.sqlite3", Path(tmp), evidence=_StubEvidence())
        summary = ledger.initialize(plan, read_json(policy_path))
        status = ledger.status(plan["id"])
    if not isinstance(summary, dict) or not isinstance(status, dict):
        raise ProgressError("INVALID_EVIDENCE", "self-test readback malformed")
    return {"self_test": "ok", "stage": plan["id"]}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if _watch.maybe_version(argv):
        return 0
    if "--schema" in argv:
        if "--json" in argv:
            sys.stdout.write(json.dumps(PLAN_SCHEMA_ROWS, indent=2) + "\n")
        else:
            for key in PLAN_SCHEMA_ROWS:
                row = PLAN_SCHEMA_ROWS[key]
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, row["type"], "required" if row["required"] else "optional")
                )
        return 0
    args = build_parser().parse_args(argv)
    try:
        if args.command == "lint":
            policy_path = Path(args.policy) if args.policy else Path(__file__).resolve().parent.parent / "policy.json"
            try:
                policy_doc = read_json(policy_path)
                plan_doc = read_json(Path(args.plan))
                validate_progress_policy(policy_doc)
                validate_plan(plan_doc, policy_doc)
            except ProgressError as exc:
                sys.stdout.write(json.dumps({"lint": "invalid", "code": exc.code, "message": str(exc)}) + "\n")
                return 1
            result = {"lint": "ok", "stage": plan_doc["id"], "items": len(plan_doc["items"]), "checks": len(plan_doc["checks"])}
            rc = _emit_jq(result, args.jq)
            if rc is not None:
                return rc
            sys.stdout.write(json.dumps(result, indent=2) + "\n")
            return 0
        repo = Path(args.repo).resolve()
        database = Path(args.db).resolve() if args.db else repo / ".devin" / "progress.sqlite3"
        ledger = Ledger(database, repo)
        if args.command == "init":
            policy_path = Path(args.policy) if args.policy else Path(__file__).resolve().parent.parent / "policy.json"
            result = ledger.initialize(read_json(Path(args.plan)), read_json(policy_path))
        elif args.command == "assess":
            result = ledger.assess(args.stage, args.item, args.summary, retry_unavailable=args.retry_unavailable)
        elif args.command == "status":
            if args.watch and args.watch > 0:
                return _status_watch(ledger, args)
            result = ledger.status(args.stage)
        elif args.command == "history":
            if args.watch and args.watch > 0:
                return _history_watch(ledger, args)
            result = ledger.history(args.stage)
        elif args.command == "evidence":
            result = ledger.evidence(args.stage, args.sequence)
        elif args.command == "invalidate":
            result = ledger.invalidate(args.stage, args.item, args.reason)
        elif args.command == "restore":
            result = ledger.restore(args.stage, args.item, args.reason, args.reviewer)
        elif args.command == "self-test":
            result = _self_test(args)
        elif args.command == "report":
            summary = ledger.status(args.stage)
            hist = ledger.history(args.stage)
            md = _report_md(summary, hist)
            if args.out:
                atomic_write_text(Path(args.out), md)
                result = {"wrote": args.out, "bytes": len(md.encode("utf-8"))}
            elif args.jq:
                result = {"report_md": md, "stage": summary["stage_id"],
                          "points": summary["points"], "action": summary["action"]}
            else:
                sys.stdout.write(md + "\n")
                return 0
        else:
            result = ledger.review(args.stage, args.reason, args.reviewer, approve_finish=args.approve_finish)
    except ProgressError as exc:
        sys.stdout.write(json.dumps({"error": {"code": exc.code, "message": str(exc)}}) + "\n")
        return 2
    rc = _emit_jq(result, getattr(args, "jq", ""))
    if rc is not None:
        return rc
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
