"""JEV_POLICY override: documented as the policy every script loads.

Until these pins existed only jev.py honored it; inventory's _policy_dict
(hook thresholds, catalogs, stop-words, sidecar TTL), trigger_lint's
default policy, progress's init/lint policy and doctor's policy check all
read the bundled file regardless.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "skills" / "jev-consult" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import inventory  # noqa: E402
import peer_fill  # noqa: E402
import catalog_fill  # noqa: E402
import apply_fill  # noqa: E402

POLICY_JSON = ROOT / "skills" / "jev-consult" / "policy.json"


def _run(name: str, argv: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / name), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def _custom_policy(tmp: str, **extra) -> Path:
    base = json.loads(POLICY_JSON.read_text(encoding="utf-8"))
    base.update(extra)
    path = Path(tmp) / "custom-policy.json"
    path.write_text(json.dumps(base, indent=2), encoding="utf-8")
    return path


def _env_with(path: Path) -> dict:
    env = dict(os.environ)
    env["JEV_POLICY"] = str(path)
    return env


class InventoryPolicyEnvTests(unittest.TestCase):
    def test_catalogs_honor_env_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(
                tmp,
                catalogs=[{"name": "internal", "url": "https://example.invalid"}],
            )
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                self.assertEqual(
                    inventory.catalogs(), (("internal", "https://example.invalid"),)
                )
            self.assertNotEqual(
                inventory.catalogs(), (("internal", "https://example.invalid"),)
            )

    def test_ttl_honors_env_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, sidecar_ttl_seconds=4242)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                # JEV_HOOK_TTL env would still win; clear it for the probe.
                env = dict(os.environ)
                env.pop("JEV_HOOK_TTL", None)
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(inventory.sidecar_ttl_seconds(), 4242.0)
            self.assertNotEqual(inventory.sidecar_ttl_seconds(), 4242.0)

    def test_unreadable_env_policy_falls_back_to_defaults(self) -> None:
        """An explicit-but-unreadable JEV_POLICY yields {} -> built-in
        defaults, never the bundled file's values silently."""
        with patch.dict(
            os.environ, {"JEV_POLICY": str(Path("nope") / "missing.json")}
        ):
            self.assertEqual(inventory._policy_dict(), {})


class TriggerLintPolicyEnvTests(unittest.TestCase):
    def test_env_policy_used_when_no_flag(self) -> None:
        """A cases file covering a made-up must_ask kind fails under the
        bundled policy but passes under a custom env policy listing it."""
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, must_ask=["special_kind"])
            cases = Path(tmp) / "cases.json"
            cases.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "pos-one",
                                "prompt": "decide the special thing now please",
                                "should_trigger": True,
                                "covers": ["special_kind"],
                            },
                            {
                                "id": "neg-one",
                                "prompt": "just write the code",
                                "should_trigger": False,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = _env_with(custom)
            proc = _run("trigger_lint.py", [str(cases), "--json"], env)
            payload = json.loads(proc.stdout)
            bad = [
                f for f in payload.get("findings", []) if f.get("rule") == "T008"
            ]
            self.assertEqual(bad, [], "env policy must_ask not honored")

    def test_flag_beats_env_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, must_ask=["special_kind"])
            cases = Path(tmp) / "cases.json"
            cases.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "id": "pos-one",
                                "prompt": "decide the special thing now please",
                                "should_trigger": True,
                                "covers": ["special_kind"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            env = _env_with(custom)
            proc = _run(
                "trigger_lint.py",
                [str(cases), "--policy", str(POLICY_JSON), "--json"],
                env,
            )
            payload = json.loads(proc.stdout)
            rules = {f.get("rule") for f in payload.get("findings", [])}
            self.assertIn("T008", rules, "--policy must win over JEV_POLICY")


class ProgressPolicyEnvTests(unittest.TestCase):
    def test_lint_env_policy_rejects_foreign_plan_key(self) -> None:
        """progress lint under a custom env policy must use ITS required
        point levels — a plan valid under stock but wrong under custom
        fails only when JEV_POLICY is set."""
        with tempfile.TemporaryDirectory() as tmp:
            custom_doc = json.loads(POLICY_JSON.read_text(encoding="utf-8"))
            custom_doc["progress"] = dict(
                custom_doc["progress"], points={"zero": 0, "tiny": 1}
            )
            custom = Path(tmp) / "custom-policy.json"
            custom.write_text(json.dumps(custom_doc, indent=2), encoding="utf-8")
            env = _env_with(custom)
            plan = Path(tmp) / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "id": "p1",
                        "goal": "g",
                        "checks": {"smoke": ["{python}", "-c", "pass"]},
                        "required_checks": ["smoke"],
                        "items": [
                            {
                                "id": "i1",
                                "description": "d",
                                "checks": ["smoke"],
                                "paths": ["x.py"],
                                "level": "small",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            proc = _run(
                "progress.py",
                ["lint", str(plan)],
                env,
            )
            out = proc.stdout
            self.assertEqual(proc.returncode, 1, out)
            self.assertIn("invalid", out)


class DoctorPolicyEnvTests(unittest.TestCase):
    def test_policy_check_reads_env_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent-policy.json"
            env = _env_with(missing)
            home = Path(tmp) / "home"
            home.mkdir()
            proc = _run(
                "doctor.py",
                ["--json", "--home", str(home), "--only", "policy"],
                env,
            )
            payload = json.loads(proc.stdout)
            checks = {c["check"]: c for c in payload["checks"]}
            self.assertFalse(checks["policy"]["ok"], proc.stdout)
            custom = _custom_policy(tmp)
            proc2 = _run(
                "doctor.py",
                ["--json", "--home", str(home), "--only", "policy"],
                _env_with(custom),
            )
            payload2 = json.loads(proc2.stdout)
            checks2 = {c["check"]: c for c in payload2["checks"]}
            self.assertTrue(checks2["policy"]["ok"], proc2.stdout)


class ThresholdPolicyEnvTests(unittest.TestCase):
    """Every script knob is policy-backed: JEV_POLICY path feeds it, env wins
    where an override exists."""

    def test_fill_timeout_honors_policy_and_env_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, fill_timeout_seconds=33)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                env = dict(os.environ)
                env.pop("JEV_FILL_TIMEOUT", None)
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(peer_fill.fill_timeout_seconds(), 33.0)
                with patch.dict(
                    os.environ, {"JEV_FILL_TIMEOUT": "12"}, clear=False
                ):
                    self.assertEqual(peer_fill.fill_timeout_seconds(), 12.0)

    def test_scan_cache_seconds_honors_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, scan_cache_seconds=7)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                self.assertEqual(inventory.scan_cache_seconds(), 7.0)
            self.assertNotEqual(inventory.scan_cache_seconds(), 7.0)

    def test_catalog_search_limit_honors_policy_in_both_fills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, catalog_search_limit=3)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                self.assertEqual(catalog_fill.catalog_search_limit(), 3)
                self.assertEqual(apply_fill.catalog_search_limit(), 3)
            self.assertEqual(catalog_fill.catalog_search_limit(), 8)
            self.assertEqual(apply_fill.catalog_search_limit(), 8)

    def test_catalog_cache_max_queries_honors_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, catalog_cache_max_queries=5)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                self.assertEqual(catalog_fill.cache_max_queries(), 5)
            self.assertEqual(catalog_fill.cache_max_queries(), 50)

    def test_env_file_max_bytes_honors_policy(self) -> None:
        import doctor
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, env_file_max_bytes=64)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                self.assertEqual(doctor._env_file_max_bytes(), 64)
            self.assertEqual(doctor._env_file_max_bytes(), 65536)

    def test_sidecar_task_max_chars_honors_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, sidecar_task_max_chars=10)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                self.assertEqual(inventory.sidecar_task_max_chars(), 10)
            self.assertEqual(inventory.sidecar_task_max_chars(), 500)

    def test_write_sidecar_truncates_task_to_policy_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = _custom_policy(tmp, sidecar_task_max_chars=7)
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                sidecar = Path(tmp) / ".jev-tools.json"
                inventory.write_sidecar(
                    sidecar, "claude-code", "x" * 20,
                    [{"id": "k:n", "kind": "skill", "name": "n"}],
                )
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                self.assertEqual(data["task"], "x" * 7)

    def test_shipped_policy_defines_all_threshold_keys(self) -> None:
        policy = json.loads(POLICY_JSON.read_text(encoding="utf-8"))
        for key in (
            "fill_timeout_seconds",
            "scan_cache_seconds",
            "catalog_search_limit",
            "catalog_cache_max_queries",
            "hermes_install_timeout_seconds",
            "env_file_max_bytes",
            "sidecar_task_max_chars",
        ):
            self.assertIn(key, policy, key)


if __name__ == "__main__":
    unittest.main()
