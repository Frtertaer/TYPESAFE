"""Every flag a script's USAGE string claims must be implemented.

Scans hand-rolled argv parsers (USAGE = '...' literals) and checks each
`--flag` named in the usage text also appears quoted elsewhere in the
source (parse site). `--version` counts as implemented when the script
delegates to _watch.maybe_version.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def _usage_text(src: str) -> str:
    match = re.search(r"USAGE\s*=\s*'(.*?)'\n", src, re.S)
    return match.group(1) if match else ""


class UsageFlagParityTests(unittest.TestCase):
    def test_every_usage_flag_is_implemented(self) -> None:
        problems = []
        for path in sorted(SCRIPTS.glob("*.py")):
            src = path.read_text(encoding="utf-8")
            usage = _usage_text(src)
            if not usage:
                continue
            body = src.replace(usage, "")
            claimed = set(re.findall(r"--[a-z-]+", usage))
            implemented = set(re.findall(r"[\"'](--[a-z-]+)[\"']", body))
            if "maybe_version" in body:
                implemented.add("--version")
            missing = claimed - implemented
            if missing:
                problems.append("%s: %s" % (path.name, sorted(missing)))
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
