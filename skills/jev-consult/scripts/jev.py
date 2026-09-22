#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Call TypeSafe Jev (System One). Never print API keys or Authorization headers."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

try:
    import question_lint
except ImportError:
    question_lint = None

try:
    import policy_lint
except ImportError:
    policy_lint = None


def policy_warnings(policy: dict[str, Any]) -> list[str]:
    if policy_lint is None:
        return ["policy_lint unavailable; static policy checks skipped"]
    warnings: list[str] = []
    for finding in policy_lint.lint_policy(policy):
        if finding["severity"] in ("error", "warn"):
            warnings.append(
                "policy_lint %s %s: %s" % (finding["rule"], finding["path"], finding["message"])
            )
    return warnings

ENDPOINT_DEFAULT = "https://api.typesafe.ai/v1/systemone"
ASK_ESCALATE_EXIT = 2
SECRET_RE = re.compile(
    r"apikey_[A-Za-z0-9]{20,}_[A-Za-z0-9]{20,}"
    r"|(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{20,}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def load_policy(path: str | None = None) -> dict[str, Any]:
    policy_path = Path(path) if path else skill_root() / "policy.json"
    with policy_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("policy.json must be an object")
    return data


def parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            close = value.find(quote, 1)
            value = value[1:close] if close > 0 else value.strip("'\"")
        else:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def extra_env_files() -> list[Path]:
    files: list[Path] = []
    hermes = os.environ.get("HERMES_HOME", "").strip()
    if hermes:
        files.append(Path(hermes) / ".env")
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
    files.append(home / ".hermes" / ".env")
    files.append(Path("D:/Hermes/home") / ".env")
    files.append(home / ".env")
    return files


def load_api_key() -> str:
    env_val = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if env_val:
        return env_val
    cwd = Path.cwd()
    seen: set[Path] = set()
    for folder in [cwd, *cwd.parents]:
        resolved = folder.resolve()
        if resolved in seen:
            break
        seen.add(resolved)
        value = parse_env_file(folder / ".env").get("TYPESAFE_API_KEY", "").strip()
        if value:
            return value
        if (folder / ".git").exists():
            break
    for path in extra_env_files():
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        value = parse_env_file(path).get("TYPESAFE_API_KEY", "").strip()
        if value:
            return value
    raise SystemExit(
        "TYPESAFE_API_KEY is not set. Put it in the environment or a local .env. "
        "Never paste it into chat."
    )


def redact(text: str) -> str:
    text = SECRET_RE.sub("[REDACTED]", text)
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)apikey_[A-Za-z0-9_\-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)sk-[A-Za-z0-9_\-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(TYPESAFE_API_KEY\s*[=:]\s*)\S+", r"\1[REDACTED]", text)
    return text


def policy_get(policy: dict[str, Any], *paths: Any, default: Any = None) -> Any:
    """First present path wins. A path is a top-level key or a nested tuple."""
    for path in paths:
        keys = (path,) if isinstance(path, str) else path
        cur: Any = policy
        found = True
        for key in keys:
            if not isinstance(cur, dict) or key not in cur:
                found = False
                break
            cur = cur[key]
        if found and cur is not None:
            return cur
    return default


def validate_questions(questions: Any, policy: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty object")
    hard = int(policy_get(policy, "question_hard_max", "hard_max_questions", default=32))
    if len(questions) > hard:
        raise ValueError("at most %s questions per request" % hard)
    soft = int(policy_get(policy, "question_soft_max", "soft_max_questions", default=8))
    if len(questions) > soft:
        warnings.append("more than %s questions; keep them independent and narrow" % soft)
    hatch = set(policy_get(policy, ("choice", "hatch_ids"), default=["none", "other"]))
    require_hatch = bool(policy_get(policy, "require_hatch", ("choice", "require_hatch"), default=True))
    for qid, question in questions.items():
        if not isinstance(question, dict):
            raise ValueError("%s: question must be an object" % qid)
        qtype = question.get("type")
        if qtype not in ("choice", "noul", "score"):
            raise ValueError("%s: type must be choice|noul|score" % qid)
        if not question.get("instructions"):
            raise ValueError("%s: instructions required" % qid)
        if qtype == "choice":
            criteria = question.get("criteria")
            if not isinstance(criteria, dict) or len(criteria) < 2:
                raise ValueError("%s: choice needs criteria map with >=2 options" % qid)
            if require_hatch and not (hatch & set(criteria)):
                warnings.append(
                    "%s: no hatch option (none/other); add one if doing nothing is possible"
                    % qid
                )
        elif qtype == "score":
            criteria = question.get("criteria")
            if not isinstance(criteria, list) or len(criteria) < 2:
                raise ValueError("%s: score needs criteria list with >=2 levels" % qid)
    return warnings


def _finite(value: Any, low: float | None = None, high: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    if low is not None and value < low:
        return False
    if high is not None and value > high:
        return False
    return True


def validate_response(parsed: dict, questions: dict) -> dict:
    """Reject malformed/mismatched Jev responses before policy sees them."""
    if not isinstance(parsed, dict):
        raise SystemExit("Jev response invalid: not an object")
    answers = parsed.get("answers")
    if not isinstance(answers, dict):
        raise SystemExit("Jev response invalid: answers must be an object")
    extra = set(answers) - set(questions or {})
    if extra:
        raise SystemExit("Jev response invalid: unexpected answers %s" % sorted(extra))
    for qid, question in (questions or {}).items():
        if not isinstance(question, dict):
            continue
        answer = answers.get(qid)
        qtype = question.get("type")
        if not isinstance(answer, dict) or answer.get("type") != qtype:
            raise SystemExit("Jev response invalid: %s missing or mismatched type" % qid)
        if qtype == "choice":
            criteria = question.get("criteria") or {}
            probs = answer.get("probabilities")
            if answer.get("choice") not in criteria:
                raise SystemExit("Jev response invalid: %s choice not in criteria" % qid)
            if not _finite(answer.get("confidence"), 0, 1):
                raise SystemExit("Jev response invalid: %s confidence out of range" % qid)
            if not isinstance(probs, dict) or set(probs) != set(criteria):
                raise SystemExit("Jev response invalid: %s probabilities keys" % qid)
            if not all(_finite(value, 0, 1) for value in probs.values()):
                raise SystemExit("Jev response invalid: %s probability out of range" % qid)
            if abs(sum(probs.values()) - 1.0) > 0.06:
                raise SystemExit("Jev response invalid: %s probabilities do not sum to 1" % qid)
        elif qtype == "noul":
            if not _finite(answer.get("noul"), 0, 1):
                raise SystemExit("Jev response invalid: %s noul out of range" % qid)
        elif qtype == "score":
            if not _finite(answer.get("score")):
                raise SystemExit("Jev response invalid: %s score not finite" % qid)
            if not _finite(answer.get("confidence"), 0, 1):
                raise SystemExit("Jev response invalid: %s score confidence out of range" % qid)
    usage = parsed.get("usage")
    if usage is not None:
        if not isinstance(usage, dict) or any(
            isinstance(usage.get(k), bool)
            or not isinstance(usage.get(k), int)
            or usage.get(k) < 0
            for k in ("input_tokens", "output_tokens")
        ):
            raise SystemExit("Jev response invalid: usage tokens")
    result = {
        "model": parsed.get("model"),
        "answers": answers,
        "usage": usage,
    }
    if "warnings" in parsed:
        result["warnings"] = parsed["warnings"]
    return result


def post_systemone(
    state: Any,
    questions: dict[str, Any],
    policy: dict[str, Any],
    model: str | None = None,
    timeout: float = 60,
    retries: int = 1,
) -> dict[str, Any]:
    key = load_api_key()
    payload = {
        "state": state,
        "model": model or policy.get("model") or "jev-latest",
        "questions": questions,
    }
    text = json.dumps(payload, ensure_ascii=False)
    if SECRET_RE.search(text) or (key and key in text):
        raise SystemExit("Jev request blocked: suspected credential in state/questions")
    data = text.encode("utf-8")
    request = urllib.request.Request(
        policy.get("endpoint") or ENDPOINT_DEFAULT,
        data=data,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "jev-consult/1.0",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    attempts = 0
    while True:
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as err:
            if 300 <= err.code < 400:
                raise SystemExit("Jev redirect blocked (HTTP %d)" % err.code) from None
            if (err.code == 429 or err.code >= 500) and attempts < retries:
                attempts += 1
                time.sleep(2)
                continue
            raw = err.read().decode("utf-8", errors="replace")
            raise SystemExit("Jev HTTP %s: %s" % (err.code, redact(raw)[:500])) from None
        except urllib.error.URLError as err:
            raise SystemExit("Jev network error: %s" % err.reason) from None
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise SystemExit("Jev response invalid: not JSON (%s)" % exc) from None
    return validate_response(parsed, questions)


def top_two_gap(probabilities: dict[str, Any]) -> float:
    values = sorted(
        (float(v) for v in probabilities.values() if _finite(v)), reverse=True
    )
    if len(values) < 2:
        return 1.0
    return values[0] - values[1]


def _pfloat(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def decide(
    answers: dict[str, Any],
    policy: dict[str, Any],
    irreversible: bool = False,
) -> dict[str, Any]:
    notes: list[str] = []
    picks: dict[str, Any] = {}
    all_probs: dict[str, Any] = {}
    action = "proceed"
    conf_floor = _pfloat(
        policy_get(
            policy,
            "confidence_floor",
            ("escalate_if", "confidence_below"),
            ("choice", "escalate_if_confidence_below"),
            default=0.55,
        ),
        0.55,
    )
    gap_floor = _pfloat(
        policy_get(
            policy,
            "tight_gap",
            ("escalate_if", "choice_gap_below"),
            ("choice", "escalate_if_top_two_gap_below"),
            default=0.15,
        ),
        0.15,
    )
    yes_above = _pfloat(policy_get(policy, "noul_yes", ("noul", "yes_above"), default=0.7), 0.7)
    no_below = _pfloat(policy_get(policy, "noul_no", ("noul", "no_below"), default=0.3), 0.3)
    score_floor = _pfloat(
        policy_get(
            policy,
            "confidence_floor",
            ("escalate_if", "confidence_below"),
            ("score", "escalate_if_confidence_below"),
            default=0.55,
        ),
        0.55,
    )
    noul_escalate = bool(
        policy_get(policy, ("noul", "escalate_uncertain_if_irreversible"), default=True)
    )

    for qid, answer in answers.items():
        if not isinstance(answer, dict):
            notes.append("%s: malformed answer" % qid)
            action = "escalate"
            continue
        qtype = answer.get("type")
        if qtype == "choice":
            picked = answer.get("choice")
            picks[qid] = picked
            try:
                confidence = float(answer.get("confidence"))
            except (TypeError, ValueError):
                confidence = 0.0
            if not math.isfinite(confidence):
                confidence = 0.0
            probabilities = answer.get("probabilities") or {}
            if not isinstance(probabilities, dict):
                probabilities = {}
            all_probs[qid] = probabilities
            gap = top_two_gap(probabilities)
            if confidence < conf_floor:
                notes.append("%s: low confidence %.3f" % (qid, confidence))
                action = "escalate"
            elif gap < gap_floor:
                notes.append(
                    "%s: top-two gap %.3f; using max probability (%s)"
                    % (qid, gap, picked)
                )
                if irreversible:
                    action = "escalate"
            else:
                notes.append("%s: %s (max probability)" % (qid, picked))
        elif qtype == "noul":
            try:
                probability = float(answer.get("noul"))
            except (TypeError, ValueError):
                probability = math.nan
            if not _finite(probability, 0, 1):
                notes.append("%s: malformed noul" % qid)
                action = "escalate"
                continue
            picks[qid] = probability
            if probability >= yes_above:
                notes.append("%s: yes (%.3f)" % (qid, probability))
            elif probability <= no_below:
                notes.append("%s: no (%.3f)" % (qid, probability))
            else:
                notes.append(
                    "%s: uncertain noul=%.3f (0.5 means equally yes/no, not medium)"
                    % (qid, probability)
                )
                if irreversible and noul_escalate:
                    action = "escalate"
        elif qtype == "score":
            picks[qid] = answer.get("score")
            try:
                confidence = float(answer.get("confidence"))
            except (TypeError, ValueError):
                confidence = 0.0
            if not math.isfinite(confidence):
                confidence = 0.0
            if confidence < score_floor:
                notes.append("%s: low score confidence %.3f" % (qid, confidence))
                action = "escalate"
            else:
                notes.append("%s: score=%s" % (qid, answer.get("score")))
        else:
            notes.append("%s: unknown answer type %s" % (qid, qtype))
            action = "escalate"
    return {
        "action": action,
        "picks": picks,
        "probabilities": all_probs,
        "notes": notes,
        "irreversible": irreversible,
    }


def apply_trace(state: Any, trace_path: str | None) -> Any:
    if not trace_path:
        return state
    path = Path(__file__).resolve().parent / "trace.py"
    spec = importlib.util.spec_from_file_location("jev_trace", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.merge_state(state, mod.load(Path(trace_path)))


def read_json_arg(file_arg: str) -> Any:
    try:
        raw = sys.stdin.read() if file_arg == "-" else Path(file_arg).read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit("cannot read %s: %s" % (file_arg, exc))
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SystemExit("%s is not JSON: %s" % (file_arg, exc))


def emit(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_ask(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    request = read_json_arg(args.file)
    if not isinstance(request, dict):
        raise SystemExit("request JSON must be an object")
    state = request.get("state")
    questions = request.get("questions")
    if state is None or not questions:
        raise SystemExit("request needs state and questions")
    state = apply_trace(state, getattr(args, "trace", None))
    warnings = policy_warnings(policy)
    warnings += validate_questions(questions, policy)
    if question_lint is not None:
        for finding in question_lint.lint_request(request):
            if finding["severity"] in ("error", "warn"):
                warnings.append(
                    "lint %s %s: %s" % (finding["rule"], finding["qid"], finding["message"])
                )
    else:
        warnings.append("question_lint unavailable; static wording checks skipped")
    if getattr(args, "dry", False):
        emit(
            {
                "dry": True,
                "model": request.get("model"),
                "state": state,
                "questions": questions,
                "warnings": warnings,
            }
        )
        return 0
    result = post_systemone(state, questions, policy, model=request.get("model"))
    answers = result.get("answers") or {}
    if not isinstance(answers, dict):
        raise SystemExit("Jev answers must be an object")
    decision = decide(
        answers,
        policy,
        irreversible=bool(request.get("irreversible", False)),
    )
    emit(
        {
            "model": result.get("model"),
            "answers": answers,
            "decision": decision,
            "usage": result.get("usage"),
            "warnings": warnings,
        }
    )
    if decision["action"] != "proceed":
        return ASK_ESCALATE_EXIT
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    payload = read_json_arg(args.file)
    if not isinstance(payload, dict):
        raise SystemExit("decide JSON must be an object")
    answers = payload.get("answers", payload)
    if not isinstance(answers, dict):
        raise SystemExit("answers must be an object")
    irreversible = bool(payload.get("irreversible", args.irreversible))
    decision = decide(answers, policy, irreversible=irreversible)
    emit({"decision": decision, "warnings": policy_warnings(policy)})
    if decision["action"] != "proceed":
        return ASK_ESCALATE_EXIT
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    request = read_json_arg(args.file)
    if not isinstance(request, dict):
        raise SystemExit("request JSON must be an object")
    import question_lint

    policy = load_policy(args.policy)
    max_options = int(
        policy_get(policy, "choice_option_hard_max", default=255)
    )
    findings = question_lint.lint_request(request, max_options=max_options)
    errors = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warn")
    infos = sum(1 for f in findings if f["severity"] == "info")
    if getattr(args, "json", False):
        emit(
            {
                "findings": findings,
                "errors": errors,
                "warnings": warns,
                "infos": infos,
            }
        )
    else:
        for finding in findings:
            sys.stdout.write(question_lint.format_finding(finding) + "\n")
        sys.stdout.write(
            "lint: %d error(s), %d warning(s), %d info\n" % (errors, warns, infos)
        )
    if errors or (args.strict and warns):
        return 1
    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    result = post_systemone(
        state="ping from jev-consult CLI; connectivity check, not a coding decision",
        questions={
            "ok": {
                "type": "noul",
                "instructions": "Is this a connectivity check rather than a user coding task?",
                "criteria": {
                    "true": "This is only a ping",
                    "false": "This is a real coding decision",
                },
            }
        },
        policy=policy,
    )
    answer = (result.get("answers") or {}).get("ok") or {}
    sys.stdout.write(
        "ok model=%s noul=%s\n" % (result.get("model"), answer.get("noul"))
    )
    return 0


def parse_option(raw: str) -> tuple[str, str, str]:
    text = (raw or "").strip()
    if "=" not in text:
        raise ValueError("option must be ID=key:label")
    qid, rest = text.split("=", 1)
    if ":" not in rest:
        raise ValueError("option must be ID=key:label")
    key, label = rest.split(":", 1)
    qid, key, label = qid.strip(), key.strip(), label.strip()
    if not qid or not key or not label:
        raise ValueError("option must be ID=key:label")
    return qid, key, label


def load_scaffold_state(path: str | None, plan: str | None) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if path:
        raw = read_json_arg(path)
        if not isinstance(raw, dict):
            raise ValueError("state JSON must be an object")
        inner = raw.get("state")
        if isinstance(inner, dict):
            state = dict(inner)
        else:
            state = dict(raw)
    if plan:
        state.setdefault("plan", plan)
        state.setdefault("task", plan)
    return state


def scaffold_request(
    policy: dict[str, Any],
    names: list[str],
    state: dict[str, Any],
    extra: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    templates = policy.get("templates") or {}
    extra = extra or {}
    ordered: list[str] = []
    for name in names:
        if name not in ordered:
            ordered.append(name)
    unknown = [name for name in extra if name not in ordered]
    if unknown:
        raise ValueError("option id not in templates: %s" % ", ".join(unknown))
    hatch_key = str(policy_get(policy, "hatch_key", ("hatch", "key"), default="none"))
    hatch_label = str(
        policy_get(policy, "hatch_label", ("hatch", "label"), default="none of these")
    )
    questions: dict[str, Any] = {}
    missing: list[str] = []
    for name in ordered:
        tmpl = templates.get(name)
        if not isinstance(tmpl, dict):
            raise ValueError("unknown template: %s" % name)
        qtype = tmpl.get("type")
        extra_opts = extra.get(name) or {}
        if qtype == "noul" and extra_opts:
            raise ValueError("noul template %s does not take --option" % name)
        question: dict[str, Any] = {
            "type": qtype,
            "instructions": tmpl.get("instructions"),
        }
        if qtype == "choice":
            criteria = dict(tmpl.get("criteria") or {})
            criteria.update(extra_opts)
            if hatch_key not in criteria:
                criteria[hatch_key] = hatch_label
            if len(criteria) < 2:
                missing.append(name)
            question["criteria"] = criteria
        questions[name] = question
    if missing:
        raise ValueError(
            "choice %s needs --option ID=key:label (empty template criteria)"
            % ", ".join(missing)
        )
    validate_questions(questions, policy)
    return {"state": state, "questions": questions}


def cmd_scaffold(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    if getattr(args, "list", False):
        templates = policy.get("templates") or {}
        for name in sorted(templates):
            sys.stdout.write("%s\n" % name)
        return 0
    if not args.templates:
        sys.stderr.write("scaffold: template id(s) required (or --list)\n")
        return ASK_ESCALATE_EXIT
    if not getattr(args, "out", None):
        sys.stderr.write("scaffold: --out is required\n")
        return ASK_ESCALATE_EXIT
    extra: dict[str, dict[str, str]] = {}
    try:
        for raw in args.option or []:
            qid, key, label = parse_option(raw)
            extra.setdefault(qid, {})[key] = label
        state = load_scaffold_state(getattr(args, "state", None), getattr(args, "plan", None))
        payload = scaffold_request(policy, list(args.templates), state, extra)
    except ValueError as exc:
        sys.stderr.write("%s\n" % exc)
        return ASK_ESCALATE_EXIT
    out = Path(args.out)
    try:
        if out.parent != Path(""):
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        sys.stderr.write("scaffold: cannot write %s: %s\n" % (out, exc))
        return ASK_ESCALATE_EXIT
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask TypeSafe Jev and apply jev-consult policy. Never prints secrets."
    )
    parser.add_argument("--policy", help="Path to policy.json (defaults to skill policy.json)")
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the jev-consult policy version and exit.",
    )
    sub = parser.add_subparsers(dest="command")
    ask = sub.add_parser("ask", help="POST state+questions, print answers and decision")
    ask.add_argument("file", help="JSON file or - for stdin")
    ask.add_argument(
        "--trace",
        nargs="?",
        const=".jev-trace.json",
        default=None,
        help="Merge trace JSON into state (default .jev-trace.json)",
    )
    ask.add_argument(
        "--dry",
        action="store_true",
        help="Validate and print the resolved request; no API call, no key needed.",
    )
    ask.set_defaults(func=cmd_ask)
    decide_cmd = sub.add_parser("decide", help="Apply policy to an answers object")
    decide_cmd.add_argument("file", help="JSON file or - for stdin")
    decide_cmd.add_argument(
        "--irreversible",
        action="store_true",
        help="Treat the pending action as hard to undo",
    )
    decide_cmd.set_defaults(func=cmd_decide)
    lint_cmd = sub.add_parser(
        "lint", help="Static wording checks on a request JSON (no API call)"
    )
    lint_cmd.add_argument("file", help="JSON file or - for stdin")
    lint_cmd.add_argument(
        "--strict", action="store_true", help="Warnings also fail the lint"
    )
    lint_cmd.add_argument(
        "--json", action="store_true", help="Machine-readable findings"
    )
    lint_cmd.set_defaults(func=cmd_lint)
    ping = sub.add_parser("ping", help="Live connectivity check; prints model and noul only")
    ping.set_defaults(func=cmd_ping)
    scaffold = sub.add_parser(
        "scaffold",
        help="Write request.json from policy templates (does not call the API)",
    )
    scaffold.add_argument("templates", nargs="*", help="Template ids from policy.json")
    scaffold.add_argument(
        "--list", action="store_true", help="Print known template ids and exit"
    )
    scaffold.add_argument("--out", help="Path to write request.json")
    scaffold.add_argument("--state", help="JSON file or - for stdin (object becomes state)")
    scaffold.add_argument("--plan", help="Copied into state.plan (and state.task if missing)")
    scaffold.add_argument(
        "--option",
        action="append",
        default=[],
        help="ID=key:label extra choice option (repeatable)",
    )
    scaffold.set_defaults(func=cmd_scaffold)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        try:
            version = load_policy(args.policy).get("version", "?")
        except Exception:
            version = "?"
        sys.stdout.write("jev-consult (policy v%s)\n" % version)
        return 0
    if not hasattr(args, "func"):
        parser.error("a command is required")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
