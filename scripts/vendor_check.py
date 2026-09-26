#!/usr/bin/env python3
"""vendor_check.py — drift report for inspect-only vendored snapshots.

Reads vendor/PORTS.json: each snapshot pins {dir, upstream, commit} and the
ported file pairs (vendored source -> our runtime script).

Default (offline): verifies every snapshot has an UPSTREAM_COMMIT file that
matches the manifest pin and that every ported pair's files exist, then prints
per-pair diff stats so an upstream refresh can be reviewed against the port.
--fetch additionally queries `git ls-remote` for each upstream HEAD and exits
nonzero when an upstream moved past its pinned commit.

Usage: python scripts/vendor_check.py [--fetch] [--diff] [--json]
Exit 0 clean, 1 on drift/failure, 2 on bad args or manifest errors.
"""
from __future__ import annotations

import difflib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "vendor" / "PORTS.json"


def _sha_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.is_file() else ""


def _diff_stats(a: Path, b: Path) -> tuple[int, int]:
    diff = list(
        difflib.unified_diff(
            a.read_text(encoding="utf-8", errors="replace").splitlines(),
            b.read_text(encoding="utf-8", errors="replace").splitlines(),
            lineterm="",
        )
    )
    adds = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    dels = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return adds, dels


def _upstream_head(repo: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    return out.stdout.split()[0]


def main(argv) -> int:
    fetch = "--fetch" in argv
    show_diff = "--diff" in argv
    as_json = "--json" in argv
    unknown = [a for a in argv[1:] if a not in ("--fetch", "--diff", "--json")]
    if unknown:
        print(f"vendor-check: unknown arguments: {' '.join(unknown)}", file=sys.stderr)
        return 2
    if not MANIFEST.is_file():
        print(f"vendor-check: missing {MANIFEST}", file=sys.stderr)
        return 2
    try:
        snaps = json.loads(MANIFEST.read_text(encoding="utf-8"))["snapshots"]
    except (ValueError, KeyError) as exc:
        print(f"vendor-check: invalid {MANIFEST}: {exc}", file=sys.stderr)
        return 2

    errors = []
    rows = []
    for snap in snaps:
        d = ROOT / "vendor" / snap["dir"]
        row = {"dir": snap["dir"], "upstream": snap["upstream"], "commit": snap["commit"][:12]}
        if not d.is_dir():
            errors.append(f"{snap['dir']}: snapshot directory missing")
            row["status"] = "missing"
            rows.append(row)
            continue
        pinned = _sha_text(d / "UPSTREAM_COMMIT")
        if pinned != snap["commit"]:
            errors.append(
                f"{snap['dir']}: UPSTREAM_COMMIT is {pinned[:12] or 'absent'}"
                f", manifest pins {snap['commit'][:12]}"
            )
            row["status"] = "pin-mismatch"
        pairs = []
        for pair in snap.get("ported", []):
            vend = d / pair["vendor"]
            ours = ROOT / pair["ours"]
            if not vend.is_file():
                errors.append(f"{snap['dir']}: vendored file {pair['vendor']} missing")
                continue
            if not ours.is_file():
                errors.append(f"{snap['dir']}: ported target {pair['ours']} missing")
                continue
            adds, dels = _diff_stats(vend, ours)
            pairs.append({"vendor": pair["vendor"], "ours": pair["ours"], "+": adds, "-": dels})
        row["pairs"] = pairs
        if fetch:
            head = _upstream_head(snap["upstream"])
            row["upstream_head"] = head[:12] if head else None
            if head is None:
                row["fetch"] = "unreachable"
            elif head != snap["commit"]:
                row["fetch"] = "upstream moved"
                errors.append(
                    f"{snap['dir']}: upstream {snap['upstream']} moved "
                    f"{snap['commit'][:12]} -> {head[:12]}"
                )
            else:
                row["fetch"] = "in sync"
        rows.append(row)

    if as_json:
        print(json.dumps({"ok": not errors, "snapshots": rows, "errors": errors}, indent=2))
    else:
        for row in rows:
            print(f"{row['dir']} @ {row['commit']} ({row['upstream']}){'' if 'status' not in row else ' — ' + row['status']}")
            for p in row.get("pairs", []):
                print(f"  {p['vendor']} -> {p['ours']}  (+{p['+']}/-{p['-']} vs vendored)")
            if "fetch" in row:
                head = row.get("upstream_head") or "?"
                print(f"  upstream HEAD {head}: {row['fetch']}")
            if show_diff:
                for p in row.get("pairs", []):
                    vend = ROOT / "vendor" / row["dir"] / p["vendor"]
                    ours = ROOT / p["ours"]
                    print("".join(difflib.unified_diff(
                        vend.read_text(encoding="utf-8", errors="replace").splitlines(True),
                        ours.read_text(encoding="utf-8", errors="replace").splitlines(True),
                        fromfile=str(vend.relative_to(ROOT)), tofile=p["ours"],
                    )))
        for err in errors:
            print(f"vendor-check: {err}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
