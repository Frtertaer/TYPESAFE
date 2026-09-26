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


def _upstream_head(repo: str) -> tuple[str | None, str | None]:
    """(sha, error) — error set when the remote couldn't be reached, so an
    unreachable upstream reports as a failure instead of passing silently."""
    try:
        out = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "ls-remote failed: %s" % exc
    if out.returncode != 0:
        return None, "ls-remote rc=%d: %s" % (out.returncode, (out.stderr or "").strip()[:120])
    if not out.stdout.strip():
        return None, "ls-remote returned no HEAD"
    return out.stdout.split()[0], None


def _manifest_errors(snapshots: object) -> list[str]:
    """Structural validation — malformed entries exit 2, not traceback."""
    problems = []
    if not isinstance(snapshots, list):
        return ["snapshots is not a list"]
    for i, snap in enumerate(snapshots):
        where = "snapshots[%d]" % i
        if not isinstance(snap, dict):
            problems.append("%s: not an object" % where)
            continue
        for key in ("dir", "upstream", "commit"):
            if not isinstance(snap.get(key), str) or not snap[key]:
                problems.append("%s: missing/non-string %r" % (where, key))
        ported = snap.get("ported", [])
        if not isinstance(ported, list):
            problems.append("%s: 'ported' not a list" % where)
            continue
        for j, pair in enumerate(ported):
            if not isinstance(pair, dict) or not all(
                isinstance(pair.get(k), str) and pair[k] for k in ("vendor", "ours")
            ):
                problems.append("%s.ported[%d]: needs non-empty 'vendor' and 'ours'" % (where, j))
    return problems


def _within_repo(path: Path) -> bool:
    """A manifest path must resolve inside the repo — '../' segments would
    otherwise let PORTS.json point reads outside the checkout."""
    try:
        path.resolve().relative_to(ROOT)
    except (OSError, ValueError):
        return False
    return True


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
        snaps = json.loads(MANIFEST.read_text(encoding="utf-8")).get("snapshots")
    except (ValueError, AttributeError) as exc:
        print(f"vendor-check: invalid {MANIFEST}: {exc}", file=sys.stderr)
        return 2
    manifest_problems = _manifest_errors(snaps)
    for problem in manifest_problems:
        print(f"vendor-check: invalid {MANIFEST}: {problem}", file=sys.stderr)
    if manifest_problems:
        return 2

    errors = []
    rows = []
    for snap in snaps:
        d = (ROOT / "vendor" / snap["dir"]).resolve()
        row = {"dir": snap["dir"], "upstream": snap["upstream"], "commit": snap["commit"][:12]}
        if not _within_repo(d):
            errors.append(f"{snap['dir']}: snapshot dir escapes repo")
            row["status"] = "missing"
            rows.append(row)
            continue
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
            vend = (d / pair["vendor"]).resolve()
            ours = (ROOT / pair["ours"]).resolve()
            if not _within_repo(vend) or not _within_repo(ours):
                errors.append(
                    f"{snap['dir']}: ported pair escapes repo: "
                    f"{pair['vendor']} -> {pair['ours']}"
                )
                continue
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
            head, fetch_err = _upstream_head(snap["upstream"])
            row["upstream_head"] = head[:12] if head else None
            if head is None:
                row["fetch"] = "unreachable"
                errors.append(
                    f"{snap['dir']}: could not reach {snap['upstream']}: {fetch_err}"
                )
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
                    vend = (ROOT / "vendor" / row["dir"] / p["vendor"]).resolve()
                    ours = (ROOT / p["ours"]).resolve()
                    if not (_within_repo(vend) and _within_repo(ours)):
                        continue
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
