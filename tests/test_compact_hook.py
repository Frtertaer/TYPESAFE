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

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "scripts" / "install.py"
HOOK_PATH = ROOT / "skills" / "jev-consult" / "scripts" / "compact_hook.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


INSTALL = load(INSTALL_PATH, "jev_install")
HOOK = load(HOOK_PATH, "jev_compact_hook")


class HookTests(unittest.TestCase):
    def setUp(self):
        # Keep test runs out of the real ~/.cache spill dir.
        self._spill_env = patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"})
        self._spill_env.start()
        self.addCleanup(self._spill_env.stop)

    def test_small_result_unchanged(self):
        out = HOOK.handle({"hook_event_name": "PostToolUse", "tool_response": "ok"})
        self.assertEqual(out, {})

    def test_error_not_abridged(self):
        fat = "x" * 40000
        out = HOOK.handle({"hook_event_name": "PostToolUse", "tool_response": fat, "is_error": True})
        self.assertEqual(out, {})

    def test_fat_result_replaced(self):
        fat = "HEAD" + ("n" * 40000) + "TAIL"
        out = HOOK.handle({"hook_event_name": "PostToolUse", "tool_response": fat})
        replaced = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("HEAD", replaced)
        self.assertIn("TAIL", replaced)
        self.assertIn("omitted", replaced)
        self.assertLess(len(replaced), len(fat))

    def test_fat_result_spills_to_disk_and_marker_names_file(self):
        fat = "HEAD" + ("n" * 40000) + "TAIL"
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": tmp}):
                out = HOOK.handle(
                    {"hook_event_name": "PostToolUse", "tool_response": fat}
                )
            files = list(Path(tmp).glob("*.txt"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(encoding="utf-8"), fat)
            replaced = out["hookSpecificOutput"]["updatedToolOutput"]
            self.assertIn("full output saved:", replaced)
            self.assertIn(str(files[0]), replaced)
            self.assertLess(len(replaced), len(fat))

    def test_grok_toolresult_shape_kept(self):
        fat = "HEAD" + ("n" * 40000) + "TAIL"
        original = {"type": "Bash", "output_for_prompt": fat, "exit_code": 0}
        out = HOOK.handle({"hook_event_name": "PostToolUse", "toolResult": original})
        replaced = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertEqual(replaced["type"], "Bash")
        self.assertEqual(replaced["exit_code"], 0)
        self.assertLess(len(replaced["output_for_prompt"]), len(fat))

    def test_truncated_flag_skips(self):
        out = HOOK.handle(
            {
                "hook_event_name": "PostToolUse",
                "toolResultTruncated": True,
                "toolResult": "x" * 40000,
            }
        )
        self.assertEqual(out, {})

    def test_camelcase_event_and_iserror_variants(self):
        fat = "y" * 40000
        out = HOOK.handle({"hookEventName": "post_tool_use", "toolResult": fat})
        self.assertIn("updatedToolOutput", out["hookSpecificOutput"])
        out = HOOK.handle(
            {"hook_event_name": "PostToolUse", "toolResult": fat, "isError": True}
        )
        self.assertEqual(out, {})

    def test_nested_content_dict_gets_output_for_prompt_key(self):
        # text found via content->text nesting; replace_payload adds
        # output_for_prompt since no flat output key exists.
        fat = "z" * 40000
        original = {"content": {"text": fat}}
        out = HOOK.handle({"hook_event_name": "PostToolUse", "toolResult": original})
        replaced = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIsInstance(replaced, dict)
        self.assertIn("output_for_prompt", replaced)
        self.assertLess(len(replaced["output_for_prompt"]), len(fat))
        self.assertEqual(replaced["content"], {"text": fat})

    def test_dict_result_without_known_key_noop(self):
        # a dict whose text can't be extracted at all abridges to nothing.
        out = HOOK.handle(
            {"hook_event_name": "PostToolUse", "toolResult": {"weird": "x" * 40000}}
        )
        self.assertEqual(out, {})


class InstallHookTests(unittest.TestCase):
    def test_claude_hook_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PostToolUse": [
                                {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "keep-me"}]}
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            script = Path(tmp) / "jev-consult" / "scripts" / "compact_hook.py"
            INSTALL.upsert_claude_hook(settings, script, dry_run=False)
            INSTALL.upsert_claude_hook(settings, script, dry_run=False)
            data = json.loads(settings.read_text(encoding="utf-8"))
            posts = data["hooks"]["PostToolUse"]
            self.assertEqual(len(posts), 2)
            self.assertEqual(posts[0]["matcher"], "Write|Edit")
            self.assertIn("compact_hook.py", json.dumps(posts[1]))

    def test_enable_plugin_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.yaml"
            config.write_text("plugins:\n  enabled:\n    - disk-cleanup\n", encoding="utf-8")
            INSTALL.enable_hermes_plugin(config, "jev-compact", dry_run=False)
            INSTALL.enable_hermes_plugin(config, "jev-compact", dry_run=False)
            text = config.read_text(encoding="utf-8")
            self.assertEqual(text.count("- jev-compact"), 1)
            INSTALL.disable_hermes_plugin(config, "jev-compact", dry_run=False)
            self.assertNotIn("jev-compact", config.read_text(encoding="utf-8"))


class HookE2ETests(unittest.TestCase):
    """Subprocess e2e: stdin payload -> stdout JSON, no Jev key needed."""

    def _run(self, stdin_text: str, home: str | None = None, spill: str | None = None):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        if home:
            env["USERPROFILE"] = home
            env["HOME"] = home
            env["HERMES_HOME"] = str(Path(home) / ".hermes")
        if spill:
            env["JEV_CONSULT_SPILL"] = spill
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def _run_full(self, stdin_text: str, argv: list | None = None):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH), *(argv or [])],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_invalid_utf8_stdin_fail_open(self) -> None:
        """Binary garbage on stdin (not valid utf-8) must still emit {} rc 0."""
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=b"\x80\x81\xff\xfe\x00abc",
            capture_output=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.decode("utf-8", "replace").strip(), "{}")
        self.assertNotIn(b"Traceback", proc.stderr)

    def test_verbose_prints_skip_reason(self) -> None:
        proc = self._run_full(
            json.dumps({"hook_event_name": "UserPromptSubmit", "toolResult": "x"}),
            argv=["--verbose"],
        )
        self.assertEqual(json.loads(proc.stdout.strip()), {})
        self.assertIn("not a PostToolUse event", proc.stderr)

    def test_no_verbose_stderr_quiet(self) -> None:
        proc = self._run_full(
            json.dumps({"hook_event_name": "UserPromptSubmit", "toolResult": "x"}),
        )
        self.assertEqual(json.loads(proc.stdout.strip()), {})
        self.assertEqual(proc.stderr, "")

    def test_file_flag_reads_event_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "event.json"
            f.write_text(
                json.dumps(
                    {
                        "hook_event_name": "PostToolUse",
                        "toolResult": "A" * 40000,
                    }
                ),
                encoding="utf-8",
            )
            proc = self._run_full("", argv=["--file", str(f)])
            out = json.loads(proc.stdout.strip())
            self.assertIn("hookSpecificOutput", out)

    def test_file_missing_prints_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run_full(
                "", argv=["--file", str(Path(tmp) / "nope.json"), "--verbose"]
            )
            self.assertEqual(json.loads(proc.stdout.strip()), {})
            self.assertIn("unreadable --file", proc.stderr)

    def test_simulate_small_text_skips(self) -> None:
        proc = self._run_full("", argv=["--simulate", "tiny", "--verbose"])
        self.assertEqual(json.loads(proc.stdout.strip()), {})
        self.assertIn("below live-fat threshold", proc.stderr)

    def test_empty_and_bad_stdin(self) -> None:
        self.assertEqual(json.loads(self._run("")), {})
        self.assertEqual(json.loads(self._run("[1,2]")), {})

    def test_verdict_skip_and_compacted(self) -> None:
        """--verdict writes the slim compacted|skip verdict after each run."""
        with tempfile.TemporaryDirectory() as tmp:
            vpath = Path(tmp) / "v.json"
            proc = self._run_full(
                "",
                argv=["--simulate", "tiny", "--verdict", str(vpath)],
            )
            self.assertEqual(json.loads(proc.stdout.strip()), {})
            verdict = json.loads(vpath.read_text(encoding="utf-8"))
            self.assertEqual(verdict["verdict"], "skip")
            self.assertEqual(verdict["reason"], "below live-fat threshold")
            self.assertIn("ts", verdict)

            fat = "y" * 40000
            proc = self._run_full(
                json.dumps(
                    {"hook_event_name": "PostToolUse", "toolResult": fat}
                ),
                argv=["--verdict", str(vpath)],
            )
            self.assertIn("hookSpecificOutput", proc.stdout)
            verdict = json.loads(vpath.read_text(encoding="utf-8"))
            self.assertEqual(verdict["verdict"], "compacted")

            # bad stdin still writes a skip verdict (fail-open)
            vpath.unlink()
            proc = self._run_full("not json", argv=["--verdict", str(vpath)])
            self.assertEqual(json.loads(proc.stdout.strip()), {})
            verdict = json.loads(vpath.read_text(encoding="utf-8"))
            self.assertEqual(verdict["verdict"], "skip")
            self.assertEqual(verdict["reason"], "invalid JSON")

    def test_verdict_dash_streams_to_stderr_not_stdout(self) -> None:
        """--verdict - keeps stdout to the single hook payload and prints the
        slim verdict on stderr instead."""
        proc = self._run_full(
            "",
            argv=["--simulate", "tiny", "--verdict", "-"],
        )
        self.assertEqual(json.loads(proc.stdout.strip()), {})
        verdict = json.loads(proc.stderr.strip())
        self.assertEqual(verdict["verdict"], "skip")
        self.assertIn("ts", verdict)

    def test_verdict_bad_path_still_emits(self) -> None:
        proc = self._run_full(
            "",
            argv=[
                "--simulate",
                "tiny",
                "--verdict",
                "N:\\no\\such\\dir\\v.json",
            ],
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout.strip()), {})
        self.assertIn("--verdict failed", proc.stderr)

    def test_malformed_stdin_shapes_all_noop(self) -> None:
        """Fail-open contract: every malformed stdin shape exits 0 with {}."""
        for shape in (
            "",
            "not json",
            "null",
            "[]",
            '{"x":{}}',
            '{"prompt":123}',
            '{"prompt":null}',
            "   ",
            '{"prompt":""}',
            "{" + '"a":' * 50,  # truncated mid-JSON
        ):
            with self.subTest(shape=shape):
                self.assertEqual(json.loads(self._run(shape)), {})

    def test_non_post_event_noop(self) -> None:
        out = json.loads(self._run(json.dumps({"hook_event_name": "UserPromptSubmit", "toolResult": "x"})))
        self.assertEqual(out, {})

    def test_fat_toolresult_abridged_and_spilled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            spill = Path(tmp) / "spill"
            home.mkdir()
            fat = "A" * 40000
            out = json.loads(
                self._run(
                    json.dumps(
                        {
                            "hook_event_name": "PostToolUse",
                            "toolResult": {"output": fat},
                        }
                    ),
                    home=str(home),
                    spill=str(spill),
                )
            )
            node = out["hookSpecificOutput"]["updatedToolOutput"]
            self.assertLess(len(node["output"]), len(fat))
            self.assertIn("omitted", node["output"])
            self.assertTrue(any(spill.iterdir()))

    def test_env_prints_resolved_config(self) -> None:
        proc = self._run_full("", argv=["--env"])
        report = json.loads(proc.stdout)
        self.assertEqual(report["live_fat"], HOOK.C.LIVE_FAT)
        self.assertEqual(report["policy"], "default")
        self.assertIn("spill_max_files", report)

    def test_env_reflects_spill_disable_and_jq(self) -> None:
        import subprocess

        env = dict(os.environ)
        env["JEV_CONSULT_SPILL"] = "0"
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--env"],
            input="",
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertTrue(json.loads(proc.stdout)["spill_disabled"])
        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--env", "--jq", "live_fat"],
            input="",
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(json.loads(proc.stdout.strip()), HOOK.C.LIVE_FAT)

    def test_env_jq_bad_key_and_out_file(self) -> None:
        import subprocess

        proc = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--env", "--jq", "nope"],
            input="",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "env.json"
            proc = self._run_full("", argv=["--env", "--out", str(target)])
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["live_fat"],
                HOOK.C.LIVE_FAT,
            )


class DryRunTests(unittest.TestCase):
    """--dry-run keeps live spill dir untouched; spill goes under <dir>/dry/."""

    def _fat_event(self) -> dict:
        return {
            "hook_event_name": "PostToolUse",
            "toolResult": "HEAD" + ("n" * 40000) + "TAIL",
        }

    def test_handle_dry_run_spills_under_dry_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": tmp}):
                out = HOOK.handle(self._fat_event(), dry_run=True)
            live = [p for p in Path(tmp).iterdir() if p.is_file()]
            self.assertEqual(live, [])
            dry_files = list((Path(tmp) / "dry").glob("*.txt"))
            self.assertEqual(len(dry_files), 1)
            replaced = out["hookSpecificOutput"]["updatedToolOutput"]
            self.assertIn(str(Path(tmp) / "dry"), replaced)

    def test_handle_default_still_spills_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"JEV_CONSULT_SPILL": tmp}):
                HOOK.handle(self._fat_event())
            self.assertEqual(len(list(Path(tmp).glob("*.txt"))), 1)
            self.assertFalse((Path(tmp) / "dry").exists())

    def test_dry_run_disabled_spill_still_abridges(self) -> None:
        with patch.dict(os.environ, {"JEV_CONSULT_SPILL": "0"}):
            out = HOOK.handle(self._fat_event(), dry_run=True)
        replaced = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("chars omitted", replaced)
        self.assertNotIn("full output saved:", replaced)

    def test_cli_dry_run_and_simulate(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            spill = Path(tmp) / "spill"
            env = dict(os.environ)
            env.pop("TYPESAFE_API_KEY", None)
            env["JEV_CONSULT_LOG"] = "0"
            env["JEV_CONSULT_SPILL"] = str(spill)
            fat = "A" * 40000
            event = json.dumps(
                {"hook_event_name": "PostToolUse", "toolResult": fat}
            )
            proc = subprocess.run(
                [sys.executable, str(HOOK_PATH), "--dry-run"],
                input=event,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("hookSpecificOutput", proc.stdout)
            self.assertTrue((spill / "dry").is_dir())
            self.assertEqual(
                [p for p in spill.iterdir() if p.is_file()], []
            )
            # without --dry-run the live dir gets the file
            subprocess.run(
                [sys.executable, str(HOOK_PATH)],
                input=event,
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertTrue(any(p.suffix == ".txt" for p in spill.iterdir()))


class DebugFlagTests(unittest.TestCase):
    """--debug prints a one-line JSON diag record to stderr every run."""

    def _run_debug(self, stdin_text: str, argv: list):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        env["JEV_CONSULT_SPILL"] = "0"
        return subprocess.run(
            [sys.executable, str(HOOK_PATH), *argv],
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def _record(self, proc) -> dict:
        line = [
            ln
            for ln in proc.stderr.splitlines()
            if ln.startswith("compact_hook: {")
        ]
        self.assertEqual(len(line), 1, proc.stderr)
        return json.loads(line[0].split(": ", 1)[1])

    def test_debug_compact_records_lengths(self) -> None:
        fat = "B" * 40000
        proc = self._run_debug(
            json.dumps({"hook_event_name": "PostToolUse", "toolResult": fat}),
            ["--debug"],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rec = self._record(proc)
        self.assertEqual(rec["verdict"], "compacted")
        self.assertEqual(rec["in_chars"], len(fat))
        self.assertLess(rec["out_chars"], len(fat))
        self.assertIn("hookSpecificOutput", proc.stdout)

    def test_debug_skip_has_reason(self) -> None:
        proc = self._run_debug(
            json.dumps(
                {"hook_event_name": "PostToolUse", "toolResult": "tiny"}
            ),
            ["--debug"],
        )
        rec = self._record(proc)
        self.assertEqual(rec["verdict"], "skip")
        self.assertEqual(rec["reason"], "below live-fat threshold")
        self.assertEqual(rec["in_chars"], 4)

    def test_debug_bad_stdin_records_null_lens(self) -> None:
        proc = self._run_debug("not json", ["--debug"])
        rec = self._record(proc)
        self.assertEqual(rec["verdict"], "skip")
        self.assertEqual(rec["reason"], "invalid JSON")
        self.assertIsNone(rec["in_chars"])

    def test_no_debug_no_record(self) -> None:
        proc = self._run_debug("not json", [])
        self.assertNotIn("compact_hook: {", proc.stderr)


class SimulateEventTests(unittest.TestCase):
    """--simulate --event NAME overrides the synthetic hook event name."""

    def _sim(self, argv: list):
        import subprocess

        env = dict(os.environ)
        env.pop("TYPESAFE_API_KEY", None)
        env["JEV_CONSULT_LOG"] = "0"
        env["JEV_CONSULT_SPILL"] = "0"
        return subprocess.run(
            [sys.executable, str(HOOK_PATH), *argv],
            input="",
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_event_pretooluse_skips_with_reason(self) -> None:
        proc = self._sim(
            ["--simulate", "tiny", "--event", "PreToolUse", "--verbose"]
        )
        self.assertEqual(json.loads(proc.stdout.strip()), {})
        self.assertIn("not a PostToolUse event", proc.stderr)

    def test_event_default_unchanged(self) -> None:
        proc = self._sim(["--simulate", "tiny", "--verbose"])
        self.assertIn("below live-fat threshold", proc.stderr)


class SchemaFlagTests(unittest.TestCase):
    def _run(self, argv):
        import subprocess

        return subprocess.run(
            [sys.executable, str(HOOK_PATH), *argv],
            capture_output=True,
            text=True,
        )

    def test_schema_lists_payload_keys(self) -> None:
        proc = self._run(["--schema"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hookSpecificOutput.hookEventName", proc.stdout)
        self.assertIn("hookSpecificOutput.updatedToolOutput", proc.stdout)

    def test_schema_json_emits_object(self) -> None:
        proc = self._run(["--schema", "--json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        schema = json.loads(proc.stdout)
        self.assertTrue(schema["hookSpecificOutput.updatedToolOutput"]["required"])

    def test_schema_no_stdin_needed(self) -> None:
        proc = self._run(["--schema"])
        self.assertNotIn("hook", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
