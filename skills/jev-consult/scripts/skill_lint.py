#!/usr/bin/env python3
"""skill_lint.py — lint SKILL.md files for frontmatter sanity.

Usage: python skill_lint.py SKILL.md [more.md ...]
Exit 0 clean/warn, 1 on any error, 2 on bad args.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

_FM_KEY = re.compile(r"^(name|description):\s*(.*)$")


_FM_END = re.compile(r"\n---[ \t]*(\r?\n|$)")


def _fm_end(text: str, start: int) -> int:
    """Index of the newline before the closing '---' marker, or -1.

    The closer must be a bare '---' line (trailing blanks tolerated), so
    '\n---foo' inside the block is not mistaken for the end.
    """
    match = _FM_END.search(text, start)
    return match.start() if match else -1


def raw_frontmatter(text: str) -> dict[str, str] | None:
    """Minimal frontmatter read: single-line name/description scalars only.

    Returns None when there is no --- ... --- block at all, else a dict
    (possibly empty) of the recognized keys.
    """
    if not text.lstrip().startswith("---"):
        return None
    start = text.index("---") + 3
    end = _fm_end(text, start)
    if end < 0:
        return None
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
    if meta is None:
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

            parsed = _json.loads(policy_file.read_text(encoding="utf-8"))
            policy_keys = set(parsed) if isinstance(parsed, dict) else None
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


def _fm_bounds(text: str) -> tuple[int, int] | None:
    if not text.lstrip().startswith("---"):
        return None
    start = text.index("---") + 3
    end = _fm_end(text, start)
    if end < 0:
        return None
    return start, end


def _write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
        os.replace(tmp, str(path))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def fix_name(path: Path) -> bool:
    """Rewrite the frontmatter name to the parent directory name. Returns True if changed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    bounds = _fm_bounds(text)
    if bounds is None:
        return False
    start, end = bounds
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
    _write(path, text[:start] + new_block + text[end:])
    return True


def fix_case(path: Path) -> bool:
    """Rewrite the frontmatter name as lowercase-hyphenated. Returns True if changed."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    bounds = _fm_bounds(text)
    if bounds is None:
        return False
    start, end = bounds
    block = text[start:end]

    def normalize(m: "re.Match[str]") -> str:
        if m.group(1) != "name":
            return m.group(0)
        value = re.sub(r"[^a-z0-9]+", "-", m.group(2).strip().lower()).strip("-")
        return "name: %s" % value

    new_block, n = re.subn(r"^(name|description):\s*(.*)$", normalize, block, flags=re.M)
    if n == 0 or new_block == block:
        return False
    _write(path, text[:start] + new_block + text[end:])
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    do_fix = "--fix" in argv
    as_json = "--json" in argv
    strict = "--strict" in argv
    quiet = "--quiet" in argv
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
    argv = [a for a in argv if a not in ("--fix", "--json", "--strict", "--quiet")]
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
    if do_fix:
        for path in paths:
            if any(f["rule"] == "S005" for f in lint_skill(path)):
                if fix_name(path):
                    sys.stderr.write("fixed S005 %s\n" % path)
            if any(f["rule"] == "S008" for f in lint_skill(path)):
                if fix_case(path):
                    sys.stderr.write("fixed S008 %s\n" % path)
    if as_json:
        import json as _json

        all_rows = [
            {"path": str(path), **f} for path in paths for f in lint_skill(path)
        ]
        rows = [r for r in all_rows if not severity or r["severity"] == severity]
        def bad(r: dict) -> bool:
            return r["severity"] == "error" or (strict and r["severity"] == "warn")

        sys.stdout.write(_json.dumps({"findings": rows}, indent=2) + "\n")
        return 1 if any(bad(r) for r in all_rows) else 0
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
    return rc


if __name__ == "__main__":
    sys.exit(main())
