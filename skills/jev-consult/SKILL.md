---
name: jev-consult
description: Use on every coding or planning task. Jev decides; you inspect and implement. Also use when off-track, stuck, looping, you do not know, you do not remember, or picking skills/plugins/MCP. Call it before choosing an approach, library, architecture, name, or between delete, refactor, rewrite, keep vs change; before the next move; and when deciding if the work is good enough.
compatibility: >-
  Makes network calls: scripts/jev.py POSTs to the TypeSafe API using
  TYPESAFE_API_KEY from the environment. Python 3 stdlib only, no install-time
  execution, never installs marketplace items on its own.
---

# Jev consult

You inspect this repo and write code. **Jev decides.** Default: call Jev. Skip only facts a tool can check, and mechanical follow-through of a pick Jev already made this session.

Jev is TypeSafe System One. Answers are only Choice, Noul, or Score. Jev has no memory: every call needs current `state`. Jev cannot see files, skills, plugins, MCP, or a marketplace. Jev cannot write "go this way" in prose. A redirect is the chosen option.

## Limits (read this)

Jev does **not** maximally eliminate hallucinations. A 0% invalid-type rate is not a 0% wrong answer. Do not ask Jev "is this Wikipedia fact true" with empty evidence — that is another guess. Cut invented *decisions* by forcing a Choice among options you inspected. Check facts with tools. If evidence is in `state`, you may Noul `grounded_enough` before proceeding.

There is **no watchdog** in Hermes, Claude Code, Codex, or Grok. If you skip this skill, Jev is not called.

Never print `TYPESAFE_API_KEY`. Never auto-install marketplace items.

## How to call

```text
python skills/jev-consult/scripts/jev.py ask request.json --trace
python skills/jev-consult/scripts/jev.py scaffold keep_vs_change --out request.json --plan "<task>"
python skills/jev-consult/scripts/jev.py lint request.json
python skills/jev-consult/scripts/policy_lint.py  # validate policy.json before editing thresholds/templates
```

Repo copy: `python skills/jev-consult/scripts/jev.py`. Installed user copy: `scripts/jev.py` next to this skill.

Write `request.json` from `policy.json` templates (`scaffold` copies them; add `--option ID=key:label` when criteria are empty). Put real options in `criteria`. Include a hatch (`none` / `other`) when the policy says so. Pass `state`: human plan, current step, files, attempt count, last error, shortlist.

Read `decision.action`. `proceed` → take `answers`. `escalate` → stop and ask the human. If `ask` stdout has no `decision.action`, do not pick architecture, library, or approach. Scaffold then ask.

Thresholds live only in `policy.json`.

## Jev owns

`approach`, `keep_vs_change`, `architecture`, `library`, `delete`, `refactor_vs_rewrite`, `naming`, `good_enough`, `on_track`, `stuck_move`, `unknown_move`, `load_tools`, `installed_enough`, `market_tools`.

You own inspection, inventory, marketplace search (names only), code, tests, and translating the typed answer into an action.

## Off-track

If the current step is not the human plan (wrong file, extra feature, different stack):

1. Put `plan` and `current_step` in `state`.
2. Noul `on_track`.
3. If no / unsure: Choice `next_move` (`return_to_plan` / `change_approach` / `ask_human` / `none`).
4. Do that option. Do not invent a fourth path.

## Stuck

Call Jev when you repeat the same edit, hit the same error twice, make no file/test progress, or cannot solve. Jev cannot see a loop unless `state` has it:

- `attempt_count`
- `last_error`
- `what_did_not_change`

Then Choice `stuck_move` (`change_approach` / `narrower_scope` / `ask_human` / `stop` / `none`). Do not retry the same failing step after that pick.

## Don't know

When you do not know the next step (no approach, no library, no move):

1. Do not invent. Put `unknown` and what you already inspected in `state`.
2. Choice among those real options plus `ask_human` / `none`. If it is a loop, use `stuck_move` instead.
3. Do that option.

If you do not remember the plan, load `.jev-trace.json` first (`--trace`). If it is missing, `unknown` is the plan. Template: `examples/forget.request.json`.

Jev only sees this request. Labeled examples belong in this `state` or in criteria `examples` arrays — they do not accumulate between calls. Jev does not raise an LLM's intelligence; it only picks.

## Don't stall

Do not keep writing an answer when you lack a next file, command, or Jev pick. That stall is `unknown_move` (or `stuck_move` if you already looped). There is no watchdog: if you skip this skill, Jev is not called.

## Trace

Jev and a new session forget. `.jev-trace.json` is the memory you stuff into each ask.

```text
python skills/jev-consult/scripts/trace.py init --plan "<human task>"
python skills/jev-consult/scripts/jev.py ask request.json --trace
python skills/jev-consult/scripts/trace.py record --pick return_to_plan --kind next_move
python skills/jev-consult/scripts/trace.py state --out state.json  # bare state dict for scaffold --state
python skills/jev-consult/scripts/trace.py notes  # list notes (--json for the array, --limit N caps rows); record --note appends one
```

## Compare

Same sticky prompts, unguarded vs trace+Jev. Offline lists defects; `--live` scores `on_track` / `grounded_enough`. This does not raise IQ.

```text
python skills/jev-consult/scripts/compare.py
python skills/jev-consult/scripts/compare.py --live
```

## Compact

Default is **LIVE_FAT only**: truncate the current fat tool result (>32k chars) as it arrives. Hermes `transform_tool_result`, Claude and Grok `PostToolUse` `updatedToolOutput`. Errors and small reads stay. The omitted middle is saved whole to `~/.cache/jev-consult/spill/<sha>.txt` and the marker names the file (cap 200, `JEV_CONSULT_SPILL=0` disables). Not a watchdog. No second-LLM summary. No post-compact resync.

Do **not** run session-history drop as the default. Hermes eval (Teknium, 2026-09-20) did not adopt Tamara retention: it deletes old tool calls, breaks the prompt cache, and loses to production summary. Codex has no mutate hook — that is not a reason to rewrite dumps.

`compact.py --history` still exists as opt-in (Python port of `vendor/fast-jev-compaction`, MIT). Do not call it from hooks. `--keep-text PATTERN` pins messages/tool calls whose text, tool name, or input matches the regex — never dropped regardless of the asker; an invalid regex is ignored. `--dry-run` reports decisions+stats while returning the messages unchanged. Other opt-ins: `--dir DIR` batch, `--list-spill`, `--prune-spill S`.

```text
python skills/jev-consult/scripts/compact.py transcript.json --history --trace .jev-trace.json
```

## Session tools

Native harness tools are already in the session. A hook IDF-shortlists **already-installed** skills / plugins / MCP, then one Jev Choice + `need_skill` (8s timeout, fail-open to the IDF list). The user does not pick tools. Hook never installs.

| Harness | What runs without the user |
| --- | --- |
| Hermes | plugin `jev-compact` `pre_llm_call` injects up to 6 names |
| Claude Code | `UserPromptSubmit` → `inventory_hook.py` additionalContext |
| Grok | `UserPromptSubmit` writes `.jev-tools.json` (stdout context is discarded) |
| Codex | `UserPromptSubmit` → `~/.codex/hooks.json` → `inventory_hook.py` additionalContext (trust in `/hooks` or Codex skips it). If skipped: miss file → `peer_fill` / `catalog_fill` / `apply_fill`; else sidecar or inventory then Jev |

Hook may call Jev **once** on the IDF shortlist (`load_tools` + `need_skill`). Timeout fail-open. Winner injects one `<skill_relevance>` line; `none` injects nothing. A repeat of the exact same prompt in a directory with a fresh `.jev-tools.json` replays that sidecar's pick without a second Jev call (`dedupe: true` in the log entry). A choice with `load_tools` probability ≥ `strong_pick` (0.85 in `policy.json`) wins even when `need_skill` is unsure. Every hook decision is appended to `~/.cache/jev-consult/decisions.jsonl` (`JEV_CONSULT_LOG=0` disables). Hook never installs. Never `--force`. Never npx.

`.jev-tools.json` and `.jev-tools-miss.json` sidecars carry `written_at`. They are fresh for `sidecar_ttl_seconds` (policy.json, 4h default). Check with `python scripts/inventory.py --check-sidecar` (`fresh` / `stale` / `missing` / `invalid`, plus `age Ns` when parseable). Stale sidecar → ignore it and re-run `inventory.py`; `read_miss` already returns `{}` on stale miss files. `python scripts/inventory.py --prune-sidecars DIR` unlinks stale/invalid `.jev-tools*.json` under DIR recursively (`--dry-run` lists without deleting); `--show FILE` dumps the parsed payload plus `age_seconds`. Pass `--ttl SECONDS` to override the policy TTL on any of those; the `JEV_HOOK_TTL` env var (seconds) overrides it everywhere, including the hooks.

Log maintenance: `python skills/jev-consult/scripts/decisions.py` prints stats (`--days N` / `--week` / `--since`/`--until EPOCH-or-ISO8601` window, `--harness` / `--status` / `--outcome` / `--fill` / `--field KEY=VAL` filters, `--tail N`, `--json`, `--csv`, `--md`, `--jsonl`, and count lists `--statuses` / `--harnesses` / `--winners` / `--outcomes` / `--fills` / `--fields` (`--top N` caps rows)); `--prune` rewrites decisions.jsonl keeping only entries matching the filters (requires at least one).

Environment: `TYPESAFE_API_KEY` authorizes Jev calls (never print it). `JEV_CONSULT_LOG=0` disables `decisions.jsonl`. `JEV_CONSULT_SPILL=0` disables spill writes. `JEV_HOOK_DEBUG=1` makes the hook echo the last decision (`jev_status`, `winner`, `dedupe`, `shortlist` size, `latency_ms`) to stderr; `JEV_HOOK_DEBUG_FILE=<path>` appends the same line to a file (fail-open on bad path). `JEV_HOOK_TTL` overrides `sidecar_ttl_seconds` (seconds); `JEV_HOOK_TIMEOUT` overrides `hook_jev_timeout_seconds`; `JEV_HOOK_CWD` supplies the cwd when the payload lacks one; `JEV_HOOK_EVENT` overrides the payload's event name; `JEV_HOOK_LIMIT` overrides the hook shortlist size (`hook_limit` in policy.json, 6 default); `JEV_HOOK_RETRIES` overrides `hook_jev_retries` (0 default); `JEV_HOOK_BUDGET` overrides `hook_budget_seconds`; `JEV_KEEP_TEXT` supplies the compact pin pattern; `JEV_PRESERVE_RECENT` sets the default compact tail-pin count; `JEV_KEEP_THRESHOLD` sets the default compact keep threshold; `JEV_TRUNCATE_HEAD` sets the default compact head-truncation chars; `JEV_MIN_REDUCTION` sets the default compact minimum-reduction gate; `JEV_LIMIT` sets the default inventory shortlist size; `JEV_DECISIONS` overrides the decisions.jsonl path `decisions.py` reads; `JEV_HOOK_HARNESS` overrides harness detection when no `harness` arg is passed; `HERMES_HOME` makes harness detection return `hermes` when the script path gives no hint; `JEV_HOOK_OFF=1` disables the hook entirely (returns `{}`); `JEV_HOOK_WINNER=<name>` force-picks that installed item as an explicit winner; `JEV_HOOK_NOSIDECAR=1` skips sidecar/miss writes (note still emitted); `JEV_HOOK_NOMISS=1` skips only the miss marker. `JEV_TRACE` sets the default trace file `trace.py` reads.

The hook enforces a wall-clock budget: `hook_budget_seconds` (policy.json, 12s default) caps each invocation; on timeout it skips the Jev pick, logs `jev_status=budget`, and returns the IDF shortlist without a winner (fail-open).

If `.jev-tools-miss.json` exists, do not ask the user. Run:

```text
python skills/jev-consult/scripts/peer_fill.py --from-miss
python skills/jev-consult/scripts/catalog_fill.py --from-miss
python skills/jev-consult/scripts/apply_fill.py --from-miss
```

`peer_fill.py` first: Jev picks one skill already on this machine and copies it (`--json` emits `{"outcome": ...}` objects instead of bare tokens). If stdout is `no_peer`, `catalog_fill.py` searches Hermes find-skill, Jev picks one identifier (exploit/attack/hack names dropped), inspects, then `hermes skills install --yes`. If stdout is `no_catalog`, `apply_fill.py` searches Hermes plugins + official MCP, Jev picks **one** plugin (`install --no-enable`) or **one** official MCP (`mcp install`). Hermes only. Other harness markets stay human. Codex: same three commands if `/hooks` has not trusted `hooks.json`.

If you are stuck on an installed set that does not fit:

```text
python skills/jev-consult/scripts/inventory.py --task "<task>" --harness auto --write-ask tools.request.json
python skills/jev-consult/scripts/jev.py ask tools.request.json
```

`--harness` is `hermes`, `claude-code`, `codex`, or `grok`. The script scans all installed items, keyword-filters a shortlist (Choice cap 255; keep ≤12), and writes `load_tools` + `installed_enough`. Pin a missed name with `--include`. `--scores` adds the IDF score to each shortlist item. Do not dump hundreds of options on Jev.

If `installed_enough` is no and `peer_fill` printed `no_peer`, run `catalog_fill.py --from-miss` (one skill, inspect, `install --yes`). If that prints `no_catalog`, run `apply_fill.py --from-miss` (one Hermes plugin `--no-enable` or one official MCP). **Do not install** npx, `claude plugin install`, Git URLs, or skillbox. Never `--force`. Hook does not install.

Mid-session: same path. You ask Jev; Jev only chooses.

Trigger evals: `trigger-cases.json` in the repo's test fixtures lists prompts that must (and must not) route to this skill. The vendored `run_trigger_evals` tool scores each case's lexical overlap with this SKILL.md description — every positive must clear the strongest negative by a 1.15 margin (`"lexical": false` cases skip the lexical tier). `covers` names must match `must_ask` kinds in `policy.json`, and every kind needs at least one positive case. A failure means fix the description, not the scorer.

## After clone

```text
python scripts/install.py
python skills/jev-consult/scripts/doctor.py  # verify hooks/skill/key/policy per harness; exit 0 = all ok; failing checks carry a `hint`
```
