#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INV = load(SCRIPTS / "inventory.py", "jev_inventory_ttl")
PEER = load(SCRIPTS / "peer_fill.py", "jev_peer_fill_ttl")


class SidecarTtlTests(unittest.TestCase):
    def test_write_sidecar_stamps_written_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.SIDECAR_NAME
            before = int(time.time()) - 1
            INV.write_sidecar(
                path,
                "claude-code",
                "task",
                [{"kind": "skill", "name": "jwt-auth"}],
            )
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["written_at"], before)
        self.assertLessEqual(data["written_at"], int(time.time()) + 1)

    def test_write_miss_stamps_written_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.MISS_NAME
            INV.write_miss(path, "codex", "task")
            data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("written_at", data)

    def test_sidecar_fresh_within_ttl(self) -> None:
        now = time.time()
        payload = {"written_at": now - 100}
        self.assertTrue(INV.sidecar_fresh(payload, ttl_seconds=3600, now=now))

    def test_sidecar_fresh_past_ttl(self) -> None:
        now = time.time()
        payload = {"written_at": now - 3700}
        self.assertFalse(INV.sidecar_fresh(payload, ttl_seconds=3600, now=now))

    def test_sidecar_fresh_boundary_and_skew(self) -> None:
        now = time.time()
        self.assertTrue(
            INV.sidecar_fresh({"written_at": now - 3600}, ttl_seconds=3600, now=now)
        )
        self.assertTrue(
            INV.sidecar_fresh({"written_at": now + 60}, ttl_seconds=3600, now=now)
        )

    def test_sidecar_fresh_missing_timestamp(self) -> None:
        self.assertFalse(INV.sidecar_fresh({"names": []}, ttl_seconds=3600, now=1.0))
        self.assertFalse(
            INV.sidecar_fresh({"written_at": "soon"}, ttl_seconds=3600, now=1.0)
        )
        self.assertFalse(
            INV.sidecar_fresh({"written_at": True}, ttl_seconds=3600, now=1.0)
        )

    def test_sidecar_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.SIDECAR_NAME
            self.assertEqual(INV.sidecar_status(path, now=1000.0), "missing")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(INV.sidecar_status(path, now=1000.0), "invalid")
            path.write_text(json.dumps({"written_at": 500}), encoding="utf-8")
            self.assertEqual(
                INV.sidecar_status(path, ttl_seconds=600, now=1000.0), "fresh"
            )
            self.assertEqual(
                INV.sidecar_status(path, ttl_seconds=400, now=1000.0), "stale"
            )

    def test_check_sidecar_cli(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.SIDECAR_NAME
            buf = StringIO()
            with redirect_stdout(buf):
                code = INV.main(["--check-sidecar", str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(buf.getvalue().strip(), "missing")

            INV.write_sidecar(path, "grok", "t", [])
            buf = StringIO()
            with redirect_stdout(buf):
                code = INV.main(["--check-sidecar", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(buf.getvalue().strip().startswith("fresh"))

    def test_read_miss_drops_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.MISS_NAME
            INV.write_miss(path, "hermes", "old prompt")
            fresh = PEER.read_miss(path)
            self.assertEqual(fresh["task"], "old prompt")
            stale = PEER.read_miss(
                path, ttl_seconds=60, now=time.time() + 3600
            )
            self.assertEqual(stale, {})

    def test_read_miss_legacy_without_timestamp_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / INV.MISS_NAME
            path.write_text(
                json.dumps({"harness": "hermes", "task": "t", "empty": True}),
                encoding="utf-8",
            )
            self.assertEqual(PEER.read_miss(path, ttl_seconds=60), {})

    def test_policy_threshold_value(self) -> None:
        policy = json.loads(
            (ROOT / "skills" / "jev-consult" / "policy.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(policy["sidecar_ttl_seconds"], 14400)
        self.assertEqual(INV.sidecar_ttl_seconds(), 14400.0)


if __name__ == "__main__":
    unittest.main()
