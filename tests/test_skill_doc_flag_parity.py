"""Flags SKILL.md attributes to a script must exist in that script.

SKILL.md is the interface an agent reads; a documented ``--flag`` that the
script does not implement is a silent lie. For each
``skills/jev-consult/scripts/<name>.py`` mention in SKILL.md, every
``--flag`` appearing in the same paragraph/list-item is checked against
the script's source (parse sites and USAGE text both count).
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"

SCRIPT_REF = re.compile(r"scripts/([a-z_]+)\.py")
FLAG = re.compile(r"--[a-z][a-z-]*")


def _script_flags(path: Path) -> set:
    src = path.read_text(encoding="utf-8")
    flags = set(re.findall(r"[\"'](--[a-z][a-z-]*)[\"']", src))
    if "maybe_version" in src or "args.version" in src:
        flags.add("--version")
    return flags


class SkillDocFlagParityTests(unittest.TestCase):
    def test_flags_documented_beside_a_script_exist(self) -> None:
        doc = SKILL_MD.read_text(encoding="utf-8")
        problems = []
        # Each backticked `python .../scripts/X.py <rest>` / `X.py <rest>`
        # chunk documents flags for X; also catch prose like
        # "inventory.py --jq KEY prints ...".
        for match in re.finditer(r"([a-z_]+)\.py([^\n`]*)", doc):
            name, tail = match.group(1), match.group(2)
            # doc comments like "# ... for scaffold --state" name other
            # scripts' flags; flags only count before the '#' comment.
            tail = tail.split("#", 1)[0]
            script = SCRIPTS / (name + ".py")
            if not script.is_file():
                continue
            implemented = _script_flags(script)
            for flag in set(FLAG.findall(tail)):
                if flag not in implemented:
                    problems.append("%s: %s" % (name, flag))
        self.assertEqual(problems, [])

    def test_every_nonvendored_script_is_named_in_skill_md(self) -> None:
        doc = SKILL_MD.read_text(encoding="utf-8")
        undocumented = []
        for script in sorted(SCRIPTS.glob("*.py")):
            if "[vendored]" in script.read_text(encoding="utf-8")[:600]:
                continue
            if script.name not in doc:
                undocumented.append(script.name)
        self.assertEqual(undocumented, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
