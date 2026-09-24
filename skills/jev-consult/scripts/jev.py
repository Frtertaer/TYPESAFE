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
import tempfile
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

try:
    import _watch
except ImportError:
    _watch = None


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
    env_path = os.environ.get("JEV_POLICY", "")
    policy_path = (
        Path(path) if path else Path(env_path) if env_path else skill_root() / "policy.json"
    )
    try:
        with policy_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise SystemExit("policy.json unreadable: %s" % exc) from None
    except json.JSONDecodeError as exc:
        raise SystemExit("policy.json is not JSON: %s" % exc) from None
    if not isinstance(data, dict):
        raise SystemExit("policy.json must be an object")
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


def env_timeout() -> float | None:
    """JEV_TIMEOUT seconds override; None when unset/invalid/non-positive."""
    raw = os.environ.get("JEV_TIMEOUT", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


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
                body = response.read().decode("utf-8", errors="replace")
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
        raise SystemExit("Jev response was not JSON (%s)" % exc) from None
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
    escalate_irrev = bool(
        policy_get(policy, ("escalate_if", "irreversible"), default=True)
    )

    for qid, answer in answers.items():
        if not isinstance(answer, dict):
            notes.append("%s: malformed answer" % qid)
            action = "escalate"
            continue
        qtype = answer.get("type")
        if qtype == "choice":
            picked = answer.get("choice")
            if not isinstance(picked, str) or not picked:
                notes.append("%s: malformed choice" % qid)
                action = "escalate"
                continue
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
            clean_probs: dict[str, float] = {}
            for key, value in probabilities.items():
                try:
                    prob = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(prob):
                    clean_probs[key] = prob
            all_probs[qid] = clean_probs
            gap = top_two_gap(clean_probs)
            if confidence < conf_floor:
                notes.append("%s: low confidence %.3f" % (qid, confidence))
                action = "escalate"
            elif gap < gap_floor:
                notes.append(
                    "%s: top-two gap %.3f; using max probability (%s)"
                    % (qid, gap, picked)
                )
                if irreversible and escalate_irrev:
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
                if irreversible and noul_escalate and escalate_irrev:
                    action = "escalate"
        elif qtype == "score":
            try:
                score_value = float(answer.get("score"))
            except (TypeError, ValueError):
                score_value = math.nan
            if not math.isfinite(score_value):
                notes.append("%s: malformed score" % qid)
                action = "escalate"
                continue
            picks[qid] = score_value
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
                notes.append("%s: score=%s" % (qid, score_value))
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


def _atomic_write(path: Path, text: str) -> None:
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


def write_out(path: str, payload: dict[str, Any]) -> bool:
    """Write the payload JSON to PATH; warn + False on error."""
    try:
        _atomic_write(
            Path(path),
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )
    except OSError as exc:
        sys.stderr.write("cannot write %s: %s\n" % (path, exc))
        return False
    sys.stderr.write("wrote %s\n" % path)
    return True


def jq_lookup(obj, path: str):
    """Dotted-path lookup; (value, True) or (None, False) when any part misses."""
    if _watch is not None:
        return _watch.dig(obj, path)
    # Fallback copy of _watch.dig (lists index numerically; keys containing
    # dots resolve as longest literal matches) for when _watch.py is absent.
    cur = obj
    parts = path.split(".")
    i = 0
    while i < len(parts):
        part = parts[i]
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
            i += 1
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
            i += 1
        elif isinstance(cur, dict):
            hit = False
            for j in range(len(parts), i + 1, -1):
                literal = ".".join(parts[i:j])
                if literal in cur:
                    cur = cur[literal]
                    i = j
                    hit = True
                    break
            if not hit:
                return None, False
        else:
            return None, False
    return cur, True


def _hatch_ids(policy: dict[str, Any]) -> tuple[str, ...]:
    raw = policy_get(policy, ("choice", "hatch_ids"), default=["none", "other"])
    if isinstance(raw, (list, tuple)):
        return tuple(str(h) for h in raw)
    return ("none", "other")


def _all_hatch(answers: dict, policy: dict[str, Any]) -> bool:
    """True when every choice-type answer is a hatch pick (none/other)."""
    hatch = set(_hatch_ids(policy))
    saw_choice = False
    for ans in answers.values():
        if isinstance(ans, dict) and ans.get("type") == "choice":
            saw_choice = True
            if str(ans.get("choice") or "") not in hatch:
                return False
    return saw_choice


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
        for finding in question_lint.lint_request(request, hatch=_hatch_ids(policy)):
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
    retry_none = max(0, getattr(args, "retry_none", 0) or 0)
    attempt = 0
    while True:
        result = post_systemone(
            state,
            questions,
            policy,
            model=request.get("model"),
            timeout=args.timeout or env_timeout() or 60,
            retries=max(0, args.retries),
        )
        answers = result.get("answers") or {}
        if not isinstance(answers, dict):
            raise SystemExit("Jev answers must be an object")
        attempt += 1
        if attempt > retry_none or not _all_hatch(answers, policy):
            break
        sys.stderr.write(
            "ask: all choice answers are hatch picks (attempt %d); retrying\n"
            % attempt
        )
    decision = decide(
        answers,
        policy,
        irreversible=bool(request.get("irreversible", False)),
    )
    payload = {
        "model": result.get("model"),
        "answers": answers,
        "decision": decision,
        "usage": result.get("usage"),
        "warnings": warnings,
    }
    if retry_none:
        payload["ask_attempts"] = attempt
    if getattr(args, "out", "") and not write_out(args.out, payload):
        return 1
    jq_key = getattr(args, "jq", "") or ""
    if jq_key:
        value, found = jq_lookup(payload, jq_key)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (jq_key, ", ".join(sorted(payload)))
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
    else:
        emit(payload)
    verdict_path = getattr(args, "verdict", "") or ""
    if verdict_path and _watch is not None:
        picks = {}
        for qid, ans in answers.items():
            if isinstance(ans, dict) and ans.get("choice") is not None:
                picks[qid] = ans.get("choice")
        _watch.write_verdict(
            verdict_path,
            {
                "verdict": "proceed"
                if decision["action"] == "proceed"
                else "escalate",
                "action": decision["action"],
                "picks": picks,
                "warnings": len(warnings),
            },
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
    payload = {"decision": decision, "warnings": policy_warnings(policy)}
    if getattr(args, "out", "") and not write_out(args.out, payload):
        return 1
    jq_key = getattr(args, "jq", "") or ""
    if jq_key:
        value, found = jq_lookup(payload, jq_key)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (jq_key, ", ".join(sorted(payload)))
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
    else:
        emit(payload)
    verdict_path = getattr(args, "verdict", "") or ""
    if verdict_path and _watch is not None:
        _watch.write_verdict(
            verdict_path,
            {
                "verdict": "proceed"
                if decision["action"] == "proceed"
                else "escalate",
                "action": decision["action"],
            },
        )
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
    findings = question_lint.lint_request(
        request, max_options=max_options, hatch=_hatch_ids(policy)
    )
    errors = sum(1 for f in findings if f["severity"] == "error")
    warns = sum(1 for f in findings if f["severity"] == "warn")
    infos = sum(1 for f in findings if f["severity"] == "info")
    jq_key = getattr(args, "jq", "") or ""
    if jq_key:
        payload = {
            "findings": findings,
            "errors": errors,
            "warnings": warns,
            "infos": infos,
        }
        value, found = jq_lookup(payload, jq_key)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (jq_key, ", ".join(sorted(payload)))
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
    elif getattr(args, "json", False):
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


def _ping_once(policy: dict[str, Any], args: argparse.Namespace) -> dict:
    started = time.time()
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
        timeout=args.timeout or env_timeout() or 60,
        retries=max(0, args.retries),
    )
    answer = (result.get("answers") or {}).get("ok") or {}
    return {
        "ok": True,
        "model": result.get("model"),
        "noul": answer.get("noul"),
        "ms": int((time.time() - started) * 1000),
    }


def cmd_ping(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    watch = getattr(args, "watch", 0.0) or 0.0
    if watch > 0 and _watch is not None:
        max_ticks = _watch.cap("JEV_PING_WATCH_MAX", getattr(args, "max_ticks", 0))
        dead = _watch.deadline("JEV_PING_WATCH_SECS", getattr(args, "watch_max", 0.0))
        quiet = _watch.quiet("JEV_PING_WATCH_QUIET", getattr(args, "quiet", False))
        verdict_path = getattr(args, "verdict", "") or ""
        alert_ms = getattr(args, "alert_ms", 0.0) or 0.0
        ticks = 0
        last_ok = True
        verdict_ok = True
        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = time.time()

        def _write_verdict() -> bool:
            return _watch.write_verdict(
                verdict_path,
                {
                    "verdict": "up" if last_ok else "down",
                    "ticks": ticks,
                    "ok": last_ok,
                    "elapsed_s": round(time.time() - watch_t0, 2),
                },
            )

        while (max_ticks <= 0 or ticks < max_ticks) and (
            not dead or time.time() < dead
        ):
            now = time.time()
            try:
                slim_tick = _ping_once(policy, args)
                tick = {
                    "ts": int(now),
                    "ok": True,
                    "model": slim_tick.get("model"),
                    "noul": slim_tick.get("noul"),
                    "ms": slim_tick.get("ms"),
                    "elapsed_s": round(now - watch_t0, 2),
                }
            except SystemExit as err:
                tick = {
                    "ts": int(now),
                    "ok": False,
                    "error": str(err.code)[:160],
                    "elapsed_s": round(now - watch_t0, 2),
                }
            last_ok = bool(tick.get("ok"))
            if (
                alert_ms > 0
                and last_ok
                and isinstance(tick.get("ms"), (int, float))
                and not isinstance(tick.get("ms"), bool)
                and tick["ms"] > alert_ms
            ):
                tick["slow"] = True
                sys.stderr.write(
                    "watch alert: ms=%s exceeds --alert-ms %s\n"
                    % (tick["ms"], alert_ms)
                )
            _watch.emit_or_jq(
                tick,
                getattr(args, "jq", ""),
                getattr(args, "out", ""),
                quiet=quiet,
                bad=not last_ok,
            )
            ticks += 1
            sys.stderr.write("watch tick=%d ok=%s\n" % (ticks, last_ok))
            if verdict_path and verdict_ok and not _write_verdict():
                verdict_ok = False
            if getattr(args, "fail_fast", False) and not last_ok:
                break
            if _watch.same_tick(prev_tick, tick, ignore=("ts", "elapsed_s", "ms")):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            time.sleep(watch)
        if verdict_path and verdict_ok and not _write_verdict():
            return 1
        if max_ticks and not last_ok:
            return 1
        return 0
    slim = _ping_once(policy, args)
    verdict_path = getattr(args, "verdict", "") or ""
    if verdict_path and _watch is not None:
        _watch.write_verdict(verdict_path, slim)
    if getattr(args, "out", "") and not write_out(args.out, slim):
        return 1
    jq_key = getattr(args, "jq", "") or ""
    if jq_key:
        value, found = jq_lookup(slim, jq_key)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (jq_key, ", ".join(sorted(slim)))
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
        return 0
    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(slim) + "\n")
        return 0
    sys.stdout.write(
        "ok model=%s noul=%s\n" % (slim.get("model"), slim.get("noul"))
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
    plan = _watch.text_arg(plan) if plan else plan
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


def env_report(args: argparse.Namespace) -> dict:
    """Resolved jev.py environment. Presence flags only — never the key."""
    try:
        watch_secs = float(os.environ.get("JEV_PING_WATCH_SECS", "") or 0)
    except ValueError:
        watch_secs = 0.0
    policy = (getattr(args, "policy", "") or "").strip() or os.environ.get(
        "JEV_POLICY", ""
    ).strip()
    return {
        "api_key_set": bool(load_api_key()),
        "policy": policy if policy else "default",
        "timeout_seconds": env_timeout(),
        "watch_max": _watch.cap("JEV_PING_WATCH_MAX", None) if _watch else None,
        "watch_quiet": _watch.quiet("JEV_PING_WATCH_QUIET", False) if _watch else False,
        "watch_secs": watch_secs,
    }


def cmd_env(args: argparse.Namespace) -> int:
    report = env_report(args)
    if args.jq:
        if _watch is not None:
            value, found = _watch.dig(report, args.jq)
        else:
            value, found = report.get(args.jq), args.jq in report
        if found:
            sys.stdout.write(json.dumps(value) + "\n")
            return 0
        sys.stderr.write(
            "bad --jq key %r (env has: %s)\n"
            % (args.jq, ", ".join(sorted(report)))
        )
        return 2
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    sys.stdout.write(text)
    if getattr(args, "out", ""):
        try:
            _atomic_write(Path(args.out), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
    return 0


def cmd_self_test(args: argparse.Namespace) -> int:
    """Offline sanity: scaffold a request, round-trip it through disk, lint it."""
    checks: dict[str, bool] = {}
    try:
        policy = load_policy(args.policy)
        checks["policy_templates"] = bool(policy.get("templates"))
        payload = scaffold_request(
            policy,
            ["approach"],
            {"task": "self-test", "plan": "self-test"},
            {"approach": {"selftest": "self-test option"}},
        )
        questions = payload.get("questions") or {}
        checks["scaffold"] = "approach" in questions
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            _atomic_write(req, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            back = read_json_arg(str(req))
        checks["roundtrip"] = back == payload
        if question_lint is None:
            checks["lint_clean"] = False
        else:
            findings = question_lint.lint_request(
                back,
                max_options=int(
                    policy_get(policy, "choice_option_hard_max", default=255)
                ),
                hatch=_hatch_ids(policy),
            )
            checks["lint_clean"] = not any(
                f.get("severity") == "error" for f in findings
            )
    except Exception:
        checks = {"raised": False}
    ok = bool(checks) and all(checks.values())
    if args.json:
        emit({"self_test": "ok" if ok else "FAIL", "checks": checks})
    else:
        sys.stdout.write(
            "self-test: %s %s\n"
            % (
                "ok" if ok else "FAIL",
                " ".join(
                    "%s=%s" % (k, "ok" if v else "FAIL")
                    for k, v in sorted(checks.items())
                ),
            )
        )
    return 0 if ok else 1


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
    if getattr(args, "dry_run", False):
        sys.stdout.write(
            json.dumps({"dry_run": True, "out": str(out), "request": payload}, ensure_ascii=False) + "\n"
        )
    else:
        try:
            if out.parent != Path(""):
                out.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(out, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            sys.stderr.write("scaffold: cannot write %s: %s\n" % (out, exc))
            return ASK_ESCALATE_EXIT
    if getattr(args, "lint", False) and question_lint is not None:
        findings = question_lint.lint_request(payload, hatch=_hatch_ids(policy))
        for finding in findings:
            sys.stderr.write(
                "lint %s %s: %s\n"
                % (finding["rule"], finding["qid"], finding["message"])
            )
        if any(f["severity"] == "error" for f in findings):
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
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Print the request/response key contract and exit (--json emits the object)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --schema: emit the schema object instead of text rows",
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
    ask.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="HTTP timeout seconds (default JEV_TIMEOUT env or 60)",
    )
    ask.add_argument(
        "--retries",
        type=int,
        default=1,
        help="extra attempts on HTTP 429/5xx (default 1)",
    )
    ask.add_argument(
        "--verdict",
        metavar="PATH",
        default="",
        help="Write a slim {verdict: proceed|escalate, action, picks, warnings} JSON to PATH after the ask (atomic via .tmp+rename). '-' prints it to stdout.",
    )
    ask.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="Print just one dotted-path field of the response (e.g. answers.approach.choice); unknown key exits 2.",
    )
    ask.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Also write the full response JSON to PATH.",
    )
    ask.add_argument(
        "--retry-none",
        metavar="N",
        type=int,
        default=0,
        help="Re-ask up to N extra times when every choice answer is a hatch pick (none/other); response gains ask_attempts",
    )
    ask.set_defaults(func=cmd_ask)
    decide_cmd = sub.add_parser("decide", help="Apply policy to an answers object")
    decide_cmd.add_argument(
        "--verdict",
        metavar="PATH",
        default="",
        help="Write a slim {verdict: proceed|escalate, action} JSON to PATH (atomic via .tmp+rename). '-' prints it to stdout.",
    )
    decide_cmd.add_argument("file", help="JSON file or - for stdin")
    decide_cmd.add_argument(
        "--irreversible",
        action="store_true",
        help="Treat the pending action as hard to undo",
    )
    decide_cmd.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="Print just one dotted-path field (e.g. decision.action); unknown key exits 2.",
    )
    decide_cmd.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Also write the decision payload JSON to PATH.",
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
    lint_cmd.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="Print just one dotted-path field of the lint payload (e.g. errors); unknown key exits 2.",
    )
    lint_cmd.set_defaults(func=cmd_lint)
    ping = sub.add_parser("ping", help="Live connectivity check; prints model and noul only")
    ping.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="HTTP timeout seconds (default JEV_TIMEOUT env or 60)",
    )
    ping.add_argument(
        "--retries",
        type=int,
        default=1,
        help="extra attempts on HTTP 429/5xx (default 1)",
    )
    ping.add_argument(
        "--json",
        action="store_true",
        help="Print {ok, model, noul, ms} as a JSON object",
    )
    ping.add_argument(
        "--verdict",
        metavar="PATH",
        default="",
        help="Write a slim {ok, model, noul, ms} JSON to PATH (atomic via .tmp+rename). '-' prints it to stdout.",
    )
    ping.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="Print just one field of the slim payload (ok|model|noul|ms); unknown key exits 2.",
    )
    ping.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Also write the slim {ok, model, noul, ms} JSON to PATH.",
    )
    ping.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-ping every S seconds, emitting a {ts,ok,model,noul,ms,elapsed_s} tick per pass (failed pings emit {ts,ok:false,error,elapsed_s})",
    )
    ping.add_argument(
        "--max-ticks",
        metavar="N",
        type=int,
        default=0,
        help="With --watch: stop after N ticks (overrides JEV_PING_WATCH_MAX)",
    )
    ping.add_argument(
        "--watch-max",
        metavar="S",
        type=float,
        default=0.0,
        help="With --watch: stop after S elapsed seconds (JEV_PING_WATCH_SECS also caps)",
    )
    ping.add_argument(
        "--quiet",
        action="store_true",
        help="With --watch: print only failing ticks to stdout (--out still logs all)",
    )
    ping.add_argument(
        "--fail-fast",
        action="store_true",
        help="With --watch: stop after the first failed ping tick",
    )
    ping.add_argument(
        "--alert-ms",
        metavar="MS",
        type=float,
        default=0.0,
        help="With --watch: mark ok ticks whose ms exceeds MS with slow=true and log a stderr alert",
    )
    ping.add_argument(
        "--unchanged-max",
        metavar="N",
        type=int,
        default=0,
        help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s/ms ignored)",
    )
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
    scaffold.add_argument("--plan", help="Copied into state.plan (and state.task if missing); '-' reads it from stdin")
    scaffold.add_argument(
        "--option",
        action="append",
        default=[],
        help="ID=key:label extra choice option (repeatable)",
    )
    scaffold.add_argument(
        "--lint",
        action="store_true",
        help="Run question_lint on the scaffolded request; findings to stderr, rc 1 on errors",
    )
    scaffold.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate options + lint without writing --out; prints {dry_run, out, request} JSON",
    )
    scaffold.set_defaults(func=cmd_scaffold)
    selftest = sub.add_parser(
        "self-test",
        help="Offline scaffold+lint round-trip in a temp dir; exit 1 on failure",
    )
    selftest.add_argument(
        "--json",
        action="store_true",
        help="Emit the self-test payload as JSON.",
    )
    selftest.set_defaults(func=cmd_self_test)
    env_cmd = sub.add_parser(
        "env",
        help="Print the resolved env config JSON ({api_key_set presence flag — never the key —, policy, timeout_seconds, watch_*}) and exit (--jq KEY prints one field, rc 2 on unknown)",
    )
    env_cmd.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="Print just one field of the env report.",
    )
    env_cmd.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Also write the env report JSON to PATH (fail-open).",
    )
    env_cmd.set_defaults(func=cmd_env)
    return parser


# Response-side key contract (the request side reuses question_lint.REQUEST_SCHEMA_ROWS).
RESPONSE_SCHEMA_ROWS = {
    "response.model": {"required": False, "type": "string, the Jev model that answered"},
    "response.answers": {"required": True, "type": "object{qid: answer}, one per request question"},
    "response.usage": {"required": False, "type": "object{input_tokens, output_tokens} non-negative ints"},
    "response.warnings": {"required": False, "type": "list[string], server-side warnings"},
    "answer.type": {"required": True, "type": "choice|noul|score, must equal request question.type"},
    "answer.choice": {"required": False, "type": "string option key (choice answers, must be in criteria)"},
    "answer.noul": {"required": False, "type": "number 0..1 (noul answers)"},
    "answer.score": {"required": False, "type": "finite number (score answers)"},
    "answer.confidence": {"required": False, "type": "float 0..1 (required for choice/score)"},
    "answer.probabilities": {"required": False, "type": "object{option: p} keys=criteria, sum to 1 (choice answers)"},
}


def schema_rows() -> dict:
    rows = {
        "request." + key: dict(row)
        for key, row in getattr(question_lint, "REQUEST_SCHEMA_ROWS", {}).items()
    }
    rows["request.model"] = {"required": False, "type": "string, Jev model override (policy default otherwise)"}
    rows.update(RESPONSE_SCHEMA_ROWS)
    return rows


def main(argv: list[str] | None = None) -> int:
    if _watch:
        _watch.fix_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.schema:
        rows = schema_rows()
        if args.json:
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        else:
            for key, row in rows.items():
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, row["type"], "required" if row["required"] else "optional")
                )
        return 0
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
    _watch.exit_safely(main())
