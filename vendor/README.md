# vendor/

Inspect-only snapshots. Runtime is `skills/jev-consult`, not these trees.

| Path | Upstream | Why it is here |
| --- | --- | --- |
| `fast-jev-compaction` | Tamara / MIT | Already ported to `compact.py --history` |
| `awesome-jev` | [AppitStudio/awesome-jev](https://github.com/AppitStudio/awesome-jev) CC0 list + MIT examples | Catalog/docs for Jev patterns |
| `typesafeai-cli` | [maddygoround/typesafeai-cli](https://github.com/maddygoround/typesafeai-cli) MIT | Python CLI (`ask` / `decide` / `screen` / `verify`). Our live client stays `jev.py` (no extra SDK). |
| `awesome-llm-apps-skill-evals` | [Shubhamsaboo/awesome-llm-apps](https://github.com/Shubhamsaboo/awesome-llm-apps) Apache-2.0 | Skill evals tools only (`skill_lint`, `skill_scanner`, `run_trigger_evals`). Runtime scanner is `skills/jev-consult/scripts/skill_scanner.py`. |
| `jev-skill-suggester` | [win4r/jev-skill-suggester](https://github.com/win4r/jev-skill-suggester) MIT @ 05fbd7ce | Two-stage skill suggestion; ported pieces: response validation, no-redirect+retry, secret guard, untrusted-metadata rule, block-scalar frontmatter, explicit-only, explicit mention. We do not run their CLI. |
| `jevcal` | jevcal MIT @ ae8f314 | Jev question lint rules J001–J021; ported to `question_lint.py` (J010 sharpened). We do not run their CLI. |
| `skill-router` | [lomeshdutta/skill-router](https://github.com/lomeshdutta/skill-router) MIT @ 4c538d8 | Per-session skill routing; ported pieces: `decide` probabilities, `strong_pick` threshold, decisions.jsonl log. |
| `omp-jev-compaction` | omp-jev-compaction MIT @ 3719495 | Context compaction for OpenCode; ported pieces: content-addressed spill to disk (`compact.spill`), truncation marker naming the spill file. |

Not cloned: SkillRanker (Rust + OpenAI/Anthropic rider), Node/`npx` tools, unlicensed Jev Sift, Telegram/Discord/Chrome/voice apps.
Inspected, not vendored: abide @ f268382 (MIT) — Jev said no (rules_hook 0.17): TS/npm surface, a new Stop-hook, and the user already has a mechanical Stop gate.

Jev 2026-09-20: `library` = `clone_python_tools`.
Jev 2026-09-21: approach = `evals_only`.
Jev 2026-09-21: approach = `harden_core`.
Jev 2026-09-21: approach = `lint_log_spill` (jevable.com set).
