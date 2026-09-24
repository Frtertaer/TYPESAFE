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
                    self.assertTrue(
                        "os.replace" in body or "atomic_replace(" in body,
                        "helper %s in %s must rename via os.replace/"
                        "_watch.atomic_replace" % (m.group(1), path.name),
                    )

    def test_tmp_file_is_sibling_of_target(self) -> None:
        """Atomic helpers must create the tmp file in the target's own
        directory — os.replace across filesystems is not atomic (and on
        some platforms raises). Pin: tmp derives via ``with_name(`` /
        ``dir=str(<target>.parent)``."""
        helper_re = re.compile(
            r"def (_atomic_write\w*|atomic_write_text|write_verdict"
            r"|_write_anchor|_atomic_write_surrogate)\("
        )
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            for m in helper_re.finditer(src):
                body_start = m.end()
                nxt = src.find("\ndef ", body_start)
                nxt_m = src.find("\n    def ", body_start)
                stops = [s for s in (nxt, nxt_m) if s > 0]
                body = src[body_start : min(stops) if stops else len(src)]
                if "replace" not in body:
                    continue
                with self.subTest(file=path.name, helper=m.group(1)):
                    ok = (
                        ".with_name(" in body
                        or "dir=str(" in body
                        or "dir=" in body and "parent" in body
                    )
                    if not ok:
                        offenders.append(
                            "%s:%s tmp not sibling-derived" % (path.name, m.group(1))
                        )
        self.assertEqual(offenders, [])

    def test_no_bare_os_replace_in_scripts(self) -> None:
        """Every rename goes through _watch.atomic_replace (PermissionError
        retry on Windows); only _watch.atomic_replace itself may call
        os.replace."""
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name == "_watch.py":
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if re.search(r"(?<![\w.])os\.replace\(", line):
                    offenders.append("%s:%d %s" % (path.name, lineno, line.strip()))
        self.assertEqual(
            offenders, [], "bare os.replace calls: %s" % offenders
        )

    def test_no_bare_open_write_outside_helpers_and_self_tests(self) -> None:
        """open(..., "w") is banned outside atomic helpers and self-test/
        tempfile fixture blocks — same rule as bare .write_text but for
        the open() form. Known fixture sites are pinned by stripped line
        text so the allowlist only shrinks."""
        open_w = re.compile(r'\bopen\([^)]*["\']w["\']')
        known_fixture_sites = {
            "decisions.py": [
                'with open(log, "w", encoding="utf-8") as fh:',
            ],
            "skill_lint.py": [
                'with open(bad, "w", encoding="utf-8") as fh:',
            ],
            "trigger_lint.py": [
                'with open(p, "w", encoding="utf-8") as fh:',
            ],
            "skill_scanner.py": [
                'with open(skill / "SKILL.md", "w", encoding="utf-8") as fh:',
            ],
        }
        seen: dict[str, set[str]] = {}
        offenders = []
        for path in sorted(SCRIPTS.glob("*.py")):
            if path.name in ALLOW_BARE or path.name == "_watch.py":
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if not open_w.search(line):
                    continue
                stripped = line.strip()
                if stripped in known_fixture_sites.get(path.name, []):
                    seen.setdefault(path.name, set()).add(stripped)
                    continue
                # os.fdopen inside mkstemp-based atomic writers is fine
                if "os.fdopen" in stripped or "mkstemp" in stripped:
                    continue
                offenders.append("%s:%d %s" % (path.name, lineno, stripped))
        self.assertEqual(
            offenders, [], "bare open(w) output writes: %s" % offenders
        )
        unused = {
            name: sorted(set(sites) - seen.get(name, set()))
            for name, sites in known_fixture_sites.items()
        }
        self.assertEqual(
            {k: v for k, v in unused.items() if v},
            {},
            "allowlisted fixture writes no longer present — shrink the set",
        )


if __name__ == "__main__":
    unittest.main()
