#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""trigger_lint.py - lint a trigger-cases fixture file for schema sanity.

Usage: python trigger_lint.py [CASES.json] [--json] [--quiet]
Findings: {"rule", "severity", "id", "message"}. Exit 0 clean/warn,
1 on any error, 2 on bad args or an unreadable file.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_CASES = SCRIPTS.parents[2] / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
DEFAULT_POLICY = SCRIPTS.parent / "policy.json"
ID_RE = re.compile(r"^(pos|neg)-[a-z0-9][a-z0-9-]*$")
SEVERITIES = ("error", "warn", "info")


def _must_ask_kinds(policy_path: Path = DEFAULT_POLICY) -> set[str] | None:
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    kinds = data.get("must_ask") if isinstance(data, dict) else None
    if not isinstance(kinds, list):
        return None
    return {str(k) for k in kinds}


def lint_cases(path: Path, policy_path: Path = DEFAULT_POLICY) -> list[dict]:
    findings: list[dict] = []

    def add(rule: str, severity: str, cid: str, message: str) -> None:
        findings.append({"rule": rule, "severity": severity, "id": cid, "message": message})

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        add("T001", "error", "-", "cannot parse %s: %s" % (path, exc))
        return findings
    if not isinstance(data, dict):
        add("T001", "error", "-", "cases file must contain one JSON object")
        return findings
    cases = data.get("cases")
    if not isinstance(cases, list):
        add("T002", "error", "-", "missing 'cases' list")
        return findings
    seen: set[str] = set()
    must_ask = _must_ask_kinds(policy_path)
    n_pos = 0
    for index, case in enumerate(cases):
        cid = "case[%d]" % index
        if not isinstance(case, dict):
            add("T003", "error", cid, "case is not an object")
            continue
        cid = str(case.get("id") or cid)
        for key in ("id", "prompt", "should_trigger"):
            if key not in case:
                add("T003", "error", cid, "missing key %r" % key)
        if cid in seen:
            add("T004", "error", cid, "duplicate case id")
        seen.add(cid)
        if isinstance(case.get("id"), str) and not ID_RE.match(case["id"]):
            add("T005", "warn", cid, "id %r is not pos-/neg- prefixed lowercase-hyphen" % case["id"])
        prompt = case.get("prompt")
        if "prompt" in case and (not isinstance(prompt, str) or len(prompt.strip()) < 8):
            add("T006", "warn", cid, "prompt is missing or shorter than 8 chars")
        should = case.get("should_trigger")
        if "should_trigger" in case and not isinstance(should, bool):
            add("T007", "error", cid, "should_trigger must be a boolean")
        if "lexical" in case and not isinstance(case["lexical"], bool):
            add("T007", "error", cid, "lexical must be a boolean when present")
        covers = case.get("covers")
        if covers is not None and not isinstance(covers, list):
            add("T007", "error", cid, "covers must be a list")
            covers = None
        if isinstance(covers, list):
            for kind in covers:
                if not isinstance(kind, str):
                    add("T007", "error", cid, "covers entries must be strings")
                elif must_ask is not None and kind not in must_ask:
                    add("T008", "error", cid, "covers kind %r not in policy must_ask" % kind)
        if should is True:
            n_pos += 1
            if not covers:
                add("T009", "warn", cid, "positive case declares no covers kind")
    if cases and n_pos == 0:
        add("T010", "warn", "-", "no positive cases: the margin eval cannot pass")
    return findings


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
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
    else:
        env_sev = os.environ.get("JEV_TLINT_SEVERITY", "").strip().lower()
        if env_sev in SEVERITIES:
            severity = env_sev
    out_path = ""
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 >= len(argv):
            sys.stderr.write("--out needs a PATH value\n")
            return 2
        out_path = argv[idx + 1].strip()
        argv = argv[:idx] + argv[idx + 2 :]
    argv = [a for a in argv if a not in ("--json", "--quiet")]
    if len(argv) > 1:
        sys.stderr.write("usage: trigger_lint.py [CASES.json] [--json] [--quiet] [--severity L] [--out PATH]\n")
        return 2
    path = Path(argv[0]) if argv else DEFAULT_CASES
    findings = lint_cases(path)
    shown = [
        f
        for f in findings
        if (not severity or f["severity"] == severity)
        and (not quiet or f["severity"] == "error")
    ]
    n_err = sum(1 for f in findings if f["severity"] == "error")
    n_warn = sum(1 for f in findings if f["severity"] == "warn")
    n_info = sum(1 for f in findings if f["severity"] == "info")
    if out_path:
        try:
            Path(out_path).write_text(
                json.dumps(
                    {
                        "path": str(path),
                        "findings": shown,
                        "errors": n_err,
                        "warnings": n_warn,
                        "infos": n_info,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d finding(s) to %s\n" % (len(shown), out_path))
    if as_json:
        sys.stdout.write(
            json.dumps(
                {
                    "path": str(path),
                    "findings": shown,
                    "errors": n_err,
                    "warnings": n_warn,
                    "infos": n_info,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        for f in shown:
            sys.stdout.write(
                "%s %s %s: %s\n" % (f["severity"].upper(), f["rule"], f["id"], f["message"])
            )
        sys.stdout.write(
            "trigger_lint: %d error(s), %d warning(s), %d info\n" % (n_err, n_warn, n_info)
        )
    return 1 if n_err else 0


if __name__ == "__main__":
    raise SystemExit(main())
