"""Doc-listed `--flag` tokens must exist in the referenced script's source.

Every docs line shaped like `python <script>.py --flag ...` promises a flag
the script accepts. If a flag is renamed or dropped the docs go stale
silently — this test greps each script's source for each doc'd flag.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "CLAUDE.md",
    ROOT / ".hermes.md",
    ROOT / "docs" / "for-agents.md",
    ROOT / "skills" / "jev-consult" / "SKILL.md",
]

# `python <path/to/script.py> [--flag ...]` — only .py invocations.
CMD = re.compile(r"python(?:3)?\s+((?:[\w./-]+)\.py)\b")
FLAG = re.compile(r"(?<!\w)--([a-zA-Z][\w-]*)")
# ignore values glued on like `--flag=<x>` placeholders after the flag itself


class DocFlagParityTests(unittest.TestCase):
    def _lines(self):
        for doc in DOCS:
            if not doc.exists():
                continue
            base = doc.parent
            for ln, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
                yield doc, base, ln, line

    def test_doc_flags_exist_in_script_source(self) -> None:
        # a prose line may name several scripts (e.g. lint tools are
        # described together); each flag must exist in at least one of
        # the scripts referenced on that line.
        missing = []
        seen = set()
        srcs = {}
        for doc, base, ln, line in self._lines():
            line = line.split("#", 1)[0]  # trailing comments describe other tools
            scripts = []
            for m in CMD.finditer(line):
                script = (base / m.group(1)).resolve()
                if not script.exists():
                    script = (ROOT / m.group(1)).resolve()
                if not script.exists() or script.suffix != ".py":
                    continue  # path existence itself is test_doc_paths' job
                if script not in srcs:
                    try:
                        srcs[script] = script.read_text(encoding="utf-8")
                    except OSError:
                        srcs[script] = ""
                scripts.append(script)
            if not scripts:
                continue
            for flag in FLAG.findall(line):
                token = "--" + flag
                key = (doc.name, ln, token)
                if key in seen:
                    continue
                seen.add(key)
                if not any(token in srcs[s] for s in scripts):
                    missing.append(
                        "%s:%d: documents %s not handled by %s"
                        % (doc.name, ln, token, ", ".join(s.name for s in scripts))
                    )
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
