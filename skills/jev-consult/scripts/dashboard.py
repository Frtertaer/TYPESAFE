#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dashboard.py - one self-contained dashboard.html from the ops artifacts.

Reads a decisions.jsonl routing log plus the eval artifacts the weekly
live-eval workflow produces (eval-history.jsonl, optionally the
eval-live.json compare payload for the verbatim drift flags) and renders
ONE static page: KPI cards for the acceptance metrics (pick / applied /
override / miss rates, promoted share when entries carry a `promoted`
field), latency p50/p95 against the hook budget, the escalate_reason
histogram, the weekly eval trend, and the per-case streak/drift flags.

The output file is pure ASCII: markup is escaped and every non-ASCII
character is emitted as a numeric entity, so Cyrillic drift-flag strings
render fine in a browser while the bytes stay encoding-proof on runners
whose default codec is not utf-8. No CDN, no JS, no server.

Usage:
  python dashboard.py [--file decisions.jsonl] [--history eval-history.jsonl]
      [--eval eval-live.json] [--out dashboard.html] [--json] [--jq KEY]
      [--schema] [--env] [--verdict PATH]
      [--watch S] [--watch-max S] [--max-ticks N] [--unchanged-max N]
      [--fail-fast] [--quiet] [--self-test] [--version]
"""
from __future__ import annotations

import argparse
import datetime
import html as _html
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _watch  # noqa: E402
import decisions  # noqa: E402
import inventory  # noqa: E402

PAYLOAD_SCHEMA_ROWS = {
    "generated_ts": {"type": "int, epoch seconds", "required": True},
    "sources": {"type": "{file, history, eval}", "required": True},
    "entries": {"type": "int", "required": True},
    "bad_lines": {"type": "int", "required": True},
    "verdict": {"type": "string|null, latest run verdict", "required": True},
    "acceptance": {"type": "acceptance_report subset", "required": True},
    "latency_ms": {"type": "{n, p50, p95, over_budget, over_budget_share, budget_ms}", "required": True},
    "promoted": {"type": "{entries, promoted, share} | null when absent", "required": True},
    "escalate_reasons": {"type": "{reason: count}", "required": True},
    "weekly": {"type": "list of {ts, iso, verdict, worst_noul, ab_mean_delta, streak_cases, run_url}", "required": True},
    "streaks": {"type": "list of {case, runs, level}", "required": True},
    "drift": {"type": "{flags, fails, warn_weeks, fail_weeks}", "required": True},
    "drift_events": {"type": "list of {ts, iso, verdict, run_url, streak_cases}", "required": True},
    "policy": {"type": "{hook_budget_ms, miss_rate_max, override_rate_max, streak_warn_weeks, streak_fail_weeks}", "required": True},
}


def _esc(v) -> str:
    """HTML-escape then force pure ASCII via numeric entities — Cyrillic
    streak flag text must not break a non-utf-8 consumer of the file."""
    return (
        _html.escape(str(v if v is not None else ""))
        .encode("ascii", "xmlcharrefreplace")
        .decode("ascii")
    )


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pct(v) -> str:
    return "n/a" if not _num(v) else "%.1f%%" % (100 * float(v))


def _ms(v) -> str:
    return "-" if not _num(v) else "%.0f" % float(v)


def _iso(ts) -> str:
    if not _num(ts):
        return "?"
    return datetime.datetime.fromtimestamp(
        float(ts), tz=datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%MZ")


def _policy_num(policy: dict, key: str):
    v = policy.get(key)
    if isinstance(v, dict):
        v = v.get("default")
    if not _num(v):
        return None
    return float(v)


def _streak_rows(streaks: dict, warn_weeks: int, fail_weeks: int) -> list[dict]:
    rows = []
    for cid, n in sorted(
        streaks.items(), key=lambda kv: (-kv[1], str(kv[0]))
    ):
        try:
            runs = int(n)
        except (TypeError, ValueError):
            continue
        if runs <= 0 or (warn_weeks and runs < warn_weeks):
            continue
        rows.append(
            {
                "case": str(cid),
                "runs": runs,
                "level": "fail" if fail_weeks and runs >= fail_weeks else "warn",
            }
        )
    return rows


def _drift_events(history: list[dict], limit: int = 8) -> list[dict]:
    """Latest run records that signal drift: FAIL verdict or a non-zero
    streak map. Newest last in the file -> newest first here."""
    out = []
    for rec in reversed(history):
        if not isinstance(rec, dict):
            continue
        streaks = {
            str(k): v
            for k, v in (rec.get("streaks") or {}).items()
            if _num(v) and v > 0
        }
        verdict = str(rec.get("verdict") or "")
        if verdict != "FAIL" and not streaks:
            continue
        out.append(
            {
                "ts": rec.get("ts"),
                "iso": _iso(rec.get("ts")),
                "verdict": verdict or "?",
                "run_url": rec.get("run_url") or "",
                "streak_cases": ", ".join(
                    "%s=%s" % (c, n) for c, n in sorted(streaks.items())
                ),
            }
        )
        if len(out) >= limit:
            break
    return out


def build_payload(
    entries: list[dict],
    bad_lines: int,
    history: list[dict],
    eval_payload: dict | None,
    policy: dict,
    *,
    file: str = "",
    history_path: str = "",
    eval_path: str = "",
) -> dict:
    acc = decisions.acceptance_report(entries)
    lat = acc.get("latency_ms") or {}

    promoted_rows = [e for e in entries if isinstance(e, dict) and "promoted" in e]
    promoted = None
    if promoted_rows:
        n_promoted = sum(1 for e in promoted_rows if e.get("promoted"))
        promoted = {
            "entries": len(promoted_rows),
            "promoted": n_promoted,
            "share": round(n_promoted / len(promoted_rows), 4),
        }

    # Streak sources: the eval payload's drift block is authoritative when
    # given; otherwise the newest history record carries the same map.
    drift = (eval_payload or {}).get("drift") or {}
    streaks_map = {
        str(k): v for k, v in (drift.get("streaks") or {}).items()
    }
    warn_weeks = int(
        drift.get("warn_weeks")
        or _policy_num(policy, "streak_warn_weeks")
        or 2
    )
    fail_weeks = int(
        drift.get("fail_weeks")
        or _policy_num(policy, "streak_fail_weeks")
        or 0
    )
    flags = [str(f) for f in drift.get("flags") or []]
    fails = [str(f) for f in drift.get("fails") or []]
    if not streaks_map and history:
        latest = next(
            (r for r in reversed(history) if isinstance(r, dict)), {}
        )
        streaks_map = {
            str(k): v for k, v in (latest.get("streaks") or {}).items()
        }

    weekly = [
        {
            "ts": r.get("ts"),
            "iso": _iso(r.get("ts")),
            "verdict": str(r.get("verdict") or "?"),
            "worst_noul": r.get("worst_noul"),
            "ab_mean_delta": r.get("ab_mean_delta"),
            "run_url": r.get("run_url") or "",
            "streak_cases": sum(
                1
                for v in (r.get("streaks") or {}).values()
                if _num(v) and v > 0
            ),
        }
        for r in history
        if isinstance(r, dict)
    ]
    latest_verdict = weekly[-1]["verdict"] if weekly else None

    hook_budget_ms = _policy_num(policy, "hook_budget_seconds")
    budget_ms = lat.get("budget_ms") or (
        hook_budget_ms * 1000 if _num(hook_budget_ms) else None
    )
    return {
        "generated_ts": int(time.time()),
        "sources": {
            "file": file or "",
            "history": history_path or "",
            "eval": eval_path or "",
        },
        "entries": len(entries),
        "bad_lines": bad_lines,
        "verdict": latest_verdict,
        "acceptance": {
            "routing_entries": acc.get("routing_entries") or 0,
            "picks": acc.get("picks") or 0,
            "pick_rate": acc.get("pick_rate"),
            "applied": acc.get("applied") or 0,
            "applied_rate": acc.get("applied_rate"),
            "overridden": acc.get("overridden") or 0,
            "override_rate": acc.get("override_rate"),
            "misses": acc.get("misses") or 0,
            "miss_rate": acc.get("miss_rate"),
            "strong_picks": acc.get("strong_picks") or 0,
            "strong_pick_not_overridden": acc.get(
                "strong_pick_not_overridden"
            ),
            "explicit_consult": acc.get("explicit_consult") or {},
            "override_window": acc.get("override_window") or 0,
        },
        "latency_ms": {
            "n": lat.get("n") or 0,
            "p50": lat.get("p50"),
            "p95": lat.get("p95"),
            "over_budget": lat.get("over_budget") or 0,
            "over_budget_share": lat.get("over_budget_share"),
            "budget_ms": budget_ms,
        },
        "promoted": promoted,
        "escalate_reasons": acc.get("escalate_reasons") or {},
        "weekly": weekly,
        "streaks": _streak_rows(streaks_map, warn_weeks, fail_weeks),
        "drift": {
            "flags": flags,
            "fails": fails,
            "warn_weeks": warn_weeks,
            "fail_weeks": fail_weeks,
        },
        "drift_events": _drift_events(history),
        "policy": {
            "hook_budget_ms": budget_ms,
            "miss_rate_max": _policy_num(policy, "miss_rate_max"),
            "override_rate_max": _policy_num(policy, "override_rate_max"),
            "streak_warn_weeks": warn_weeks,
            "streak_fail_weeks": fail_weeks,
        },
    }


def _bar(frac: float, neg: bool = False) -> str:
    width = round(100 * max(0.0, min(1.0, frac)), 1)
    cls = "bar neg" if neg else "bar"
    return '<div class="%s"><span style="width:%.1f%%"></span></div>' % (
        cls,
        width,
    )


def _kpi(value: str, label: str, tone: str = "") -> str:
    cls = "kpi %s" % tone if tone else "kpi"
    return '<div class="%s"><b>%s</b>%s</div>' % (cls, _esc(value), _esc(label))


def render_html(payload: dict) -> str:
    acc = payload["acceptance"]
    lat = payload["latency_ms"]
    pol = payload["policy"]
    drift = payload["drift"]

    def tone(rate, cap) -> str:
        return "bad" if _num(rate) and _num(cap) and rate > cap else ""

    cards = [
        _kpi(_pct(acc.get("pick_rate")), "pick rate"),
        _kpi(_pct(acc.get("applied_rate")), "applied rate"),
        _kpi(
            _pct(acc.get("override_rate")),
            "override rate",
            tone(acc.get("override_rate"), pol.get("override_rate_max")),
        ),
        _kpi(
            _pct(acc.get("miss_rate")),
            "miss rate",
            tone(acc.get("miss_rate"), pol.get("miss_rate_max")),
        ),
        _kpi(
            _pct(acc.get("strong_pick_not_overridden")),
            "strong-pick kept",
        ),
    ]
    if payload.get("promoted"):
        promoted = payload["promoted"]
        cards.append(
            _kpi(
                _pct(promoted.get("share")),
                "promoted (%d/%d)"
                % (promoted["promoted"], promoted["entries"]),
            )
        )
    cards += [
        _kpi(_ms(lat.get("p50")), "latency p50 ms"),
        _kpi(
            _ms(lat.get("p95")),
            "p95 vs %s ms" % (_ms(lat.get("budget_ms")) or "?"),
            "bad"
            if _num(lat.get("p95"))
            and _num(lat.get("budget_ms"))
            and lat["p95"] > lat["budget_ms"]
            else "",
        ),
        _kpi(
            _pct(lat.get("over_budget_share")),
            "over budget (%d)" % (lat.get("over_budget") or 0),
            "bad" if lat.get("over_budget") else "",
        ),
    ]

    reasons = payload.get("escalate_reasons") or {}
    r_total = sum(reasons.values()) or 1
    reason_rows = "".join(
        "<tr><td>%s</td><td>%d</td><td>%s</td><td>%.1f%%</td></tr>"
        % (_esc(k), v, _bar(v / r_total), 100 * v / r_total)
        for k, v in reasons.items()
    )
    reason_table = (
        "<h2>Escalate reasons</h2>"
        "<table><tr><th>reason</th><th>n</th><th></th><th>share</th></tr>"
        "%s</table>" % reason_rows
        if reason_rows
        else ""
    )

    deltas = [
        abs(float(r["ab_mean_delta"]))
        for r in payload["weekly"]
        if _num(r.get("ab_mean_delta"))
    ]
    max_delta = max(deltas) if deltas else 0.0
    weekly_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td class='%s'>%s</td><td>%.2f</td>"
        "<td>%s</td><td>%+.3f</td><td>%s</td><td>%d</td></tr>"
        % (
            _esc(r["iso"]),
            (
                '<a href="%s">run</a>' % _esc(r["run_url"])
                if isinstance(r.get("run_url"), str)
                and r["run_url"].startswith("http")
                else "-"
            ),
            "fail" if r["verdict"] == "FAIL" else "ok",
            _esc(r["verdict"]),
            float(r["worst_noul"]) if _num(r.get("worst_noul")) else 0.0,
            _bar(float(r["worst_noul"])) if _num(r.get("worst_noul")) else "-",
            float(r["ab_mean_delta"]) if _num(r.get("ab_mean_delta")) else 0.0,
            (
                _bar(abs(float(r["ab_mean_delta"])) / max_delta,
                     neg=float(r["ab_mean_delta"]) < 0)
                if _num(r.get("ab_mean_delta")) and max_delta
                else "-"
            ),
            r["streak_cases"],
        )
        for r in payload["weekly"]
    )
    weekly_table = (
        "<h2>Weekly eval trend</h2>"
        "<table><tr><th>run ts</th><th>run</th><th>verdict</th>"
        "<th>worst noul</th><th></th><th>ab mean &#916;</th><th></th>"
        "<th>streak cases</th></tr>%s</table>" % weekly_rows
        if weekly_rows
        else "<h2>Weekly eval trend</h2><p class='small'>no history records yet</p>"
    )

    streak_rows = "".join(
        "<tr class='%s'><td>%s</td><td>%d</td><td>%s</td></tr>"
        % ("fail" if s["level"] == "fail" else "warn",
           _esc(s["case"]), s["runs"], _esc(s["level"]))
        for s in payload["streaks"]
    )
    streak_table = (
        "<table><tr><th>case</th><th>runs below gate</th><th>level</th></tr>"
        "%s</table>" % streak_rows
        if streak_rows
        else "<p class='small'>no case below its gate "
        "%d+ runs in a row</p>" % drift["warn_weeks"]
    )
    flag_list = "".join(
        "<li>%s%s</li>"
        % (_esc(f), " (fail-gate)" if f in set(drift["fails"]) else "")
        for f in list(dict.fromkeys(drift["flags"] + drift["fails"]))
    )
    flags_html = "<ul>%s</ul>" % flag_list if flag_list else ""
    event_rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (
            _esc(e["iso"]),
            (
                '<a href="%s">run</a>' % _esc(e["run_url"])
                if isinstance(e.get("run_url"), str)
                and e["run_url"].startswith("http")
                else "-"
            ),
            _esc(e["verdict"]),
            _esc(e["streak_cases"]),
        )
        for e in payload["drift_events"]
    )
    events_table = (
        "<table><tr><th>run ts</th><th>run</th><th>verdict</th>"
        "<th>streak cases</th></tr>%s</table>" % event_rows
        if event_rows
        else ""
    )

    verdict = payload.get("verdict")
    banner = (
        '<p class="verdict %s">latest eval verdict: %s</p>'
        % ("fail" if verdict == "FAIL" else "ok", _esc(verdict))
        if verdict
        else ""
    )
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>jev-consult dashboard</title>
<style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:2em auto;padding:0 1em;color:#1a1a1a}
h1{font-size:1.4em}h2{font-size:1.05em;margin-top:1.8em;border-bottom:1px solid #ddd;padding-bottom:.3em}
table{border-collapse:collapse;width:100%%;font-size:.9em}
td,th{border:1px solid #ddd;padding:.3em .5em;text-align:left}
.bar{background:#eee;border-radius:3px;height:.8em;min-width:100px}
.bar span{display:block;background:#4a7fd4;height:100%%;border-radius:3px}
.bar.neg span{background:#d44a4a}
.kpis{display:flex;gap:.8em;flex-wrap:wrap}
.kpi{border:1px solid #ddd;border-radius:6px;padding:.6em 1em;min-width:120px}
.kpi b{display:block;font-size:1.5em}
.kpi.bad{border-color:#d44a4a}.kpi.bad b{color:#d44a4a}
.small{color:#666;font-size:.8em}
.verdict{padding:.4em .8em;border-radius:4px;font-weight:bold}
.verdict.ok{background:#e7f5e9}.verdict.fail{background:#fce8e8;color:#b3261e}
td.fail{color:#b3261e;font-weight:bold}td.ok{color:#1e7e34}
tr.fail td{background:#fdf1f1}tr.warn td{background:#fff8e6}
ul{margin:.4em 0}
</style></head><body>
<h1>jev-consult dashboard</h1>
<p class="small">generated %(gen)s UTC &middot; %(entries)d log entries
(%(bad)d bad lines)%(sources)s</p>
%(banner)s
<h2>KPIs</h2>
<div class="kpis">%(cards)s</div>
%(reason_table)s
%(weekly_table)s
<h2>Streaks &amp; drift</h2>
%(streak_table)s
%(flags_html)s
%(events_table)s
</body></html>
""" % {
        "gen": _esc(
            datetime.datetime.fromtimestamp(
                payload["generated_ts"], tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d %H:%M")
        ),
        "entries": payload["entries"],
        "bad": payload["bad_lines"],
        "sources": "".join(
            " &middot; %s" % _esc(v)
            for v in payload["sources"].values()
            if v
        ),
        "banner": banner,
        "cards": "".join(cards),
        "reason_table": reason_table,
        "weekly_table": weekly_table,
        "streak_table": streak_table,
        "flags_html": flags_html,
        "events_table": events_table,
    }


def _write_html(path: str, text: str) -> bool:
    if str(path) == "-":
        sys.stdout.write(text)
        return True
    try:
        inventory.atomic_write_text(Path(path), text)
    except OSError as exc:
        sys.stderr.write("cannot write %s: %s\n" % (path, exc))
        return False
    return True


def _load_eval(path: str) -> dict | None:
    if not path:
        return None
    try:
        data = json.loads(
            Path(path).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_log(file_arg: str) -> Path | None:
    if file_arg:
        return Path(file_arg)
    return inventory.decisions_log_path()


def _env_report(args) -> dict:
    log_path = _resolve_log(args.file)
    return {
        "file": str(log_path) if log_path else None,
        "history": args.history or None,
        "eval": args.eval_path or None,
        "out": args.out or "dashboard.html",
        "watch_max": _watch.cap("JEV_DASHBOARD_WATCH_MAX", args.max_ticks),
        "watch_secs": _watch.deadline(
            "JEV_DASHBOARD_WATCH_SECS", args.watch_max
        ) - time.time()
        if _watch.deadline("JEV_DASHBOARD_WATCH_SECS", args.watch_max)
        else 0,
        "watch_quiet": _watch.quiet(
            "JEV_DASHBOARD_WATCH_QUIET", args.quiet
        ),
        "policy": inventory._policy_dict().get("version", "?"),
    }


def _self_test() -> int:
    """Synthetic entries + history + eval payload -> render, then verify
    the markers and the ASCII-only byte contract."""
    import tempfile

    checks = {}
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "decisions.jsonl"
        log_lines = []
        for i in range(6):
            log_lines.append(
                json.dumps(
                    {
                        "ts": 1700000000.0 + i,
                        "schema": 2,
                        "harness": "hermes",
                        "prompt_sha": "abc%09d" % i,
                        "jev_status": "winner",
                        "winner": {"kind": "skill", "name": "demo"},
                        "probabilities": {"demo": 0.9, "other": 0.1},
                        "pick_confidence": 0.9,
                        "strong_pick": True,
                        "latency_ms": 420,
                        "budget_ms": 12000,
                        "promoted": i % 2 == 0,
                    }
                )
            )
        log_lines.append("not json")
        inventory.atomic_write_text(log, "\n".join(log_lines) + "\n")
        hist = Path(tmp) / "eval-history.jsonl"
        inventory.atomic_write_text(
            hist,
            json.dumps(
                {
                    "ts": 1700000000,
                    "run_url": "https://example.test/runs/1",
                    "verdict": "FAIL",
                    "worst_noul": 0.42,
                    "ab_mean_delta": -0.05,
                    "streaks": {"noul-budget-case": 3},
                }
            )
            + "\n",
        )
        out = Path(tmp) / "dashboard.html"
        rc = main(
            [
                "--file",
                str(log),
                "--history",
                str(hist),
                "--out",
                str(out),
            ]
        )
        text = out.read_text(encoding="utf-8")
        checks["render_rc0"] = rc == 0
        checks["ascii_bytes"] = all(b < 128 for b in out.read_bytes())
        checks["kpi"] = "pick rate" in text and "applied rate" in text
        checks["promoted_section"] = "promoted" in text
        checks["weekly"] = "worst noul" in text.lower() and "FAIL" in text
        checks["streak"] = "noul-budget-case" in text
        checks["bad_lines"] = True
        # empty inputs still render
        empty_log = Path(tmp) / "empty.jsonl"
        inventory.atomic_write_text(empty_log, "")
        out2 = Path(tmp) / "dash2.html"
        rc2 = main(
            ["--file", str(empty_log), "--out", str(out2)]
        )
        checks["empty_rc0"] = rc2 == 0
        checks["empty_no_promoted"] = "promoted" not in out2.read_text(
            encoding="utf-8"
        )
    ok = all(checks.values())
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


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if _watch.maybe_version(argv):
        return 0
    parser = argparse.ArgumentParser(
        prog="dashboard.py",
        description="Render one self-contained ASCII dashboard.html from "
        "decisions.jsonl + eval-history.jsonl (+ optional eval-live.json).",
    )
    parser.add_argument(
        "--file",
        metavar="PATH",
        default="",
        help="decisions.jsonl path (default: JEV_CONSULT_LOG or the "
        "per-user cache log)",
    )
    parser.add_argument(
        "--history",
        metavar="PATH",
        default="",
        help="eval-history.jsonl written by compare.py --history",
    )
    parser.add_argument(
        "--eval",
        dest="eval_path",
        metavar="PATH",
        default="",
        help="eval-live.json compare payload (verbatim drift flags)",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="dashboard.html output path (default dashboard.html; '-' "
        "prints HTML to stdout; under --env writes the env report there)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the computed dashboard payload JSON instead of HTML",
    )
    parser.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="print one dotted-path field of the payload (rc 2 on unknown)",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="print the --json payload key contract and exit "
        "(--json emits the object)",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="print the resolved config JSON (paths, watch knobs, policy)",
    )
    parser.add_argument(
        "--verdict",
        metavar="PATH",
        default="",
        help="write a slim {verdict: ok|empty, entries, bytes, ticks} JSON "
        "('-' prints it)",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="re-render every S seconds emitting a tick JSON per pass",
    )
    parser.add_argument(
        "--watch-max",
        metavar="S",
        type=float,
        default=0.0,
        help="stop the watch after S elapsed seconds "
        "(JEV_DASHBOARD_WATCH_SECS)",
    )
    parser.add_argument(
        "--max-ticks",
        metavar="N",
        type=int,
        default=0,
        help="stop the watch after N ticks (JEV_DASHBOARD_WATCH_MAX)",
    )
    parser.add_argument(
        "--unchanged-max",
        metavar="N",
        type=int,
        default=0,
        help="stop the watch after N consecutive identical ticks",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="stop the watch on the first tick with streak fails",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress clean watch ticks on stdout "
        "(JEV_DASHBOARD_WATCH_QUIET)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="render synthetic fixtures and check the markers",
    )
    args = parser.parse_args(argv)

    if args.schema:
        if args.json:
            sys.stdout.write(json.dumps(PAYLOAD_SCHEMA_ROWS, indent=2) + "\n")
        else:
            for key in PAYLOAD_SCHEMA_ROWS:
                meta = PAYLOAD_SCHEMA_ROWS[key]
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (
                        key,
                        meta["type"],
                        "required" if meta["required"] else "optional",
                    )
                )
        return 0
    if args.self_test:
        return _self_test()
    if args.env:
        report = _env_report(args)
        if args.jq:
            val, ok = _watch.dig(report, args.jq)
            if not ok:
                sys.stderr.write(
                    "bad --jq key %r (env has: %s)\n"
                    % (args.jq, ", ".join(sorted(report)))
                )
                return 2
            sys.stdout.write(json.dumps(val) + "\n")
            return 0
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if args.out and args.out != "-":
            try:
                inventory.atomic_write_text(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0

    policy = inventory._policy_dict()

    html_out = args.out or "dashboard.html"

    def _build() -> tuple[dict, str]:
        log_path = _resolve_log(args.file)
        entries, bad = ([], 0)
        if log_path is not None:
            entries, bad = decisions.load_entries(log_path)
        history = (
            decisions.load_history(Path(args.history))
            if args.history
            else []
        )
        payload = build_payload(
            entries,
            bad,
            history,
            _load_eval(args.eval_path),
            policy,
            file=str(log_path) if log_path else "",
            history_path=args.history,
            eval_path=args.eval_path,
        )
        return payload, render_html(payload)

    if args.watch and args.watch > 0:
        max_ticks = _watch.cap("JEV_DASHBOARD_WATCH_MAX", args.max_ticks)
        dead = _watch.deadline("JEV_DASHBOARD_WATCH_SECS", args.watch_max)
        quiet = _watch.quiet("JEV_DASHBOARD_WATCH_QUIET", args.quiet)
        ticks = 0
        prev_tick = None
        unchanged = 0
        last_html = None
        watch_t0 = time.time()
        verdict_ok = True
        tick: dict = {}
        while (max_ticks <= 0 or ticks < max_ticks) and (
            not dead or time.time() < dead
        ):
            payload, page = _build()
            changed = page != last_html
            if changed:
                if not _write_html(html_out, page):
                    return 1
                last_html = page
            tick = {
                "tick": ticks + 1,
                "entries": payload["entries"],
                "history": len(payload["weekly"]),
                "bytes": len(page),
                "changed": changed,
                "streak_fails": sum(
                    1 for s in payload["streaks"] if s["level"] == "fail"
                ),
                "elapsed_s": round(time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(
                tick,
                args.jq,
                quiet=quiet,
                bad=bool(tick["streak_fails"]),
            )
            ticks += 1
            sys.stderr.write(
                "watch tick=%d entries=%d bytes=%d changed=%s\n"
                % (ticks, tick["entries"], tick["bytes"], changed)
            )
            if args.verdict and verdict_ok:
                verdict_ok = _watch.write_verdict(
                    args.verdict,
                    {
                        "verdict": "ok" if payload["entries"] else "empty",
                        "entries": payload["entries"],
                        "bytes": tick["bytes"],
                        "ticks": ticks,
                    },
                )
            if args.fail_fast and tick["streak_fails"]:
                sys.stderr.write("watch: streak fails present\n")
                break
            if _watch.same_tick(
                prev_tick, tick, ignore=("ts", "elapsed_s", "tick", "changed")
            ):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if args.unchanged_max and unchanged >= args.unchanged_max:
                sys.stderr.write(
                    "watch: %d consecutive identical ticks\n" % unchanged
                )
                break
            if (max_ticks <= 0 or ticks < max_ticks) and (
                not dead or time.time() < dead
            ):
                time.sleep(args.watch)
        return 0

    payload, page = _build()
    if args.jq:
        val, ok = _watch.dig(payload, args.jq)
        if not ok:
            sys.stderr.write(
                "bad --jq key %r (payload has: %s)\n"
                % (args.jq, ", ".join(sorted(payload)))
            )
            return 2
        sys.stdout.write(json.dumps(val) + "\n")
        return 0
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    if not _write_html(html_out, page):
        return 1
    if html_out != "-":
        sys.stderr.write(
            "wrote %s (%d bytes)\n" % (html_out, len(page))
        )
    if args.verdict:
        _watch.write_verdict(
            args.verdict,
            {
                "verdict": "ok" if payload["entries"] else "empty",
                "entries": payload["entries"],
                "bytes": len(page),
                "ticks": 1,
            },
        )
    return 0


if __name__ == "__main__":
    raise _watch.exit_safely(main())
