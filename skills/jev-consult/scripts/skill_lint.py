#!/usr/bin/env python3
"""skill_lint.py — lint SKILL.md files for frontmatter sanity.

Usage: python skill_lint.py SKILL.md [more.md ...]
Exit 0 clean/warn, 1 on any error, 2 on bad args.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402

# Flat frontmatter scalar lines only: `key: value` at column 0 (nested YAML
# blocks are skipped — their indented lines cannot match).
_FM_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")


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


_STDIN_MD: str | None = None


def _skill_text(path: Path) -> str | None:
    """Read the SKILL.md text; the '-' path reads stdin once (cached)."""
    global _STDIN_MD
    if str(path) == "-":
        if _STDIN_MD is None:
            _STDIN_MD = sys.stdin.read()
        return _STDIN_MD
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None


def _skill_summary(path: Path) -> dict:
    """Compact comparable view of one SKILL.md ('-' reads stdin)."""
    text = _skill_text(path)
    if text is None:
        return {"readable": False}
    meta = raw_frontmatter(text)
    body = text
    if meta is not None:
        end = _fm_end(text, text.index("---") + 3)
        if end >= 0:
            close_end = text.find("\n", end + 1)
            body = text[close_end + 1 :] if close_end > 0 else ""
    return {
        "readable": True,
        "has_frontmatter": meta is not None,
        "frontmatter": meta or {},
        "cited_scripts": sorted(
            set(re.findall(r"`?scripts/([A-Za-z0-9_-]+\.py)`?", text))
        ),
        "body_lines": len(body.splitlines()),
        "body_sha1": hashlib.sha1(body.encode("utf-8")).hexdigest()[:12],
    }


def diff_skill(a: dict, b: dict) -> list[str]:
    """+/-/~ lines comparing two _skill_summary dicts."""
    lines: list[str] = []
    for key in sorted(set(a) | set(b)):
        if key == "frontmatter":
            fa = a.get(key) or {}
            fb = b.get(key) or {}
            for fk in sorted(set(fa) | set(fb)):
                if fk not in fa:
                    lines.append("+ frontmatter.%s = %s" % (fk, fb[fk]))
                elif fk not in fb:
                    lines.append("- frontmatter.%s = %s" % (fk, fa[fk]))
                elif fa[fk] != fb[fk]:
                    lines.append(
                        "~ frontmatter.%s: %s -> %s" % (fk, fa[fk], fb[fk])
                    )
        elif key == "cited_scripts":
            sa = set(a.get(key) or [])
            sb = set(b.get(key) or [])
            for item in sorted(sb - sa):
                lines.append("+ cited_scripts.%s" % item)
            for item in sorted(sa - sb):
                lines.append("- cited_scripts.%s" % item)
        elif key not in a:
            lines.append("+ %s = %s" % (key, b[key]))
        elif key not in b:
            lines.append("- %s = %s" % (key, a[key]))
        elif a[key] != b[key]:
            lines.append("~ %s: %s -> %s" % (key, a[key], b[key]))
    return lines


# Keys a SKILL.md frontmatter may legitimately carry (Anthropic agent-skills
# spec + the pack's own contract). --usage flags anything else as S014.
KNOWN_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


def lint_skill(path: Path, usage: bool = False) -> list[dict]:
    findings = []
    if str(path) != "-" and not path.is_file():
        return [
            {
                "rule": "S001",
                "severity": "error",
                "message": "file not found",
            }
        ]
    text = _skill_text(path)
    if text is None:
        return [{"rule": "S001", "severity": "error", "message": "unreadable"}]
    meta = raw_frontmatter(text)
    if meta is None:
        findings.append(
            {"rule": "S002", "severity": "error", "message": "no frontmatter block"}
        )
        return findings
    if usage:
        for key in sorted(set(meta) - KNOWN_FRONTMATTER_KEYS):
            findings.append(
                {
                    "rule": "S014",
                    "severity": "info",
                    "message": "frontmatter key %r is outside the skill-metadata contract" % key,
                }
            )
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

            parsed = _json.loads(policy_file.read_text(encoding="utf-8-sig"))
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
            for key in sorted(policy_keys):
                if not re.search(r"\b" + re.escape(key) + r"\b", text):
                    findings.append(
                        {
                            "rule": "S011",
                            "severity": "warn",
                            "message": "policy.json key %r never mentioned"
                            % key,
                        }
                    )
    scripts_dir = path.parent / "scripts"
    script_roots = [scripts_dir]
    if path.parent.parent.name == "skills":
        # skills/<name>/SKILL.md may cite repo-root scripts/ paths too
        script_roots.append(path.parent.parent.parent / "scripts")
    if scripts_dir.is_dir():
        cited_scripts = set(
            re.findall(r"`?scripts/([A-Za-z0-9_-]+\.py)`?", text)
        )
        for script in sorted(cited_scripts):
            if not any((root / script).is_file() for root in script_roots):
                findings.append(
                    {
                        "rule": "S009",
                        "severity": "warn",
                        "message": "cited scripts/%s not found" % script,
                    }
                )
        mentioned = set(re.findall(r"\b([A-Za-z0-9_-]+\.py)\b", text))
        for script in sorted(p.name for p in scripts_dir.glob("*.py")):
            if script.startswith("_") or script in mentioned:
                continue
            try:
                head = (scripts_dir / script).read_text(
                    encoding="utf-8-sig", errors="replace"
                )[:2000]
            except OSError:
                continue
            if "[vendored]" in head:
                continue
            findings.append(
                {
                    "rule": "S010",
                    "severity": "warn",
                    "message": "scripts/%s never mentioned in SKILL.md" % script,
                }
            )
        exposed: dict[str, set] = {}
        for p in scripts_dir.glob("*.py"):
            try:
                src = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            flags = set(re.findall(r"""add_argument\([^)]*["'](--[a-z][a-z0-9-]+)["']""", src))
            flags |= set(re.findall(r"""["'](--[a-z][a-z0-9-]+)["']\s+in\s+argv""", src))
            flags |= set(re.findall(r"""argv\[\w*\]\s*==\s*["'](--[a-z][a-z0-9-]+)["']""", src))
            flags |= set(re.findall(r"""_flag_value\([^)]*["'](--[a-z][a-z0-9-]+)["']""", src))
            exposed[p.name] = flags
        for script in sorted(p.name for p in scripts_dir.glob("*.py")):
            if script.startswith("_") or script not in mentioned:
                continue
            for flag in sorted(exposed.get(script, set())):
                if flag in ("--help", "--version", "--") or flag in text:
                    continue
                findings.append(
                    {
                        "rule": "S012",
                        "severity": "info",
                        "message": "scripts/%s exposes %s but SKILL.md never documents it" % (script, flag),
                    }
                )
        doc_flags = set(re.findall(r"--[a-z][a-z0-9-]+", text))
        exposed_all = set().union(*exposed.values()) if exposed else set()
        exempt = {
            "--" + n
            for n in (
                "help", "version", "-", "force", "yes", "enable",
                "no-enable", "no-verify", "no-gpg-sign",
                "uninstall", "check-key",
            )
        }
        for flag in sorted(doc_flags - exposed_all - exempt):
            findings.append(
                {
                    "rule": "S013",
                    "severity": "warn",
                    "message": "SKILL.md documents %s but no scripts/*.py exposes it" % flag,
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


def _fix_name_text(path: Path) -> str | None:
    """Return the text with the frontmatter name set to the parent dir, or None."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    bounds = _fm_bounds(text)
    if bounds is None:
        return None
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
        return None
    return text[:start] + new_block + text[end:]


def fix_name(path: Path) -> bool:
    """Rewrite the frontmatter name to the parent directory name. Returns True if changed."""
    new_text = _fix_name_text(path)
    if new_text is None:
        return False
    _atomic_write(path, new_text)
    return True


def _fix_case_text(path: Path) -> str | None:
    """Return the text with the frontmatter name lowercase-hyphenated, or None."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    bounds = _fm_bounds(text)
    if bounds is None:
        return None
    start, end = bounds
    block = text[start:end]

    def normalize(m: "re.Match[str]") -> str:
        if m.group(1) != "name":
            return m.group(0)
        value = re.sub(r"[^a-z0-9]+", "-", m.group(2).strip().lower()).strip("-")
        return "name: %s" % value

    new_block, n = re.subn(r"^(name|description):\s*(.*)$", normalize, block, flags=re.M)
    if n == 0 or new_block == block:
        return None
    return text[:start] + new_block + text[end:]


def fix_case(path: Path) -> bool:
    """Rewrite the frontmatter name as lowercase-hyphenated. Returns True if changed."""
    new_text = _fix_case_text(path)
    if new_text is None:
        return False
    _atomic_write(path, new_text)
    return True


# --init prints this minimal skeleton; the S005 name==directory rule means it
# only lints clean under a directory named 'my-skill' — rename as needed.
INIT_DOC = (
    "---\n"
    "name: my-skill\n"
    "description: one line saying what this skill does and when to use it\n"
    "---\n"
    "\n"
    "# my-skill\n"
    "\n"
    "## Usage\n"
    "\n"
    "Describe the workflow steps here.\n"
)

RULES = {
    "S001": "SKILL.md file not found or unreadable",
    "S002": "no YAML frontmatter block",
    "S003": "frontmatter has no name",
    "S004": "frontmatter has no description",
    "S005": "name does not match the directory name",
    "S006": "description is over 1024 chars",
    "S007": "cited policy.json key does not exist in the sibling policy.json",
    "S008": "name is not lowercase-hyphenated",
    "S009": "cited scripts/*.py file does not exist in the sibling scripts/ dir",
    "S010": "scripts/*.py file is never mentioned in SKILL.md (skips _* and [vendored] scripts)",
    "S011": "policy.json key is never mentioned in SKILL.md",
    "S012": "a mentioned scripts/*.py exposes a --flag that SKILL.md never documents",
    "S013": "SKILL.md documents a --flag that no scripts/*.py exposes",
    "S014": "frontmatter key is outside the skill-metadata contract; harnesses ignore it",
}


def _baseline_key(row: dict) -> tuple:
    """Stable identity of a finding: file + rule + message text."""
    return _watch.baseline_key(row, ("path", "rule", "message"))


def load_baseline(path: str) -> set:
    """Load a baseline findings file (from --baseline-write or --out);
    missing/corrupt warns and returns an empty set so all findings count."""
    return _watch.load_baseline(path, ("path", "rule", "message"))


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


USAGE = 'Usage: python skill_lint.py SKILL.md [more.md ...] [flags]\nLint SKILL.md frontmatter sanity (name, description, length caps).\nFlags:\n  --strict          exit 1 on warnings too\n  --fix             auto-apply safe fixes in place\n  --dry-run         with --fix: report what would change without writing\n  --explain RULE    print the description of one rule id and exit ("-" reads it from stdin)\n  --rules           print every rule id + description (--json emits a list)\n  --schema          print the frontmatter key contract (--json emits an object)\n  --severity S[,S...]  only these severities (error|warn|info comma list; JEV_SLINT_SEVERITY)\n  --only R[,R...]     lint only these rule ids (rc 2 on unknown id)\n  --usage           add info findings for frontmatter keys outside the contract\n  --env             print the resolved env config JSON (files, severity, strict, quiet, watch_max, watch_secs, watch_quiet; --jq KEY one field, --out PATH writes it)\n  --quiet           print only errors/warnings count\n  --baseline PATH   suppress findings already recorded in PATH ("-" reads it from stdin)\n  --baseline-write PATH  write current findings to PATH for --baseline runs\n  --json            findings as JSON array\n  --md              findings as a Markdown table\n  --csv             findings as CSV rows (--keys picks the columns)\n  --jsonl           findings as one JSON object per line (adds path)\n  --keys a,b        with --jsonl/--csv: keep only these keys in each row / as the columns (rc 2 on empty)\n  --rules           list every rule id + description (with --json/--md)\n  --jq KEY          one dotted-path field of the findings payload\n  --out PATH        append/write the payload to a file (fail-open)\n  -                 read SKILL.md content from stdin (no --fix/--watch/--diff)\n  --diff PATH       diff this SKILL.md against another (frontmatter/cited_scripts/body_lines/body_sha1; "-" reads the other side from stdin)\n  --self-test       lint a synthetic known-bad SKILL.md; exit 1 when no findings\n  --init            print a minimal SKILL.md skeleton (name it after its directory) and exit\n  --help            print this usage and exit\n  --version         print the pack policy version and exit\n  --watch S         re-lint every S seconds emitting tick JSON\n  --watch-max S     stop the watch after S elapsed seconds\n  --max-ticks N     stop the watch after N ticks\n  --fail-fast       stop the watch on the first erroring tick\n  --unchanged-max N stop the watch after N consecutive identical ticks\n  --verdict PATH    write a slim {verdict: pass|fail, ...} JSON ("-" prints it to stdout)\nExit 0 clean/warn, 1 on any error, 2 on bad args.\n'


def main(argv: list[str] | None = None) -> int:
    _watch.fix_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if _watch.maybe_version(argv):
        return 0
    if "-h" in argv or "--help" in argv:
        sys.stdout.write(USAGE)
        return 0
    do_fix = "--fix" in argv
    dry_run = "--dry-run" in argv
    as_json = "--json" in argv
    as_md = "--md" in argv
    as_csv = "--csv" in argv
    as_jsonl = "--jsonl" in argv
    strict = "--strict" in argv
    usage = "--usage" in argv
    quiet = "--quiet" in argv
    fail_fast = "--fail-fast" in argv
    severity: set[str] = set()
    if "--severity" in argv:
        idx = argv.index("--severity")
        if idx + 1 >= len(argv):
            sys.stderr.write("--severity needs a value (error|warn|info)\n")
            return 2
        picked = _watch.severity_arg(argv[idx + 1])
        if picked is None:
            sys.stderr.write("bad --severity %r (want comma list of error|warn|info)\n" % argv[idx + 1])
            return 2
        severity = picked
        argv = argv[:idx] + argv[idx + 2 :]
    else:
        env_sev = os.environ.get("JEV_SLINT_SEVERITY", "").strip().lower()
        picked = _watch.severity_arg(env_sev)
        if picked:
            severity = picked
    only: set[str] = set()
    if "--only" in argv:
        idx = argv.index("--only")
        if idx + 1 >= len(argv):
            sys.stderr.write("--only needs a RULE[,RULE...] value\n")
            return 2
        only = {s.strip().upper() for s in argv[idx + 1].split(",") if s.strip()}
        unknown = only - set(RULES)
        if not only or unknown:
            sys.stderr.write(
                "bad --only %r (rules: %s)\n" % (argv[idx + 1], ", ".join(sorted(RULES)))
            )
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    keys_sel = ""
    if "--keys" in argv:
        idx = argv.index("--keys")
        if idx + 1 >= len(argv):
            sys.stderr.write("--keys needs a a,b value\n")
            return 2
        keys_sel = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    keys = [k.strip() for k in keys_sel.split(",") if k.strip()] if keys_sel else []
    if keys_sel and not keys:
        sys.stderr.write("--keys names no fields\n")
        return 2
    unchanged_max = 0
    if "--unchanged-max" in argv:
        idx = argv.index("--unchanged-max")
        if idx + 1 >= len(argv):
            sys.stderr.write("--unchanged-max needs a value (int ticks)\n")
            return 2
        try:
            unchanged_max = int(argv[idx + 1])
        except ValueError:
            sys.stderr.write("bad --unchanged-max %r\n" % argv[idx + 1])
            return 2
        argv = argv[:idx] + argv[idx + 2 :]
    if "--rules" in argv:
        argv = [a for a in argv if a != "--rules"]
        if as_json:
            sys.stdout.write(
                json.dumps(
                    [{"rule": k, "description": v} for k, v in sorted(RULES.items())],
                    indent=2,
                )
                + "\n"
            )
        elif as_md:
            _watch.md_table(
                [{"rule": k, "description": v} for k, v in sorted(RULES.items())],
                ["rule", "description"],
            )
        elif as_csv:
            _watch.csv_table(
                [{k: r.get(k) for k in keys} if keys else r
                 for r in ({"rule": k, "description": v} for k, v in sorted(RULES.items()))],
                keys or ["rule", "description"],
            )
        else:
            for _rule in sorted(RULES):
                sys.stdout.write("%s: %s\n" % (_rule, RULES[_rule]))
        return 0
    if "--explain" in argv:
        idx = argv.index("--explain")
        if idx + 1 >= len(argv):
            sys.stderr.write("--explain needs a RULE value\n")
            return 2
        rule = _watch.text_arg(argv[idx + 1]).strip().upper()
        if rule not in RULES:
            sys.stderr.write(
                "unknown rule %r (rules: %s)\n" % (rule, ", ".join(sorted(RULES)))
            )
            return 2
        sys.stdout.write("%s: %s\n" % (rule, RULES[rule]))
        return 0
    if "--self-test" in argv:
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad-skill" / "SKILL.md"
            bad.parent.mkdir(parents=True, exist_ok=True)
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("no frontmatter here\n")
            findings = lint_skill(bad)
        rules = sorted({f["rule"] for f in findings})
        ok = bool(rules)
        if as_json:
            sys.stdout.write(
                json.dumps({"self_test": "ok" if ok else "FAIL", "rules": rules})
                + "\n"
            )
        else:
            sys.stdout.write(
                "self-test: %s rules=%s\n" % ("ok" if ok else "FAIL", ",".join(rules))
            )
        return 0 if ok else 1
    if "--init" in argv:
        sys.stdout.write(INIT_DOC)
        return 0
    if "--schema" in argv:
        rows = {
            "name": {
                "required": True,
                "type": "slug ^[a-z0-9][a-z0-9-]*$, equals the parent dir name",
            },
            "description": {
                "required": False,
                "type": "string, <= 1024 chars",
            },
        }
        if "--json" in argv:
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        else:
            for key in rows:
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (
                        key,
                        rows[key]["type"],
                        "required" if rows[key]["required"] else "recommended",
                    )
                )
        return 0
    out_path = ""
    if "--out" in argv:
        idx = argv.index("--out")
        if idx + 1 >= len(argv):
            sys.stderr.write("--out needs a PATH value\n")
            return 2
        out_path = argv[idx + 1].strip()
        argv = argv[:idx] + argv[idx + 2 :]
    diff_path = None
    if "--diff" in argv:
        idx = argv.index("--diff")
        if idx + 1 >= len(argv):
            sys.stderr.write("--diff needs a PATH value\n")
            return 2
        diff_path = argv[idx + 1]
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
    jq_value = ""
    if "--jq" in argv:
        i = argv.index("--jq")
        if i + 1 >= len(argv):
            sys.stderr.write("--jq needs a KEY value\n")
            return 2
        jq_value = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
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
    baseline_path = ""
    if "--baseline" in argv:
        idx = argv.index("--baseline")
        if idx + 1 >= len(argv):
            sys.stderr.write("--baseline needs a PATH value\n")
            return 2
        baseline_path = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    baseline_write = ""
    if "--baseline-write" in argv:
        idx = argv.index("--baseline-write")
        if idx + 1 >= len(argv):
            sys.stderr.write("--baseline-write needs a PATH value\n")
            return 2
        baseline_write = argv[idx + 1]
        argv = argv[:idx] + argv[idx + 2 :]
    argv = [
        a
        for a in argv
        if a not in ("--fix", "--dry-run", "--usage", "--json", "--md", "--csv", "--jsonl", "--strict", "--quiet", "--fail-fast")
    ]
    if "--env" in argv:
        try:
            env_watch_secs = float(os.environ.get("JEV_SLINT_WATCH_SECS", "") or 0)
        except ValueError:
            env_watch_secs = 0.0
        report = {
            "files": [a for a in argv if not a.startswith("--")],
            "severity": ",".join(sorted(severity)),
            "strict": strict,
            "quiet": quiet,
            "watch_max": _watch.cap("JEV_SLINT_WATCH_MAX", None),
            "watch_secs": env_watch_secs,
            "watch_quiet": _watch.quiet("JEV_SLINT_WATCH_QUIET", quiet),
        }
        if jq_value:
            value, found = _watch.dig(report, jq_value)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (env has: %s)\n"
                    % (jq_value, ", ".join(sorted(report)))
                )
                return 2
            sys.stdout.write(json.dumps(value) + "\n")
            return 0
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if out_path:
            try:
                _atomic_write(Path(out_path), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
        return 0
    argv = [a for a in argv if a != "--env"]
    if not argv:
        sys.stderr.write(
            "usage: skill_lint.py SKILL.md [more.md ...] [--fix] [--strict]\n"
        )
        return 2
    unknown = [a for a in argv if a.startswith("-") and a != "-"]
    if unknown:
        sys.stderr.write("unknown flag(s): %s\n" % ", ".join(unknown))
        return 2
    if "-" in argv and (do_fix or watch_seconds > 0 or diff_path is not None):
        sys.stderr.write("- (stdin) supports neither --fix, --watch nor --diff\n")
        return 2
    rc = 0
    paths: list[Path] = []
    for arg in argv:
        path = Path(arg)
        if arg != "-" and path.is_dir():
            paths.extend(sorted(path.rglob("SKILL.md")))
        else:
            paths.append(path)
    if diff_path is not None:
        if len(paths) != 1:
            sys.stderr.write("--diff needs exactly one SKILL.md path\n")
            return 2
        other = _skill_summary(Path(diff_path))
        this = _skill_summary(paths[0])
        if not (other.get("readable") and this.get("readable")):
            sys.stderr.write("--diff needs readable SKILL.md docs on both sides\n")
            return 2
        lines = diff_skill(other, this)
        sys.stdout.write("diff %s -> %s\n" % (diff_path, paths[0]))
        for line in lines:
            sys.stdout.write(line + "\n")
        sys.stdout.write("%d difference(s)\n" % len(lines))
        return 0
    baseline_keys: set | None = None
    if baseline_write:
        snapshot = [
            {"path": str(p), **f}
            for p in paths
            for f in _watch.only_filter(lint_skill(p, usage=usage), only)
        ]
        try:
            _atomic_write(
                Path(baseline_write),
                json.dumps({"findings": snapshot}, indent=2) + "\n",
            )
            sys.stderr.write(
                "wrote baseline %s (%d findings)\n"
                % (baseline_write, len(snapshot))
            )
        except OSError as exc:
            sys.stderr.write(
                "cannot write --baseline-write %s: %s\n" % (baseline_write, exc)
            )
            return 1
    if baseline_path:
        baseline_keys = load_baseline(baseline_path)
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
                    "suppressed": tick.get("suppressed", 0),
                    "elapsed_s": round(_time.time() - watch_t0, 2),
                },
            )

        watch_t0 = _time.time()
        prev_tick: dict | None = None
        unchanged = 0
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            rows = [
                {**f, "path": str(path)}
                for path in paths
                for f in _watch.only_filter(lint_skill(path, usage=usage), only)
            ]
            pre_drop = len(rows)
            if baseline_keys is not None:
                rows = [
                    r for r in rows if _baseline_key(r) not in baseline_keys
                ]
            tick = {
                "ts": int(_time.time()),
                "findings": len(rows),
                "suppressed": pre_drop - len(rows),
                "errors": sum(1 for r in rows if r["severity"] == "error"),
                "warnings": sum(1 for r in rows if r["severity"] == "warn"),
                "infos": sum(1 for r in rows if r["severity"] == "info"),
            }
            _watch.emit_or_jq(tick, jq_value, out_path, quiet=_watch.quiet("JEV_SLINT_WATCH_QUIET", quiet), bad=tick["errors"] or (strict and tick["findings"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d findings=%d errors=%d\n"
                % (ticks, tick["findings"], tick["errors"])
            )
            if verdict_path and verdict_ok:
                rc_now = 1 if (tick["errors"] or (strict and tick["findings"])) else 0
                if not _write_verdict(rc_now):
                    verdict_ok = False  # warn once, stop retrying
            if fail_fast and (tick["errors"] or (strict and tick["findings"])):
                break
            if _watch.same_tick(prev_tick, tick):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if unchanged_max and unchanged >= unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            _time.sleep(watch_seconds)
        rc = 1 if (tick.get("errors", 0) or (strict and tick.get("findings", 0))) else 0
        if verdict_path and verdict_ok and not _write_verdict(rc):
            return 1
        return rc
    if do_fix:
        for path in paths:
            if any(f["rule"] == "S005" for f in lint_skill(path)):
                if dry_run:
                    if _fix_name_text(path) is not None:
                        sys.stderr.write("would fix S005 %s\n" % path)
                elif fix_name(path):
                    sys.stderr.write("fixed S005 %s\n" % path)
            if any(f["rule"] == "S008" for f in lint_skill(path)):
                if dry_run:
                    if _fix_case_text(path) is not None:
                        sys.stderr.write("would fix S008 %s\n" % path)
                elif fix_case(path):
                    sys.stderr.write("fixed S008 %s\n" % path)
    if as_json or as_jsonl or out_path or jq_value:
        import json as _json

        all_rows = [
            {"path": str(path), **f}
            for path in paths
            for f in _watch.only_filter(lint_skill(path, usage=usage), only)
        ]
        suppressed = 0
        if baseline_keys is not None:
            kept = [r for r in all_rows if _baseline_key(r) not in baseline_keys]
            suppressed = len(all_rows) - len(kept)
            all_rows = kept
        rows = [r for r in all_rows if not severity or r["severity"] in severity]
        payload = {"findings": rows}
        if suppressed:
            sys.stderr.write(
                "baseline: suppressed %d known finding(s)\n" % suppressed
            )
        if out_path:
            try:
                _atomic_write(Path(out_path),
                    _json.dumps(payload, indent=2) + "\n")
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (out_path, exc))
                return 1
            sys.stderr.write("wrote %d finding(s) to %s\n" % (len(rows), out_path))
        def bad(r: dict) -> bool:
            return r["severity"] == "error" or (strict and r["severity"] == "warn")
        rc_now = 1 if any(bad(r) for r in all_rows) else 0
        if verdict_path:
            if not _watch.write_verdict(
                verdict_path,
                {
                    "verdict": "fail" if rc_now else "pass",
                    "ticks": 1,
                    "findings": len(all_rows),
                    "suppressed": suppressed,
                    "errors": sum(1 for r in all_rows if r["severity"] == "error"),
                    "warnings": sum(1 for r in all_rows if r["severity"] == "warn"),
                    "infos": sum(1 for r in all_rows if r["severity"] == "info"),
                },
            ):
                return 1
        if jq_value:
            value, found = _watch.dig(payload, jq_value)
            if not found:
                sys.stderr.write(
                    "bad --jq key %r (payload has: %s)\n"
                    % (jq_value, ", ".join(sorted(payload)))
                )
                return 2
            sys.stdout.write(_json.dumps(value) + "\n")
            return 0
        if not as_json and not as_jsonl:
            return rc_now
        if as_jsonl:
            for r in rows:
                row = {k: r.get(k) for k in keys} if keys and isinstance(r, dict) else r
                sys.stdout.write(_json.dumps(row, ensure_ascii=False) + "\n")
            return rc_now
        sys.stdout.write(_json.dumps({"findings": rows}, indent=2) + "\n")
        return rc_now
    n_err = 0
    n_warn = 0
    n_suppressed = 0
    md_rows: list[dict] = []
    for path in paths:
        for f in _watch.only_filter(lint_skill(path, usage=usage), only):
            row = {"path": str(path), **f}
            if baseline_keys is not None and _baseline_key(row) in baseline_keys:
                n_suppressed += 1
                continue
            if f["severity"] == "error":
                n_err += 1
            else:
                n_warn += 1
            if f["severity"] == "error" or (strict and f["severity"] == "warn"):
                rc = 1
            if severity and f["severity"] not in severity:
                continue
            if quiet and f["severity"] != "error":
                continue
            if as_md or as_csv:
                md_rows.append(row)
            else:
                sys.stdout.write("%s %s %s: %s\n" % (f["severity"], f["rule"], path, f["message"]))
    if as_md:
        _watch.md_table(md_rows, ["severity", "rule", "path", "message"])
    if as_csv:
        _watch.csv_table(md_rows, keys or ["severity", "rule", "path", "message"])
    if n_suppressed:
        sys.stderr.write(
            "baseline: suppressed %d known finding(s)\n" % n_suppressed
        )
    if not quiet and not as_md and n_err + n_warn and len(paths) > 1:
        sys.stdout.write(
            "%d findings (%d errors, %d warns) in %d files\n"
            % (n_err + n_warn, n_err, n_warn, len(paths))
        )
    if verdict_path:
        all_f = [
            {"path": str(path), **f}
            for path in paths
            for f in _watch.only_filter(lint_skill(path, usage=usage), only)
        ]
        if baseline_keys is not None:
            all_f = [
                r for r in all_f if _baseline_key(r) not in baseline_keys
            ]
        if not _watch.write_verdict(
            verdict_path,
            {
                "verdict": "fail" if rc else "pass",
                "ticks": 1,
                "findings": len(all_f),
                "suppressed": n_suppressed,
                "errors": sum(1 for f in all_f if f["severity"] == "error"),
                "warnings": sum(1 for f in all_f if f["severity"] == "warn"),
                "infos": sum(1 for f in all_f if f["severity"] == "info"),
            },
        ):
            return 1
    return rc


if __name__ == "__main__":
    _watch.exit_safely(main())
