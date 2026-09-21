#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

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

    def test_question_hard_max_v3_beats_nested(self) -> None:
        policy = dict(self.policy)
        policy["question_hard_max"] = 2
        policy["hard_max_questions"] = 99
        questions = {
            "a": {"type": "noul", "instructions": "A?"},
            "b": {"type": "noul", "instructions": "B?"},
            "c": {"type": "noul", "instructions": "C?"},
        }
        with self.assertRaises(ValueError):
            jev.validate_questions(questions, policy)

    def test_example_request_validates(self) -> None:
        example = json.loads(
            (ROOT / "skills" / "jev-consult" / "examples" / "jwt-auth.request.json").read_text(
                encoding="utf-8"
            )
        )
        warnings = jev.validate_questions(example["questions"], self.policy)
        self.assertEqual(warnings, [])

    def test_guard_examples_validate(self) -> None:
        folder = ROOT / "skills" / "jev-consult" / "examples"
        found = list(folder.glob("*.request.json"))
        self.assertGreaterEqual(len(found), 5)
        for path in found:
            payload = json.loads(path.read_text(encoding="utf-8"))
            warnings = jev.validate_questions(payload["questions"], self.policy)
            self.assertEqual(warnings, [], msg=path.name)


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

    def test_v3_tight_gap_used_not_nested_015(self) -> None:
        answers = {
            "where": {
                "type": "choice",
                "choice": "refactor",
                "confidence": 0.7,
                "probabilities": {"refactor": 0.55, "rewrite": 0.45},
            }
        }
        decision = jev.decide(answers, self.policy, irreversible=True)
        self.assertEqual(decision["action"], "proceed")

    def test_decide_returns_probabilities_per_choice(self) -> None:
        answers = {
            "where": {
                "type": "choice",
                "choice": "new_module",
                "confidence": 0.8,
                "probabilities": {"new_module": 0.8, "none": 0.2},
            },
            "sure": {"type": "noul", "noul": 0.9},
        }
        decision = jev.decide(answers, self.policy)
        self.assertEqual(
            decision["probabilities"], {"where": {"new_module": 0.8, "none": 0.2}}
        )


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
            self.assertIn("Jev decides", text)

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


class PolicyTests(unittest.TestCase):
    def test_role_is_decision_maker(self) -> None:
        policy = jev.load_policy(str(ROOT / "skills" / "jev-consult" / "policy.json"))
        self.assertEqual(policy.get("role"), "decision_maker")
        self.assertEqual(policy.get("default"), "ask_jev")
        self.assertIn("keep_vs_change", policy.get("must_ask", []))
        self.assertIn("approach", policy.get("must_ask", []))
        self.assertEqual(policy.get("version"), 3)
        for key in (
            "on_track",
            "stuck_move",
            "unknown_move",
            "load_tools",
            "installed_enough",
            "market_tools",
        ):
            self.assertIn(key, policy.get("must_ask", []))
        self.assertIn("on_track", policy.get("templates", {}))
        self.assertIn("load_tools", policy.get("templates", {}))

    def test_skill_says_jev_decides(self) -> None:
        text = (ROOT / "skills" / "jev-consult" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Jev decides", text)
        self.assertIn("every coding or planning task", text.lower())
        self.assertIn("Don't know", text)
        self.assertIn("Don't stall", text)
        self.assertIn(".jev-trace.json", text)
        self.assertIn("compact.py", text)
        self.assertIn("no watchdog", text.lower())
        self.assertIn("inventory.py", text)
        self.assertIn("peer_fill.py", text)
        self.assertIn("catalog_fill.py", text)
        self.assertIn("apply_fill.py", text)
        self.assertIn("not** maximally eliminate", text)
        self.assertIn("Do not install", text)

    def test_install_snippets_say_jev_decides(self) -> None:
        self.assertIn("Jev decides", install.SNIPPET)
        self.assertIn("Jev decides", install.REPO_SNIPPET)
        self.assertIn("inventory.py", install.SNIPPET)
        self.assertIn("inventory.py", install.REPO_SNIPPET)
        self.assertIn("peer_fill.py", install.SNIPPET)
        self.assertIn("peer_fill.py", install.REPO_SNIPPET)
        self.assertIn("catalog_fill.py", install.SNIPPET)
        self.assertIn("catalog_fill.py", install.REPO_SNIPPET)
        self.assertIn("apply_fill.py", install.SNIPPET)
        self.assertIn("apply_fill.py", install.REPO_SNIPPET)
        self.assertIn("Hook never auto-installs", install.SNIPPET)
        self.assertIn(".jev-trace.json", install.SNIPPET)
        self.assertIn(".jev-trace.json", install.REPO_SNIPPET)

    def test_extra_env_files_include_default_hermes(self) -> None:
        paths = [str(path).replace("\\", "/") for path in jev.extra_env_files()]
        self.assertTrue(any(item.endswith("D:/Hermes/home/.env") or item.endswith("D:/Hermes/home/.env") for item in paths) or any("Hermes/home/.env" in item for item in paths))


class ScaffoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = jev.load_policy(str(ROOT / "skills" / "jev-consult" / "policy.json"))

    def test_scaffold_keep_vs_change_has_hatch(self) -> None:
        payload = jev.scaffold_request(self.policy, ["keep_vs_change"], {"plan": "x"})
        criteria = payload["questions"]["keep_vs_change"]["criteria"]
        self.assertIn("keep", criteria)
        self.assertIn("none", criteria)
        self.assertEqual(payload["state"]["plan"], "x")
        self.assertEqual(jev.validate_questions(payload["questions"], self.policy), [])

    def test_scaffold_empty_criteria_needs_options(self) -> None:
        with self.assertRaises(ValueError):
            jev.scaffold_request(self.policy, ["architecture"], {"plan": "x"})

    def test_scaffold_empty_criteria_with_options(self) -> None:
        extra = {"architecture": {"hooks_json": "write Codex hooks.json"}}
        payload = jev.scaffold_request(self.policy, ["architecture"], {"plan": "x"}, extra)
        criteria = payload["questions"]["architecture"]["criteria"]
        self.assertIn("hooks_json", criteria)
        self.assertIn("none", criteria)
        self.assertEqual(jev.validate_questions(payload["questions"], self.policy), [])

    def test_scaffold_unknown_template(self) -> None:
        with self.assertRaises(ValueError):
            jev.scaffold_request(self.policy, ["not_a_template"], {})

    def test_parse_option(self) -> None:
        self.assertEqual(
            jev.parse_option("architecture=hooks_json:write Codex hooks.json"),
            ("architecture", "hooks_json", "write Codex hooks.json"),
        )

    def test_cmd_scaffold_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "req.json"
            rc = jev.main(
                ["scaffold", "keep_vs_change", "--out", str(out), "--plan", "ship pack"]
            )
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("keep_vs_change", data["questions"])
            self.assertEqual(data["state"]["plan"], "ship pack")

    def test_guard_no_decision_action(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "jev-consult" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("decision.action", agents)
        self.assertIn("scaffold", agents)
        self.assertIn("decision.action", skill)
        self.assertIn("scaffold", skill)
        self.assertIn("decision.action", install.REPO_SNIPPET)
        self.assertIn("scaffold", install.REPO_SNIPPET)
        self.assertIn("decision.action", install.SNIPPET)
        self.assertIn("scaffold", install.SNIPPET)


class _FakeResponse:
    def __init__(self, body: dict) -> None:
        self._raw = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


class _FakeOpener:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def open(self, _request, timeout=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.typesafe.ai/v1/systemone",
        code,
        "err",
        None,
        io.BytesIO(b"upstream"),
    )


class PostSystemoneTests(unittest.TestCase):
    QUESTIONS = {
        "pick": {
            "type": "choice",
            "instructions": "Which?",
            "criteria": {"a": "A", "b": "B", "none": "n"},
        },
        "sure": {"type": "noul", "instructions": "Sure?"},
    }
    GOOD = {
        "model": "jev-test",
        "answers": {
            "pick": {
                "type": "choice",
                "choice": "a",
                "confidence": 0.9,
                "probabilities": {"a": 0.9, "b": 0.05, "none": 0.05},
            },
            "sure": {"type": "noul", "noul": 0.8},
        },
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }

    def _post(self, outcomes, state=None, questions=None, **kwargs):
        opener = _FakeOpener(outcomes)
        env = {"TYPESAFE_API_KEY": "dummy-test-key-not-a-real-secret"}
        with patch.dict(os.environ, env), patch.object(
            jev, "load_api_key", return_value=env["TYPESAFE_API_KEY"]
        ), patch("urllib.request.build_opener", return_value=opener), patch.object(
            jev.time, "sleep"
        ):
            result = jev.post_systemone(
                state if state is not None else {"task": "t"},
                questions if questions is not None else self.QUESTIONS,
                {},
                **kwargs,
            )
        return result, opener

    def test_valid_response_passes_unchanged(self) -> None:
        result, opener = self._post([self.GOOD])
        self.assertEqual(opener.calls, 1)
        self.assertEqual(result["answers"], self.GOOD["answers"])
        self.assertEqual(result["model"], "jev-test")
        self.assertEqual(result["usage"], self.GOOD["usage"])

    def test_choice_not_in_criteria_blocked(self) -> None:
        bad = json.loads(json.dumps(self.GOOD))
        bad["answers"]["pick"]["choice"] = "bogus"
        with self.assertRaises(SystemExit):
            self._post([bad])

    def test_probabilities_sum_blocked(self) -> None:
        bad = json.loads(json.dumps(self.GOOD))
        bad["answers"]["pick"]["probabilities"] = {"a": 0.6, "b": 0.1, "none": 0.1}
        with self.assertRaises(SystemExit):
            self._post([bad])

    def test_noul_out_of_range_blocked(self) -> None:
        bad = json.loads(json.dumps(self.GOOD))
        bad["answers"]["sure"]["noul"] = 1.4
        with self.assertRaises(SystemExit):
            self._post([bad])

    def test_usage_bool_blocked(self) -> None:
        bad = json.loads(json.dumps(self.GOOD))
        bad["usage"]["input_tokens"] = True
        with self.assertRaises(SystemExit):
            self._post([bad])

    def test_503_then_200_retries_once(self) -> None:
        result, opener = self._post([_http_error(503), self.GOOD], retries=1)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(result["answers"], self.GOOD["answers"])

    def test_503_no_retry(self) -> None:
        opener = _FakeOpener([_http_error(503)])
        env = {"TYPESAFE_API_KEY": "dummy-test-key-not-a-real-secret"}
        with self.assertRaises(SystemExit):
            with patch.dict(os.environ, env), patch.object(
                jev, "load_api_key", return_value=env["TYPESAFE_API_KEY"]
            ), patch("urllib.request.build_opener", return_value=opener):
                jev.post_systemone({"task": "t"}, self.QUESTIONS, {}, retries=0)
        self.assertEqual(opener.calls, 1)

    def test_302_redirect_blocked_no_retry(self) -> None:
        opener = _FakeOpener([_http_error(302)])
        env = {"TYPESAFE_API_KEY": "dummy-test-key-not-a-real-secret"}
        with self.assertRaises(SystemExit):
            with patch.dict(os.environ, env), patch.object(
                jev, "load_api_key", return_value=env["TYPESAFE_API_KEY"]
            ), patch("urllib.request.build_opener", return_value=opener):
                jev.post_systemone({"task": "t"}, self.QUESTIONS, {}, retries=1)
        self.assertEqual(opener.calls, 1)

    def test_secret_in_state_blocked_before_http(self) -> None:
        opener = _FakeOpener([self.GOOD])
        env = {"TYPESAFE_API_KEY": "dummy-test-key-not-a-real-secret"}
        with self.assertRaises(SystemExit):
            with patch.dict(os.environ, env), patch.object(
                jev, "load_api_key", return_value=env["TYPESAFE_API_KEY"]
            ), patch("urllib.request.build_opener", return_value=opener):
                jev.post_systemone(
                    {"task": "sk-" + "a" * 24}, self.QUESTIONS, {}
                )
        self.assertEqual(opener.calls, 0)

    def test_loaded_key_in_state_blocked(self) -> None:
        key = "dummy-test-key-not-a-real-secret"
        opener = _FakeOpener([self.GOOD])
        with self.assertRaises(SystemExit):
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": key}), patch.object(
                jev, "load_api_key", return_value=key
            ), patch("urllib.request.build_opener", return_value=opener):
                jev.post_systemone({"task": "echo " + key}, self.QUESTIONS, {})
        self.assertEqual(opener.calls, 0)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)
