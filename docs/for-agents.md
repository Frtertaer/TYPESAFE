# jev-consult

Committed at repo root (do not copy by hand): `AGENTS.md`, `CLAUDE.md`, `.hermes.md`.

User gives a plan. The coding agent inspects the repo. At semantic gates it asks Jev via `skills/jev-consult/scripts/jev.py`, then writes the code.

Load `skills/jev-consult/SKILL.md` before implementing, refactoring, choosing architecture or a library, deleting code, or judging "good enough".

Do not ask Jev for facts a tool can check. Jev has no memory: send a fresh `state` every call. Edit thresholds only in `skills/jev-consult/policy.json`. Never print `TYPESAFE_API_KEY`.

CLI (from this repo):

```text
python skills/jev-consult/scripts/jev.py ask skills/jev-consult/examples/jwt-auth.request.json
python tests/test_jev.py
python scripts/install.py
```

On this Windows host use `python`, not `python3`.
