#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Call TypeSafe Jev (System One). Never print API keys or Authorization headers."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ENDPOINT_DEFAULT = "https://api.typesafe.ai/v1/systemone"
ASK_ESCALATE_EXIT = 2


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
        value = value.strip().strip("'").strip('"')
        if key:
            out[key] = value
    return out


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
    hermes = os.environ.get("HERMES_HOME", "").strip()
    if hermes:
        value = parse_env_file(Path(hermes) / ".env").get("TYPESAFE_API_KEY", "").strip()
        if value:
            return value
    raise SystemExit(
        "TYPESAFE_API_KEY is not set. Put it in the environment or a local .env. "
        "Never paste it into chat."
    )


def redact(text: str) -> str:
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)apikey_[A-Za-z0-9_\-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)sk-[A-Za-z0-9_\-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)(TYPESAFE_API_KEY\s*[=:]\s*)\S+", r"\1[REDACTED]", text)
    return text


def validate_questions(questions: Any, policy: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty object")
    hard = int(policy.get("hard_max_questions", 32))
    if len(questions) > hard:
        raise ValueError("at most %s questions per request" % hard)
    soft = int(policy.get("soft_max_questions", 8))
    if len(questions) > soft:
        warnings.append("more than %s questions; keep them independent and narrow" % soft)
    hatch = set(policy.get("choice", {}).get("hatch_ids", ["none", "other"]))
    require_hatch = bool(policy.get("choice", {}).get("require_hatch", True))
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


def post_systemone(
    state: Any,
    questions: dict[str, Any],
    policy: dict[str, Any],
    model: str | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    key = load_api_key()
    payload = {
        "state": state,
        "model": model or policy.get("model") or "jev-latest",
        "questions": questions,
    }
    data = json.dumps(payload).encode("utf-8")
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
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", errors="replace")
        raise SystemExit("Jev HTTP %s: %s" % (err.code, redact(raw)[:500])) from None
    except urllib.error.URLError as err:
        raise SystemExit("Jev network error: %s" % err.reason) from None
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise SystemExit("Jev returned a non-object body")
    return parsed


def top_two_gap(probabilities: dict[str, Any]) -> float:
    values = sorted((float(v) for v in probabilities.values()), reverse=True)
    if len(values) < 2:
        return 1.0
    return values[0] - values[1]


def decide(
    answers: dict[str, Any],
    policy: dict[str, Any],
    irreversible: bool = False,
) -> dict[str, Any]:
    notes: list[str] = []
    picks: dict[str, Any] = {}
    action = "proceed"
    choice_cfg = policy.get("choice", {})
    noul_cfg = policy.get("noul", {})
    score_cfg = policy.get("score", {})
    conf_floor = float(choice_cfg.get("escalate_if_confidence_below", 0.55))
    gap_floor = float(choice_cfg.get("escalate_if_top_two_gap_below", 0.15))
    yes_above = float(noul_cfg.get("yes_above", 0.7))
    no_below = float(noul_cfg.get("no_below", 0.3))
    score_floor = float(score_cfg.get("escalate_if_confidence_below", 0.55))
    noul_escalate = bool(noul_cfg.get("escalate_uncertain_if_irreversible", True))

    for qid, answer in answers.items():
        if not isinstance(answer, dict):
            notes.append("%s: malformed answer" % qid)
            action = "escalate"
            continue
        qtype = answer.get("type")
        if qtype == "choice":
            picked = answer.get("choice")
            picks[qid] = picked
            confidence = float(answer.get("confidence") or 0.0)
            probabilities = answer.get("probabilities") or {}
            if not isinstance(probabilities, dict):
                probabilities = {}
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
            probability = float(answer.get("noul"))
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
            confidence = float(answer.get("confidence") or 0.0)
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
        "notes": notes,
        "irreversible": irreversible,
    }


def read_json_arg(file_arg: str) -> Any:
    if file_arg == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(file_arg).read_text(encoding="utf-8")
    return json.loads(raw)


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
    warnings = validate_questions(questions, policy)
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
    emit({"decision": decision})
    if decision["action"] != "proceed":
        return ASK_ESCALATE_EXIT
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask TypeSafe Jev and apply jev-consult policy. Never prints secrets."
    )
    parser.add_argument("--policy", help="Path to policy.json (defaults to skill policy.json)")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="POST state+questions, print answers and decision")
    ask.add_argument("file", help="JSON file or - for stdin")
    ask.set_defaults(func=cmd_ask)
    decide_cmd = sub.add_parser("decide", help="Apply policy to an answers object")
    decide_cmd.add_argument("file", help="JSON file or - for stdin")
    decide_cmd.add_argument(
        "--irreversible",
        action="store_true",
        help="Treat the pending action as hard to undo",
    )
    decide_cmd.set_defaults(func=cmd_decide)
    ping = sub.add_parser("ping", help="Live connectivity check; prints model and noul only")
    ping.set_defaults(func=cmd_ping)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
