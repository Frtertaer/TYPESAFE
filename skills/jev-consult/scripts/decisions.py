#!/usr/bin/env python3
"""Read decisions.jsonl and report routing stats for threshold calibration.

The hook appends one JSON line per routed prompt. This reads them back:
status mix, explicit/strong-pick rates, need_skill mean, latency percentiles.
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402
import inventory


def _parse_jsonl(text: str) -> tuple[list[dict], int]:
    entries: list[dict] = []
    bad = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(item, dict):
            entries.append(item)
        else:
            bad += 1
    return entries, bad


# The decisions.jsonl entry contract. Two writers: routing entries from
# inventory_hook.LAST_DECISION carry every required key; fill entries from
# apply_fill/catalog_fill carry only ts/harness/jev_status/fill/outcome/
# prompt_head. --schema prints rows like the other pack scripts: `key: type
# (required|optional)` text rows, or the object with --json.
ENTRY_SCHEMA_ROWS = {
    "ts": {"required": True, "type": "number, epoch seconds"},
    "harness": {"required": True, "type": "string, emitting harness"},
    "prompt_sha": {"required": True, "type": "string, 12-hex sha256 prefix of the prompt"},
    "prompt_head": {"required": True, "type": "string, first 160 redacted chars"},
    "prompt_tail": {"required": True, "type": "string, last 80 redacted chars"},
    "prompt_len": {"required": True, "type": "int, unredacted prompt length"},
    "prompt_truncated": {"required": True, "type": "bool"},
    "n_catalog": {"required": True, "type": "int, catalog size at emit time"},
    "shortlist_n": {"required": True, "type": "int, shortlisted skill count"},
    "shortlist": {"required": True, "type": "list[string], shortlisted skill ids"},
    "explicit": {"required": True, "type": "bool, env-forced winner"},
    "jev_status": {"required": True, "type": "string, routing outcome (idf|skip|winner|miss|budget|none|...)"},
    "reason": {"required": True, "type": "string, why this status"},
    "question": {"required": True, "type": "string|null, Jev question asked"},
    "need": {"required": True, "type": "object|null, Jev ask payload"},
    "probabilities": {"required": True, "type": "object{option: p}, Jev softmax"},
    "shortlist_score_avg": {"required": True, "type": "number, mean IDF score of picks"},
    "winner": {"required": True, "type": "{kind, name}|null, applied pick"},
    "strong_pick": {"required": True, "type": "bool"},
    "latency_ms": {"required": True, "type": "number|null, Jev call latency"},
    "budget_ms": {"required": True, "type": "int, configured hook budget"},
    "over_budget": {"required": True, "type": "bool, latency exceeded budget"},
    "stale_sidecar": {"required": True, "type": "bool, a stale sidecar was auto-pruned"},
    "sidecar_age_s": {"required": True, "type": "int|null, age of the pruned sidecar"},
    "note": {"required": False, "type": "string, extra note tag (written only when set)"},
    "fill": {"required": False, "type": "string, fill writer (apply|catalog|peer) — fill entries only"},
    "outcome": {"required": False, "type": "string, first word of the fill result — fill entries only"},
}


_STDIN_TEXT: str | None = None


def _log_text(path: Path) -> str | None:
    """Read the log; the '-' path reads stdin once and caches it."""
    global _STDIN_TEXT
    if str(path) == "-":
        if _STDIN_TEXT is None:
            _STDIN_TEXT = sys.stdin.read()
        return _STDIN_TEXT
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def load_entries(path: Path) -> tuple[list[dict], int]:
    text = _log_text(path)
    if text is None:
        return [], 0
    return _parse_jsonl(text)


def load_bad_lines(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, raw)] for lines that failed to parse as JSON objects."""
    bad_rows: list[tuple[int, str]] = []
    text = _log_text(path)
    if text is None:
        return bad_rows
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except ValueError:
            bad_rows.append((lineno, stripped))
            continue
        if not isinstance(item, dict):
            bad_rows.append((lineno, stripped))
    return bad_rows


def verify_log(path: Path) -> dict:
    """Integrity check: parseable lines, ts present and non-decreasing,
    jev_status present. Returns {ok, entries, bad_lines, problems}."""
    problems: list[dict] = []
    entries: list[dict] = []
    text = _log_text(path)
    if text is None:
        return {"ok": False, "entries": 0, "bad_lines": 0,
                "problems": [{"line": 0, "issue": "unreadable"}]}
    lines = text.splitlines()
    bad = 0
    prev_ts: float | None = None
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except ValueError:
            bad += 1
            problems.append({"line": lineno, "issue": "unparseable"})
            continue
        if not isinstance(item, dict):
            bad += 1
            problems.append({"line": lineno, "issue": "not an object"})
            continue
        entries.append(item)
        ts = item.get("ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            problems.append({"line": lineno, "issue": "missing ts"})
        elif prev_ts is not None and ts < prev_ts:
            problems.append({"line": lineno, "issue": "ts regression"})
        else:
            prev_ts = ts if prev_ts is None else max(prev_ts, ts)
        if "jev_status" not in item:
            problems.append({"line": lineno, "issue": "missing jev_status"})
        if item.get("jev_status") == "fill":
            want = inventory.FILL_SCHEMA_ROWS
        elif "prompt_sha" in item or "shortlist" in item:
            want = ENTRY_SCHEMA_ROWS
        else:
            # minimal entry (tests, hand-written): ts/jev_status checks only
            want = {}
        missing = [
            key
            for key, meta in want.items()
            if meta.get("required") and key not in item
        ]
        if missing:
            problems.append(
                {"line": lineno, "issue": "missing keys: %s" % ",".join(missing)}
            )
    return {
        "ok": not problems,
        "entries": len(entries),
        "bad_lines": bad,
        "problems": problems,
    }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


def summarize(entries: list[dict], bad: int = 0) -> dict:
    by_status: dict[str, int] = {}
    by_harness: dict[str, int] = {}
    winners: dict[str, int] = {}
    needs: list[float] = []
    latencies: list[float] = []
    shortlists: list[float] = []
    prompts: dict[str, int] = {}
    stamps: list[float] = []
    explicit = strong = 0
    for item in entries:
        status = str(item.get("jev_status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        harness = str(item.get("harness") or "unknown")
        by_harness[harness] = by_harness.get(harness, 0) + 1
        if item.get("explicit"):
            explicit += 1
        if item.get("strong_pick"):
            strong += 1
        head = str(item.get("prompt_head") or "").strip()[:120]
        if head:
            prompts[head] = prompts.get(head, 0) + 1
        winner = item.get("winner")
        if isinstance(winner, dict) and winner.get("name"):
            key = "%s:%s" % (winner.get("kind") or "?", winner["name"])
            winners[key] = winners.get(key, 0) + 1
        need = item.get("need")
        if isinstance(need, (int, float)):
            needs.append(float(need))
        latency = item.get("latency_ms")
        if isinstance(latency, (int, float)):
            latencies.append(float(latency))
        sl = item.get("shortlist_n")
        if isinstance(sl, (int, float)) and not isinstance(sl, bool):
            shortlists.append(float(sl))
        ts = item.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            stamps.append(float(ts))
    return {
        "total": len(entries),
        "bad_lines": bad,
        "first_ts": min(stamps) if stamps else None,
        "last_ts": max(stamps) if stamps else None,
        "first_iso": _iso(min(stamps)) if stamps else None,
        "last_iso": _iso(max(stamps)) if stamps else None,
        "by_status": by_status,
        "by_harness": by_harness,
        "explicit": explicit,
        "strong_pick": strong,
        "need_skill": {
            "n": len(needs),
            "mean": round(sum(needs) / len(needs), 4) if needs else None,
            "p50": _percentile(needs, 0.5),
            "p90": _percentile(needs, 0.9),
        },
        "latency_ms": {
            "n": len(latencies),
            "mean": round(sum(latencies) / len(latencies), 1) if latencies else None,
            "p50": _percentile(latencies, 0.5),
            "p90": _percentile(latencies, 0.9),
            "max": max(latencies) if latencies else None,
        },
        "shortlist_n": {
            "n": len(shortlists),
            "mean": round(sum(shortlists) / len(shortlists), 2) if shortlists else None,
            "min": min(shortlists) if shortlists else None,
            "max": max(shortlists) if shortlists else None,
        },
        "top_winners": dict(
            sorted(winners.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        ),
        "top_prompts": dict(
            sorted(prompts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        ),
    }


def format_stats(stats: dict) -> str:
    lines = [
        "entries: %(total)d (bad lines: %(bad_lines)d)" % stats,
        "status: " + ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(stats["by_status"].items())
        ),
        "harness: " + ", ".join(
            "%s=%d" % (k, v) for k, v in sorted(stats["by_harness"].items())
        ),
        "explicit: %d  strong_pick: %d" % (stats["explicit"], stats["strong_pick"]),
    ]
    if stats.get("first_ts") is not None:
        duration = max(0.0, stats["last_ts"] - stats["first_ts"])
        days = duration / 86400
        human = "%.1fd" % days if days >= 1 else "%.1fh" % (duration / 3600)
        lines.append(
            "span: first_ts=%s last_ts=%s (%s)" % (stats["first_ts"], stats["last_ts"], human)
        )
    need = stats["need_skill"]
    if need["n"]:
        lines.append(
            "need_skill: n=%d mean=%s p50=%s p90=%s"
            % (need["n"], need["mean"], need["p50"], need["p90"])
        )
    latency = stats["latency_ms"]
    if latency["n"]:
        lines.append(
            "latency_ms: n=%d mean=%s p50=%s p90=%s max=%s"
            % (latency["n"], latency["mean"], latency["p50"], latency["p90"], latency["max"])
        )
    sl = stats["shortlist_n"]
    if sl["n"]:
        lines.append(
            "shortlist_n: n=%d mean=%s min=%s max=%s"
            % (sl["n"], sl["mean"], sl["min"], sl["max"])
        )
    if stats["top_winners"]:
        lines.append(
            "top winners: "
            + ", ".join(
                "%s=%d" % (k, v) for k, v in stats["top_winners"].items()
            )
        )
    if stats["top_prompts"]:
        lines.append("top prompts:")
        lines.extend(
            "  %3d  %s" % (count, head)
            for head, count in stats["top_prompts"].items()
        )
    return "\n".join(lines)


def format_entry(item: dict) -> str:
    winner = item.get("winner") or {}
    name = winner.get("name") if isinstance(winner, dict) else None
    return "%s %-10s %-9s %s %s" % (
        time_str(item.get("ts")),
        item.get("harness") or "?",
        item.get("jev_status") or "?",
        name or "-",
        str(item.get("prompt_head") or "")[:80],
    )


def _iso(ts: float) -> str | None:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
    except (OverflowError, OSError, ValueError):
        return None


def time_str(ts: object) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def filter_status(entries: list[dict], status: str | None) -> list[dict]:
    if not status:
        return entries
    wanted = {part.strip() for part in status.split(",") if part.strip()}
    return [
        item
        for item in entries
        if str(item.get("jev_status") or "unknown") in wanted
    ]


def filter_harness(entries: list[dict], harness: str | None) -> list[dict]:
    if not harness:
        return entries
    wanted = {part.strip() for part in harness.split(",") if part.strip()}
    return [item for item in entries if str(item.get("harness") or "") in wanted]


def filter_outcome(entries: list[dict], outcome: str | None) -> list[dict]:
    if not outcome:
        return entries
    wanted = {part.strip() for part in outcome.split(",") if part.strip()}
    return [item for item in entries if str(item.get("outcome") or "") in wanted]


def filter_fill(entries: list[dict], fill: str | None) -> list[dict]:
    if not fill:
        return entries
    wanted = {part.strip() for part in fill.split(",") if part.strip()}
    return [item for item in entries if str(item.get("fill") or "") in wanted]


def filter_field(entries: list[dict], spec: str | None) -> list[dict]:
    """Generic KEY=VALUE equality filter; bad specs (no =) match nothing."""
    if not spec:
        return entries
    if "=" not in spec:
        return []
    key, _, value = spec.partition("=")
    key = key.strip()
    value = value.strip()

    def dig(item: dict):
        value, found = _watch.dig(item, key)
        return value if found else None

    return [item for item in entries if str(dig(item) or "") == value]


def _dig(item: dict, key: str):
    value, found = _watch.dig(item, key)
    return value if found else None


def group_by(entries: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in entries:
        value = _dig(item, field)
        if value is None or value == "":
            value = "unknown"
        elif isinstance(value, bool):
            value = str(value)
        elif not isinstance(value, (int, float, str)):
            value = json.dumps(value, sort_keys=True)
        else:
            value = str(value)
        counts[value] = counts.get(value, 0) + 1
    return counts


MISS_STATUSES = {"none", "idf", "empty", "error"}


def _entry_ts(item: dict) -> float | None:
    ts = item.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    return None


def _entry_key(item: dict) -> str:
    return str(
        item.get("sha")
        or item.get("ts")
        or json.dumps(item, sort_keys=True, default=str)
    )


def is_miss_entry(item: dict) -> bool:
    if str(item.get("jev_status") or "") not in MISS_STATUSES:
        return False
    if item.get("jev_pick") or item.get("winner"):
        return False
    return True


def fill_gaps(entries: list[dict], now: float | None = None) -> list[dict]:
    """Per-harness miss events (no winner) with no later same-prompt fill.

    Rows gain ``oldest_open_ts`` (earliest ts among still-open misses, or
    None) and ``age_s`` (``now`` minus that ts) so triage can sort by
    staleness; ``now`` is injectable for deterministic tests.
    """
    fills = [
        item
        for item in entries
        if str(item.get("jev_status") or "") == "fill"
    ]
    groups: dict[str, dict] = {}
    for item in entries:
        if not is_miss_entry(item):
            continue
        harness = str(item.get("harness") or "unknown")
        group = groups.setdefault(
            harness,
            {
                "harness": harness,
                "misses": 0,
                "filled": 0,
                "open": 0,
                "examples": [],
                "oldest_open_ts": None,
            },
        )
        group["misses"] += 1
        head = str(item.get("prompt_head") or "")[:120]
        ts = _entry_ts(item)
        hit = False
        for fill in fills:
            if str(fill.get("harness") or "unknown") != harness:
                continue
            if str(fill.get("prompt_head") or "")[:120] != head:
                continue
            fts = _entry_ts(fill)
            if ts is None or fts is None or fts >= ts:
                hit = True
                break
        if hit:
            group["filled"] += 1
        else:
            group["open"] += 1
            if ts is not None and (
                group["oldest_open_ts"] is None or ts < group["oldest_open_ts"]
            ):
                group["oldest_open_ts"] = ts
            if head and len(group["examples"]) < 3:
                group["examples"].append(head[:60])
    if now is None:
        now = time.time()
    rows = list(groups.values())
    for row in rows:
        oldest = row["oldest_open_ts"]
        row["age_s"] = round(now - oldest, 1) if oldest is not None else None
        row["fill_rate"] = round(row["filled"] / row["misses"], 3) if row["misses"] else None
    return sorted(rows, key=lambda g: (-g["open"], g["harness"]))


def format_fill_gaps(rows: list[dict]) -> str:
    if not rows:
        return "no miss entries"
    lines = ["harness        misses  filled  open   rate    age_s      examples"]
    for row in rows:
        age = row.get("age_s")
        rate = row.get("fill_rate")
        lines.append(
            "%-14s %-7d %-7d %-6d %-7s %-10s %s"
            % (
                row["harness"],
                row["misses"],
                row["filled"],
                row["open"],
                "-" if rate is None else "%.3f" % rate,
                "-" if age is None else "%.1f" % age,
                "; ".join(row["examples"]),
            )
        )
    return "\n".join(lines)


def evidence_report(entries: list[dict], now: float | None = None) -> dict:
    """Compact routing-evidence block for PRs and reviews."""
    statuses: dict[str, int] = {}
    harnesses: dict[str, int] = {}
    winners: dict[str, int] = {}
    ts_min: float | None = None
    ts_max: float | None = None
    for item in entries:
        if not isinstance(item, dict):
            continue
        status = str(item.get("jev_status") or "unknown")
        statuses[status] = statuses.get(status, 0) + 1
        harness = str(item.get("harness") or "?")
        harnesses[harness] = harnesses.get(harness, 0) + 1
        winner = item.get("winner")
        if isinstance(winner, dict) and winner.get("name"):
            key = "%s:%s" % (winner.get("kind") or "?", winner["name"])
            winners[key] = winners.get(key, 0) + 1
        ts = item.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            ts_min = ts if ts_min is None else min(ts_min, ts)
            ts_max = ts if ts_max is None else max(ts_max, ts)
    gaps = fill_gaps(entries, now=now)
    open_by_harness = {row["harness"]: row["open"] for row in gaps if row["open"]}

    def _sorted(d: dict[str, int]) -> dict[str, int]:
        return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))

    def _iso(ts: float | None) -> str | None:
        if ts is None:
            return None
        return (
            datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%SZ")
        )

    return {
        "entries": len(entries),
        "span": [_iso(ts_min), _iso(ts_max)],
        "statuses": _sorted(statuses),
        "harnesses": _sorted(harnesses),
        "winners": _sorted(winners),
        "open_misses": sum(open_by_harness.values()),
        "open_misses_by_harness": _sorted(open_by_harness),
    }


def format_evidence(data: dict) -> str:
    def _kv(d: dict[str, int]) -> str:
        return ", ".join("%s %d" % kv for kv in d.items()) or "none"

    span = data.get("span") or [None, None]
    span_txt = " .. ".join(x or "?" for x in span)
    lines = [
        "## Jev routing evidence",
        "",
        "- entries: %d (%s)" % (data.get("entries") or 0, span_txt),
        "- statuses: " + _kv(data.get("statuses") or {}),
        "- winners: " + _kv(data.get("winners") or {}),
        "- open misses: %d%s"
        % (
            data.get("open_misses") or 0,
            " (%s)" % _kv(data.get("open_misses_by_harness") or {})
            if data.get("open_misses")
            else "",
        ),
    ]
    return "\n".join(lines)


def quiet_gaps(entries: list[dict], min_seconds: float) -> list[dict]:
    """Quiet periods: consecutive timestamps (sorted) more than min_seconds apart."""
    stamps = sorted(
        ts for ts in (_entry_ts(item) for item in entries) if ts is not None
    )
    gaps = []
    for prev, cur in zip(stamps, stamps[1:]):
        delta = cur - prev
        if delta > min_seconds:
            gaps.append({"from_ts": prev, "to_ts": cur, "seconds": delta})
    return gaps


def silent_harnesses(
    entries: list[dict], seconds: float, now: float | None = None
) -> list[dict]:
    """Harnesses whose newest filtered entry is older than `seconds` ago."""
    now = now if now is not None else time.time()
    last: dict[str, float] = {}
    for item in entries:
        ts = _entry_ts(item)
        if ts is None:
            continue
        harness = str(item.get("harness") or "-")
        if harness not in last or ts > last[harness]:
            last[harness] = ts
    rows = [
        {"harness": harness, "last_ts": ts, "age_s": round(now - ts, 1)}
        for harness, ts in last.items()
        if now - ts > seconds
    ]
    return sorted(rows, key=lambda row: -row["age_s"])


def format_silent(rows: list[dict], seconds: float) -> str:
    if not rows:
        return "silent: none (threshold %ss)" % int(seconds)
    lines = []
    for row in rows:
        lines.append(
            "%s last=%s age=%ss"
            % (row["harness"], _iso_full(row["last_ts"]) or "?", int(row["age_s"]))
        )
    return "\n".join(lines)


def status_streaks(entries: list[dict]) -> list[dict]:
    """Per harness: current and longest runs of consecutive same jev_status."""
    by_harness: dict[str, list[dict]] = {}
    for item in entries:
        by_harness.setdefault(str(item.get("harness") or "-"), []).append(item)
    rows: list[dict] = []
    for harness in sorted(by_harness):
        items = sorted(
            by_harness[harness],
            key=lambda i: (_entry_ts(i) is None, _entry_ts(i) or 0.0),
        )
        best_status, best_n = "", 0
        cur_status, cur_n = "", 0
        for it in items:
            st = str(it.get("jev_status") or "-")
            if st == cur_status:
                cur_n += 1
            else:
                if cur_n > best_n:
                    best_status, best_n = cur_status, cur_n
                cur_status, cur_n = st, 1
        if cur_n > best_n:
            best_status, best_n = cur_status, cur_n
        rows.append(
            {
                "harness": harness,
                "entries": len(items),
                "current_status": cur_status,
                "current_streak": cur_n,
                "best_status": best_status,
                "best_streak": best_n,
            }
        )
    return sorted(rows, key=lambda r: (-r["best_streak"], r["harness"]))


# jev_status values that mean no Jev call happened — dedupe replays a
# sidecar pick, idf shortlists locally; only attempted calls belong in a
# timeout/error rate denominator.
_NO_CALL_STATUSES = {"dedupe", "idf", "skip", "disabled", ""}


def harness_health(entries: list[dict]) -> list[dict]:
    """Per (harness, UTC hour) timeout/error rates — a cheap quota signal.

    jev_status 'timeout'/'error' come from the hook when the Jev call
    stalls or fails; the window bucket is the hour the entry logged.
    error_rate/timeout_rate divide by entries that actually attempted a
    Jev call, so dedupe/idf records don't dilute the signal."""
    buckets: dict[tuple[str, str], dict] = {}
    for item in entries:
        harness = str(item.get("harness") or "unknown")
        ts = _entry_ts(item)
        if ts is None:
            window = "unknown"
        else:
            window = datetime.datetime.fromtimestamp(
                ts, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:00Z")
        row = buckets.setdefault(
            (harness, window),
            {
                "harness": harness,
                "window": window,
                "entries": 0,
                "attempted": 0,
                "errors": 0,
                "timeouts": 0,
            },
        )
        row["entries"] += 1
        status = str(item.get("jev_status") or "")
        if status not in _NO_CALL_STATUSES:
            row["attempted"] += 1
        if status == "error":
            row["errors"] += 1
        elif status == "timeout":
            row["timeouts"] += 1
    rows = []
    for r in buckets.values():
        n = r["attempted"]
        rows.append(
            dict(
                r,
                error_rate=round(r["errors"] / n, 4) if n else 0.0,
                timeout_rate=round(r["timeouts"] / n, 4) if n else 0.0,
            )
        )
    return sorted(rows, key=lambda r: (r["harness"], r["window"]))


HEALTH_COLS = [
    "harness", "window", "entries", "attempted",
    "errors", "timeouts", "error_rate", "timeout_rate",
]


def format_health(rows: list[dict]) -> str:
    if not rows:
        return "no entries"
    lines = [
        "harness     window               entries  attempted  errors  timeouts  error_rate  timeout_rate"
    ]
    for r in rows:
        lines.append(
            "%-11s %-20s %-8d %-9d %-7d %-9d %-11g %g"
            % (
                r["harness"],
                r["window"],
                r["entries"],
                r["attempted"],
                r["errors"],
                r["timeouts"],
                r["error_rate"],
                r["timeout_rate"],
            )
        )
    return "\n".join(lines)


def prompt_chains(entries: list[dict], min_n: int = 2) -> list[dict]:
    """prompt_head values consulted at least MIN_N times (loop detector)."""
    grouped: dict[str, list[dict]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        head = str(item.get("prompt_head") or "").strip()
        if not head:
            continue
        grouped.setdefault(head, []).append(item)
    rows: list[dict] = []
    for head, items in grouped.items():
        if len(items) < min_n:
            continue
        tss = [t for t in (_entry_ts(i) for i in items) if t is not None]
        rows.append(
            {
                "prompt_head": head[:120],
                "count": len(items),
                "first_ts": min(tss) if tss else None,
                "last_ts": max(tss) if tss else None,
                "statuses": sorted(
                    {str(i.get("jev_status") or "unknown") for i in items}
                ),
            }
        )
    return sorted(rows, key=lambda r: (-r["count"], r["prompt_head"]))


def format_chains(rows: list[dict]) -> str:
    if not rows:
        return "no chains"
    lines = []
    for row in rows:
        lines.append(
            "%dx %s [%s] %s -> %s"
            % (
                row["count"],
                row["prompt_head"],
                ",".join(row["statuses"]),
                _iso_full(row["first_ts"]) or "?",
                _iso_full(row["last_ts"]) or "?",
            )
        )
    return "\n".join(lines)


def format_streaks(rows: list[dict]) -> str:
    if not rows:
        return "no entries"
    lines = ["harness  entries  cur           best"]
    for row in rows:
        lines.append(
            "%-8s %-8d %-13s %s"
            % (
                row["harness"],
                row["entries"],
                "%sx%d" % (row["current_status"], row["current_streak"]),
                "%sx%d" % (row["best_status"], row["best_streak"]),
            )
        )
    return "\n".join(lines)


def _iso_full(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        .strftime("%Y-%m-%d %H:%M:%SZ")
    )


def format_gaps(gaps: list[dict]) -> str:
    if not gaps:
        return "no quiet periods"
    lines = ["seconds     from                 to"]
    for gap in gaps:
        lines.append(
            "%-11d %-20s %s"
            % (
                int(gap["seconds"]),
                _iso_full(gap["from_ts"]) or "?",
                _iso_full(gap["to_ts"]) or "?",
            )
        )
    return "\n".join(lines)


def _ts_arg(raw: str) -> float | None:
    """Parse an epoch-seconds or ISO8601 timestamp argument. Empty -> None."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("bad timestamp %r (want epoch seconds or ISO8601)" % raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def filter_since(entries: list[dict], since: float | None) -> list[dict]:
    if since is None:
        return entries
    out = []
    for item in entries:
        try:
            ts = float(item.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts >= since:
            out.append(item)
    return out


def filter_until(entries: list[dict], until: float | None) -> list[dict]:
    if until is None:
        return entries
    out = []
    for item in entries:
        try:
            ts = float(item.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts <= until:
            out.append(item)
    return out


def prune_entries(path: Path, apply_filters, retries: int = 8, archive=None) -> dict | None:
    """Rewrite `path` keeping entries that pass `apply_filters`.

    Snapshot-checked: the log is append-only, so the rewrite happens only
    when the file is byte-identical to the snapshot the kept list was
    filtered from; an appended or rewritten file retries with a fresh
    snapshot. A file that does not end in a newline may have a line being
    appended right now and is treated as busy. Returns a stats dict or
    None when the log kept changing underneath us.

    With `archive` set, the dropped entries are appended to that JSONL path
    just before the rewrite commits (a crash between the two may re-archive
    them on the next run -- dedupe-safe, never lossy).
    """
    for _ in range(retries):
        snap = path.read_bytes()
        if snap and not snap.endswith(b"\n"):
            time.sleep(0.05)
            continue
        entries, bad = _parse_jsonl(snap.decode("utf-8", errors="replace"))
        kept = apply_filters(entries)
        kept_ids = {id(item) for item in kept}
        dropped = [item for item in entries if id(item) not in kept_ids]
        fd, tmp = tempfile.mkstemp(
            prefix=path.name + ".", dir=str(path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
                for item in kept:
                    out.write(json.dumps(item, sort_keys=True) + "\n")
            if path.read_bytes() == snap:
                if archive is not None and dropped:
                    with Path(archive).open("a", encoding="utf-8", newline="\n") as fh:
                        for item in dropped:
                            fh.write(json.dumps(item, sort_keys=True) + "\n")
                _watch.atomic_replace(tmp, str(path))
                return {
                    "total": len(entries),
                    "kept": len(kept),
                    "dropped": len(entries) - len(kept),
                    "bad": bad,
                    "archived": len(dropped) if archive is not None else 0,
                }
            os.unlink(tmp)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        time.sleep(0.05)
    return None


def merge_log(path: Path, other: Path) -> dict:
    """Append OTHER's entries not already in PATH (identity = _entry_key).

    Append-only: PATH is never rewritten, and a trailing partial line is
    completed before the new rows land. Stats dict: added/skipped/total/
    bad_lines (unparseable lines in OTHER).
    """
    cur_text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    cur, _cur_bad = _parse_jsonl(cur_text)
    incoming, inc_bad = _parse_jsonl(other.read_text(encoding="utf-8", errors="replace"))
    seen = {_entry_key(item) for item in cur}
    new = [item for item in incoming if _entry_key(item) not in seen]
    if new:
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            if cur_text and not cur_text.endswith("\n"):
                fh.write("\n")
            for item in new:
                fh.write(json.dumps(item, sort_keys=True) + "\n")
    return {
        "added": len(new),
        "skipped": len(incoming) - len(new),
        "total": len(cur) + len(new),
        "bad_lines": inc_bad,
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
        description="Stats over ~/.cache/jev-consult/decisions.jsonl."
    )
    parser.add_argument("--file", help="Override decisions.jsonl path ('-' reads the JSONL log from stdin)")
    try:
        env_tail = int(os.environ.get("JEV_DECISIONS_TAIL", "") or 0)
    except ValueError:
        env_tail = 0
    try:
        env_first = int(os.environ.get("JEV_DECISIONS_FIRST", "") or 0)
    except ValueError:
        env_first = 0
    parser.add_argument("--tail", type=int, default=max(0, env_tail), help="Print last N entries")
    parser.add_argument("--first", type=int, default=max(0, env_first), help="Print first N entries")
    try:
        env_top = int(os.environ.get("JEV_DECISIONS_TOP", "") or 0)
    except ValueError:
        env_top = 0
    parser.add_argument(
        "--top", type=int, default=max(0, env_top), help="Cap count-list output (--statuses et al.) and --verify problem rows to N rows"
    )
    try:
        env_days = float(os.environ.get("JEV_DECISIONS_DAYS", "") or 0)
    except ValueError:
        env_days = 0.0
    parser.add_argument("--days", type=float, default=max(0.0, env_days), help="Only entries from the last N days")
    parser.add_argument(
        "--week", action="store_true", help="Alias for --days 7"
    )
    parser.add_argument("--since", default=os.environ.get("JEV_DECISIONS_SINCE", ""), help="Only entries with ts >= epoch seconds or ISO8601")
    parser.add_argument("--until", default=os.environ.get("JEV_DECISIONS_UNTIL", ""), help="Only entries with ts <= epoch seconds or ISO8601")
    parser.add_argument(
        "--since-last",
        dest="since_last",
        metavar="STATUS",
        nargs="?",
        const="ok",
        default="",
        help="Only entries logged after the newest entry whose jev_status is STATUS (default: ok).",
    )
    parser.add_argument(
        "--until-last",
        dest="until_last",
        metavar="STATUS",
        nargs="?",
        const="ok",
        default="",
        help="Only entries logged before the newest entry whose jev_status is STATUS (default: ok) — the complement of --since-last.",
    )
    parser.add_argument("--harness", default=os.environ.get("JEV_DECISIONS_HARNESS", ""), help="Only entries for this harness")
    parser.add_argument("--status", default=os.environ.get("JEV_DECISIONS_STATUS", ""), help="Only entries with this jev_status")
    parser.add_argument("--outcome", default=os.environ.get("JEV_DECISIONS_OUTCOME", ""), help="Only entries with this outcome (e.g. human, blocked)")
    parser.add_argument("--fill", default=os.environ.get("JEV_DECISIONS_FILL", ""), help="Only entries with this fill kind (apply, catalog, peer)")
    parser.add_argument("--field", default=os.environ.get("JEV_DECISIONS_FIELD", ""), help="Generic filter: KEY=VALUE equality on any entry field (a.b digs into nested objects)")
    parser.add_argument("--prompt", default=os.environ.get("JEV_DECISIONS_PROMPT", ""), help="Only entries whose prompt_head/prompt_tail contain this substring (case-insensitive)")
    parser.add_argument("--reason", default=os.environ.get("JEV_DECISIONS_REASON", ""), help="Only entries whose reason field contains this substring (case-insensitive)")
    parser.add_argument("--grep", default=os.environ.get("JEV_DECISIONS_GREP", ""), help="Only entries where any string field contains this substring (case-insensitive, one nesting level deep); '-' reads SUBSTR from stdin")
    env_min_need = os.environ.get("JEV_DECISIONS_MIN_NEED", "").strip()
    try:
        env_min_need = float(env_min_need) if env_min_need else None
    except ValueError:
        env_min_need = None
    parser.add_argument("--min-need", type=float, default=env_min_need, help="Only entries with numeric need >= F")
    env_min_lat = os.environ.get("JEV_DECISIONS_MIN_LATENCY", "").strip()
    try:
        env_min_lat = float(env_min_lat) if env_min_lat else None
    except ValueError:
        env_min_lat = None
    parser.add_argument("--min-latency", type=float, default=env_min_lat, help="Only entries with numeric latency_ms >= MS")
    parser.add_argument("--winner", default=os.environ.get("JEV_DECISIONS_WINNER", ""), help="Only entries whose winner name or kind:name equals NAME")
    env_explicit = os.environ.get("JEV_DECISIONS_EXPLICIT", "").strip().lower() in ("1", "true", "yes")
    parser.add_argument("--explicit", action="store_true", default=env_explicit, help="Only entries with explicit=true")
    parser.add_argument("--question", default=os.environ.get("JEV_DECISIONS_QUESTION", ""), help="Only entries with this question kind (e.g. load_tools, explicit, env, dedupe)")
    env_dedupe = os.environ.get("JEV_DECISIONS_DEDUPE", "").strip().lower() in ("1", "true", "yes")
    parser.add_argument("--dedupe-only", dest="dedupe_only", action="store_true", default=env_dedupe, help="Only entries with dedupe=true")
    env_stale = os.environ.get("JEV_DECISIONS_STALE", "").strip().lower() in ("1", "true", "yes")
    parser.add_argument("--stale", action="store_true", default=env_stale, help="Only entries with stale_sidecar=true")
    parser.add_argument("--sha", default=os.environ.get("JEV_DECISIONS_SHA", ""), help="Only entries whose prompt_sha starts with PREFIX")
    env_max_need = os.environ.get("JEV_DECISIONS_MAX_NEED", "")
    try:
        env_max_need = float(env_max_need) if env_max_need else None
    except ValueError:
        env_max_need = None
    parser.add_argument("--max-need", type=float, default=env_max_need, help="Only entries with numeric need <= F")
    env_max_lat = os.environ.get("JEV_DECISIONS_MAX_LATENCY", "")
    try:
        env_max_lat = float(env_max_lat) if env_max_lat else None
    except ValueError:
        env_max_lat = None
    parser.add_argument("--max-latency", type=float, default=env_max_lat, help="Only entries with numeric latency_ms <= MS")
    env_over_budget = os.environ.get("JEV_DECISIONS_OVER_BUDGET", "").strip().lower() in ("1", "true", "yes")
    parser.add_argument("--over-budget", dest="over_budget", action="store_true", default=env_over_budget, help="Only entries with over_budget=true")
    env_strong = os.environ.get("JEV_DECISIONS_STRONG", "").strip().lower() in ("1", "true", "yes")
    parser.add_argument("--strong", action="store_true", default=env_strong, help="Only entries with strong_pick=true")
    env_min_score = os.environ.get("JEV_DECISIONS_MIN_SCORE", "")
    try:
        env_min_score = float(env_min_score) if env_min_score else None
    except ValueError:
        env_min_score = None
    parser.add_argument("--min-score", type=float, default=env_min_score, help="Only entries with numeric shortlist_score_avg >= F")
    env_min_cat = os.environ.get("JEV_DECISIONS_MIN_CATALOG", "")
    try:
        env_min_cat = float(env_min_cat) if env_min_cat else None
    except ValueError:
        env_min_cat = None
    parser.add_argument("--min-catalog", type=float, default=env_min_cat, help="Only entries with numeric n_catalog >= N")
    parser.add_argument("--reverse", action="store_true", help="Print listed entries newest-first (--out/--jsonl/--csv/--md/--jq/--tail/--first)")
    env_min_sl = os.environ.get("JEV_DECISIONS_MIN_SHORTLIST", "")
    try:
        env_min_sl = float(env_min_sl) if env_min_sl else None
    except ValueError:
        env_min_sl = None
    parser.add_argument("--min-shortlist", type=float, default=env_min_sl, help="Only entries with numeric shortlist_n >= N")
    env_min_plen = os.environ.get("JEV_DECISIONS_MIN_PROMPT_LEN", "")
    try:
        env_min_plen = float(env_min_plen) if env_min_plen else None
    except ValueError:
        env_min_plen = None
    parser.add_argument("--min-prompt-len", type=float, default=env_min_plen, help="Only entries with numeric prompt_len >= N")
    parser.add_argument(
        "--where",
        action="append",
        metavar="KEY=VAL",
        default=None,
        help="Keep entries whose KEY field (dotted dig) string-equals VAL; repeatable",
    )
    parser.add_argument(
        "--where-not",
        action="append",
        metavar="KEY=VAL",
        default=None,
        help="Drop entries whose KEY field (dotted dig) string-equals VAL; repeatable",
    )
    parser.add_argument(
        "--missing",
        metavar="FIELD",
        default=os.environ.get("JEV_DECISIONS_MISSING", ""),
        help="Only entries lacking FIELD (dotted dig resolves to None)",
    )
    parser.add_argument(
        "--statuses",
        action="store_true",
        help="Print unique jev_status values with counts, sorted desc",
    )
    parser.add_argument(
        "--harnesses",
        action="store_true",
        help="Print unique harness values with counts, sorted desc",
    )
    parser.add_argument(
        "--never-picked",
        action="store_true",
        help="List installed items never recorded as a winner in the filtered entries (scans every --harness value, or all four when unset)",
    )
    parser.add_argument(
        "--home",
        default="",
        help="Override the harness home root (test override; --never-picked only)",
    )
    parser.add_argument(
        "--hermes-home",
        default="",
        help="Override the Hermes root (test override; --never-picked only)",
    )
    parser.add_argument(
        "--winners",
        action="store_true",
        help="Print unique winner kind:name pairs with counts, sorted desc",
    )
    parser.add_argument(
        "--outcomes",
        action="store_true",
        help="Print unique outcome values with counts, sorted desc",
    )
    parser.add_argument(
        "--fills",
        action="store_true",
        help="Print unique fill kind values with counts, sorted desc",
    )
    parser.add_argument(
        "--questions",
        action="store_true",
        help="Print unique question kind values with counts, sorted desc",
    )
    parser.add_argument(
        "--fields",
        action="store_true",
        help="Print all field names seen in entries with counts, sorted desc",
    )
    parser.add_argument(
        "--dedupes",
        action="store_true",
        help="Print counts of dedupe true/false, sorted desc",
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Print per-day entry counts (UTC YYYY-MM-DD), sorted desc (--md renders a Markdown table)",
    )
    parser.add_argument(
        "--hourly",
        action="store_true",
        help="Print per-hour-of-day entry counts (UTC 00-23), sorted desc (--md renders a Markdown table)",
    )
    parser.add_argument(
        "--daily-status",
        dest="daily_status",
        action="store_true",
        help="Print a per-day x per-status matrix: rows 'YYYY-MM-DD STATUS N' sorted day-desc then count-desc (--json emits {daily_status: {day: {status: n}}}; --md renders a Markdown table)",
    )
    parser.add_argument(
        "--evidence",
        action="store_true",
        help="Print a routing-evidence block (statuses/winners/open misses) for PRs; --json emits it as JSON",
    )
    parser.add_argument(
        "--gap",
        metavar="S",
        type=float,
        default=None,
        help="List quiet periods: consecutive entries more than S seconds apart (--json emits {gaps: [...]})",
    )
    parser.add_argument(
        "--streaks",
        action="store_true",
        help="Print per-harness current/longest runs of consecutive same jev_status (--json emits {streaks: [...]})",
    )
    parser.add_argument(
        "--harness-health",
        dest="harness_health",
        action="store_true",
        help="Print per-harness per-UTC-hour timeout/error rates (quota signal; --json emits {harness_health: [...]})",
    )
    parser.add_argument(
        "--silent-since",
        metavar="S",
        type=float,
        default=None,
        help="List harnesses whose newest filtered entry is older than S seconds ago (rc 1 when any; --json emits {silent: [...]})",
    )
    parser.add_argument(
        "--chains",
        action="store_true",
        help="List prompt_head values consulted at least --chain-min times (rc 1 when any; --json emits {chains: [...]}, --jsonl one row per line, --md a table)",
    )
    parser.add_argument(
        "--chain-min",
        metavar="N",
        type=int,
        default=2,
        help="Minimum repeats for a --chains row (default 2)",
    )
    parser.add_argument(
        "--count",
        action="store_true",
        help="Print only the number of entries matching the filters",
    )
    parser.add_argument(
        "--group-by",
        metavar="FIELD",
        default=os.environ.get("JEV_DECISIONS_GROUP_BY", ""),
        help="Count entries grouped by FIELD (a.b digs into nested objects)",
    )
    parser.add_argument(
        "--jq-where-contains",
        metavar="SUB",
        default="",
        help="With --jq: keep only extracted values containing SUB.",
    )
    parser.add_argument(
        "--jq",
        metavar="FIELD",
        default="",
        help="Print the FIELD value of each entry, one per line (a.b digs into nested objects; comma-separated fields print tab-separated columns)",
    )
    parser.add_argument(
        "--uniq",
        action="store_true",
        default=os.environ.get("JEV_DECISIONS_UNIQ", "").strip().lower() in ("1", "true", "yes"),
        help="With --jq: print each value only once (first occurrence wins)",
    )
    parser.add_argument(
        "--jq-first",
        action="store_true",
        help="With --jq: print only the first extracted value",
    )
    parser.add_argument(
        "--jq-last",
        action="store_true",
        help="With --jq: print only the last extracted value",
    )
    parser.add_argument(
        "--errors",
        action="store_true",
        help="Print the unparseable jsonl lines with line numbers (rc 1 when any)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Print parseable entries missing a numeric ts or a non-empty jev_status (index + reason)",
    )
    parser.add_argument(
        "--fill-gaps",
        action="store_true",
        help="Per-harness report of miss entries (no winner) never followed by a fill for the same prompt",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="With --fill-gaps: exit 1 when any harness has open misses",
    )
    parser.add_argument(
        "--max-open",
        type=int,
        default=None,
        metavar="N",
        help="With --fill-gaps: exit 1 when total open misses exceed N (--strict is --max-open 0)",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Rewrite the log keeping only entries matching the time/status filters",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --prune/--drop-bad: report what would be dropped without rewriting the log",
    )
    parser.add_argument(
        "--archive",
        metavar="PATH",
        default="",
        help="With --prune: append the dropped entries to PATH as JSONL before rewriting the log (lossless prune)",
    )
    parser.add_argument(
        "--merge",
        metavar="PATH",
        default="",
        help="Append entries from another decisions.jsonl that are not already in the log (identity = sha/ts/dump key), then continue into the normal report",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="With --verify: print every problem line instead of the first 20",
    )
    parser.add_argument(
        "--rotate",
        type=int,
        default=None,
        metavar="N",
        help="Rewrite the log keeping only the last N entries (positional prune; honors --archive and --dry-run)",
    )
    parser.add_argument(
        "--drop-bad",
        action="store_true",
        help="Rewrite the log dropping unparseable lines (keeps all well-formed entries)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable stats")
    parser.add_argument(
        "--env",
        action="store_true",
        help="Print the resolved env config JSON (file, source, exists, count, env) — --jq KEY prints one field, --out PATH also writes it",
    )
    parser.add_argument(
        "--schema", action="store_true",
        help="Print the decisions.jsonl entry key contract and exit (--json emits the object)",
    )
    parser.add_argument("--report", metavar="PATH", default="", help="Also write a markdown stats report (totals, status/harness/winners tables) to PATH")
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Print filtered entries as CSV; also emits CSV tables on the report views (--daily/--hourly/--winners/--statuses/--outcomes/--fills/--harnesses/--dedupes/--fields/--daily-status/--gap/--streaks/--silent-since/--chains/--fill-gaps)",
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Print filtered entries as a Markdown table",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Print filtered entries as raw JSON lines (for piping); with --verify emits one {line, issue} row per problem",
    )
    parser.add_argument(
        "--keys",
        metavar="a,b",
        default="",
        help="Keep only these entry keys in emitted rows (--jsonl/--out/--nth/--sample; --csv/--md tables use them as columns; rc 2 when empty)",
    )
    parser.add_argument(
        "--last",
        action="store_true",
        default=os.environ.get("JEV_DECISIONS_LAST", "").strip().lower() in ("1", "true", "yes"),
        help="Print only the newest matching entry as JSON",
    )
    parser.add_argument(
        "--oldest",
        action="store_true",
        default=os.environ.get("JEV_DECISIONS_OLDEST", "").strip().lower() in ("1", "true", "yes"),
        help="Print only the oldest matching entry as JSON",
    )
    parser.add_argument(
        "--nth",
        type=int,
        default=0,
        metavar="N",
        help="Print only the Nth matching entry (1-based, after --reverse) as JSON",
    )
    parser.add_argument(
        "--skip",
        type=int,
        default=0,
        metavar="N",
        help="Drop the first N matching entries (after filters, before --first/--tail/--jq)",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        metavar="N",
        help="Emit N randomly picked matching entries as JSON lines (honors --reverse; combines with --out/--jsonl/--csv/--md)",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Write the filtered entries as JSONL to PATH instead of printing",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-read the log every S seconds and print a {\"ts\",\"count\"} JSON tick",
    )
    parser.add_argument(
        "--follow",
        metavar="S",
        type=float,
        default=0.0,
        help="Like --watch S, but prints each newly appended entry as a JSONL line (--jq digs fields) instead of count ticks",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick that reports removals.")
    parser.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s ignored)")
    parser.add_argument("--verdict", metavar="PATH", default="", help="Write a slim verdict JSON to PATH — with --watch a {verdict, count, added, removed, ticks} payload refreshed every tick; with --verify a {verdict: ok|fail, entries, bad_lines, problems} payload; without either a one-shot {verdict: ok|empty, count, ticks: 1} probe of the filtered entries. '-' prints it to stdout.")
    parser.add_argument("--self-test", action="store_true", help="Parse a synthetic 3-entry log + 1 bad line; exit 1 when the counts do not match")
    parser.add_argument("--verify", action="store_true", help="Chain check the raw log: unparseable lines, missing ts/jev_status, missing required schema keys on full routing/fill entries, ts regressions; rc 1 on any problem (--jq KEY digs the report, rc 2 on unknown; --out PATH writes the report JSON)")
    parser.add_argument("--fix", action="store_true", help="With --verify: rewrite the log dropping unparseable / non-object lines, then re-verify (report gains `fixed`; rc 2 on --file -)")
    args = parser.parse_args(argv)
    args.grep = _watch.text_arg(args.grep)
    if getattr(args, "schema", False):
        if args.json:
            sys.stdout.write(json.dumps(ENTRY_SCHEMA_ROWS, indent=2) + "\n")
        else:
            for key in ENTRY_SCHEMA_ROWS:
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, ENTRY_SCHEMA_ROWS[key]["type"], "required" if ENTRY_SCHEMA_ROWS[key]["required"] else "optional")
                )
        return 0
    if getattr(args, "self_test", False):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "decisions.jsonl"
            with open(log, "w", encoding="utf-8") as fh:
                for i in range(3):
                    fh.write(
                        json.dumps(
                            {
                                "ts": 1700000000.0 + i,
                                "jev_status": "ok" if i else "none",
                                "harness": "hermes",
                                "prompt_head": "t%d" % i,
                            }
                        )
                        + "\n"
                    )
                fh.write("not json\n")
            entries_st, bad_st = load_entries(log)
        stats = summarize(entries_st, bad_st)
        ok = stats["total"] == 3 and stats["bad_lines"] == 1
        payload = {
            "self_test": "ok" if ok else "FAIL",
            "total": stats["total"],
            "bad_lines": stats["bad_lines"],
        }
        if args.json:
            sys.stdout.write(json.dumps(payload) + "\n")
        else:
            sys.stdout.write(
                "self-test: %s total=%d bad=%d\n"
                % (payload["self_test"], payload["total"], payload["bad_lines"])
            )
        return 0 if ok else 1
    file_arg = args.file or os.environ.get("JEV_DECISIONS", "").strip()
    path = Path(file_arg) if file_arg else inventory.decisions_log_path()
    if getattr(args, "env", False):
        envvars = {
            key: os.environ[key]
            for key in sorted(os.environ)
            if key.startswith("JEV_DECISIONS")
        }
        exists = bool(path and path.is_file())
        if args.file:
            source = "--file"
        elif os.environ.get("JEV_DECISIONS", "").strip():
            source = "env"
        elif path is None:
            source = "disabled"
        else:
            source = "default"
        try:
            env_watch_secs = float(
                os.environ.get("JEV_DECISIONS_WATCH_SECS", "") or 0
            )
        except (TypeError, ValueError):
            env_watch_secs = 0.0
        report = {
            "file": str(path) if path else None,
            "source": source,
            "exists": exists,
            "count": len(load_entries(path)[0]) if exists else 0,
            "env": envvars,
            "watch_max": _watch.cap("JEV_DECISIONS_WATCH_MAX", None),
            "watch_secs": env_watch_secs,
            "watch_quiet": _watch.quiet(
                "JEV_DECISIONS_WATCH_QUIET", args.quiet
            ),
        }
        if args.jq:
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
        if args.out:
            try:
                _atomic_write(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0
    if path is None:
        sys.stderr.write("decisions log disabled (JEV_CONSULT_LOG=0)\n")
        return 2
    if str(path) == "-" and (
        args.prune
        or getattr(args, "drop_bad", False)
        or args.watch
        or getattr(args, "follow", 0.0)
        or getattr(args, "rotate", None) is not None
    ):
        sys.stderr.write("--file - (stdin) supports neither --prune, --drop-bad, --rotate, --watch nor --follow\n")
        return 2
    if getattr(args, "rotate", None) is not None and (
        args.prune or getattr(args, "drop_bad", False)
    ):
        sys.stderr.write("--rotate conflicts with --prune/--drop-bad\n")
        return 2
    if args.rotate is not None and args.rotate < 1:
        sys.stderr.write("--rotate needs N >= 1\n")
        return 2
    if getattr(args, "archive", "") and not args.prune and args.rotate is None:
        sys.stderr.write("--archive requires --prune or --rotate\n")
        return 2
    if getattr(args, "verbose", False) and not getattr(args, "verify", False):
        sys.stderr.write("--verbose requires --verify\n")
        return 2
    if getattr(args, "merge", ""):
        if str(path) == "-":
            sys.stderr.write("--file - (stdin) does not support --merge\n")
            return 2
        other = Path(args.merge)
        if not other.is_file():
            sys.stderr.write("cannot read --merge file %s\n" % other)
            return 2
        try:
            merged = merge_log(path, other)
        except OSError as exc:
            sys.stderr.write("merge failed: %s\n" % exc)
            return 1
        sys.stderr.write(
            "merged %d of %d entries from %s (skipped %d dupes, %d bad line(s))\n"
            % (
                merged["added"],
                merged["added"] + merged["skipped"],
                other,
                merged["skipped"],
                merged["bad_lines"],
            )
        )
    if str(path) != "-" and not path.is_file():
        sys.stderr.write("no decisions log at %s\n" % path)
        return 1
    if getattr(args, "verify", False):
        report = verify_log(path)
        if getattr(args, "fix", False):
            if str(path) == "-":
                sys.stderr.write("--fix cannot rewrite a stdin log\n")
                return 2
            fixable = {"unparseable", "not an object"}
            to_drop = {
                p["line"]
                for p in report["problems"]
                if p.get("issue") in fixable
            }
            if to_drop:
                text = _log_text(path) or ""
                kept = [
                    line
                    for i, line in enumerate(text.splitlines(), 1)
                    if i not in to_drop
                ]
                try:
                    _atomic_write(
                        path, "\n".join(kept) + ("\n" if kept else "")
                    )
                except OSError as exc:
                    sys.stderr.write("cannot fix %s: %s\n" % (path, exc))
                    return 1
                report = verify_log(path)
            report["fixed"] = len(to_drop)
        if args.verdict:
            _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "ok" if report["ok"] else "fail",
                    "entries": report["entries"],
                    "bad_lines": report["bad_lines"],
                    "problems": len(report["problems"]),
                },
            )
        if args.jq:
            node, found = _watch.dig(report, args.jq)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (verify has: %s)\n"
                    % (args.jq, ", ".join(sorted(report)))
                )
                return 2
            sys.stdout.write(json.dumps(node, ensure_ascii=False) + "\n")
            return 0 if report["ok"] else 1
        if getattr(args, "out", ""):
            try:
                _atomic_write(
                    Path(args.out),
                    json.dumps(report, indent=2) + "\n",
                )
                sys.stderr.write("wrote %s\n" % args.out)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        if args.json:
            sys.stdout.write(json.dumps(report, indent=2) + "\n")
        elif getattr(args, "jsonl", False):
            for row in report["problems"]:
                sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(
                "verify: %s entries=%d bad_lines=%d problems=%d\n"
                % (
                    "ok" if report["ok"] else "FAIL",
                    report["entries"],
                    report["bad_lines"],
                    len(report["problems"]),
                )
            )
            cap = max(1, getattr(args, "top", 0) or 20)
            shown = (
                report["problems"]
                if getattr(args, "verbose", False)
                else report["problems"][:cap]
            )
            for row in shown:
                sys.stdout.write("  line %d: %s\n" % (row["line"], row["issue"]))
            extra = len(report["problems"]) - len(shown)
            if extra:
                sys.stdout.write("  ... and %d more (--verbose lists all)\n" % extra)
        return 0 if report["ok"] else 1
    all_entries, bad = load_entries(path)
    if getattr(args, "validate", False):
        bad_rows = []
        for index, item in enumerate(all_entries):
            problems = []
            if not isinstance(item.get("ts"), (int, float)) or isinstance(item.get("ts"), bool):
                problems.append("ts")
            if not str(item.get("jev_status") or "").strip():
                problems.append("jev_status")
            if problems:
                bad_rows.append((index, ",".join(problems)))
        if args.json:
            sys.stdout.write(
                json.dumps(
                    [
                        {"index": index, "missing": why.split(",")}
                        for index, why in bad_rows
                    ],
                    indent=2,
                )
                + "\n"
            )
        else:
            for index, why in bad_rows:
                sys.stdout.write("entry[%d] missing %s\n" % (index, why))
            sys.stdout.write(
                "%d invalid entr%s\n" % (len(bad_rows), "y" if len(bad_rows) == 1 else "ies")
            )
        return 1 if bad_rows else 0
    if getattr(args, "drop_bad", False):
        if getattr(args, "dry_run", False):
            sys.stderr.write("dry-run: would drop %d bad line(s)\n" % bad)
        elif bad:
            try:
                prune_entries(path, lambda items: items)
                sys.stderr.write("dropped %d bad line(s)\n" % bad)
            except OSError as exc:
                sys.stderr.write("drop-bad failed: %s\n" % exc)
                return 1
    try:
        since = _ts_arg(args.since)
        until = _ts_arg(args.until)
    except ValueError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    days = args.days if args.days > 0 else (7.0 if args.week else 0.0)
    if days > 0:
        since = time.time() - days * 86400
    missing_field = getattr(args, "missing", "") or ""
    def _filtered(items):
        if since is not None:
            items = filter_since(items, since)
        if until is not None:
            items = filter_until(items, until)
        if getattr(args, "since_last", ""):
            bound = None
            for item in items:
                ts = item.get("ts")
                if (
                    str(item.get("jev_status") or "") == args.since_last
                    and isinstance(ts, (int, float))
                    and not isinstance(ts, bool)
                ):
                    bound = ts if bound is None else max(bound, ts)
            if bound is None:
                items = []
            else:
                items = [
                    item
                    for item in items
                    if isinstance(item.get("ts"), (int, float))
                    and not isinstance(item.get("ts"), bool)
                    and float(item["ts"]) > bound
                ]
        if getattr(args, "until_last", ""):
            bound = None
            for item in items:
                ts = item.get("ts")
                if (
                    str(item.get("jev_status") or "") == args.until_last
                    and isinstance(ts, (int, float))
                    and not isinstance(ts, bool)
                ):
                    bound = ts if bound is None else max(bound, ts)
            if bound is not None:
                items = [
                    item
                    for item in items
                    if isinstance(item.get("ts"), (int, float))
                    and not isinstance(item.get("ts"), bool)
                    and float(item["ts"]) < bound
                ]
        if args.harness:
            items = filter_harness(items, args.harness)
        if args.status:
            items = filter_status(items, args.status)
        if args.outcome:
            items = filter_outcome(items, args.outcome)
        if args.fill:
            items = filter_fill(items, args.fill)
        if args.field:
            items = filter_field(items, args.field)
        if args.max_need is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("need"), (int, float))
                and not isinstance(item.get("need"), bool)
                and item["need"] <= args.max_need
            ]
        if args.min_need is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("need"), (int, float))
                and not isinstance(item.get("need"), bool)
                and float(item.get("need")) >= args.min_need
            ]
        if getattr(args, "explicit", False):
            items = [item for item in items if item.get("explicit") is True]
        if getattr(args, "dedupe_only", False):
            items = [item for item in items if item.get("dedupe") is True]
        if getattr(args, "stale", False):
            items = [item for item in items if item.get("stale_sidecar") is True]
        if getattr(args, "over_budget", False):
            items = [item for item in items if item.get("over_budget") is True]
        if getattr(args, "strong", False):
            items = [item for item in items if item.get("strong_pick") is True]
        if getattr(args, "min_prompt_len", None) is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("prompt_len"), (int, float))
                and not isinstance(item.get("prompt_len"), bool)
                and float(item.get("prompt_len")) >= args.min_prompt_len
            ]
        if getattr(args, "min_shortlist", None) is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("shortlist_n"), (int, float))
                and not isinstance(item.get("shortlist_n"), bool)
                and float(item.get("shortlist_n")) >= args.min_shortlist
            ]
        if getattr(args, "min_catalog", None) is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("n_catalog"), (int, float))
                and not isinstance(item.get("n_catalog"), bool)
                and float(item.get("n_catalog")) >= args.min_catalog
            ]
        if getattr(args, "min_score", None) is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("shortlist_score_avg"), (int, float))
                and not isinstance(item.get("shortlist_score_avg"), bool)
                and float(item.get("shortlist_score_avg")) >= args.min_score
            ]
        if args.sha:
            want_sha = args.sha.strip().lower()
            items = [
                item
                for item in items
                if str(item.get("prompt_sha") or "").lower().startswith(want_sha)
            ]
        if args.question:
            want_q = args.question.strip().lower()
            items = [
                item
                for item in items
                if str(item.get("question") or "").lower() == want_q
            ]
        if args.winner:
            wants = {part.strip().lower() for part in args.winner.split(",") if part.strip()}
            items = [
                item
                for item in items
                if isinstance(item.get("winner"), dict)
                and (
                    str(item["winner"].get("name") or "").lower() in wants
                    or "%s:%s"
                    % (
                        str(item["winner"].get("kind") or "").lower(),
                        str(item["winner"].get("name") or "").lower(),
                    )
                    in wants
                )
            ]
        if args.max_latency is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("latency_ms"), (int, float))
                and not isinstance(item.get("latency_ms"), bool)
                and float(item.get("latency_ms")) <= args.max_latency
            ]
        if args.min_latency is not None:
            items = [
                item
                for item in items
                if isinstance(item.get("latency_ms"), (int, float))
                and not isinstance(item.get("latency_ms"), bool)
                and float(item.get("latency_ms")) >= args.min_latency
            ]
        for pair in args.where or []:
            if "=" not in pair:
                continue
            wkey, wval = pair.split("=", 1)
            wkey = wkey.strip()
            wvals = {part.strip().lower() for part in wval.split(",") if part.strip()} or {""}
            items = [
                item
                for item in items
                if str(_dig(item, wkey) if _dig(item, wkey) is not None else "").lower() in wvals
            ]
        if missing_field:
            missing_keys = [part.strip() for part in missing_field.split(",") if part.strip()]
            items = [
                item
                for item in items
                if any(_dig(item, key) is None for key in missing_keys)
            ]
        for pair in getattr(args, "where_not", None) or []:
            if "=" not in pair:
                continue
            wkey, wval = pair.split("=", 1)
            wkey = wkey.strip()
            wvals = {part.strip().lower() for part in wval.split(",") if part.strip()} or {""}
            items = [
                item
                for item in items
                if str(_dig(item, wkey) if _dig(item, wkey) is not None else "").lower() not in wvals
            ]
        if args.prompt:
            needle = args.prompt.lower()
            items = [
                item
                for item in items
                if needle in str(item.get("prompt_head") or "").lower()
                or needle in str(item.get("prompt_tail") or "").lower()
            ]
        if args.reason:
            needle = args.reason.lower()
            items = [
                item
                for item in items
                if needle in str(item.get("reason") or "").lower()
            ]
        if args.grep:
            needle = args.grep.lower()

            def _haystack(item: dict) -> str:
                parts = []
                for v in item.values():
                    if isinstance(v, str):
                        parts.append(v)
                    elif isinstance(v, dict):
                        parts.extend(x for x in v.values() if isinstance(x, str))
                return " ".join(parts).lower()

            items = [item for item in items if needle in _haystack(item)]
        skip = getattr(args, "skip", 0) or 0
        if skip > 0:
            items = items[skip:]
        return items
    entries = _filtered(all_entries)
    if getattr(args, "evidence", False):
        data = evidence_report(entries)
        rendered = (
            json.dumps(data, indent=2) + "\n" if args.json else format_evidence(data) + "\n"
        )
        if args.out:
            out_path = Path(args.out)
            try:
                _atomic_write(out_path, rendered)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
                return 1
            sys.stderr.write("wrote evidence to %s\n" % out_path)
            return 0
        sys.stdout.write(rendered)
        return 0
    if getattr(args, "gap", None) is not None:
        gaps = quiet_gaps(entries, float(args.gap))
        if args.json:
            sys.stdout.write(json.dumps({"gaps": gaps}, indent=2) + "\n")
        elif getattr(args, "md", False):
            _watch.md_table(
                [
                    {
                        "from": _iso_full(g["from_ts"]) or "?",
                        "to": _iso_full(g["to_ts"]) or "?",
                        "seconds": g["seconds"],
                    }
                    for g in gaps
                ],
                ["from", "to", "seconds"],
            )
        elif getattr(args, "csv", False):
            _watch.csv_table(
                [
                    {
                        "from": _iso_full(g["from_ts"]) or "?",
                        "to": _iso_full(g["to_ts"]) or "?",
                        "seconds": g["seconds"],
                    }
                    for g in gaps
                ],
                ["from", "to", "seconds"],
            )
        else:
            sys.stdout.write(format_gaps(gaps) + "\n")
        return 0
    if getattr(args, "streaks", False):
        rows = status_streaks(entries)
        if args.json:
            sys.stdout.write(json.dumps({"streaks": rows}, indent=2) + "\n")
        elif getattr(args, "md", False):
            _watch.md_table(
                rows,
                [
                    "harness", "entries", "current_status",
                    "current_streak", "best_status", "best_streak",
                ],
            )
        elif getattr(args, "csv", False):
            _watch.csv_table(
                rows,
                [
                    "harness", "entries", "current_status",
                    "current_streak", "best_status", "best_streak",
                ],
            )
        else:
            sys.stdout.write(format_streaks(rows) + "\n")
        return 0
    if getattr(args, "harness_health", False):
        rows = harness_health(entries)
        if args.json:
            sys.stdout.write(json.dumps({"harness_health": rows}, indent=2) + "\n")
        elif getattr(args, "jsonl", False):
            for row in rows:
                sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
        elif getattr(args, "md", False):
            _watch.md_table(rows, HEALTH_COLS)
        elif getattr(args, "csv", False):
            _watch.csv_table(rows, HEALTH_COLS)
        else:
            sys.stdout.write(format_health(rows) + "\n")
        return 0
    if getattr(args, "silent_since", None) is not None:
        rows = silent_harnesses(entries, float(args.silent_since))
        if args.json:
            sys.stdout.write(json.dumps({"silent": rows}, indent=2) + "\n")
        elif getattr(args, "md", False):
            _watch.md_table(
                [
                    {
                        "harness": r["harness"],
                        "last": _iso_full(r["last_ts"]) or "?",
                        "age_s": r["age_s"],
                    }
                    for r in rows
                ],
                ["harness", "last", "age_s"],
            )
        elif getattr(args, "csv", False):
            _watch.csv_table(
                [
                    {
                        "harness": r["harness"],
                        "last": _iso_full(r["last_ts"]) or "?",
                        "age_s": r["age_s"],
                    }
                    for r in rows
                ],
                ["harness", "last", "age_s"],
            )
        else:
            sys.stdout.write(format_silent(rows, float(args.silent_since)) + "\n")
        return 1 if rows else 0
    if getattr(args, "chains", False):
        rows = prompt_chains(entries, int(getattr(args, "chain_min", 2)))
        if args.json:
            sys.stdout.write(json.dumps({"chains": rows}, indent=2) + "\n")
        elif getattr(args, "jsonl", False):
            for row in rows:
                sys.stdout.write(json.dumps(row, sort_keys=True) + "\n")
        elif getattr(args, "md", False):
            _watch.md_table(
                [
                    {
                        "prompt_head": r["prompt_head"],
                        "count": r["count"],
                        "first": _iso_full(r["first_ts"]) or "?",
                        "last": _iso_full(r["last_ts"]) or "?",
                        "statuses": ",".join(r["statuses"]),
                    }
                    for r in rows
                ],
                ["prompt_head", "count", "first", "last", "statuses"],
            )
        elif getattr(args, "csv", False):
            _watch.csv_table(
                [
                    {
                        "prompt_head": r["prompt_head"],
                        "count": r["count"],
                        "first": _iso_full(r["first_ts"]) or "?",
                        "last": _iso_full(r["last_ts"]) or "?",
                        "statuses": ",".join(r["statuses"]),
                    }
                    for r in rows
                ],
                ["prompt_head", "count", "first", "last", "statuses"],
            )
        else:
            sys.stdout.write(format_chains(rows) + "\n")
        return 1 if rows else 0
    if getattr(args, "fill_gaps", False):
        rows = fill_gaps(entries)
        if args.json:
            sys.stdout.write(json.dumps({"fill_gaps": rows}, indent=2) + "\n")
        elif getattr(args, "md", False):
            _watch.md_table(
                [
                    {
                        "harness": r["harness"],
                        "misses": r["misses"],
                        "filled": r["filled"],
                        "open": r["open"],
                        "fill_rate": (
                            "-" if r.get("fill_rate") is None
                            else "%.3f" % r["fill_rate"]
                        ),
                        "age_s": "-" if r.get("age_s") is None else r["age_s"],
                        "examples": "; ".join(r["examples"]),
                    }
                    for r in rows
                ],
                ["harness", "misses", "filled", "open",
                 "fill_rate", "age_s", "examples"],
            )
        elif getattr(args, "csv", False):
            _watch.csv_table(
                [
                    {
                        "harness": r["harness"],
                        "misses": r["misses"],
                        "filled": r["filled"],
                        "open": r["open"],
                        "fill_rate": (
                            "-" if r.get("fill_rate") is None
                            else "%.3f" % r["fill_rate"]
                        ),
                        "age_s": "-" if r.get("age_s") is None else r["age_s"],
                        "examples": "; ".join(r["examples"]),
                    }
                    for r in rows
                ],
                ["harness", "misses", "filled", "open",
                 "fill_rate", "age_s", "examples"],
            )
        else:
            sys.stdout.write(format_fill_gaps(rows) + "\n")
        open_total = sum(row["open"] for row in rows)
        max_open = getattr(args, "max_open", None)
        gate_fail = open_total > 0 if getattr(args, "strict", False) else False
        if max_open is not None and open_total > max_open:
            sys.stderr.write(
                "max-open: %d open miss%s exceeds %d\n"
                % (open_total, "es" if open_total != 1 else "", max_open)
            )
            return 1
        if gate_fail:
            sys.stderr.write(
                "strict: %d open miss%s across %d harness%s\n"
                % (
                    open_total,
                    "es" if open_total != 1 else "",
                    len(rows),
                    "es" if len(rows) != 1 else "",
                )
            )
            return 1
        return 0
    if getattr(args, "watch", 0) > 0 or getattr(args, "follow", 0) > 0:
        following = getattr(args, "watch", 0) <= 0
        watch_s = args.follow if following else args.watch
        max_ticks = _watch.cap("JEV_DECISIONS_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_DECISIONS_WATCH_SECS", getattr(args, "watch_max", 0.0))
        prev_keys: set | None = None
        total_added = 0
        total_removed = 0
        tick: dict = {}
        verdict_ok = True
        prev_tick: dict | None = None
        unchanged = 0
        watch_t0 = time.time()

        def _write_verdict() -> bool:
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "removed" if tick.get("removed") else "ok",
                    "count": tick.get("count", 0),
                    "newest_ts": tick.get("newest_ts"),
                    "added": total_added,
                    "removed": total_removed,
                    "ticks": ticks,
                    "elapsed_s": round(time.time() - watch_t0, 2),
                    "delta_pct": tick.get("delta_pct"),
                },
            )

        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
            cur_keys = {
                _entry_key(e) for e in entries if isinstance(e, dict)
            }
            newest_ts = max(
                (
                    e["ts"]
                    for e in entries
                    if isinstance(e, dict) and isinstance(e.get("ts"), (int, float))
                ),
                default=None,
            )
            tick = {
                "ts": int(time.time()),
                "count": len(entries),
                "newest_ts": newest_ts,
                "elapsed_s": round(time.time() - watch_t0, 2),
            }
            if prev_keys is not None:
                tick["added"] = len(cur_keys - prev_keys)
                tick["removed"] = len(prev_keys - cur_keys)
                prev_count = len(prev_keys)
                if prev_count:
                    tick["delta_pct"] = round(
                        100.0 * (len(entries) - prev_count) / prev_count, 1
                    )
                else:
                    tick["delta_pct"] = None
            total_added += int(tick.get("added", 0))
            total_removed += int(tick.get("removed", 0))
            if following:
                if prev_keys is not None:
                    new_keys = cur_keys - prev_keys
                    for item in entries:
                        if not isinstance(item, dict):
                            continue
                        if _entry_key(item) not in new_keys:
                            continue
                        if args.jq:
                            for field in [
                                f.strip() for f in args.jq.split(",") if f.strip()
                            ]:
                                sys.stdout.write(
                                    json.dumps(_dig(item, field)) + "\n"
                                )
                        else:
                            sys.stdout.write(
                                json.dumps(item, sort_keys=True) + "\n"
                            )
            elif args.jq:
                for field in [f.strip() for f in args.jq.split(",") if f.strip()]:
                    sys.stdout.write(json.dumps(_dig(tick, field)) + "\n")
            else:
                _watch.emit(tick, getattr(args, "out", "") or None, quiet=_watch.quiet("JEV_DECISIONS_WATCH_QUIET", args.quiet), bad=bool(tick.get("added") or tick.get("removed")))
            prev_keys = cur_keys
            ticks += 1
            sys.stderr.write(
                "watch tick=%d count=%d added=%d removed=%d\n"
                % (
                    ticks,
                    tick["count"],
                    tick.get("added", 0),
                    tick.get("removed", 0),
                )
            )
            if args.verdict and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if getattr(args, "fail_fast", False) and tick.get("removed"):
                break
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            time.sleep(watch_s)
            try:
                fresh, _bad = load_entries(path)
                entries = _filtered(fresh)
            except Exception:
                pass
        if args.verdict and verdict_ok and not _write_verdict():
            return 1
        return 1 if tick.get("removed") else 0

    if args.verdict:
        if not _watch.write_verdict(
            args.verdict,
            {
                "verdict": "ok" if entries else "empty",
                "ticks": 1,
                "count": len(entries),
            },
        ):
            return 1

    if args.prune:
        if since is None and until is None and not (
            args.harness
            or args.status
            or args.outcome
            or args.fill
            or args.field
            or args.prompt
            or args.reason
            or args.grep
            or args.min_need is not None
            or args.min_latency is not None
            or args.winner
            or getattr(args, "explicit", False)
            or getattr(args, "dedupe_only", False)
            or getattr(args, "stale", False)
            or args.question
            or args.sha
            or args.max_need is not None
            or args.max_latency is not None
            or getattr(args, "over_budget", False)
            or getattr(args, "strong", False)
            or getattr(args, "min_score", None) is not None
            or getattr(args, "min_catalog", None) is not None
            or getattr(args, "min_shortlist", None) is not None
            or getattr(args, "min_prompt_len", None) is not None
            or args.where
            or getattr(args, "where_not", None)
            or missing_field
        ):
            sys.stderr.write(
                "--prune requires --days, --since, --until, --harness, --status, --outcome, --fill, --field, --min-need, --min-latency, --winner, --explicit, --question, --dedupe-only, --stale, --sha, --max-need, --max-latency, --over-budget, --strong, --min-score, --min-catalog, --min-shortlist, --min-prompt-len, --where, --where-not, --missing, or --prompt (--reverse does not affect --prune)\n"
            )
            return 2
        total, total_bad = load_entries(path)
        if getattr(args, "dry_run", False):
            if args.json:
                args._prune_dry_run = {
                    "would_prune": len(total) - len(entries),
                    "total": len(total),
                    "kept": len(entries),
                    "bad_lines": total_bad,
                }
            else:
                sys.stderr.write(
                    "dry-run: would prune %d of %d entries (kept %d, dropped %d bad line(s))\n"
                    % (len(total) - len(entries), len(total), len(entries), total_bad)
                )
        else:
            try:
                result = prune_entries(
                    path, _filtered, archive=args.archive or None
                )
            except OSError as exc:
                sys.stderr.write("prune failed: %s\n" % exc)
                return 1
            if result is None:
                sys.stderr.write(
                    "prune failed: decisions log kept changing (appends in flight); retry shortly\n"
                )
                return 1
            sys.stderr.write(
                "pruned %d of %d entries (kept %d, dropped %d bad line(s)%s)\n"
                % (
                    result["dropped"],
                    result["total"],
                    result["kept"],
                    result["bad"],
                    ", archived %d to %s" % (result["archived"], args.archive)
                    if args.archive
                    else "",
                )
            )
    if args.rotate is not None:
        n = args.rotate
        if getattr(args, "dry_run", False):
            total_all, total_bad_lines = load_entries(path)
            kept_n = min(n, len(total_all))
            if args.json:
                args._rotate_dry_run = {
                    "would_rotate": len(total_all) - kept_n,
                    "total": len(total_all),
                    "kept": kept_n,
                    "bad_lines": total_bad_lines,
                }
            else:
                sys.stderr.write(
                    "dry-run: would rotate %d of %d entries (kept %d, dropped %d bad line(s))\n"
                    % (len(total_all) - kept_n, len(total_all), kept_n, total_bad_lines)
                )
        else:
            try:
                result = prune_entries(
                    path, lambda items: items[-n:], archive=args.archive or None
                )
            except OSError as exc:
                sys.stderr.write("rotate failed: %s\n" % exc)
                return 1
            if result is None:
                sys.stderr.write(
                    "rotate failed: decisions log kept changing (appends in flight); retry shortly\n"
                )
                return 1
            sys.stderr.write(
                "rotated %d of %d entries (kept %d, dropped %d bad line(s)%s)\n"
                % (
                    result["dropped"],
                    result["total"],
                    result["kept"],
                    result["bad"],
                    ", archived %d to %s" % (result["archived"], args.archive)
                    if args.archive
                    else "",
                )
            )
    if args.errors:
        bad_rows = load_bad_lines(path)
        if args.json:
            sys.stdout.write(
                json.dumps(
                    [{"line": lineno, "raw": raw[:200]} for lineno, raw in bad_rows],
                    indent=2,
                )
                + "\n"
            )
        else:
            for lineno, raw in bad_rows:
                sys.stdout.write("%d: %s\n" % (lineno, raw[:200]))
        return 1 if bad_rows else 0
    if args.count:
        sys.stdout.write("%d\n" % len(entries))
        return 0
    if args.jq:
        jq_fields = [part.strip() for part in args.jq.split(",") if part.strip()]
        ordered = entries[::-1] if getattr(args, "reverse", False) else entries
        if len(jq_fields) > 1:
            values = [tuple(_dig(item, field) for field in jq_fields) for item in ordered]
        else:
            values = [_dig(item, args.jq) for item in ordered]
        if getattr(args, "uniq", False):
            seen = set()
            uniq_values = []
            for value in values:
                key = value if isinstance(value, str) else json.dumps(list(value) if isinstance(value, tuple) else value, sort_keys=True)
                if key in seen:
                    continue
                seen.add(key)
                uniq_values.append(value)
            values = uniq_values
        if args.jq_where_contains:
            needle = args.jq_where_contains
            filtered = []
            for value in values:
                haystack = (
                    value
                    if isinstance(value, str)
                    else json.dumps(
                        list(value) if isinstance(value, tuple) else value,
                        sort_keys=True,
                    )
                )
                if needle in haystack:
                    filtered.append(value)
            values = filtered
        if getattr(args, "jq_first", False):
            values = values[:1]
        if getattr(args, "jq_last", False):
            values = values[-1:]
        if args.json:
            out_values = [list(v) if isinstance(v, tuple) else v for v in values]
            sys.stdout.write(json.dumps({"field": args.jq, "values": out_values}, indent=2) + "\n")
        else:
            for value in values:
                if isinstance(value, tuple):
                    cells = []
                    for cell in value:
                        if cell is None:
                            cells.append("null")
                        elif isinstance(cell, str):
                            cells.append(cell)
                        else:
                            cells.append(json.dumps(cell, sort_keys=True))
                    sys.stdout.write("\t".join(cells) + "\n")
                elif value is None:
                    sys.stdout.write("null\n")
                elif isinstance(value, str):
                    sys.stdout.write(value + "\n")
                else:
                    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")
        return 0
    if args.group_by:
        rows = sorted(group_by(entries, args.group_by).items(), key=lambda kv: (-kv[1], kv[0]))
        if args.top > 0:
            rows = rows[: args.top]
        if args.json:
            sys.stdout.write(json.dumps({"field": args.group_by, "counts": dict(rows)}, indent=2) + "\n")
        else:
            for value, n in rows:
                sys.stdout.write("%s %d\n" % (value, n))
        return 0
    if (
        args.statuses
        or args.harnesses
        or args.winners
        or args.outcomes
        or args.fills
        or args.questions
        or args.fields
        or args.dedupes
        or args.daily
        or args.daily_status
        or args.hourly
    ):
        counts: dict[str, int] = {}
        if args.daily_status:
            matrix: dict[str, dict[str, int]] = {}
            for item in entries:
                ts = item.get("ts")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    day = datetime.datetime.fromtimestamp(
                        float(ts), tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d")
                else:
                    day = "unknown"
                status = str(item.get("jev_status") or "unknown")
                bucket = matrix.setdefault(day, {})
                bucket[status] = bucket.get(status, 0) + 1
            rows = [
                (day, status, n)
                for day, bucket in matrix.items()
                for status, n in bucket.items()
            ]
            rows.sort(key=lambda r: (-r[2], r[1]))
            rows.sort(key=lambda r: r[0], reverse=True)
            rows.sort(key=lambda r: r[0] == "unknown")
            if args.top > 0:
                rows = rows[: args.top]
            if args.json:
                sys.stdout.write(json.dumps({"daily_status": matrix}, indent=2) + "\n")
            elif args.md:
                sys.stdout.write("| day | status | n |\n| --- | --- | --- |\n")
                for day, status, n in rows:
                    sys.stdout.write("| %s | %s | %d |\n" % (day, status, n))
            elif getattr(args, "csv", False):
                _watch.csv_table(
                    [{"day": day, "status": status, "n": n} for day, status, n in rows],
                    ["day", "status", "n"],
                )
            else:
                for day, status, n in rows:
                    sys.stdout.write("%s %s %d\n" % (day, status, n))
            return 0
        if args.daily:
            for item in entries:
                ts = item.get("ts")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    day = datetime.datetime.fromtimestamp(
                        float(ts), tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%d")
                else:
                    day = "unknown"
                counts[day] = counts.get(day, 0) + 1
        elif args.hourly:
            for item in entries:
                ts = item.get("ts")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                    hour = "%02d" % datetime.datetime.fromtimestamp(
                        float(ts), tz=datetime.timezone.utc
                    ).hour
                else:
                    hour = "unknown"
                counts[hour] = counts.get(hour, 0) + 1
        elif args.winners:
            for item in entries:
                winner = item.get("winner")
                if isinstance(winner, dict) and winner.get("name"):
                    key = "%s:%s" % (winner.get("kind") or "?", winner["name"])
                    counts[key] = counts.get(key, 0) + 1
        else:
            if args.fields:
                field = None
            elif args.dedupes:
                field = "dedupe"
            elif args.outcomes:
                field = "outcome"
            elif args.fills:
                field = "fill"
            elif args.questions:
                field = "question"
            elif args.harnesses:
                field = "harness"
            else:
                field = "jev_status"
            for item in entries:
                if field is None:
                    for k in item:
                        counts[k] = counts.get(k, 0) + 1
                else:
                    value = str(item.get(field) or "unknown")
                    counts[value] = counts.get(value, 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if args.top > 0:
            rows = rows[: args.top]
        if args.json:
            sys.stdout.write(json.dumps({"counts": dict(rows)}, indent=2) + "\n")
        elif args.md:
            key_col = "day" if args.daily else "hour" if args.hourly else "value"
            sys.stdout.write("| %s | n |\n| --- | --- |\n" % key_col)
            for value, n in rows:
                sys.stdout.write("| %s | %d |\n" % (value, n))
        elif getattr(args, "csv", False):
            key_col = "day" if args.daily else "hour" if args.hourly else "value"
            _watch.csv_table(
                [{key_col: value, "n": n} for value, n in rows],
                [key_col, "n"],
            )
        else:
            for value, n in rows:
                sys.stdout.write("%s %d\n" % (value, n))
        return 0
    if getattr(args, "never_picked", False):
        picked = set()
        for item in entries:
            winner = item.get("winner")
            if isinstance(winner, dict) and winner.get("name"):
                picked.add(
                    "%s:%s" % (winner.get("kind") or "?", winner["name"])
                )
        wanted = [h.strip() for h in (args.harness or "").split(",") if h.strip()]
        harness_list = wanted or list(inventory.HARNESSES)
        home = Path(args.home) if args.home else None
        hermes = Path(args.hermes_home) if args.hermes_home else None
        rows = []
        for h in harness_list:
            try:
                scanned = inventory.scan(h, home=home, hermes=hermes)
            except ValueError:
                sys.stderr.write("--never-picked: unknown harness %r\n" % h)
                return 2
            for item in scanned:
                key = "%s:%s" % (item.get("kind") or "?", item.get("name"))
                if key not in picked:
                    rows.append(
                        {
                            "harness": h,
                            "kind": item.get("kind") or "?",
                            "name": item.get("name") or "",
                        }
                    )
        rows.sort(key=lambda r: (r["harness"], r["kind"], r["name"]))
        if args.top > 0:
            rows = rows[: args.top]
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {"harnesses": harness_list, "never_picked": rows, "count": len(rows)},
                    indent=2,
                )
                + "\n"
            )
        elif getattr(args, "jsonl", False):
            for row in rows:
                sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        elif getattr(args, "csv", False):
            _watch.csv_table(rows, ["harness", "kind", "name"])
        elif args.md:
            _watch.md_table(rows, ["harness", "kind", "name"])
        else:
            for row in rows:
                sys.stdout.write(
                    "%s %s:%s\n" % (row["harness"], row["kind"], row["name"])
                )
        return 0
    emit_entries = entries[::-1] if getattr(args, "reverse", False) else entries
    sample_n = getattr(args, "sample", 0) or 0
    if sample_n > 0:
        import random as _random

        emit_entries = _random.sample(
            emit_entries, min(sample_n, len(emit_entries))
        )
    key_sel = getattr(args, "keys", "") or ""
    proj = [k.strip() for k in key_sel.split(",") if k.strip()]
    if key_sel and not proj:
        sys.stderr.write("--keys names no fields\n")
        return 2
    if proj:
        emit_entries = [
            {k: item.get(k) for k in proj} if isinstance(item, dict) else item
            for item in emit_entries
        ]
    if getattr(args, "last", False):
        if emit_entries:
            item = entries[-1]
            if proj and isinstance(item, dict):
                item = {k: item.get(k) for k in proj}
            sys.stdout.write(json.dumps(item, indent=2, sort_keys=True) + "\n")
        return 0
    if getattr(args, "oldest", False):
        if emit_entries:
            item = entries[0]
            if proj and isinstance(item, dict):
                item = {k: item.get(k) for k in proj}
            sys.stdout.write(json.dumps(item, indent=2, sort_keys=True) + "\n")
        return 0
    if getattr(args, "nth", 0):
        if args.nth < 1 or args.nth > len(emit_entries):
            sys.stderr.write(
                "--nth %d out of range (%d entries)\n" % (args.nth, len(emit_entries))
            )
            return 2
        sys.stdout.write(json.dumps(emit_entries[args.nth - 1], indent=2, sort_keys=True) + "\n")
        return 0
    if sample_n > 0 and not (args.out or args.jsonl or args.csv or args.md):
        for item in emit_entries:
            sys.stdout.write(json.dumps(item, sort_keys=True) + "\n")
        return 0

    def _cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    def _rows():
        if proj:
            rows = []
            for item in emit_entries:
                row = []
                for k in proj:
                    value = item.get(k) if isinstance(item, dict) else ""
                    row.append(
                        json.dumps(value, sort_keys=True)
                        if isinstance(value, (dict, list))
                        else str(value if value is not None else "")
                    )
                rows.append(row)
            return proj, rows
        rows = []
        for item in emit_entries:
            winner = item.get("winner")
            winner_name = winner.get("name") if isinstance(winner, dict) else ""
            rows.append(
                [
                    str(item.get("ts") if item.get("ts") is not None else ""),
                    str(item.get("harness") or ""),
                    str(item.get("jev_status") or "unknown"),
                    str(winner_name or ""),
                    str(bool(item.get("dedupe"))),
                    str(item.get("fill") or ""),
                    str(item.get("outcome") or ""),
                    str(item.get("prompt_head") or "").strip()[:120],
                ]
            )
        header = ["ts", "harness", "jev_status", "winner", "dedupe", "fill", "outcome", "prompt_head"]
        return header, rows

    def _md_text(header, rows) -> str:
        out = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
        out.extend("| " + " | ".join(_cell(cell) for cell in row) + " |" for row in rows)
        return "\n".join(out) + "\n"

    if args.out:
        out_path = Path(args.out)
        try:
            if args.csv:
                header, rows = _rows()
                buf = io.StringIO()
                writer = csv.writer(buf, lineterminator="\n")
                writer.writerow(header)
                writer.writerows(rows)
                _atomic_write(out_path, buf.getvalue())
            elif args.md:
                header, rows = _rows()
                _atomic_write(out_path, _md_text(header, rows))
            else:
                _atomic_write(
                    out_path,
                    "".join(
                        json.dumps(item, sort_keys=True) + "\n"
                        for item in emit_entries
                    ),
                )
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d entries to %s\n" % (len(entries), out_path))
        return 0
    if args.jsonl:
        for item in emit_entries:
            sys.stdout.write(json.dumps(item, sort_keys=True) + "\n")
        return 0
    if args.csv or args.md:
        header, rows = _rows()
        if args.csv:
            writer = csv.writer(sys.stdout, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        else:
            sys.stdout.write(_md_text(header, rows))
        return 0
    stats = summarize(entries, bad)
    stats["filtered"] = len(entries)
    stats["since"] = since
    stats["until"] = until
    if getattr(args, "_prune_dry_run", None):
        stats["prune_dry_run"] = args._prune_dry_run
    if getattr(args, "_rotate_dry_run", None):
        stats["rotate_dry_run"] = args._rotate_dry_run
    if getattr(args, "report", ""):
        rep = [
            "# decisions report",
            "",
            "- entries: %(total)d (filtered: %(filtered)d, bad lines: %(bad_lines)d)" % stats,
            "- window: %s -> %s" % (stats.get("first_iso") or "-", stats.get("last_iso") or "-"),
            "- explicit: %d  strong_pick: %d" % (stats["explicit"], stats["strong_pick"]),
            "",
            "## by status",
            "",
            "| status | count |",
            "| --- | --- |",
        ]
        rep += ["| %s | %d |" % (k, v) for k, v in sorted(stats["by_status"].items())]
        rep += ["", "## by harness", "", "| harness | count |", "| --- | --- |"]
        rep += ["| %s | %d |" % (k, v) for k, v in sorted(stats["by_harness"].items())]
        if stats["top_winners"]:
            rep += ["", "## top winners", "", "| winner | count |", "| --- | --- |"]
            rep += ["| %s | %d |" % (k, v) for k, v in stats["top_winners"].items()]
        need = stats["need_skill"]
        lat = stats["latency_ms"]
        rep += [
            "",
            "## latency / need",
            "",
            "- need_skill: n=%d mean=%s p50=%s p90=%s"
            % (need["n"], need["mean"], need["p50"], need["p90"]),
            "- latency_ms: n=%d mean=%s p50=%s p90=%s max=%s"
            % (lat["n"], lat["mean"], lat["p50"], lat["p90"], lat["max"]),
        ]
        try:
            _atomic_write(Path(args.report), "\n".join(rep) + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.report, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.report)
    if args.json:
        sys.stdout.write(json.dumps(stats, indent=2) + "\n")
    else:
        sys.stdout.write(format_stats(stats) + "\n")
    shown = entries[: args.first] if args.first > 0 else entries[-args.tail :]
    if getattr(args, "reverse", False):
        shown = shown[::-1]
    if args.first > 0 or args.tail > 0:
        for item in shown:
            sys.stdout.write(format_entry(item) + "\n")
    return 0


if __name__ == "__main__":
    _watch.exit_safely(main())
