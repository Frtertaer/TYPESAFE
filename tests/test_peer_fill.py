#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INV = load(SCRIPTS / "inventory.py", "jev_inventory_peer")
FILL = load(SCRIPTS / "peer_fill.py", "jev_peer_fill")
HOOK = load(SCRIPTS / "inventory_hook.py", "jev_inventory_hook_peer")

JWT_MD = """---
name: jwt-auth
description: Add JWT access tokens and refresh flow in Python APIs.
---

# jwt-auth
"""
NOISE_MD = """---
name: ascii-art
description: Draw cowsay banners.
---

# ascii-art
"""


def write_skill(root: Path, name: str, body: str) -> Path:
    dest = root / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "SKILL.md").write_text(body, encoding="utf-8")
    return dest


class PeerFillTests(unittest.TestCase):
    def test_copies_peer_skill_into_empty_harness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            cwd = base / "cwd"
            cwd.mkdir()
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            write_skill(hermes / "skills", "ascii-art", NOISE_MD)
            rc = FILL.fill(
                "Add JWT access tokens in Python",
                "claude-code",
                home,
                hermes,
                cwd,
                "jwt-auth",
                False,
                cwd / "ask.json",
            )
            self.assertEqual(rc, 0)
            dest = home / ".claude" / "skills" / "jwt-auth" / "SKILL.md"
            self.assertTrue(dest.is_file())
            self.assertFalse((home / ".claude" / "skills" / "ascii-art").exists())
            sidecar = json.loads((cwd / INV.SIDECAR_NAME).read_text(encoding="utf-8"))
            self.assertEqual(sidecar["names"][0]["name"], "jwt-auth")

    def test_dry_run_does_not_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            cwd = base / "cwd"
            cwd.mkdir()
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            rc = FILL.fill(
                "Add JWT access tokens in Python",
                "claude-code",
                home,
                hermes,
                cwd,
                "jwt-auth",
                True,
                cwd / "ask.json",
            )
            self.assertEqual(rc, 0)
            self.assertFalse((home / ".claude" / "skills" / "jwt-auth").exists())

    def test_already_enough_skips_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            cwd = base / "cwd"
            cwd.mkdir()
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            write_skill(home / ".claude" / "skills", "jwt-auth", JWT_MD)
            rc = FILL.fill(
                "Add JWT access tokens in Python",
                "claude-code",
                home,
                hermes,
                cwd,
                "jwt-auth",
                False,
                cwd / "ask.json",
            )
            self.assertEqual(rc, 0)

    def test_does_not_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            cwd = base / "cwd"
            cwd.mkdir()
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            dest = write_skill(home / ".claude" / "skills", "jwt-auth", "# keep\n")
            (dest / "SKILL.md").write_text("# keep\n", encoding="utf-8")
            src = hermes / "skills" / "jwt-auth"
            copied = FILL.copy_one(src, home / ".claude" / "skills", False)
            self.assertIsNone(copied)
            self.assertEqual((dest / "SKILL.md").read_text(encoding="utf-8"), "# keep\n")

    def test_from_miss_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            cwd = base / "cwd"
            cwd.mkdir()
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            INV.write_miss(cwd / INV.MISS_NAME, "claude-code", "Add JWT access tokens")
            argv = [
                "peer_fill.py",
                "--from-miss",
                "--home",
                str(home),
                "--hermes-home",
                str(hermes),
                "--cwd",
                str(cwd),
                "--pick",
                "jwt-auth",
            ]
            with patch.object(sys, "argv", argv):
                rc = FILL.main()
            self.assertEqual(rc, 0)
            self.assertTrue((home / ".claude" / "skills" / "jwt-auth" / "SKILL.md").is_file())
            self.assertFalse((cwd / INV.MISS_NAME).exists())

    def test_write_peer_ask_has_none_hatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask.json"
            FILL.write_peer_ask(
                path,
                "JWT",
                "claude-code",
                [{"id": "skill_jwt_auth", "name": "jwt-auth", "source_harness": "hermes", "description": "JWT"}],
            )
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("none", data["questions"]["load_tools"]["criteria"])
            self.assertNotIn("installed_enough", data["questions"])
            blob = path.read_text(encoding="utf-8").lower()
            self.assertIn("do not install marketplace", blob)
            self.assertNotIn("claude plugin install", blob)

    def test_refuses_source_outside_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside" / "jwt-auth"
            outside.mkdir(parents=True)
            (outside / "SKILL.md").write_text(JWT_MD, encoding="utf-8")
            roots = [Path(tmp) / "hermes" / "skills"]
            roots[0].mkdir(parents=True)
            self.assertFalse(FILL.under_any(outside, roots))

    def test_hook_empty_jwt_writes_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            out = HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": str(cwd),
                },
                items=[],
                harness="claude-code",
            )
            note = out["hookSpecificOutput"]["additionalContext"]
            self.assertIn("peer_fill.py", note)
            self.assertIn("--from-miss", note)
            miss = json.loads((cwd / INV.MISS_NAME).read_text(encoding="utf-8"))
            self.assertTrue(miss["empty"])
            self.assertEqual(miss["harness"], "claude-code")

    def test_hook_match_clears_miss(self) -> None:
        items = [
            {
                "kind": INV.KIND_SKILL,
                "id": "skill_jwt_auth",
                "name": "jwt-auth",
                "description": "Add JWT access tokens",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            INV.write_miss(cwd / INV.MISS_NAME, "claude-code", "old")
            HOOK.handle(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "Add JWT access tokens in Python",
                    "cwd": str(cwd),
                },
                items=items,
                harness="claude-code",
                pick_fn=lambda *_args, **_kwargs: {"status": "skip", "winner": None},
            )
            self.assertFalse((cwd / INV.MISS_NAME).exists())


if __name__ == "__main__":
    unittest.main()
