#!/usr/bin/env python3
"""skill_lint.py — lint SKILL.md files for frontmatter sanity.

Usage: python skill_lint.py SKILL.md [more.md ...]
Exit 0 clean/warn, 1 on any error, 2 on bad args.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402

_FM_KEY = re.compile(r"^(name|description):\s*(.*)$")


def raw_frontmatter(text: str) -> dict[str, str]:
    """Minimal frontmatter read: single-line name/description scalars only."""
    if not text.lstrip().startswith("---"):
        return {}
    start = text.index("---") + 3
    end = text.find("\n---", start)
    if end < 0:
        return {}
    meta: dict[str, str] = {}
    for line in text[start:end].splitlines():
        match = _FM_KEY.match(line)
        if match and match.group(1) not in meta:
            meta[match.group(1)] = match.group(2).strip().strip("\"'")
    return meta


def lint_skill(path: Path) -> list[dict]:
    findings = []
    if not path.is_file():
        return [
            {
                "rule": "S001",
                "severity": "error",
                "message": "file not found",
            }
        ]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [{"rule": "S001", "severity": "error", "message": "unreadable: %s" % exc}]
    meta = raw_frontmatter(text)
    if not meta:
        findings.append(
            {"rule": "S002", "severity": "error", "message": "no frontmatter block"}
        )
        return findings
    name = meta.get("name", "").strip()
    if not name:
        findings.append(
            {"rule": "S003", "severity": "error", "message": "missing name"}
        )
    elif path.parent.name and name != path.parent.name:
        findings.append(
            {
                "rule": "S005",
                "severity": "warn",
                "message": "name %r does not match directory %r" % (name, path.parent.name),
            }
        )
    if name and not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
        findings.append(
            {
                "rule": "S008",
                "severity": "warn",
                "message": "name %r is not lowercase-hyphenated" % name,
            }
        )
    description = meta.get("description", "").strip()
    if not description:
        findings.append(
            {"rule": "S004", "severity": "warn", "message": "missing description"}
        )
    elif len(description) > 1024:
        findings.append(
            {
                "rule": "S006",
                "severity": "warn",
                "message": "description is %d chars (over 1024)" % len(description),
            }
        )
    policy_file = path.parent / "policy.json"
    if policy_file.is_file():
        try:
            import json as _json

            policy_keys = set(_json.loads(policy_file.read_text(encoding="utf-8")))
        except (OSError, _json.JSONDecodeError):
            policy_keys = None
        if policy_keys is not None:
            cited = set(
                re.findall(r"`([a-z][a-z0-9_]*)`\s*\([^)]*policy\.json", text)
            )
            for key in sorted(cited - policy_keys):
                findings.append(
                    {
                        "rule": "S007",
                        "severity": "warn",
                        "message": "cited policy.json key %r not found" % key,
                    }
                )
    return findings


def fix_name(path: Path) -> bool:
    """Rewrite the frontmatter name to the parent directory name. Returns True if changed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    start = text.index("---") + 3 if text.lstrip().startswith("---") else -1
    end = text.find("\n---", start) if start >= 0 else -1
    if end < 0:
        return False
    block = text[start:end]
    new_block, n = re.subn(
        r"^(name|description):\s*(.*)$",
        lambda m: "%s: %s" % (m.group(1), path.parent.name)
        if m.group(1) == "name"
        else m.group(0),
        block,
        flags=re.M,
    )
    if n == 0:
        return False
    path.write_text(text[:start] + new_block + text[end:], encoding="utf-8")
    return True


def fix_case(path: Path) -> bool:
    """Rewrite the frontmatter name as lowercase-hyphenated. Returns True if changed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    start = text.index("---") + 3 if text.lstrip().startswith("---") else -1
    end = text.find("\n---", start) if start >= 0 else -1
    if end < 0:
        return False
    block = text[start:end]

    def normalize(m: "re.Match[str]") -> str:
        if m.group(1) != "name":
            return m.group(0)
        value = re.sub(r"[^a-z0-9]+", "-", m.group(2).strip().lower()).strip("-")
        return "name: %s" % value

    new_block, n = re.subn(r"^(name|description):\s*(.*)$", normalize, block, flags=re.M)
    if n == 0 or new_block == block:
        return False
    path.write_text(text[:start] + new_block + text[end:], encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    do_fix = "--fix" in argv
    as_json = "--json" in argv
    strict = "--strict" in argv
    quiet = "--quiet" in argv
    fail_fast = "--fail-fast" in argv
    severity = ""
    if "--severity" in argv:
        idx = argv.index("--severity")
        if idx + 1 >= len(argv):
            sys.stderr.write("--severity needs a value (error|warn|info)\n")
            return 2
        severity = argv[idx + 1].strip().lower()
        if severity not in ("error", "warn", "info"):
            sys.stderr.write("bad --severity %r (want error|warn|info)\n" % severity)
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    else:
        env_sev = os.environ.get("JEV_SLINT_SEVERITY", "").strip().lower()
        if env_sev in ("error", "warn", "info"):
            severity = env_sev
    out_path = ""
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 >= len(argv):
            sys.stderr.write("--out needs a PATH value\n")
            return 2
        out_path = argv[idx + 1].strip()
        argv = argv[:idx] + argv[idx + 2 :]
    watch_seconds = 0.0
    if "--watch" in argv:
        idx = argv.index("--watch")
        if idx + 1 >= len(argv):
            sys.stderr.write("--watch needs a SECONDS value\n")
            return 2
        try:
            watch_seconds = float(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --watch %r (seconds)\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    max_ticks_arg = 0
    if "--max-ticks" in argv:
        idx = argv.index("--max-ticks")
        if idx + 1 >= len(argv):
            sys.stderr.write("--max-ticks needs an N value\n")
            return 2
        try:
            max_ticks_arg = int(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --max-ticks %r (integer)\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    watch_max_arg = 0.0
    if "--watch-max" in argv:
        idx = argv.index("--watch-max")
        if idx + 1 >= len(argv):
            sys.stderr.write("--watch-max needs a SECONDS value\n")
            return 2
        try:
            watch_max_arg = float(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --watch-max %r (seconds)\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    verdict_path = ""
    if "--verdict" in argv:
        idx = argv.index("--verdict")
        if idx + 1 >= len(argv):
            sys.stderr.write("--verdict needs a PATH value\n")
            return 2
        verdict_path = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    argv = [
        a
        for a in argv
        if a not in ("--fix", "--json", "--strict", "--quiet", "--fail-fast")
    ]
    if not argv:
        sys.stderr.write(
            "usage: skill_lint.py SKILL.md [more.md ...] [--fix] [--strict]\n"
        )
        return 2
    rc = 0
    paths: list[Path] = []
    for arg in argv:
        path = Path(arg)
        if path.is_dir():
            paths.extend(sorted(path.rglob("SKILL.md")))
        else:
            paths.append(path)
    if watch_seconds > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_SLINT_WATCH_MAX", max_ticks_arg)
        ticks = 0
        dead = _watch.deadline("JEV_SLINT_WATCH_SECS", watch_max_arg)
        tick: dict = {}
        verdict_ok = True

        def _write_verdict(rc_now: int) -> bool:
            return _watch.write_verdict(
                verdict_path,
                {
                    "verdict": "fail" if rc_now else "pass",
                    "ticks": ticks,
                    "findings": tick.get("findings", 0),
                    "errors": tick.get("errors", 0),
                    "warnings": tick.get("warnings", 0),
                    "infos": tick.get("infos", 0),
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            rows = [f for path in paths for f in lint_skill(path)]
            tick = {
                "ts": int(_time.time()),
                "findings": len(rows),
                "errors": sum(1 for r in rows if r["severity"] == "error"),
                "warnings": sum(1 for r in rows if r["severity"] == "warn"),
                "infos": sum(1 for r in rows if r["severity"] == "info"),
            }
            _watch.emit(tick, out_path, quiet=_watch.quiet("JEV_SLINT_WATCH_QUIET", quiet), bad=tick["errors"] or (strict and tick["findings"]))
            ticks += 1
            if verdict_path and verdict_ok:
                rc_now = 1 if (tick["errors"] or (strict and tick["findings"])) else 0
                if not _write_verdict(rc_now):
                    verdict_ok = False  # warn once, stop retrying
            if fail_fast and (tick["errors"] or (strict and tick["findings"])):
                break
            _time.sleep(watch_seconds)
        rc = 1 if (tick.get("errors", 0) or (strict and tick.get("findings", 0))) else 0
        if verdict_path and verdict_ok and not _write_verdict(rc):
            return 1
        return rc
    if do_fix:
        for path in paths:
            if any(f["rule"] == "S005" for f in lint_skill(path)):
                if fix_name(path):
                    sys.stderr.write("fixed S005 %s\n" % path)
            if any(f["rule"] == "S008" for f in lint_skill(path)):
                if fix_case(path):
                    sys.stderr.write("fixed S008 %s\n" % path)
    if as_json or out_path:
        import json as _json

        all_rows = [
            {"path": str(path), **f} for path in paths for f in lint_skill(path)
        ]
        rows = [r for r in all_rows if not severity or r["severity"] == severity]
        def bad(r: dict) -> bool:
            return r["severity"] == "error" or (strict and r["severity"] == "warn")

        if out_path:
            try:
                Path(out_path).write_text(
                    _json.dumps({"findings": rows}, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
                return 1
            sys.stderr.write("wrote %d finding(s) to %s\n" % (len(rows), out_path))
        rc_now = 1 if any(bad(r) for r in all_rows) else 0
        if verdict_path:
            if not _watch.write_verdict(
                verdict_path,
                {
                    "verdict": "fail" if rc_now else "pass",
                    "ticks": 1,
                    "findings": len(all_rows),
                    "errors": sum(1 for r in all_rows if r["severity"] == "error"),
                    "warnings": sum(1 for r in all_rows if r["severity"] == "warn"),
                    "infos": sum(1 for r in all_rows if r["severity"] == "info"),
                },
            ):
                return 1
        if not as_json:
            return rc_now
        sys.stdout.write(_json.dumps({"findings": rows}, indent=2) + "\n")
        return rc_now
    n_err = 0
    n_warn = 0
    for path in paths:
        for f in lint_skill(path):
            if f["severity"] == "error":
                n_err += 1
            else:
                n_warn += 1
            if f["severity"] == "error" or (strict and f["severity"] == "warn"):
                rc = 1
            if severity and f["severity"] != severity:
                continue
            if quiet and f["severity"] != "error":
                continue
            sys.stdout.write("%s %s %s: %s\n" % (f["severity"], f["rule"], path, f["message"]))
    if not quiet and n_err + n_warn and len(paths) > 1:
        sys.stdout.write(
            "%d findings (%d errors, %d warns) in %d files\n"
            % (n_err + n_warn, n_err, n_warn, len(paths))
        )
    if verdict_path:
        all_f = [f for path in paths for f in lint_skill(path)]
        if not _watch.write_verdict(
            verdict_path,
            {
                "verdict": "fail" if rc else "pass",
                "ticks": 1,
                "findings": len(all_f),
                "errors": sum(1 for f in all_f if f["severity"] == "error"),
                "warnings": sum(1 for f in all_f if f["severity"] == "warn"),
                "infos": sum(1 for f in all_f if f["severity"] == "info"),
            },
        ):
            return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
