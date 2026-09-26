#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import annotations

import getpass
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PATH = ROOT / "scripts" / "install.py"
PACKAGE_PATH = ROOT / "scripts" / "package_release.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


install = load(INSTALL_PATH, "install_jev_setup")
package_release = load(PACKAGE_PATH, "package_release")


def make_bundle(base: Path) -> Path:
    """Minimal unpacked release bundle: skills/jev-consult + scripts/install.py."""
    bundle = base / "bundle"
    skill = bundle / "skills" / "jev-consult"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: jev-consult\ndescription: test bundle\n---\n", encoding="utf-8"
    )
    (skill / "scripts" / "doctor.py").write_text(
        "import sys; sys.exit(0)\n", encoding="utf-8"
    )
    (bundle / "scripts").mkdir(exist_ok=True)
    (bundle / "scripts" / "install.py").write_text("# stub\n", encoding="utf-8")
    return bundle


class SourceTests(unittest.TestCase):
    def tearDown(self) -> None:
        install.set_source(None)

    def test_source_dir_overrides_repo_and_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            install.set_source(str(bundle))
            self.assertEqual(install.repo_root(), bundle.resolve())
            self.assertEqual(
                install.skill_source(),
                (bundle / "skills" / "jev-consult").resolve(),
            )

    def test_source_accepts_bare_skill_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill = make_bundle(Path(tmp)) / "skills" / "jev-consult"
            install.set_source(str(skill))
            self.assertEqual(install.skill_source(), skill.resolve())

    def test_source_missing_dir_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                install.set_source(str(Path(tmp) / "nope"))

    def test_install_from_bundle_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            with patch.object(install, "user_home", return_value=home), patch.object(
                install, "hermes_home", return_value=hermes
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = install.main(
                        ["--source", str(bundle), "--agents", "claude-code"]
                    )
                self.assertEqual(rc, 0)
                skill = home / ".claude" / "skills" / "jev-consult"
                self.assertTrue((skill / "SKILL.md").is_file())
                marker = (skill / ".jev-consult-source").read_text(encoding="utf-8")
                self.assertIn(
                    str(bundle.resolve()).replace("\\", "/"),
                    marker.replace("\\", "/"),
                )
                # repo marker files were written into the bundle root
                self.assertIn(
                    "jev-consult",
                    (bundle / "AGENTS.md").read_text(encoding="utf-8"),
                )

    def test_env_report_includes_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = install.main(
                    ["--source", str(bundle), "--env", "--jq", "source"]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(
                json.loads(buf.getvalue().strip()), str(bundle.resolve())
            )


class SetupModeTests(unittest.TestCase):
    def tearDown(self) -> None:
        install.set_source(None)

    def test_setup_non_tty_refuses_cleanly(self) -> None:
        err = io.StringIO()
        with patch.object(install, "_tty", return_value=False), redirect_stderr(err):
            rc = install.main(["--setup"])
        self.assertEqual(rc, 2)
        self.assertIn("TTY", err.getvalue())

    def test_bare_no_args_non_tty_installs(self) -> None:
        """Backward compat: no args without a TTY = plain install."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            with patch.object(install, "_tty", return_value=False), patch.object(
                install, "user_home", return_value=home
            ), patch.object(install, "hermes_home", return_value=hermes):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = install.main([])
            self.assertEqual(rc, 0)
            self.assertTrue(
                (home / ".claude" / "skills" / "jev-consult" / "SKILL.md").is_file()
            )

    def test_live_alone_in_tty_opens_menu(self) -> None:
        """--live is a modifier: bare `--live` in a terminal opens the
        setup menu (with live probing on), it does not silently install
        every harness."""
        with patch.object(install, "_tty", return_value=True), patch.object(
            install, "run_setup", return_value=0
        ) as menu:
            rc = install.main(["--live"])
        self.assertEqual(rc, 0)
        menu.assert_called_once()
        self.assertTrue(menu.call_args.kwargs.get("live"))

    def test_live_alone_non_tty_installs_then_probes(self) -> None:
        """Without a TTY `--live` keeps the noninteractive path: install,
        then the live doctor probe."""
        with patch.object(install, "_tty", return_value=False), patch.object(
            install, "install", return_value=0
        ), patch.object(install, "run_doctor", return_value=0) as doc:
            rc = install.main(["--live"])
        self.assertEqual(rc, 0)
        doc.assert_called_once()
        self.assertTrue(doc.call_args.kwargs.get("live"))

    def test_setup_menu_installs_detected_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            (home / ".claude" / "skills").mkdir(parents=True)
            env = {
                "USERPROFILE": str(home),
                "HOME": str(home),
                "HERMES_HOME": str(hermes),
            }
            # agents prompt -> Enter (detected default), menu -> Install, then Exit
            answers = iter(["", "1", "4"])
            with patch.dict(os.environ, env, clear=False), patch.object(
                install, "_tty", return_value=True
            ), patch("builtins.input", lambda prompt="": next(answers)), patch.object(
                getpass, "getpass", return_value=""
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = install.main(["--setup"])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("claude-code", out)
            self.assertIn("doctor:", out)
            self.assertTrue(
                (home / ".claude" / "skills" / "jev-consult" / "SKILL.md").is_file()
            )
            self.assertFalse((home / ".grok" / "skills" / "jev-consult").exists())

    def test_detected_agents_reads_existing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex" / "skills").mkdir(parents=True)
            env = {"USERPROFILE": tmp, "HOME": tmp, "HERMES_HOME": str(home / "h")}
            with patch.dict(os.environ, env, clear=False):
                found = install.detected_agents(install.env_report(list(install.ALLOWED)))
            self.assertEqual(found, ["codex"])


class KeyWriteTests(unittest.TestCase):
    def tearDown(self) -> None:
        install.set_source(None)

    def test_write_api_key_upserts_and_preserves_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# note\nOTHER=1\nexport TYPESAFE_API_KEY=old\nTYPESAFE_API_KEY=dup\n",
                encoding="utf-8",
            )
            install.write_api_key(path, "sekret-new")
            text = path.read_text(encoding="utf-8")
            self.assertIn("# note", text)
            self.assertIn("OTHER=1", text)
            self.assertIn("TYPESAFE_API_KEY=sekret-new", text)
            self.assertNotIn("old", text)
            self.assertEqual(text.count("TYPESAFE_API_KEY="), 1)

    def test_setup_key_never_echoes_value(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            install.set_source(str(bundle))
            home = Path(tmp) / "home"
            hermes = Path(tmp) / "hermes"
            env = {
                "USERPROFILE": str(home),
                "HOME": str(home),
                "HERMES_HOME": str(hermes),
            }
            with patch.dict(os.environ, env, clear=True), patch.object(
                getpass, "getpass", return_value="sekret-12345"
            ):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    install._setup_key(["claude-code"])
            self.assertNotIn("sekret-12345", buf.getvalue())
            env_path = home / ".claude" / ".env"
            self.assertTrue(install.env_file_has_key(env_path))
            self.assertIn("sekret-12345", env_path.read_text(encoding="utf-8"))
            # bundle .env (repo_root under --source) and ~/.env got the key too
            self.assertTrue(install.env_file_has_key(bundle / ".env"))
            self.assertTrue(install.env_file_has_key(home / ".env"))

    def test_write_api_key_chmod_600(self) -> None:
        if os.name == "nt":
            self.skipTest("posix mode bits only")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            install.write_api_key(path, "sekret")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_write_api_key_rejects_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            with self.assertRaises(SystemExit):
                install.write_api_key(path, "good\nINJECTED=1")
            self.assertFalse(path.exists())

    def test_run_doctor_passes_hermes_home(self) -> None:
        import subprocess

        calls = []

        def fake_run(cmd, **_kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)

        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            install.set_source(str(bundle))
            hermes = Path(tmp) / "custom-hermes"
            with patch.object(subprocess, "run", fake_run), patch.object(
                install, "hermes_home", return_value=hermes
            ), patch.object(install, "user_home", return_value=Path(tmp)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    install.run_doctor(["hermes"])
        self.assertIn("--hermes-home", calls[0])
        self.assertIn(str(hermes), calls[0])


class BootstrapScriptTests(unittest.TestCase):
    def test_install_sh_no_python_prints_pointer(self) -> None:
        import shutil
        import subprocess

        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh on this box")
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sh, str(ROOT / "install.sh")],
                capture_output=True,
                text=True,
                env={"PATH": tmp},
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("python.org", proc.stderr)
        self.assertIn("PATH", proc.stderr)

    def test_install_cmd_carries_bootstrap_hints(self) -> None:
        text = (ROOT / "install.cmd").read_text(encoding="utf-8")
        for needle in ("py -3", "python3", "python", "python.org", "PATH"):
            self.assertIn(needle, text)

    def test_install_cmd_no_python_prints_pointer(self) -> None:
        if os.name != "nt":
            self.skipTest("install.cmd is Windows-only")
        import subprocess

        env = {"PATH": ""}
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
            if key in os.environ:
                env[key] = os.environ[key]
        with tempfile.TemporaryDirectory() as tmp:
            env["PATH"] = tmp
            proc = subprocess.run(
                ["cmd", "/c", str(ROOT / "install.cmd")],
                capture_output=True,
                text=True,
                env=env,
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("python.org", proc.stderr)

    def test_wrappers_forward_to_setup(self) -> None:
        sh_text = (ROOT / "install.sh").read_text(encoding="utf-8")
        cmd_text = (ROOT / "install.cmd").read_text(encoding="utf-8")
        self.assertIn("--setup", sh_text)
        self.assertIn("--setup", cmd_text)
        # wrappers only force --setup on a real terminal; unattended runs
        # keep the old plain-install behaviour
        self.assertIn("-t 0", sh_text)
        self.assertIn("IsInputRedirected", cmd_text)


class DoctorSummaryTests(unittest.TestCase):
    """install.run_doctor prints a verdict that separates absent harnesses
    from failing ones."""

    def test_summary_counts_ok_and_absent(self) -> None:
        data = {
            "checks": [
                {"agent": "claude-code", "check": "skill", "ok": True},
                {"agent": "claude-code", "check": "presence", "ok": True},
                {"agent": "codex", "check": "presence", "ok": True, "skipped": True},
                {"agent": "grok", "check": "presence", "ok": True, "skipped": True},
                {"agent": "*", "check": "api_key", "ok": True},
            ],
            "absent": ["codex", "grok"],
        }
        self.assertEqual(
            install._doctor_summary(data, 0),
            "PASS (1 harness ok; 2 not installed)",
        )

    def test_summary_names_failing_harnesses_and_checks(self) -> None:
        data = {
            "checks": [
                {"agent": "hermes", "check": "skill", "ok": False},
                {"agent": "*", "check": "api_key", "ok": False},
            ],
            "absent": ["grok"],
        }
        summary = install._doctor_summary(data, 1)
        self.assertTrue(summary.startswith("FAIL"))
        self.assertIn("hermes", summary)
        self.assertIn("api_key", summary)
        self.assertIn("1 not installed", summary)

    def test_summary_falls_back_on_unparseable_stdout(self) -> None:
        # e.g. a stub doctor (tests pin this shape) or a crash traceback
        self.assertEqual(install._doctor_summary(None, 0), "PASS")
        self.assertEqual(install._doctor_summary("x", 1), "FAIL")
        self.assertEqual(install._doctor_summary({"checks": "x"}, 1), "FAIL")

    def test_run_doctor_reprints_payload_and_summarizes(self) -> None:
        import subprocess

        payload = json.dumps(
            {
                "checks": [
                    {"agent": "codex", "check": "presence", "ok": True, "skipped": True}
                ],
                "absent": ["codex"],
            }
        )

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            install.set_source(str(bundle))
            try:
                with patch.object(subprocess, "run", fake_run), patch.object(
                    install, "user_home", return_value=Path(tmp)
                ):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = install.run_doctor(["codex"])
            finally:
                install.set_source(None)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn(payload, out)  # raw doctor JSON still printed
        self.assertIn("doctor: PASS (1 not installed)", out)


class KeyHelpTests(unittest.TestCase):
    def test_key_help_default_mentions_admin(self) -> None:
        env = dict(os.environ)
        env.pop("JEV_KEY_HELP_URL", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertIn("admin", install.key_help())

    def test_key_help_env_override(self) -> None:
        with patch.dict(
            os.environ, {"JEV_KEY_HELP_URL": "https://keys.example.org"}
        ):
            self.assertEqual(install.key_help(), "https://keys.example.org")

    def test_setup_key_prints_help_before_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = make_bundle(Path(tmp))
            install.set_source(str(bundle))
            home = Path(tmp) / "home"
            env = {
                "USERPROFILE": str(home),
                "HOME": str(home),
                "HERMES_HOME": str(Path(tmp) / "h"),
                "JEV_KEY_HELP_URL": "https://keys.example.org/get-one",
            }
            try:
                with patch.dict(os.environ, env, clear=True), patch.object(
                    getpass, "getpass", return_value=""
                ):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        install._setup_key(["claude-code"])
            finally:
                install.set_source(None)
        out = buf.getvalue()
        self.assertIn("get a TYPESAFE_API_KEY", out)
        self.assertIn("https://keys.example.org/get-one", out)


class HookInterpreterTests(unittest.TestCase):
    def test_default_is_running_interpreter(self) -> None:
        env = dict(os.environ)
        env.pop("JEV_HOOK_PYTHON", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(install._hook_interpreter(), sys.executable)

    def test_env_override_wins(self) -> None:
        with patch.dict(
            os.environ,
            {"JEV_HOOK_PYTHON": "C:\\jev-consult\\jev-runtime.exe"},
        ):
            self.assertEqual(
                install._hook_interpreter(), "C:\\jev-consult\\jev-runtime.exe"
            )
            cmd = install.grok_hook_command(Path("x") / "hook.py")
            self.assertIn("jev-runtime.exe", cmd)


class PackageReleaseTests(unittest.TestCase):
    def test_build_pyz_and_run_env(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "jev-setup.pyz"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = package_release.main(["--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())
            with zipfile.ZipFile(out) as zf:
                names = set(zf.namelist())
            self.assertIn("__main__.py", names)
            self.assertIn("payload/skills/jev-consult/SKILL.md", names)
            self.assertIn("payload/skills/jev-consult/scripts/doctor.py", names)
            self.assertIn("payload/scripts/install.py", names)
            env = dict(os.environ)
            env["TYPESAFE_API_KEY"] = "x"
            proc = subprocess.run(
                [sys.executable, str(out), "--env", "--jq", "key_set"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])
            self.assertEqual(json.loads(proc.stdout.strip()), True)

    def test_build_missing_out_dir_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "deep" / "jev-setup.pyz"
            with redirect_stdout(io.StringIO()):
                rc = package_release.main(["--out", str(out)])
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_file())

    def test_exe_flag_stages_pyinstaller_tree(self) -> None:
        """--exe emits the spec/payload/build-exe.cmd tree; the real binary
        needs a Windows host (PyInstaller cannot cross-compile)."""
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "jev-setup.pyz"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = package_release.main(["--out", str(out), "--exe"])
            self.assertEqual(rc, 0)
            exe_dir = Path(tmp) / "exe"
            spec = (exe_dir / "jev-setup.spec").read_text(encoding="utf-8")
            self.assertIn('name="jev-setup-windows-amd64"', spec)
            self.assertIn("console=True", spec)
            self.assertIn('"payload"', spec)  # datas under _MEIPASS/payload
            self.assertIn("hiddenimports", spec)
            entry = exe_dir / "jev_setup_entry.py"
            self.assertTrue(entry.is_file())
            self.assertIn("_MEIPASS", entry.read_text(encoding="utf-8"))
            cmd_text = (exe_dir / "build-exe.cmd").read_text(encoding="utf-8")
            self.assertIn(package_release.PYINSTALLER_PIN, cmd_text)
            self.assertTrue(
                (exe_dir / "payload" / "skills" / "jev-consult" / "SKILL.md").is_file()
            )
            self.assertTrue(
                (exe_dir / "payload" / "scripts" / "install.py").is_file()
            )
            built = exe_dir.parent / "jev-setup-windows-amd64.exe"
            if os.name != "nt":
                # no exe produced off-Windows; the message says so
                self.assertFalse(built.exists())
                self.assertIn("Windows host", buf.getvalue())

    def test_stdlib_imports_cover_payload_network_deps(self) -> None:
        """hiddenimports must name what datas scripts import — PyInstaller
        does not analyze datas, so urllib.request/getpass would otherwise
        be missing from the frozen binary."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload"
            package_release._stage_payload(payload)
            names = package_release._stdlib_imports(
                payload, package_release.EXE_ENTRY
            )
        for need in ("urllib.request", "urllib.error", "getpass", "msvcrt"):
            self.assertIn(need, names)


def _bootstrap_ns() -> dict:
    """Exec the pyz __main__ source into a namespace without running main()."""
    ns = {"__name__": "jev_bootstrap_test"}
    exec(compile(package_release.BOOTSTRAP, "<bootstrap>", "exec"), ns)
    return ns


class BootstrapHardeningTests(unittest.TestCase):
    def test_launcher_checks_pyz_exists(self) -> None:
        self.assertIn('if not exist "%~dp0@PYZ@"', package_release.WINDOWS_LAUNCHER)
        self.assertIn("not found in %~dp0", package_release.WINDOWS_LAUNCHER)
        self.assertIn("exit /b 1", package_release.WINDOWS_LAUNCHER)

    def test_bootstrap_rmtree_fix_retries_once(self) -> None:
        ns = _bootstrap_ns()
        calls: list[str] = []

        def func(path: str) -> None:
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError("locked")

        ns["_rmtree_fix"](func, "p", None)
        self.assertEqual(calls, ["p", "p"])

        def stuck(path: str) -> None:
            raise PermissionError("locked")

        self.assertRaises(PermissionError, ns["_rmtree_fix"], stuck, "p", None)

    def test_bootstrap_locked_roundtrip(self) -> None:
        ns = _bootstrap_ns()
        with tempfile.TemporaryDirectory() as tmp:
            ns["LOCK_DIR"] = Path(tmp) / "bundle.lock"
            self.assertEqual(ns["_locked"](lambda: 42), 42)
            self.assertFalse(ns["LOCK_DIR"].exists())

    def test_bootstrap_locked_breaks_stale_lock(self) -> None:
        ns = _bootstrap_ns()
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "bundle.lock"
            lock.mkdir()
            stale = os.path.getmtime(lock) - 300
            os.utime(lock, (stale, stale))
            ns["LOCK_DIR"] = lock
            self.assertEqual(ns["_locked"](lambda: "ran"), "ran")
            self.assertFalse(lock.exists())

    def test_bootstrap_locked_live_lock_times_out(self) -> None:
        import itertools
        import types

        ns = _bootstrap_ns()
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "bundle.lock"
            lock.mkdir()  # held by "another process"
            ns["LOCK_DIR"] = lock
            ticks = itertools.count(0, 60)  # jump past the 30s deadline
            ns["time"] = types.SimpleNamespace(
                monotonic=lambda: next(ticks),
                time=__import__("time").time,
                sleep=lambda _s: None,
            )
            self.assertRaises(TimeoutError, ns["_locked"], lambda: None)
            self.assertTrue(lock.exists())  # holder's lock left alone


if __name__ == "__main__":
    unittest.main(verbosity=2)
