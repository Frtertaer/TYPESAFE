# jev-consult

Committed at repo root (do not copy by hand): `AGENTS.md`, `CLAUDE.md`, `.hermes.md`.

User gives a plan. The coding agent inspects the repo. **Jev decides.** The coder then writes the code.

Load `skills/jev-consult/SKILL.md` and run `ask` before picking approach, keep vs change, architecture, library, delete, or good-enough. If `ask` stdout has no `decision.action`, do not pick architecture, library, or approach. Write the request with `jev.py scaffold` then `ask`. If you drift, get stuck, loop, do not know, do not remember, or cannot solve, call Jev with that state. At session start, a hook IDF-shortlists already-installed skills/plugins/MCP then one Jev pick (fail-open, never installs). Marketplace fill is miss-sidecar only (`peer_fill` then `catalog_fill` then `apply_fill`); hook never installs. Codex: `install.py` writes `~/.codex/hooks.json` UserPromptSubmit; trust it in `/hooks` or Codex skips the hook. Skip only facts a tool can check and mechanical follow-through of a Jev pick already made this session.

Jev has no memory: send a fresh `state` every call. Jev does not fact-check empty claims. Edit thresholds only in `skills/jev-consult/policy.json`. Never auto-install marketplace items. Never print `TYPESAFE_API_KEY`.

CLI (from this repo):

```text
python skills/jev-consult/scripts/jev.py ask skills/jev-consult/examples/jwt-auth.request.json --trace
python skills/jev-consult/scripts/jev.py scaffold keep_vs_change --out request.json --plan "<task>"
python skills/jev-consult/scripts/inventory.py --task "<task>" --harness auto --write-ask tools.request.json
python skills/jev-consult/scripts/inventory.py --check-sidecar [path]   # fresh/stale/missing/invalid
python skills/jev-consult/scripts/compare.py --live
python skills/jev-consult/scripts/compact.py transcript.json --trace
python tests/test_jev.py
python tests/test_inventory.py
python tests/test_trace.py
python tests/test_compare.py
python tests/test_compact.py
python scripts/install.py
```

On this Windows host use `python`, not `python3`.
