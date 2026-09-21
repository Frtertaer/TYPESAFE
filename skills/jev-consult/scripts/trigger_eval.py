#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""trigger_eval.py - score the jev-consult trigger cases against this
SKILL.md's description using the vendored lexical scorer, printing one
row per case. Read-only; no network; rc 0 when the margin passes.

    python scripts/trigger_eval.py                 # text rows
    python scripts/trigger_eval.py --json          # machine-readable
    python scripts/trigger_eval.py --cases FILE    # another cases file
    python scripts/trigger_eval.py --skill DIR     # another skill dir
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SKILL_DIR = SCRIPTS.parent
REPO_ROOT = SCRIPTS.parents[2]
DEFAULT_CASES = REPO_ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
VENDORED_SCORER = (
    REPO_ROOT / "vendor" / "awesome-llm-apps-skill-evals" / "run_trigger_evals.py"
)


def _load_scorer(path: Path):
    spec = importlib.util.spec_from_file_location("jev_trigger_evals_scorer", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def evaluate(
    cases_path: Path,
    skill_dir: Path,
    case_id: str = "",
    desc_text: str = "",
) -> dict | None:
    """Per-case scores plus the aggregate margin verdict; None on missing inputs.
    With `case_id`, only that case is evaluated (margin still computed across it).
    With `desc_text`, prompts score against that text instead of SKILL.md."""
    if not VENDORED_SCORER.is_file() or not cases_path.is_file():
        return None
    scorer = _load_scorer(VENDORED_SCORER)
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    if case_id:
        cases = [case for case in cases if case.get("id") == case_id]
    desc_tokens = scorer.tokens(
        desc_text if desc_text else scorer.description_of(str(skill_dir))
    )
    margin = getattr(scorer, "MARGIN", 1.15)
    rows: list[dict] = []
    pos: list[float] = []
    neg: list[float] = []
    for case in cases:
        lexical = case.get("lexical") is not False
        expected = bool(case.get("should_trigger"))
        score = None
        if lexical:
            score = scorer.score(scorer.tokens(case.get("prompt", "")), desc_tokens)
            (pos if expected else neg).append(score)
        rows.append(
            {
                "id": case.get("id"),
                "should_trigger": expected,
                "lexical": lexical,
                "score": score,
                "covers": case.get("covers"),
                "ok": (score is not None and score > 0) if (expected and lexical) else True,
            }
        )
    worst_pos = min(pos) if pos else 0.0
    best_neg = max(neg) if neg else 0.0
    ok = bool(pos) and worst_pos > 0 and worst_pos > best_neg * margin
    return {
        "ok": ok,
        "margin": margin,
        "worst_positive": worst_pos,
        "best_negative": best_neg,
        "n_positives": len(pos),
        "n_negatives": len(neg),
        "cases": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score jev-consult trigger cases lexically (per-case rows)."
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--skill", default=str(SKILL_DIR))
    parser.add_argument(
        "--score",
        metavar="TEXT",
        default="",
        help="Score one ad-hoc prompt against the skill description and exit.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Print only the verdict line.")
    parser.add_argument(
        "--fail",
        action="store_true",
        help="Print only the failing case rows (positives that scored 0).",
    )
    parser.add_argument(
        "--ids",
        action="store_true",
        help="Print only the case ids, one per line (with --fail: failing ids only).",
    )
    parser.add_argument(
        "--id",
        metavar="CASE",
        default="",
        help="Evaluate only the case with this id.",
    )
    parser.add_argument(
        "--min-score",
        metavar="F",
        type=float,
        default=None,
        help="Show only rows whose lexical score reaches F (after --fail).",
    )
    parser.add_argument(
        "--desc",
        metavar="TEXT",
        default="",
        help="Score prompts against TEXT instead of the skill's SKILL.md description.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Exit 1 when any case row fails or any lexical negative scores > 0, "
            "not just the margin verdict."
        ),
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Also write the result JSON to PATH.",
    )
    args = parser.parse_args(argv)
    if args.score:
        if not VENDORED_SCORER.is_file():
            sys.stderr.write("missing vendored scorer (%s)\n" % VENDORED_SCORER)
            return 2
        scorer = _load_scorer(VENDORED_SCORER)
        try:
            score = scorer.score(
                scorer.tokens(args.score),
                scorer.tokens(
                    args.desc if args.desc else scorer.description_of(args.skill)
                ),
            )
        except OSError as exc:
            sys.stderr.write("trigger_eval failed: %s\n" % exc)
            return 2
        if args.json:
            sys.stdout.write(
                json.dumps({"prompt": args.score, "score": score}) + "\n"
            )
        else:
            sys.stdout.write("score=%.3f\n" % score)
        return 0
    try:
        result = evaluate(
            Path(args.cases), Path(args.skill), case_id=args.id, desc_text=args.desc
        )
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write("trigger_eval failed: %s\n" % exc)
        return 2
    if result is None:
        sys.stderr.write(
            "missing cases file or vendored scorer (%s)\n" % VENDORED_SCORER
        )
        return 2
    if args.id and not result["cases"]:
        sys.stderr.write("no case with id %r\n" % args.id)
        return 2
    if args.id:
        result["ok"] = all(row["ok"] for row in result["cases"])
    if args.out:
        try:
            Path(args.out).write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            sys.stderr.write("--out failed: %s\n" % exc)
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
    def _rows() -> list[dict]:
        rows = result["cases"]
        if args.fail:
            rows = [r for r in rows if not r["ok"]]
        if args.min_score is not None:
            rows = [
                r
                for r in rows
                if r["score"] is not None and r["score"] >= args.min_score
            ]
        return rows

    if args.ids:
        for row in _rows():
            sys.stdout.write("%s\n" % row["id"])
        return 0 if result["ok"] else 1
    if args.json:
        payload = dict(result)
        payload["cases"] = _rows()
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        if not args.quiet:
            for row in _rows():
                score = "-" if row["score"] is None else "%.3f" % row["score"]
                marker = "" if row["ok"] else "  <-- FAIL"
                sys.stdout.write(
                    "%-28s should_trigger=%-5s lexical=%-5s score=%s%s\n"
                    % (
                        row["id"],
                        row["should_trigger"],
                        row["lexical"],
                        score,
                        marker,
                    )
                )
        sys.stdout.write(
            "margin: %s (worst positive %.3f vs best negative %.3f x %.2f)\n"
            % (
                "PASS" if result["ok"] else "FAIL",
                result["worst_positive"],
                result["best_negative"],
                result["margin"],
            )
        )
    if args.strict and any(
        not row["ok"] or (not row["should_trigger"] and (row["score"] or 0) > 0)
        for row in result["cases"]
    ):
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
