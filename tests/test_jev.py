#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JEV_PATH = ROOT / "skills" / "jev-consult" / "scripts" / "jev.py"
INSTALL_PATH = ROOT / "scripts" / "install.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


jev = load(JEV_PATH, "jev")
install = load(INSTALL_PATH, "install_jev")


class ValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = jev.load_policy(str(ROOT / "skills" / "jev-consult" / "policy.json"))

    def test_choice_requires_two_options(self) -> None:
        with self.assertRaises(ValueError):
            jev.validate_questions(
                {
                    "where": {
                        "type": "choice",
                        "instructions": "Where?",
                        "criteria": {"none": "stop"},
                    }
                },
                self.policy,
            )

    def test_hatch_warning(self) -> None:
        warnings = jev.validate_questions(
            {
                "lib": {
                    "type": "choice",
                    "instructions": "Which library?",
                    "criteria": {"a": "A", "b": "B"},
                }
            },
            self.policy,
        )
        self.assertTrue(any("hatch" in item for item in warnings))

    def test_hard_cap(self) -> None:
        questions = {
            "q%s" % i: {"type": "noul", "instructions": "Yes?"}
            for i in range(33)
        }
        with self.assertRaises(ValueError):
            jev.validate_questions(questions, self.policy)

    def test_example_request_validates(self) -> None:
        example = json.loads(
            (ROOT / "skills" / "jev-consult" / "examples" / "jwt-auth.request.json").read_text(
                encoding="utf-8"
            )
        )
        warnings = jev.validate_questions(example["questions"], self.policy)
        self.assertEqual(warnings, [])


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = jev.load_policy(str(ROOT / "skills" / "jev-consult" / "policy.json"))

    def test_max_probability_not_threshold_on_all(self) -> None:
        answers = {
            "where": {
                "type": "choice",
                "choice": "new_module",
                "confidence": 0.8,
                "probabilities": {
                    "new_module": 0.62,
                    "extend_session": 0.28,
                    "none": 0.10,
                },
            }
        }
        decision = jev.decide(answers, self.policy, irreversible=False)
        self.assertEqual(decision["action"], "proceed")
        self.assertEqual(decision["picks"]["where"], "new_module")

    def test_low_confidence_escalates(self) -> None:
        answers = {
            "where": {
                "type": "choice",
                "choice": "new_module",
                "confidence": 0.2,
                "probabilities": {"new_module": 0.4, "none": 0.6},
            }
        }
        decision = jev.decide(answers, self.policy)
        self.assertEqual(decision["action"], "escalate")

    def test_noul_half_is_uncertain(self) -> None:
        answers = {"touch": {"type": "noul", "noul": 0.5}}
        reversible = jev.decide(answers, self.policy, irreversible=False)
        self.assertEqual(reversible["action"], "proceed")
        self.assertTrue(any("uncertain" in note for note in reversible["notes"]))
        irreversible = jev.decide(answers, self.policy, irreversible=True)
        self.assertEqual(irreversible["action"], "escalate")

    def test_tight_gap_irreversible_escalates(self) -> None:
        answers = {
            "where": {
                "type": "choice",
                "choice": "refactor",
                "confidence": 0.7,
                "probabilities": {"refactor": 0.51, "rewrite": 0.49},
            }
        }
        ok = jev.decide(answers, self.policy, irreversible=False)
        self.assertEqual(ok["action"], "proceed")
        bad = jev.decide(answers, self.policy, irreversible=True)
        self.assertEqual(bad["action"], "escalate")


class SecretTests(unittest.TestCase):
    def test_redact(self) -> None:
        leaked = jev.redact("Authorization: Bearer apikey_SECRETEXAMPLEVALUE")
        self.assertNotIn("SECRETEXAMPLEVALUE", leaked)
        self.assertIn("[REDACTED]", leaked)

    def test_load_api_key_message_has_no_value(self) -> None:
        old = os.environ.pop("TYPESAFE_API_KEY", None)
        old_hermes = os.environ.pop("HERMES_HOME", None)
        orig = jev.parse_env_file
        jev.parse_env_file = lambda path: {}
        try:
            with self.assertRaises(SystemExit) as ctx:
                jev.load_api_key()
            message = str(ctx.exception).lower()
            self.assertNotIn("apikey_", message)
            self.assertIn("typesafe_api_key is not set", message)
        finally:
            jev.parse_env_file = orig
            if old is not None:
                os.environ["TYPESAFE_API_KEY"] = old
            if old_hermes is not None:
                os.environ["HERMES_HOME"] = old_hermes


class InstallTests(unittest.TestCase):
    def test_refuses_other_agents(self) -> None:
        with self.assertRaises(SystemExit):
            install.parse_agents("cursor")
        with self.assertRaises(SystemExit):
            install.parse_agents("all")

    def test_default_four(self) -> None:
        self.assertEqual(
            install.parse_agents(None),
            ["hermes", "claude-code", "codex", "grok"],
        )

    def test_path_map_uses_hermes_home(self) -> None:
        mapping = install.targets(
            home=Path("C:/tmp/home"),
            hermes=Path("D:/Hermes/home"),
        )
        self.assertEqual(mapping["hermes"]["skills"][0], Path("D:/Hermes/home/skills"))
        self.assertTrue(
            any(path.parent.name == ".agents" for path in mapping["codex"]["skills"])
        )
        gemini_paths = json.dumps(
            {k: [str(p) for p in v["skills"]] for k, v in mapping.items()}
        )
        self.assertNotIn(".gemini", gemini_paths)
        self.assertNotIn(".cursor", gemini_paths)

    def test_repo_instruction_files_exist(self) -> None:
        for name in ("AGENTS.md", "CLAUDE.md", ".hermes.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("<!-- jev-consult:start -->", text)
            self.assertIn("skills/jev-consult/SKILL.md", text)

    def test_wrappers_exist(self) -> None:
        self.assertTrue((ROOT / "install.cmd").is_file())
        self.assertTrue((ROOT / "install.sh").is_file())

    def test_report_key_never_prints_value(self) -> None:
        old = os.environ.pop("TYPESAFE_API_KEY", None)
        try:
            os.environ["TYPESAFE_API_KEY"] = "apikey_SHOULD_NOT_APPEAR"
            self.assertTrue(install.key_is_set())
            from io import StringIO
            from contextlib import redirect_stdout

            buf = StringIO()
            with redirect_stdout(buf):
                install.report_key()
            out = buf.getvalue()
            self.assertIn("TYPESAFE_API_KEY: set", out)
            self.assertNotIn("apikey_SHOULD_NOT_APPEAR", out)
        finally:
            if old is None:
                os.environ.pop("TYPESAFE_API_KEY", None)
            else:
                os.environ["TYPESAFE_API_KEY"] = old


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)
