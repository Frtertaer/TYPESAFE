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
import os
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
    include_tokens: bool = False,
    margin_override: float | None = None,
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
    margin = (
        getattr(scorer, "MARGIN", 1.15)
        if margin_override is None
        else margin_override
    )
    rows: list[dict] = []
    pos: list[float] = []
    neg: list[float] = []
    for case in cases:
        lexical = case.get("lexical") is not False
        expected = bool(case.get("should_trigger"))
        prompt_text = case.get("prompt", "")
        score = None
        matched: list[str] = []
        row_unmatched: list[str] = []
        if lexical:
            prompt_tokens = scorer.tokens(case.get("prompt", ""))
            score = scorer.score(prompt_tokens, desc_tokens)
            (pos if expected else neg).append(score)
            if include_tokens:
                matched = sorted(set(prompt_tokens) & set(desc_tokens))
                row_unmatched = sorted(set(prompt_tokens) - set(desc_tokens))
        row = {
            "id": case.get("id"),
            "prompt": prompt_text,
            "should_trigger": expected,
            "lexical": lexical,
            "score": score,
            "covers": case.get("covers"),
            "ok": (score is not None and score > 0) if (expected and lexical) else True,
        }
        if include_tokens:
            row["matched"] = matched
            row["unmatched"] = row_unmatched
        rows.append(row)
    worst_pos = min(pos) if pos else 0.0
    best_neg = max(neg) if neg else 0.0
    ok = bool(pos) and worst_pos > 0 and worst_pos > best_neg * margin
    hits = sum(
        1
        for row in rows
        if (row["should_trigger"] and row["score"] is not None and row["score"] > 0)
        or (
            not row["should_trigger"]
            and (not row["lexical"] or not row["score"])
        )
    )
    return {
        "ok": ok,
        "margin": margin,
        "worst_positive": worst_pos,
        "best_negative": best_neg,
        "n_positives": len(pos),
        "n_negatives": len(neg),
        "hits": hits,
        "coverage": (hits / len(rows)) if rows else 0.0,
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
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Print rows as CSV: id,should_trigger,lexical,score,ok.",
    )
    parser.add_argument(
        "--covers",
        action="store_true",
        help="Print per-tag coverage counts and ids of cases with no covers field.",
    )
    parser.add_argument(
        "--covers-map",
        action="store_true",
        help="Print each covers tag followed by the case ids that carry it.",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Print the case hit rate (positives scoring >0 plus negatives scoring 0) and exit.",
    )
    parser.add_argument(
        "--min-coverage",
        type=float,
        default=None,
        metavar="F",
        help="Fail (rc 1) when the case hit rate is below F (0-1).",
    )
    parser.add_argument(
        "--dist",
        action="store_true",
        help="Print a histogram of lexical scores (0.25-wide buckets).",
    )
    parser.add_argument(
        "--sort",
        action="store_true",
        help="Sort rows by score ascending (weakest first, unscored last).",
    )
    parser.add_argument(
        "--top",
        metavar="N",
        type=int,
        default=0,
        help="Print only the N weakest rows (implies score-ascending order).",
    )
    parser.add_argument(
        "--min-covers",
        metavar="N",
        type=int,
        default=0,
        help="Exit 1 when any covers tag has fewer than N cases.",
    )
    parser.add_argument("--quiet", action="store_true", help="Print only the verdict line.")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print only the aggregate stats line (counts, worst/best, margin).",
    )
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
        "--prompts",
        action="store_true",
        help="Print each case id and its prompt text, one per line.",
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
        "--tokens",
        action="store_true",
        help="Show which prompt tokens matched the description per row.",
    )
    parser.add_argument(
        "--unmatched",
        action="store_true",
        help="Show which prompt tokens missed the description per row.",
    )
    parser.add_argument(
        "--desc-tokens",
        action="store_true",
        help="Print the scored description token set (or --desc tokens) and exit.",
    )
    parser.add_argument(
        "--margin",
        metavar="F",
        type=float,
        default=None,
        help="Override the scorer's margin factor (default 1.15) for the verdict.",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="Print the resolved config (paths, margin, scorer) as JSON and exit.",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-evaluate every S seconds, printing one verdict tick per pass.",
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
    parser.add_argument(
        "--report",
        metavar="PATH",
        default="",
        help="Write a markdown eval report (verdict, stats, per-case table) to PATH.",
    )
    args = parser.parse_args(argv)
    if args.env:
        scorer_margin = None
        if VENDORED_SCORER.is_file():
            scorer_margin = getattr(
                _load_scorer(VENDORED_SCORER), "MARGIN", 1.15
            )
        report = {
            "cases": args.cases,
            "cases_exists": Path(args.cases).is_file(),
            "skill": args.skill,
            "scorer": str(VENDORED_SCORER),
            "scorer_exists": VENDORED_SCORER.is_file(),
            "margin": args.margin if args.margin is not None else scorer_margin,
            "margin_default": scorer_margin,
            "desc_override": bool(args.desc),
        }
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
        return 0
    if args.desc_tokens:
        if not VENDORED_SCORER.is_file():
            sys.stderr.write("missing vendored scorer (%s)\n" % VENDORED_SCORER)
            return 2
        scorer = _load_scorer(VENDORED_SCORER)
        try:
            text = args.desc if args.desc else scorer.description_of(args.skill)
        except OSError as exc:
            sys.stderr.write("trigger_eval failed: %s\n" % exc)
            return 2
        for tok in sorted(set(scorer.tokens(text))):
            sys.stdout.write("%s\n" % tok)
        return 0
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
            Path(args.cases),
            Path(args.skill),
            case_id=args.id,
            desc_text=args.desc,
            include_tokens=args.tokens or args.unmatched,
            margin_override=args.margin,
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

    def _covers_counts() -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in result["cases"]:
            for tag in row["covers"] or []:
                counts[tag] = counts.get(tag, 0) + 1
        return counts

    def _covers_ok() -> bool:
        return not args.min_covers or all(
            n >= args.min_covers for n in _covers_counts().values()
        )

    def _coverage_ok() -> bool:
        return args.min_coverage is None or result["coverage"] >= args.min_coverage

    if args.watch and args.watch > 0:
        import time as _time

        try:
            max_ticks = int(os.environ.get("JEV_TRIGGER_WATCH_MAX", "") or 0)
        except ValueError:
            max_ticks = 0
        ticks = 0
        cur = result
        while max_ticks <= 0 or ticks < max_ticks:
            tick = {
                "ts": int(_time.time()),
                "ok": cur["ok"],
                "worst_positive": cur["worst_positive"],
                "best_negative": cur["best_negative"],
                "coverage": cur["coverage"],
            }
            sys.stdout.write(json.dumps(tick) + "\n")
            sys.stdout.flush()
            ticks += 1
            _time.sleep(args.watch)
            try:
                cur = evaluate(
                    Path(args.cases),
                    Path(args.skill),
                    case_id=args.id,
                    desc_text=args.desc,
                    margin_override=args.margin,
                )
            except (OSError, ValueError, KeyError):
                cur = None
            if cur is None:
                sys.stdout.write(
                    json.dumps({"ts": int(_time.time()), "ok": None}) + "\n"
                )
                sys.stdout.flush()
                ticks += 1
                cur = result
        return 0
    if args.out:
        try:
            Path(args.out).write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            sys.stderr.write("--out failed: %s\n" % exc)
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
    if args.report:
        lines = [
            "# trigger eval report",
            "",
            "verdict: **%s**" % ("PASS" if result["ok"] else "FAIL"),
            "",
            "- positives: %d" % result["n_positives"],
            "- negatives: %d" % result["n_negatives"],
            "- coverage: %d/%d (%.0f%%)"
            % (result["hits"], len(result["cases"]), result["coverage"] * 100),
            "- worst positive: %.3f" % result["worst_positive"],
            "- best negative: %.3f" % result["best_negative"],
            "- margin: %.2f" % result["margin"],
        ]
        if args.min_coverage is not None:
            lines.append(
                "- min-coverage gate: %.2f -> %s"
                % (args.min_coverage, "PASS" if _coverage_ok() else "FAIL")
            )
        if args.min_covers:
            lines.append(
                "- min-covers gate: %d -> %s"
                % (args.min_covers, "PASS" if _covers_ok() else "FAIL")
            )
        lines += [
            "",
            "| id | should_trigger | lexical | score | ok |",
            "|---|---|---|---|---|",
        ]
        for row in result["cases"]:
            score = "-" if row["score"] is None else "%.3f" % row["score"]
            lines.append(
                "| %s | %s | %s | %s | %s |"
                % (
                    row["id"],
                    row["should_trigger"],
                    row["lexical"],
                    score,
                    "yes" if row["ok"] else "NO",
                )
            )
        try:
            Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            sys.stderr.write("--report failed: %s\n" % exc)
            return 1
        sys.stderr.write("wrote %s\n" % args.report)
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
        if args.sort or args.top > 0:
            rows = sorted(rows, key=lambda r: (r["score"] is None, r["score"]))
        if args.top > 0:
            rows = rows[: args.top]
        return rows

    if args.covers or args.covers_map:
        counts = _covers_counts()
        id_map: dict[str, list[str]] = {}
        uncovered: list[str] = []
        for row in _rows():
            tags = row["covers"] or []
            for tag in tags:
                id_map.setdefault(tag, []).append(row["id"])
            if not tags:
                uncovered.append(row["id"])
        for tag in sorted(counts):
            low = "  <-- below --min-covers" if (
                args.min_covers and counts[tag] < args.min_covers
            ) else ""
            if args.covers_map:
                sys.stdout.write("%s: %s%s\n" % (tag, ", ".join(id_map[tag]), low))
            else:
                sys.stdout.write("%s %d%s\n" % (tag, counts[tag], low))
        for cid in uncovered:
            sys.stdout.write("uncovered: %s\n" % cid)
        return 0 if (result["ok"] and _covers_ok()) else 1
    if args.dist:
        buckets: dict[int, int] = {}
        unscored = 0
        for row in _rows():
            if row["score"] is None:
                unscored += 1
                continue
            buckets[int(row["score"] / 0.25)] = buckets.get(int(row["score"] / 0.25), 0) + 1
        for b in sorted(buckets):
            sys.stdout.write("%.2f-%.2f %d\n" % (b * 0.25, (b + 1) * 0.25, buckets[b]))
        if unscored:
            sys.stdout.write("unscored %d\n" % unscored)
        return 0 if result["ok"] else 1
    if args.coverage:
        if args.ids:
            uncovered_ids = [
                row["id"]
                for row in result["cases"]
                if not (
                    (
                        row["should_trigger"]
                        and row["score"] is not None
                        and row["score"] > 0
                    )
                    or (
                        not row["should_trigger"]
                        and (not row["lexical"] or not row["score"])
                    )
                )
            ]
            for cid in uncovered_ids:
                sys.stdout.write("%s\n" % cid)
            strict_cov_ok = not args.strict or result["coverage"] >= 1.0
            return 0 if (result["ok"] and _coverage_ok() and strict_cov_ok) else 1
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {
                        "hits": result["hits"],
                        "total": len(result["cases"]),
                        "coverage": result["coverage"],
                        "ok": result["ok"] and _coverage_ok(),
                    }
                )
                + "\n"
            )
        else:
            sys.stdout.write(
                "coverage: %d/%d (%.0f%%)\n"
                % (result["hits"], len(result["cases"]), result["coverage"] * 100)
            )
        strict_cov_ok = not args.strict or result["coverage"] >= 1.0
        return 0 if (result["ok"] and _coverage_ok() and strict_cov_ok) else 1
    if args.ids:
        for row in _rows():
            sys.stdout.write("%s\n" % row["id"])
        return 0 if result["ok"] else 1
    if args.prompts:
        for row in _rows():
            sys.stdout.write("%s: %s\n" % (row["id"], row["prompt"]))
        return 0 if result["ok"] else 1
    if args.csv:
        sys.stdout.write("id,should_trigger,lexical,score,ok\n")
        for row in _rows():
            score = "" if row["score"] is None else "%.3f" % row["score"]
            sys.stdout.write(
                "%s,%s,%s,%s,%s\n"
                % (
                    row["id"],
                    row["should_trigger"],
                    row["lexical"],
                    score,
                    row["ok"],
                )
            )
        return 0 if result["ok"] else 1
    if args.json:
        payload = dict(result)
        payload["cases"] = _rows()
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        if not args.quiet and not args.summary:
            for row in _rows():
                score = "-" if row["score"] is None else "%.3f" % row["score"]
                marker = "" if row["ok"] else "  <-- FAIL"
                line = "%-28s should_trigger=%-5s lexical=%-5s score=%s%s" % (
                    row["id"],
                    row["should_trigger"],
                    row["lexical"],
                    score,
                    marker,
                )
                if args.tokens:
                    line += "  tokens=%s" % ",".join(row["matched"])
                if args.unmatched:
                    line += "  missed=%s" % ",".join(row["unmatched"])
                sys.stdout.write(line + "\n")
        sys.stdout.write(
            "margin: %s (worst positive %.3f vs best negative %.3f x %.2f)\n"
            % (
                "PASS" if result["ok"] else "FAIL",
                result["worst_positive"],
                result["best_negative"],
                result["margin"],
            )
        )
        if args.summary:
            sys.stdout.write(
                "positives=%d negatives=%d\n"
                % (result["n_positives"], result["n_negatives"])
            )
    if args.strict and any(
        not row["ok"] or (not row["should_trigger"] and (row["score"] or 0) > 0)
        for row in result["cases"]
    ):
        return 1
    if not _covers_ok() or not _coverage_ok():
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
