"""JEV_<PREFIX>_WATCH_MAX caps every watch loop at N ticks.

The env cap is the operator knob for bounding a watch; a script that
ignores it would loop forever in an unattended harness. Each probe runs
`--watch 0.05` with the cap set to 1 and counts single-line JSON ticks
(lines that parse to an object containing 'ts').
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"
SKILL_MD = ROOT / "skills" / "jev-consult" / "SKILL.md"
EXAMPLE_REQ = ROOT / "skills" / "jev-consult" / "examples" / "jwt-auth.request.json"

# env name -> (script, argv with the watch interval appended)
WATCH_CAP = {
    "JEV_COMPARE_WATCH_MAX": ("compare.py", ["--watch", "0.05"]),
    "JEV_DECISIONS_WATCH_MAX": ("decisions.py", ["--watch", "0.05"]),
    "JEV_DOCTOR_WATCH_MAX": ("doctor.py", ["--watch", "0.05"]),
    "JEV_INV_WATCH_MAX": ("inventory.py", ["--watch", "0.05"]),
    "JEV_PLINT_WATCH_MAX": ("policy_lint.py", ["--watch", "0.05"]),
    "JEV_QLINT_WATCH_MAX": (
        "question_lint.py",
        [str(EXAMPLE_REQ), "--watch", "0.05"],
    ),
    "JEV_SLINT_WATCH_MAX": ("skill_lint.py", [str(SKILL_MD), "--watch", "0.05"]),
    "JEV_TLINT_WATCH_MAX": ("trigger_lint.py", ["--watch", "0.05"]),
    "JEV_TRIGGER_WATCH_MAX": ("trigger_eval.py", ["--watch", "0.05"]),
}


def _tick_lines(stdout: str) -> int:
    ticks = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "ts" in obj:
            ticks += 1
    return ticks


class WatchCapTests(unittest.TestCase):
    def _run_watch(
        self, env_name: str, name: str, argv: list[str], cwd: str | None = None
    ) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env[env_name] = "1"
        return subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / name), *argv],
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )

    def test_env_watch_max_caps_ticks(self) -> None:
        for env_name, (name, argv) in sorted(WATCH_CAP.items()):
            with self.subTest(script=name):
                proc = self._run_watch(env_name, name, argv)
                ticks = _tick_lines(proc.stdout)
                self.assertEqual(
                    ticks,
                    1,
                    "%s emitted %d ticks under %s=1: %s"
                    % (name, ticks, env_name, proc.stdout[:300]),
                )
                self.assertNotIn("Traceback", proc.stderr, name)

    def test_trace_state_watch_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_file = Path(tmp) / "t.trace.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS_DIR / "trace.py"),
                    "--file",
                    str(trace_file),
                    "init",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            proc = self._run_watch(
                "JEV_TRACE_WATCH_MAX",
                "trace.py",
                ["--file", str(trace_file), "state", "--watch", "0.05"],
            )
            self.assertEqual(
                _tick_lines(proc.stdout), 1, "trace state watch: %s" % proc.stdout[:300]
            )

    def test_progress_status_watch_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_watch(
                "JEV_PROGRESS_WATCH_MAX",
                "progress.py",
                ["--repo", tmp, "status", "build", "--watch", "0.05"],
            )
            self.assertEqual(
                _tick_lines(proc.stdout),
                1,
                "progress status watch: %s" % proc.stdout[:300],
            )


if __name__ == "__main__":
    unittest.main()
