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
    prompts: dict[str, int] = {}
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
    return {
        "total": len(entries),
        "bad_lines": bad,
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
            "p50": _percentile(latencies, 0.5),
            "p90": _percentile(latencies, 0.9),
            "max": max(latencies) if latencies else None,
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
    need = stats["need_skill"]
    if need["n"]:
        lines.append(
            "need_skill: n=%d mean=%s p50=%s p90=%s"
            % (need["n"], need["mean"], need["p50"], need["p90"])
        )
    latency = stats["latency_ms"]
    if latency["n"]:
        lines.append(
            "latency_ms: n=%d p50=%s p90=%s max=%s"
            % (latency["n"], latency["p50"], latency["p90"], latency["max"])
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
    return [item for item in entries if str(item.get(key.strip()) or "") == value.strip()]


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
    parser.add_argument("--tail", type=int, default=0, help="Print last N entries")
    parser.add_argument("--days", type=float, default=0.0, help="Only entries from the last N days")
    parser.add_argument("--since", type=float, default=0.0, help="Only entries with ts >= epoch seconds")
    parser.add_argument("--harness", default="", help="Only entries for this harness")
    parser.add_argument("--status", default="", help="Only entries with this jev_status")
    parser.add_argument("--outcome", default="", help="Only entries with this outcome (e.g. human, blocked)")
    parser.add_argument("--fill", default="", help="Only entries with this fill kind (apply, catalog, peer)")
    parser.add_argument("--field", default="", help="Generic filter: KEY=VALUE equality on any entry field")
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
        "--prune",
        action="store_true",
        help="Rewrite the log keeping only entries matching --days/--since/--harness/--status filters",
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
    args = parser.parse_args(argv)
    path = Path(args.file) if args.file else inventory.decisions_log_path()
    if path is None:
        sys.stderr.write("decisions log disabled (JEV_CONSULT_LOG=0)\n")
        return 2
    if not path.is_file():
        sys.stderr.write("no decisions log at %s\n" % path)
        return 1
    entries, bad = load_entries(path)
    since = args.since or None
    if args.days > 0:
        since = time.time() - args.days * 86400
    if since is not None:
        entries = filter_since(entries, since)
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
    if args.prune:
        if since is None and not (
            args.harness or args.status or args.outcome or args.fill or args.field
        ):
            sys.stderr.write(
                "--prune requires --days, --since, --harness, --status, --outcome, --fill, or --field\n"
            )
            return 2
        total, _ = load_entries(path)
        try:
            prune_entries(path, entries)
        except OSError as exc:
            sys.stderr.write("prune failed: %s\n" % exc)
            return 1
        sys.stderr.write(
            "pruned %d of %d entries (kept %d)\n"
            % (len(total) - len(entries), len(total), len(entries))
        )
    if (
        args.statuses
        or args.harnesses
        or args.winners
        or args.outcomes
        or args.fills
        or args.fields
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
        for value, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            sys.stdout.write("%s %d\n" % (value, n))
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
    if args.json:
        sys.stdout.write(json.dumps(stats, indent=2) + "\n")
    else:
        sys.stdout.write(format_stats(stats) + "\n")
    if args.tail > 0:
        for item in entries[-args.tail:]:
            sys.stdout.write(format_entry(item) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
