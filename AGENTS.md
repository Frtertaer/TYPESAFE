

<!-- jev-consult:start -->
# jev-consult

User gives a plan. You inspect this repo. At architecture, deleting code, refactor vs rewrite, library choice, or "is this good enough", load `skills/jev-consult/SKILL.md` and run:

```text
python skills/jev-consult/scripts/jev.py ask request.json
```

Thresholds live only in `skills/jev-consult/policy.json`. Do not ask Jev for facts a tool can check. Never print `TYPESAFE_API_KEY`.

After clone, one command wires user-scope Hermes / Claude Code / Codex / Grok Build:

```text
python scripts/install.py
```
<!-- jev-consult:end -->
