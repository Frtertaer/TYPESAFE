#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Windows-facing install paths, structured OS-agnostically.

os.name is patched to "nt" where feasible (icacls via mocked subprocess);
shutil.rmtree retry is simulated with a chmod-gated unlink. A real Windows
pass of install.cmd - interactive and `< nul` redirected - has been run on a
live Windows box and produced the NUL-isatty / probe-stdout regressions
encoded below; CI on windows-latest covers the Python parts.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "scripts" / "install.py"
PACKAGE_PATH = ROOT / "scripts" / "package_release.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


EXE_ENTRY_PATH = ROOT / "scripts" / "exe_entry.py"

install = load(INSTALL_PATH, "install_jev_windows")
package_release = load(PACKAGE_PATH, "package_release_windows")
exe_entry = load(EXE_ENTRY_PATH, "exe_entry_windows")


class WindowsAclTests(unittest.TestCase):
    """write_api_key uses an owner-only icacls ACL on nt, chmod 600 elsewhere."""

    def test_nt_calls_icacls_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            proc = subprocess.CompletedProcess([], 0)
            with patch.object(install.os, "name", "nt"), patch.object(
                install.subprocess, "run", return_value=proc
            ) as run, patch.object(install.os, "chmod") as chmod, patch.dict(
                os.environ, {"USERNAME": "winuser"}
            ):
                install.write_api_key(path, "sekret")
            self.assertEqual(chmod.call_count, 0)  # mode bits no-op on nt
            args = run.call_args[0][0]
            self.assertEqual(args[0], "icacls")
            self.assertIn(str(path), args)
            self.assertIn("/inheritance:r", args)
            self.assertIn("/grant:r", args)
            self.assertIn("winuser:F", args)
            # the .env content itself is platform-agnostic
            self.assertTrue(install.env_file_has_key(path))

    def test_nt_icacls_failure_warns_not_crashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with patch.object(install.os, "name", "nt"), patch.object(
                install.subprocess, "run", side_effect=OSError("no icacls")
            ), patch.dict(os.environ, {"USERNAME": "u"}):
                err = io.StringIO()
                with redirect_stderr(err):
                    out = install.write_api_key(path, "sekret")
            self.assertIn("wrote", out)
            self.assertIn("warning", err.getvalue())
            self.assertTrue(install.env_file_has_key(path))

    def test_nt_icacls_nonzero_exit_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            proc = subprocess.CompletedProcess([], 5, stderr=b"denied")
            with patch.object(install.os, "name", "nt"), patch.object(
                install.subprocess, "run", return_value=proc
            ), patch.dict(os.environ, {"USERNAME": "u"}):
                err = io.StringIO()
                with redirect_stderr(err):
                    install.write_api_key(path, "sekret")
            self.assertIn("warning", err.getvalue())
            self.assertTrue(install.env_file_has_key(path))

    def test_nt_username_env_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            proc = subprocess.CompletedProcess([], 0)
            env = dict(os.environ)
            env.pop("USERNAME", None)
            with patch.object(install.os, "name", "nt"), patch.object(
                install.subprocess, "run", return_value=proc
            ) as run, patch.dict(os.environ, env, clear=True), patch.object(
                install.getpass, "getuser", return_value="fallback"
            ):
                install.write_api_key(path, "sekret")
            self.assertIn("fallback:F", run.call_args[0][0])

    def test_posix_chmod_no_icacls(self) -> None:
        if os.name == "nt":
            self.skipTest("posix branch")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with patch.object(install.subprocess, "run") as run:
                install.write_api_key(path, "sekret")
            self.assertEqual(run.call_count, 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class InstallCmdTests(unittest.TestCase):
    """install.cmd launcher discovery: text-level checks (no cmd.exe here)."""

    def setUp(self) -> None:
        self.text = (ROOT / "install.cmd").read_text(encoding="utf-8")

    def test_probe_order_py_then_python_then_python3(self) -> None:
        py = self.text.index('call :try "py -3"')
        python = self.text.index('call :try "python"')
        python3 = self.text.index('call :try "python3"')
        self.assertLess(py, python)
        self.assertLess(python, python3)

    def test_probe_runs_interpreter_not_just_which(self) -> None:
        # `py` with no Python 3 installed exits with a launcher error and
        # prints nothing on stdout - the probe must actually run the
        # interpreter and demand a real version report.
        self.assertIn("sys.version_info[0]", self.text)
        self.assertIn('=="3"', self.text)
        self.assertIn("2^>nul", self.text)

    def test_no_python_message_and_nonzero_exit(self) -> None:
        self.assertIn("python.org", self.text)
        self.assertIn("Add python.exe to PATH", self.text)
        self.assertIn("exit /b 1", self.text)

    def test_tty_guard_still_selects_setup(self) -> None:
        self.assertIn("IsInputRedirected", self.text)
        self.assertIn("IsOutputRedirected", self.text)
        self.assertIn("--setup", self.text)
        self.assertIn("scripts\\install.py %*", self.text)

    def test_gate_probe_does_not_redirect_own_stdout(self) -> None:
        """`>nul` on the probe makes IsOutputRedirected always true inside
        powershell - the gate would then report 'redirected' on a real
        console. Only stderr may be silenced."""
        for text in (self.text, package_release.WINDOWS_LAUNCHER):
            line = next(l for l in text.splitlines() if "IsInputRedirected" in l)
            self.assertIn("2>nul", line)
            self.assertNotIn(">nul", line.replace("2>nul", ""))


class TtyDetectionTests(unittest.TestCase):
    """isatty() reports NUL/char devices as ttys on Windows; _tty() must
    additionally demand a real console handle there."""

    def _streams(self, stdin_tty=True, stdout_tty=True):
        stdin = MagicMock()
        stdout = MagicMock()
        stdin.isatty.return_value = stdin_tty
        stdout.isatty.return_value = stdout_tty
        return stdin, stdout

    def test_nt_rejects_char_device_stdin(self) -> None:
        """`< nul` on a console: isatty()=True but no console mode."""
        stdin, stdout = self._streams()
        with patch.object(install.os, "name", "nt"), patch.object(
            install.sys, "stdin", stdin
        ), patch.object(install.sys, "stdout", stdout), patch.object(
            install, "_console_handle", return_value=False
        ):
            self.assertFalse(install._tty())

    def test_nt_real_console(self) -> None:
        stdin, stdout = self._streams()
        with patch.object(install.os, "name", "nt"), patch.object(
            install.sys, "stdin", stdin
        ), patch.object(install.sys, "stdout", stdout), patch.object(
            install, "_console_handle", return_value=True
        ):
            self.assertTrue(install._tty())

    def test_posix_skips_console_check(self) -> None:
        stdin, stdout = self._streams()
        with patch.object(install.os, "name", "posix"), patch.object(
            install.sys, "stdin", stdin
        ), patch.object(install.sys, "stdout", stdout), patch.object(
            install, "_console_handle"
        ) as ch:
            self.assertTrue(install._tty())
            ch.assert_not_called()

    def test_piped_stdin_still_plain(self) -> None:
        stdin, stdout = self._streams(stdin_tty=False)
        with patch.object(install.sys, "stdin", stdin), patch.object(
            install.sys, "stdout", stdout
        ):
            self.assertFalse(install._tty())

    def test_bootstrap_tty_uses_console_handle(self) -> None:
        """The pyz's embedded _tty() must carry the same NUL fix."""
        self.assertIn("_console_handle", package_release.BOOTSTRAP)
        self.assertIn("GetConsoleMode", package_release.BOOTSTRAP)


class RemovePathTests(unittest.TestCase):
    def test_rmtree_fix_retries_after_chmod(self) -> None:
        """Simulated Windows unlink: refuses until the write bit is set."""
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "ro.txt"
            f.write_text("x", encoding="utf-8")
            os.chmod(str(f), 0o444)
            calls = []

            def win_unlink(p):
                if not os.stat(p).st_mode & stat.S_IWRITE:
                    raise PermissionError("read-only")
                calls.append(p)
                os.unlink(p)

            with self.assertRaises(PermissionError):
                win_unlink(str(f))
            install._rmtree_fix(win_unlink, str(f), PermissionError("ro"))
            self.assertFalse(f.exists())
            self.assertEqual(calls, [str(f)])

    def test_remove_path_passes_retry_handler(self) -> None:
        import shutil

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "d"
            dest.mkdir()
            seen = []
            real = shutil.rmtree

            def spy(path, **kw):
                seen.append(kw)
                return real(path, **kw)

            with patch.object(install.shutil, "rmtree", spy):
                install._remove_path(dest)
            self.assertFalse(dest.exists())
            handler = seen[0].get("onexc") or seen[0].get("onerror")
            self.assertTrue(callable(handler))

    def test_remove_readonly_tree_and_file(self) -> None:
        """Read-only files remove cleanly on any OS; on Windows this
        exercises the chmod-retry handler for real."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "dest"
            sub = dest / "sub"
            sub.mkdir(parents=True)
            ro = sub / "ro.txt"
            ro.write_text("x", encoding="utf-8")
            os.chmod(str(ro), 0o444)
            install._remove_path(dest)
            self.assertFalse(dest.exists())
            f = Path(tmp) / "one.txt"
            f.write_text("x", encoding="utf-8")
            os.chmod(str(f), 0o444)
            install._remove_path(f)
            self.assertFalse(f.exists())

    def test_unlink_retries_after_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "ro.txt"
            f.write_text("x", encoding="utf-8")
            os.chmod(str(f), 0o444)
            orig = Path.unlink

            def win_unlink(self, *a, **k):
                if not os.stat(str(self)).st_mode & stat.S_IWRITE:
                    raise PermissionError("read-only")
                return orig(self, *a, **k)

            with patch.object(install.Path, "unlink", win_unlink):
                install._remove_path(f)
            self.assertFalse(f.exists())


class StableBundleTests(unittest.TestCase):
    """The pyz stages its payload at ~/.jev-consult/bundle so recorded paths
    (markers, repo instructions, bundle .env) outlive the installer."""

    def _build(self, tmp: str) -> Path:
        out = Path(tmp) / "jev-setup.pyz"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(package_release.main(["--out", str(out)]), 0)
        return out

    def _env(self, tmp: str) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(Path(tmp) / "home"),
                "USERPROFILE": str(Path(tmp) / "home"),
                "HERMES_HOME": str(Path(tmp) / "hermes"),
                "TYPESAFE_API_KEY": env.get("TYPESAFE_API_KEY") or "x",
            }
        )
        return env

    def test_pyz_install_records_stable_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._build(tmp)
            home = Path(tmp) / "home"
            proc = subprocess.run(
                [sys.executable, str(out), "--agents", "claude-code"],
                capture_output=True,
                text=True,
                env=self._env(tmp),
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:500])
            bundle = home / ".jev-consult" / "bundle"
            self.assertTrue((bundle / "skills" / "jev-consult" / "SKILL.md").is_file())
            skill = home / ".claude" / "skills" / "jev-consult"
            marker = (skill / ".jev-consult-source").read_text(encoding="utf-8").strip()
            self.assertEqual(
                marker.replace("\\", "/"),
                str((bundle / "skills" / "jev-consult").resolve()).replace("\\", "/"),
            )
            # no staging leftovers and nothing recorded points at a temp dir
            self.assertEqual(list((home / ".jev-consult").glob("bundle-*")), [])
            settings = json.loads(
                (home / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            home_s = str(home).replace("\\", "/")
            for event_entries in settings["hooks"].values():
                for entry in event_entries:
                    for hook in entry["hooks"]:
                        blob = json.dumps(hook)
                        self.assertIn("jev-consult", blob)
                        self.assertNotIn("jev-setup-", blob)  # temp prefix
                        self.assertNotIn("bundle-", blob)  # staging prefix
                        for arg in hook.get("args") or []:
                            self.assertTrue(
                                arg.replace("\\", "/").startswith(home_s),
                                "hook script not under home: %s" % arg,
                            )

    def test_pyz_no_args_non_tty_plain_installs(self) -> None:
        """Mirrors install.cmd: redirected stdin/stdout => no --setup."""
        with tempfile.TemporaryDirectory() as tmp:
            out = self._build(tmp)
            home = Path(tmp) / "home"
            proc = subprocess.run(
                [sys.executable, str(out)],
                capture_output=True,
                text=True,
                env=self._env(tmp),
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:500])
            self.assertNotIn("needs a TTY", proc.stderr)
            self.assertTrue(
                (home / ".claude" / "skills" / "jev-consult" / "SKILL.md").is_file()
            )

    def test_pyz_env_source_is_stable_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._build(tmp)
            proc = subprocess.run(
                [sys.executable, str(out), "--env", "--jq", "source"],
                capture_output=True,
                text=True,
                env=self._env(tmp),
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:500])
            source = json.loads(proc.stdout.strip())
            expected = Path(tmp) / "home" / ".jev-consult" / "bundle"
            self.assertEqual(
                source.replace("\\", "/"), str(expected.resolve()).replace("\\", "/")
            )

    def test_windows_launcher_emitted_next_to_pyz(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = self._build(tmp)
            cmd = Path(tmp) / "jev-setup.cmd"
            self.assertTrue(cmd.is_file())
            raw = cmd.read_bytes()
            self.assertIn(b"\r\n", raw)  # batch files need CRLF
            self.assertNotIn(b"\r\r\n", raw)  # no double translation
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))  # no bare LF
            text = raw.decode("utf-8")
            self.assertIn('%~dp0jev-setup.pyz', text)
            py = text.index('call :try "py -3"')
            python = text.index('call :try "python"')
            python3 = text.index('call :try "python3"')
            self.assertLess(py, python)
            self.assertLess(python, python3)
            self.assertIn("python.org", text)
            self.assertIn("exit /b", text)
            # exit code survives the trailing pause; pause is TTY-gated so
            # `jev-setup.cmd < nul` is unaffected
            self.assertIn('set "RC=%ERRORLEVEL%"', text)
            self.assertIn("exit /b %RC%", text)
            self.assertLess(text.index("set \"RC=%ERRORLEVEL%\""), text.index("pause"))
            self.assertIn("IsInputRedirected", text)

    def test_launcher_follows_pyz_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "custom.pyz"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(package_release.main(["--out", str(out)]), 0)
            cmd = Path(tmp) / "custom.cmd"
            self.assertTrue(cmd.is_file())
            self.assertIn("%~dp0custom.pyz", cmd.read_text(encoding="utf-8"))


class ExeEntryTests(unittest.TestCase):
    """scripts/exe_entry.py — the PyInstaller entry point — exercised as an
    importable module (the real binary needs a Windows host; a frozen
    Linux onefile build verified the same paths end-to-end)."""

    def test_payload_dir_unfrozen_is_repo_root(self) -> None:
        self.assertEqual(exe_entry._payload_dir(), ROOT)

    def test_payload_dir_frozen_is_meipass(self) -> None:
        with patch.object(exe_entry.sys, "frozen", True, create=True), patch.object(
            exe_entry.sys, "_MEIPASS", "/x/_MEIabc", create=True
        ):
            self.assertEqual(
                exe_entry._payload_dir(), Path("/x/_MEIabc") / "payload"
            )

    def test_dispatch_runs_script_never_stages(self) -> None:
        """`<exe> script.py args` replays the script in-process — the hook
        path on Python-free boxes. No staging, no pause."""
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "probe.py"
            script.write_text(
                "import sys;\nprint('ARGS=' + ','.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            with patch.object(
                exe_entry, "_stage_payload", side_effect=AssertionError
            ), patch.object(exe_entry, "_pause", side_effect=AssertionError), patch.object(
                exe_entry, "_stage_runtime", side_effect=AssertionError
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = exe_entry.main([str(script), "a", "b"])
            self.assertEqual(rc, 0)
            self.assertIn("ARGS=a,b", buf.getvalue())

    def test_run_script_returns_system_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "die.py"
            script.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
            self.assertEqual(exe_entry.main([str(script)]), 3)

    def test_no_args_tty_runs_setup_then_pauses(self) -> None:
        """Double-click path: stage -> install --source <bundle> --setup -> pause."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "bundle"
            ran, paused = [], []
            with patch.object(exe_entry, "_tty", return_value=True), patch.object(
                exe_entry, "_stage_payload", return_value=payload
            ), patch.object(exe_entry, "_stage_runtime", return_value=None), patch.object(
                exe_entry, "_pause", lambda: paused.append(True)
            ), patch.object(
                exe_entry, "_run_script", lambda s, a: ran.append((s, a)) or 0
            ):
                rc = exe_entry.main([])
            self.assertEqual(rc, 0)
            script, args = ran[0]
            self.assertEqual(script, payload / "scripts" / "install.py")
            self.assertEqual(args[0:2], ["--source", str(payload)])
            self.assertIn("--setup", args)
            self.assertEqual(paused, [True])

    def test_no_args_non_tty_no_setup_no_pause(self) -> None:
        """Redirected run: plain install args, returns without pausing."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "bundle"
            ran = []
            with patch.object(exe_entry, "_tty", return_value=False), patch.object(
                exe_entry, "_stage_payload", return_value=payload
            ), patch.object(exe_entry, "_stage_runtime", return_value=None), patch.object(
                exe_entry, "_pause", side_effect=AssertionError
            ), patch.object(
                exe_entry, "_run_script", lambda s, a: ran.append((s, a)) or 0
            ):
                rc = exe_entry.main([])
            self.assertEqual(rc, 0)
            self.assertNotIn("--setup", ran[0][1])

    def test_main_exports_hook_python_from_staged_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "bundle"
            runtime = Path(tmp) / "jev-runtime.exe"
            with patch.object(exe_entry, "_tty", return_value=False), patch.object(
                exe_entry, "_stage_payload", return_value=payload
            ), patch.object(
                exe_entry, "_stage_runtime", return_value=runtime
            ), patch.object(exe_entry, "_run_script", return_value=0), patch.dict(
                os.environ, {}, clear=False
            ):
                os.environ.pop("JEV_HOOK_PYTHON", None)
                exe_entry.main(["--check-key"])
                self.assertEqual(os.environ.get("JEV_HOOK_PYTHON"), str(runtime))

    def test_stage_payload_replaces_stale_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / ".jev-consult" / "bundle"
            src = Path(tmp) / "payload-src"
            (src / "skills" / "jev-consult").mkdir(parents=True)
            (src / "skills" / "jev-consult" / "SKILL.md").write_text(
                "x", encoding="utf-8"
            )
            bundle.mkdir(parents=True)
            (bundle / "stale.txt").write_text("old", encoding="utf-8")
            with patch.object(exe_entry, "BUNDLE_DIR", bundle), patch.object(
                exe_entry, "_payload_dir", return_value=src
            ):
                out = exe_entry._stage_payload()
            self.assertEqual(out, bundle)
            self.assertTrue((bundle / "skills" / "jev-consult" / "SKILL.md").is_file())
            self.assertFalse((bundle / "stale.txt").exists())
            # no bundle-* staging leftovers next to it
            self.assertEqual(
                list((Path(tmp) / ".jev-consult").glob("bundle-*")), []
            )

    def test_stage_runtime_unfrozen_is_noop(self) -> None:
        self.assertIsNone(exe_entry._stage_runtime())

    def test_stage_runtime_frozen_copies_exe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_exe = Path(tmp) / "jev-setup-windows-amd64.exe"
            fake_exe.write_bytes(b"MZ-fake")
            runtime = Path(tmp) / "jev-consult" / "jev-runtime.exe"
            runtime.parent.mkdir()
            with patch.object(exe_entry.sys, "frozen", True, create=True), patch.object(
                exe_entry.sys, "executable", str(fake_exe)
            ), patch.object(exe_entry, "RUNTIME_EXE", runtime):
                out = exe_entry._stage_runtime()
            self.assertEqual(out, runtime)
            self.assertEqual(runtime.read_bytes(), b"MZ-fake")


if __name__ == "__main__":
    unittest.main(verbosity=2)
