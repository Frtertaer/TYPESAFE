"""Every backtick-quoted repo-relative path in the docs must exist.

Root docs resolve against the repo root; SKILL.md resolves against the
skill directory (its `scripts/x.py` means skills/jev-consult/scripts/x.py).
Home-rooted (~/) and bare filenames are out of scope.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOCS = {
    "README.md": ROOT,
    "AGENTS.md": ROOT,
    "CLAUDE.md": ROOT,
    ".hermes.md": ROOT,
    "docs/for-agents.md": ROOT,
    "skills/jev-consult/SKILL.md": ROOT / "skills" / "jev-consult",
}

PATH_RE = re.compile(
    r"`((?:scripts|skills|tests|docs|vendor|references)/[A-Za-z0-9_./-]+)`"
)


class DocPathTests(unittest.TestCase):
    def test_quoted_paths_exist(self) -> None:
        found = 0
        missing: list[str] = []
        for doc, base in DOCS.items():
            text = (ROOT / doc).read_text(encoding="utf-8")
            for ref in PATH_RE.findall(text):
                found += 1
                rel = ref.rstrip("/").rstrip(".,;:")
                # docs may quote paths relative to the doc's dir OR the repo root
                if not (base / rel).exists() and not (ROOT / rel).exists():
                    missing.append("%s -> %s" % (doc, rel))
        self.assertGreaterEqual(found, 20, "ref regex stopped matching")
        self.assertEqual(missing, [], "stale doc paths: %s" % missing)


if __name__ == "__main__":
    unittest.main()
