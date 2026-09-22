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

    def test_decide_malformed_numerics_escalate(self) -> None:
        for raw in ("abc", None, float("nan"), 1.5):
            decision = jev.decide(
                {"q": {"type": "noul", "noul": raw}}, self.policy
            )
            self.assertEqual(decision["action"], "escalate", "noul=%r" % raw)
        nan_conf = jev.decide(
            {
                "q": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": float("nan"),
                    "probabilities": {"a": 0.9, "b": 0.1},
                }
            },
            self.policy,
        )
        self.assertEqual(nan_conf["action"], "escalate")
        bad_probs = jev.decide(
            {
                "q": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": 0.9,
                    "probabilities": {"a": "x", "b": 0.1},
                }
            },
            self.policy,
        )
        self.assertEqual(bad_probs["action"], "proceed")


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


class JevInternalsTests(unittest.TestCase):
    def test_policy_get(self) -> None:
        policy = {"a": {"b": 1}, "flat": 2}
        self.assertEqual(jev.policy_get(policy, "missing", ("a", "b")), 1)
        self.assertEqual(jev.policy_get(policy, "flat", ("a", "b")), 2)  # first wins
        self.assertEqual(jev.policy_get(policy, "nope", default=9), 9)
        self.assertIsNone(jev.policy_get(policy, "nope"))
        self.assertIsNone(jev.policy_get(policy, ("a", "missing")))
        self.assertIsNone(jev.policy_get({"a": None}, "a"))  # None treated as absent

    def test_finite(self) -> None:
        self.assertTrue(jev._finite(0.5, 0, 1))
        self.assertFalse(jev._finite(True, 0, 1))
        self.assertFalse(jev._finite("0.5", 0, 1))
        self.assertFalse(jev._finite(float("nan")))
        self.assertFalse(jev._finite(float("inf"), 0, 1))
        self.assertFalse(jev._finite(-0.1, 0, 1))
        self.assertFalse(jev._finite(1.1, 0, 1))

    def test_top_two_gap(self) -> None:
        self.assertEqual(jev.top_two_gap({}), 1.0)
        self.assertEqual(jev.top_two_gap({"a": 0.4}), 1.0)
        self.assertAlmostEqual(jev.top_two_gap({"a": 0.6, "b": 0.3, "c": 0.1}), 0.3)

    def test_policy_warnings_clean_policy(self) -> None:
        policy = jev.load_policy(str(ROOT / "skills" / "jev-consult" / "policy.json"))
        warnings = jev.policy_warnings(policy)
        self.assertEqual(warnings, [])

    def test_policy_warnings_lint_unavailable(self) -> None:
        with patch.object(jev, "policy_lint", None):
            warnings = jev.policy_warnings({})
        self.assertEqual(warnings, ["policy_lint unavailable; static policy checks skipped"])

    def test_policy_warnings_bad_policy(self) -> None:
        warnings = jev.policy_warnings({"noul_yes": 5})  # out-of-range + missing keys
        self.assertTrue(any("P002" in w or "P001" in w for w in warnings))

    def test_read_json_arg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(jev.read_json_arg(str(path)), {"a": 1})
        with patch.object(sys, "stdin", io.StringIO('{"b": 2}')):
            self.assertEqual(jev.read_json_arg("-"), {"b": 2})

    def test_emit_writes_json(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            jev.emit({"a": 1})
        self.assertEqual(json.loads(buf.getvalue()), {"a": 1})

    def test_apply_trace_passthrough(self) -> None:
        state = {"plan": "p"}
        self.assertIs(jev.apply_trace(state, None), state)

    def test_apply_trace_merges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "t.json"
            trace.write_text(
                json.dumps({"attempt_count": 3, "last_pick": "x", "plan": "old"}),
                encoding="utf-8",
            )
            merged = jev.apply_trace({"plan": "new"}, str(trace))
        self.assertEqual(merged["attempt_count"], 3)
        self.assertEqual(merged["last_pick"], "x")
        self.assertEqual(merged["plan"], "new")  # present request fields win

    def test_load_scaffold_state(self) -> None:
        self.assertEqual(jev.load_scaffold_state(None, None), {})
        self.assertEqual(
            jev.load_scaffold_state(None, "the plan"),
            {"plan": "the plan", "task": "the plan"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            path.write_text('{"state": {"goal": "g"}}', encoding="utf-8")
            self.assertEqual(jev.load_scaffold_state(str(path), None), {"goal": "g"})
            path.write_text('{"goal": "g2"}', encoding="utf-8")
            self.assertEqual(jev.load_scaffold_state(str(path), None), {"goal": "g2"})
            path.write_text('[1]', encoding="utf-8")
            with self.assertRaises(ValueError):
                jev.load_scaffold_state(str(path), None)

    def test_cmd_decide_proceed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            path.write_text(
                json.dumps({"answers": {"q": {"type": "noul", "noul": 0.9}}}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["decide", str(path)])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue())["decision"]["action"], "proceed")

    def test_cmd_decide_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            path.write_text(
                json.dumps(
                    {
                        "irreversible": True,
                        "answers": {"q": {"type": "noul", "noul": 0.4}},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(sys, "stdout", io.StringIO()):
                rc = jev.main(["decide", str(path)])
            self.assertEqual(rc, jev.ASK_ESCALATE_EXIT)

    def test_cmd_decide_rejects_non_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            path.write_text("[1]", encoding="utf-8")
            with self.assertRaises(SystemExit):
                jev.main(["decide", str(path)])

    def test_cmd_lint_clean(self) -> None:
        request = {
            "state": {"task": "t"},
            "questions": {
                "where": {
                    "type": "choice",
                    "instructions": "Which library should handle auth?",
                    "criteria": {"jwtlib": "jwt lib", "none": "skip"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["lint", str(path)])
            self.assertEqual(rc, 0)
            self.assertIn("0 error(s)", buf.getvalue())

    def test_cmd_lint_errors(self) -> None:
        request = {
            "state": {"task": "t"},
            "questions": {
                "q": {
                    "type": "noul",
                    "instructions": "Should we not skip and not proceed?",
                    "criteria": {"true": "yes", "false": "no"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps(request), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["lint", str(path)])
            self.assertNotEqual(rc, 0)

    def test_cmd_ping(self) -> None:
        fake = {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.95}}}
        buf = io.StringIO()
        with patch.object(jev, "post_systemone", return_value=fake), patch.object(
            sys, "stdout", buf
        ):
            rc = jev.main(["ping"])
        self.assertEqual(rc, 0)
        self.assertIn("ok model=m1 noul=0.95", buf.getvalue())

    def test_main_requires_command(self) -> None:
        with self.assertRaises(SystemExit):
            jev.main([])

    def test_version_flag(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["--version"])
        self.assertEqual(rc, 0)
        self.assertIn("jev-consult", buf.getvalue())
        self.assertIn("policy v", buf.getvalue())

    def test_version_flag_with_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "policy.json"
            custom.write_text('{"version": 42}', encoding="utf-8")
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["--policy", str(custom), "--version"])
        self.assertEqual(rc, 0)
        self.assertIn("v42", buf.getvalue())

    def test_ask_dry_no_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(
                json.dumps(
                    {
                        "state": {"task": "t"},
                        "questions": {
                            "q": {
                                "type": "noul",
                                "instructions": "ok?",
                                "criteria": {"yes": "y", "no": "n"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["ask", str(req), "--dry"])
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["dry"])
            self.assertEqual(out["state"]["task"], "t")
            self.assertIn("q", out["questions"])

    def test_lint_json_emits_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(
                json.dumps(
                    {
                        "state": {},
                        "questions": {
                            "q": {"type": "choice", "instructions": "pick", "criteria": {"only": "one"}}
                        },
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["lint", str(req), "--json"])
            out = json.loads(buf.getvalue())
            self.assertIn("findings", out)
            self.assertIn("errors", out)
            self.assertIn(rc, (0, 1))

    def test_lint_text_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(
                json.dumps({"state": {}, "questions": {"q": {"type": "choice", "instructions": "pick one", "criteria": {"a": "x", "b": "y"}}}}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["lint", str(req)])
            self.assertEqual(rc, 0)
            self.assertIn("lint:", buf.getvalue())

    def test_scaffold_list_prints_template_ids(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["scaffold", "--list"])
        self.assertEqual(rc, 0)
        names = buf.getvalue().split()
        self.assertIn("approach", names)
        self.assertIn("keep_vs_change", names)

    def test_scaffold_missing_templates_exits_2(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf), patch.object(sys, "stderr", io.StringIO()):
            rc = jev.main(["scaffold", "--out", "x.json"])
        self.assertEqual(rc, jev.ASK_ESCALATE_EXIT)

    def test_scaffold_missing_out_exits_2(self) -> None:
        with patch.object(sys, "stderr", io.StringIO()):
            rc = jev.main(["scaffold", "approach"])
        self.assertEqual(rc, jev.ASK_ESCALATE_EXIT)

    def test_ask_dry_never_posts(self) -> None:
        def _boom(*a, **k):
            raise AssertionError("post called")

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(
                json.dumps({"state": {"task": "t"}, "questions": {"q": {"type": "noul", "instructions": "i", "criteria": {"a": "a", "b": "b"}}}}),
                encoding="utf-8",
            )
            with patch.object(jev, "post_systemone", side_effect=_boom):
                buf = io.StringIO()
                with patch.object(sys, "stdout", buf):
                    rc = jev.main(["ask", str(req), "--dry"])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)
