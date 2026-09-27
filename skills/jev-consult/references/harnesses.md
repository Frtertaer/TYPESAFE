# Harness roots

Install copies `skills/jev-consult/` into **user** skill dirs so the mode works in any session of these four agents. It does **not** install Cursor, Gemini, Antigravity, or a bare `npx skills add`.

| Agent | Skill dir | Always-on pointer |
| --- | --- | --- |
| Hermes | `%HERMES_HOME%/skills/jev-consult` (this machine: `D:\Hermes\home\skills`) | Skill catalog description. Do not use `~/.hermes/skills` unless `HERMES_HOME` points there. |
| Claude Code (desktop) | `%USERPROFILE%\.claude\skills\jev-consult` | Append-only marked block in `%USERPROFILE%\.claude\CLAUDE.md` |
| Codex | `%USERPROFILE%\.codex\skills\jev-consult` and `%USERPROFILE%\.agents\skills\jev-consult` | Append-only marked block in `%USERPROFILE%\.codex\AGENTS.md` |
| Grok Build | `%USERPROFILE%\.grok\skills\jev-consult` | `%USERPROFILE%\.grok\AGENTS.md` (created if missing) |

Repo-local (committed; clone/open needs no copy):

- `AGENTS.md` / `CLAUDE.md` / `.hermes.md` at the root: **Jev decides**; the coder inspects and implements
- CLI: `python skills/jev-consult/scripts/jev.py`
- Inventory (harness-scoped): `python skills/jev-consult/scripts/inventory.py --task "<task>" --harness hermes|claude-code|codex|grok|cursor|gemini|windsurf|opencode`
- One command after clone: `python scripts/install.py` (`install.cmd` / `install.sh`)

Grok also reads `AGENTS.md` and `CLAUDE.md`. Codex user skills are documented as `%USERPROFILE%\.agents\skills`; some builds also scan `%USERPROFILE%\.codex\skills`. The installer writes both.

After editing `policy.json` or `scripts/jev.py` in the repo, re-run `python scripts/install.py` so the four copies update.

Live truncate (no CLI): `install.py` also copies Hermes plugin `jev-compact` (`transform_tool_result` + `pre_llm_call`) and writes Claude/Grok `PostToolUse` (`compact_hook.py`) plus Claude/Grok/Codex `UserPromptSubmit` (`inventory_hook.py`). Codex lives in `%USERPROFILE%\.codex\hooks.json` (not `config.toml`); untrusted until `/hooks`. Codex has no compact `PostToolUse`. The tools hook IDF-shortlists then one Jev pick (8s, fail-open). Empty shortlist writes `.jev-tools-miss.json`; the agent runs `peer_fill.py --from-miss`, then `catalog_fill.py --from-miss` if stdout is `no_peer`, then `apply_fill.py --from-miss` if stdout is `no_catalog` (one Hermes plugin `--no-enable` or one official MCP). Hook does not install. Never `--force`. Never npx. Never `claude plugin install`. Session-history drop is not the default.
