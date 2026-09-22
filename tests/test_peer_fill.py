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

    def test_list_prints_peer_items(self) -> None:
        import io
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            write_skill(hermes / "skills", "ascii-art", NOISE_MD)
            env = dict(__import__("os").environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = str(home)
            env["HOME"] = str(home)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--task",
                    "jwt tokens",
                    "--harness",
                    "claude-code",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                    "--list",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(base),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("jwt-auth", proc.stdout)
            self.assertFalse((base / INV.SIDECAR_NAME).exists())

    def test_show_dumps_one_item(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            env = dict(__import__("os").environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = str(home)
            env["HOME"] = str(home)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--harness",
                    "claude-code",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                    "--show",
                    "jwt-auth",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(base),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            out = json.loads(proc.stdout)
            self.assertEqual(out["name"], "jwt-auth")
            self.assertIn("path", out)
            self.assertIn("description", out)

    def test_show_missing_name_reports_not_found(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            env = dict(__import__("os").environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = str(home)
            env["HOME"] = str(home)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--harness",
                    "claude-code",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                    "--show",
                    "nope",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(base),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("not found: nope", proc.stdout)

    def test_list_json_emits_array(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            env = dict(__import__("os").environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = str(home)
            env["HOME"] = str(home)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--harness",
                    "claude-code",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                    "--list",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(base),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = json.loads(proc.stdout)
            self.assertIsInstance(rows, list)
            self.assertIn("jwt-auth", {r["name"] for r in rows})
            self.assertEqual(rows[0].keys() - {"kind", "name", "path"}, set())

    def test_json_fill_emits_outcome_object(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            env = dict(__import__("os").environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = str(home)
            env["HOME"] = str(home)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--harness",
                    "claude-code",
                    "--home",
                    str(home),
                    "--hermes-home",
                    str(hermes),
                    "--task",
                    "unrelated zebra task",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(base),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            row = json.loads(proc.stdout.strip())
            self.assertEqual(row["outcome"], "no_peer")

    def test_json_no_task_emits_object(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            env = dict(__import__("os").environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = str(base / "home")
            env["HOME"] = str(base / "home")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--json",
                ],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(base),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout.strip())["outcome"], "no_task")

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


class PeerFillInternalsTests(unittest.TestCase):
    def test_watch_emits_fill_state_ticks(self) -> None:
        import io
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / INV.MISS_NAME).write_text(
                json.dumps({"task": "jwt flow", "written_at": int(time.time())}),
                encoding="utf-8",
            )
            env = dict(__import__("os").environ)
            env["JEV_PEER_WATCH_MAX"] = "2"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(cwd),
                    "--watch",
                    "0.01",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            ticks = [
                json.loads(l) for l in proc.stdout.splitlines() if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(t["miss"] for t in ticks))
            self.assertTrue(all(t["miss_task"] == "jwt flow" for t in ticks))
            self.assertFalse(any(t["ask"] for t in ticks))
            stderr_lines = [
                l for l in proc.stderr.splitlines() if l.startswith("watch tick=")
            ]
            self.assertEqual(len(stderr_lines), 2)
            self.assertIn("miss=True", stderr_lines[0])
            self.assertIn("ask=False", stderr_lines[0])

    def test_watch_fail_fast_breaks_on_miss(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / INV.MISS_NAME).write_text(
                json.dumps({"task": "jwt flow", "written_at": int(time.time())}),
                encoding="utf-8",
            )
            env = dict(__import__("os").environ)
            env["JEV_PEER_WATCH_MAX"] = "5"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(cwd),
                    "--watch",
                    "0.01",
                    "--fail-fast",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            ticks = [
                json.loads(l) for l in proc.stdout.splitlines() if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertTrue(ticks[0]["miss"])

    def test_watch_verdict_writes_fill_state(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / INV.MISS_NAME).write_text(
                json.dumps({"task": "jwt flow", "written_at": int(time.time())}),
                encoding="utf-8",
            )
            verdict = cwd / "v.json"
            env = dict(__import__("os").environ)
            env["JEV_PEER_WATCH_MAX"] = "2"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(cwd),
                    "--watch",
                    "0.01",
                    "--verdict",
                    str(verdict),
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pending")
            self.assertEqual(payload["ticks"], 2)
            self.assertTrue(payload["miss"])
            self.assertEqual(payload["miss_task"], "jwt flow")
            self.assertFalse(payload["ask"])

    def test_watch_verdict_clean_when_no_files(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            verdict = cwd / "v.json"
            env = dict(__import__("os").environ)
            env["JEV_PEER_WATCH_MAX"] = "1"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(cwd),
                    "--watch",
                    "0.01",
                    "--verdict",
                    str(verdict),
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "clean")
            self.assertFalse(payload["miss"])

    def test_nonwatch_verdict_pending_with_miss(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / INV.MISS_NAME).write_text(
                json.dumps({"task": "jwt flow", "written_at": int(time.time())}),
                encoding="utf-8",
            )
            verdict = cwd / "v.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(cwd),
                    "--verdict",
                    str(verdict),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pending")
            self.assertEqual(payload["ticks"], 1)
            self.assertTrue(payload["miss"])
            self.assertEqual(payload["miss_task"], "jwt flow")

    def test_nonwatch_verdict_clean(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            verdict = cwd / "v.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(cwd),
                    "--verdict",
                    str(verdict),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "clean")
            self.assertEqual(payload["ticks"], 1)

    def test_watch_appends_ticks_to_out_file(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            out = cwd / "ticks.jsonl"
            env = dict(__import__("os").environ)
            env["JEV_PEER_WATCH_MAX"] = "2"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(cwd),
                    "--watch",
                    "0.01",
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(proc.returncode, 0)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("miss" in t and "ask" in t for t in lines))

    def test_item_for_pick(self) -> None:
        candidates = [
            {"id": "skill_jwt_auth", "name": "jwt-auth"},
            {"id": "skill_ascii", "name": "ascii-art"},
        ]
        self.assertIsNone(FILL.item_for_pick("", candidates))
        self.assertIsNone(FILL.item_for_pick("none", candidates))
        self.assertIsNone(FILL.item_for_pick("missing", candidates))
        self.assertEqual(FILL.item_for_pick("skill_jwt_auth", candidates)["name"], "jwt-auth")
        self.assertEqual(FILL.item_for_pick("ascii-art", candidates)["id"], "skill_ascii")

    def test_ignore_drops_cruft(self) -> None:
        skipped = FILL.ignore(
            "x",
            ["SKILL.md", ".git", "node_modules", "__pycache__", "mod.pyc", "notes.txt"],
        )
        self.assertEqual(skipped, {".git", "node_modules", "__pycache__", "mod.pyc"})

    def test_names_in_lowercases(self) -> None:
        self.assertEqual(FILL.names_in([{"name": "JWT-Auth"}, {"name": "x"}]), {"jwt-auth", "x"})
        self.assertIn("", FILL.names_in([{}]))

    def test_under_any(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            root.mkdir()
            inside = root / "jwt-auth"
            inside.mkdir()
            self.assertTrue(FILL.under_any(inside, [root]))
            self.assertFalse(FILL.under_any(Path(tmp) / "elsewhere", [root]))

    def test_peer_skills_skips_dest_and_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            write_skill(home / ".grok" / "skills", "jwt-auth", JWT_MD)  # dup name across peers
            write_skill(home / ".codex" / "skills", "jev-consult", JWT_MD)  # pack itself
            write_skill(home / ".claude" / "skills", "jwt-auth", JWT_MD)  # already in dest
            peers = FILL.peer_skills("claude-code", home, hermes)
            self.assertEqual(peers, [])  # jwt-auth already installed in dest; pack skipped

    def test_peer_skills_lists_other_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            hermes = base / "hermes"
            home = base / "home"
            write_skill(hermes / "skills", "jwt-auth", JWT_MD)
            peers = FILL.peer_skills("claude-code", home, hermes)
            self.assertEqual(len(peers), 1)
            self.assertEqual(peers[0]["name"], "jwt-auth")
            self.assertEqual(peers[0]["source_harness"], "hermes")

    def test_run_jev_fail_open(self) -> None:
        class FakeProc:
            def __init__(self, rc, out):
                self.returncode = rc
                self.stdout = out

        with patch("subprocess.run", return_value=FakeProc(2, "{}")):
            self.assertIsNone(FILL.run_jev(Path("ask.json")))
        with patch("subprocess.run", return_value=FakeProc(0, "not json")):
            self.assertIsNone(FILL.run_jev(Path("ask.json")))
        with patch("subprocess.run", return_value=FakeProc(0, "[1]")):
            self.assertIsNone(FILL.run_jev(Path("ask.json")))
        with patch("subprocess.run", return_value=FakeProc(0, '{"decision": {}}')):
            self.assertEqual(FILL.run_jev(Path("ask.json")), {"decision": {}})
        with patch("subprocess.run", side_effect=OSError("no python")):
            self.assertIsNone(FILL.run_jev(Path("ask.json")))

    def test_run_jev_timeout(self) -> None:
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("jev", 90)):
            self.assertIsNone(FILL.run_jev(Path("ask.json")))

    def test_fill_timeout_env_override(self) -> None:
        import os

        self.assertEqual(FILL.fill_timeout_seconds(), 90.0)
        with patch.dict(os.environ, {"JEV_FILL_TIMEOUT": "12.5"}):
            self.assertEqual(FILL.fill_timeout_seconds(), 12.5)
        with patch.dict(os.environ, {"JEV_FILL_TIMEOUT": "bogus"}):
            self.assertEqual(FILL.fill_timeout_seconds(), 90.0)
        with patch.dict(os.environ, {"JEV_FILL_TIMEOUT": "-3"}):
            self.assertEqual(FILL.fill_timeout_seconds(), 90.0)
        captured = {}

        class FakeProc:
            returncode = 0
            stdout = "{}"

        def fake_run(argv, **kw):
            captured.update(kw)
            return FakeProc()

        with patch.dict(os.environ, {"JEV_FILL_TIMEOUT": "7"}):
            with patch("subprocess.run", side_effect=fake_run):
                FILL.run_jev(Path("ask.json"))
        self.assertEqual(captured.get("timeout"), 7.0)


class ReadMissPruneTests(unittest.TestCase):
    def _miss(self, path: Path, age: float) -> None:
        import time as _time

        path.write_text(
            json.dumps(
                {
                    "harness": "hermes",
                    "task": "x",
                    "empty": True,
                    "written_at": int(_time.time() - age),
                }
            ),
            encoding="utf-8",
        )

    def test_stale_miss_is_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools-miss.json"
            self._miss(path, age=999999)
            self.assertEqual(FILL.read_miss(path), {})
            self.assertFalse(path.exists())  # stale marker pruned

    def test_fresh_miss_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools-miss.json"
            self._miss(path, age=10)
            data = FILL.read_miss(path)
            self.assertEqual(data["harness"], "hermes")
            self.assertTrue(path.exists())

    def test_missing_or_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools-miss.json"
            self.assertEqual(FILL.read_miss(path), {})
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(FILL.read_miss(path), {})
            self.assertTrue(path.exists())  # unparseable files left alone


class PeerFillE2ETests(unittest.TestCase):
    """Subprocess: no_task / already_enough / no_peer tags."""

    SCRIPT = SCRIPTS / "peer_fill.py"

    def _run(self, argv: list[str], cwd: str, home: str, log: str | None = "0"):
        import os
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        if log is not None:
            env["JEV_CONSULT_LOG"] = log
        env["USERPROFILE"] = home
        env["HOME"] = home
        proc = subprocess.run(
            [sys.executable, str(self.SCRIPT)] + argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_no_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run([], tmp, tmp), "no_task")

    def test_no_peer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex" / "skills").mkdir(parents=True)
            out = self._run(
                ["--task", "jwt tokens", "--harness", "codex", "--home", str(home)],
                tmp,
                tmp,
            )
            self.assertEqual(out, "no_peer")

    def test_already_enough_when_local_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            skill = home / ".codex" / "skills" / "jwt-auth"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: jwt-auth\ndescription: jwt\n---\n", encoding="utf-8"
            )
            out = self._run(
                [
                    "--task",
                    "jwt tokens",
                    "--harness",
                    "codex",
                    "--home",
                    str(home),
                    "--cwd",
                    tmp,
                ],
                tmp,
                tmp,
            )
            self.assertEqual(out, "already_enough")
            self.assertTrue((Path(tmp) / ".jev-tools.json").is_file())

    def test_fill_outcome_logged_to_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".codex" / "skills").mkdir(parents=True)
            log_path = Path(tmp) / "decisions.jsonl"
            out = self._run(
                ["--task", "jwt tokens", "--harness", "codex", "--home", str(home)],
                tmp,
                tmp,
                log=str(log_path),
            )
            self.assertEqual(out, "no_peer")
            entries = [
                json.loads(l)
                for l in log_path.read_text(encoding="utf-8").splitlines()
                if l.strip()
            ]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["jev_status"], "fill")
            self.assertEqual(entries[0]["fill"], "peer")
            self.assertEqual(entries[0]["outcome"], "no_peer")

    def test_status_reports_fill_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(["--status", "--cwd", tmp], tmp, tmp)
            report = json.loads(out)
            self.assertFalse(report["miss"])
            self.assertFalse(report["ask_file_exists"])
            self.assertEqual(report["cwd"], str(Path(tmp).resolve()))
            (Path(tmp) / ".jev-peer-fill.request.json").write_text(
                "{}", encoding="utf-8"
            )
            (Path(tmp) / ".jev-tools-miss.json").write_text(
                json.dumps(
                    {"task": "jwt", "harness": "codex", "written_at": time.time()}
                ),
                encoding="utf-8",
            )
            out = self._run(["--status", "--cwd", tmp], tmp, tmp)
            report = json.loads(out)
            self.assertTrue(report["miss"])
            self.assertEqual(report["miss_task"], "jwt")
            self.assertTrue(report["ask_file_exists"])

    def test_status_jq_prints_one_field(self) -> None:
        import os
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".jev-tools-miss.json").write_text(
                json.dumps({"task": "jwt", "harness": "codex", "written_at": time.time()}),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = tmp
            env["HOME"] = tmp
            proc = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--status", "--cwd", tmp, "--jq", "miss_task"],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), "jwt")
            proc = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--status", "--cwd", tmp, "--jq", "nope"],
                capture_output=True,
                text=True,
                cwd=tmp,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("bad --jq key", proc.stderr)


if __name__ == "__main__":
    unittest.main()
