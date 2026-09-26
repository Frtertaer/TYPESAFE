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

--exe additionally stages the PyInstaller build tree for
jev-setup-windows-amd64.exe under dist/exe/ (entry point, payload, .spec,
build-exe.cmd) so Windows users need no Python at all. PyInstaller only
produces a Windows exe on a Windows host: on Windows with PyInstaller
installed this runs it directly; anywhere else the staged build-exe.cmd
(or .github/workflows/release-exe.yml) finishes the job.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "jev-consult"
INSTALLER = ROOT / "scripts" / "install.py"
EXE_ENTRY = ROOT / "scripts" / "exe_entry.py"
DEFAULT_OUT = ROOT / "dist" / "jev-setup.pyz"
EXE_NAME = "jev-setup-windows-amd64"
EXE_BUILD_DIR = ROOT / "dist" / "exe"
PYINSTALLER_PIN = "pyinstaller==6.21.0"

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
import time
import zipfile
from pathlib import Path

BUNDLE_DIR = Path.home() / ".jev-consult" / "bundle"
LOCK_DIR = BUNDLE_DIR.parent / "bundle.lock"


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
    try:
        func(path)
    except OSError:
        # transient lock (AV/indexer) — settle briefly, retry once
        time.sleep(0.05)
        func(path)


def _rmtree(path: Path) -> None:
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_rmtree_fix)
    else:
        shutil.rmtree(path, onerror=_rmtree_fix)


def _console_handle(stream) -> bool:
    """True only for a real Windows console handle.

    isatty() reports character devices like NUL as ttys on Windows, so
    `jev-setup.cmd < nul` would otherwise look interactive; GetConsoleMode
    succeeds only on console input/output handles.
    """
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


def _locked(fn):
    """Serialize the BUNDLE_DIR swap: two jev-setup processes racing
    _stage() could move over a payload the other one just deleted. The
    mkdir lock is atomic on Windows and POSIX; a lock older than two
    minutes is stale (holder died mid-stage) and gets broken."""
    deadline = time.monotonic() + 30
    while True:
        try:
            os.mkdir(LOCK_DIR)
            break
        except FileExistsError:
            try:
                stale = time.time() - LOCK_DIR.stat().st_mtime > 120
            except OSError:
                stale = True
            if stale:
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
                continue
            if time.monotonic() > deadline:
                raise TimeoutError("jev-setup: bundle lock held by another setup process")
            time.sleep(0.1)
    try:
        return fn()
    finally:
        shutil.rmtree(LOCK_DIR, ignore_errors=True)


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
        payload = _locked(_stage)
    except (OSError, TimeoutError) as exc:
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

if not exist "%~dp0@PYZ@" (
    >&2 echo jev-setup.cmd: @PYZ@ not found in %~dp0.
    >&2 echo Keep this .cmd and @PYZ@ in the same folder - or re-download the release.
    exit /b 1
)

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
set "RC=%ERRORLEVEL%"

rem A double-clicked console window closes on exit - keep it open so the
rem user sees the menu result and the doctor verdict. Interactive consoles
rem only; `jev-setup.cmd < nul` passes through untouched.
rem The probe must see this script's own handles: redirecting its stdout
rem would make IsOutputRedirected always true, so only stderr is nulled.
powershell -NoProfile -Command "exit ([int]([Console]::IsInputRedirected -or [Console]::IsOutputRedirected))" 2>nul
if not errorlevel 1 pause
exit /b %RC%

:try
rem Run the interpreter: `py` present with no Python 3 exits with a
rem launcher error and prints nothing - it falls through like a miss.
set "CAND=%~1"
set "MAJOR="
for /f "delims=" %%v in ('%CAND% -c "import sys; print(sys.version_info[0])" 2^>nul') do set "MAJOR=%%v"
if "%MAJOR%"=="3" set "PY=%CAND%"
exit /b 0
'''


def _stage_payload(payload: Path) -> None:
    """payload/skills/jev-consult + payload/scripts/install.py — the same
    tree the pyz embeds and the exe unpacks from _MEIPASS."""
    (payload / "skills").mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SKILL,
        payload / "skills" / "jev-consult",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    (payload / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(INSTALLER, payload / "scripts" / "install.py")


def build(out: Path) -> Path:
    """Stage payload/ + __main__.py in a temp dir and zipapp it."""
    with tempfile.TemporaryDirectory(prefix="jev-setup-build-") as tmp:
        app = Path(tmp) / "app"
        payload = app / "payload"
        _stage_payload(payload)
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
    # bytes: write_text() would translate our explicit \r\n into \r\r\n
    cmd_path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    return cmd_path


SPEC = '''# -*- mode: python ; coding: utf-8 -*-
# Generated by scripts/package_release.py --exe; rebuild with build-exe.cmd.

a = Analysis(
    [r"@ENTRY@"],
    pathex=[],
    binaries=[],
    datas=[(r"@PAYLOAD@", "payload")],
    hiddenimports=@HIDDEN@,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="@NAME@",
    console=True,
    upx=False,
)
'''

BUILD_EXE_CMD = '''@echo off
rem Build @NAME@.exe on a Windows host (PyInstaller cannot cross-compile).
rem Python 3 + pip are needed only to build; the exe itself is self-contained.
py -3 -m pip install --disable-pip-version-check "@PIN@" || python -m pip install --disable-pip-version-check "@PIN@" || exit /b 1
py -3 -m PyInstaller --clean --noconfirm --distpath .. --workpath build jev-setup.spec || python -m PyInstaller --clean --noconfirm --distpath .. --workpath build jev-setup.spec || exit /b 1
echo wrote ..\@NAME@.exe
'''


def _stdlib_imports(payload: Path, entry: Path) -> list[str]:
    """Stdlib modules the payload scripts import. Payload .py files are
    PyInstaller datas, not analyzed code, so their stdlib deps (getpass,
    sqlite3, urllib.request, msvcrt...) must be named as hiddenimports;
    PyInstaller then traces those modules' own deps itself."""
    names = set()
    files = list(payload.rglob("*.py")) + [entry]
    for py in files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
                for alias in node.names:
                    names.add("%s.%s" % (node.module, alias.name))
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    return sorted(
        n for n in names if n.split(".")[0] in stdlib and n != "__main__"
    )


def build_exe(build_dir: Path = EXE_BUILD_DIR) -> Path | None:
    """Stage the PyInstaller build tree and, on a Windows host with
    PyInstaller installed, run it. Returns the exe path or None."""
    payload = build_dir / "payload"
    if build_dir.is_dir():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    _stage_payload(payload)
    entry = build_dir / "jev_setup_entry.py"
    shutil.copy2(EXE_ENTRY, entry)
    spec = build_dir / "jev-setup.spec"
    spec.write_text(
        SPEC.replace("@ENTRY@", str(entry).replace("\\", "/"))
        .replace("@PAYLOAD@", str(payload).replace("\\", "/"))
        .replace("@NAME@", EXE_NAME)
        .replace("@HIDDEN@", repr(_stdlib_imports(payload, entry))),
        encoding="utf-8",
    )
    (build_dir / "build-exe.cmd").write_bytes(
        BUILD_EXE_CMD.replace("@NAME@", EXE_NAME)
        .replace("@PIN@", PYINSTALLER_PIN)
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    if os.name != "nt" or importlib.util.find_spec("PyInstaller") is None:
        return None
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            "--distpath",
            str(build_dir.parent),
            "--workpath",
            str(build_dir / "build"),
            str(spec),
        ]
    )
    exe = build_dir.parent / (EXE_NAME + ".exe")
    return exe if proc.returncode == 0 and exe.is_file() else None


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
    parser.add_argument(
        "--exe",
        action="store_true",
        help="Also stage the PyInstaller build for %s.exe under dist/exe "
        "(runs PyInstaller itself on a Windows host with PyInstaller "
        "installed; otherwise emits jev-setup.spec + build-exe.cmd)." % EXE_NAME,
    )
    args = parser.parse_args(argv)
    for need in (SKILL / "SKILL.md", INSTALLER):
        if not need.is_file():
            sys.stderr.write("package_release: missing %s\n" % need)
            return 2
    out = build(Path(args.out))
    sys.stdout.write("wrote %s\n" % out)
    sys.stdout.write("wrote %s\n" % out.with_suffix(".cmd"))
    if args.exe:
        if not EXE_ENTRY.is_file():
            sys.stderr.write("package_release: missing %s\n" % EXE_ENTRY)
            return 2
        exe_dir = Path(args.out).parent / "exe"
        exe = build_exe(exe_dir)
        if exe is not None:
            sys.stdout.write("wrote %s\n" % exe)
        else:
            sys.stdout.write(
                "staged %s (Windows exe needs a Windows host: run "
                "build-exe.cmd there, or .github/workflows/release-exe.yml)\n"
                % exe_dir
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
