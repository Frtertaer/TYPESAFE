<!-- jev-consult:start -->
# jev-consult

You inspect this repo and write code. Jev decides. Load `skills/jev-consult/SKILL.md` and run `ask` before you pick approach, keep vs change, architecture, library, delete, refactor vs rewrite, naming, or good-enough. If `ask` stdout has no `decision.action`, do not pick architecture, library, or approach. Write the request with `jev.py scaffold` then `ask`. If you drift, get stuck, loop, or cannot solve, call Jev with that state and a Choice of next moves. If you do not know or do not remember, load `.jev-trace.json` and ask. At session start, a hook IDF-shortlists already-installed skills/plugins/MCP then one Jev pick (fail-open, never installs). Codex: `install.py` writes `~/.codex/hooks.json` UserPromptSubmit; trust it in `/hooks` or the hook is skipped. If `.jev-tools-miss.json` exists, run `peer_fill.py --from-miss`; if `no_peer`, run `catalog_fill.py --from-miss`; if `no_catalog`, run `apply_fill.py --from-miss`. If `.jev-tools.json` exists and `inventory.py --check-sidecar` prints `fresh`, load those names. Stale or missing: run `inventory.py` then Jev Choice on the shortlist. Skip only tool-checkable facts and mechanical follow-through of a Jev pick already made this session.

```text
python skills/jev-consult/scripts/jev.py ask request.json --trace
python skills/jev-consult/scripts/jev.py scaffold keep_vs_change --out request.json --plan "<task>"
python skills/jev-consult/scripts/inventory.py --task "<task>" --harness auto --write-ask tools.request.json
python skills/jev-consult/scripts/peer_fill.py --from-miss
python skills/jev-consult/scripts/catalog_fill.py --from-miss
python skills/jev-consult/scripts/apply_fill.py --from-miss
python skills/jev-consult/scripts/compare.py --live
python skills/jev-consult/scripts/compact.py transcript.json --history --trace
```

Thresholds live only in `skills/jev-consult/policy.json`. Hook never auto-installs. Never `--force`. Never npx. Never `claude plugin install`. Never print `TYPESAFE_API_KEY`.

After clone, one command wires user-scope Hermes / Claude Code / Codex / Grok Build:

```text
python scripts/install.py
```
<!-- jev-consult:end -->
## Verification for contribution review

The progress feature is opt-in; its scoring policy is in `skills/jev-consult/policy.json`, not in hooks. Run `python -m unittest discover -s tests -p test_progress.py -v` for the core, `python -m unittest discover -s tests -p test_progress_cli.py -v` for CLI integration, and `python skills/jev-consult/scripts/policy_lint.py --strict` for configuration. The full offline suite is `python -m unittest discover -s tests`. Never treat contribution credits as a percentage of project readiness.
