"""Confinement: a hook run may only create its declared files.

In a sandbox cwd with JEV_CONSULT_LOG redirected inside the sandbox, the
hook may touch only .jev-tools.json, .jev-tools-miss.json and the log —
nothing else inside the sandbox, nothing outside it.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"
HOOK = SCRIPTS / "inventory_hook.py"

ALLOWED = {".jev-tools.json", ".jev-tools-miss.json", "decisions.jsonl"}


def run_hook(cwd: Path, log: Path, payload: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("TYPESAFE_API_KEY", None)
    env["JEV_CONSULT_LOG"] = str(log)
    env["JEV_HOOK_CWD"] = str(cwd)
    env["JEV_HOOK_TIMEOUT"] = "0.01"  # Jev unreachable — fail-open fast
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=30,
    )


class HookConfinementTests(unittest.TestCase):
    def _snapshot(self, root: Path) -> set[Path]:
        return {p.relative_to(root) for p in root.rglob("*") if p.is_file()}

    def test_hook_writes_only_declared_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            log = cwd / "decisions.jsonl"
            before = self._snapshot(cwd)
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "jwt auth",
                "cwd": str(cwd),
            }
            proc = run_hook(cwd, log, payload)
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            created = self._snapshot(cwd) - before
            self.assertLessEqual(
                {p.name for p in created},
                ALLOWED,
                "hook wrote undeclared files: %s" % created,
            )
            # anything created lives directly under the sandbox (no dirs)
            for p in created:
                self.assertEqual(len(p.parts), 1, "unexpected nested path: %s" % p)

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            env = dict(os.environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = str(cwd / "decisions.jsonl")
            env["JEV_HOOK_CWD"] = str(cwd)
            proc = subprocess.run(
                [sys.executable, str(HOOK), "--dry-run"],
                input=json.dumps(
                    {"hook_event_name": "UserPromptSubmit", "prompt": "jwt", "cwd": str(cwd)}
                ),
                capture_output=True,
                text=True,
                cwd=str(cwd),
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            leftovers = [p.name for p in cwd.iterdir() if p.name != "decisions.jsonl"]
            self.assertEqual(
                leftovers, [], "--dry-run leaked files: %s" % leftovers
            )

    def test_log_disabled_writes_no_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            env = dict(os.environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["JEV_HOOK_CWD"] = str(cwd)
            proc = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(
                    {"hook_event_name": "UserPromptSubmit", "prompt": "jwt", "cwd": str(cwd)}
                ),
                capture_output=True,
                text=True,
                cwd=str(cwd),
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            self.assertFalse((cwd / "decisions.jsonl").exists())



class EncodingConfinementTests(unittest.TestCase):
    def test_non_ascii_prompt_on_cp1252_stdio(self) -> None:
        """Legacy Windows consoles (cp1252) must not crash the hook —
        it still emits a parseable payload and rc 0."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            env = dict(os.environ)
            env.pop("TYPESAFE_API_KEY", None)
            env.update(
                {
                    "PYTHONIOENCODING": "cp1252",
                    "USERPROFILE": tmp,
                    "HOME": tmp,
                    "HERMES_HOME": str(cwd / ".hermes"),
                    "JEV_HOOK_CWD": tmp,
                    "JEV_CONSULT_LOG": "0",
                }
            )
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "сжатие журналов — кириллица",
                "cwd": str(cwd),
            }
            proc = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(payload).encode("utf-8"),
                capture_output=True,
                env=env,
                cwd=str(cwd),
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[:300])
            self.assertNotIn(
                "Traceback", proc.stderr.decode("utf-8", "replace")
            )
            json.loads(proc.stdout.decode("utf-8", "replace"))

    def test_non_ascii_sidecar_roundtrips_utf8(self) -> None:
        """A pick whose item has non-ascii text lands in .jev-tools.json
        as valid utf-8 JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            skill = home / ".claude" / "skills" / "rus-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: rus-skill\ndescription: сжатие и поиск журналов\n---\nbody\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            env.pop("TYPESAFE_API_KEY", None)
            env.update(
                {
                    "USERPROFILE": tmp,
                    "HOME": tmp,
                    "HERMES_HOME": str(home / ".hermes"),
                    "JEV_HOOK_CWD": tmp,
                    "JEV_CONSULT_LOG": "0",
                }
            )
            proc = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "rus-skill",
                        "cwd": str(home),
                    }
                ).encode("utf-8"),
                capture_output=True,
                env=env,
                cwd=str(home),
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace")[:300])
            for name in (".jev-tools.json", ".jev-tools-miss.json"):
                sidecar = home / name
                if sidecar.exists():
                    json.loads(sidecar.read_text(encoding="utf-8"))


class ConcurrentHookTests(unittest.TestCase):
    def test_racing_hooks_leave_parseable_sidecars(self) -> None:
        """Two+ hooks writing the same cwd must not interleave bytes —
        whatever lands parses, and no .tmp litter remains."""
        import concurrent.futures

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "jwt auth",
                "cwd": str(cwd),
            }

            def run_one(i: int) -> int:
                return run_hook(cwd, cwd / "decisions.jsonl", payload).returncode

            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                rcs = list(pool.map(run_one, range(6)))
            self.assertEqual(rcs, [0] * 6)
            for f in cwd.iterdir():
                self.assertNotIn(
                    ".tmp", f.name, "atomic write littered: %s" % f.name
                )
                if f.name.endswith(".json") or f.name.endswith(".jsonl"):
                    if f.name == "decisions.jsonl":
                        for line in f.read_text(encoding="utf-8").splitlines():
                            if line.strip():
                                json.loads(line)
                    else:
                        json.loads(f.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
