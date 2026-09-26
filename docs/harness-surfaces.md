# Harness surface evaluation — Cursor & Gemini

Evaluation of the hook/instruction surfaces of harnesses formerly in
`install.py`'s BLOCKED set. Both are now wired (see "Status" under each);
checked 2026-09-26 against vendor docs — hook event names move fast.

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
- Status: WIRED — `install.py` writes `~/.cursor/hooks.json`
  `beforeSubmitPrompt` → `inventory_hook.py`, the skill to
  `~/.cursor/skills/jev-consult`, and the instruction rule to
  `~/.cursor/rules/jev-consult.mdc` (file-backed user rules; known to be
  flaky under the Agents window — Cursor Settings rules are the
  reliable path). `postToolUse` exists but can only rewrite MCP-tool
  output (`updated_mcp_tool_output`), so `compact_hook.py` is not wired.
  `beforeSubmitPrompt` does not fire for cloud agents.

## Gemini CLI — `geminicli.com/docs/hooks/`

- Config: `hooks` object inside `settings.json`
  (`~/.gemini/settings.json` or project `.gemini/settings.json`).
- Relevant events:
  - `BeforeAgent` — after prompt submit, before planning;
    `hookSpecificOutput.additionalContext` appends to the prompt → the
    `inventory_hook.py` slot.
  - `PreCompress` — before context compression; it carries no tool
    result, so nothing maps to `compact_hook.py` (PostToolUse-only).
- Status: WIRED — `install.py` merges `BeforeAgent` →
  `inventory_hook.py` into the `hooks` object of the shared
  `~/.gemini/settings.json` (`{matcher, hooks: [{name, type: command,
  command, timeout(ms)}]}` entries), the skill to
  `~/.gemini/skills/jev-consult`, instructions to `~/.gemini/GEMINI.md`.
  No tool-output rewrite event exists → no live compaction.

## Still unevaluated

`antigravity`, `windsurf`, `cline`, `aider`, `copilot`, `opencode` —
no comparable hook surface confirmed; stay BLOCKED.
