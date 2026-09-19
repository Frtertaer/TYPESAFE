# Harness roots

Install copies `skills/jev-consult/` into **user** skill dirs so the mode works in any session of these four agents. It does **not** install Cursor, Gemini, Antigravity, or a bare `npx skills add`.

| Agent | Skill dir | Always-on pointer |
| --- | --- | --- |
| Hermes | `%HERMES_HOME%/skills/jev-consult` (this machine: `D:\Hermes\home\skills`) | Skill catalog description. Do not use `~/.hermes/skills` unless `HERMES_HOME` points there. |
| Claude Code (desktop) | `%USERPROFILE%\.claude\skills\jev-consult` | Append-only marked block in `%USERPROFILE%\.claude\CLAUDE.md` |
| Codex | `%USERPROFILE%\.codex\skills\jev-consult` and `%USERPROFILE%\.agents\skills\jev-consult` | Append-only marked block in `%USERPROFILE%\.codex\AGENTS.md` |
| Grok Build | `%USERPROFILE%\.grok\skills\jev-consult` | `%USERPROFILE%\.grok\AGENTS.md` (created if missing) |

Repo-local (this git tree, no install needed while cwd is the repo):

- `AGENTS.md` / `CLAUDE.md` at the root
- CLI: `python skills/jev-consult/scripts/jev.py`

Grok also reads `AGENTS.md` and `CLAUDE.md`. Codex user skills are documented as `%USERPROFILE%\.agents\skills`; some builds also scan `%USERPROFILE%\.codex\skills`. The installer writes both.

After editing `policy.json` or `scripts/jev.py` in the repo, re-run `python scripts/install.py` so the four copies update.
