#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Entry point for jev-setup-windows-amd64.exe (PyInstaller --onefile).

Frozen layout: sys._MEIPASS/payload mirrors the .pyz payload
(skills/jev-consult + scripts/install.py). Staging copies it to the
stable ~/.jev-consult/bundle dir — hook commands and .jev-consult-source
markers must never reference the _MEIPASS temp dir, which PyInstaller
deletes when the process exits.

The exe doubles as a script runner so installed hooks keep working on a
Python-free box: `<exe> some_script.py [args]` runs the script in-process
(runpy). install.py hooks call it through the stable copy staged at
~/.jev-consult/jev-runtime.exe (JEV_HOOK_PYTHON), not the downloaded
exe the user may delete.
"""
from __future__ import annotations

import os
import runpy
import shutil
import stat
import sys
import tempfile
from pathlib import Path

BUNDLE_DIR = Path.home() / ".jev-consult" / "bundle"
RUNTIME_EXE = Path.home() / ".jev-consult" / "jev-runtime.exe"


def _payload_dir() -> Path:
    """Embedded payload root: _MEIPASS/payload frozen, repo root unfrozen."""
    mei = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and mei:
        return Path(mei) / "payload"
    return Path(__file__).resolve().parent.parent


def _rmtree_fix(func, path, _exc) -> None:
    """rmtree handler: clear the read-only bit, retry (Windows)."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_rmtree_fix)
    else:
        shutil.rmtree(path, onerror=_rmtree_fix)


def _console_handle(stream) -> bool:
    """True only for a real Windows console handle — isatty() reports
    character devices like NUL as ttys, so GetConsoleMode is the check."""
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        mode = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (ImportError, OSError, ValueError):
        return False


def _tty() -> bool:
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (OSError, ValueError):
        return False
    if os.name == "nt":
        return _console_handle(sys.stdin) and _console_handle(sys.stdout)
    return True


def _copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))


def _stage_payload() -> Path:
    """Refresh BUNDLE_DIR with the embedded payload and return it."""
    src = _payload_dir()
    BUNDLE_DIR.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="bundle-", dir=str(BUNDLE_DIR.parent)))
    try:
        _copytree(src, stage / "staged")
        # A prior install hardened bundle/.env to a sole owner ACE — move
        # the file itself (same volume) so the restage keeps both the key
        # and its ACL instead of deleting it.
        env_file = BUNDLE_DIR / ".env"
        saved = None
        if env_file.is_file():
            saved = stage / ".env.preserved"
            shutil.move(str(env_file), str(saved))
        if BUNDLE_DIR.is_symlink() or (BUNDLE_DIR.exists() and not BUNDLE_DIR.is_dir()):
            BUNDLE_DIR.unlink()
        elif BUNDLE_DIR.is_dir():
            _rmtree(BUNDLE_DIR)
        shutil.move(str(stage / "staged"), str(BUNDLE_DIR))
        if saved is not None:
            env_file.unlink(missing_ok=True)
            shutil.move(str(saved), str(env_file))
        return BUNDLE_DIR
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _stage_runtime() -> Path | None:
    """Copy the running exe next to the bundle so hooks can replay scripts
    after the downloaded exe is gone. Unfrozen: nothing to stage."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        if Path(sys.executable).resolve() == RUNTIME_EXE.resolve():
            return RUNTIME_EXE
        shutil.copy2(sys.executable, RUNTIME_EXE)
        return RUNTIME_EXE
    except OSError:
        return None


def _run_script(script: Path, argv: list[str]) -> int:
    """Run a payload .py in-process (hook dispatch and install.py itself).

    A plain runpy does not put the script's dir on sys.path, so sibling
    imports would fail; add it for the duration of the run.
    """
    sys.argv = [str(script)] + argv
    sys.path.insert(0, str(script.parent))
    try:
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as exc:
            code = exc.code
            if isinstance(code, str) and code:
                # SystemExit("<msg>") would otherwise exit silently — the
                # string is the error message (e.g. a bad --agents name).
                sys.stderr.write("%s\n" % code)
            return code if isinstance(code, int) else (0 if code in (None, "") else 1)
        return 0
    finally:
        try:
            sys.path.remove(str(script.parent))
        except ValueError:
            pass


def _pause() -> None:
    if os.name == "nt":
        os.system("pause")
    else:
        input("Press Enter to exit...")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].endswith(".py"):
        # Hook/script dispatch: non-interactive, never stages, never pauses.
        return _run_script(Path(argv[0]), argv[1:])

    try:
        payload = _stage_payload()
    except OSError as exc:
        sys.stderr.write(
            "jev-setup: could not stage %s (%s)\n" % (BUNDLE_DIR, exc)
        )
        return 2
    runtime = _stage_runtime()
    if runtime is not None:
        os.environ["JEV_HOOK_PYTHON"] = str(runtime)
    args = argv or (["--setup"] if _tty() else [])
    rc = _run_script(
        payload / "scripts" / "install.py",
        ["--source", str(payload)] + args,
    )
    # A double-clicked console window closes on exit; keep it open so the
    # user sees the menu result and the doctor verdict. Piped runs pass
    # straight through.
    if _tty():
        _pause()
    return rc


if __name__ == "__main__":
    sys.exit(main())
