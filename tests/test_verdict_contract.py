"""Every --verdict writer routes through _watch.write_verdict.

That's the shared atomic (.tmp + rename) writer that also injects `ts`;
a script that hand-rolls its verdict write would skip both guarantees.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "jev-consult" / "scripts"


class VerdictContractTests(unittest.TestCase):
    def test_every_verdict_flag_uses_shared_writer(self) -> None:
        offenders = []
        count = 0
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name.startswith("_"):
                continue
            src = f.read_text(encoding="utf-8")
            if '"--verdict"' not in src:
                continue
            count += 1
            if "write_verdict(" not in src:
                offenders.append(f.name)
        self.assertGreaterEqual(count, 10, "verdict writers went missing")
        self.assertEqual(
            offenders, [], "scripts with --verdict but no write_verdict: %s" % offenders
        )

    def test_every_watch_script_emits_via_shared_helper(self) -> None:
        offenders = []
        count = 0
        # skill_scanner.py is a standalone vendored script (no _watch import
        # possible); it mirrors the emit contract inline instead.
        standalone = {"skill_scanner.py"}
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name.startswith("_") or f.name in standalone:
                continue
            src = f.read_text(encoding="utf-8")
            has_watch = '"--watch"' in src or "'--watch'" in src
            if not has_watch:
                continue
            count += 1
            if "emit_or_jq(" not in src and "_watch.emit(" not in src:
                offenders.append(f.name)
        self.assertGreaterEqual(count, 10, "watch loops went missing")
        self.assertEqual(
            offenders, [], "watch loops not using _watch emit: %s" % offenders
        )

    def test_watch_knob_flags_route_through_helpers(self) -> None:
        # --quiet presets read via _watch.quiet; --max-ticks via _watch.cap;
        # --watch-max via _watch.deadline — so env overrides stay uniform.
        # skill_scanner.py is standalone (no _watch import possible).
        standalone = {"skill_scanner.py"}
        offenders = []
        for f in sorted(SCRIPTS.glob("*.py")):
            if f.name.startswith("_") or f.name in standalone:
                continue
            src = f.read_text(encoding="utf-8")
            for flag, helper in (
                ('"--quiet"', "_watch.quiet("),
                ('"--max-ticks"', "cap("),
                ('"--watch-max"', "deadline("),
            ):
                if flag in src and helper not in src:
                    offenders.append("%s: %s without %s" % (f.name, flag, helper))
        self.assertEqual(offenders, [])

    def test_scanner_write_verdict_matches_shared_contract(self) -> None:
        # skill_scanner carries a standalone write_verdict (no _watch import
        # possible). Its guarantees must stay the shared ones: int-ts
        # injection, atomic file write, and '-' streaming the indent=2
        # payload to the given stream.
        import importlib.util
        import io
        import json
        import sys
        import tempfile

        def _load(name):
            spec = importlib.util.spec_from_file_location(
                "vcon_" + name.replace(".", "_"), SCRIPTS / name
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod

        watch = _load("_watch.py")
        scanner = _load("skill_scanner.py")
        payload = {"verdict": "REJECT-PENDING-REVIEW", "CRITICAL": 2}
        with tempfile.TemporaryDirectory() as tmp:
            for label, writer in (("watch", watch.write_verdict),
                                  ("scanner", scanner.write_verdict)):
                with self.subTest(impl=label):
                    out = Path(tmp) / ("%s.json" % label)
                    self.assertTrue(writer(str(out), dict(payload)))
                    data = json.loads(out.read_text(encoding="utf-8"))
                    self.assertEqual(data["verdict"], payload["verdict"])
                    self.assertIsInstance(data["ts"], int)
                    buf = io.StringIO()
                    self.assertTrue(writer("-", dict(payload), stream=buf))
                    streamed = json.loads(buf.getvalue())
                    self.assertEqual(streamed["verdict"], payload["verdict"])
                    self.assertIn("ts", streamed)

    def test_write_verdict_injects_ts(self) -> None:
        import importlib.util
        import sys
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "_watch", SCRIPTS / "_watch.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_watch"] = mod
        spec.loader.exec_module(mod)
        import json

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v.json"
            self.assertTrue(mod.write_verdict(str(out), {"verdict": "pass"}))
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["verdict"], "pass")
            self.assertIsInstance(data["ts"], int)


if __name__ == "__main__":
    unittest.main()
