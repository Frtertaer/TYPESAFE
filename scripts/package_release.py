#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Build dist/jev-setup.pyz — a one-file jev-consult installer (zipapp).

The .pyz carries payload/skills/jev-consult (including scripts/doctor.py) and
payload/scripts/install.py plus a __main__.py that copies the payload to the
stable ~/.jev-consult/bundle dir and runs install.py --source <bundle>, so
.jev-consult-source markers and hook commands never point at a temp
directory that dies when the installer exits. A jev-setup.cmd double-
clickable Windows launcher is emitted next to the .pyz. No git clone needed:

    python jev-setup.pyz            # interactive setup menu in a TTY
    python jev-setup.pyz --check-key
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "jev-consult"
INSTALLER = ROOT / "scripts" / "install.py"
DEFAULT_OUT = ROOT / "dist" / "jev-setup.pyz"

BOOTSTRAP = '''#!/usr/bin/env python
"""jev-setup bootstrap: unpack the bundled payload, run install.py.

The payload is staged at ~/.jev-consult/bundle (not a temp dir): hook
commands and .jev-consult-source markers record absolute payload paths,
and a %TEMP%/jev-setup-* style extraction dir dies on exit.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

BUNDLE_DIR = Path.home() / ".jev-consult" / "bundle"


def _extract(dest_dir: Path) -> Path:
    with zipfile.ZipFile(sys.argv[0]) as zf:
        for name in zf.namelist():
            if name.startswith("payload/"):
                zf.extract(name, dest_dir)
    return dest_dir / "payload"


def _rmtree_fix(func, path, _exc):
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


def _tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (OSError, ValueError):
        return False


def _stage() -> Path:
    """Refresh BUNDLE_DIR with this pyz's payload and return it."""
    BUNDLE_DIR.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="bundle-", dir=str(BUNDLE_DIR.parent)))
    try:
        payload = _extract(stage)
        if BUNDLE_DIR.is_symlink() or (BUNDLE_DIR.exists() and not BUNDLE_DIR.is_dir()):
            BUNDLE_DIR.unlink()
        elif BUNDLE_DIR.is_dir():
            _rmtree(BUNDLE_DIR)
        shutil.move(str(payload), str(BUNDLE_DIR))
        return BUNDLE_DIR
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    cleanup = None
    try:
        payload = _stage()
    except OSError as exc:
        temp = Path(tempfile.mkdtemp(prefix="jev-setup-"))
        payload = _extract(temp)
        cleanup = temp
        sys.stderr.write(
            "jev-setup: warning: could not stage %s (%s); "
            "installed paths may go stale after this run\\n" % (BUNDLE_DIR, exc)
        )
    args = sys.argv[1:]
    cmd = [
        sys.executable,
        str(payload / "scripts" / "install.py"),
        "--source",
        str(payload),
    ]
    if not args and _tty():
        cmd.append("--setup")
    cmd.extend(args)
    try:
        return subprocess.call(cmd)
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
'''


WINDOWS_LAUNCHER = '''@echo off
setlocal

set "PY="
call :try "py -3"
if not defined PY call :try "python"
if not defined PY call :try "python3"
if not defined PY (
    >&2 echo jev-setup.cmd: Python 3 not found on PATH.
    >&2 echo Download Python 3 from https://www.python.org/downloads/ - in the installer tick "Add python.exe to PATH".
    exit /b 1
)

%PY% "%~dp0@PYZ@" %*
exit /b %ERRORLEVEL%

:try
rem Run the interpreter: `py` present with no Python 3 exits with a
rem launcher error and prints nothing - it falls through like a miss.
set "CAND=%~1"
set "MAJOR="
for /f "delims=" %%v in ('%CAND% -c "import sys; print(sys.version_info[0])" 2^>nul') do set "MAJOR=%%v"
if "%MAJOR%"=="3" set "PY=%CAND%"
exit /b 0
'''


def build(out: Path) -> Path:
    """Stage payload/ + __main__.py in a temp dir and zipapp it."""
    with tempfile.TemporaryDirectory(prefix="jev-setup-build-") as tmp:
        app = Path(tmp) / "app"
        payload = app / "payload"
        (payload / "skills").mkdir(parents=True)
        shutil.copytree(SKILL, payload / "skills" / "jev-consult")
        (payload / "scripts").mkdir(parents=True)
        shutil.copy2(INSTALLER, payload / "scripts" / "install.py")
        (app / "__main__.py").write_text(BOOTSTRAP, encoding="utf-8")
        out.parent.mkdir(parents=True, exist_ok=True)
        zipapp.create_archive(
            app,
            target=str(out),
            interpreter="/usr/bin/env python3",
            compressed=True,
        )
    _write_windows_launcher(out)
    return out


def _write_windows_launcher(out: Path) -> Path:
    """Drop a double-clickable <pyz>.cmd next to the .pyz on disk."""
    cmd_path = out.with_suffix(".cmd")
    text = WINDOWS_LAUNCHER.replace("@PYZ@", out.name)
    cmd_path.write_text(text.replace("\n", "\r\n"), encoding="utf-8")
    return cmd_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the single-file jev-setup.pyz installer (zipapp)."
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        metavar="PATH",
        help="Output .pyz path (default: dist/jev-setup.pyz).",
    )
    args = parser.parse_args(argv)
    for need in (SKILL / "SKILL.md", INSTALLER):
        if not need.is_file():
            sys.stderr.write("package_release: missing %s\n" % need)
            return 2
    out = build(Path(args.out))
    sys.stdout.write("wrote %s\n" % out)
    sys.stdout.write("wrote %s\n" % out.with_suffix(".cmd"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
