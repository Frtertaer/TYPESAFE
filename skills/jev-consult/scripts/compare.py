#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Score canned unguarded vs guarded traces. Live uses Jev Noul; default is offline."""
from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _watch  # noqa: E402

# Evidence lines a coder-alone baseline could never contain: the Jev
# consult's own outputs (picks, traces, sidecars).
_JEV_EVIDENCE = re.compile(r"\bjev\b|\.jev-", re.IGNORECASE)

# Live-eval errors are jev/HTTP exceptions — they can echo the request
# (headers, URL, body) including TYPESAFE_API_KEY. Reports land in job
# artifacts and summaries, so scrub secret-shaped values at capture.
_REDACT_ASSIGN = re.compile(
    r"([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Za-z0-9_]*"
    r"\s*[=:]\s*)\S+",
    re.IGNORECASE,
)
_REDACT_BLOB = re.compile(
    r"sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_-]{12,}|(?i:bearer\s+\S+)"
)
_REDACT_TOKEN = re.compile(r"[A-Za-z0-9_+./=-]{24,}")


def _redact(text: str) -> str:
    text = _REDACT_ASSIGN.sub(lambda m: m.group(1) + "<redacted>", text)
    return _REDACT_TOKEN.sub(
        "<redacted>", _REDACT_BLOB.sub("<redacted>", text)
    )

# Plausible installed-items pool for observed routing on negative cases:
# the corpus's "routing_pool" overrides it. The items mimic a typical
# dev-tooling install — specific enough that a mechanical prompt matching
# one of them genuinely is a false-accept signal.
DEFAULT_ROUTING_POOL = [
    {
        "id": "skill:dep-audit",
        "kind": "skill",
        "name": "dep-audit",
        "description": "audit and upgrade project dependencies",
    },
    {
        "id": "skill:db-migrate",
        "kind": "skill",
        "name": "db-migrate",
        "description": "plan and apply database schema migrations",
    },
    {
        "id": "skill:api-contract",
        "kind": "skill",
        "name": "api-contract",
        "description": "design and check REST API contracts",
    },
    {
        "id": "mcp:ci-watch",
        "kind": "mcp",
        "name": "ci-watch",
        "description": "watch CI runs and surface failures",
    },
]


def _load_module(filename: str, modname: str):
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def load_jev():
    return _load_module("jev.py", "jev_consult_jev")


_INV_MOD = None


def load_inventory():
    global _INV_MOD
    if _INV_MOD is None:
        _INV_MOD = _load_module("inventory.py", "jev_consult_inventory")
    return _INV_MOD


def observed_consult(case: dict[str, Any], pool: list[dict]) -> bool | None:
    """The hook's real routing decision for the case's prompt: the same
    explicit_mentions -> shortlist -> chooser gate inventory_hook.handle()
    applies — True when the chooser would be invoked (a Jev call spent),
    False when the prompt shortlists nothing or resolves by explicit
    mention. Returns None when inventory cannot load here."""
    prompt = str(case.get("prompt") or "").strip()
    if not prompt:
        return None
    try:
        inv = load_inventory()
        hits = inv.explicit_mentions(prompt, pool)
        if len(hits) == 1:
            return False  # explicit $name mention resolves without Jev
        picked = inv.shortlist(
            pool,
            prompt,
            inv.hook_limit(),
            [str(h.get("name") or "") for h in hits],
        )
        return bool(picked)
    except Exception:
        return None


def observed_route(case: dict[str, Any], pool: list[dict]) -> str | None:
    """The hook's route label for the case's prompt — the same branch order
    inventory_hook.handle() applies: 'explicit' (a single named item wins
    without a Jev call), 'explicit_consult' (a policy.json
    explicit_consult_tokens phrase matched — the consult bypass), 'idf'
    (shortlist only) or 'none' (nothing shortlisted). Returns None when
    inventory cannot load here."""
    prompt = str(case.get("prompt") or "").strip()
    if not prompt:
        return None
    try:
        inv = load_inventory()
        hits = inv.explicit_mentions(prompt, pool)
        if len(hits) == 1:
            return "explicit"
        if inv.explicit_consult(prompt):
            return "explicit_consult"
        picked = inv.shortlist(
            pool,
            prompt,
            inv.hook_limit(),
            [str(h.get("name") or "") for h in hits],
        )
        return "idf" if picked else "none"
    except Exception:
        return None


def cases_path() -> Path:
    return HERE.parent / "examples" / "compare-cases.json"


def load_cases(path: Path | str | None = None) -> dict[str, Any]:
    try:
        if path is not None and str(path) == "-":
            data = json.loads(sys.stdin.read())
        else:
            data = json.loads((Path(path) if path else cases_path()).read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit("cannot read cases: %s" % exc)
    except ValueError as exc:
        raise SystemExit("cases file is not JSON: %s" % exc)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise SystemExit("compare-cases.json must have a cases list")
    for case in data["cases"]:
        if not isinstance(case, dict):
            raise SystemExit("each case must be an object")
        if "expect_call" in case and not isinstance(case["expect_call"], bool):
            raise SystemExit(
                "case %r: expect_call must be a JSON boolean — a truthy string "
                "silently reads as 'call expected'" % case.get("id")
            )
        if "explicit_consult" in case and not isinstance(case["explicit_consult"], bool):
            raise SystemExit(
                "case %r: explicit_consult must be a JSON boolean — the case "
                "marks whether the prompt takes the explicit_consult route"
                % case.get("id")
            )
    return data


def side_state(side: dict[str, Any]) -> dict[str, Any]:
    skip = {"called_jev", "invented", "last_pick"}
    return {key: value for key, value in side.items() if key not in skip}


def build_request(policy: dict[str, Any], case: dict[str, Any], side: dict[str, Any]) -> dict[str, Any]:
    qid = str(case.get("score") or "on_track")
    templates = policy.get("templates") or {}
    question = templates.get(qid)
    if not isinstance(question, dict):
        raise SystemExit("policy is missing template %s" % qid)
    return {"state": side_state(side), "questions": {qid: dict(question)}}


def noul_value(answers: dict[str, Any], qid: str) -> float | None:
    item = answers.get(qid) or {}
    if not isinstance(item, dict):
        return None
    raw = item.get("noul")
    if raw is None:
        raw = item.get("value")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def score_live(jev, policy: dict[str, Any], case: dict[str, Any], side: dict[str, Any]) -> dict[str, Any]:
    request = build_request(policy, case, side)
    parsed = jev.post_systemone(request["state"], request["questions"], policy)
    answers = parsed.get("answers") or {}
    qid = str(case.get("score") or "on_track")
    decided = jev.decide(answers if isinstance(answers, dict) else {}, policy)
    return {
        "noul": noul_value(answers if isinstance(answers, dict) else {}, qid),
        "model": parsed.get("model"),
        "action": decided.get("action"),
        "note": parsed.get("note"),
    }


def _eval_log_path() -> Path:
    env = os.environ.get("JEV_CONSULT_LOG", "").strip()
    if env:
        return Path(env)
    home = Path(
        os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    )
    return home / ".cache" / "jev-consult" / "decisions.jsonl"


def log_eval_call(
    case: dict[str, Any],
    after: dict[str, Any],
    latency_ms: float,
    policy: dict[str, Any],
) -> None:
    """Append one routing-style record per live-scored case so the weekly
    CI run leaves a real decisions.jsonl -- decisions.py --acceptance/--html
    then reports on actual eval traffic, and the artifact accumulates
    calibration data. Entries are tagged harness=live-eval + note=eval so
    they stay separable from real harness records. Never raises."""
    try:
        import hashlib

        prompt = str(case.get("prompt") or case.get("id") or "")
        sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        entry: dict[str, Any] = {
            "ts": time.time(),
            "harness": "live-eval",
            "jev_status": "winner",
            "jev_attempted": True,
            "prompt_sha": sha,
            "prompt_head": prompt[:120],
            "question": str(case.get("score") or "on_track"),
            "winner": {
                "kind": "eval",
                "name": str(
                    (case.get("after") or {}).get("last_pick")
                    or after.get("action")
                    or "unknown"
                ),
            },
            "latency_ms": int(latency_ms),
            "budget_ms": int(
                float(policy.get("hook_budget_seconds") or 12) * 1000
            ),
            "note": after.get("note") or "eval",
            "noul": after.get("noul"),
            "case": case.get("id"),
        }
        path = _eval_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def row_offline(case: dict[str, Any]) -> dict[str, Any]:
    before = case.get("before") or {}
    after = case.get("after") or {}
    row = {
        "id": case.get("id"),
        "defect": case.get("defect"),
        "score": case.get("score"),
        "prompt": case.get("prompt"),
        "before": {
            "called_jev": bool(before.get("called_jev")),
            "invented": before.get("invented"),
            "step": before.get("current_step"),
        },
        "after": {
            "called_jev": bool(after.get("called_jev")),
            "last_pick": after.get("last_pick"),
            "step": after.get("current_step"),
        },
    }
    for key in ("min_noul", "max_noul", "expect_call", "explicit_consult", "note", "ab"):
        if key in case:
            row[key] = case[key]
    return row


def format_table(rows: list[dict[str, Any]], live: bool) -> str:
    lines = [
        "goal: cut stall / unknown / forget / drift — not IQ",
        "",
    ]
    header = (
        "case        defect              called_jev  before_noul  after_noul"
        if live
        else "case        defect              before_jev  after_jev  after_pick"
    )
    lines.append(header)
    for row in rows:
        cid = str(row.get("id") or "")[:11]
        defect = str(row.get("defect") or "")[:18]
        before = row.get("before") or {}
        after = row.get("after") or {}
        if live:
            bn = before.get("noul")
            an = after.get("noul")
            lines.append(
                "%-11s %-18s %-10s %-12s %s"
                % (
                    cid,
                    defect,
                    "yes" if after.get("called_jev") else "no",
                    "-" if bn is None else "%.2f" % bn,
                    "-" if an is None else "%.2f" % an,
                )
            )
        else:
            lines.append(
                "%-11s %-18s %-10s %-9s %s"
                % (
                    cid,
                    defect,
                    "no" if not before.get("called_jev") else "yes",
                    "yes" if after.get("called_jev") else "no",
                    after.get("last_pick") or "-",
                )
            )
    lines.append("")
    lines.append("No watchdog: if the coder skips the skill, the after-path does not run.")
    return "\n".join(lines) + "\n"


def format_md(rows: list[dict[str, Any]], live: bool) -> str:
    if live:
        header = ["case", "defect", "called_jev", "before_noul", "after_noul"]
    else:
        header = ["case", "defect", "before_jev", "after_jev", "after_pick"]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    for row in rows:
        before = row.get("before") or {}
        after = row.get("after") or {}
        if live:
            bn = before.get("noul")
            an = after.get("noul")
            cells = [
                str(row.get("id") or ""),
                str(row.get("defect") or ""),
                "yes" if after.get("called_jev") else "no",
                "-" if bn is None else "%.2f" % bn,
                "-" if an is None else "%.2f" % an,
            ]
        else:
            cells = [
                str(row.get("id") or ""),
                str(row.get("defect") or ""),
                "yes" if before.get("called_jev") else "no",
                "yes" if after.get("called_jev") else "no",
                str(after.get("last_pick") or "-"),
            ]
        cells = [c.replace("|", "\\|").replace("\n", " ") for c in cells]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _write_verdict(path: str, tick: dict[str, Any]) -> None:
    """Slim verdict JSON {verdict, cases, failures}; failures is a count or a list."""
    failures = tick.get("failures")
    n = len(failures) if isinstance(failures, list) else int(failures or 0)
    payload = {
        "verdict": "PASS" if n == 0 else "FAIL",
        "cases": tick.get("cases"),
        "failures": failures if isinstance(failures, list) else n,
    }
    if "new_failures" in tick:
        payload["new_failures"] = tick["new_failures"]
    if "elapsed_s" in tick:
        payload["elapsed_s"] = tick["elapsed_s"]
    return _watch.write_verdict(path, payload)


def case_gate(row: dict[str, Any], policy: dict[str, Any] | None = None) -> float:
    """Effective yes-gate for a row: per-case min_noul wins, else the
    resolved noul_yes for the case's score qid (per-category maps allowed)."""
    try:
        override = float(row.get("min_noul"))
        return override
    except (TypeError, ValueError):
        pass
    if policy is None:
        try:
            policy = load_jev().load_policy()
        except Exception:
            policy = {}
    try:
        jev = load_jev()
        if hasattr(jev, "resolve_noul_yes"):
            return jev.resolve_noul_yes(policy, str(row.get("score") or "on_track"))
    except Exception:
        pass
    try:
        return float(policy.get("noul_yes", 0.7))
    except (TypeError, ValueError):
        return 0.7


def strict_failures(
    rows: list[dict[str, Any]], live: bool, error: str = ""
) -> list[str]:
    """CI gate: the guarded (after) side must have called Jev (unless the case
    sets expect_call: false — a negative case whose correct outcome is not
    calling) and, when live, score within its noul band: after >= min_noul
    (per-case field or the resolved noul_yes for the case's score question)
    or, for negative cases, after <= max_noul. A live scoring error or a
    missing noul is a failure — the canned row must not stand in for a live
    score that never landed."""
    failures: list[str] = []
    if error:
        failures.append("live scoring failed: %s" % error)
    try:
        policy = load_jev().load_policy()
    except Exception:
        policy = {}
    for row in rows:
        cid = str(row.get("id") or "?")
        after = row.get("after") or {}
        want_consult = row.get("explicit_consult")
        if want_consult is not None:
            # Consult-route gate: the prompt's observed route must match the
            # declared flag — a consult-mention prompt that fires the bypass
            # is a false-accept, an explicit ask that misses it a bypass bug.
            observed_route = row.get("observed_route")
            if want_consult is False and observed_route == "explicit_consult":
                failures.append(
                    "%s: consult-mention prompt took the explicit_consult "
                    "route (false-accept)" % cid
                )
            elif (
                want_consult is True
                and observed_route is not None
                and observed_route != "explicit_consult"
            ):
                failures.append(
                    "%s: explicit-consult ask routed %s, expected "
                    "explicit_consult (missed bypass)" % (cid, observed_route)
                )
        expect_call = bool(row.get("expect_call", True))
        if expect_call and not after.get("called_jev"):
            failures.append("%s: guarded side did not call Jev" % cid)
            continue
        if not expect_call:
            # negative case: the correct outcome is no Jev call at all.
            # observed_call is the real hook routing decision for the
            # prompt (shortlist -> chooser); fall back to the fixture's
            # declared flag only when the hook cannot run here.
            observed = row.get("observed_call")
            if observed is True:
                failures.append(
                    "%s: negative case routed to Jev (false-accept)" % cid
                )
            elif after.get("called_jev"):
                # fixture contradiction: expect_call says the right outcome
                # skips Jev, yet the after-state claims a call happened
                failures.append(
                    "%s: negative case called Jev (false-accept)" % cid
                )
            elif live:
                max_noul = row.get("max_noul")
                an = after.get("noul")
                if isinstance(max_noul, (int, float)) and isinstance(
                    an, (int, float)
                ) and float(an) > float(max_noul):
                    failures.append(
                        "%s: after noul %.2f > max_noul %.2f (false-accept)"
                        % (cid, float(an), float(max_noul))
                    )
            continue
        if live:
            an = after.get("noul")
            if an is None:
                failures.append("%s: no live noul (live scoring missing)" % cid)
                continue
            max_noul = row.get("max_noul")
            if isinstance(max_noul, (int, float)):
                if an > float(max_noul):
                    failures.append(
                        "%s: after noul %.2f > max_noul %.2f (false-accept)"
                        % (cid, an, float(max_noul))
                    )
                continue
            gate = case_gate(row, policy)
            if an < gate:
                failures.append("%s: after noul %.2f < %.2f" % (cid, an, gate))
    return failures


def run(
    live: bool,
    as_json: bool,
    path: Path | None = None,
    only: set[str] | None = None,
    ab: bool = False,
) -> dict[str, Any]:
    blob = load_cases(path)
    cases = list(blob["cases"])
    if only:
        cases = [case for case in cases if str(case.get("id") or "") in only]
    rows = [row_offline(case) for case in cases]
    routing_pool = blob.get("routing_pool")
    if not isinstance(routing_pool, list) or not routing_pool:
        routing_pool = DEFAULT_ROUTING_POOL
    for index, case in enumerate(cases):
        if case.get("expect_call") is False:
            rows[index]["observed_call"] = observed_consult(case, routing_pool)
        if "explicit_consult" in case:
            rows[index]["observed_route"] = observed_route(case, routing_pool)
    live_error = ""
    if live:
        try:
            jev = load_jev()
            policy = jev.load_policy()
            templates = policy.get("templates") or {}
            bad_scores = [
                "%s (score %s is %s)" % (
                    case.get("id") or "?",
                    case.get("score") or "on_track",
                    (
                        "missing"
                        if not isinstance(templates.get(str(case.get("score") or "on_track")), dict)
                        else "type %r" % templates[str(case.get("score") or "on_track")].get("type")
                    ),
                )
                for case in cases
                if (templates.get(str(case.get("score") or "on_track")) or {}).get("type") != "noul"
            ]
            if bad_scores:
                raise SystemExit(
                    "case score must name a noul template: %s"
                    % ", ".join(bad_scores)
                )
            for index, case in enumerate(cases):
                before = score_live(jev, policy, case, case.get("before") or {})
                t0 = time.monotonic()
                after = score_live(jev, policy, case, case.get("after") or {})
                log_eval_call(
                    case, after, (time.monotonic() - t0) * 1000.0, policy
                )
                rows[index]["before"]["noul"] = before.get("noul")
                rows[index]["after"]["noul"] = after.get("noul")
                rows[index]["model"] = after.get("model") or before.get("model")
                ab_spec = case.get("ab")
                if (
                    ab
                    and isinstance(ab_spec, dict)
                    and ab_spec.get("baseline_pick")
                    and ab_spec.get("baseline_step")
                ):
                    # The judge never sees last_pick/invented (side_state
                    # strips them), so the baseline arm substitutes
                    # current_step — the visible outcome of the naive pick.
                    # ab.state overrides the whole side; otherwise copy
                    # `after` minus evidence produced by the Jev consult
                    # itself (a coder-alone state never contains it).
                    base_state = ab_spec.get("state")
                    if isinstance(base_state, dict):
                        base_side = dict(base_state)
                    else:
                        base_side = dict(case.get("after") or {})
                        inspected = base_side.get("inspected")
                        if isinstance(inspected, list):
                            base_side["inspected"] = [
                                s
                                for s in inspected
                                if not _JEV_EVIDENCE.search(str(s))
                            ]
                    base_side["last_pick"] = ab_spec["baseline_pick"]
                    base_side["current_step"] = ab_spec["baseline_step"]
                    base = score_live(jev, policy, case, base_side)
                    rows[index]["baseline"] = {
                        "pick": ab_spec["baseline_pick"],
                        "noul": base.get("noul"),
                    }
        except SystemExit as exc:
            # jev.py reports missing key / invalid response via SystemExit —
            # capture it as the run error so --strict fails cleanly instead
            # of the CLI dying mid-report.
            live_error = _redact(str(exc.code or exc)) or "SystemExit"
        except Exception as exc:
            live_error = _redact(str(exc)) or exc.__class__.__name__
    return {
        "goal": blob.get("goal"),
        "live": live,
        "error": live_error,
        "rows": rows,
    }


def ab_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Jev-vs-baseline table: for cases carrying an `ab` block the live run
    also scored the same guarded state with the baseline (naive) pick
    substituted — delta = after_noul - baseline_noul is the value of Jev's
    choice itself. before_noul stays the no-consult arm."""
    deltas = []
    for row in rows:
        after = row.get("after") or {}
        base = row.get("baseline") or {}
        an = after.get("noul")
        bn = base.get("noul")
        delta = None
        if isinstance(an, (int, float)) and isinstance(bn, (int, float)):
            delta = round(float(an) - float(bn), 4)
        deltas.append(
            {
                "id": row.get("id"),
                "defect": row.get("defect"),
                "jev_pick": after.get("last_pick"),
                "baseline_pick": base.get("pick"),
                "jev_noul": an,
                "baseline_noul": bn,
                "delta": delta,
            }
        )
    scored = [d["delta"] for d in deltas if d["delta"] is not None]
    return {
        "cases": deltas,
        "summary": {
            "scored": len(scored),
            "mean_delta": round(sum(scored) / len(scored), 4) if scored else None,
            "wins": sum(1 for d in scored if d > 0),
            "losses": sum(1 for d in scored if d < 0),
        },
    }


def format_ab_md(ab: dict[str, Any]) -> str:
    lines = [
        "| case | jev_pick | baseline_pick | jev_noul | baseline_noul | delta |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in ab.get("cases") or []:
        cells = [
            str(row.get("id") or ""),
            str(row.get("jev_pick") or "-"),
            str(row.get("baseline_pick") or "-"),
            "-" if row.get("jev_noul") is None else "%.2f" % row["jev_noul"],
            "-" if row.get("baseline_noul") is None else "%.2f" % row["baseline_noul"],
            "-" if row.get("delta") is None else "%+.2f" % row["delta"],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    summary = ab.get("summary") or {}
    if summary.get("scored"):
        lines.append(
            "\nmean delta **%+.2f** over %d scored arms (%d wins / %d losses)."
            % (summary["mean_delta"], summary["scored"], summary["wins"], summary["losses"])
        )
    return "\n".join(lines) + "\n"


def _week_threshold(policy: dict[str, Any], key: str, default: int) -> int:
    try:
        value = int(policy.get(key) or default)
    except (TypeError, ValueError):
        value = default
    return value if value >= 0 else default


def update_streaks(
    prior: dict[str, int], rows: list[dict[str, Any]], live: bool,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Per-case consecutive-below-gate counter, carried across weekly runs
    inside the cached eval-baseline.json payload. A case at or above its
    gate (or a negative case under max_noul) resets to 0.

    policy.json thresholds: streak_warn_weeks (default 2) marks a case in
    `flags` once it sits below its gate that many runs in a row;
    streak_fail_weeks (default 0 = off) marks it in `fails` — the strict
    gate and verdict treat those as failures."""
    if not isinstance(policy, dict):
        policy = {}
    warn_weeks = max(1, _week_threshold(policy, "streak_warn_weeks", 2))
    fail_weeks = _week_threshold(policy, "streak_fail_weeks", 0)
    streaks = dict(prior) if isinstance(prior, dict) else {}
    out = {}
    for row in rows:
        cid = str(row.get("id") or "?")
        after = row.get("after") or {}
        an = after.get("noul")
        below = False
        if not row.get("expect_call", True):
            # negative cases are false-accept controls, not floor-gated —
            # a low noul there is expected, never a streak flag
            below = False
        elif live and isinstance(an, (int, float)):
            max_noul = row.get("max_noul")
            if isinstance(max_noul, (int, float)):
                below = float(an) > float(max_noul)
            else:
                below = float(an) < case_gate(row, policy)
        elif live:
            below = True  # missing noul on a live run counts as below-gate
        out[cid] = (int(streaks.get(cid) or 0) + 1) if below else 0
    return {
        "streaks": out,
        "flags": sorted(
            "case %s ниже гейта %d прогонов подряд" % (cid, n)
            for cid, n in out.items()
            if n >= warn_weeks
        ),
        "fails": sorted(
            "case %s ниже гейта %d прогонов подряд" % (cid, n)
            for cid, n in out.items()
            if fail_weeks and n >= fail_weeks
        ),
        "warn_weeks": warn_weeks,
        "fail_weeks": fail_weeks,
    }


def run_failures(result: dict[str, Any], live: bool) -> list[str]:
    """The full eval gate: strict failures + baseline regressions + streak
    fails. This is the list --strict exits on and --verdict mirrors."""
    failures = strict_failures(
        result["rows"], live, result.get("error") or ""
    )
    for entry in (result.get("diff") or {}).get("regressions", []):
        failures.append(
            "%s: regressed vs baseline (%s)" % (entry["id"], entry["why"])
        )
    for flag in (result.get("drift") or {}).get("fails") or []:
        failures.append("streak: %s" % flag)
    return failures


def history_record(
    result: dict[str, Any], failures: list[str], run_url: str
) -> dict[str, Any]:
    """One eval-history.jsonl record: {ts, run_url, verdict, worst_noul,
    ab_mean_delta, streaks}. worst_noul is the lowest after-side noul
    among floor-gated cases (negative cases excluded — a low score there
    is the expected outcome)."""
    nouls = [
        float((row.get("after") or {}).get("noul"))
        for row in result.get("rows") or []
        if row.get("expect_call", True)
        and isinstance((row.get("after") or {}).get("noul"), (int, float))
        and not isinstance((row.get("after") or {}).get("noul"), bool)
    ]
    summary = (result.get("ab") or {}).get("summary") or {}
    return {
        "ts": int(time.time()),
        "run_url": run_url or None,
        "verdict": "PASS" if not failures else "FAIL",
        "worst_noul": round(min(nouls), 4) if nouls else None,
        "ab_mean_delta": summary.get("mean_delta"),
        "streaks": (result.get("drift") or {}).get("streaks") or {},
    }


def append_history(path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL record; creates parent dirs. Raises OSError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _row_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id") or "?"): row for row in rows if isinstance(row, dict)}


def _failing_ids(rows: list[dict[str, Any]], live: bool) -> set[str]:
    return {
        f.split(":", 1)[0]
        for f in strict_failures(rows, live)
    }


def diff_baseline(
    baseline_rows: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    live: bool,
) -> dict[str, Any]:
    """Per-case diff vs a saved baseline: strict-gate flips, noul drops,
    called_jev flips, added/removed cases."""
    before = _row_map(baseline_rows)
    after = _row_map(rows)
    was_failing = _failing_ids(list(before.values()), live)
    now_failing = _failing_ids(list(after.values()), live)
    regressions: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    changed: list[str] = []
    unchanged = 0
    for cid, row in after.items():
        old = before.get(cid)
        if old is None:
            continue  # added
        if cid in now_failing and cid not in was_failing:
            regressions.append({"id": cid, "why": "strict_failure"})
            continue
        if cid in was_failing and cid not in now_failing:
            improved.append({"id": cid, "why": "strict_pass"})
            continue
        delta = ""
        if live:
            old_noul = (old.get("after") or {}).get("noul")
            new_noul = (row.get("after") or {}).get("noul")
            if isinstance(old_noul, (int, float)) and isinstance(new_noul, (int, float)):
                delta = round(float(new_noul) - float(old_noul), 4)
                if delta <= -0.1:
                    regressions.append(
                        {"id": cid, "why": "noul_drop", "delta": delta}
                    )
                    continue
                if delta >= 0.1:
                    improved.append(
                        {"id": cid, "why": "noul_gain", "delta": delta}
                    )
                    continue
        old_after = old.get("after") or {}
        new_after = row.get("after") or {}
        if (
            old_after.get("last_pick") != new_after.get("last_pick")
            or old_after.get("step") != new_after.get("step")
            or old.get("defect") != row.get("defect")
        ):
            changed.append(cid)
            continue
        unchanged += 1
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    return {
        "regressions": regressions,
        "improved": improved,
        "changed": sorted(changed),
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "counts": {
            "regressions": len(regressions),
            "improved": len(improved),
            "changed": len(changed),
            "added": len(added),
            "removed": len(removed),
            "unchanged": unchanged,
        },
    }


def env_report(args) -> dict:
    """Resolved compare.py environment. Values only — never secrets."""
    policy = os.environ.get("JEV_POLICY", "").strip()
    try:
        watch_secs = float(os.environ.get("JEV_COMPARE_WATCH_SECS", "") or 0)
    except ValueError:
        watch_secs = 0.0
    cases = args.cases or str(cases_path())
    return {
        "cases": str(cases),
        "cases_exists": cases != "-" and Path(cases).is_file(),
        "only": args.only,
        "live": bool(args.live),
        "strict": bool(args.strict),
        "policy": policy if policy else "default",
        "watch_max": _watch.cap("JEV_COMPARE_WATCH_MAX", None),
        "watch_secs": watch_secs,
        "watch_quiet": _watch.quiet("JEV_COMPARE_WATCH_QUIET", False),
    }


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


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    if _watch.maybe_version(sys.argv[1:] if argv is None else argv):
        return 0
    parser = argparse.ArgumentParser(
        description="Compare unguarded vs Jev-guarded traces on sticky prompts."
    )
    parser.add_argument("--live", action="store_true", help="Call Jev Noul for each side")
    parser.add_argument("--failing", action="store_true", help="Show only cases whose guarded (after) side fails the strict gate")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--jq", metavar="KEY", default="", help="Print just this dotted-path field of the result payload (e.g. failures); unknown key exits 2")
    parser.add_argument("--md", action="store_true", help="Print rows as a Markdown table")
    parser.add_argument("--jsonl", action="store_true", help="Print one row JSON per line")
    parser.add_argument("--csv", action="store_true", help="Emit the case rows as CSV (id,defect,... columns; --keys a,b overrides the columns)")
    parser.add_argument("--keys", metavar="a,b", default="", help="With --jsonl: keep only these keys in each emitted row (rc 2 on an empty list)")
    parser.add_argument("--out", metavar="PATH", default="", help="Also write the result JSON to PATH")
    parser.add_argument("--report", metavar="PATH", default="", help="Write a markdown compare report (verdict + per-case table) to PATH; with --json writes the report object instead only when PATH ends in .json")
    parser.add_argument("--verdict", metavar="PATH", default="", help="Write a slim {verdict, cases, failures} JSON to PATH (in --watch mode refreshed every tick) '-' prints it to stdout.")
    parser.add_argument("--cases", default=os.environ.get("JEV_COMPARE_CASES", "") or None, help="Path to compare-cases.json ('-' reads cases JSON from stdin; needs a file for --watch/--diff)")
    parser.add_argument("--schema", action="store_true", help="Print the compare-cases.json key contract and exit (--json emits the object)")
    parser.add_argument(
        "--only",
        default=os.environ.get("JEV_COMPARE_ONLY", ""),
        help="Comma-separated case ids to run (default: all; JEV_COMPARE_ONLY presets; '-' reads the list from stdin).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when any case's guarded side skipped Jev (or scored below noul_yes with --live).",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-run the cases every S seconds, printing a {ts,cases,failures} JSON tick (JEV_COMPARE_WATCH_MAX caps ticks).",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick with failures.")
    parser.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    parser.add_argument(
        "--ab",
        action="store_true",
        help="Score an extra arm per case: the guarded state with the case's baseline_pick substituted — the \"Jev vs without Jev\" delta table (needs --live; cases without an ab block are skipped).",
    )
    parser.add_argument("--baseline", metavar="PATH", default="", help="Write the current rows to PATH as a baseline file for a later --diff")
    parser.add_argument("--history", metavar="PATH", default="", help="Append one JSONL run record ({ts, run_url, verdict, worst_noul, ab_mean_delta, streaks}) to PATH after scoring; skipped when the run errored or under --watch")
    parser.add_argument("--run-url", metavar="URL", default="", help="Recorded as run_url on --history records (e.g. the CI run link)")
    parser.add_argument("--diff", metavar="PATH", default="", help="Load a --baseline file and add a diff block (regressions/improved/changed/added/removed) to the result payload; regressions also join the --strict failure list ('-' reads the baseline JSON from stdin; needs a file --cases, no --watch)")
    parser.add_argument("--trend", metavar="DIR", default="", help="Diff the current rows against every *.json baseline in DIR; adds a trend list ({file,ts,regressions,improved,changed,added,removed,unchanged} sorted by ts) to the payload and one stderr line per baseline")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the offline scorer on two synthetic cases (one pass, one deliberate strict-gate fail); exit 1 when the failure is not detected (--json emits the checks).",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="Print the resolved env config JSON ({cases, cases_exists, only, live, strict, policy, watch_max, watch_secs, watch_quiet}) and exit (--jq KEY prints one field, rc 2 on unknown; --out PATH also writes it).",
    )
    args = parser.parse_args(argv)
    if getattr(args, "schema", False):
        rows = {
            "goal": {"required": False, "type": "string, shared task description"},
            "cases": {"required": True, "type": "list[case]"},
            "case.id": {"required": True, "type": "string, unique"},
            "case.prompt": {"required": True, "type": "string, sticky prompt text"},
            "case.defect": {"required": False, "type": "string label (e.g. drift)"},
            "case.before": {"required": True, "type": "object, unguarded outcome fields"},
            "case.after": {"required": True, "type": "object, guarded outcome fields"},
            "case.score": {"required": False, "type": "number, hand-tuned weight"},
            "case.expect_call": {"required": False, "type": "bool, whether the prompt should spend a Jev call"},
            "case.explicit_consult": {"required": False, "type": "bool, whether the prompt should take the explicit_consult route"},
        }
        if args.as_json:
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        else:
            for key in rows:
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, rows[key]["type"], "required" if rows[key]["required"] else "optional")
                )
        return 0
    if getattr(args, "env", False):
        report = env_report(args)
        if getattr(args, "jq", ""):
            node, found = _watch.dig(report, args.jq)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (env has: %s)\n"
                    % (args.jq, ", ".join(sorted(report)))
                )
                return 2
            sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
            return 0
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if getattr(args, "out", ""):
            try:
                _atomic_write(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0
    if args.self_test:
        fixture = [
            {
                "id": "st_good",
                "defect": "none",
                "prompt": "self-test prompt",
                "before": {"called_jev": False, "current_step": "drifted"},
                "after": {
                    "called_jev": True,
                    "last_pick": "return_to_plan",
                    "current_step": "on plan",
                },
            },
            {
                "id": "st_bad",
                "defect": "drift",
                "prompt": "self-test prompt",
                "before": {"called_jev": False, "current_step": "drifted"},
                "after": {"called_jev": False, "current_step": "still drifted"},
            },
        ]
        checks: dict[str, bool] = {}
        try:
            rows = [row_offline(case) for case in fixture]
            checks["rows"] = [r.get("id") for r in rows] == [
                "st_good",
                "st_bad",
            ]
            failures = strict_failures(rows, live=False)
            checks["strict_detection"] = failures == [
                "st_bad: guarded side did not call Jev"
            ]
            checks["render"] = "st_bad" in format_table(
                rows, False
            ) and "st_bad" in format_md(rows, False)
        except Exception:
            checks = {"raised": False}
        ok = bool(checks) and all(checks.values())
        payload = {"self_test": "ok" if ok else "FAIL", "checks": checks}
        if args.as_json:
            sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        else:
            sys.stdout.write(
                "self-test: %s %s\n"
                % (
                    payload["self_test"],
                    " ".join(
                        "%s=%s" % (k, "ok" if v else "FAIL")
                        for k, v in sorted(checks.items())
                    ),
                )
            )
        return 0 if ok else 1
    only = {s.strip() for s in _watch.text_arg(args.only).split(",") if s.strip()} or None
    if args.cases == "-" and (args.watch or args.diff):
        sys.stderr.write("--cases - (stdin) supports neither --watch nor --diff\n")
        return 2
    if args.diff == "-" and (args.watch or args.cases == "-"):
        sys.stderr.write(
            "--diff - (stdin) needs a file --cases and no --watch\n"
        )
        return 2
    if args.watch and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_COMPARE_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_COMPARE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        verdict_ok = True
        prev_failures: set[str] = set()
        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            cur = run(
                live=args.live,
                as_json=args.as_json,
                path=args.cases or None,
                only=only,
                ab=bool(getattr(args, "ab", False)),
            )
            failing = strict_failures(cur["rows"], args.live, cur.get("error") or "")
            new_failures = sorted(set(failing) - prev_failures)
            prev_failures = set(failing)
            tick = {
                "ts": int(_time.time()),
                "cases": len(cur["rows"]),
                "failures": len(failing),
                "new_failures": new_failures,
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, getattr(args, "jq", ""), args.out, quiet=_watch.quiet("JEV_COMPARE_WATCH_QUIET", args.quiet), bad=bool(tick["failures"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d cases=%d failures=%d\n"
                % (ticks, tick["cases"], tick["failures"])
            )
            if args.verdict and verdict_ok and not _write_verdict(args.verdict, tick):
                verdict_ok = False  # warn once, stop retrying
            if args.fail_fast and failing:
                break
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(args.watch)
        if args.verdict and verdict_ok:
            _write_verdict(args.verdict, tick)
        return 0 if tick["failures"] == 0 else 1
    result = run(
        live=args.live,
        as_json=args.as_json,
        path=args.cases or None,
        only=only,
        ab=bool(getattr(args, "ab", False)),
    )
    if getattr(args, "ab", False):
        result["ab"] = ab_block(result["rows"])
    if args.baseline:
        try:
            _atomic_write(
                Path(args.baseline),
                json.dumps(
                    {
                        "ts": int(time.time()),
                        "live": args.live,
                        "rows": result["rows"],
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.baseline, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.baseline)
    if args.diff:
        try:
            raw = json.loads(
                sys.stdin.read()
                if args.diff == "-"
                else Path(args.diff).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            sys.stderr.write("cannot read baseline %s: %s\n" % (args.diff, exc))
            return 2
        base_rows = raw.get("rows") if isinstance(raw, dict) else raw
        if not isinstance(base_rows, list):
            sys.stderr.write("baseline %s has no rows list\n" % args.diff)
            return 2
        result["diff"] = diff_baseline(base_rows, result["rows"], args.live)
        prior_streaks = (
            (raw.get("drift") or {}).get("streaks")
            if isinstance(raw, dict)
            else {}
        ) or {}
        try:
            policy = load_jev().load_policy()
        except Exception:
            policy = {}
        result["drift"] = update_streaks(prior_streaks, result["rows"], args.live, policy)
        for flag in result["drift"]["flags"]:
            sys.stderr.write("drift: %s\n" % flag)
        for fail in result["drift"]["fails"]:
            sys.stderr.write("drift-fail: %s\n" % fail)
        for entry in result["diff"]["regressions"]:
            sys.stderr.write(
                "regression: %s (%s)\n" % (entry["id"], entry["why"])
            )
    if getattr(args, "trend", ""):
        trend_dir = Path(args.trend)
        if not trend_dir.is_dir():
            sys.stderr.write("trend dir %s missing\n" % trend_dir)
            return 2
        trend = []
        for path in sorted(trend_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            base_rows = raw.get("rows") if isinstance(raw, dict) else raw
            if not isinstance(base_rows, list):
                continue
            diff = diff_baseline(base_rows, result["rows"], args.live)
            counts = diff.get("counts") or {}
            trend.append(
                {
                    "file": path.name,
                    "ts": raw.get("ts") if isinstance(raw, dict) else None,
                    "regressions": counts.get("regressions", 0),
                    "improved": counts.get("improved", 0),
                    "changed": counts.get("changed", 0),
                    "added": counts.get("added", 0),
                    "removed": counts.get("removed", 0),
                    "unchanged": counts.get("unchanged", 0),
                }
            )
        trend.sort(key=lambda row: (row["ts"] is None, row["ts"], row["file"]))
        result["trend"] = trend
        for row in trend:
            sys.stderr.write(
                "trend: %s regressions=%d improved=%d changed=%d added=%d removed=%d\n"
                % (
                    row["file"],
                    row["regressions"],
                    row["improved"],
                    row["changed"],
                    row["added"],
                    row["removed"],
                )
            )
    if getattr(args, "history", ""):
        # append before --failing narrows the rows: the record tracks the
        # full run; an errored run (no scores) writes nothing, same rule
        # as the baseline-save gate.
        if not result.get("error"):
            try:
                append_history(
                    Path(args.history),
                    history_record(
                        result, run_failures(result, args.live), args.run_url
                    ),
                )
                sys.stderr.write("history: appended to %s\n" % args.history)
            except OSError as exc:
                sys.stderr.write("cannot append %s: %s\n" % (args.history, exc))
                return 1
        else:
            sys.stderr.write("history: skipped (run errored)\n")
    if args.failing:
        failing_ids = {
            f.split(":", 1)[0]
            for f in strict_failures(result["rows"], args.live)
        }
        result["rows"] = [
            r for r in result["rows"] if str(r.get("id") or "?") in failing_ids
        ]
    if args.out:
        out_path = Path(args.out)
        try:
            _atomic_write(
                out_path,
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %s\n" % out_path)
    if args.verdict:
        _write_verdict(
            args.verdict,
            {
                "cases": len(result["rows"]),
                "failures": run_failures(result, args.live),
            },
        )
    if args.report:
        failures = run_failures(result, args.live)
        # --report is the human-readable artifact: JSON goes there only when
        # the path itself asks for it (.json); '--json --report eval.md'
        # keeps the markdown table, with the payload going to --out/stdout.
        if args.as_json and Path(args.report).suffix.lower() == ".json":
            report_obj = {
                "verdict": "PASS" if not failures else "FAIL",
                "goal": result.get("goal"),
                "live": args.live,
                "cases": len(result["rows"]),
                "failures": failures,
                "rows": result["rows"],
            }
            text = json.dumps(report_obj, indent=2, ensure_ascii=False) + "\n"
        else:
            text = (
                "# compare report\n\n"
                + "verdict: **%s**\n\n" % ("PASS" if not failures else "FAIL")
                + "- cases: %d\n" % len(result["rows"])
                + "- failures: %d\n\n" % len(failures)
                + "".join("- %s\n" % f for f in failures)
                + "\n"
                + format_md(result["rows"], live=args.live)
                + "".join(
                    "\n**drift flag**: %s\n" % flag
                    for flag in (result.get("drift") or {}).get("flags") or []
                )
                + "".join(
                    "\n**drift fail**: %s\n" % flag
                    for flag in (result.get("drift") or {}).get("fails") or []
                )
                + (
                    "\n## Jev vs baseline (ab)\n\n" + format_ab_md(result["ab"])
                    if isinstance(result.get("ab"), dict)
                    else ""
                )
            )
        try:
            _atomic_write(Path(args.report), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.report, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.report)
    if getattr(args, "jq", ""):
        node, found = _watch.dig(result, args.jq)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (args.jq, ", ".join(sorted(result)) if isinstance(result, dict) else "")
            )
            return 2
        sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
    elif args.as_json:
        json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif args.md:
        sys.stdout.write(format_md(result["rows"], live=args.live))
    elif getattr(args, "csv", False):
        key_sel = getattr(args, "keys", "") or ""
        proj = [k.strip() for k in key_sel.split(",") if k.strip()] if key_sel else []
        if key_sel and not proj:
            sys.stderr.write("--keys names no fields\n")
            return 2
        if proj:
            header = proj
            data = [
                [
                    json.dumps(value, sort_keys=True)
                    if isinstance((value := row.get(k)), (dict, list))
                    else str(value if value is not None else "")
                    for k in proj
                ]
                for row in result["rows"]
            ]
        elif args.live:
            header = ["id", "defect", "called_jev", "before_noul", "after_noul"]
            data = [
                [
                    row.get("id") or "",
                    row.get("defect") or "",
                    "yes" if (row.get("after") or {}).get("called_jev") else "no",
                    "" if (row.get("before") or {}).get("noul") is None else "%.2f" % row["before"]["noul"],
                    "" if (row.get("after") or {}).get("noul") is None else "%.2f" % row["after"]["noul"],
                ]
                for row in result["rows"]
            ]
        else:
            header = ["id", "defect", "before_jev", "after_jev", "after_pick"]
            data = [
                [
                    row.get("id") or "",
                    row.get("defect") or "",
                    "yes" if (row.get("before") or {}).get("called_jev") else "no",
                    "yes" if (row.get("after") or {}).get("called_jev") else "no",
                    (row.get("after") or {}).get("last_pick") or "",
                ]
                for row in result["rows"]
            ]
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(data)
        sys.stdout.write(buf.getvalue())
    elif args.jsonl:
        key_sel = getattr(args, "keys", "") or ""
        proj = [k.strip() for k in key_sel.split(",") if k.strip()] if key_sel else []
        if key_sel and not proj:
            sys.stderr.write("--keys names no fields\n")
            return 2
        for row in result["rows"]:
            if proj and isinstance(row, dict):
                row = {k: row.get(k) for k in proj}
            sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(format_table(result["rows"], live=args.live))
    if result.get("error"):
        sys.stderr.write("live scoring failed: %s\n" % result["error"])
    if args.strict:
        failures = run_failures(result, args.live)
        for failure in failures:
            sys.stderr.write("strict: %s\n" % failure)
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    _watch.exit_safely(main())
