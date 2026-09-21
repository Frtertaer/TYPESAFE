# jev-consult

Committed at repo root (do not copy by hand): `AGENTS.md`, `CLAUDE.md`, `.hermes.md`.

User gives a plan. The coding agent inspects the repo. **Jev decides.** The coder then writes the code.

Load `skills/jev-consult/SKILL.md` and run `ask` before picking approach, keep vs change, architecture, library, delete, or good-enough. If `ask` stdout has no `decision.action`, do not pick architecture, library, or approach. Write the request with `jev.py scaffold` then `ask`. If you drift, get stuck, loop, do not know, do not remember, or cannot solve, call Jev with that state. At session start, a hook IDF-shortlists already-installed skills/plugins/MCP then one Jev pick (fail-open, never installs). Marketplace fill is miss-sidecar only (`peer_fill` then `catalog_fill` then `apply_fill`); hook never installs. Codex: `install.py` writes `~/.codex/hooks.json` UserPromptSubmit; trust it in `/hooks` or Codex skips the hook. Skip only facts a tool can check and mechanical follow-through of a Jev pick already made this session.

Jev has no memory: send a fresh `state` every call. Jev does not fact-check empty claims. Edit thresholds only in `skills/jev-consult/policy.json`. Never auto-install marketplace items. Never print `TYPESAFE_API_KEY`. Every hook pick and every fill outcome (`apply_fill`/`peer_fill`/`catalog_fill`) appends to `decisions.jsonl` (`jev_status=fill` for fills) — read stats with `decisions.py`.

CLI (from this repo):

```text
python skills/jev-consult/scripts/jev.py ask skills/jev-consult/examples/jwt-auth.request.json --trace
python skills/jev-consult/scripts/jev.py scaffold keep_vs_change --out request.json --plan "<task>"
python skills/jev-consult/scripts/inventory.py --task "<task>" --harness auto --write-ask tools.request.json
python skills/jev-consult/scripts/inventory.py --check-sidecar [path]   # fresh/stale/missing/invalid
python skills/jev-consult/scripts/inventory.py --check-miss [path]      # same statuses for .jev-tools-miss.json
python skills/jev-consult/scripts/inventory.py --prune-sidecars DIR     # unlink stale/invalid .jev-tools*.json (--dry-run lists)
python skills/jev-consult/scripts/inventory.py --show FILE              # sidecar payload + status + age_seconds
python skills/jev-consult/scripts/inventory.py --show-policy            # effective policy.json contents
python skills/jev-consult/scripts/inventory.py --out PATH               # write the payload JSON to a file instead of stdout
python skills/jev-consult/scripts/policy_lint.py [--strict|--show|--diff other.json]  # validates policy.json
python skills/jev-consult/scripts/question_lint.py request.json [--json|--fix|--out PATH]  # lint a request file standalone
python skills/jev-consult/scripts/skill_lint.py skills/*/SKILL.md       # SKILL.md sanity; [--strict] warns fail, [--fix] rewrites name, [--json]
python skills/jev-consult/scripts/doctor.py                             # verify per-harness install; exit 0 = all ok; --quiet/--out PATH
python skills/jev-consult/scripts/smoke.py                              # offline e2e sanity, no API calls; --only/--list/--fail-fast/--out PATH
python skills/jev-consult/scripts/decisions.py                          # stats + --days/--since/--harness/--status/--outcome/--fill
                                                                        # filters, --tail/--json/--csv/--md, count lists
                                                                        # --statuses/--harnesses/--winners/--outcomes/--fills/--fields,
                                                                        # --jsonl raw entries; --prune keeps only filtered
python skills/jev-consult/scripts/compare.py --live
python skills/jev-consult/scripts/compact.py transcript.json --history --fake   # --dir DIR for batch, --prune-spill S, --list-spill
python skills/jev-consult/scripts/trace.py show --key plan                      # single field from .jev-trace.json
python skills/jev-consult/scripts/trace.py stats                                # counts, last_pick, file age
python tests/test_jev.py
python tests/test_inventory.py
python tests/test_trace.py
python tests/test_compare.py
python tests/test_compact.py
python scripts/install.py
```

On this Windows host use `python`, not `python3`.
