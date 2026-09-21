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


INV = load(SCRIPTS / "inventory.py", "jev_inventory_apply")
FILL = load(SCRIPTS / "apply_fill.py", "jev_apply_fill")

PLUGIN_JSON = {
    "query": "jwt",
    "results": [
        {
            "name": "fmsg-platform",
            "description": "Authenticates with a short-lived JWT",
            "repo": "https://github.com/markmnl/hermes-fmsg",
        },
        {
            "name": "exploit-jwt-plugin",
            "description": "attack helpers",
        },
    ],
}

MCP_TEXT = """
  MCP Catalog + configured servers:

  Name               Status                   Description
  ------------------ ------------------------ -----------
  airtable           available                Bases, tables, and records from Airtable.
  jwt-auth           available                Sign and verify JWT access tokens.
  hack-shell         available                exploit payload runner.
"""


class ApplyFillTests(unittest.TestCase):
    def test_drop_blocked_skips_attacks(self) -> None:
        hits = FILL.parse_plugin_search(json.dumps(PLUGIN_JSON))
        names = {item["name"] for item in hits}
        self.assertIn("fmsg-platform", names)
        self.assertNotIn("exploit-jwt-plugin", names)

    def test_parse_mcp_filters_query(self) -> None:
        hits = FILL.parse_mcp_catalog(MCP_TEXT, {"jwt"})
        names = {item["name"] for item in hits}
        self.assertIn("jwt-auth", names)
        self.assertNotIn("airtable", names)
        self.assertNotIn("hack-shell", names)

    def test_install_argv_never_force_or_enable(self) -> None:
        plugin = FILL.install_argv("plugin", "fmsg-platform")
        self.assertEqual(plugin, ["plugins", "install", "fmsg-platform", "--no-enable"])
        self.assertNotIn("--force", plugin)
        self.assertNotIn("--enable", plugin)
        mcp = FILL.install_argv("mcp", "jwt-auth")
        self.assertEqual(mcp, ["mcp", "install", "jwt-auth"])
        self.assertNotIn("--force", mcp)

    def test_write_ask_has_none_and_no_npx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask.json"
            hits = FILL.parse_plugin_search(json.dumps(PLUGIN_JSON))
            FILL.write_apply_ask(path, "JWT", hits)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("none", data["questions"]["load_tools"]["criteria"])
            blob = path.read_text(encoding="utf-8").lower()
            self.assertIn("never --force", blob)
            self.assertIn("never npx", blob)
            self.assertIn("never claude plugin install", blob)
            self.assertIn("not skillbox", blob)

    def test_other_harness_stays_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            calls: list[list[str]] = []

            def fake_hermes(argv: list[str], timeout: int = 120) -> tuple[int, str]:
                calls.append(list(argv))
                return 0, ""

            with patch.object(FILL, "run_hermes", fake_hermes):
                rc = FILL.fill(
                    "JWT",
                    "claude-code",
                    cwd,
                    "plugin:fmsg-platform",
                    False,
                    cwd / "ask.json",
                )
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [])

    def test_blocked_pick_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            rc = FILL.fill(
                "JWT",
                "hermes",
                cwd,
                "plugin:exploit-jwt",
                True,
                cwd / "ask.json",
            )
            self.assertEqual(rc, 0)

    def test_dry_run_does_not_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            calls: list[list[str]] = []

            def fake_hermes(argv: list[str], timeout: int = 120) -> tuple[int, str]:
                calls.append(list(argv))
                if argv[:2] == ["plugins", "search"]:
                    return 0, json.dumps(PLUGIN_JSON)
                if argv[:2] == ["plugins", "install"]:
                    return 0, "installed"
                return 1, ""

            with patch.object(FILL, "run_hermes", fake_hermes):
                rc = FILL.fill(
                    "Add JWT access tokens in Python",
                    "hermes",
                    cwd,
                    "plugin:fmsg-platform",
                    True,
                    cwd / "ask.json",
                )
            self.assertEqual(rc, 0)
            self.assertTrue(any(c[:2] == ["plugins", "search"] for c in calls))
            self.assertFalse(any(c[:2] == ["plugins", "install"] for c in calls))

    def test_live_plugin_installs_no_enable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            calls: list[list[str]] = []

            def fake_hermes(argv: list[str], timeout: int = 120) -> tuple[int, str]:
                calls.append(list(argv))
                if argv[:2] == ["plugins", "search"]:
                    return 0, json.dumps(PLUGIN_JSON)
                if argv[:2] == ["plugins", "install"]:
                    self.assertIn("--no-enable", argv)
                    self.assertNotIn("--force", argv)
                    self.assertNotIn("--enable", argv)
                    return 0, "installed"
                return 1, ""

            with patch.object(FILL, "run_hermes", fake_hermes):
                rc = FILL.fill(
                    "JWT",
                    "hermes",
                    cwd,
                    "plugin:fmsg-platform",
                    False,
                    cwd / "ask.json",
                )
            self.assertEqual(rc, 0)
            self.assertTrue(any(c[:2] == ["plugins", "install"] for c in calls))

    def test_git_url_pick_rejected(self) -> None:
        parsed = FILL.parse_kind_pick("https://github.com/kitze/skillbox")
        self.assertIsNone(parsed)
        parsed = FILL.parse_kind_pick("owner/repo")
        self.assertIsNone(parsed)

    def test_miss_note_mentions_apply_fill(self) -> None:
        note = INV.format_miss_note(SCRIPTS / "peer_fill.py")
        self.assertIn("apply_fill.py", note)
        self.assertIn("catalog_fill.py", note)


if __name__ == "__main__":
    unittest.main()
