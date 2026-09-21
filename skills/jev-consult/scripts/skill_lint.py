#!/usr/bin/env python3
"""skill_lint.py — lint SKILL.md files for frontmatter sanity.

Usage: python skill_lint.py SKILL.md [more.md ...]
Exit 0 clean/warn, 1 on any error, 2 on bad args.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

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
    return findings


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write("usage: skill_lint.py SKILL.md [more.md ...]\n")
        return 2
    rc = 0
    paths: list[Path] = []
    for arg in argv:
        path = Path(arg)
        if path.is_dir():
            paths.extend(sorted(path.rglob("SKILL.md")))
        else:
            paths.append(path)
    for path in paths:
        for f in lint_skill(path):
            sys.stdout.write("%s %s %s: %s\n" % (f["severity"], f["rule"], path, f["message"]))
            if f["severity"] == "error":
                rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
