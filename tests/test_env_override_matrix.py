"""Flag/env override parity: env var must behave exactly like the flag,
and an explicit flag must win over a conflicting env value."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"

DECISIONS = SCRIPTS / "decisions.py"

ENTRIES = [
    {
        "ts": 1000,
        "harness": "claude",
        "jev_status": "winner",
        "outcome": "human",
        "fill": "peer",
        "winner": {"kind": "skill", "name": "alpha"},
        "prompt_head": "Build auth",
        "need": 0.5,
        "latency_ms": 100,
    },
    {
        "ts": 2000,
        "harness": "hermes",
        "jev_status": "none",
        "outcome": "blocked",
        "fill": "catalog",
        "winner": {"kind": "skill", "name": "beta"},
        "prompt_head": "Fix bug",
        "need": 0.9,
        "latency_ms": 900,
    },
    {
        "ts": 3000,
        "harness": "claude",
        "jev_status": "winner",
        "outcome": "human",
        "fill": "apply",
        "winner": {"kind": "skill", "name": "alpha"},
        "prompt_head": "Build more",
        "need": 0.1,
        "latency_ms": 10,
    },
]

# (flag, env_var, good_value, conflicting_value)
PAIRS = [
    ("--since", "JEV_DECISIONS_SINCE", "1500", "2500"),
    ("--until", "JEV_DECISIONS_UNTIL", "2500", "1500"),
    ("--harness", "JEV_DECISIONS_HARNESS", "claude", "hermes"),
    ("--status", "JEV_DECISIONS_STATUS", "winner", "none"),
    ("--outcome", "JEV_DECISIONS_OUTCOME", "human", "blocked"),
    ("--fill", "JEV_DECISIONS_FILL", "peer", "catalog"),
    ("--prompt", "JEV_DECISIONS_PROMPT", "build", "fix"),
    ("--grep", "JEV_DECISIONS_GREP", "alpha", "beta"),
    ("--field", "JEV_DECISIONS_FIELD", "harness=claude", "harness=hermes"),
    ("--tail", "JEV_DECISIONS_TAIL", "1", "2"),
    ("--first", "JEV_DECISIONS_FIRST", "1", "2"),
    ("--min-need", "JEV_DECISIONS_MIN_NEED", "0.4", "0.95"),
    ("--min-latency", "JEV_DECISIONS_MIN_LATENCY", "50", "950"),
    ("--winner", "JEV_DECISIONS_WINNER", "alpha", "beta"),
    ("--days", "JEV_DECISIONS_DAYS", "99999", "0.00001"),
]


def run_decisions(*argv: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    # strip any ambient JEV_DECISIONS_* so tests are hermetic
    for key in [k for k in full_env if k.startswith("JEV_DECISIONS_")]:
        full_env.pop(key)
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(DECISIONS), *argv],
        capture_output=True,
        text=True,
        env=full_env,
    )


class EnvParityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "decisions.jsonl"
        self.log.write_text(
            "".join(json.dumps(e) + "\n" for e in ENTRIES), encoding="utf-8"
        )

    def test_env_matches_flag(self) -> None:
        for flag, env_var, good, _bad in PAIRS:
            with self.subTest(flag=flag, env=env_var):
                flag_run = run_decisions("--file", str(self.log), flag, good)
                env_run = run_decisions("--file", str(self.log), env={env_var: good})
                self.assertEqual(flag_run.returncode, 0, flag_run.stderr)
                self.assertEqual(env_run.returncode, 0, env_run.stderr)
                self.assertEqual(flag_run.stdout, env_run.stdout)

    def test_flag_beats_env(self) -> None:
        for flag, env_var, good, bad in PAIRS:
            with self.subTest(flag=flag, env=env_var):
                flag_run = run_decisions("--file", str(self.log), flag, good)
                both_run = run_decisions(
                    "--file", str(self.log), flag, good, env={env_var: bad}
                )
                self.assertEqual(flag_run.returncode, 0, flag_run.stderr)
                self.assertEqual(both_run.returncode, 0, both_run.stderr)
                self.assertEqual(flag_run.stdout, both_run.stdout)


if __name__ == "__main__":
    unittest.main()
