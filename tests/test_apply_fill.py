#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import os
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

    def test_watch_emits_readonly_ticks(self) -> None:
        import subprocess
        import time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".jev-tools-miss.json").write_text(
                json.dumps({"task": "jwt", "harness": "hermes", "written_at": time.time()}),
                encoding="utf-8",
            )
            env = dict(os.environ, JEV_APPLY_WATCH_MAX="2")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.01",
                    "--cwd", str(cwd),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ticks = [
                json.loads(l)
                for l in proc.stdout.splitlines()
                if l.startswith("{")
            ]
            stderr_lines = [
                l for l in proc.stderr.splitlines() if l.startswith("watch tick=")
            ]
            self.assertEqual(len(stderr_lines), 2)
            self.assertIn("miss=True", stderr_lines[0])
            self.assertIn("ask=False", stderr_lines[0])
            self.assertEqual(len(ticks), 2)
            self.assertTrue(all(t["miss"] is True for t in ticks))
            self.assertTrue(all(t["ask"] is False for t in ticks))
            self.assertTrue(all(t["miss_age_s"] >= 0 for t in ticks))

    def test_watch_fail_fast_breaks_when_miss_present(self) -> None:
        import subprocess
        import time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".jev-tools-miss.json").write_text(
                json.dumps({"task": "jwt", "harness": "hermes", "written_at": time.time()}),
                encoding="utf-8",
            )
            env = dict(os.environ, JEV_APPLY_WATCH_MAX="9")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.01",
                    "--cwd", str(cwd),
                    "--fail-fast",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ticks = [
                json.loads(l)
                for l in proc.stdout.splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertTrue(ticks[0]["miss"])

    def test_watch_tick_miss_age_none_without_miss(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            env = dict(os.environ, JEV_APPLY_WATCH_MAX="1")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.01",
                    "--cwd", str(cwd),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            ticks = [
                json.loads(l)
                for l in proc.stdout.splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(ticks), 1)
            self.assertIsNone(ticks[0]["miss_age_s"])

    def test_watch_verdict_writes_state_json(self) -> None:
        import subprocess
        import time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".jev-tools-miss.json").write_text(
                json.dumps({"task": "jwt", "harness": "hermes", "written_at": time.time()}),
                encoding="utf-8",
            )
            verdict = cwd / "v.json"
            env = dict(os.environ, JEV_APPLY_WATCH_MAX="2")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.01",
                    "--cwd", str(cwd),
                    "--verdict", str(verdict),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pending")
            self.assertEqual(payload["ticks"], 2)
            self.assertTrue(payload["miss"])
            self.assertFalse(payload["ask"])

    def test_watch_verdict_clean_when_no_files(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            verdict = cwd / "v.json"
            env = dict(os.environ, JEV_APPLY_WATCH_MAX="1")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.01",
                    "--cwd", str(cwd),
                    "--verdict", str(verdict),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "clean")
            self.assertFalse(payload["miss"])
            self.assertFalse(payload["ask"])

    def test_nonwatch_verdict_pending_with_miss(self) -> None:
        import subprocess
        import time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".jev-tools-miss.json").write_text(
                json.dumps({"task": "jwt", "harness": "hermes", "written_at": time.time()}),
                encoding="utf-8",
            )
            verdict = cwd / "v.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--cwd", str(cwd),
                    "--verdict", str(verdict),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "pending")
            self.assertEqual(payload["ticks"], 1)
            self.assertTrue(payload["miss"])

    def test_nonwatch_verdict_clean(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            verdict = cwd / "v.json"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--cwd", str(cwd),
                    "--verdict", str(verdict),
                ],
                capture_output=True,
                text=True,
                timeout=30,
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
            env = dict(os.environ, JEV_APPLY_WATCH_MAX="2")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.01",
                    "--cwd", str(cwd),
                    "--out", str(out),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [
                json.loads(l)
                for l in out.read_text(encoding="utf-8").splitlines()
                if l.startswith("{")
            ]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all("miss" in t and "ask" in t for t in lines))

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

    def test_json_outcome_emits_object(self) -> None:
        import io

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            with patch("sys.stdout", buf):
                rc = FILL.fill(
                    "JWT",
                    "claude-code",
                    cwd,
                    "plugin:fmsg-platform",
                    False,
                    cwd / "ask.json",
                    as_json=True,
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["outcome"], "human")
            self.assertEqual(out["detail"], "human")

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


class ApplyFillInternalsTests(unittest.TestCase):
    def test_bare_name(self) -> None:
        self.assertEqual(FILL.bare_name(" jwt-auth "), "jwt-auth")
        self.assertEqual(FILL.bare_name(""), "")
        self.assertEqual(FILL.bare_name("https://evil.example/x"), "")
        self.assertEqual(FILL.bare_name("a/b"), "")
        self.assertEqual(FILL.bare_name("a\\b"), "")

    def test_parse_kind_pick(self) -> None:
        self.assertIsNone(FILL.parse_kind_pick(""))
        self.assertIsNone(FILL.parse_kind_pick("none"))
        self.assertEqual(FILL.parse_kind_pick("plugin:jwt"), ("plugin", "jwt"))
        self.assertEqual(FILL.parse_kind_pick("MCP:Airtable"), ("mcp", "Airtable"))
        self.assertEqual(FILL.parse_kind_pick("jwt-auth"), ("plugin", "jwt-auth"))
        self.assertIsNone(FILL.parse_kind_pick("plugin:https://evil.example"))
        self.assertIsNone(FILL.parse_kind_pick("mcp:a/b"))

    def test_as_item(self) -> None:
        item = FILL.as_item("plugin", "jwt", "desc")
        self.assertEqual(item["kind"], "plugin")
        self.assertEqual(item["name"], "jwt")
        self.assertEqual(item["identifier"], "plugin:jwt")
        self.assertTrue(item["id"])

    def test_parse_plugin_search_shapes(self) -> None:
        self.assertEqual(FILL.parse_plugin_search("not json"), [])
        self.assertEqual(FILL.parse_plugin_search('"str"'), [])
        self.assertEqual(FILL.parse_plugin_search('{"results": "junk"}'), [])
        rows = FILL.parse_plugin_search(
            json.dumps({"plugins": [{"name": "jwt"}, {"no_name": 1}, "junk"]})
        )
        self.assertEqual([r["name"] for r in rows], ["jwt"])

    def test_parse_mcp_catalog(self) -> None:
        hits = FILL.parse_mcp_catalog(MCP_TEXT, {"jwt"})
        self.assertEqual([h["name"] for h in hits], ["jwt-auth"])
        self.assertEqual(hits[0]["kind"], "mcp")
        # empty query admits every well-formed row except blocked names
        all_hits = FILL.parse_mcp_catalog(MCP_TEXT, set())
        self.assertEqual(
            [h["name"] for h in all_hits], ["airtable", "jwt-auth"]
        )  # hack-shell dropped by blocked_text

    def test_search_hits_missing_hermes(self) -> None:
        calls = []

        def fake(argv, timeout=120):
            calls.append(argv[0])
            if argv[0] == "plugins":
                return 127, ""
            return 0, ""

        with patch.object(FILL, "run_hermes", side_effect=fake):
            self.assertIsNone(FILL.search_hits("jwt"))

    def test_search_hits_merges(self) -> None:
        def fake(argv, timeout=120):
            if argv[0] == "plugins":
                return 0, json.dumps({"results": [{"name": "jwt"}]})
            if argv[0] == "mcp":
                return 0, MCP_TEXT
            return 1, ""

        with patch.object(FILL, "run_hermes", side_effect=fake):
            hits = FILL.search_hits("jwt auth")
        self.assertEqual(
            {h["identifier"] for h in hits}, {"plugin:jwt", "mcp:jwt-auth"}
        )

    def test_inspect_ok(self) -> None:
        good = json.dumps({"results": [{"name": "jwt"}]})
        with patch.object(FILL, "run_hermes", return_value=(0, good)):
            self.assertTrue(FILL.inspect_ok("plugin", "jwt"))
        with patch.object(FILL, "run_hermes", return_value=(0, good)):
            self.assertFalse(FILL.inspect_ok("plugin", "other"))  # name not in hits
        with patch.object(FILL, "run_hermes", return_value=(0, "verdict: blocked " + good)):
            self.assertFalse(FILL.inspect_ok("plugin", "jwt"))
        with patch.object(FILL, "run_hermes", return_value=(1, "")):
            self.assertFalse(FILL.inspect_ok("plugin", "jwt"))
        self.assertTrue(FILL.inspect_ok("mcp", "airtable"))  # no hermes call needed
        self.assertFalse(FILL.inspect_ok("mcp", "a/b"))
        self.assertFalse(FILL.inspect_ok("skill", "x"))

    def test_install_one(self) -> None:
        self.assertTrue(FILL.install_one("plugin", "jwt", dry_run=True))
        self.assertTrue(FILL.install_one("mcp", "airtable", dry_run=True))
        self.assertFalse(FILL.install_one("skill", "x", dry_run=True))  # empty argv
        with patch.object(FILL, "run_hermes", return_value=(0, "")) as run:
            self.assertTrue(FILL.install_one("plugin", "jwt", dry_run=False))
        self.assertIn("--no-enable", run.call_args[0][0])
        with patch.object(FILL, "run_hermes", return_value=(1, "")):
            self.assertFalse(FILL.install_one("plugin", "jwt", dry_run=False))

    def test_item_for_pick(self) -> None:
        hits = [FILL.as_item("plugin", "jwt"), FILL.as_item("mcp", "airtable")]
        self.assertIsNone(FILL.item_for_pick("none", hits))
        self.assertIsNone(FILL.item_for_pick("missing", hits))
        self.assertEqual(FILL.item_for_pick("mcp:airtable", hits)["name"], "airtable")
        self.assertEqual(FILL.item_for_pick("plugin:jwt", hits)["kind"], "plugin")
        self.assertEqual(FILL.item_for_pick(hits[0]["id"], hits)["name"], "jwt")
        self.assertEqual(FILL.item_for_pick("jwt", hits)["kind"], "plugin")


class ApplyFillE2ETests(unittest.TestCase):
    """Subprocess: stable fail-open tags without network or Hermes."""

    SCRIPT = SCRIPTS / "apply_fill.py"

    def _run(self, argv: list[str], cwd: str | None = None, home: str | None = None, log: str | None = "0"):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        if log is not None:
            env["JEV_CONSULT_LOG"] = log
        if home:
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

    def test_no_task_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._run(["--cwd", tmp], cwd=tmp, home=tmp), "no_task")

    def test_non_hermes_dest_is_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(
                ["--task", "x", "--harness", "codex", "--cwd", tmp], cwd=tmp, home=tmp
            )
            self.assertEqual(out, "human")

    def test_from_miss_missing_is_no_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(["--from-miss", "--cwd", tmp], cwd=tmp, home=tmp)
            self.assertEqual(out, "no_task")

    def test_blocked_pick_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(
                [
                    "--task",
                    "x",
                    "--harness",
                    "hermes",
                    "--cwd",
                    tmp,
                    "--pick",
                    "plugin:exploit kit",
                ],
                cwd=tmp,
                home=tmp,
            )
            self.assertEqual(out, "blocked")

    def test_fill_outcome_logged_to_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "decisions.jsonl"
            out = self._run(
                ["--task", "x", "--harness", "codex", "--cwd", tmp],
                cwd=tmp,
                home=tmp,
                log=str(log_path),
            )
            self.assertEqual(out, "human")
            entries = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(entries), 1)
            entry = entries[0]
            self.assertEqual(entry["jev_status"], "fill")
            self.assertEqual(entry["fill"], "apply")
            self.assertEqual(entry["outcome"], "human")
            self.assertEqual(entry["harness"], "codex")



    def test_status_reports_fill_state(self) -> None:
        import time

        with tempfile.TemporaryDirectory() as tmp:
            out = self._run(["--status", "--cwd", tmp], cwd=tmp, home=tmp)
            report = json.loads(out)
            self.assertFalse(report["miss"])
            self.assertFalse(report["ask"])
            self.assertIsNone(report["miss_age_s"])
            (Path(tmp) / INV.MISS_NAME).write_text(
                json.dumps({"task": "jwt", "harness": "codex", "written_at": time.time()}),
                encoding="utf-8",
            )
            out = self._run(["--status", "--cwd", tmp], cwd=tmp, home=tmp)
            report = json.loads(out)
            self.assertTrue(report["miss"])
            self.assertEqual(report["task"], "jwt")
            self.assertIsNotNone(report["miss_age_s"])

    def test_status_jq_prints_one_field(self) -> None:
        import subprocess
        import time

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / INV.MISS_NAME).write_text(
                json.dumps({"task": "jwt", "harness": "codex", "written_at": time.time()}),
                encoding="utf-8",
            )
            env = dict(os.environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["USERPROFILE"] = tmp
            env["HOME"] = tmp
            proc = subprocess.run(
                [sys.executable, str(self.SCRIPT), "--status", "--cwd", tmp, "--jq", "task"],
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


class WatchJqTests(unittest.TestCase):
    def test_watch_jq_prints_only_named_tick_field(self) -> None:
        import subprocess
        import time

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".jev-tools-miss.json").write_text(
                json.dumps({"task": "jwt", "harness": "hermes", "written_at": time.time()}),
                encoding="utf-8",
            )
            env = dict(os.environ, JEV_APPLY_WATCH_MAX="2")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.01",
                    "--cwd", str(cwd),
                    "--jq", "miss",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.splitlines(), ["true", "true"])

class WatchSecsEnvTests(unittest.TestCase):
    def test_watch_secs_env_bounds_loop(self) -> None:
        import subprocess
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(
                os.environ,
                JEV_APPLY_WATCH_MAX="0",
                JEV_APPLY_WATCH_SECS="0.05",
            )
            start = _time.time()
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "apply_fill.py"),
                    "--watch", "0.02",
                    "--cwd", tmp,
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertLess(_time.time() - start, 10.0)
            ticks = [
                l for l in proc.stdout.splitlines() if l.startswith("{")
            ]
            self.assertLessEqual(len(ticks), 10)
            self.assertGreaterEqual(len(ticks), 1)

class WriteAskAtomicTests(unittest.TestCase):
    def test_write_ask_atomic_no_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "ask.json"
            hits = [{"id": "kind:name", "kind": "kind", "name": "name"}]
            FILL.write_apply_ask(out, "task", hits)
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(names, ["ask.json"])
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("questions", data)

if __name__ == "__main__":
    unittest.main()
