---
name: jev-consult
description: Use when implementing, refactoring, choosing architecture or a library, deleting code, or judging if work is good enough. Ask Jev (TypeSafe) at decision gates; user may supply only a plan.
when-to-use: jev, typesafe, consult, architecture, refactor, library choice, delete code, good enough, coding decision
---

# Jev consult

The user gives a high-level task or plan. You keep the repo, write the code, and ask **Jev** (TypeSafe System One) only at semantic forks.

Jev is not a senior in chat. It never says "do this next" in prose. It returns:

- **Choice** — one option + full probabilities + confidence
- **Noul** — P(yes) in `[0, 1]`; **no confidence**; `0.5` means equally yes/no, not "medium"
- **Score** — weighted value on your rubric + confidence

You translate that into an action, or you escalate to the user.

## When to ask

Must ask before: architecture change, deleting code, refactor vs rewrite, choosing a library, "is this good enough?".

Do **not** ask Jev for facts a tool can check: file exists, tests red, grep, compiler/syntax.

Batch independent questions in **one** request (policy `soft_max_questions`, hard cap 32). Second request only if you need new facts or options from the first answer. Jev has **no memory** — every call needs a fresh `state`.

`state`: named JSON fields with **code excerpts and facts**, not just filenames.

Choice: options from the actual repo. If "do nothing" is possible, include hatch `none` or `other`.

## How to call

Skill directory = folder that contains this `SKILL.md`.

```text
python scripts/jev.py ask request.json
python scripts/jev.py decide answers.json --irreversible
python scripts/jev.py ping
```

On Windows this host, `python` exists; `python3` may not.

`ask` JSON:

```json
{
  "state": {"task": "...", "excerpts": {}, "constraints": []},
  "questions": {},
  "irreversible": false
}
```

Copy question shapes from `policy.json` → `templates`, then fill real options. Do not invent API fields.

Exit `0` = policy says proceed. Exit `2` = escalate or reformulate. Never print `TYPESAFE_API_KEY` or `Authorization`.

## Policy (edit `policy.json`, not this file)

- Choice: take **max probability**, not a threshold on every option.
- Escalate Choice if `confidence` < `escalate_if_confidence_below`, or (irreversible and top-two gap too small).
- Noul: `>= yes_above` yes, `<= no_below` no; the band between is uncertain. Uncertain + irreversible → escalate.
- Low Score confidence → escalate.
- Low confidence on an irreversible step: ask the user, or rebuild `state` and ask Jev again. Do not guess.

## Loop

1. User gives only the plan.
2. You inspect the repo with tools.
3. If a must-ask gate applies, write `request.json`, run `ask`.
4. `proceed` → implement. `escalate` → user or a tighter question.
5. Repeat only at the next gate, not every edit.

## Key

Read `TYPESAFE_API_KEY` from the environment or a local `.env`. Do not commit it. Do not paste it into chat.
