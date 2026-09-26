---
name: testing-setup-bundle
description: How to end-to-end test the jev-setup release bundle (pyz installer, PyInstaller exe entry, interactive --setup menu, doctor absent-vs-failed) on a Linux box
---

# Testing the jev-setup setup/release bundle

End-to-end testing for `scripts/install.py --setup`, `scripts/package_release.py`, `scripts/exe_entry.py`, and `skills/jev-consult/scripts/doctor.py` on Linux.

## Build artifacts

```bash
python3 scripts/package_release.py          # dist/jev-setup.pyz + dist/jev-setup.cmd
python3 scripts/package_release.py --exe    # stages dist/exe/ (entry, payload, jev-setup.spec, build-exe.cmd)
```

Build the frozen binary on Linux (PyInstaller cannot cross-compile to Windows, but a Linux ELF exercises the same exe_entry.py code paths):

```bash
python3 -m venv /tmp/pyi-venv && /tmp/pyi-venv/bin/pip install pyinstaller==6.21.0  # once
cd dist/exe && /tmp/pyi-venv/bin/python -m PyInstaller --clean --noconfirm --distpath .. --workpath build jev-setup.spec
# produces dist/jev-setup-windows-amd64 (ELF on Linux, .exe on Windows)
```

AST-derived hiddenimports may include Windows-only names (msvcrt) — PyInstaller warns and skips; build still succeeds.

## Isolate with a fake HOME

The installer keys everything off `USERPROFILE`/`HOME`. Always run with `HOME=/tmp/fh-x` and `HERMES_HOME` unset so hermes_home() falls back to `$HOME/.hermes`. Also `cd` to a scratch dir first — doctor reads `Path.cwd()/.env`, `.jev-tools.json`, and `.devin/progress.sqlite3` (the repo root has a `.jev-tools.json` which skews the `sidecars` check).

```bash
mkdir -p /tmp/jevtest && cd /tmp/jevtest
HOME=/tmp/fh-a ./dist/jev-setup-windows-amd64 </dev/null   # non-TTY: plain install, no menu/pause
```

## Driving the interactive menu

`--setup` auto-engages only when stdin AND stdout are TTYs. Non-interactive runs get plain install; `--setup` on a pipe exits rc=2 ("needs a TTY").

- For a recorded demo: `konsole` is installed; `wmctrl -i -r <id> -b add,maximized_vert,maximized_horz` to maximize; type via computer tool. `getpass` reads /dev/tty so typed keys never echo.
- For scripted checks: `pexpect` is installed (`script` and `expect` too). `python3 dist/jev-setup.pyz` under pexpect gets a real PTY.

Menu flow: `harnesses detected:` line -> `agents [hermes,claude-code,codex,grok]:` prompt (Enter = all) -> loop `choice [1-4]:`. Choice 1 prints `get a TYPESAFE_API_KEY: <help>` (override via `JEV_KEY_HELP_URL`) BEFORE `TYPESAFE_API_KEY (input hidden; Enter skips):`, writes `~/.env` + per-harness `.env` + bundle `.env` (chmod 600), installs, runs doctor, prints `doctor: VERDICT (details)`. Choice 4 exits; the frozen exe then pauses `Press Enter to exit...` on TTY.

## What to assert

- Hooks: `~/.claude/settings.json` hook `command` and `~/.codex/hooks.json`/`~/.grok/hooks/*.json` must point at `~/.jev-consult/jev-runtime.exe` (JEV_HOOK_PYTHON), never the downloaded exe or a `_MEI*` temp path.
- `<exe-or-runtime> <script>.py [args]` replays payload scripts in-process — e.g. dispatch `bundle/skills/jev-consult/scripts/doctor.py --agents claude-code --home $HOME` and expect JSON.
- doctor: absent harness home dir => one `presence` check `{ok:true, skipped:true}` + top-level `absent` list; a present-but-unconfigured harness still FAILs real checks (e.g. `mkdir ~/.claude` empty => skill/hooks FAIL).
- Fresh-HOME quirk: installing `hermes` creates `~/.hermes/{skills,plugins}` so doctor treats it as present, but `plugin_enabled` FAILs without `~/.hermes/config.yaml` => expect `doctor: FAIL (harnesses failing: hermes; N harnesses ok)` on a clean home.
- Bundle re-staging wipes `~/.jev-consult/bundle` each launch — `bundle/.env` and bundle `AGENTS.md/CLAUDE.md/.hermes.md` are ephemeral; the durable key is `~/.env`.
- Never grep-print key values; use `grep -l`/`grep -c TYPESAFE_API_KEY` (names/counts only).

## Devin Secrets Needed

None — test with a fake key like `sekret-AAAA-BBBB-CCCC-DDDD`; nothing in the install path requires a valid key.

## Untestable on Linux

`.cmd` launcher, `pause`/`GetConsoleMode` console gating, icacls ACL lockdown, and the real Windows `.exe` (release-exe.yml builds it on windows-latest).
