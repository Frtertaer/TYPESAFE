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
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _watch  # noqa: E402

DEFAULT_CASES = SCRIPTS.parents[2] / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
DEFAULT_POLICY = SCRIPTS.parent / "policy.json"
ID_RE = re.compile(r"^(pos|neg)-[a-z0-9][a-z0-9-]*$")
SEVERITIES = ("error", "warn", "info")


def _must_ask_kinds(policy_path: Path = DEFAULT_POLICY) -> set[str] | None:
    try:
        data = json.loads(policy_path.read_text(encoding="utf-8-sig"))
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
        data = json.loads(path.read_text(encoding="utf-8-sig"))
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
    if must_ask is not None:
        covered = {
            kind
            for case in cases
            if isinstance(case, dict)
            and case.get("should_trigger") is True
            and isinstance(case.get("covers"), list)
            for kind in case["covers"]
            if isinstance(kind, str)
        }
        for kind in sorted(must_ask - covered):
            add("T011", "warn", "-", "must_ask kind %r has no positive coverage case" % kind)
    return findings


def fix_cases(data: dict) -> list[str]:
    """Apply mechanical fixes to a cases file in place; returns rule ids fixed."""
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list):
        return []
    applied: list[str] = []
    used = {
        str(c.get("id"))
        for c in cases
        if isinstance(c, dict) and isinstance(c.get("id"), str)
    }
    for case in cases:
        if not isinstance(case, dict):
            continue
        cid = case.get("id")
        if isinstance(cid, str) and not ID_RE.match(cid):
            want = "pos" if case.get("should_trigger") is True else "neg"
            slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", cid.lower())).strip("-")
            slug = re.sub(r"^(pos|neg)-", "", slug)
            if slug:
                new_id = "%s-%s" % (want, slug)
                n = 2
                while new_id in used and new_id != cid:
                    new_id = "%s-%s-%d" % (want, slug, n)
                    n += 1
                if new_id != cid:
                    used.discard(cid)
                    used.add(new_id)
                    case["id"] = new_id
                    applied.append("T005")
        covers = case.get("covers")
        if isinstance(covers, list):
            cleaned: list = []
            changed = False
            for kind in covers:
                if not isinstance(kind, str) or kind in cleaned:
                    changed = True
                    continue
                cleaned.append(kind)
            if changed:
                case["covers"] = cleaned
                applied.append("T007")
    return applied


RULES = {
    "T001": "cases file cannot be parsed or is not a JSON object",
    "T002": "missing 'cases' list",
    "T003": "case is not an object or is missing id/prompt/should_trigger",
    "T004": "duplicate case id",
    "T005": "id is not pos-/neg- prefixed lowercase-hyphen",
    "T006": "prompt is missing or shorter than 8 chars",
    "T007": "field has the wrong type (should_trigger/lexical must be bool, covers a list of strings)",
    "T008": "covers kind is not in policy must_ask",
    "T009": "positive case declares no covers kind",
    "T010": "no positive cases; the margin eval cannot pass",
    "T011": "a must_ask kind has no positive coverage case",
}


def _atomic_write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


USAGE = 'Usage: python trigger_lint.py [CASES.json ...] [flags]\nLint a trigger-cases fixture file for schema sanity.\nFlags:\n  --policy PATH     lint cases against a specific policy.json\n  --strict          exit 1 on warnings too\n  --fix             auto-apply safe fixes in place\n  --dry-run         with --fix: print the diff, write nothing\n  --explain RULE    print the description of one rule id and exit\n  --rules           print every rule id + description (--json emits a list)\n  --severity S      preset severity floor (error|warn|info; JEV_TLINT_SEVERITY)\n  --env             print the resolved env config JSON (files, policy, severity, strict, quiet, watch_max, watch_secs, watch_quiet; --jq KEY one field, --out PATH writes it)\n  --quiet           print only errors/warnings count\n  --json            findings as JSON array\n  --jq KEY          one dotted-path field of the findings payload\n  --out PATH        append/write the payload to a file (fail-open)\n  --self-test       lint a synthetic known-bad cases file; exit 1 when no findings\n  --help            print this usage and exit\n  --version         print the pack policy version and exit\n  --watch S         re-lint every S seconds emitting tick JSON\n  --watch-max S     stop the watch after S elapsed seconds\n  --max-ticks N     stop the watch after N ticks\n  --fail-fast       stop the watch on the first erroring tick\n  --verdict PATH    write a slim {verdict: pass|fail, ...} JSON\nExit 0 clean/warn, 1 on any error, 2 on bad args or unreadable file.\n'


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if _watch.maybe_version(argv):
        return 0
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(USAGE)
        return 0
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
    if "--explain" in argv:
        idx = argv.index("--explain")
        if idx + 1 >= len(argv):
            sys.stderr.write("--explain needs a RULE value\n")
            return 2
        rule = argv[idx + 1].strip().upper()
        if rule not in RULES:
            sys.stderr.write(
                "unknown rule %r (rules: %s)\n" % (rule, ", ".join(sorted(RULES)))
            )
            return 2
        sys.stdout.write("%s: %s\n" % (rule, RULES[rule]))
        return 0
    if "--self-test" in argv:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cases.json"
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "cases": [
                                {"id": "dup", "should_trigger": True},
                                {"id": "dup"},
                            ]
                        }
                    )
                )
            findings = lint_cases(p)
        rules = sorted({f["rule"] for f in findings})
        ok = bool(rules)
        if as_json:
            sys.stdout.write(
                json.dumps({"self_test": "ok" if ok else "FAIL", "rules": rules})
                + "\n"
            )
        else:
            sys.stdout.write(
                "self-test: %s rules=%s\n" % ("ok" if ok else "FAIL", ",".join(rules))
            )
        return 0 if ok else 1
    if "--rules" in argv:
        if "--json" in argv:
            sys.stdout.write(
                json.dumps(
                    [{"rule": r, "description": RULES[r]} for r in sorted(RULES)],
                    indent=2,
                )
                + "\n"
            )
        else:
            for r in sorted(RULES):
                sys.stdout.write("%s: %s\n" % (r, RULES[r]))
        return 0
    out_path = ""
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 >= len(argv):
            sys.stderr.write("--out needs a PATH value\n")
            return 2
        out_path = argv[idx + 1].strip()
        argv = argv[:idx] + argv[idx + 2 :]
    policy_path = DEFAULT_POLICY
    if "--policy" in argv:
        idx = argv.index("--policy")
        if idx + 1 >= len(argv):
            sys.stderr.write("--policy needs a PATH value\n")
            return 2
        policy_path = Path(argv[idx + 1].strip())
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
        i = argv.index("--jq")
        if i + 1 >= len(argv):
            sys.stderr.write("--jq needs a KEY value\n")
            return 2
        jq_value = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
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
    strict = "--strict" in argv
    do_fix = "--fix" in argv
    dry_run = "--dry-run" in argv
    fail_fast = "--fail-fast" in argv
    argv = [
        a
        for a in argv
        if a not in ("--json", "--quiet", "--strict", "--fix", "--dry-run", "--fail-fast")
    ]
    if "--env" in argv:
        try:
            env_watch_secs = float(os.environ.get("JEV_TLINT_WATCH_SECS", "") or 0)
        except ValueError:
            env_watch_secs = 0.0
        report = {
            "files": [a for a in argv if not a.startswith("--")],
            "policy": str(policy_path),
            "severity": severity,
            "strict": strict,
            "quiet": quiet,
            "watch_max": _watch.cap("JEV_TLINT_WATCH_MAX", None),
            "watch_secs": env_watch_secs,
            "watch_quiet": _watch.quiet("JEV_TLINT_WATCH_QUIET", quiet),
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
    unknown = [a for a in argv if a.startswith("-")]
    if unknown:
        sys.stderr.write("unknown flag(s): %s\n" % ", ".join(unknown))
        return 2
    if len(argv) > 1:
        if watch_seconds > 0 or do_fix:
            sys.stderr.write("multiple paths support neither --watch nor --fix\n")
            return 2
        results = []
        for arg in argv:
            fpath = Path(arg)
            frows = lint_cases(fpath, policy_path=policy_path)
            ferr = sum(1 for f in frows if f["severity"] == "error")
            fwarn = sum(1 for f in frows if f["severity"] == "warn")
            finfo = sum(1 for f in frows if f["severity"] == "info")
            fshown = [
                f
                for f in frows
                if (not severity or f["severity"] == severity)
                and (not quiet or f["severity"] == "error")
            ]
            results.append(
                {
                    "path": str(fpath),
                    "findings": fshown,
                    "errors": ferr,
                    "warnings": fwarn,
                    "infos": finfo,
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
        else:
            for res in results:
                sys.stdout.write("%s:\n" % res["path"])
                for f in res["findings"]:
                    sys.stdout.write(
                        "%s %s %s: %s\n"
                        % (f["severity"].upper(), f["rule"], f["id"], f["message"])
                    )
                sys.stdout.write(
                    "  %d error(s), %d warning(s), %d info\n"
                    % (res["errors"], res["warnings"], res["infos"])
                )
        any_err = any(r["errors"] for r in results)
        any_find = any(
            (r["errors"] or r["warnings"] or r["infos"]) for r in results
        )
        return 1 if any_err or (strict and any_find) else 0
    path = Path(argv[0]) if argv else DEFAULT_CASES
    if watch_seconds > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_TLINT_WATCH_MAX", max_ticks_arg)
        ticks = 0
        dead = _watch.deadline("JEV_TLINT_WATCH_SECS", watch_max_arg)
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
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            rows = lint_cases(path, policy_path=policy_path)
            tick = {
                "ts": int(_time.time()),
                "findings": len(rows),
                "errors": sum(1 for r in rows if r["severity"] == "error"),
                "warnings": sum(1 for r in rows if r["severity"] == "warn"),
                "infos": sum(1 for r in rows if r["severity"] == "info"),
            }
            _watch.emit_or_jq(tick, jq_value, out_path, quiet=_watch.quiet("JEV_TLINT_WATCH_QUIET", quiet), bad=tick["errors"] or (strict and tick["findings"]))
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
            _time.sleep(watch_seconds)
        rc = 1 if (tick.get("errors", 0) or (strict and tick.get("findings", 0))) else 0
        if verdict_path and verdict_ok and not _write_verdict(rc):
            return 1
        return rc
    if do_fix:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            sys.stderr.write("cannot fix %s: %s\n" % (path, exc))
            return 2
        applied = fix_cases(data) if isinstance(data, dict) else []
        if applied and not dry_run:
            try:
                _atomic_write(
                    path, json.dumps(data, indent=2, ensure_ascii=False) + "\n"
                )
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (path, exc))
                return 1
        verb = "would fix" if dry_run else "fixed"
        for rule in sorted(set(applied)):
            sys.stderr.write("%s %s x%d\n" % (verb, rule, applied.count(rule)))
    findings = lint_cases(path, policy_path=policy_path)
    shown = [
        f
        for f in findings
        if (not severity or f["severity"] == severity)
        and (not quiet or f["severity"] == "error")
    ]
    n_err = sum(1 for f in findings if f["severity"] == "error")
    n_warn = sum(1 for f in findings if f["severity"] == "warn")
    n_info = sum(1 for f in findings if f["severity"] == "info")
    payload = {
        "path": str(path),
        "findings": shown,
        "errors": n_err,
        "warnings": n_warn,
        "infos": n_info,
    }
    if out_path:
        try:
            _atomic_write(Path(out_path),
                json.dumps(payload, indent=2)
                + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d finding(s) to %s\n" % (len(shown), out_path))
    rc = 1 if (n_err or (strict and findings)) else 0
    if verdict_path:
        if not _watch.write_verdict(
            verdict_path,
            {
                "verdict": "fail" if rc else "pass",
                "ticks": 1,
                "findings": len(findings),
                "errors": n_err,
                "warnings": n_warn,
                "infos": n_info,
            },
        ):
            return 1
    if jq_value:
        value, found = _watch.dig(payload, jq_value)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (jq_value, ", ".join(sorted(payload)))
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
        return 0
    if as_json:
        sys.stdout.write(
            json.dumps(payload, indent=2)
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
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
