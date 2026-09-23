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

    def test_escalate_if_irreversible_false_disables_choice_escalate(self) -> None:
        policy = dict(self.policy)
        policy["escalate_if"] = dict(self.policy.get("escalate_if") or {})
        policy["escalate_if"]["irreversible"] = False
        answers = {
            "where": {
                "type": "choice",
                "choice": "refactor",
                "confidence": 0.7,
                "probabilities": {"refactor": 0.51, "rewrite": 0.49},
            }
        }
        decision = jev.decide(answers, policy, irreversible=True)
        self.assertEqual(decision["action"], "proceed")

    def test_escalate_if_irreversible_false_disables_noul_escalate(self) -> None:
        policy = dict(self.policy)
        policy["escalate_if"] = dict(self.policy.get("escalate_if") or {})
        policy["escalate_if"]["irreversible"] = False
        answers = {"touch": {"type": "noul", "noul": 0.5}}
        decision = jev.decide(answers, policy, irreversible=True)
        self.assertEqual(decision["action"], "proceed")

    def test_escalate_if_irreversible_true_keeps_escalate(self) -> None:
        policy = dict(self.policy)
        policy["escalate_if"] = dict(self.policy.get("escalate_if") or {})
        policy["escalate_if"]["irreversible"] = True
        answers = {
            "where": {
                "type": "choice",
                "choice": "refactor",
                "confidence": 0.7,
                "probabilities": {"refactor": 0.51, "rewrite": 0.49},
            }
        }
        decision = jev.decide(answers, policy, irreversible=True)
        self.assertEqual(decision["action"], "escalate")

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

    def test_decide_malformed_policy_numbers_fall_back(self) -> None:
        policy = {
            "confidence_floor": "abc",
            "tight_gap": float("nan"),
            "noul_yes": None,
            "noul_no": "x",
        }
        decision = jev.decide(
            {
                "q": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": 0.9,
                    "probabilities": {"a": 0.9, "b": 0.1},
                },
                "n": {"type": "noul", "noul": 0.9},
            },
            policy,
        )
        self.assertEqual(decision["action"], "proceed")
        low = jev.decide(
            {
                "q": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": 0.2,
                    "probabilities": {"a": 0.9, "b": 0.1},
                }
            },
            policy,
        )
        self.assertEqual(low["action"], "escalate")

    def test_decide_malformed_score_escalates(self) -> None:
        for raw in ("abc", float("nan"), float("inf"), None):
            decision = jev.decide(
                {"s": {"type": "score", "score": raw, "confidence": 0.9}},
                self.policy,
            )
            self.assertEqual(decision["action"], "escalate", "score=%r" % raw)
            self.assertNotIn("s", decision["picks"])
        good = jev.decide(
            {"s": {"type": "score", "score": "0.8", "confidence": 0.9}},
            self.policy,
        )
        self.assertEqual(good["action"], "proceed")
        self.assertEqual(good["picks"]["s"], 0.8)

    def test_decide_malformed_choice_and_probs(self) -> None:
        for raw in (None, float("nan"), 7, ""):
            decision = jev.decide(
                {
                    "q": {
                        "type": "choice",
                        "choice": raw,
                        "confidence": 0.9,
                        "probabilities": {"a": 0.9, "b": 0.1},
                    }
                },
                self.policy,
            )
            self.assertEqual(decision["action"], "escalate", "choice=%r" % raw)
            self.assertNotIn("q", decision["picks"])
        clean = jev.decide(
            {
                "q": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": 0.9,
                    "probabilities": {"a": 0.9, "b": "x", "c": float("nan")},
                }
            },
            self.policy,
        )
        self.assertEqual(clean["probabilities"]["q"], {"a": 0.9})
        self.assertEqual(
            json.loads(json.dumps(clean, allow_nan=False))["action"], "proceed"
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
        self.assertIn("risky", policy.get("templates", {}))

    def test_scaffold_risky_builds_noul_question(self) -> None:
        import jev

        policy = jev.load_policy()
        req = jev.scaffold_request(policy, ["risky"], {"note": "x"})
        q = req["questions"]["risky"]
        self.assertEqual(q["type"], "noul")
        self.assertTrue(q["instructions"].rstrip().endswith("?"))

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

    def test_cmd_scaffold_lint_reports_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "req.json"
            err = io.StringIO()
            with patch("sys.stderr", err):
                rc = jev.main(
                    [
                        "scaffold", "approach", "--out", str(out),
                        "--option", "approach=a:do a thing",
                        "--option", "approach=b:do b thing",
                        "--lint",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn("lint J012", err.getvalue())
            self.assertTrue(out.is_file())

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
        if hasattr(outcome, "read") and hasattr(outcome, "__enter__"):
            return outcome
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

    def test_network_error_is_clean_systemexit(self) -> None:
        opener = _FakeOpener(
            [urllib.error.URLError("name or service not known")]
        )
        env = {"TYPESAFE_API_KEY": "dummy-test-key-not-a-real-secret"}
        with self.assertRaises(SystemExit) as ctx:
            with patch.dict(os.environ, env), patch.object(
                jev, "load_api_key", return_value=env["TYPESAFE_API_KEY"]
            ), patch("urllib.request.build_opener", return_value=opener):
                jev.post_systemone({"task": "t"}, self.QUESTIONS, {}, retries=0)
        self.assertIn("Jev network error", str(ctx.exception))

    def test_non_json_body_is_clean_systemexit(self) -> None:
        class _Raw(_FakeResponse):
            def __init__(self, raw: bytes) -> None:
                self._raw = raw

        for body in (b"<html>upstream error</html>", b"\xff\xfe not utf8"):
            with self.subTest(body=body[:16]):
                opener = _FakeOpener([_Raw(body)])
                env = {"TYPESAFE_API_KEY": "dummy-test-key-not-a-real-secret"}
                with self.assertRaises(SystemExit) as ctx:
                    with patch.dict(os.environ, env), patch.object(
                        jev, "load_api_key", return_value=env["TYPESAFE_API_KEY"]
                    ), patch(
                        "urllib.request.build_opener", return_value=opener
                    ):
                        jev.post_systemone(
                            {"task": "t"}, self.QUESTIONS, {}, retries=0
                        )
                self.assertIn("Jev response was not JSON", str(ctx.exception))

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

    def test_read_json_arg_errors_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            with self.assertRaises(SystemExit):
                jev.read_json_arg(str(missing))
            bad = Path(tmp) / "bad.json"
            bad.write_text("{{{", encoding="utf-8")
            with self.assertRaises(SystemExit):
                jev.read_json_arg(str(bad))

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

    def test_cmd_self_test_roundtrips_offline(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["self-test"])
        self.assertEqual(rc, 0)
        self.assertIn("self-test: ok", buf.getvalue())

        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["self-test", "--json"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["self_test"], "ok")
        self.assertTrue(all(out["checks"].values()))

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

    def test_cmd_ping_verdict_writes_slim_json(self) -> None:
        fake = {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.95}}}
        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            buf = io.StringIO()
            with patch.object(jev, "post_systemone", return_value=fake), patch.object(
                sys, "stdout", buf
            ):
                rc = jev.main(["ping", "--verdict", str(verdict)])
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["model"], "m1")
            self.assertEqual(payload["noul"], 0.95)
            self.assertIn("ms", payload)

    def test_ping_timeout_env_and_flag(self) -> None:
        calls = []

        def fake_post(state, questions, policy, model=None, timeout=60, retries=1):
            calls.append(timeout)
            return {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.9}}}

        buf = io.StringIO()
        with patch.object(jev, "post_systemone", side_effect=fake_post), patch.object(
            sys, "stdout", buf
        ), patch.dict(os.environ, {"JEV_TIMEOUT": "7.5"}):
            rc = jev.main(["ping"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [7.5])
        calls.clear()
        with patch.object(jev, "post_systemone", side_effect=fake_post), patch.object(
            sys, "stdout", buf
        ), patch.dict(os.environ, {"JEV_TIMEOUT": "bogus"}):
            rc = jev.main(["ping", "--timeout", "3"])
        self.assertEqual(calls, [3])

    def test_ping_retries_flag_passed_to_post(self) -> None:
        calls = []

        def fake_post(state, questions, policy, model=None, timeout=60, retries=1):
            calls.append(retries)
            return {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.9}}}

        with patch.object(jev, "post_systemone", side_effect=fake_post), patch.object(
            sys, "stdout", io.StringIO()
        ):
            rc = jev.main(["ping", "--retries", "4"])
            rc2 = jev.main(["ping"])
        self.assertEqual((rc, rc2), (0, 0))
        self.assertEqual(calls, [4, 1])

    def test_ping_jq_prints_one_field(self) -> None:
        fake = {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.9}}}
        buf = io.StringIO()
        with patch.object(jev, "post_systemone", return_value=fake), patch.object(
            sys, "stdout", buf
        ):
            rc = jev.main(["ping", "--jq", "model"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), "m1")
        with patch.object(jev, "post_systemone", return_value=fake), patch.object(
            sys, "stdout", io.StringIO()
        ), patch.object(sys, "stderr", io.StringIO()):
            rc = jev.main(["ping", "--jq", "nope"])
        self.assertEqual(rc, 2)

    def test_ping_out_writes_slim_json(self) -> None:
        fake = {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.9}}}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p.json"
            buf = io.StringIO()
            with patch.object(jev, "post_systemone", return_value=fake), patch.object(
                sys, "stdout", buf
            ), patch.object(sys, "stderr", io.StringIO()):
                rc = jev.main(["ping", "--out", str(out)])
            self.assertEqual(rc, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["model"], "m1")

    def test_ping_watch_emits_ticks_capped(self) -> None:
        fake = {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.9}}}
        buf = io.StringIO()
        err = io.StringIO()
        with patch.object(jev, "post_systemone", return_value=fake), patch.object(
            sys, "stdout", buf
        ), patch.object(sys, "stderr", err):
            rc = jev.main(["ping", "--watch", "0.01", "--max-ticks", "3"])
        self.assertEqual(rc, 0)
        ticks = [
            json.loads(line)
            for line in buf.getvalue().splitlines()
            if line.startswith('{"ts"')
        ]
        self.assertEqual(len(ticks), 3)
        self.assertTrue(all(t["ok"] and t["model"] == "m1" for t in ticks))
        self.assertIn("watch tick=3", err.getvalue())

    def test_ping_watch_failed_tick_exits_1_when_capped(self) -> None:
        def boom(*_a, **_k):
            raise SystemExit("Jev network error: refused")

        buf = io.StringIO()
        with patch.object(jev, "post_systemone", side_effect=boom), patch.object(
            sys, "stdout", buf
        ), patch.object(sys, "stderr", io.StringIO()):
            rc = jev.main(["ping", "--watch", "0.01", "--max-ticks", "2"])
        self.assertEqual(rc, 1)
        ticks = [
            json.loads(line)
            for line in buf.getvalue().splitlines()
            if line.startswith('{"ts"')
        ]
        self.assertEqual(len(ticks), 2)
        self.assertFalse(ticks[0]["ok"])
        self.assertIn("refused", ticks[0]["error"])

    def test_ping_watch_fail_fast_and_verdict(self) -> None:
        def boom(*_a, **_k):
            raise SystemExit("down")

        with tempfile.TemporaryDirectory() as tmp:
            verdict = Path(tmp) / "v.json"
            out = Path(tmp) / "t.jsonl"
            with patch.object(jev, "post_systemone", side_effect=boom), patch.object(
                sys, "stdout", io.StringIO()
            ), patch.object(sys, "stderr", io.StringIO()):
                rc = jev.main(
                    [
                        "ping",
                        "--watch",
                        "0.01",
                        "--fail-fast",
                        "--out",
                        str(out),
                        "--verdict",
                        str(verdict),
                    ]
                )
            self.assertEqual(rc, 0)
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertEqual(payload["verdict"], "down")
            self.assertEqual(payload["ticks"], 1)
            lines = out.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertFalse(json.loads(lines[0])["ok"])

    def test_ping_watch_max_env_caps(self) -> None:
        fake = {"model": "m1", "answers": {"ok": {"type": "noul", "noul": 0.9}}}
        buf = io.StringIO()
        with patch.object(jev, "post_systemone", return_value=fake), patch.object(
            sys, "stdout", buf
        ), patch.object(sys, "stderr", io.StringIO()), patch.dict(
            os.environ, {"JEV_PING_WATCH_MAX": "2"}
        ):
            rc = jev.main(["ping", "--watch", "0.01"])
        self.assertEqual(rc, 0)
        ticks = [
            line
            for line in buf.getvalue().splitlines()
            if line.startswith('{"ts"')
        ]
        self.assertEqual(len(ticks), 2)

    def test_cmd_decide_out_writes_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            out = Path(tmp) / "out.json"
            path.write_text(
                json.dumps({"answers": {"q": {"type": "noul", "noul": 0.9}}}),
                encoding="utf-8",
            )
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", io.StringIO()
            ):
                rc = jev.main(["decide", str(path), "--out", str(out)])
            self.assertEqual(rc, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["decision"]["action"], "proceed")

    def test_cmd_ask_out_writes_response(self) -> None:
        fake = {
            "model": "m1",
            "answers": {
                "q": {"type": "choice", "choice": "a", "confidence": 0.9}
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "r.json"
            out = Path(tmp) / "out.json"
            req.write_text(
                json.dumps(
                    {
                        "state": {"task": "t"},
                        "questions": {
                            "q": {
                                "type": "choice",
                                "instructions": "pick",
                                "criteria": {"a": "pick a", "b": "pick b"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(jev, "post_systemone", return_value=fake), patch.object(
                sys, "stdout", io.StringIO()
            ), patch.object(sys, "stderr", io.StringIO()):
                rc = jev.main(["ask", str(req), "--out", str(out)])
            self.assertEqual(rc, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["answers"]["q"]["choice"], "a")
            self.assertIn("decision", payload)

    def test_cmd_lint_jq_prints_one_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(
                json.dumps(
                    {
                        "questions": {
                            "q": {
                                "type": "choice",
                                "instructions": "How many files are there?",
                                "criteria": {"a": "one", "b": "two", "none": "none of these"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["lint", str(path), "--jq", "warnings"])
            self.assertEqual(json.loads(buf.getvalue()), 1)
            self.assertEqual(rc, 0)
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", io.StringIO()
            ) as err:
                rc = jev.main(["lint", str(path), "--jq", "nope"])
            self.assertEqual(rc, 2)
            self.assertIn("bad --jq key", err.getvalue())

    def test_ping_json_emits_object(self) -> None:
        def fake_post(state, questions, policy, model=None, timeout=60, retries=1):
            return {"model": "m9", "answers": {"ok": {"type": "noul", "noul": 0.9}}}

        buf = io.StringIO()
        with patch.object(jev, "post_systemone", side_effect=fake_post), patch.object(
            sys, "stdout", buf
        ):
            rc = jev.main(["ping", "--json"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(out["model"], "m9")
        self.assertEqual(out["noul"], 0.9)
        self.assertIsInstance(out["ms"], int)

    def test_malformed_policy_is_clean_systemexit(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{bad json", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                jev.load_policy(str(bad))
            self.assertIn("not JSON", str(ctx.exception))
            arr = Path(tmp) / "arr.json"
            arr.write_text("[]", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                jev.load_policy(str(arr))
            self.assertIn("must be an object", str(ctx.exception))
            with self.assertRaises(SystemExit) as ctx:
                jev.load_policy(str(Path(tmp) / "nope.json"))
            self.assertIn("unreadable", str(ctx.exception))

    def test_jev_policy_env_overrides_path(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            custom = Path(tmp) / "policy.json"
            custom.write_text(json.dumps({"version": 999}), encoding="utf-8")
            with patch.dict(os.environ, {"JEV_POLICY": str(custom)}):
                self.assertEqual(jev.load_policy()["version"], 999)
            with patch.dict(os.environ, {"JEV_POLICY": ""}):
                self.assertNotEqual(jev.load_policy().get("version"), 999)

    def test_env_timeout_helper(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_TIMEOUT", None)
            self.assertIsNone(jev.env_timeout())
        with patch.dict(os.environ, {"JEV_TIMEOUT": "0"}):
            self.assertIsNone(jev.env_timeout())
        with patch.dict(os.environ, {"JEV_TIMEOUT": "2.5"}):
            self.assertEqual(jev.env_timeout(), 2.5)

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

    def test_cmd_ask_jq_prints_one_field(self) -> None:
        req_obj = {
            "state": {"task": "t"},
            "questions": {
                "q": {
                    "type": "choice",
                    "instructions": "pick one",
                    "criteria": {"a": "x", "b": "y"},
                }
            },
        }
        fake = {
            "model": "m1",
            "answers": {"q": {"type": "choice", "choice": "a", "confidence": 0.9}},
            "usage": {"n": 1},
        }
        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps(req_obj), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(jev, "post_systemone", return_value=fake), patch.object(
                sys, "stdout", buf
            ):
                rc = jev.main(["ask", str(req), "--jq", "answers.q.choice"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), "a")
            buf = io.StringIO()
            with patch.object(jev, "post_systemone", return_value=fake), patch.object(
                sys, "stdout", buf
            ):
                rc = jev.main(["ask", str(req), "--jq", "decision.action"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), "proceed")
            with patch.object(jev, "post_systemone", return_value=fake), patch.object(
                sys, "stdout", io.StringIO()
            ), patch.object(sys, "stderr", io.StringIO()):
                rc = jev.main(["ask", str(req), "--jq", "nope.deep"])
            self.assertEqual(rc, 2)

    def test_cmd_decide_jq_prints_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            path.write_text(
                json.dumps({"answers": {"q": {"type": "noul", "noul": 0.9}}}),
                encoding="utf-8",
            )
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["decide", str(path), "--jq", "decision.action"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), "proceed")
            with patch.object(sys, "stdout", io.StringIO()), patch.object(
                sys, "stderr", io.StringIO()
            ):
                rc = jev.main(["decide", str(path), "--jq", "nope"])
            self.assertEqual(rc, 2)

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

    def test_scaffold_unwritable_out_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("x", encoding="utf-8")
            err = io.StringIO()
            with patch.object(sys, "stderr", err):
                rc = jev.main(
                    [
                        "scaffold",
                        "keep_vs_change",
                        "--out",
                        str(blocker / "x.json"),
                    ]
                )
            self.assertEqual(rc, jev.ASK_ESCALATE_EXIT)
            self.assertIn("cannot write", err.getvalue())

    def test_ask_verdict_writes_outcome_json(self) -> None:
        def fake_post(state, questions, policy, model=None, timeout=60, retries=1):
            return {
                "model": "m1",
                "answers": {
                    "q": {"choice": "a", "confidence": 0.99, "probabilities": {"a": 0.99, "b": 0.01}}
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(
                json.dumps(
                    {
                        "state": {"task": "t"},
                        "questions": {
                            "q": {
                                "type": "choice",
                                "instructions": "pick one",
                                "criteria": {"a": "x", "b": "y"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            verdict = Path(tmp) / "v.json"
            buf = io.StringIO()
            with patch.object(jev, "post_systemone", side_effect=fake_post), patch.object(
                sys, "stdout", buf
            ):
                rc = jev.main(["ask", str(req), "--verdict", str(verdict)])
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertIn(payload["verdict"], ("proceed", "escalate"))
            self.assertEqual(payload["verdict"], "proceed" if rc == 0 else "escalate")
            self.assertEqual(payload["picks"], {"q": "a"})
            self.assertIn("action", payload)
            self.assertFalse((Path(tmp) / "v.json.tmp").exists())

    def test_decide_verdict_writes_action_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "a.json"
            payload.write_text(
                json.dumps(
                    {
                        "answers": {
                            "q": {
                                "choice": "a",
                                "confidence": 0.99,
                                "probabilities": {"a": 0.99, "b": 0.01},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            verdict = Path(tmp) / "v.json"
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(
                    ["decide", str(payload), "--verdict", str(verdict)]
                )
            out = json.loads(verdict.read_text(encoding="utf-8"))
            self.assertIn(out["verdict"], ("proceed", "escalate"))
            self.assertEqual(out["verdict"], "proceed" if rc == 0 else "escalate")
            self.assertIn("action", out)

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


class AskTimeoutTests(unittest.TestCase):
    REQ = {
        "state": {"task": "t"},
        "questions": {
            "q": {
                "type": "choice",
                "instructions": "pick one",
                "criteria": {"a": "x", "b": "y"},
            }
        },
    }
    FAKE = {
        "model": "m1",
        "answers": {"q": {"type": "choice", "choice": "a", "confidence": 0.9}},
    }

    def _ask_with_timeout(self, argv_extra: list, env: dict) -> list:
        calls = []

        def fake_post(state, questions, policy, model=None, timeout=60, retries=1):
            calls.append(timeout)
            return dict(self.FAKE)

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps(self.REQ), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(jev, "post_systemone", side_effect=fake_post), patch.object(
                sys, "stdout", buf
            ), patch.dict(os.environ, env):
                rc = jev.main(["ask", str(req)] + argv_extra)
        self.assertEqual(rc, 0, buf.getvalue())
        return calls

    def test_ask_timeout_env(self) -> None:
        self.assertEqual(
            self._ask_with_timeout([], {"JEV_TIMEOUT": "9.5"}), [9.5]
        )

    def test_ask_timeout_flag_wins_over_env(self) -> None:
        self.assertEqual(
            self._ask_with_timeout(["--timeout", "2"], {"JEV_TIMEOUT": "9.5"}),
            [2],
        )

    def test_ask_retries_flag_passed_to_post(self) -> None:
        calls = []

        def fake_post(state, questions, policy, model=None, timeout=60, retries=1):
            calls.append(retries)
            return dict(self.FAKE)

        with tempfile.TemporaryDirectory() as tmp:
            req = Path(tmp) / "req.json"
            req.write_text(json.dumps(self.REQ), encoding="utf-8")
            buf = io.StringIO()
            with patch.object(
                jev, "post_systemone", side_effect=fake_post
            ), patch.object(sys, "stdout", buf):
                rc = jev.main(["ask", str(req), "--retries", "3"])
                rc2 = jev.main(["ask", str(req)])
        self.assertEqual((rc, rc2), (0, 0), buf.getvalue())
        self.assertEqual(calls, [3, 1])

    def test_ask_timeout_defaults_60(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_TIMEOUT", None)
            calls = self._ask_with_timeout([], {})
        self.assertEqual(calls, [60])


class SchemaTests(unittest.TestCase):
    """--schema ties the printed contract to the real validator."""

    def test_schema_rows_mirror_question_lint_and_validate(self) -> None:
        rows = jev.schema_rows()
        # request.* mirrors question_lint.REQUEST_SCHEMA_ROWS verbatim
        for key, meta in jev.question_lint.REQUEST_SCHEMA_ROWS.items():
            self.assertIn("request." + key, rows)
            self.assertEqual(rows["request." + key]["required"], meta["required"])
        # response side: a well-formed response passes the validator and its
        # keys are a subset of the documented answer.* contract
        questions = {
            "q1": {"type": "choice", "criteria": {"a": "A", "b": "B"}},
            "q2": {"type": "noul"},
            "q3": {"type": "score"},
        }
        response = {
            "model": "m",
            "answers": {
                "q1": {"type": "choice", "choice": "a", "confidence": 0.9,
                       "probabilities": {"a": 0.7, "b": 0.3}},
                "q2": {"type": "noul", "noul": 1},
                "q3": {"type": "score", "score": 4, "confidence": 0.5},
            },
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }
        out = jev.validate_response(response, questions)
        self.assertEqual(out["model"], "m")
        allowed_answer = {
            k.split(".", 1)[1] for k in rows if k.startswith("answer.")
        }
        for answer in response["answers"].values():
            self.assertEqual(set(answer) - allowed_answer, set())

    def test_schema_flag_text_and_json(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["--schema"])
        self.assertEqual(rc, 0)
        self.assertIn("request.questions:", buf.getvalue())
        self.assertIn("response.answers:", buf.getvalue())

        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["--schema", "--json"])
        self.assertEqual(rc, 0)
        rows = json.loads(buf.getvalue())
        self.assertIn("response.answers", rows)

    def test_cmd_env_reports_presence_only(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["env"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        for key in ("api_key_set", "policy", "timeout_seconds", "watch_max", "watch_quiet", "watch_secs"):
            self.assertIn(key, report)
        self.assertIsInstance(report["api_key_set"], bool)
        self.assertEqual(report["policy"], "default")
        # never leaks the key value
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "apikey_secret_jev_env_test"}):
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                jev.main(["env"])
            self.assertNotIn("apikey_secret_jev_env_test", buf.getvalue())

    def test_cmd_env_jq_and_bad_key(self) -> None:
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            rc = jev.main(["env", "--jq", "policy"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), "default")
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            rc = jev.main(["env", "--jq", "nope"])
        self.assertEqual(rc, 2)
        self.assertIn("env has", err.getvalue())

    def test_cmd_env_reflects_env_overrides(self) -> None:
        env = {
            "JEV_TIMEOUT": "15",
            "JEV_POLICY": "/tmp/p.json",
            "JEV_PING_WATCH_MAX": "4",
            "JEV_PING_WATCH_QUIET": "1",
        }
        buf = io.StringIO()
        with patch.dict(os.environ, env):
            with patch.object(sys, "stdout", buf):
                jev.main(["env"])
        report = json.loads(buf.getvalue())
        self.assertEqual(report["timeout_seconds"], 15.0)
        self.assertEqual(report["policy"], "/tmp/p.json")
        self.assertEqual(report["watch_max"], 4)
        self.assertTrue(report["watch_quiet"])

    def test_cmd_env_out_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "env.json"
            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                rc = jev.main(["env", "--out", str(target)])
            self.assertEqual(rc, 0)
            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("api_key_set", saved)
            self.assertEqual(saved["policy"], "default")


if __name__ == "__main__":
    sys.exit(0 if unittest.main(verbosity=2) else 1)
