---
name: testing-jev-installer
description: How to end-to-end test the jev-consult installer (install.sh/install.py --setup/jev-setup.pyz) safely in a real terminal without polluting real harness dirs or leaking the API key.
---

# Testing the jev-consult installer end-to-end

Applies to `install.sh`, `install.cmd`, `scripts/install.py --setup`, and the
`dist/jev-setup.pyz` bundle in this repo.

## Isolate with a temp home

- `install.py` resolves its user dirs from `USERPROFILE` then `HOME`
  (`user_home()`), and `HERMES_HOME` overrides the hermes root. On Linux
  `export HOME=/tmp/jev-home` in the test terminal is enough; also check
  `HERMES_HOME`/`USERPROFILE` are unset first (`env | grep -E 'USERPROFILE|HERMES'`).
- Setup writes `.env` files under the temp home AND into `repo_root()/.env`
  (the real repo when run without `--source`; the extracted payload when run
  from the pyz). Delete `<repo>/.env` afterwards; it is gitignored but holds
  the key. Repo instruction files (AGENTS.md/CLAUDE.md/.hermes.md) already
  carry the marker block so the upsert is idempotent — verify with
  `git status --porcelain`.

## Drive the interactive menu in a GUI terminal

- Open a real terminal (konsole on this box), maximize with
  `wmctrl -r :ACTIVE: -b add,maximized_vert,maximized_horz`, then run
  `script -f /tmp/jev-session.log`. Everything inside that pty is logged —
  later `grep -c 'YOUR_FAKE_KEY' /tmp/jev-session.log` must return 0 to prove
  getpass never echoes and the installer never prints the value.
- Detection (`env_report.targets ∩ existing`) only counts a harness when one
  of its target paths exists: seed e.g. `mkdir -p "$HOME/.claude/skills"
  "$HOME/.hermes/skills"` — a bare `~/.claude` dir alone is NOT detected.
  Seed `~/.hermes/skills` if you want the doctor subprocess to exercise
  `--hermes-home`.
- Menu accepts digits + Enter; the computer `type` action works but menu
  echoes may lag behind re-rendered output — send digits via `key` if
  screenshots look stale, and trust the session log over the screen.
- getpass masks input: after `type`ing the fake key the screen must show no
  characters — screenshot before pressing Enter.
- Never `cat` .env files during verification (prints the key). Use
  `grep -c '^TYPESAFE_API_KEY='` / `awk -F= '{print $1}'` / `stat -c %a`.

## Non-TTY paths

- `python scripts/install.py --setup </dev/null` → rc 2 + TTY guidance.
- Post-review-fix wrappers gate `--setup` on `[ -t 0 ] && [ -t 1 ]`:
  `HOME=/tmp/x sh install.sh </dev/null` does a *plain install of all four
  agents* (rc 0), no menu — verify that path too.
- The pyz deletes its extracted payload dir on exit; the installed
  `.jev-consult-source` marker still records the (now-deleted) payload path —
  read it after exit, it should match `/tmp/jev-setup-*/payload/skills/jev-consult`.

## Known gotchas

- `doctor: FAIL` is expected without a real `TYPESAFE_API_KEY`/config.yaml
  (`plugin_enabled` check fails on missing `~/.hermes/config.yaml`). What
  matters is it runs and reports PASS|FAIL.
- Uninstall intentionally does NOT remove .env files — that's by design.
- The unittest suite leaks Windows-path fixture files (`N:\no\such\dir\...`,
  incl. a dir whose name contains a real newline from `Path("N:\no\such\dir")`
  in test_inventory.py) into the repo cwd on POSIX. Check `git status` before
  AND after a session so you don't attribute pre-existing junk to the PR.

## Devin Secrets Needed

- `TYPESAFE_API_KEY` is NOT provisioned on the box; use a distinctive fake
  (e.g. `TSK-TEST-SECRET-9999`) so the secrecy grep is unambiguous.
