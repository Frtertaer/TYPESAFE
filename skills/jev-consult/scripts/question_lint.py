#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Static checks for Jev question wording (port of jevcal lint.py, MIT).

Source: https://docs.typesafe.ai/model-jaggedness/jev-1.13
Findings: {"rule", "severity", "qid", "message", "fix"},
severity in ("error", "warn", "info").

Sharpened vs upstream: J010 (compound noul) fires only on `and/or` or a
conjunction followed by a second auxiliary verb ("is it done and does it
pass?"). A plain noun list like "skills, plugins, or MCP servers" is one
question and does not fire.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from compact import estimate_tokens  # noqa: E402

SEVERITIES = ("error", "warn", "info")

NEGATION = re.compile(r"\b(not|never|no longer|isn't|aren't|doesn't|don't|didn't|won't|cannot|can't|without|except|unless|neither|nor)\b", re.I)
ARITHMETIC = re.compile(r"\b(how many|count of|number of|at least \d+|at most \d+|more than \d+|fewer than \d+|less than \d+|sum of|total of|average|percent(age)?)\b", re.I)
DATETIME = re.compile(r"\b(within the (last|past|next)|older than|newer than|earlier than|later than|expired?|overdue|past due|(before|after|since|until) \d|\d+\s*(minutes?|hours?|days?|weeks?|months?|years?))\b", re.I)
NUMERIC = re.compile(r"(\b(greater than|less than|exceeds?|at least|at most|above|below|over|under)\s+[$€£]?\d|[<>]=?\s*[$€£]?\d)", re.I)
MULTI_HOP = re.compile(r"\b(whose|of the \w+ of|which of .+ that|if .+ then .+ otherwise)\b", re.I)
VAGUE = re.compile(r"\b(good|bad|appropriate|relevant|quality|suitable|acceptable|reasonable|important|interesting)\b", re.I)
# Second auxiliary verb after a conjunction = two questions glued together.
COMPOUND_NOUL = re.compile(r"\band/or\b|\b(and|or)\s+(is|are|does|do|did|should|can|could|has|have|will|would)\b", re.I)


def _text_of(question: dict) -> str:
    parts = [str(question.get("instructions") or "")]
    criteria = question.get("criteria")
    if isinstance(criteria, dict):
        parts += [str(v) for v in criteria.values() if v]
    elif isinstance(criteria, list):
        parts += [str(v) for v in criteria]
    return " \n ".join(parts)


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]{3,}", text.lower())}


def lint_question(qid: str, q: dict, max_options: int = 255) -> list[dict]:
    text = _text_of(q)
    instructions = str(q.get("instructions") or "")
    criteria = q.get("criteria")
    qtype = q.get("type")
    findings: list[dict] = []

    def add(rule: str, severity: str, message: str, fix: str) -> None:
        findings.append(
            {"rule": rule, "severity": severity, "qid": qid, "message": message, "fix": fix}
        )

    negations = NEGATION.findall(text)
    if len(negations) >= 2:
        add(
            "J002",
            "error",
            "multiple negations (%s)" % ", ".join(sorted({n.lower() for n in negations})),
            "Jev reads negations literally and double negatives cut accuracy. Ask the positive version and flip the answer in code.",
        )
    elif negations:
        add(
            "J001",
            "warn",
            "negation (%r)" % negations[0].lower(),
            "P(noul) and 1 - P(negated noul) are not interchangeable on Jev. Prefer the positive phrasing.",
        )
    if ARITHMETIC.search(text):
        add(
            "J003",
            "warn",
            "asks the model to count or do arithmetic",
            "Jev is not a calculator. Compute the number in code and put the result in the state.",
        )
    if DATETIME.search(text):
        add(
            "J004",
            "warn",
            "depends on a date or duration comparison",
            "Dates are read as text. Precompute the comparison (e.g. days_overdue: 12) and ask about that field.",
        )
    if NUMERIC.search(text):
        add(
            "J005",
            "warn",
            "depends on a numeric comparison",
            "Do the comparison in code, or describe the bands in words inside the criteria.",
        )
    if MULTI_HOP.search(text):
        add(
            "J006",
            "info",
            "looks like a multi-hop question",
            "Indirection lowers accuracy. Split into atomic questions and combine the answers in code; extra questions are nearly free.",
        )
    if len(instructions.split()) < 4:
        add(
            "J007",
            "warn",
            "instructions are very short",
            "Jev answers the question as written and will not infer intent. Say exactly what should count.",
        )
    if VAGUE.search(instructions) and not criteria:
        add(
            "J008",
            "info",
            "subjective wording with no criteria",
            "Define what the subjective word means in `criteria`, including the boundary cases.",
        )
    if qtype == "noul":
        if COMPOUND_NOUL.search(instructions):
            add(
                "J010",
                "warn",
                "compound yes/no question (and / or)",
                "Ask one thing per noul. Several nouls in one request cost almost nothing extra.",
            )
        if not criteria:
            add(
                "J009",
                "info",
                "noul has no criteria",
                "Describe what a yes and a no mean, especially near the boundary.",
            )
        elif isinstance(criteria, dict) and bool(
            str(criteria.get("true", "")).strip()
        ) != bool(str(criteria.get("false", "")).strip()):
            add(
                "J009",
                "info",
                "noul criteria describes only one side",
                "Describe what a yes and a no mean, especially near the boundary.",
            )
        elif (
            isinstance(criteria, dict)
            and str(criteria.get("true", "")).strip()
            and str(criteria.get("true", "")).strip().lower()
            == str(criteria.get("false", "")).strip().lower()
        ):
            add(
                "J014",
                "error",
                "true and false criteria are identical",
                "Contradictory guidance confuses the model. Make them distinct.",
            )
    if qtype == "choice":
        options = criteria if isinstance(criteria, dict) else {}
        if len(options) > max_options:
            add(
                "J011",
                "error",
                "%d options exceeds the %d-option limit" % (len(options), max_options),
                "Use hierarchical classification.",
            )
        undescribed = [k for k, v in options.items() if not v]
        if len(undescribed) > len(options) / 2:
            add(
                "J009",
                "info",
                "%d of %d options have no description" % (len(undescribed), len(options)),
                "Option descriptions are where domain rules live. Describe each option.",
            )
        keys = list(options) if len(options) <= 512 else []
        for i, first in enumerate(keys):
            for second in keys[i + 1:]:
                if not options[first] or not options[second]:
                    continue
                a = _tokens("%s %s" % (first, options[first] or ""))
                b = _tokens("%s %s" % (second, options[second] or ""))
                if a and b and len(a & b) / len(a | b) > 0.6:
                    add(
                        "J012",
                        "warn",
                        "options %r and %r overlap heavily" % (first, second),
                        "Overlapping options flatten the distribution and tank confidence. Merge them or sharpen the boundary.",
                    )
    if qtype == "score" and isinstance(criteria, list) and len(criteria) > 7:
        add(
            "J013",
            "warn",
            "%d score levels" % len(criteria),
            "Fine-grained scales produce low confidence. Use 3 to 5 levels with clear descriptions.",
        )
    return findings


def lint_state(state) -> list[dict]:
    text = state if isinstance(state, str) else str(state)
    size = estimate_tokens(text)
    findings: list[dict] = []
    if size > 32_000:
        findings.append(
            {
                "rule": "J020",
                "severity": "error",
                "qid": "*",
                "message": "state is ~%s tokens, above the 32k state limit" % format(size, ","),
                "fix": "Trim or chunk the state before sending it.",
            }
        )
    elif size > 8_000:
        findings.append(
            {
                "rule": "J021",
                "severity": "warn",
                "qid": "*",
                "message": "state is ~%s tokens" % format(size, ","),
                "fix": "Irrelevant state is a distractor and accuracy falls as it grows. Send only what the question needs.",
            }
        )
    return findings


def lint_request(request: dict, max_options: int = 255) -> list[dict]:
    findings: list[dict] = []
    questions = request.get("questions") if isinstance(request, dict) else None
    if isinstance(questions, dict):
        for qid, question in questions.items():
            if isinstance(question, dict):
                findings += lint_question(str(qid), question, max_options=max_options)
    if isinstance(request, dict) and "state" in request:
        state = request["state"]
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        findings += lint_state(text)
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return findings


def format_finding(f: dict) -> str:
    sev = {"error": "error", "warn": "warn ", "info": "info "}.get(f["severity"], f["severity"])
    return "%s %s %s: %s — %s" % (
        sev,
        f["rule"],
        f["qid"],
        f["message"],
        f["fix"],
    )


_FIXABLE = ("J009", "J014")


def apply_fixes(request: dict) -> list[str]:
    """Apply mechanical fixes to the request in place; returns rule ids fixed."""
    questions = request.get("questions")
    if not isinstance(questions, dict):
        return []
    applied: list[str] = []
    for qid, q in questions.items():
        if not isinstance(q, dict):
            continue
        before = {f["rule"] for f in lint_question(qid, q)}
        criteria = q.get("criteria")
        if q.get("type") == "choice" and "J009" in before and isinstance(criteria, dict):
            for key in criteria:
                if not criteria[key]:
                    criteria[key] = key
            applied.append("J009")
        if (
            q.get("type") == "noul"
            and "J014" in before
            and isinstance(criteria, dict)
        ):
            criteria["false"] = "The condition does not hold."
            applied.append("J014")
    return applied


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI: python question_lint.py request.json [--json] [--fix]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    do_fix = "--fix" in argv
    strict = "--strict" in argv
    quiet = "--quiet" in argv
    severity = ""
    if "--severity" in argv:
        idx = argv.index("--severity")
        if idx + 1 >= len(argv):
            sys.stderr.write("--severity needs a value (error|warn|info)\n")
            return 2
        severity = argv[idx + 1].strip().lower()
        if severity not in SEVERITIES:
            sys.stderr.write("bad --severity %r (want error|warn|info)\n" % severity)
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    argv = [a for a in argv if a not in ("--json", "--fix", "--strict", "--quiet")]
    if not argv:
        sys.stderr.write("usage: question_lint.py FILE [--json] [--fix] [--strict]\n")
        return 2
    try:
        text = Path(argv[0]).read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write("cannot read %s (%s)\n" % (argv[0], exc))
        return 2
    try:
        request = json.loads(text)
    except ValueError as exc:
        sys.stderr.write("cannot parse %s (%s)\n" % (argv[0], exc))
        return 2
    if not isinstance(request, dict):
        sys.stderr.write("request JSON must be an object\n")
        return 2
    if do_fix:
        applied = apply_fixes(request)
        if applied:
            text = json.dumps(request, indent=2, ensure_ascii=False) + "\n"
            fd, tmp = tempfile.mkstemp(prefix=Path(argv[0]).name + ".", dir=str(Path(argv[0]).resolve().parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                    out.write(text)
                os.replace(tmp, argv[0])
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            for rule in applied:
                sys.stderr.write("fixed %s\n" % rule)
    findings = lint_request(request)
    shown = [f for f in findings if not severity or f["severity"] == severity]
    if as_json:
        sys.stdout.write(json.dumps({"findings": shown}, indent=2) + "\n")
    else:
        for f in shown:
            if quiet and f["severity"] != "error":
                continue
            sys.stdout.write(format_finding(f) + "\n")
        if not quiet:
            sys.stdout.write("lint: %d finding(s)\n" % len(shown))
    if any(f["severity"] == "error" for f in findings):
        return 1
    return 1 if strict and findings else 0


if __name__ == "__main__":
    sys.exit(main())
