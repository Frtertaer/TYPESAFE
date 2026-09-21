#!/usr/bin/env python3
"""Read decisions.jsonl and report routing stats for threshold calibration.

The hook appends one JSON line per routed prompt. This reads them back:
status mix, explicit/strong-pick rates, need_skill mean, latency percentiles.
"""
import argparse
import csv
import datetime
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

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
    return [
        item
        for item in entries
        if str(item.get("jev_status") or "unknown") == status
    ]


def filter_harness(entries: list[dict], harness: str | None) -> list[dict]:
    if not harness:
        return entries
    return [item for item in entries if str(item.get("harness") or "") == harness]


def filter_outcome(entries: list[dict], outcome: str | None) -> list[dict]:
    if not outcome:
        return entries
    return [item for item in entries if str(item.get("outcome") or "") == outcome]


def filter_fill(entries: list[dict], fill: str | None) -> list[dict]:
    if not fill:
        return entries
    return [item for item in entries if str(item.get("fill") or "") == fill]


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


def main(argv: list[str] | None = None) -> int:
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
    parser.add_argument("--harness", default=os.environ.get("JEV_DECISIONS_HARNESS", ""), help="Only entries for this harness")
    parser.add_argument("--status", default=os.environ.get("JEV_DECISIONS_STATUS", ""), help="Only entries with this jev_status")
    parser.add_argument("--outcome", default=os.environ.get("JEV_DECISIONS_OUTCOME", ""), help="Only entries with this outcome (e.g. human, blocked)")
    parser.add_argument("--fill", default=os.environ.get("JEV_DECISIONS_FILL", ""), help="Only entries with this fill kind (apply, catalog, peer)")
    parser.add_argument("--field", default=os.environ.get("JEV_DECISIONS_FIELD", ""), help="Generic filter: KEY=VALUE equality on any entry field (a.b digs into nested objects)")
    parser.add_argument("--prompt", default=os.environ.get("JEV_DECISIONS_PROMPT", ""), help="Only entries whose prompt_head/prompt_tail contain this substring (case-insensitive)")
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
        "--jq",
        metavar="FIELD",
        default="",
        help="Print the FIELD value of each entry, one per line (a.b digs into nested objects)",
    )
    parser.add_argument(
        "--errors",
        action="store_true",
        help="Print the unparseable jsonl lines with line numbers",
    )
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Rewrite the log keeping only entries matching the time/status filters",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="With --prune: report what would be dropped without rewriting the log",
    )
    parser.add_argument("--json", action="store_true", help="Machine-readable stats")
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
        "--out",
        metavar="PATH",
        default="",
        help="Write the filtered entries as JSONL to PATH instead of printing",
    )
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
    try:
        since = _ts_arg(args.since)
        until = _ts_arg(args.until)
    except ValueError as exc:
        sys.stderr.write("%s\n" % exc)
        return 2
    days = args.days if args.days > 0 else (7.0 if args.week else 0.0)
    if days > 0:
        since = time.time() - days * 86400
    if since is not None:
        entries = filter_since(entries, since)
    if until is not None:
        entries = filter_until(entries, until)
    if args.harness:
        entries = filter_harness(entries, args.harness)
    if args.status:
        entries = filter_status(entries, args.status)
    if args.outcome:
        entries = filter_outcome(entries, args.outcome)
    if args.fill:
        entries = filter_fill(entries, args.fill)
    if args.field:
        entries = filter_field(entries, args.field)
    if args.prompt:
        needle = args.prompt.lower()
        entries = [
            item
            for item in entries
            if needle in str(item.get("prompt_head") or "").lower()
            or needle in str(item.get("prompt_tail") or "").lower()
        ]
    if args.prune:
        if since is None and until is None and not (
            args.harness or args.status or args.outcome or args.fill or args.field or args.prompt
        ):
            sys.stderr.write(
                "--prune requires --days, --since, --until, --harness, --status, --outcome, --fill, --field, or --prompt\n"
            )
            return 2
        total, total_bad = load_entries(path)
        if getattr(args, "dry_run", False):
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
        for lineno, raw in load_bad_lines(path):
            sys.stdout.write("%d: %s\n" % (lineno, raw[:200]))
        return 0
    if args.count:
        sys.stdout.write("%d\n" % len(entries))
        return 0
    if args.jq:
        values = [_dig(item, args.jq) for item in entries]
        if args.json:
            sys.stdout.write(json.dumps({"field": args.jq, "values": values}, indent=2) + "\n")
        else:
            for value in values:
                if value is None:
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
    ):
        counts: dict[str, int] = {}
        if args.winners:
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
    if args.out:
        out_path = Path(args.out)
        try:
            with out_path.open("w", encoding="utf-8") as fh:
                for item in entries:
                    fh.write(json.dumps(item, sort_keys=True) + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
            return 1
        sys.stderr.write("wrote %d entries to %s\n" % (len(entries), out_path))
        return 0
    if args.jsonl:
        for item in entries:
            sys.stdout.write(json.dumps(item, sort_keys=True) + "\n")
        return 0
    if args.csv or args.md:
        rows = []
        for item in entries:
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
        if args.csv:
            writer = csv.writer(sys.stdout, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        else:
            def _cell(value: str) -> str:
                return value.replace("|", "\\|").replace("\n", " ")

            sys.stdout.write("| " + " | ".join(header) + " |\n")
            sys.stdout.write("|" + " --- |" * len(header) + "\n")
            for row in rows:
                sys.stdout.write("| " + " | ".join(_cell(c) for c in row) + " |\n")
        return 0
    stats = summarize(entries, bad)
    stats["filtered"] = len(entries)
    stats["since"] = since
    stats["until"] = until
    if args.json:
        sys.stdout.write(json.dumps(stats, indent=2) + "\n")
    else:
        sys.stdout.write(format_stats(stats) + "\n")
    shown = entries[: args.first] if args.first > 0 else entries[-args.tail :]
    if args.first > 0 or args.tail > 0:
        for item in shown:
            sys.stdout.write(format_entry(item) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
