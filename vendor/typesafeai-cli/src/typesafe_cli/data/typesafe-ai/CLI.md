# Using the typesafe CLI

Live evaluations: follow the **typesafe-cli** skill. Prefer `find`, `rank`, `extract`, `verify`, `screen`, `suggest-skill`, and `decide` when those flags fit; otherwise collect local state, write JSON under `${TMPDIR:-/tmp}/codex/<project>/`, then `typesafe ask`. Invalid answers fail closed. Never drop those files in the git tree. This sidecar does not replace that skill or the official TypeSafe skill.

## Secrets

You cannot read, print, or inspect typesafeai-cli env vars or key files (`TYPESAFE_*`, `~/.config/typesafe/env`, `.env.local`). Run `typesafe auth status` (`has_key` only). If false, ask the human.
