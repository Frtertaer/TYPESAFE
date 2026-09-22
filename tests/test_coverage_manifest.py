"""Every scripts/*.py must be referenced by at least one tests/*.py."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
TESTS = ROOT / "tests"


class CoverageManifestTests(unittest.TestCase):
    def test_every_script_referenced_by_a_test(self) -> None:
        test_blobs = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(TESTS.glob("test_*.py"))
        )
        missing = [
            p.name
            for p in sorted(SCRIPTS.glob("*.py"))
            if p.stem not in test_blobs
        ]
        self.assertEqual(missing, [])

    def test_every_script_exists_on_disk(self) -> None:
        names = [p.name for p in sorted(SCRIPTS.glob("*.py"))]
        self.assertGreater(len(names), 10)
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
