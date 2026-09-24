# Convenience targets — everything is plain `python` + stdlib only.
# On Windows without make, run the recipes directly (see README).

.PHONY: test lint smoke smoke-quick hooks-install

test:            ## full suite
	python -m unittest discover -s tests

test-%:          ## one test file by suffix: make test-jev runs tests.test_jev
	python -m unittest tests.test_$*

lint:            ## pack lints (policy + trigger cases + skill doc)
	python skills/jev-consult/scripts/policy_lint.py skills/jev-consult/policy.json
	python skills/jev-consult/scripts/trigger_lint.py tests/fixtures/jev-consult.trigger-cases.json
	python skills/jev-consult/scripts/question_lint.py --severity error skills/jev-consult/examples/*.request.json
	python skills/jev-consult/scripts/skill_lint.py skills/jev-consult/SKILL.md

smoke:           ## offline e2e steps
	python skills/jev-consult/scripts/smoke.py

smoke-quick:     ## fastest offline wiring check
	python skills/jev-consult/scripts/smoke.py --only self_test --quiet

hooks-install:   ## wire .pre-commit-config.yaml locally
	pre-commit install
