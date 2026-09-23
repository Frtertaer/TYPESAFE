# jev-consult examples

Ready-made fixtures for every consumer in the pack — lint them, feed them to
`jev.py`, or use them as templates for your own files.

## Request files (`*.request.json`)

Jev question bundles (`{state, questions}`) — the input `jev.py ask` POSTs and
`jev.py decide`/`question_lint.py`/`jev.py lint` validate offline.

| file | questions | use |
| --- | --- | --- |
| `forget.request.json` | `unknown_move` | template for "plan forgotten" asks — SKILL.md cites it for `--trace` misses |
| `jwt-auth.request.json` | `where`, `touch_middleware`, `risk` | multi-question ask with `irreversible` set |
| `market-tools.request.json` | `market_tools` | marketplace/tool-pick question shape |
| `off-track.request.json` | `on_track`, `next_move` | drift check + recovery pick in one ask |
| `session-tools.request.json` | `load_tools`, `installed_enough` | the hook's own tool-shortlist question pair |
| `stuck.request.json` | `stuck_move` | unstick pick after failed attempts |
| `unknown.request.json` | `unknown_move` | minimal single-question scaffold target |

Try one:

```text
python ../scripts/jev.py lint jwt-auth.request.json
python ../scripts/question_lint.py *.request.json
python ../scripts/jev.py decide <answers-file>   # after an ask
```

## Other fixtures

| file | consumer |
| --- | --- |
| `compare-cases.json` | `compare.py --cases` — unguarded vs trace+Jev sticky prompts |
| `compact-transcript.json` | `compact.py --history` — small transcript to dry-run compaction on |
| `progress-plan.json` | `progress.py lint` / `progress.py init` — stage plan with checks + items |
| `trace.template.json` | `.jev-trace.json` skeleton — copy it or let `trace.py init` write one |
