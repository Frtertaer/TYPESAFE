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

ROOT = Path(__file__).resolve().parent.parent


def load_inventory():
    path = ROOT / "skills" / "jev-consult" / "scripts" / "inventory.py"
    spec = importlib.util.spec_from_file_location("jev_inventory", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


inv = load_inventory()
FIXTURE = ROOT / "tests" / "fixtures" / "inventory-harness"


class InventoryTests(unittest.TestCase):
    def test_scan_fixture_skills_and_mcp_names_only(self) -> None:
        items = inv.scan("hermes", hermes=FIXTURE)
        names = {item["name"] for item in items}
        self.assertIn("jwt-auth", names)
        self.assertIn("ascii-art", names)
        self.assertIn("context7", names)
        self.assertIn("composio", names)
        self.assertIn("demo-plug", names)
        blob = json.dumps(items)
        self.assertNotIn("should-not-appear", blob)
        self.assertNotIn("TYPESAFE_API_KEY", blob)

    def test_shortlist_prefers_task_tokens(self) -> None:
        items = inv.scan("hermes", hermes=FIXTURE)
        picked = inv.shortlist(items, "Add JWT access tokens in Python", 8, [])
        names = [item["name"] for item in picked]
        self.assertIn("jwt-auth", names)
        self.assertNotIn("ascii-art", names)
        self.assertNotIn("authorized-scan", names)
        self.assertEqual(names[0], "jwt-auth")

    def test_rare_name_token_beats_common_access(self) -> None:
        items = [
            {"kind": "skill", "name": "implementing-jwt-signing", "description": "sign tokens", "id": "jwt"}
        ]
        items.extend(
            {
                "kind": "skill",
                "name": "credential-access-%s" % i,
                "description": "access logs",
                "id": "a%s" % i,
            }
            for i in range(9)
        )
        picked = inv.shortlist(items, "Add JWT access tokens in Python", 6, [])
        names = [item["name"] for item in picked]
        self.assertEqual(names, ["implementing-jwt-signing"])

    def test_include_pins_missed_name(self) -> None:
        items = inv.scan("hermes", hermes=FIXTURE)
        picked = inv.shortlist(items, "Add JWT access tokens in Python", 8, ["ascii-art"])
        names = [item["name"] for item in picked]
        self.assertIn("jwt-auth", names)
        self.assertIn("ascii-art", names)

    def test_write_ask_has_hatch_and_noul(self) -> None:
        items = inv.scan("hermes", hermes=FIXTURE)
        picked = inv.shortlist(items, "jwt", 8, [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ask.json"
            inv.write_ask(path, "jwt", "hermes", picked)
            payload = json.loads(path.read_text(encoding="utf-8"))
        criteria = payload["questions"]["load_tools"]["criteria"]
        self.assertIn("none", criteria)
        self.assertEqual(payload["questions"]["need_skill"]["type"], "noul")
        self.assertEqual(payload["questions"]["installed_enough"]["type"], "noul")

    def test_resolve_picker_winner_and_hatches(self) -> None:
        picked = [{"id": "skill_jwt_auth", "kind": "skill", "name": "jwt-auth"}]
        winner = inv.resolve_picker(
            picked,
            {"action": "proceed", "picks": {"load_tools": "skill_jwt_auth", "need_skill": 0.9}},
        )
        self.assertEqual(winner["status"], "winner")
        self.assertEqual(winner["winner"]["name"], "jwt-auth")
        none = inv.resolve_picker(
            picked,
            {"action": "proceed", "picks": {"load_tools": "none", "need_skill": 0.95}},
        )
        self.assertEqual(none["status"], "none")
        low = inv.resolve_picker(
            picked,
            {"action": "proceed", "picks": {"load_tools": "skill_jwt_auth", "need_skill": 0.2}},
        )
        self.assertEqual(low["status"], "none")
        unsure = inv.resolve_picker(
            picked,
            {"action": "proceed", "picks": {"load_tools": "skill_jwt_auth", "need_skill": 0.5}},
        )
        self.assertEqual(unsure["status"], "escalate")
        bad = inv.resolve_picker(picked, {"action": "escalate", "picks": {}})
        self.assertEqual(bad["status"], "escalate")

    def test_resolve_picker_strong_pick(self) -> None:
        picked = [{"id": "skill_jwt_auth", "kind": "skill", "name": "jwt-auth"}]
        strong = inv.resolve_picker(
            picked,
            {
                "action": "proceed",
                "picks": {"load_tools": "skill_jwt_auth", "need_skill": 0.37},
                "probabilities": {"load_tools": {"skill_jwt_auth": 1.0, "none": 0.0}},
            },
        )
        self.assertEqual(strong["status"], "winner")
        self.assertTrue(strong.get("strong"))
        weak_probs = inv.resolve_picker(
            picked,
            {
                "action": "proceed",
                "picks": {"load_tools": "skill_jwt_auth", "need_skill": 0.5},
                "probabilities": {"load_tools": {"skill_jwt_auth": 0.6, "none": 0.4}},
            },
        )
        self.assertEqual(weak_probs["status"], "escalate")
        high_need = inv.resolve_picker(
            picked,
            {"action": "proceed", "picks": {"load_tools": "skill_jwt_auth", "need_skill": 0.8}},
        )
        self.assertEqual(high_need["status"], "winner")
        self.assertNotIn("strong", high_need)
        strict = inv.resolve_picker(
            picked,
            {"action": "proceed", "picks": {"load_tools": "skill_jwt_auth", "need_skill": 0.8}},
            policy={"noul_yes": 0.9},
        )
        self.assertEqual(strict["status"], "escalate")

    def test_format_winner_note(self) -> None:
        note = inv.format_winner_note({"kind": "skill", "name": "jwt-auth"})
        self.assertIn("<skill_relevance>", note)
        self.assertIn("jwt-auth", note)

    def test_catalogs_flag(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(["--catalogs"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("https://skills.sh", out)
        self.assertIn("smithery", out)

    def test_watch_ticks_emit_jsonl(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout
        from unittest.mock import patch

        buf = StringIO()
        with patch.dict(os.environ, {"JEV_INV_WATCH_MAX": "2"}):
            with redirect_stdout(buf):
                code = inv.main(
                    [
                        "--harness",
                        "hermes",
                        "--hermes-home",
                        str(FIXTURE),
                        "--watch",
                        "0.01",
                    ]
                )
        self.assertEqual(code, 0)
        lines = buf.getvalue().splitlines()
        ticks = [json.loads(l) for l in lines[1:] if l.startswith('{"ts"')]
        self.assertEqual(len(ticks), 2)
        self.assertIn("counts", ticks[0])
        self.assertGreater(ticks[0]["counts"]["skill"], 0)
        self.assertIn("shortlist", ticks[0])

    def test_watch_ticks_report_added_removed(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout
        from unittest.mock import patch

        results = [
            [{"id": "a", "kind": "skill", "name": "a"}],
            [{"id": "b", "kind": "skill", "name": "b"},
             {"id": "c", "kind": "plugin", "name": "c"}],
        ]

        def fake_scan(harness, home=None, hermes=None):
            if results:
                return results.pop(0)
            return []

        buf = StringIO()
        with patch.dict(os.environ, {"JEV_INV_WATCH_MAX": "2"}):
            with patch.object(inv, "scan", side_effect=fake_scan):
                with redirect_stdout(buf):
                    code = inv.main(
                        [
                            "--harness",
                            "hermes",
                            "--hermes-home",
                            str(FIXTURE),
                            "--watch",
                            "0.01",
                        ]
                    )
        self.assertEqual(code, 0)
        ticks = [
            json.loads(l)
            for l in buf.getvalue().splitlines()
            if l.startswith('{"ts"')
        ]
        self.assertEqual(len(ticks), 2)
        self.assertNotIn("added", ticks[0])
        self.assertNotIn("removed", ticks[0])
        self.assertEqual(ticks[1]["added"], [])
        self.assertEqual(ticks[1]["removed"], ["b", "c"])

    def test_cli_json_shortlist(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--task",
                    "jwt tokens",
                    "--limit",
                    "8",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        names = [item["name"] for item in payload["shortlist"]]
        self.assertIn("jwt-auth", names)
        self.assertNotIn("ascii-art", names)

    def test_cli_csv_shortlist(self) -> None:
        import csv as _csv
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--task",
                    "jwt tokens",
                    "--limit",
                    "8",
                    "--csv",
                ]
            )
        self.assertEqual(code, 0)
        rows = list(_csv.reader(StringIO(buf.getvalue())))
        self.assertEqual(rows[0], ["id", "kind", "name"])
        names = [row[2] for row in rows[1:]]
        self.assertIn("jwt-auth", names)
        self.assertNotIn("ascii-art", names)

    def test_cli_grep_filters_items(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--grep",
                    "jwt",
                    "--task",
                    "jwt tokens",
                    "--limit",
                    "8",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        names = [item["name"] for item in payload["shortlist"]]
        self.assertIn("jwt-auth", names)
        self.assertNotIn("ascii-art", names)
        self.assertEqual(payload["counts"]["skill"], 1)

    def test_cli_grep_lists_matches_without_task(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--grep",
                    "jwt",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        names = [item["name"] for item in payload["shortlist"]]
        self.assertEqual(names, ["jwt-auth"])

    def test_cli_paths_prints_item_paths(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--task",
                    "jwt tokens",
                    "--paths",
                ]
            )
        self.assertEqual(code, 0)
        lines = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip()]
        self.assertTrue(lines)
        self.assertTrue(any("jwt-auth" in ln for ln in lines))

    def test_cli_id_prints_matching_item(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--id",
                    "jwt-auth",
                ]
            )
        self.assertEqual(code, 0)
        item = json.loads(buf.getvalue())
        self.assertEqual(item["name"], "jwt-auth")
        err = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(err):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--id",
                    "nope-missing",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("nope-missing", err.getvalue())

    def test_cli_out_writes_payload_file(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "payload.json"
            with redirect_stdout(StringIO()):
                code = inv.main(
                    [
                        "--harness",
                        "hermes",
                        "--hermes-home",
                        str(FIXTURE),
                        "--task",
                        "jwt tokens",
                        "--limit",
                        "8",
                        "--out",
                        str(out_path),
                    ]
                )
            self.assertEqual(code, 0)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            names = [item["name"] for item in payload["shortlist"]]
            self.assertIn("jwt-auth", names)

    def test_cli_names_prints_bare_ids(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--task",
                    "jwt tokens",
                    "--limit",
                    "8",
                    "--names",
                ]
            )
        self.assertEqual(code, 0)
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertIn("skill_jwt_auth", lines)
        self.assertTrue(all(" " not in l for l in lines))
        self.assertNotIn("{", buf.getvalue())

    def test_cli_kind_filter(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--kind",
                    "mcp",
                    "--limit",
                    "8",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(all(i["kind"] == "mcp" for i in payload["shortlist"]))

    def test_cli_task_env_default(self) -> None:
        import io
        from contextlib import redirect_stdout
        from unittest.mock import patch

        buf = io.StringIO()
        with patch.dict(os.environ, {"JEV_TASK": "jwt tokens"}):
            with redirect_stdout(buf):
                code = inv.main(
                    [
                        "--harness",
                        "hermes",
                        "--hermes-home",
                        str(FIXTURE),
                        "--limit",
                        "8",
                    ]
                )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["task"], "jwt tokens")
        names = [item["name"] for item in payload["shortlist"]]
        self.assertIn("jwt-auth", names)

    def test_cli_explain_adds_matched_tokens(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--task",
                    "jwt tokens",
                    "--limit",
                    "8",
                    "--explain",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        by_name = {item["name"]: item for item in payload["shortlist"]}
        self.assertIn("jwt", by_name["jwt-auth"]["matched"])

    def _write_skill(self, tmp: str, name: str, frontmatter: str) -> Path:
        skill = Path(tmp) / name
        skill.mkdir(parents=True)
        path = skill / "SKILL.md"
        path.write_text(frontmatter, encoding="utf-8")
        return path

    def test_parse_frontmatter_plain_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_skill(
                tmp, "s1", "---\nname: s1\ndescription: Plain words here\n---\n"
            )
            meta = inv.parse_frontmatter(path)
        self.assertEqual(meta["description"], "Plain words here")

    def test_parse_frontmatter_double_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_skill(
                tmp, "s1", '---\nname: s1\ndescription: "Quoted: desc"\n---\n'
            )
            meta = inv.parse_frontmatter(path)
        self.assertEqual(meta["description"], "Quoted: desc")

    def test_parse_frontmatter_single_quoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_skill(
                tmp, "s1", "---\nname: s1\ndescription: 'Single ''q'' desc'\n---\n"
            )
            meta = inv.parse_frontmatter(path)
        self.assertEqual(meta["description"], "Single 'q' desc")

    def test_parse_frontmatter_block_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_skill(
                tmp,
                "s1",
                "---\nname: s1\ndescription: >-\n  First line\n  second line\n---\n",
            )
            meta = inv.parse_frontmatter(path)
        self.assertEqual(meta["description"], "First line second line")
        self.assertNotIn(meta["description"], (">-", ">", "|", "|-", ">+", "|+"))

    def test_parse_frontmatter_literal_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_skill(
                tmp,
                "s1",
                "---\nname: s1\ndescription: |\n  First line\n  second line\n---\n",
            )
            meta = inv.parse_frontmatter(path)
        self.assertEqual(meta["description"], "First line second line")

    def test_parse_frontmatter_plain_with_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_skill(
                tmp,
                "s1",
                "---\nname: s1\ndescription: First part\n  continued here\n---\n",
            )
            meta = inv.parse_frontmatter(path)
        self.assertEqual(meta["description"], "First part continued here")

    def test_check_sidecar_reports_age(self) -> None:
        import re
        import time as time_mod
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            path.write_text(
                json.dumps({"written_at": time_mod.time() - 10}), encoding="utf-8"
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--check-sidecar", str(path)])
            self.assertEqual(code, 0)
            out = buf.getvalue().strip()
            self.assertRegex(out, r"^fresh \(age \d+s\)$")
            self.assertGreaterEqual(int(re.search(r"age (\d+)s", out).group(1)), 9)

    def test_check_sidecar_missing_no_age(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--check-sidecar", str(Path(tmp) / "none.json")])
            self.assertEqual(code, 0)
            self.assertEqual(buf.getvalue().strip(), "missing")

    def test_check_sidecar_stale_reports_age(self) -> None:
        import time as time_mod
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            path.write_text(
                json.dumps({"written_at": time_mod.time() - 999999}), encoding="utf-8"
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--check-sidecar", str(path)])
            self.assertEqual(code, 0)
            self.assertTrue(buf.getvalue().strip().startswith("stale (age "))

    def test_check_sidecar_ttl_override(self) -> None:
        import time as time_mod
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            path.write_text(
                json.dumps({"written_at": time_mod.time() - 10}), encoding="utf-8"
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--check-sidecar", str(path), "--ttl", "5"])
            self.assertEqual(code, 0)
            self.assertTrue(buf.getvalue().strip().startswith("stale"))
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--check-sidecar", str(path), "--ttl", "99999"])
            self.assertEqual(code, 0)
            self.assertTrue(buf.getvalue().strip().startswith("fresh"))

    def test_prune_sidecars_ttl_override(self) -> None:
        import time as time_mod
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".jev-tools.json"
            path.write_text(
                json.dumps({"written_at": time_mod.time() - 10}), encoding="utf-8"
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--prune-sidecars", str(root), "--ttl", "5"])
            self.assertEqual(code, 0)
            self.assertIn("pruned 1", buf.getvalue())
            self.assertFalse(path.exists())

    def test_show_ttl_override(self) -> None:
        import time as time_mod
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            path.write_text(
                json.dumps({"written_at": time_mod.time() - 10}), encoding="utf-8"
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--show", str(path), "--ttl", "5"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["status"], "stale")

    def test_check_sidecar_dir_lists_all(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout
        import time as time_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / ".jev-tools.json").write_text(
                json.dumps({"written_at": time_mod.time()}), encoding="utf-8"
            )
            (root / ".jev-tools-miss.json").write_text("{bad", encoding="utf-8")
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--check-sidecar", str(root)])
            self.assertEqual(code, 0)
            out = buf.getvalue()
            self.assertIn("fresh:", out)
            self.assertIn("invalid:", out)

    def test_check_sidecar_dir_empty(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--check-sidecar", tmp])
            self.assertEqual(code, 0)
            self.assertIn("no sidecars under", buf.getvalue())

    def test_show_policy_prints_policy_json(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(["--show-policy"])
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("sidecar_ttl_seconds", data)
        self.assertIn("version", data)

    def test_show_includes_age_seconds(self) -> None:
        import time as time_mod
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            path.write_text(
                json.dumps({"written_at": time_mod.time() - 30, "task": "t"}),
                encoding="utf-8",
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--show", str(path)])
            self.assertEqual(code, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["status"], "fresh")
            self.assertGreaterEqual(out["age_seconds"], 29)

    def test_show_missing_no_age(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--show", str(Path(tmp) / "none.json")])
            self.assertEqual(code, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["status"], "missing")
            self.assertNotIn("age_seconds", out)

    def test_prune_sidecars_dry_run_keeps_files(self) -> None:
        import time as time_mod
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            stale = d / ".jev-tools.json"
            stale.write_text(
                json.dumps({"written_at": time_mod.time() - 999999}), encoding="utf-8"
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--prune-sidecars", str(d), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertTrue(stale.is_file())
            self.assertIn("would prune", buf.getvalue())

            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--prune-sidecars", str(d)])
            self.assertEqual(code, 0)
            self.assertFalse(stale.exists())
            self.assertIn("pruned 1 stale sidecars", buf.getvalue())

    def test_picker_request_has_untrusted_rule(self) -> None:
        payload = inv.picker_request(
            "task", "hermes", [{"id": "x", "kind": "skill", "name": "jwt-auth"}]
        )
        instructions = payload["questions"]["load_tools"]["instructions"]
        self.assertTrue(instructions.startswith(inv.UNTRUSTED_RULE))

    def test_explicit_only_excluded_unless_named(self) -> None:
        items = [
            {
                "kind": "skill",
                "name": "jwt-auth",
                "description": "JWT tokens access",
                "id": "skill_jwt",
                "explicit_only": True,
            },
            {
                "kind": "skill",
                "name": "ascii-art",
                "description": "banners",
                "id": "skill_ascii",
                "explicit_only": False,
            },
        ]
        picked = inv.shortlist(items, "Add JWT access tokens", 8, [])
        self.assertNotIn("jwt-auth", [i["name"] for i in picked])
        picked = inv.shortlist(items, "Add JWT access tokens", 8, ["jwt-auth"])
        self.assertIn("jwt-auth", [i["name"] for i in picked])

    def test_explicit_only_detected_from_openai_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = Path(tmp) / "skills" / "closed-skill"
            (skill / "agents").mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: closed-skill\ndescription: hidden\n---\n", encoding="utf-8"
            )
            (skill / "agents" / "openai.yaml").write_text(
                "allow_implicit_invocation: false\n", encoding="utf-8"
            )
            items = inv.iter_skills([Path(tmp) / "skills"])
        item = next(i for i in items if i["name"] == "closed-skill")
        self.assertTrue(item["explicit_only"])

    def test_explicit_mentions_dollar_name(self) -> None:
        items = [
            {"kind": "skill", "name": "hyperframes", "id": "s1"},
            {"kind": "skill", "name": "hyperframes-cli", "id": "s2", "explicit_only": True},
        ]
        hits = inv.explicit_mentions("use $hyperframes-cli to render", items)
        self.assertEqual([h["name"] for h in hits], ["hyperframes-cli"])

    def test_explicit_mentions_common_word_not_explicit(self) -> None:
        items = [{"kind": "mcp", "name": "github", "id": "m_gh"}]
        self.assertEqual(inv.explicit_mentions("look at this github repo", items), [])

    def test_explicit_mentions_dollar_common_word(self) -> None:
        items = [{"kind": "mcp", "name": "github", "id": "m_gh"}]
        hits = inv.explicit_mentions("use $github for the PR", items)
        self.assertEqual([h["name"] for h in hits], ["github"])

    def test_explicit_mentions_bare_single_word_skill_no_match(self) -> None:
        items = [
            {"kind": "skill", "name": "hyperframes", "id": "s1"},
            {"kind": "skill", "name": "hyperframes-cli", "id": "s2"},
        ]
        self.assertEqual(inv.explicit_mentions("use hyperframes to render", items), [])

    def test_explicit_mentions_bare_slug(self) -> None:
        items = [
            {"kind": "skill", "name": "hyperframes", "id": "s1"},
            {"kind": "skill", "name": "hyperframes-cli", "id": "s2"},
        ]
        hits = inv.explicit_mentions("use hyperframes-cli to render", items)
        self.assertEqual([h["name"] for h in hits], ["hyperframes-cli"])

    def test_explicit_mentions_no_match(self) -> None:
        items = [{"kind": "skill", "name": "hyperframes", "id": "s1"}]
        self.assertEqual(inv.explicit_mentions("render frames", items), [])

    def test_skill_item_has_path(self) -> None:
        items = inv.scan("hermes", hermes=FIXTURE)
        jwt = next(item for item in items if item["name"] == "jwt-auth")
        self.assertEqual(Path(jwt["path"]).name, "jwt-auth")
        self.assertTrue((Path(jwt["path"]) / "SKILL.md").is_file())


class ScoresFlagTests(unittest.TestCase):
    def test_scores_adds_score_field(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness", "hermes",
                    "--hermes-home", str(FIXTURE),
                    "--home", str(FIXTURE),
                    "--task", "Add JWT access tokens in Python",
                    "--scores",
                ]
            )
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        names = {item["name"]: item for item in data["shortlist"]}
        self.assertIn("jwt-auth", names)
        self.assertGreater(names["jwt-auth"]["score"], 0)

    def test_no_scores_omits_field(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness", "hermes",
                    "--hermes-home", str(FIXTURE),
                    "--home", str(FIXTURE),
                    "--task", "Add JWT access tokens in Python",
                ]
            )
        self.assertEqual(code, 0)
        data = json.loads(buf.getvalue())
        self.assertTrue(all("score" not in item for item in data["shortlist"]))


class TtlEnvOverrideTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_TTL": "30"}):
            self.assertEqual(inv.sidecar_ttl_seconds(), 30.0)

    def test_env_invalid_falls_back_to_policy(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_TTL": "bogus"}):
            self.assertNotEqual(inv.sidecar_ttl_seconds(), 0.0)

    def test_env_negative_ignored(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_TTL": "-5"}):
            self.assertEqual(inv.sidecar_ttl_seconds(), inv._policy_float_key("sidecar_ttl_seconds", 0.0))

    def test_env_zero_disables_freshness(self) -> None:
        import os
        import time as time_mod
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_TTL": "0"}):
            prior = {"written_at": time_mod.time() - 99999}
            self.assertFalse(inv.sidecar_fresh(prior))

    def test_dedupe_ttl_env_override(self) -> None:
        import os
        from unittest.mock import patch

        self.assertEqual(inv.hook_dedupe_ttl_seconds(), 0.0)
        with patch.dict(os.environ, {"JEV_HOOK_DEDUPE_TTL": "45"}):
            self.assertEqual(inv.hook_dedupe_ttl_seconds(), 45.0)
        with patch.dict(os.environ, {"JEV_HOOK_DEDUPE_TTL": "bogus"}):
            self.assertEqual(
                inv.hook_dedupe_ttl_seconds(),
                inv._policy_float_key("dedupe_ttl_seconds", 0.0),
            )


class SidecarAgeTests(unittest.TestCase):
    def test_age_seconds(self) -> None:
        self.assertEqual(inv.sidecar_age_seconds({"written_at": 100.0}, now=140.0), 40.0)
        self.assertEqual(inv.sidecar_age_seconds({"written_at": 140.0}, now=100.0), 0.0)
        self.assertIsNone(inv.sidecar_age_seconds({}, now=100.0))
        self.assertIsNone(inv.sidecar_age_seconds({"written_at": "x"}, now=100.0))
        self.assertIsNone(inv.sidecar_age_seconds({"written_at": True}, now=100.0))


class JevTimeoutEnvTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_TIMEOUT": "1.5"}):
            self.assertEqual(inv.hook_jev_timeout_seconds(), 1.5)

    def test_env_invalid_falls_back(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_TIMEOUT": "bogus"}):
            self.assertEqual(
                inv.hook_jev_timeout_seconds(),
                inv._policy_float_key("hook_jev_timeout_seconds", 8.0),
            )

    def test_env_negative_ignored(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_TIMEOUT": "-2"}):
            self.assertEqual(
                inv.hook_jev_timeout_seconds(),
                inv._policy_float_key("hook_jev_timeout_seconds", 8.0),
            )


class HookLimitEnvTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_LIMIT": "3"}):
            self.assertEqual(inv.hook_limit(), 3)

    def test_env_invalid_and_zero_fall_back(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_LIMIT": "bogus"}):
            self.assertGreaterEqual(inv.hook_limit(), 1)
        with patch.dict(os.environ, {"JEV_HOOK_LIMIT": "0"}):
            self.assertGreaterEqual(inv.hook_limit(), 1)


class LimitEnvTests(unittest.TestCase):
    def _run_main(self, env):
        import io
        import json as _json
        from contextlib import redirect_stdout
        from unittest.mock import patch

        buf = io.StringIO()
        with patch.dict(os.environ, env):
            with redirect_stdout(buf):
                rc = inv.main(
                    [
                        "--harness",
                        "hermes",
                        "--hermes-home",
                        str(FIXTURE),
                        "--task",
                        "jwt",
                    ]
                )
        self.assertEqual(rc, 0)
        return _json.loads(buf.getvalue())

    def test_env_override_changes_default(self) -> None:
        out = self._run_main({"JEV_LIMIT": "1"})
        self.assertLessEqual(len(out["shortlist"]), 1)

    def test_env_invalid_falls_back(self) -> None:
        out = self._run_main({"JEV_LIMIT": "bogus"})
        self.assertGreaterEqual(len(out["shortlist"]), 1)


class HookBudgetEnvTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_BUDGET": "2.5"}):
            self.assertEqual(inv.hook_budget_seconds(), 2.5)

    def test_env_invalid_falls_back(self) -> None:
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_BUDGET": "bogus"}):
            self.assertEqual(
                inv.hook_budget_seconds(),
                inv._policy_float_key("hook_budget_seconds", 12.0),
            )


class HookRetriesEnvTests(unittest.TestCase):
    def test_env_override_wins(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_RETRIES": "2"}):
            self.assertEqual(inv.hook_jev_retries(), 2)

    def test_env_invalid_falls_back(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"JEV_HOOK_RETRIES": "bogus"}):
            self.assertGreaterEqual(inv.hook_jev_retries(), 0)

    def test_cli_show_reports_issues_on_malformed_sidecar(self) -> None:
        import tempfile
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            path.write_text(json.dumps({"names": [{"name": "x"}]}), encoding="utf-8")
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--show", str(path)])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["valid"])
        self.assertTrue(any("written_at" in issue for issue in payload["issues"]))
        self.assertTrue(any("entry 0 missing kind" == issue for issue in payload["issues"]))

    def test_cli_show_valid_sidecar(self) -> None:
        import tempfile
        from io import StringIO
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".jev-tools.json"
            path.write_text(
                json.dumps(
                    {
                        "written_at": 1234,
                        "names": [{"kind": "skill", "name": "x"}],
                    }
                ),
                encoding="utf-8",
            )
            buf = StringIO()
            with redirect_stdout(buf):
                code = inv.main(["--show", str(path)])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["issues"], [])

    def test_cli_jsonl_emits_one_item_per_line(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness", "hermes",
                    "--hermes-home", str(FIXTURE),
                    "--task", "jwt",
                    "--jsonl",
                ]
            )
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "skill_jwt_auth")
        self.assertEqual(rows[0]["name"], "jwt-auth")

    def test_cli_kinds_prints_per_kind_counts(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                ["--harness", "hermes", "--hermes-home", str(FIXTURE), "--kinds"]
            )
        self.assertEqual(code, 0)
        rows = dict(
            line.split() for line in buf.getvalue().strip().splitlines()
        )
        self.assertEqual(sum(int(v) for v in rows.values()), 6)
        self.assertIn("skill", rows)

    def test_cli_count_prints_picked_over_scanned(self) -> None:
        from io import StringIO
        from contextlib import redirect_stdout

        buf = StringIO()
        with redirect_stdout(buf):
            code = inv.main(
                [
                    "--harness",
                    "hermes",
                    "--hermes-home",
                    str(FIXTURE),
                    "--task",
                    "jwt tokens",
                    "--count",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().strip(), "1/6")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)
