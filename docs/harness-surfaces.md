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
  `~/.cursor/rules/jev-consult.mdc` with `alwaysApply: true`.
- `.mdc` rule pickup (verified 2026-09-26 against current docs +
  changelog, not a live Cursor run): `~/.cursor/rules/*.mdc` is the
  documented machine-local user-rules path — supported since Cursor 2.1
  ("rules in home folder will be included in context"), no account sync.
  Caveats: a confirmed pickup bug (forum #147236, Dec 2025, staff
  acknowledged) means the file is not guaranteed to enter context on
  affected builds; cloud agents don't read `~/.cursor` at all; and
  `alwaysApply` only guarantees inclusion, not adherence. Net: the
  `hooks.json` `beforeSubmitPrompt` entry is the enforced channel (it
  fires independently of rules pickup); the `.mdc` is belt-and-braces
  for agents that do read it. Project rules are `<repo>/.cursor/rules/`
  — the repo-level `AGENTS.md`/`CLAUDE.md` marker blocks already cover
  that slot, and Cursor auto-reads `AGENTS.md`.
- `postToolUse` exists but can only rewrite MCP-tool
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
- Extensions: `~/.gemini/extensions/<name>/gemini-extension.json`
  (manifest with `name`/`version`, optional `mcpServers`,
  `contextFileName`, bundled commands/skills). `roots_for("gemini")`
  maps that dir to `plugins` and `scan()` reads the manifest via
  `iter_gemini_extensions` — extensions show up as `kind: plugin`
  items (version + mcp/context presence in the description). Supported
  in inventory; extension-bundled skills under
  `extensions/<name>/skills/` are **not** scanned (decision: the
  extension item itself is the unit Jev sees).

## Windsurf (Devin Desktop) — `docs.windsurf.com`, `docs.devin.ai`

- Hooks: `~/.codeium/windsurf/hooks.json`, 12 events
  (`pre_write_code`, `post_run_command`, `pre_user_prompt`, …).
  `pre_user_prompt` is **block-only** (exit code 2 blocks; there is no
  context-injection field and `show_output` does not apply) — so no
  `inventory_hook.py` channel exists.
- Global rules: `~/.codeium/windsurf/memories/global_rules.md`
  (always-on, ≤6000 chars). Global skills:
  `~/.codeium/windsurf/skills/`. MCP: `~/.codeium/windsurf/mcp_config.json`.
- No prompt-bearing headless CLI (`windsurf` opens the IDE) → doctor
  `--live` intentionally skips it.
- Status: WIRED (skills + rules only) — `install.py` writes the skill to
  `~/.codeium/windsurf/skills/jev-consult`, upserts the marker block in
  `global_rules.md`, seeds `TYPESAFE_API_KEY` in
  `~/.codeium/windsurf/.env`, and prints the no-hook note.

## opencode — `opencode.ai/docs`

- Plugins: `~/.config/opencode/plugins/*.ts`. The
  `experimental.chat.messages.transform` hook receives session info +
  `output.messages` and must mutate parts in place (reassigning
  `output.messages` is a silent no-op).
- Global skills: `~/.config/opencode/skills/` (+ `~/.agents/skills`
  compat, shared with codex). Instructions:
  `~/.config/opencode/AGENTS.md`. MCP servers live under `"mcp"` in
  `~/.config/opencode/opencode.json` (not `mcpServers`).
- Headless prompt run: `opencode run "prompt"` → `doctor --live`
  probe.
- Status: WIRED — `install.py` writes the skill to
  `~/.config/opencode/skills/jev-consult`, upserts the marker block in
  `AGENTS.md`, seeds `.env`, and generates
  `~/.config/opencode/plugins/jev-consult.ts`: a transform plugin that
  pipes `{hook_event_name: "UserPromptSubmit", prompt, cwd}` to
  `inventory_hook.py` (env `JEV_HOOK_HARNESS=opencode`) and appends the
  `additionalContext` reply as a text part.

## Still unevaluated

`antigravity`, `cline`, `aider`, `copilot` —
no comparable hook surface confirmed; stay BLOCKED.
