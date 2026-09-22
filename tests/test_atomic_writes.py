"""Atomic-write guard: every payload/output write in scripts/ goes through
an _atomic_write*/write_verdict helper (tmp file + os.replace), so a crash
mid-write can never leave a truncated JSON on disk.

Bare ``.write_text(`` is only allowed inside the atomic helpers themselves
(their tmp-file write) and in smoke.py's fixture builders (which create
test transcripts, not user-visible outputs).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

# smoke.py fixture builders legitimately write plain files (transcripts,
# SKILL.md stubs, request JSON) as inputs for the steps it runs.
ALLOW_BARE = {"smoke.py"}

WRITE_RE = re.compile(r"\.write_text\(")


def bare_write_sites(path: Path) -> list:
    """[(lineno, line)] of .write_text calls outside the atomic helpers."""
    hits = []
    in_helper = 0
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("def _atomic_write"):
            in_helper = lineno
        elif in_helper and line and not line.startswith((" ", "\t")) and stripped.startswith("def "):
            in_helper = 0
        if in_helper:
            continue
        if WRITE_RE.search(line):
            hits.append((lineno, stripped))
    return hits


class AtomicWriteGuardTest(unittest.TestCase):
    def test_no_bare_write_text_in_scripts(self) -> None:
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in ALLOW_BARE or path.name == "_watch.py":
                continue
            for lineno, line in bare_write_sites(path):
                offenders.append("%s:%d %s" % (path.name, lineno, line))
        self.assertEqual(
            offenders, [], "bare .write_text output writes: %s" % offenders
        )

    def test_every_script_with_write_has_helper(self) -> None:
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name == "_watch.py":
                continue
            src = path.read_text(encoding="utf-8")
            if "_atomic_write(" in src and "def _atomic_write" not in src:
                # must import it instead (inventory._atomic_write_text)
                self.assertIn(
                    "_atomic_write_text",
                    src,
                    "%s calls _atomic_write without defining or importing it"
                    % path.name,
                )

    def test_helpers_use_tmp_and_replace(self) -> None:
        for path in sorted(SCRIPTS.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r"def (_atomic_write\w*)\(", src):
                body_start = m.end()
                nxt = src.find("\ndef ", body_start)
                body = src[body_start : nxt if nxt > 0 else len(src)]
                with self.subTest(file=path.name, helper=m.group(1)):
                    self.assertIn(".tmp", body)
                    self.assertIn("os.replace", body)


if __name__ == "__main__":
    unittest.main()
