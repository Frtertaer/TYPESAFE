from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch
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
    return parser


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
            sys.stdout.write(json.dumps({"lint": "ok", "stage": plan_doc["id"], "items": len(plan_doc["items"]), "checks": len(plan_doc["checks"])}, indent=2) + "\n")
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
            result = ledger.status(args.stage)
        elif args.command == "history":
            result = ledger.history(args.stage)
        elif args.command == "evidence":
            result = ledger.evidence(args.stage, args.sequence)
        elif args.command == "invalidate":
            result = ledger.invalidate(args.stage, args.item, args.reason)
        elif args.command == "restore":
            result = ledger.restore(args.stage, args.item, args.reason, args.reviewer)
        elif args.command == "self-test":
            result = _self_test(args)
        else:
            result = ledger.review(args.stage, args.reason, args.reviewer, approve_finish=args.approve_finish)
    except ProgressError as exc:
        sys.stdout.write(json.dumps({"error": {"code": exc.code, "message": str(exc)}}) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
