# Harness surface evaluation — Cursor & Gemini

Evaluation of the hook/instruction surfaces of harnesses currently in
`install.py`'s BLOCKED set. Both are viable extension targets; they stay
blocked until the per-harness protocol work below lands. Checked
2026-09-26 against vendor docs; re-verify before wiring — hook event
names move fast.

## Cursor — `cursor.com/docs/hooks.md`, `cursor.com/docs/reference/third-party-hooks.md`

- Config: `hooks.json` at project `.cursor/hooks.json`, user `~/.cursor`,
  or org level via the dashboard. `{"version": 1, "hooks": {...}}`,
  `type: "command"` entries, regex matchers, exit-code-2 blocking.
- Relevant events (camelCase, not Claude's PascalCase):
  - `beforeSubmitPrompt` — maps Claude Code `UserPromptSubmit`; stdout
    `hookSpecificOutput.additionalContext` injects context → the
    `inventory_hook.py` slot.
  - `preCompact` — maps `PreCompact` → the `compact_hook.py` slot.
  - Claude hook names in a `matcher` field are auto-mapped
    (`UserPromptSubmit` → `beforeSubmitPrompt`), but event names are not.
- Gaps to close before unblocking: project hooks only run in *trusted
  workspaces* (same trust problem as Codex — needs the same one-time
  step doc); `beforeSubmitPrompt` does not fire for cloud agents;
  `targets()` needs a `.cursor` skill dir + `AGENTS.md`-style instruction
  file (Cursor reads `.cursor/rules` / `AGENTS.md`).

## Gemini CLI — `geminicli.com/docs/hooks/`

- Config: `hooks` object inside `settings.json`
  (`~/.gemini/settings.json` or project `.gemini/settings.json`).
- Relevant events:
  - `BeforeAgent` — after prompt submit, before planning;
    `hookSpecificOutput.additionalContext` appends to the prompt → the
    `inventory_hook.py` slot.
  - `PreCompress` — before context compression → the `compact_hook.py`
    slot.
- Gaps to close before unblocking: settings.json is a shared config —
  `upsert` must merge into the existing `hooks` object (same shape as
  `upsert_claude_event`, new paths); hook entry schema is
  `{matcher, hooks: [{name, type: "command", command}]}` — nested one
  level deeper than Claude's; `targets()` needs `~/.gemini` skill dir +
  `GEMINI.md` instruction file mapping.

## Still unevaluated

`antigravity`, `windsurf`, `cline`, `aider`, `copilot`, `opencode` —
no comparable hook surface confirmed; stay BLOCKED.
