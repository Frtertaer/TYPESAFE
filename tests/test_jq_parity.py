from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"
TRIGGER_CASES = ROOT / "tests" / "fixtures" / "jev-consult.trigger-cases.json"



def _argv(name: str, tmp: str) -> list[str]:
    """An offline invocation that reaches each script's --jq payload check."""
    base = [sys.executable, str(SCRIPTS_DIR / name)]
    request_path = Path(tmp) / "request.json"
    if not request_path.exists():
        with open(request_path, "w", encoding="utf-8") as fh:
            fh.write(
                '{"questions":{"q":{"type":"choice","criteria":["a","none"],"prompt":"pick one"}}}'
            )
    extra = {
        "inventory.py": ["--task", "x", "--home", tmp],
        "peer_fill.py": ["--status", "--cwd", tmp],
        "apply_fill.py": ["--status", "--cwd", tmp],
        "catalog_fill.py": ["--status", "--cwd", tmp],
        "compact.py": ["--fake", "--dir", tmp],
        "smoke.py": ["--only", "self_test"],
        "trace.py": ["stats", "--file", str(Path(tmp) / "missing.json")],
        "trigger_eval.py": ["--json"],
        "skill_lint.py": [str(SKILL_MD)],
        "question_lint.py": [str(request_path)],
        "trigger_lint.py": [str(TRIGGER_CASES)],
        "jev.py": ["decide", "-"],
    }.get(name, [])
    return base + extra + ["--jq", "nope"]


# Scripts with --jq that dig a payload object; unknown key must exit 2.
# decisions.py is excluded: its --jq is per-entry field extraction
# (values list), not a payload dig, so unknown keys legitimately print [].
PAYLOAD_JQ = [
    "inventory.py",
    "compare.py",
    "doctor.py",
    "policy_lint.py",
    "skill_lint.py",
    "question_lint.py",
    "trigger_lint.py",
    "trigger_eval.py",
    "peer_fill.py",
    "apply_fill.py",
    "catalog_fill.py",
    "compact.py",
    "smoke.py",
    "trace.py",
    "jev.py",
    "inventory_hook.py",
]


class JqParityTests(unittest.TestCase):
    def test_unknown_jq_key_exits_2(self) -> None:
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        env.pop("TYPESAFE_API_KEY", None)
        with tempfile.TemporaryDirectory() as tmp:
            for name in PAYLOAD_JQ:
                with self.subTest(script=name):
                    proc = subprocess.run(
                        _argv(name, tmp),
                        input="{}",
                        capture_output=True,
                        text=True,
                        timeout=90,
                        env=env,
                    )
                    self.assertEqual(
                        proc.returncode,
                        2,
                        "%s: rc=%d out=%s" % (name, proc.returncode, proc.stdout + proc.stderr),
                    )

    def test_known_jq_key_exits_0(self) -> None:
        env = dict(os.environ)
        env["JEV_CONSULT_LOG"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "policy_lint.py"), "--jq", "errors"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertTrue(proc.stdout.strip().isdigit())

    def test_jq_lookup_implementations_agree_on_lists(self) -> None:
        # jev/smoke/trace/trigger_eval keep a jq_lookup shim; it must behave
        # exactly like _watch.dig — numeric parts index lists, misses are
        # (None, False).
        import importlib.util
        import sys as _sys

        def _load(name):
            spec = importlib.util.spec_from_file_location(
                "jqmod_" + name.replace(".", "_"), SCRIPTS_DIR / name
            )
            mod = importlib.util.module_from_spec(spec)
            _sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod

        watch = _load("_watch.py")
        payload = {
            "rows": [{"a": 1}, {"a": 2}],
            "top": {"n": 5},
            "a.b": {"c": 9},
            "x": {"y.z": 7},
            "flat.key": "v",
        }
        cases = [
            ("rows.0.a", 1, True), ("rows.1.a", 2, True), ("top.n", 5, True),
            ("rows.9.a", None, False), ("rows.a", None, False), ("nope", None, False),
            # keys containing dots resolve as longest literal after segments
            ("flat.key", "v", True), ("a.b.c", 9, True), ("x.y.z", 7, True),
            ("a.b.nope", None, False),
        ]
        for script in ("jev.py", "smoke.py", "trace.py", "trigger_eval.py",
                       "skill_scanner.py"):
            mod = _load(script)
            # jq_lookup is the pack shim; skill_scanner's standalone mirror
            # is named _dig — same (value, found) contract either way.
            dig = getattr(mod, "jq_lookup", None) or getattr(mod, "_dig")
            with self.subTest(script=script):
                for path, want, found in cases:
                    self.assertEqual(dig(payload, path), (want, found), path)
                    self.assertEqual(watch.dig(payload, path), (want, found), path)

    def test_no_inline_dotted_dig_loops(self) -> None:
        # Every --jq dig must go through _watch.dig (or jq_lookup, which
        # delegates to it). A raw `for part in X.split(".")` descent is the
        # drift this pack already hit: dict-only copies that silently could
        # not index lists or resolve dot-containing keys.
        import re

        allowed = {"_watch.py", "jev.py"}  # _watch.dig itself + jev fallback
        loop_re = re.compile(r'for part in .+\.split\("\."\)')
        offenders = []
        for path in sorted(SCRIPTS_DIR.glob("*.py")):
            if path.name in allowed:
                continue
            hits = loop_re.findall(path.read_text(encoding="utf-8"))
            if hits:
                offenders.append("%s: %s" % (path.name, hits[0].strip()))
        # install.py lives outside the pack and cannot import _watch; it
        # carries an identical longest-literal loop instead.
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
