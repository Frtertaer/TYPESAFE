"""Doc-to-parser parity: every --flag shown in docs next to a script
must be a real option that script's --help lists (or that its source
handles for manual-argv scripts)."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
DOCS = [
    ROOT / "skills" / "jev-consult" / "SKILL.md",
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / ".hermes.md",
    ROOT / "docs" / "for-agents.md",
    ROOT / "skills" / "jev-consult" / "examples" / "README.md",
    ROOT / "skills" / "jev-consult" / "references" / "harnesses.md",
]

SCRIPT_RE = re.compile(r"\b([a-z][a-z0-9_/-]*)\.py\b")
FLAG_RE = re.compile(r"--[a-z][a-z0-9-]+")

# Flags the doc deliberately names but the script must NOT accept (policy
# prohibitions like "Never --force") or attributes to a different tool
# (hermes install --yes) — excluded from the parity check.
EXCLUDE = {
    "apply_fill": {"--force", "--enable", "--no-enable"},
    "catalog_fill": {"--severity", "--yes", "--force", "--no-enable"},
}


def doc_flags() -> dict:
    """{script_name: {flags}} collected line-by-line from every doc."""
    table: dict = {}
    for doc in DOCS:
        if not doc.exists():
            continue
        for line in doc.read_text(encoding="utf-8").splitlines():
            marks = [m for m in SCRIPT_RE.finditer(line)]
            if not marks:
                continue
            # split the line into spans: flags between script[i] and
            # script[i+1] belong to script[i]; a tail after the last
            # belongs to the last.
            spans = []
            for i, mark in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(line)
                spans.append((mark.group(1), line[mark.start() : end]))
            head = line[: marks[0].start()]
            for name, span in spans:
                seg = head + span if name == marks[0].group(1) else span
                flags = set(FLAG_RE.findall(seg))
                name = name.rsplit("/", 1)[-1]
                table.setdefault(name, set()).update(flags)
    return table


def script_path(name: str) -> Path | None:
    for cand in (SCRIPTS / (name + ".py"), ROOT / "scripts" / (name + ".py")):
        if cand.is_file():
            return cand
    return None


class DocFlagsTest(unittest.TestCase):
    def test_every_documented_flag_parses(self) -> None:
        table = doc_flags()
        self.assertTrue(table, "no script lines found in docs")
        missing = []
        for name, flags in sorted(table.items()):
            script = script_path(name)
            if script is None:
                continue  # e.g. run_trigger_evals.py lives in vendor
            proc = subprocess.run(
                [sys.executable, str(script), "--help"],
                capture_output=True,
                text=True,
            )
            help_text = proc.stdout + proc.stderr
            if "usage:" in help_text or "Usage:" in help_text:
                subs = re.search(r"\{([a-z0-9_,-]+)\}", help_text)
                if subs:
                    for sub in subs.group(1).split(","):
                        sub = sub.strip()
                        if not sub or sub.startswith("-"):
                            continue
                        p2 = subprocess.run(
                            [sys.executable, str(script), sub, "--help"],
                            capture_output=True,
                            text=True,
                        )
                        help_text += "\n" + p2.stdout + p2.stderr
            else:
                # manual-argv scripts (hooks): the source itself must
                # mention the flag string
                try:
                    help_text = script.read_text(encoding="utf-8")
                except OSError:
                    continue
            flags -= EXCLUDE.get(name, set())
            for flag in sorted(flags):
                if flag not in help_text:
                    missing.append("%s %s" % (name, flag))
        self.assertEqual(
            missing, [], "doc flags missing from parser/source: %s" % missing
        )


if __name__ == "__main__":
    unittest.main()
