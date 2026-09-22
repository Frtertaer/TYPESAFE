#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Static checks for policy.json itself.

question_lint.py checks the wording of each question on a request;
this file checks the policy the whole pack runs on: required keys,
threshold ranges and ordering, template shape, hatch coverage, and
must_ask/never_ask consistency. A bad edit should fail here, in tests,
not inside a hook at runtime.

Findings: {"rule", "severity", "path", "message", "fix"},
severity in ("error", "warn", "info").

    python policy_lint.py [path/to/policy.json] [--strict]
Exit 0 when no errors (warnings are fine), 1 on any error,
2 when the file cannot be parsed at all.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "policy.json"

QUESTION_TYPES = ("choice", "noul", "score")
KEY_RE = re.compile(r"^[a-z0-9_]+$")

REQUIRED_KEYS = (
    "version",
    "model",
    "endpoint",
    "role",
    "coder_role",
    "default",
    "question_soft_max",
    "question_hard_max",
    "choice_option_hard_max",
    "confidence_floor",
    "noul_yes",
    "noul_no",
    "noul_unsure",
    "strong_pick",
    "tight_gap",
    "sidecar_ttl_seconds",
    "must_ask",
    "never_ask",
    "require_hatch",
    "escalate_if",
    "templates",
)

# probability-like fields: finite numbers inside [0, 1]
PROB_FIELDS = ("confidence_floor", "noul_yes", "noul_no", "noul_unsure", "strong_pick", "tight_gap")
ESCALATE_PROB_FIELDS = ("confidence_below", "noul_near", "choice_gap_below")
POSITIVE_INT_FIELDS = ("version", "question_soft_max", "question_hard_max", "choice_option_hard_max")
NONNEG_NUM_FIELDS = (
    "catalog_cache_seconds",
    "dedupe_ttl_seconds",
    "hook_budget_seconds",
    "hook_jev_retries",
    "hook_jev_timeout_seconds",
    "sidecar_ttl_seconds",
)
NONEMPTY_STR_FIELDS = ("model", "endpoint", "default", "role", "coder_role")
ESCALATE_BOOL_FIELDS = ("irreversible",)
KNOWN_ESCALATE_KEYS = ESCALATE_PROB_FIELDS + ESCALATE_BOOL_FIELDS
KNOWN_TOP_KEYS = REQUIRED_KEYS + (
    "catalog_cache_seconds",
    "catalogs",
    "dedupe_ttl_seconds",
    "hallucination",
    "hook_budget_seconds",
    "hook_jev_retries",
    "hook_jev_timeout_seconds",
    "stop_words",
)


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def lint_policy(policy) -> list[dict]:
    findings: list[dict] = []

    def add(rule: str, severity: str, path: str, message: str, fix: str) -> None:
        findings.append(
            {"rule": rule, "severity": severity, "path": path, "message": message, "fix": fix}
        )

    if not isinstance(policy, dict):
        add("P000", "error", "$", "policy is not a JSON object", "policy.json must contain one object")
        return findings

    for key in REQUIRED_KEYS:
        if key not in policy:
            add(
                "P001",
                "error",
                key,
                "missing required key %r" % key,
                "jev.py reads this field; without it a fallback fires silently",
            )

    for key in policy:
        if key not in KNOWN_TOP_KEYS:
            add(
                "P011",
                "warn",
                key,
                "unknown top-level key %r" % key,
                "typo guard: this key is never read; rename or drop it",
            )

    for key in PROB_FIELDS:
        value = policy.get(key)
        if key in policy and (not _num(value) or not 0.0 <= value <= 1.0):
            add(
                "P002",
                "error",
                key,
                "%r must be a number in [0, 1], got %r" % (key, value),
                "probability thresholds only make sense inside [0, 1]",
            )
    escalate = policy.get("escalate_if")
    if escalate is not None and not isinstance(escalate, dict):
        add("P003", "error", "escalate_if", "escalate_if must be an object", "expected an object of thresholds")
    if isinstance(escalate, dict):
        for key in ESCALATE_PROB_FIELDS:
            value = escalate.get(key)
            if key in escalate and (not _num(value) or not 0.0 <= value <= 1.0):
                add(
                    "P002",
                    "error",
                    "escalate_if." + key,
                    "%r must be a number in [0, 1], got %r" % (key, value),
                    "probability thresholds only make sense inside [0, 1]",
                )
        for key in ESCALATE_BOOL_FIELDS:
            value = escalate.get(key)
            if key in escalate and not isinstance(value, bool):
                add(
                    "P002",
                    "error",
                    "escalate_if." + key,
                    "%r must be a boolean, got %r" % (key, value),
                    "use true or false",
                )
        for key in escalate:
            if key not in KNOWN_ESCALATE_KEYS:
                add(
                    "P010",
                    "warn",
                    "escalate_if." + key,
                    "unknown escalate_if key %r" % key,
                    "typo guard: this key is never read; rename or drop it",
                )

    for key in POSITIVE_INT_FIELDS:
        value = policy.get(key)
        if key in policy and (not _num(value) or int(value) != value or value < 1):
            add(
                "P002",
                "error",
                key,
                "%r must be a positive integer, got %r" % (key, value),
                "limits are whole numbers >= 1",
            )
    for key in NONNEG_NUM_FIELDS:
        value = policy.get(key)
        if key in policy and (not _num(value) or value < 0):
            add(
                "P002",
                "error",
                key,
                "%r must be a number >= 0, got %r" % (key, value),
                "a TTL of 0 marks every sidecar stale immediately",
            )
    for key in NONEMPTY_STR_FIELDS:
        value = policy.get(key)
        if key in policy and (not isinstance(value, str) or not value.strip()):
            add("P002", "error", key, "%r must be a non-empty string" % key, "empty strings break request building")
    if "require_hatch" in policy and not isinstance(policy["require_hatch"], bool):
        add("P002", "error", "require_hatch", "require_hatch must be a boolean", "use true or false")

    endpoint = policy.get("endpoint")
    if isinstance(endpoint, str) and endpoint and not endpoint.startswith("https://"):
        add(
            "P011",
            "error",
            "endpoint",
            "endpoint is not https: %r" % endpoint,
            "jev.py POSTs the API key to this URL; never plain http",
        )

    no_below = policy.get("noul_no")
    unsure = policy.get("noul_unsure")
    yes_above = policy.get("noul_yes")
    if _num(no_below) and _num(unsure) and not no_below < unsure:
        add("P004", "error", "noul_unsure", "need noul_no < noul_unsure (%.3g !< %.3g)" % (no_below, unsure), "bands must not overlap")
    if _num(unsure) and _num(yes_above) and not unsure < yes_above:
        add("P004", "error", "noul_yes", "need noul_unsure < noul_yes (%.3g !< %.3g)" % (unsure, yes_above), "bands must not overlap")
    conf_floor = policy.get("confidence_floor")
    strong = policy.get("strong_pick")
    if _num(conf_floor) and _num(strong) and not conf_floor < strong:
        add("P004", "error", "strong_pick", "need confidence_floor < strong_pick (%.3g !< %.3g)" % (conf_floor, strong), "a strong pick must clear the escalate floor")
    soft = policy.get("question_soft_max")
    hard = policy.get("question_hard_max")
    if _num(soft) and _num(hard) and not 0 < soft <= hard:
        add("P004", "error", "question_soft_max", "need 0 < soft_max <= hard_max (%r, %r)" % (soft, hard), "the soft cap warns above it; it must sit under the hard cap")
    if isinstance(escalate, dict):
        near = escalate.get("noul_near")
        if _num(near) and _num(no_below) and _num(yes_above) and not no_below <= near <= yes_above:
            add(
                "P004",
                "error",
                "escalate_if.noul_near",
                "noul_near %.3g outside [noul_no, noul_yes] = [%.3g, %.3g]" % (near, no_below, yes_above),
                "the 'near unsure' band must sit inside the answer bands",
            )

    if isinstance(escalate, dict):
        below = escalate.get("confidence_below")
        if _num(below) and _num(conf_floor) and below != conf_floor:
            add(
                "P005",
                "warn",
                "escalate_if.confidence_below",
                "differs from top-level confidence_floor (%.3g vs %.3g)" % (below, conf_floor),
                "both are read; keep them equal or pick one source of truth",
            )
        gap = escalate.get("choice_gap_below")
        tight = policy.get("tight_gap")
        if _num(gap) and _num(tight) and gap != tight:
            add(
                "P005",
                "warn",
                "escalate_if.choice_gap_below",
                "differs from top-level tight_gap (%.3g vs %.3g)" % (gap, tight),
                "both are read; keep them equal or pick one source of truth",
            )

    for key in ("must_ask", "never_ask"):
        value = policy.get(key)
        if key in policy and (not isinstance(value, list) or not all(isinstance(k, str) and k for k in value)):
            add("P002", "error", key, "%r must be a list of non-empty strings" % key, "kind names like 'approach'")
    must_ask = [k for k in policy.get("must_ask") or [] if isinstance(k, str)]
    never_ask = [k for k in policy.get("never_ask") or [] if isinstance(k, str)]
    overlap = sorted(set(must_ask) & set(never_ask))
    if overlap:
        add(
            "P006",
            "error",
            "must_ask",
            "kinds in both must_ask and never_ask: %s" % ", ".join(overlap),
            "a kind cannot be mandatory and forbidden at once",
        )

    templates = policy.get("templates")
    if templates is not None and not isinstance(templates, dict):
        add("P003", "error", "templates", "templates must be an object", "expected a map of template id -> question template")
    require_hatch = bool(policy.get("require_hatch", True))
    hatch_ids = {"none", "other"}
    if isinstance(templates, dict):
        seen_instructions: dict[str, str] = {}
        for tid, template in templates.items():
            path = "templates." + str(tid)
            if not isinstance(template, dict):
                add("P007", "error", path, "template must be an object", "expected {type, instructions, criteria?}")
                continue
            qtype = template.get("type")
            if qtype not in QUESTION_TYPES:
                add(
                    "P007",
                    "error",
                    path + ".type",
                    "type must be one of %s, got %r" % ("/".join(QUESTION_TYPES), qtype),
                    "jev.py rejects unknown question types at ask time",
                )
            instructions = template.get("instructions")
            if not isinstance(instructions, str) or not instructions.strip():
                add("P007", "error", path + ".instructions", "instructions must be a non-empty string", "Jev cannot answer a question with no text")
            elif not instructions.rstrip().endswith("?"):
                add("P008", "warn", path + ".instructions", "instructions do not end with '?'", "templates are questions; phrasing them as such keeps wording lint consistent")
            elif instructions in seen_instructions:
                add(
                    "P008",
                    "warn",
                    path + ".instructions",
                    "instructions identical to template %r" % seen_instructions[instructions],
                    "two kinds asking the same question makes the trace ambiguous",
                )
            else:
                seen_instructions[instructions] = str(tid)
            criteria = template.get("criteria")
            if qtype == "choice":
                if criteria is not None and not isinstance(criteria, dict):
                    add("P009", "error", path + ".criteria", "choice criteria must be an object", "map option id -> meaning")
                elif isinstance(criteria, dict):
                    if len(criteria) == 1 and not (hatch_ids & set(criteria)):
                        add("P009", "error", path + ".criteria", "choice template has a single non-hatch option", "one option is not a choice; a lone 'none' marks a slot whose options are injected per request")
                    bad_keys = [k for k in criteria if not isinstance(k, str) or not KEY_RE.match(k)]
                    if bad_keys:
                        add("P009", "error", path + ".criteria", "option ids must match %s: %s" % (KEY_RE.pattern, bad_keys), "lowercase alnum/underscore ids keep answers parseable")
                    empty_vals = [k for k, v in criteria.items() if not isinstance(v, str) or not v.strip()]
                    if empty_vals:
                        add("P009", "error", path + ".criteria", "options with empty meaning: %s" % empty_vals, "every option needs a one-line meaning")
                    if require_hatch and len(criteria) >= 2 and not (hatch_ids & set(criteria)):
                        add(
                            "P009",
                            "error",
                            path + ".criteria",
                            "require_hatch is on but no hatch option (%s)" % "/".join(sorted(hatch_ids)),
                            "a closed option list must let Jev say 'none of these'",
                        )
            elif qtype in ("noul", "score") and isinstance(criteria, dict) and criteria:
                add(
                    "P009",
                    "warn",
                    path + ".criteria",
                    "%s template carries a criteria map it never uses" % qtype,
                    "dead config; drop it or make the template a choice",
                )
        for kind in must_ask:
            if kind not in templates:
                add(
                    "P012",
                    "error",
                    "templates." + kind,
                    "must_ask kind %r has no template" % kind,
                    "a mandatory ask needs a question template to send",
                )
        for tid in templates:
            if tid not in must_ask:
                add(
                    "P013",
                    "info",
                    "templates." + str(tid),
                    "template not listed in must_ask",
                    "internal templates are fine; this is a reminder the trigger layer never auto-asks it",
                )

    return findings


def format_finding(finding: dict) -> str:
    return "%s %s %s: %s" % (
        finding["severity"].upper(),
        finding["rule"],
        finding["path"],
        finding["message"],
    )


def diff_policy(a: dict, b: dict) -> list[str]:
    """Shallow top-level key diff of two policy dicts. Returns line list."""
    lines: list[str] = []
    for key in sorted(set(a) | set(b)):
        if key not in b:
            lines.append("- %s = %s" % (key, json.dumps(a[key], sort_keys=True)[:120]))
        elif key not in a:
            lines.append("+ %s = %s" % (key, json.dumps(b[key], sort_keys=True)[:120]))
        elif a[key] != b[key]:
            lines.append(
                "~ %s: %s -> %s"
                % (
                    key,
                    json.dumps(a[key], sort_keys=True)[:60],
                    json.dumps(b[key], sort_keys=True)[:60],
                )
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    strict = "--strict" in argv
    show = "--show" in argv
    quiet = "--quiet" in argv
    as_json = "--json" in argv
    severity = ""
    if "--severity" in argv:
        i = argv.index("--severity")
        if i + 1 >= len(argv):
            sys.stderr.write("--severity needs a value (error|warn|info)\n")
            return 2
        severity = argv[i + 1].strip().lower()
        if severity not in ("error", "warn", "info"):
            sys.stderr.write("bad --severity %r (want error|warn|info)\n" % severity)
            return 2
        del argv[i : i + 2]
    else:
        env_sev = os.environ.get("JEV_PLINT_SEVERITY", "").strip().lower()
        if env_sev in ("error", "warn", "info"):
            severity = env_sev
    out_path = ""
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 >= len(argv):
            sys.stderr.write("--out needs a PATH value\n")
            return 2
        out_path = argv[i + 1].strip()
        del argv[i : i + 2]
    diff_path = None
    if "--diff" in argv:
        i = argv.index("--diff")
        if i + 1 >= len(argv):
            sys.stderr.write("--diff requires a policy file to compare against\n")
            return 2
        diff_path = argv[i + 1]
        del argv[i : i + 2]
    watch_seconds = 0.0
    if "--watch" in argv:
        i = argv.index("--watch")
        if i + 1 >= len(argv):
            sys.stderr.write("--watch needs a SECONDS value\n")
            return 2
        try:
            watch_seconds = float(argv[i + 1])
        except ValueError:
            sys.stderr.write("bad --watch %r (seconds)\n" % argv[i + 1])
            return 2
        del argv[i : i + 2]
    max_ticks_arg = 0
    if "--max-ticks" in argv:
        i = argv.index("--max-ticks")
        if i + 1 >= len(argv):
            sys.stderr.write("--max-ticks needs an N value\n")
            return 2
        try:
            max_ticks_arg = int(argv[i + 1])
        except ValueError:
            sys.stderr.write("bad --max-ticks %r (integer)\n" % argv[i + 1])
            return 2
        del argv[i : i + 2]
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
        del argv[i : i + 2]
    argv = [a for a in argv if a not in {"--strict", "--show", "--quiet", "--json"}]
    if len(argv) > 1:
        results = []
        for arg in argv:
            fpath = Path(arg)
            try:
                fpol = json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                sys.stdout.write("ERROR P000 $: cannot parse %s (%s)\n" % (fpath, exc))
                return 2
            ffind = lint_policy(fpol)
            ferr = sum(1 for f in ffind if f["severity"] == "error")
            fwarn = sum(1 for f in ffind if f["severity"] == "warn")
            finfo = sum(1 for f in ffind if f["severity"] == "info")
            fshown = [
                f
                for f in ffind
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
        if as_json:
            sys.stdout.write(json.dumps(results, indent=2) + "\n")
        else:
            for res in results:
                sys.stdout.write("%s:\n" % res["path"])
                for finding in res["findings"]:
                    sys.stdout.write(format_finding(finding) + "\n")
                sys.stdout.write(
                    "  %d error(s), %d warning(s), %d info\n"
                    % (res["errors"], res["warnings"], res["infos"])
                )
        if out_path:
            try:
                Path(out_path).write_text(
                    json.dumps(results, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
                return 1
        any_err = any(r["errors"] for r in results)
        any_warn = any(r["warnings"] for r in results)
        return 1 if any_err or (strict and any_warn) else 0
    path = Path(argv[0]) if argv else DEFAULT_POLICY
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.stdout.write("ERROR P000 $: cannot parse %s (%s)\n" % (path, exc))
        return 2
    if diff_path is not None:
        try:
            other = json.loads(Path(diff_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            sys.stdout.write("ERROR P000 $: cannot parse %s (%s)\n" % (diff_path, exc))
            return 2
        lines = diff_policy(other, policy)
        sys.stdout.write("diff %s -> %s\n" % (diff_path, path))
        for line in lines:
            sys.stdout.write(line + "\n")
        sys.stdout.write("%d difference(s)\n" % len(lines))
        return 0
    if show:
        sys.stdout.write(json.dumps({"path": str(path), "policy": policy}, indent=2) + "\n")
        return 0
    if watch_seconds > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_PLINT_WATCH_MAX", max_ticks_arg)
        ticks = 0
        dead = _watch.deadline("JEV_PLINT_WATCH_SECS", watch_max_arg)
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            rows = lint_policy(policy)
            tick = {
                "ts": int(_time.time()),
                "findings": len(rows),
                "errors": sum(1 for r in rows if r["severity"] == "error"),
                "warnings": sum(1 for r in rows if r["severity"] == "warn"),
                "infos": sum(1 for r in rows if r["severity"] == "info"),
            }
            _watch.emit(tick, out_path, quiet=quiet, bad=tick["errors"] or (strict and tick["findings"]))
            ticks += 1
            _time.sleep(watch_seconds)
            try:
                policy = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return 1 if (tick["errors"] or (strict and tick["findings"])) else 0
    findings = lint_policy(policy)
    shown_rows = [
        f
        for f in findings
        if (not severity or f["severity"] == severity)
        and (not quiet or f["severity"] == "error")
    ]
    errors = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warn")
    infos = sum(1 for f in findings if f["severity"] == "info")
    if out_path:
        try:
            Path(out_path).write_text(
                json.dumps(
                    {
                        "path": str(path),
                        "findings": shown_rows,
                        "errors": errors,
                        "warnings": warns,
                        "infos": infos,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d finding(s) to %s\n" % (len(shown_rows), out_path))
    if as_json:
        sys.stdout.write(
            json.dumps(
                {
                    "path": str(path),
                    "findings": shown_rows,
                    "errors": errors,
                    "warnings": warns,
                    "infos": infos,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        for finding in findings:
            if severity and finding["severity"] != severity:
                continue
            if quiet and finding["severity"] != "error":
                continue
            sys.stdout.write(format_finding(finding) + "\n")
    if not quiet and not as_json:
        sys.stdout.write("policy_lint: %d error(s), %d warning(s), %d info\n" % (errors, warns, infos))
    if errors or (strict and warns):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
