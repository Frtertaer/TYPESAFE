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

    python policy_lint.py [path/to/policy.json] [--strict] [--fix] [--dry-run]
Exit 0 when no errors (warnings are fine), 1 on any error,
2 when the file cannot be parsed at all. --fix rewrites the file in
place, dropping unknown top-level / escalate_if keys (P011/P010);
--dry-run reports what --fix would change without writing.
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
    "hook_max_prompt_chars",
    "hook_payload_max_bytes",
    "sidecar_ttl_seconds",
    "smoke_perf_budget_seconds",
    "spill_max_bytes",
    "spill_max_files",
)
NONEMPTY_STR_FIELDS = ("model", "endpoint", "default", "role", "coder_role")
ESCALATE_BOOL_FIELDS = ("irreversible",)
KNOWN_ESCALATE_KEYS = ESCALATE_PROB_FIELDS + ESCALATE_BOOL_FIELDS
KNOWN_TOP_KEYS = REQUIRED_KEYS + (
    "catalog_cache_seconds",
    "catalogs",
    "choice",
    "dedupe_ttl_seconds",
    "hallucination",
    "hook_budget_seconds",
    "hook_jev_retries",
    "hook_jev_timeout_seconds",
    "hook_max_prompt_chars",
    "hook_payload_max_bytes",
    "progress",
    "smoke_perf_budget_seconds",
    "spill_max_bytes",
    "spill_max_files",
    "stop_words",
)


RULES = {
    "P000": "policy file is not a JSON object",
    "P001": "missing required key; jev.py reads it and a fallback fires silently without it",
    "P002": "field has the wrong type or is out of range (probability in [0,1], positive int, non-empty string, boolean)",
    "P003": "a section that must be an object is not (escalate_if, templates)",
    "P004": "threshold ordering violated (noul_no < noul_unsure < noul_yes, confidence_floor < strong_pick, soft_max <= hard_max, bands inside bands)",
    "P005": "duplicate thresholds disagree (escalate_if.confidence_below vs confidence_floor, choice_gap_below vs tight_gap)",
    "P006": "a kind is listed in both must_ask and never_ask",
    "P007": "template is malformed (not an object, missing instructions, instructions not a non-empty string)",
    "P008": "template instructions do not end with '?' or carry a bad type",
    "P009": "choice criteria malformed (not an object, single non-hatch option, bad option ids, empty meanings, require_hatch with no hatch option, dead criteria on noul/score)",
    "P010": "unknown escalate_if key; typo guard (auto-fixed by --fix)",
    "P011": "unknown top-level key or non-https endpoint; typo guard (auto-fixed by --fix)",
    "P012": "a must_ask kind has no template to send",
    "P013": "template not listed in must_ask; the trigger layer never auto-asks it",
    "P014": "require_hatch is off and a closed choice list (2+ options) has no none/other escape hatch",
    "P015": "a known policy key is never referenced by any pack script - dead knob or consumer drift (only with --usage)",
    "P016": "choice.hatch_ids malformed (not a list, empty, non-string member, or duplicates) - jev.py falls back to none/other silently",
}


def schema_rows() -> dict:
    """Key -> {required, type} for every KNOWN_TOP_KEYS entry (sorted)."""
    rows = {k: {"required": False, "type": "any"} for k in KNOWN_TOP_KEYS}
    for key in REQUIRED_KEYS:
        rows[key]["required"] = True
    for key in PROB_FIELDS:
        rows[key]["type"] = "prob [0,1]"
    for key in POSITIVE_INT_FIELDS:
        rows[key]["type"] = "positive int"
    for key in NONNEG_NUM_FIELDS:
        rows[key]["type"] = "nonneg number"
    for key in NONEMPTY_STR_FIELDS:
        rows[key]["type"] = "nonempty string"
    rows["must_ask"]["type"] = "list[str]"
    rows["never_ask"]["type"] = "list[str]"
    rows["escalate_if"]["type"] = (
        "object{confidence_below,noul_near,choice_gap_below,irreversible}"
    )
    rows["templates"]["type"] = "object{kind: {instructions}}"
    rows["require_hatch"]["type"] = "boolean"
    rows["catalogs"]["type"] = "list[{name,url}]"
    rows["stop_words"]["type"] = "list[str]"
    rows["hallucination"]["type"] = "object{claim}"
    rows["progress"]["type"] = "object{points,review_points,max_*}"
    rows["choice"]["type"] = "object{hatch_ids?: list[str]}"
    return {k: rows[k] for k in sorted(rows)}


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
    choice_sec = policy.get("choice")
    if choice_sec is not None and not isinstance(choice_sec, dict):
        add("P003", "error", "choice", "choice must be an object", "expected an object of choice-mode settings")
    hatch_ids = {"none", "other"}
    if isinstance(choice_sec, dict):
        raw_hatch = choice_sec.get("hatch_ids")
        if raw_hatch is not None:
            if not isinstance(raw_hatch, (list, tuple)) or not raw_hatch:
                add(
                    "P016",
                    "error",
                    "choice.hatch_ids",
                    "hatch_ids must be a non-empty list of strings, got %r" % (raw_hatch,),
                    "jev.py falls back to none/other when this key is unusable",
                )
            elif not all(isinstance(h, str) and h.strip() for h in raw_hatch):
                add(
                    "P016",
                    "error",
                    "choice.hatch_ids",
                    "hatch_ids members must be non-empty strings, got %r" % (raw_hatch,),
                    "jev.py falls back to none/other when this key is unusable",
                )
            elif len({h.lower() for h in raw_hatch}) != len(raw_hatch):
                add(
                    "P016",
                    "error",
                    "choice.hatch_ids",
                    "hatch_ids has case-insensitive duplicates %r" % (raw_hatch,),
                    "duplicate hatch ids change no behavior; drop one",
                )
            else:
                hatch_ids = {str(h) for h in raw_hatch}
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
    if isinstance(endpoint, str) and endpoint and not endpoint.lower().startswith("https://"):
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
                    elif not require_hatch and len(criteria) >= 2 and not (hatch_ids & set(criteria)):
                        add(
                            "P014",
                            "warn",
                            path + ".criteria",
                            "require_hatch is off and this closed list has no hatch option",
                            "if the opt-out is deliberate, fine; otherwise add 'none'/'other'",
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

    if "progress" in policy:
        from progress_core import lint_progress

        findings.extend(lint_progress(policy))

    return findings


def usage_findings(policy, scripts_dir: Path | None = None) -> list[dict]:
    """P015 rows for known keys the pack's scripts never reference."""
    if not isinstance(policy, dict):
        return []
    root = scripts_dir if scripts_dir is not None else Path(__file__).resolve().parent
    try:
        blob = "\n".join(
            f.read_text(encoding="utf-8", errors="replace")
            for f in sorted(root.glob("*.py"))
            if f.name != "policy_lint.py"
        )
    except OSError:
        return []
    findings = []
    for key in sorted(policy):
        if key not in KNOWN_TOP_KEYS:
            continue  # unknown keys are P011's job
        if not re.search(r"\b" + re.escape(key) + r"\b", blob):
            findings.append(
                {
                    "rule": "P015",
                    "severity": "info",
                    "path": key,
                    "message": "no pack script reads '%s'; dead knob or consumer drift" % key,
                    "fix": "drop the key or wire a reader in the scripts",
                }
            )
    return findings


def fix_policy(policy) -> list[str]:
    """Drop unknown keys (P011 top-level, P010 escalate_if); returns rule ids applied."""
    applied: list[str] = []
    if not isinstance(policy, dict):
        return applied
    for key in [k for k in policy if k not in KNOWN_TOP_KEYS]:
        del policy[key]
        applied.append("P011")
    escalate = policy.get("escalate_if")
    if isinstance(escalate, dict):
        for key in [k for k in escalate if k not in KNOWN_ESCALATE_KEYS]:
            del escalate[key]
            applied.append("P010")
    return applied


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


BASELINE_FIELDS = ("rule", "path", "message")


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


USAGE = 'Usage: python policy_lint.py [POLICY.json ...] [flags]  `-` reads the JSON from stdin (needs a real path for --fix/--watch/--diff).\nStatic checks for policy.json (required keys, ranges, ordering, template).\nFlags:\n  --strict          exit 1 on warnings too\n  --fix             auto-apply safe fixes in place\n  --dry-run         with --fix: print the diff, write nothing\n  --diff PATH       diff this policy against another JSON file (+/-/~ lines; "-" reads it from stdin)\n  --show            print the effective policy JSON and exit\n  --severity S[,S...]  only these severities (error|warn|info comma list; JEV_PLINT_SEVERITY)\n  --only R[,R...]     lint only these rule ids (rc 2 on unknown id)\n  --env             print the resolved env config JSON (files, policy, severity, strict, quiet, watch_max, watch_secs, watch_quiet; --jq KEY one field, --out PATH writes it)\n  --explain RULE    print the description of one rule id and exit ("-" reads it from stdin)\n  --rules           print every rule id + description (--json emits a list)\n  --schema          print the known policy.json key/type table (--json emits an object)\n  --usage           flag known keys no pack script reads (P015, info; scans scripts dir)\n  --quiet           print only errors/warnings count\n  --baseline PATH   suppress findings already recorded in PATH ("-" reads it from stdin)\n  --baseline-write PATH  write current findings to PATH for --baseline runs\n  --json            findings as JSON array\n  --md              findings as a Markdown table\n  --jq KEY          one dotted-path field of the findings payload\n  --out PATH        append/write the payload to a file (fail-open)\n  --self-test       lint a synthetic known-bad policy dict; exit 1 when no findings\n  --help            print this usage and exit\n  --version         print the pack policy version and exit\n  --watch S         re-lint every S seconds emitting tick JSON\n  --watch-max S     stop the watch after S elapsed seconds\n  --max-ticks N     stop the watch after N ticks\n  --fail-fast       stop the watch on the first erroring tick\n  --unchanged-max N stop the watch after N consecutive identical ticks\n  --verdict PATH    write a slim {verdict: pass|fail, ...} JSON ("-" prints it to stdout)\nExit 0 clean/warn, 1 on any error, 2 on bad args.\n'


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if _watch.maybe_version(argv):
        return 0
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(USAGE)
        return 0
    strict = "--strict" in argv
    show = "--show" in argv
    quiet = "--quiet" in argv
    as_json = "--json" in argv
    as_md = "--md" in argv
    fail_fast = "--fail-fast" in argv
    do_fix = "--fix" in argv
    dry_run = "--dry-run" in argv
    severity: set[str] = set()
    if "--severity" in argv:
        i = argv.index("--severity")
        if i + 1 >= len(argv):
            sys.stderr.write("--severity needs a value (error|warn|info)\n")
            return 2
        picked = _watch.severity_arg(argv[i + 1])
        if picked is None:
            sys.stderr.write("bad --severity %r (want comma list of error|warn|info)\n" % argv[i + 1])
            return 2
        severity = picked
        del argv[i : i + 2]
    else:
        env_sev = os.environ.get("JEV_PLINT_SEVERITY", "").strip().lower()
        picked = _watch.severity_arg(env_sev)
        if picked:
            severity = picked
    only: set[str] = set()
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 >= len(argv):
            sys.stderr.write("--only needs a RULE[,RULE...] value\n")
            return 2
        only = {s.strip().upper() for s in argv[i + 1].split(",") if s.strip()}
        unknown = only - set(RULES)
        if not only or unknown:
            sys.stderr.write(
                "bad --only %r (rules: %s)\n" % (argv[i + 1], ", ".join(sorted(RULES)))
            )
            return 2
        del argv[i : i + 2]
    unchanged_max = 0
    if "--unchanged-max" in argv:
        i = argv.index("--unchanged-max")
        if i + 1 >= len(argv):
            sys.stderr.write("--unchanged-max needs a value (int ticks)\n")
            return 2
        try:
            unchanged_max = int(argv[i + 1])
        except ValueError:
            sys.stderr.write("bad --unchanged-max %r\n" % argv[i + 1])
            return 2
        del argv[i : i + 2]
    if "--explain" in argv:
        i = argv.index("--explain")
        if i + 1 >= len(argv):
            sys.stderr.write("--explain needs a RULE value\n")
            return 2
        rule = _watch.text_arg(argv[i + 1]).strip().upper()
        if rule not in RULES:
            sys.stderr.write(
                "unknown rule %r (rules: %s)\n" % (rule, ", ".join(sorted(RULES)))
            )
            return 2
        sys.stdout.write("%s: %s\n" % (rule, RULES[rule]))
        return 0
    if "--self-test" in argv:
        found = sorted({f["rule"] for f in lint_policy({"version": 1})})
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
    if "--schema" in argv:
        rows = schema_rows()
        if "--json" in argv:
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        else:
            for key in rows:
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (
                        key,
                        rows[key]["type"],
                        "required" if rows[key]["required"] else "optional",
                    )
                )
        return 0
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
        del argv[idx : idx + 2]
    verdict_path = ""
    if "--verdict" in argv:
        idx = argv.index("--verdict")
        if idx + 1 >= len(argv):
            sys.stderr.write("--verdict needs a PATH value\n")
            return 2
        verdict_path = argv[idx + 1]
        del argv[idx : idx + 2]
    baseline_path = ""
    if "--baseline" in argv:
        idx = argv.index("--baseline")
        if idx + 1 >= len(argv):
            sys.stderr.write("--baseline needs a PATH value\n")
            return 2
        baseline_path = argv[idx + 1]
        del argv[idx : idx + 2]
    baseline_write = ""
    if "--baseline-write" in argv:
        idx = argv.index("--baseline-write")
        if idx + 1 >= len(argv):
            sys.stderr.write("--baseline-write needs a PATH value\n")
            return 2
        baseline_write = argv[idx + 1]
        del argv[idx : idx + 2]
    usage = "--usage" in argv
    argv = [
        a
        for a in argv
        if a
        not in {"--strict", "--show", "--quiet", "--json", "--md", "--fail-fast", "--fix", "--dry-run", "--usage"}
    ]
    baseline_keys: set | None = None
    if "--env" in argv:
        try:
            env_watch_secs = float(os.environ.get("JEV_PLINT_WATCH_SECS", "") or 0)
        except ValueError:
            env_watch_secs = 0.0
        files = [a for a in argv if not a.startswith("--")]
        report = {
            "files": files,
            "policy": files[0] if files else str(DEFAULT_POLICY),
            "severity": ",".join(sorted(severity)),
            "strict": strict,
            "quiet": quiet,
            "watch_max": _watch.cap("JEV_PLINT_WATCH_MAX", None),
            "watch_secs": env_watch_secs,
            "watch_quiet": _watch.quiet("JEV_PLINT_WATCH_QUIET", quiet),
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
    if baseline_path:
        baseline_keys = _watch.load_baseline(baseline_path, BASELINE_FIELDS)
    if len(argv) > 1:
        if do_fix:
            sys.stderr.write("--fix does not support multiple paths\n")
            return 2
        results = []
        n_suppressed = 0
        snapshot = []
        for arg in argv:
            if arg == "-":
                try:
                    fpol = json.loads(sys.stdin.read())
                except ValueError as exc:
                    sys.stdout.write("ERROR P000 $: cannot parse stdin (%s)\n" % exc)
                    return 2
            else:
                try:
                    fpol = json.loads(Path(arg).read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    sys.stdout.write("ERROR P000 $: cannot parse %s (%s)\n" % (arg, exc))
                    return 2
            ffind = _watch.only_filter(lint_policy(fpol), only)
            if usage:
                ffind += usage_findings(fpol)
                ffind = _watch.only_filter(ffind, only)
            if baseline_write:
                snapshot.extend({"file": arg, **f} for f in ffind)
            if baseline_keys is not None:
                kept = _drop_baseline(ffind, baseline_keys)
                n_suppressed += len(ffind) - len(kept)
                ffind = kept
            ferr = sum(1 for f in ffind if f["severity"] == "error")
            fwarn = sum(1 for f in ffind if f["severity"] == "warn")
            finfo = sum(1 for f in ffind if f["severity"] == "info")
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
                    "warnings": fwarn,
                    "infos": finfo,
                }
            )
        if as_json:
            sys.stdout.write(json.dumps(results, indent=2) + "\n")
        elif as_md:
            md_rows = [
                {**f, "path": res["path"]}
                for res in results
                for f in res["findings"]
            ]
            _watch.md_table(md_rows, ["severity", "rule", "path", "message"])
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
                _atomic_write(
                    Path(out_path), json.dumps(results, indent=2) + "\n"
                )
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
                return 1
        if baseline_write and not _write_baseline(baseline_write, snapshot):
            return 1
        if n_suppressed:
            sys.stderr.write(
                "baseline: suppressed %d known finding(s)\n" % n_suppressed
            )
        any_err = any(r["errors"] for r in results)
        any_warn = any(r["warnings"] for r in results)
        return 1 if any_err or (strict and any_warn) else 0
    path = Path(argv[0]) if argv else DEFAULT_POLICY
    if argv and argv[0] == "-":
        if do_fix or watch_seconds > 0 or diff_path:
            sys.stderr.write("- (stdin) supports neither --fix, --watch nor --diff\n")
            return 2
        try:
            policy = json.loads(sys.stdin.read())
        except ValueError as exc:
            sys.stdout.write("ERROR P000 $: cannot parse stdin (%s)\n" % exc)
            return 2
    else:
        try:
            policy = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            sys.stdout.write("ERROR P000 $: cannot parse %s (%s)\n" % (path, exc))
            return 2
    if do_fix:
        applied = fix_policy(policy)
        if applied and not dry_run:
            try:
                _atomic_write(
                    path, json.dumps(policy, indent=2, ensure_ascii=False) + "\n"
                )
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (path, exc))
                return 1
        verb = "would fix" if dry_run else "fixed"
        for rule in sorted(set(applied)):
            sys.stderr.write("%s %s x%d\n" % (verb, rule, applied.count(rule)))
    if diff_path is not None:
        try:
            other = json.loads(
                sys.stdin.read()
                if diff_path == "-"
                else Path(diff_path).read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError) as exc:
            sys.stdout.write("ERROR P000 $: cannot parse %s (%s)\n" % (diff_path, exc))
            return 2
        if not isinstance(policy, dict) or not isinstance(other, dict):
            sys.stdout.write("ERROR P000 $: --diff needs JSON objects on both sides\n")
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
            rows = _watch.only_filter(lint_policy(policy), only)
            if usage:
                rows += usage_findings(policy)
                rows = _watch.only_filter(rows, only)
            pre_drop = len(rows)
            if baseline_keys is not None:
                rows = _drop_baseline(rows, baseline_keys)
            tick = {
                "ts": int(_time.time()),
                "findings": len(rows),
                "suppressed": pre_drop - len(rows),
                "errors": sum(1 for r in rows if r["severity"] == "error"),
                "warnings": sum(1 for r in rows if r["severity"] == "warn"),
                "infos": sum(1 for r in rows if r["severity"] == "info"),
            }
            _watch.emit_or_jq(tick, jq_value, out_path, quiet=_watch.quiet("JEV_PLINT_WATCH_QUIET", quiet), bad=tick["errors"] or (strict and tick["findings"]))
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
                policy = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                pass
        rc = 1 if (tick.get("errors", 0) or (strict and tick.get("findings", 0))) else 0
        if verdict_path and verdict_ok and not _write_verdict(rc):
            return 1
        return rc
    findings = _watch.only_filter(lint_policy(policy), only)
    if usage:
        findings += usage_findings(policy)
        findings = _watch.only_filter(findings, only)
    if baseline_write and not _write_baseline(
        baseline_write, [{"file": str(path), **f} for f in findings]
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
    shown_rows = [
        f
        for f in findings
        if (not severity or f["severity"] in severity)
        and (not quiet or f["severity"] == "error")
    ]
    errors = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warn")
    infos = sum(1 for f in findings if f["severity"] == "info")
    payload = {
        "path": "<stdin>" if argv and argv[0] == "-" else str(path),
        "findings": shown_rows,
        "errors": errors,
        "warnings": warns,
        "infos": infos,
    }
    if out_path:
        try:
            _atomic_write(Path(out_path),
                json.dumps(payload, indent=2)
                + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d finding(s) to %s\n" % (len(shown_rows), out_path))
    rc = 1 if (errors or (strict and warns)) else 0
    if verdict_path:
        if not _watch.write_verdict(
            verdict_path,
            {
                "verdict": "fail" if rc else "pass",
                "ticks": 1,
                "findings": len(findings),
                "suppressed": n_suppressed,
                "errors": errors,
                "warnings": warns,
                "infos": infos,
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
        shown = []
        for finding in findings:
            if severity and finding["severity"] not in severity:
                continue
            if quiet and finding["severity"] != "error":
                continue
            shown.append(finding)
        if as_md:
            _watch.md_table(shown, ["severity", "rule", "path", "message"])
        else:
            for finding in shown:
                sys.stdout.write(format_finding(finding) + "\n")
    if not quiet and not as_json and not as_md:
        sys.stdout.write("policy_lint: %d error(s), %d warning(s), %d info\n" % (errors, warns, infos))
    return rc


if __name__ == "__main__":
    sys.exit(main())
