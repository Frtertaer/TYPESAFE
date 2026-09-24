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

import copy
import json
import os
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402
from compact import estimate_tokens  # noqa: E402

SEVERITIES = ("error", "warn", "info")

# The request.json contract (as scaffolded by jev.py / consumed by ask).
# --schema prints this like the other pack scripts: text rows or, with
# --json, the {key: {required, type}} object.
REQUEST_SCHEMA_ROWS = {
    "state": {"required": False, "type": "object|string, session context sent to Jev (32k-token cap)"},
    "questions": {"required": True, "type": "object{qid: question}; qid is the answers key"},
    "irreversible": {"required": False, "type": "bool, marks irreversible actions"},
    "model": {"required": False, "type": "string, overrides the policy model for this ask"},
    "question.type": {"required": True, "type": "choice|noul|score"},
    "question.instructions": {"required": True, "type": "string, the question text"},
    "question.criteria": {"required": False, "type": "object{option: label} for choice, {true,false} for noul, list[label] for score (missing fires J009)"},
}

# --init prints this minimal lint-clean request: one question per type as a
# hand-authoring starting point. (jev.py scaffold instead builds a request
# from policy.json templates.)
INIT_REQUEST = {
    "state": {"task": "short description of what is being decided"},
    "irreversible": False,
    "questions": {
        "pick": {
            "type": "choice",
            "instructions": "Which approach should the next step take?",
            "criteria": {
                "safe": "the smallest change that satisfies the ask",
                "thorough": "a wider change covering adjacent cases",
                "none": "none of these",
            },
        },
        "blocked": {
            "type": "noul",
            "instructions": "Does the task need a secret the session lacks?",
            "criteria": {
                "true": "a required credential is missing; stop and ask",
                "false": "everything needed is present; proceed",
            },
        },
        "risk": {
            "type": "score",
            "instructions": "Rate the blast radius of the chosen approach.",
            "criteria": ["trivial", "moderate", "severe"],
        },
    },
}

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


RULES = {
    "J001": "instructions contain a negation; prefer the positive phrasing",
    "J002": "multiple negations; double negatives cut accuracy",
    "J003": "asks the model to count or do arithmetic; compute it in code instead",
    "J004": "depends on a date or duration comparison; precompute it in state",
    "J005": "depends on a numeric comparison; compare in code or describe bands in criteria",
    "J006": "looks like a multi-hop question; split into atomic questions",
    "J007": "instructions are very short; Jev will not infer intent",
    "J008": "subjective wording with no criteria; define the word in criteria",
    "J009": "missing criteria (noul with none, or undescribed choice options)",
    "J010": "compound yes/no question (and/or); ask one thing per noul",
    "J011": "choice exceeds the option cap; use hierarchical classification",
    "J012": "two options overlap heavily; merge or sharpen the boundary",
    "J013": "score scale has too many levels; use 3 to 5",
    "J014": "true and false criteria are identical",
    "J015": "choice has fewer than two options",
    "J016": "choice has no 'none'/'other' escape; a forced pick returns a wrong answer",
    "J017": "two options carry identical descriptions; Jev has no basis to tell them apart",
    "J018": "option key collides with a hatch id modulo case; hatch matching is exact",
    "J019": "request or question key is outside the schema contract; jev.py ask does not consume it",
    "J020": "state exceeds the 32k-token limit; trim or chunk it first",
    "J021": "state is over 8k tokens; irrelevant state distracts and drops accuracy",
}


def lint_question(
    qid: str,
    q: dict,
    max_options: int = 255,
    hatch: tuple[str, ...] = ("none", "other"),
) -> list[dict]:
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
        if len(options) < 2:
            add(
                "J015",
                "info",
                "choice has %d option(s)" % len(options),
                "A choice needs at least two options to pick between; policy templates may legitimately carry zero (options are injected at scaffold time). Add them to `criteria` or change the type.",
            )
        if len(options) > max_options:
            add(
                "J011",
                "error",
                "%d options exceeds the %d-option limit" % (len(options), max_options),
                "Use hierarchical classification.",
            )
        if len(options) >= 2 and "none" not in options and "other" not in options:
            add(
                "J016",
                "warn",
                "choice has no 'none'/'other' escape option",
                "Without an abstain option Jev must pick something — a forced pick returns a confident wrong answer. Add a `none` criterion.",
            )
        hatch_exact = set(hatch)
        hatch_lower = {h.lower() for h in hatch_exact}
        for key in options:
            if (
                isinstance(key, str)
                and key.lower() in hatch_lower
                and key not in hatch_exact
            ):
                add(
                    "J018",
                    "warn",
                    "option key %r collides with a hatch id modulo case" % key,
                    "Hatch matching is exact — 'None' or 'OTHER' are treated as real options, not the abstain escape. Rename the key or use the exact hatch id.",
                )
        undescribed = [k for k, v in options.items() if not v]
        if len(undescribed) > len(options) / 2:
            add(
                "J009",
                "info",
                "%d of %d options have no description" % (len(undescribed), len(options)),
                "Option descriptions are where domain rules live. Describe each option.",
            )
        seen_meanings: dict[str, str] = {}
        for key, meaning in options.items():
            norm = str(meaning or "").strip().lower()
            if not norm:
                continue
            if norm in seen_meanings:
                add(
                    "J017",
                    "warn",
                    "options %r and %r share the same description" % (seen_meanings[norm], key),
                    "Identical descriptions give Jev no basis to distinguish the options; sharpen one.",
                )
            else:
                seen_meanings[norm] = key
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


def lint_request(
    request: dict,
    max_options: int = 255,
    hatch: tuple[str, ...] = ("none", "other"),
    usage: bool = False,
) -> list[dict]:
    findings: list[dict] = []
    questions = request.get("questions") if isinstance(request, dict) else None
    if usage and isinstance(request, dict):
        request_keys = {k.split(".", 1)[0] for k in REQUEST_SCHEMA_ROWS}
        for key in sorted(set(request) - request_keys):
            findings.append(
                {
                    "rule": "J019",
                    "severity": "info",
                    "qid": "*",
                    "message": "top-level key %r is outside the request contract" % key,
                    "fix": "Remove it or document a consumer; jev.py ask ignores it.",
                }
            )
        question_keys = {
            k.split(".", 1)[1] for k in REQUEST_SCHEMA_ROWS if k.startswith("question.")
        }
        if isinstance(questions, dict):
            for qid, question in questions.items():
                if not isinstance(question, dict):
                    continue
                for key in sorted(set(question) - question_keys):
                    findings.append(
                        {
                            "rule": "J019",
                            "severity": "info",
                            "qid": str(qid),
                            "message": "question key %r is outside the request contract" % key,
                            "fix": "Remove it or document a consumer; jev.py ask ignores it.",
                        }
                    )
    if isinstance(questions, dict):
        for qid, question in questions.items():
            if isinstance(question, dict):
                findings += lint_question(
                    str(qid), question, max_options=max_options, hatch=hatch
                )
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
            q.get("type") == "choice"
            and "J016" in before
            and isinstance(criteria, dict)
        ):
            criteria["none"] = "None of the listed options."
            applied.append("J016")
        if (
            q.get("type") == "noul"
            and "J014" in before
            and isinstance(criteria, dict)
        ):
            criteria["false"] = "The condition does not hold."
            applied.append("J014")
    return applied


def diff_request(a: dict, b: dict) -> list[str]:
    """Diff two request docs: shallow top-level keys plus per-question fields."""
    lines: list[str] = []
    if not isinstance(a, dict):
        a = {}
    if not isinstance(b, dict):
        b = {}
    aa = {k: v for k, v in a.items() if k != "questions"}
    bb = {k: v for k, v in b.items() if k != "questions"}
    for key in sorted(set(aa) | set(bb)):
        if key not in bb:
            lines.append("- %s = %s" % (key, json.dumps(aa[key], sort_keys=True)[:120]))
        elif key not in aa:
            lines.append("+ %s = %s" % (key, json.dumps(bb[key], sort_keys=True)[:120]))
        elif aa[key] != bb[key]:
            lines.append(
                "~ %s: %s -> %s"
                % (
                    key,
                    json.dumps(aa[key], sort_keys=True)[:60],
                    json.dumps(bb[key], sort_keys=True)[:60],
                )
            )
    qa = a.get("questions")
    qb = b.get("questions")
    if isinstance(qa, dict) or isinstance(qb, dict):
        qa = qa if isinstance(qa, dict) else {}
        qb = qb if isinstance(qb, dict) else {}
        for qid in sorted(set(qa) | set(qb)):
            if qid not in qb:
                lines.append("- questions.%s" % qid)
            elif qid not in qa:
                lines.append("+ questions.%s" % qid)
            elif qa[qid] != qb[qid]:
                xa = qa[qid] if isinstance(qa[qid], dict) else {}
                xb = qb[qid] if isinstance(qb[qid], dict) else {}
                for field in sorted(set(xa) | set(xb)):
                    if xa.get(field) != xb.get(field):
                        lines.append(
                            "~ questions.%s.%s: %s -> %s"
                            % (
                                qid,
                                field,
                                json.dumps(xa.get(field), sort_keys=True)[:60],
                                json.dumps(xb.get(field), sort_keys=True)[:60],
                            )
                        )
    return lines


def _atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        _watch.atomic_replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


BASELINE_FIELDS = ("rule", "qid", "message")


def _baseline_key(row: dict) -> tuple:
    return _watch.baseline_key(row, BASELINE_FIELDS)


def _drop_baseline(rows: list, keys: set) -> list:
    return [r for r in rows if _baseline_key(r) not in keys]


def _write_baseline(path: str, rows: list) -> bool:
    """Snapshot findings for later --baseline suppression; True on success."""
    try:
        _atomic_write(
            Path(path), json.dumps({"findings": rows}, indent=2) + "\n"
        )
    except OSError as exc:
        sys.stderr.write(
            "cannot write --baseline-write %s: %s\n" % (path, exc)
        )
        return False
    sys.stderr.write(
        "wrote baseline %s (%d findings)\n" % (path, len(rows))
    )
    return True


USAGE = 'Usage: python question_lint.py [QUESTIONS.json ...] [flags]\nLint Jev question wording (J010 compound-noul sharpening etc.). `-` reads the request JSON from stdin (single file or mixed with paths in multi mode; --fix/--watch need a real path).\nFlags:\n  --strict          exit 1 on warnings too\n  --fix             auto-apply safe fixes in place\n  --dry-run         with --fix: report what would change without writing\n  --schema          print the request.json key contract (--json emits the object)\n  --explain RULE    print the description of one rule id and exit ("-" reads it from stdin)\n  --rules           print every rule id + description (--json emits a list)\n  --severity S[,S...]  only these severities (error|warn|info comma list; JEV_QLINT_SEVERITY)\n  --only R[,R...]     lint only these rule ids (rc 2 on unknown id)\n  --usage           add info findings for keys outside the request contract\n  --env             print the resolved env config JSON (files, severity, strict, quiet, watch_max, watch_secs, watch_quiet; --jq KEY one field, --out PATH writes it)\n  --quiet           print only errors/warnings count\n  --baseline PATH   suppress findings already recorded in PATH ("-" reads it from stdin)\n  --baseline-write PATH  write current findings to PATH for --baseline runs\n  --json            findings as JSON array\n  --md              findings as a Markdown table\n  --csv             findings as CSV rows (--keys picks the columns)\n  --jsonl           findings as one JSON object per line (adds file)\n  --keys a,b        with --jsonl/--csv: keep only these keys in each row / as the columns (rc 2 on empty)\n  --rules           list every rule id + description (with --json/--md)\n  --jq KEY          one dotted-path field of the findings payload\n  --out PATH        append/write the payload to a file (fail-open)\n  --diff PATH       diff this request against another file (per-question +/- and ~ lines; "-" reads it from stdin)\n  --self-test       lint a synthetic compound-noul request; exit 1 when no findings\n  --init            print a minimal lint-clean request.json (one question per type) and exit\n  --help            print this usage and exit\n  --version         print the pack policy version and exit\n  --watch S         re-lint every S seconds emitting tick JSON\n  --watch-max S     stop the watch after S elapsed seconds\n  --max-ticks N     stop the watch after N ticks\n  --fail-fast       stop the watch on the first erroring tick\n  --unchanged-max N stop the watch after N consecutive identical ticks\n  --verdict PATH    write a slim {verdict: pass|fail, ...} JSON ("-" prints it to stdout)\nExit 0 clean/warn, 1 on any error, 2 on bad args.\n'


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    """Standalone CLI: python question_lint.py request.json [--json] [--fix]"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if _watch.maybe_version(argv):
        return 0
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(USAGE)
        return 0
    as_json = "--json" in argv
    as_md = "--md" in argv
    as_csv = "--csv" in argv
    as_jsonl = "--jsonl" in argv
    do_fix = "--fix" in argv
    dry_run = "--dry-run" in argv
    usage = "--usage" in argv
    strict = "--strict" in argv
    quiet = "--quiet" in argv
    if "--schema" in argv:
        if "--json" in argv:
            sys.stdout.write(json.dumps(REQUEST_SCHEMA_ROWS, indent=2) + "\n")
        else:
            for key in REQUEST_SCHEMA_ROWS:
                row = REQUEST_SCHEMA_ROWS[key]
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, row["type"], "required" if row["required"] else "optional")
                )
        return 0
    fail_fast = "--fail-fast" in argv
    severity: set[str] = set()
    if "--severity" in argv:
        idx = argv.index("--severity")
        if idx + 1 >= len(argv):
            sys.stderr.write("--severity needs a value (error|warn|info)\n")
            return 2
        picked = _watch.severity_arg(argv[idx + 1])
        if picked is None:
            sys.stderr.write("bad --severity %r (want comma list of error|warn|info)\n" % argv[idx + 1])
            return 2
        severity = picked
        argv = argv[:idx] + argv[idx + 2 :]
    else:
        env_sev = os.environ.get("JEV_QLINT_SEVERITY", "").strip().lower()
        picked = _watch.severity_arg(env_sev)
        if picked:
            severity = picked
    only: set[str] = set()
    if "--only" in argv:
        idx = argv.index("--only")
        if idx + 1 >= len(argv):
            sys.stderr.write("--only needs a RULE[,RULE...] value\n")
            return 2
        only = {s.strip().upper() for s in argv[idx + 1].split(",") if s.strip()}
        unknown = only - set(RULES)
        if not only or unknown:
            sys.stderr.write(
                "bad --only %r (rules: %s)\n" % (argv[idx + 1], ", ".join(sorted(RULES)))
            )
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    keys_sel = ""
    if "--keys" in argv:
        idx = argv.index("--keys")
        if idx + 1 >= len(argv):
            sys.stderr.write("--keys needs a a,b value\n")
            return 2
        keys_sel = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    keys = [k.strip() for k in keys_sel.split(",") if k.strip()] if keys_sel else []
    if keys_sel and not keys:
        sys.stderr.write("--keys names no fields\n")
        return 2

    def _project(row: dict) -> dict:
        return {k: row.get(k) for k in keys} if keys else row

    unchanged_max = 0
    if "--unchanged-max" in argv:
        idx = argv.index("--unchanged-max")
        if idx + 1 >= len(argv):
            sys.stderr.write("--unchanged-max needs a value (int ticks)\n")
            return 2
        try:
            unchanged_max = int(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --unchanged-max %r\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    out_path = ""
    diff_path = None
    if "--diff" in argv:
        idx = argv.index("--diff")
        if idx + 1 >= len(argv):
            sys.stderr.write("--diff needs a request file to compare against\n")
            return 2
        diff_path = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    if "--rules" in argv:
        argv = [a for a in argv if a != "--rules"]
        if as_json:
            sys.stdout.write(
                json.dumps(
                    [{"rule": k, "description": v} for k, v in sorted(RULES.items())],
                    indent=2,
                )
                + "\n"
            )
        elif as_md:
            _watch.md_table(
                [{"rule": k, "description": v} for k, v in sorted(RULES.items())],
                ["rule", "description"],
            )
        elif as_csv:
            _watch.csv_table(
                [_project({"rule": k, "description": v}) for k, v in sorted(RULES.items())],
                keys or ["rule", "description"],
            )
        else:
            for _rule in sorted(RULES):
                sys.stdout.write("%s: %s\n" % (_rule, RULES[_rule]))
        return 0
    if "--explain" in argv:
        idx = argv.index("--explain")
        if idx + 1 >= len(argv):
            sys.stderr.write("--explain needs a RULE value\n")
            return 2
        rule = _watch.text_arg(argv[idx + 1]).strip().upper()
        if rule not in RULES:
            sys.stderr.write(
                "unknown rule %r (rules: %s)\n" % (rule, ", ".join(sorted(RULES)))
            )
            return 2
        sys.stdout.write("%s: %s\n" % (rule, RULES[rule]))
        return 0
    if "--self-test" in argv:
        probe = {
            "questions": {
                "q": {
                    "type": "noul",
                    "instructions": "Is it done and does it pass?",
                    "criteria": {"true": "yes", "false": "no"},
                }
            }
        }
        found = sorted({f["rule"] for f in lint_request(probe)})
        ok = bool(found)
        if as_json:
            sys.stdout.write(
                json.dumps({"self_test": "ok" if ok else "FAIL", "rules": found})
                + "\n"
            )
        else:
            sys.stdout.write(
                "self-test: %s rules=%s\n" % ("ok" if ok else "FAIL", ",".join(found))
            )
        return 0 if ok else 1
    if "--init" in argv:
        sys.stdout.write(json.dumps(INIT_REQUEST, indent=2) + "\n")
        return 0
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 >= len(argv):
            sys.stderr.write("--out needs a PATH value\n")
            return 2
        out_path = argv[idx + 1].strip()
        argv = argv[:idx] + argv[idx + 2 :]
    watch_seconds = 0.0
    if "--watch" in argv:
        idx = argv.index("--watch")
        if idx + 1 >= len(argv):
            sys.stderr.write("--watch needs a SECONDS value\n")
            return 2
        try:
            watch_seconds = float(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --watch %r (seconds)\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    jq_value = ""
    if "--jq" in argv:
        idx = argv.index("--jq")
        if idx + 1 >= len(argv):
            sys.stderr.write("--jq needs a KEY value\n")
            return 2
        jq_value = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    max_ticks_arg = 0
    if "--max-ticks" in argv:
        idx = argv.index("--max-ticks")
        if idx + 1 >= len(argv):
            sys.stderr.write("--max-ticks needs an N value\n")
            return 2
        try:
            max_ticks_arg = int(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --max-ticks %r (integer)\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    watch_max_arg = 0.0
    if "--watch-max" in argv:
        idx = argv.index("--watch-max")
        if idx + 1 >= len(argv):
            sys.stderr.write("--watch-max needs a SECONDS value\n")
            return 2
        try:
            watch_max_arg = float(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --watch-max %r (seconds)\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    verdict_path = ""
    if "--verdict" in argv:
        idx = argv.index("--verdict")
        if idx + 1 >= len(argv):
            sys.stderr.write("--verdict needs a PATH value\n")
            return 2
        verdict_path = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    baseline_path = ""
    if "--baseline" in argv:
        idx = argv.index("--baseline")
        if idx + 1 >= len(argv):
            sys.stderr.write("--baseline needs a PATH value\n")
            return 2
        baseline_path = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    baseline_write = ""
    if "--baseline-write" in argv:
        idx = argv.index("--baseline-write")
        if idx + 1 >= len(argv):
            sys.stderr.write("--baseline-write needs a PATH value\n")
            return 2
        baseline_write = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    argv = [
        a
        for a in argv
        if a not in ("--json", "--md", "--csv", "--jsonl", "--fix", "--dry-run", "--usage", "--strict", "--quiet", "--fail-fast")
    ]
    if "--env" in argv:
        try:
            env_watch_secs = float(os.environ.get("JEV_QLINT_WATCH_SECS", "") or 0)
        except ValueError:
            env_watch_secs = 0.0
        report = {
            "files": [a for a in argv if not a.startswith("--")],
            "severity": ",".join(sorted(severity)),
            "strict": strict,
            "quiet": quiet,
            "watch_max": _watch.cap("JEV_QLINT_WATCH_MAX", None),
            "watch_secs": env_watch_secs,
            "watch_quiet": _watch.quiet("JEV_QLINT_WATCH_QUIET", quiet),
        }
        if jq_value:
            value, found = _watch.dig(report, jq_value)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (env has: %s)\n"
                    % (jq_value, ", ".join(sorted(report)))
                )
                return 2
            sys.stdout.write(json.dumps(value) + "\n")
            return 0
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if out_path:
            try:
                _atomic_write(Path(out_path), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
        return 0
    argv = [a for a in argv if a != "--env"]
    if not argv:
        sys.stderr.write("usage: question_lint.py FILE... [--json] [--fix] [--strict]\n")
        return 2
    unknown = [a for a in argv if a.startswith("-") and a != "-"]
    if unknown:
        sys.stderr.write("unknown flag(s): %s\n" % ", ".join(unknown))
        return 2
    baseline_keys: set | None = None
    if baseline_path:
        baseline_keys = _watch.load_baseline(baseline_path, BASELINE_FIELDS)
    if len(argv) > 1:
        if watch_seconds > 0 or do_fix or diff_path is not None:
            sys.stderr.write("multiple paths support neither --watch, --fix nor --diff\n")
            return 2
        results = []
        n_suppressed = 0
        snapshot = []
        for arg in argv:
            if arg == "-":
                try:
                    freq = json.loads(sys.stdin.read())
                except ValueError as exc:
                    sys.stderr.write("cannot parse stdin (%s)\n" % exc)
                    return 2
            else:
                fpath = Path(arg)
                try:
                    freq = json.loads(fpath.read_text(encoding="utf-8-sig"))
                except OSError as exc:
                    sys.stderr.write("cannot read %s (%s)\n" % (arg, exc))
                    return 2
                except ValueError as exc:
                    sys.stderr.write("cannot parse %s (%s)\n" % (arg, exc))
                    return 2
            if not isinstance(freq, dict):
                sys.stderr.write("request JSON must be an object (%s)\n" % arg)
                return 2
            ffind = _watch.only_filter(lint_request(freq, usage=usage), only)
            if baseline_write:
                snapshot.extend({"file": arg, **f} for f in ffind)
            if baseline_keys is not None:
                kept = _drop_baseline(ffind, baseline_keys)
                n_suppressed += len(ffind) - len(kept)
                ffind = kept
            ferr = sum(1 for f in ffind if f["severity"] == "error")
            fshown = [
                f
                for f in ffind
                if (not severity or f["severity"] in severity)
                and (not quiet or f["severity"] == "error")
            ]
            results.append(
                {
                    "path": "<stdin>" if arg == "-" else arg,
                    "findings": fshown,
                    "errors": ferr,
                    "total": len(ffind),
                }
            )
        if out_path:
            try:
                _atomic_write(Path(out_path),
                    json.dumps(results, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
                return 1
        if jq_value:
            value, found = _watch.dig(results, jq_value)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (payload is a %d-file list)\n"
                    % (jq_value, len(results))
                )
                return 2
            sys.stdout.write(json.dumps(value) + "\n")
            return 0
        if as_json:
            sys.stdout.write(json.dumps(results, indent=2) + "\n")
        elif as_jsonl:
            for res in results:
                for f in res["findings"]:
                    sys.stdout.write(
                        json.dumps(_project({"file": res["path"], **f}), ensure_ascii=False) + "\n"
                    )
        elif as_md or as_csv:
            md_rows = [
                {**f, "path": res["path"]}
                for res in results
                for f in res["findings"]
            ]
            if as_md:
                _watch.md_table(md_rows, ["severity", "rule", "path", "qid", "message"])
            else:
                _watch.csv_table(
                    [_project(r) for r in md_rows],
                    keys or ["severity", "rule", "path", "qid", "message"],
                )
        else:
            for res in results:
                sys.stdout.write("%s:\n" % res["path"])
                for f in res["findings"]:
                    sys.stdout.write(format_finding(f) + "\n")
                sys.stdout.write("  %d error(s) of %d finding(s)\n" % (res["errors"], res["total"]))
        if baseline_write and not _write_baseline(baseline_write, snapshot):
            return 1
        if n_suppressed:
            sys.stderr.write(
                "baseline: suppressed %d known finding(s)\n" % n_suppressed
            )
        any_err = any(r["errors"] for r in results)
        any_find = any(r["total"] for r in results)
        return 1 if any_err or (strict and any_find) else 0
    if argv[0] == "-":
        if do_fix or watch_seconds > 0 or diff_path is not None:
            sys.stderr.write("- (stdin) supports neither --fix, --watch nor --diff\n")
            return 2
        text = sys.stdin.read()
        try:
            request = json.loads(text)
        except ValueError as exc:
            sys.stderr.write("cannot parse stdin (%s)\n" % exc)
            return 2
    else:
        try:
            text = Path(argv[0]).read_text(encoding="utf-8-sig")
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
    if diff_path is not None:
        try:
            other = json.loads(
                sys.stdin.read()
                if diff_path == "-"
                else Path(diff_path).read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError) as exc:
            sys.stderr.write("cannot read --diff %s (%s)\n" % (diff_path, exc))
            return 2
        if not isinstance(other, dict):
            sys.stderr.write("--diff file must contain a JSON object\n")
            return 2
        lines = diff_request(other, request)
        sys.stdout.write("diff %s -> %s\n" % (diff_path, argv[0]))
        for line in lines:
            sys.stdout.write(line + "\n")
        sys.stdout.write("%d difference(s)\n" % len(lines))
        return 0
    if watch_seconds > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_QLINT_WATCH_MAX", max_ticks_arg)
        ticks = 0
        dead = _watch.deadline("JEV_QLINT_WATCH_SECS", watch_max_arg)
        tick: dict = {}
        verdict_ok = True

        def _write_verdict(rc_now: int) -> bool:
            return _watch.write_verdict(
                verdict_path,
                {
                    "verdict": "fail" if rc_now else "pass",
                    "ticks": ticks,
                    "findings": tick.get("findings", 0),
                    "errors": tick.get("errors", 0),
                    "warnings": tick.get("warnings", 0),
                    "infos": tick.get("infos", 0),
                    "suppressed": tick.get("suppressed", 0),
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        watch_t0 = _time.time()
        prev_tick: dict | None = None
        unchanged = 0
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            current = _watch.only_filter(lint_request(request, usage=usage), only)
            pre_drop = len(current)
            if baseline_keys is not None:
                current = _drop_baseline(current, baseline_keys)
            tick = {
                "ts": int(_time.time()),
                "findings": len(current),
                "suppressed": pre_drop - len(current),
                "errors": sum(1 for f in current if f["severity"] == "error"),
                "warnings": sum(1 for f in current if f["severity"] == "warn"),
                "infos": sum(1 for f in current if f["severity"] == "info"),
            }
            _watch.emit_or_jq(tick, jq_value, out_path, quiet=_watch.quiet("JEV_QLINT_WATCH_QUIET", quiet), bad=tick["errors"] or (strict and tick["findings"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d findings=%d errors=%d\n"
                % (ticks, tick["findings"], tick["errors"])
            )
            if verdict_path and verdict_ok:
                rc_now = 1 if (tick["errors"] or (strict and tick["findings"])) else 0
                if not _write_verdict(rc_now):
                    verdict_ok = False  # warn once, stop retrying
            if fail_fast and (tick["errors"] or (strict and tick["findings"])):
                break
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if unchanged_max and unchanged >= unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(watch_seconds)
            try:
                fresh = json.loads(Path(argv[0]).read_text(encoding="utf-8-sig"))
                if isinstance(fresh, dict):
                    request = fresh
            except (OSError, ValueError):
                pass
        rc = 1 if (tick.get("errors", 0) or (strict and tick.get("findings", 0))) else 0
        if verdict_path and verdict_ok and not _write_verdict(rc):
            return 1
        return rc
    if do_fix:
        target = copy.deepcopy(request) if dry_run else request
        applied = apply_fixes(target)
        if applied and not dry_run:
            _atomic_write(Path(argv[0]), json.dumps(request, indent=2, ensure_ascii=False) + "\n")
        for rule in applied:
            sys.stderr.write("%s %s\n" % ("would fix" if dry_run else "fixed", rule))
    findings = _watch.only_filter(lint_request(request, usage=usage), only)
    if baseline_write and not _write_baseline(
        baseline_write, [{"file": argv[0], **f} for f in findings]
    ):
        return 1
    n_suppressed = 0
    if baseline_keys is not None:
        kept = _drop_baseline(findings, baseline_keys)
        n_suppressed = len(findings) - len(kept)
        findings = kept
        if n_suppressed:
            sys.stderr.write(
                "baseline: suppressed %d known finding(s)\n" % n_suppressed
            )
    shown = [f for f in findings if not severity or f["severity"] in severity]
    if out_path:
        try:
            _atomic_write(
                Path(out_path),
                json.dumps({"findings": shown}, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d finding(s) to %s\n" % (len(shown), out_path))
    rc = 1 if any(f["severity"] == "error" for f in findings) or (strict and findings) else 0
    if verdict_path:
        if not _watch.write_verdict(
            verdict_path,
            {
                "verdict": "fail" if rc else "pass",
                "ticks": 1,
                "findings": len(findings),
                "suppressed": n_suppressed,
                "errors": sum(1 for f in findings if f["severity"] == "error"),
                "warnings": sum(1 for f in findings if f["severity"] == "warn"),
                "infos": sum(1 for f in findings if f["severity"] == "info"),
            },
        ):
            return 1
    if jq_value:
        value, found = _watch.dig({"findings": shown}, jq_value)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: findings)\n" % jq_value
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
        return 0
    if as_json:
        sys.stdout.write(json.dumps({"findings": shown}, indent=2) + "\n")
    elif as_jsonl:
        file_label = "<stdin>" if argv[0] == "-" else argv[0]
        for f in shown:
            if quiet and f["severity"] != "error":
                continue
            sys.stdout.write(
                json.dumps(_project({"file": file_label, **f}), ensure_ascii=False) + "\n"
            )
    elif as_md:
        _watch.md_table(
            [f for f in shown if not quiet or f["severity"] == "error"],
            ["severity", "rule", "qid", "message", "fix"],
        )
    elif as_csv:
        _watch.csv_table(
            [
                _project(f)
                for f in shown
                if not quiet or f["severity"] == "error"
            ],
            keys or ["severity", "rule", "qid", "message", "fix"],
        )
    else:
        for f in shown:
            if quiet and f["severity"] != "error":
                continue
            sys.stdout.write(format_finding(f) + "\n")
        if not quiet:
            sys.stdout.write("lint: %d finding(s)\n" % len(shown))
    return rc


if __name__ == "__main__":
    _watch.exit_safely(main())
