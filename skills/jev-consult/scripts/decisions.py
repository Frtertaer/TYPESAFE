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


def load_entries(path: Path) -> tuple[list[dict], int]:
    entries: list[dict] = []
    bad = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries, bad
    for line in lines:
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


def load_bad_lines(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, raw)] for lines that failed to parse as JSON objects."""
    bad_rows: list[tuple[int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return bad_rows
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
        "first_iso": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(stamps)))
            if stamps
            else None
        ),
        "last_iso": (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(max(stamps)))
            if stamps
            else None
        ),
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


def time_str(ts: object) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
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
        node = item
        for part in key.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        return node

    return [item for item in entries if str(dig(item) or "") == value]


def _dig(item: dict, key: str):
    node = item
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


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


def prune_entries(path: Path, entries: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent), suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            for item in entries:
                out.write(json.dumps(item, sort_keys=True) + "\n")
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def main(argv: list[str] | None = None) -> int:
    if _watch.maybe_version(sys.argv[1:] if argv is None else argv):
        return 0
    parser = argparse.ArgumentParser(
        description="Stats over ~/.cache/jev-consult/decisions.jsonl."
    )
    parser.add_argument("--file", help="Override decisions.jsonl path")
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
        "--top", type=int, default=max(0, env_top), help="Cap count-list output (--statuses et al.) to N rows"
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
    parser.add_argument("--harness", default=os.environ.get("JEV_DECISIONS_HARNESS", ""), help="Only entries for this harness")
    parser.add_argument("--status", default=os.environ.get("JEV_DECISIONS_STATUS", ""), help="Only entries with this jev_status")
    parser.add_argument("--outcome", default=os.environ.get("JEV_DECISIONS_OUTCOME", ""), help="Only entries with this outcome (e.g. human, blocked)")
    parser.add_argument("--fill", default=os.environ.get("JEV_DECISIONS_FILL", ""), help="Only entries with this fill kind (apply, catalog, peer)")
    parser.add_argument("--field", default=os.environ.get("JEV_DECISIONS_FIELD", ""), help="Generic filter: KEY=VALUE equality on any entry field (a.b digs into nested objects)")
    parser.add_argument("--prompt", default=os.environ.get("JEV_DECISIONS_PROMPT", ""), help="Only entries whose prompt_head/prompt_tail contain this substring (case-insensitive)")
    parser.add_argument("--reason", default=os.environ.get("JEV_DECISIONS_REASON", ""), help="Only entries whose reason field contains this substring (case-insensitive)")
    parser.add_argument("--grep", default=os.environ.get("JEV_DECISIONS_GREP", ""), help="Only entries where any string field contains this substring (case-insensitive, one nesting level deep)")
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
        help="Print per-day entry counts (UTC YYYY-MM-DD), sorted desc",
    )
    parser.add_argument(
        "--evidence",
        action="store_true",
        help="Print a routing-evidence block (statuses/winners/open misses) for PRs; --json emits it as JSON",
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
        "--drop-bad",
        action="store_true",
        help="Rewrite the log dropping unparseable lines (keeps all well-formed entries)",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable stats")
    parser.add_argument("--report", metavar="PATH", default="", help="Also write a markdown stats report (totals, status/harness/winners tables) to PATH")
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Print filtered entries as CSV",
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Print filtered entries as a Markdown table",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Print filtered entries as raw JSON lines (for piping)",
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
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick that reports removals.")
    parser.add_argument("--verdict", metavar="PATH", default="", help="Write a slim verdict JSON to PATH — with --watch a {verdict, count, added, removed, ticks} payload refreshed every tick; without it a one-shot {verdict: ok|empty, count, ticks: 1} probe of the filtered entries.")
    args = parser.parse_args(argv)
    file_arg = args.file or os.environ.get("JEV_DECISIONS", "").strip()
    path = Path(file_arg) if file_arg else inventory.decisions_log_path()
    if path is None:
        sys.stderr.write("decisions log disabled (JEV_CONSULT_LOG=0)\n")
        return 2
    if not path.is_file():
        sys.stderr.write("no decisions log at %s\n" % path)
        return 1
    entries, bad = load_entries(path)
    if getattr(args, "validate", False):
        bad_rows = []
        for index, item in enumerate(entries):
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
                prune_entries(path, entries)
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
    entries = _filtered(entries)
    if getattr(args, "evidence", False):
        data = evidence_report(entries)
        if args.json:
            sys.stdout.write(json.dumps(data, indent=2) + "\n")
        else:
            sys.stdout.write(format_evidence(data) + "\n")
        return 0
    if getattr(args, "fill_gaps", False):
        rows = fill_gaps(entries)
        if args.json:
            sys.stdout.write(json.dumps({"fill_gaps": rows}, indent=2) + "\n")
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
    if getattr(args, "watch", 0) > 0:
        max_ticks = _watch.cap("JEV_DECISIONS_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_DECISIONS_WATCH_SECS", getattr(args, "watch_max", 0.0))
        prev_keys: set | None = None
        total_added = 0
        total_removed = 0
        tick: dict = {}
        verdict_ok = True
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
                str(e.get("sha") or e.get("ts") or json.dumps(e, sort_keys=True, default=str))
                for e in entries
                if isinstance(e, dict)
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
            if args.jq:
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
            time.sleep(args.watch)
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
                prune_entries(path, entries)
            except OSError as exc:
                sys.stderr.write("prune failed: %s\n" % exc)
                return 1
            sys.stderr.write(
                "pruned %d of %d entries (kept %d, dropped %d bad line(s))\n"
                % (len(total) - len(entries), len(total), len(entries), total_bad)
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
        or args.fields
        or args.dedupes
        or args.daily
    ):
        counts: dict[str, int] = {}
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
        else:
            for value, n in rows:
                sys.stdout.write("%s %d\n" % (value, n))
        return 0
    emit_entries = entries[::-1] if getattr(args, "reverse", False) else entries
    sample_n = getattr(args, "sample", 0) or 0
    if sample_n > 0:
        import random as _random

        emit_entries = _random.sample(
            emit_entries, min(sample_n, len(emit_entries))
        )
    if getattr(args, "last", False):
        if emit_entries:
            sys.stdout.write(json.dumps(entries[-1], indent=2, sort_keys=True) + "\n")
        return 0
    if getattr(args, "oldest", False):
        if emit_entries:
            sys.stdout.write(json.dumps(entries[0], indent=2, sort_keys=True) + "\n")
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
    sys.exit(main())
