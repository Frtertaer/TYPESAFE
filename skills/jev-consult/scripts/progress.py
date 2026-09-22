from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from progress_core import Ledger, ProgressError, read_json


def build_parser():
    parser = argparse.ArgumentParser(description="Evidence-backed contribution credits and stage reviews; not a background agent")
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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
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
        else:
            result = ledger.review(args.stage, args.reason, args.reviewer, approve_finish=args.approve_finish)
    except ProgressError as exc:
        sys.stdout.write(json.dumps({"error": {"code": exc.code, "message": str(exc)}}) + "\n")
        return 2
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
