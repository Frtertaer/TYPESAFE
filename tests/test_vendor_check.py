#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for scripts/vendor_check.py — offline paths only (no --fetch)."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "vendor_check", ROOT / "scripts" / "vendor_check.py"
)
VC = importlib.util.module_from_spec(SPEC)
sys.modules["vendor_check"] = VC
SPEC.loader.exec_module(VC)


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = VC.main(["vendor_check.py"] + argv)
    return rc, out.getvalue(), err.getvalue()


class VendorCheckTests(unittest.TestCase):
    def test_repo_manifest_is_consistent(self) -> None:
        rc, out, err = run_main([])
        self.assertEqual(rc, 0, err)
        self.assertIn("jevcal", out)
        self.assertIn("skill-router", out)
        self.assertIn("->", out)

    def test_json_payload(self) -> None:
        rc, out, _ = run_main(["--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        dirs = {s["dir"] for s in payload["snapshots"]}
        self.assertIn("jevcal", dirs)
        self.assertIn("typesafeai-cli", dirs)
        lint = next(s for s in payload["snapshots"] if s["dir"] == "jevcal")
        self.assertEqual(lint["pairs"][0]["ours"],
                         "skills/jev-consult/scripts/question_lint.py")

    def test_unknown_arg_is_rc2(self) -> None:
        rc, _, err = run_main(["--bogus"])
        self.assertEqual(rc, 2)
        self.assertIn("--bogus", err)

    def test_missing_snapshot_dir_fails(self) -> None:
        manifest = json.loads(VC.MANIFEST.read_text(encoding="utf-8"))
        manifest["snapshots"][0]["dir"] = "no-such-snapshot"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PORTS.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(VC, "MANIFEST", path):
                rc, out, _ = run_main(["--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertTrue(any("missing" in e for e in payload["errors"]))

    def test_pin_mismatch_fails(self) -> None:
        manifest = json.loads(VC.MANIFEST.read_text(encoding="utf-8"))
        manifest["snapshots"][0]["commit"] = "0" * 40
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PORTS.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(VC, "MANIFEST", path):
                rc, _, err = run_main([])
        self.assertEqual(rc, 1)
        self.assertIn("UPSTREAM_COMMIT", err)
        self.assertIn("manifest pins", err)

    def test_fetch_marks_moved_upstream(self) -> None:
        with patch.object(VC, "_upstream_head", return_value="a" * 40):
            rc, out, _ = run_main(["--fetch", "--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(out)
        self.assertTrue(
            all(s["fetch"] == "upstream moved" for s in payload["snapshots"])
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
