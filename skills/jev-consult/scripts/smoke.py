#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""smoke.py - offline end-to-end sanity for the jev-consult pack.

Runs each script's offline path in a temp dir: policy loads + policy_lint,
jev scaffold + lint (no network ask), inventory scan, compact --fake,
decisions.py stats over a fixture log, trace.py init + state, doctor,
and jev.py ask --verdict against a one-shot 127.0.0.1 stub endpoint
(never the real Jev API; the stub key is a literal dummy). Prints JSON
{ok, steps:[{name, ok, detail}]}; exit 0/1. Safe for CI.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _watch  # noqa: E402
from xml.sax.saxutils import escape  # noqa: E402

import http.server  # noqa: E402
import threading  # noqa: E402

SKILL_DIR = SCRIPTS.parent


def jq_lookup(obj, path: str):
    """Dotted-path lookup; (value, True) or (None, False) when any part misses."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None, False
    return cur, True


def junit_xml(steps: list[dict]) -> str:
    """Render a JUnit <testsuite> document for the step rows."""
    failures = sum(1 for s in steps if not s.get("ok"))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<testsuite name="jev-smoke" tests="%d" failures="%d">'
        % (len(steps), failures),
    ]
    for s in steps:
        name = escape(str(s.get("name") or "step"), {'"': "&quot;"})
        lines.append(
            '  <testcase name="%s" classname="jev-consult.smoke">' % name
        )
        if not s.get("ok"):
            detail = escape(str(s.get("detail") or "failed"))
            lines.append('    <failure>%s</failure>' % detail)
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


def _step(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


STEP_TIMEOUT = 60.0


def _run(
    argv: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    inp: str | None = None,
    timeout: float | None = None,
) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=STEP_TIMEOUT if timeout is None else timeout,
        env=env,
        input=inp,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _stub_handler(noul: float = 0.99, drop_results: bool = False):
    """Localhost Jev stub: choice questions pick the first non-'none'
    criterion at 0.9 confidence; noul questions answer `noul` (or 0.1
    for result_* ids when drop_results is set)."""

    class _Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                request = {}
            answers: dict = {}
            for qid, q in (request.get("questions") or {}).items():
                if q.get("type") == "choice":
                    criteria = list(q.get("criteria") or [])
                    pick = next(
                        (k for k in criteria if k != "none"), "none"
                    )
                    rest = 0.1 / max(1, len(criteria) - 1)
                    answers[qid] = {
                        "type": "choice",
                        "choice": pick,
                        "confidence": 0.9,
                        "probabilities": {
                            k: (0.9 if k == pick else rest)
                            for k in criteria
                        },
                    }
                else:
                    drop = drop_results and str(qid).startswith("result_")
                    answers[qid] = {
                        "type": "noul",
                        "noul": 0.1 if drop else noul,
                    }
            reply = json.dumps({"answers": answers}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args) -> None:
            pass

    return _Stub


def _stub_policy(tmp: Path, name: str, port: int, env: dict) -> None:
    """Write a policy copy pointing at the stub port and set JEV_POLICY."""
    policy = json.loads(
        (SKILL_DIR / "policy.json").read_text(encoding="utf-8")
    )
    policy["endpoint"] = "http://127.0.0.1:%d/v1/systemone" % port
    policy_path = tmp / name
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    env["JEV_POLICY"] = str(policy_path)


def step_policy(tmp: Path) -> dict:
    path = SKILL_DIR / "policy.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _step("policy", False, "policy.json unreadable: %s" % exc)
    ok = (
        isinstance(data, dict)
        and bool(data.get("question_soft_max"))
        and isinstance(data.get("endpoint"), str)
        and data.get("endpoint", "").startswith("http")
        and bool(data.get("version"))
    )
    return _step("policy", ok, "keys=%d" % len(data) if ok else "missing question_soft_max/endpoint/version")


def step_policy_lint(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "policy_lint.py")])
    ok = rc == 0
    if ok:
        # --fix --dry-run reports without writing; --fix drops the key
        bad = tmp / "policy-bad.json"
        policy = json.loads(
            (SCRIPTS.parent / "policy.json").read_text(encoding="utf-8")
        )
        policy["smoke_bogus_key"] = 1
        bad.write_text(json.dumps(policy), encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                str(bad),
                "--fix",
                "--dry-run",
            ]
        )
        ok = (
            rc == 0
            and "would fix P011" in out
            and '"smoke_bogus_key"' in bad.read_text(encoding="utf-8")
        )
    if ok:
        rc, out = _run(
            [str(SCRIPTS / "policy_lint.py"), str(bad), "--fix"]
        )
        ok = (
            rc == 0
            and "fixed P011" in out
            and "smoke_bogus_key" not in bad.read_text(encoding="utf-8")
        )
    if ok:
        # --json emits a findings payload on the shipped policy
        rc, out = _run([str(SCRIPTS / "policy_lint.py"), "--json"])
        try:
            ok = rc == 0 and isinstance(json.loads(out).get("findings"), list)
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "policy_lint.py"), "--explain", "P001"]
        )
        ok = rc == 0 and "P001" in out
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe
        verdict = tmp / "plint-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2
        try:
            ok = ok and "verdict" in json.loads(
                verdict.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            ok = False
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        empty_policy = tmp / "policy-empty.json"
        empty_policy.write_text("{}", encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                str(empty_policy),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    if ok:
        # --show dumps the effective policy JSON and exits
        rc, out = _run(
            [str(SCRIPTS / "policy_lint.py"), str(bad), "--show"]
        )
        try:
            shown = json.loads(out)
            ok = rc == 0 and shown.get("policy", {}).get(
                "endpoint"
            ) == policy["endpoint"]
        except (ValueError, KeyError, TypeError):
            ok = False
    if ok:
        # --diff OTHER prints a key-level diff of OTHER -> PATH
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                str(bad),
                "--diff",
                str(empty_policy),
            ]
        )
        ok = (
            rc == 0
            and "difference(s)" in out
            and "+ endpoint" in out
        )
    if ok:
        # ~ line for a changed value; - line for a key only in OTHER
        changed_policy = tmp / "policy-changed.json"
        changed = dict(policy)
        changed["endpoint"] = "http://example.invalid/changed"
        changed_policy.write_text(
            json.dumps(changed), encoding="utf-8"
        )
        rc, out = _run(
            [
                str(SCRIPTS / "policy_lint.py"),
                str(bad),
                "--diff",
                str(changed_policy),
            ]
        )
        ok = (
            rc == 0
            and "~ endpoint:" in out
            and "- smoke_bogus_key" in out
        )
    return _step("policy_lint", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_jev_scaffold_lint(tmp: Path) -> dict:
    req = tmp / "smoke.request.json"
    state = tmp / "smoke.state.json"
    state.write_text(json.dumps({"plan": "smoke"}), encoding="utf-8")
    rc, out = _run(
        [
            str(SCRIPTS / "jev.py"),
            "scaffold",
            "approach",
            "--out",
            str(req),
            "--state",
            str(state),
            "--option",
            "approach=a:b",
        ]
    )
    if rc != 0 or not req.is_file():
        return _step("jev_scaffold", False, out.strip()[:160])
    rc, out = _run([str(SCRIPTS / "jev.py"), "lint", str(req)])
    ok = rc == 0
    if ok:
        # a choice template with empty criteria and no --option fails
        empty_policy = tmp / "policy-empty.json"
        policy = json.loads(
            (SKILL_DIR / "policy.json").read_text(encoding="utf-8")
        )
        policy["templates"] = {
            "empty_t": {
                "type": "choice",
                "instructions": "pick",
                "criteria": {},
            }
        }
        empty_policy.write_text(json.dumps(policy), encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "--policy",
                str(empty_policy),
                "scaffold",
                "empty_t",
                "--out",
                str(tmp / "empty.json"),
            ]
        )
        ok = rc == 2 and "empty template criteria" in out
    if ok:
        # ask --dry resolves the request locally (no API call, no key)
        env = dict(os.environ)  # skillscan:allow
        env.pop("TYPESAFE_API_KEY", None)
        rc, out = _run(
            [str(SCRIPTS / "jev.py"), "ask", str(req), "--dry"], env=env
        )
        if rc == 0:
            try:
                ok = "questions" in json.loads(out.strip())
            except ValueError:
                ok = False
        else:
            ok = False
    if ok:
        # lint --json/--jq report the findings payload; --strict turns a
        # warning-only request into rc 1
        rc, out = _run(
            [str(SCRIPTS / "jev.py"), "lint", str(req), "--json"]
        )
        try:
            ok = rc == 0 and isinstance(
                json.loads(out).get("findings"), list
            )
        except (ValueError, AttributeError):
            ok = False
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "jev.py"), "lint", str(req), "--jq", "errors"]
            )
            ok = rc == 0 and out.strip() == "0"
        if ok:
            # ~10k tokens of state hits J021 (warn) but not J020 (error)
            warn_req = tmp / "smoke-warn.request.json"
            warn_req.write_text(
                json.dumps(
                    {
                        "state": "padding " * 5000,
                        "questions": {
                            "q": {
                                "type": "noul",
                                "instructions": "Is the padding acceptable for this lint request?",
                                "criteria": {
                                    "true": "The padding is acceptable",
                                    "false": "The padding is not acceptable",
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            rc, out = _run(
                [str(SCRIPTS / "jev.py"), "lint", str(warn_req)]
            )
            ok = rc == 0 and "warning(s)" in out
            if ok:
                rc, out = _run(
                    [
                        str(SCRIPTS / "jev.py"),
                        "lint",
                        str(warn_req),
                        "--strict",
                    ]
                )
                ok = rc == 1
    return _step("jev_scaffold_lint", ok, out.strip()[:160] or "clean")


def step_inventory(tmp: Path) -> dict:
    home = tmp / "home"
    hermes = tmp / "hermes"
    (home / ".codex" / "skills" / "s").mkdir(parents=True)
    (home / ".codex" / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: smoke-skill\n---\n", encoding="utf-8"
    )
    rc, out = _run(
        [
            str(SCRIPTS / "inventory.py"),
            "--harness",
            "codex",
            "--home",
            str(home),
            "--hermes-home",
            str(hermes),
            "--task",
            "smoke",
            "--include",
            "smoke-skill",
        ]
    )
    ok = rc == 0 and "smoke-skill" in out
    if ok:
        # --diff OLD.json compares the current scan against a saved payload
        old_payload = tmp / "inv-old.json"
        old_payload.write_text(out, encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "inventory.py"),
                "--harness",
                "codex",
                "--home",
                str(home),
                "--hermes-home",
                str(hermes),
                "--task",
                "smoke",
                "--include",
                "smoke-skill",
                "--diff",
                str(old_payload),
            ]
        )
        if rc == 0:
            try:
                diff = json.loads(out)
                ok = diff.get("added") == [] and diff.get("removed") == []
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # --verdict PATH writes the slim one-shot probe JSON
        verdict = tmp / "inv-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "inventory.py"),
                "--harness",
                "codex",
                "--home",
                str(home),
                "--hermes-home",
                str(hermes),
                "--task",
                "smoke",
                "--verdict",
                str(verdict),
            ]
        )
        try:
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "ok"
        except (OSError, ValueError):
            ok = False
    if ok:
        sidecar = tmp / "sc.json"
        rc, out = _run(
            [
                str(SCRIPTS / "inventory.py"),
                "--harness",
                "codex",
                "--home",
                str(home),
                "--hermes-home",
                str(hermes),
                "--task",
                "smoke",
                "--include",
                "smoke-skill",
                "--sidecar",
                str(sidecar),
            ]
        )
        ok = rc == 0 and sidecar.is_file()
    if ok:
        rc, out = _run(
            [str(SCRIPTS / "inventory.py"), "--check-sidecar", str(sidecar)]
        )
        ok = rc == 0 and "fresh" in out
    if ok:
        # backdating written_at flips the sidecar to stale
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            payload["written_at"] = time.time() - 10 * 365 * 86400
            sidecar.write_text(json.dumps(payload), encoding="utf-8")
        except (OSError, ValueError):
            ok = False
    if ok:
        rc, out = _run(
            [str(SCRIPTS / "inventory.py"), "--check-sidecar", str(sidecar)]
        )
        ok = rc == 0 and "stale" in out
    if ok:
        rc, out = _run([str(SCRIPTS / "inventory.py"), "--show", str(sidecar)])
        if rc == 0:
            try:
                ok = json.loads(out).get("status") == "stale"
            except ValueError:
                ok = False
        else:
            ok = False
    if ok:
        # a scan with nothing installed reports an empty verdict
        empty_home = tmp / "empty-home"
        empty_home.mkdir(exist_ok=True)
        empty_verdict = tmp / "inv-empty-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "inventory.py"),
                "--harness",
                "codex",
                "--home",
                str(empty_home),
                "--hermes-home",
                str(empty_home / "hermes"),
                "--task",
                "smoke",
                "--verdict",
                str(empty_verdict),
            ]
        )
        try:
            payload = json.loads(empty_verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "empty"
        except (OSError, ValueError):
            ok = False
    if ok:
        # row-shaping flags on the scanned skill: --names, --count, --csv,
        # --jsonl, --kinds, --paths, --grep, --limit, --id, --out
        base = [
            str(SCRIPTS / "inventory.py"),
            "--harness",
            "codex",
            "--home",
            str(home),
            "--hermes-home",
            str(hermes),
            "--task",
            "smoke",
        ]
        rc, out = _run(base + ["--names"])
        ok = rc == 0 and "skill_smoke_skill" in out
        if ok:
            rc, out = _run(base + ["--count"])
            ok = rc == 0 and out.strip() == "1/1"
        if ok:
            rc, out = _run(base + ["--csv"])
            ok = rc == 0 and "skill_smoke_skill,skill,smoke-skill" in out
        if ok:
            rc, out = _run(base + ["--jsonl"])
            try:
                ok = (
                    rc == 0
                    and json.loads(out.strip()).get("id") == "skill_smoke_skill"
                )
            except (ValueError, AttributeError):
                ok = False
        if ok:
            rc, out = _run(base + ["--kinds"])
            ok = rc == 0 and "skill 1" in out
        if ok:
            rc, out = _run(base + ["--paths"])
            ok = rc == 0 and "skills" in out
        if ok:
            rc, out = _run(base + ["--grep", "smoke-skill"])
            ok = rc == 0 and "smoke-skill" in out
        if ok:
            rc, out = _run(base + ["--grep", "zzz-nope"])
            ok = rc == 0 and "smoke-skill" not in out
        if ok:
            rc, out = _run(base + ["--id", "skill_smoke_skill"])
            ok = rc == 0 and "smoke-skill" in out
        if ok:
            rc, out = _run(base + ["--id", "nope"])
            ok = rc in (0, 1) and "smoke-skill" not in out
        if ok:
            out_file = tmp / "inv-out.json"
            rc, out = _run(base + ["--out", str(out_file)])
            ok = rc == 0 and out_file.is_file()
    if ok:
        # --watch emits {counts,shortlist,added,removed} ticks; the first
        # tick always prints (baseline) and --quiet keeps later clean
        # ticks off stdout
        rc, out = _run(
            base + ["--watch", "0.03", "--max-ticks", "2", "--quiet"]
        )
        ticks = [ln for ln in out.splitlines() if '"added"' in ln]
        ok = rc == 0 and len(ticks) == 1 and "watch tick=2" in out
    if ok:
        # --limit caps the shortlist; --scores adds an IDF score to each
        rc, out = _run(base + ["--limit", "1"])
        try:
            ok = rc == 0 and len(json.loads(out).get("shortlist", [])) <= 1
        except ValueError:
            ok = False
        if ok:
            rc, out = _run(base + ["--scores"])
            try:
                ok = rc == 0 and "score" in json.dumps(
                    json.loads(out).get("shortlist", [])
                )
            except ValueError:
                ok = False
        if ok:
            rc, out = _run(base + ["--all-names"])
            ok = rc == 0 and "smoke-skill" in out
    if ok:
        # --check-miss prints fresh/stale/missing for the miss marker;
        # --ttl 0 flips a fresh one to stale
        miss = tmp / ".jev-tools-miss.json"
        miss.write_text(
            json.dumps({"task": "smoke", "written_at": time.time()}),
            encoding="utf-8",
        )
        rc, out = _run(
            [str(SCRIPTS / "inventory.py"), "--check-miss", str(miss)]
        )
        ok = rc == 0 and out.startswith("fresh")
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "inventory.py"),
                    "--check-miss",
                    str(miss),
                    "--ttl",
                    "0",
                ]
            )
            ok = rc == 0 and out.startswith("stale")
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "inventory.py"),
                    "--check-miss",
                    str(tmp / "no-such-miss.json"),
                ]
            )
            ok = rc == 0 and out.startswith("missing")
    if ok:
        # --write-ask PATH writes a picker request with installed_enough
        ask_file = tmp / "inv-ask.json"
        rc, _ = _run(base + ["--write-ask", str(ask_file)])
        try:
            ask = json.loads(ask_file.read_text(encoding="utf-8"))
            ok = rc == 0 and "installed_enough" in ask.get("questions", {})
        except (OSError, ValueError, AttributeError):
            ok = False
    if ok:
        # --fail-fast stops the watch when a new skill appears mid-run
        ff_home = tmp / "inv-ff-home"
        (ff_home / ".codex" / "skills" / "a").mkdir(parents=True)
        (ff_home / ".codex" / "skills" / "a" / "SKILL.md").write_text(
            "---\nname: alpha-skill\n---\n", encoding="utf-8"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "inventory.py"),
                "--harness",
                "codex",
                "--home",
                str(ff_home),
                "--hermes-home",
                str(tmp / "inv-ff-hermes"),
                "--task",
                "smoke",
                "--watch",
                "0.15",
                "--fail-fast",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(0.4)
        (ff_home / ".codex" / "skills" / "b").mkdir(parents=True)
        (ff_home / "b-skill").mkdir(exist_ok=True)
        (ff_home / ".codex" / "skills" / "b" / "SKILL.md").write_text(
            "---\nname: beta-skill\n---\n", encoding="utf-8"
        )
        try:
            wout, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            wout = ""
        ok = proc.returncode == 0 and '"added"' in wout and "beta" in wout
    if ok:
        # --catalogs prints marketplace name/url rows; --show-policy
        # dumps the effective policy dict
        rc, out = _run([str(SCRIPTS / "inventory.py"), "--catalogs"])
        ok = rc == 0 and "\t" in out and "http" in out
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "inventory.py"), "--show-policy"]
            )
            try:
                ok = rc == 0 and isinstance(
                    json.loads(out).get("endpoint"), str
                )
            except (ValueError, AttributeError):
                ok = False
    if ok:
        # --prune-sidecars unlinks stale .jev-tools*.json under DIR;
        # --dry-run lists without unlinking
        prune_dir = tmp / "inv-prune"
        prune_dir.mkdir(parents=True, exist_ok=True)
        stale_sc = prune_dir / ".jev-tools.json"
        stale_sc.write_text(
            json.dumps({"written_at": 1}), encoding="utf-8"
        )
        fresh_sc = prune_dir / ".jev-tools-miss.json"
        fresh_sc.write_text(
            json.dumps({"written_at": int(time.time())}),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "inventory.py"),
                "--prune-sidecars",
                str(prune_dir),
                "--dry-run",
            ]
        )
        ok = rc == 0 and stale_sc.is_file()
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "inventory.py"),
                    "--prune-sidecars",
                    str(prune_dir),
                ]
            )
            ok = (
                rc == 0
                and not stale_sc.is_file()
                and fresh_sc.is_file()
            )
    if ok:
        # --kind filters the emitted rows to the named kind
        kind_home = tmp / "inv-kind-home"
        (kind_home / ".codex" / "skills" / "a").mkdir(parents=True)
        (kind_home / ".codex" / "skills" / "a" / "SKILL.md").write_text(
            "---\nname: alpha-kind\n---\n", encoding="utf-8"
        )
        rc, out = _run(
            [
                str(SCRIPTS / "inventory.py"),
                "--harness",
                "codex",
                "--home",
                str(kind_home),
                "--hermes-home",
                str(tmp / "inv-kind-hermes"),
                "--task",
                "alpha",
                "--kind",
                "skill",
            ]
        )
        try:
            payload = json.loads(out)
            items = payload.get("shortlist", [])
            ok = (
                rc == 0
                and items
                and all(r.get("kind") == "skill" for r in items)
                and payload.get("counts", {}).get("skill") == 1
            )
        except (ValueError, AttributeError, TypeError):
            ok = False
    return _step("inventory", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_compact_fake(tmp: Path) -> dict:
    transcript = tmp / "t.json"
    transcript.write_text(
        json.dumps(
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "ok"},
            ]
        ),
        encoding="utf-8",
    )
    rc, out = _run(
        [str(SCRIPTS / "compact.py"), str(transcript), "--history", "--fake", "--min-reduction", "0"]
    )
    ok = rc == 0 and '"stats"' in out
    if ok:
        # --check exits 1 when the transcript compacts below the gate
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--check",
                "--min-reduction",
                "50",
            ]
        )
        ok = rc == 1 and "check: FAIL" in out
    if ok:
        # --report writes the stats dict as JSON to PATH
        report = tmp / "compact-report.json"
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--report",
                str(report),
            ]
        )
        try:
            stats = json.loads(report.read_text(encoding="utf-8"))
            ok = rc == 0 and isinstance(stats, dict) and bool(stats)
        except (OSError, ValueError):
            ok = False
    if ok:
        # --watch S --max-ticks N re-scores the transcript each tick
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
            ]
        )
        # exits 1 when the last tick fell back (already-compact transcript)
        ok = rc in (0, 1) and out.count("watch tick=") == 2
    if ok:
        # --quiet keeps non-fallback ticks off stdout; stderr still logs
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"fallback"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --verdict writes a slim {verdict, ticks, reduction, fallback}
        # probe — one-shot after a plain run, refreshed per watch tick
        cverdict = tmp / "compact-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--verdict",
                str(cverdict),
            ]
        )
        try:
            ok = rc == 0 and json.loads(
                cverdict.read_text(encoding="utf-8")
            ).get("verdict") == "ok"
        except (OSError, ValueError):
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "compact.py"),
                    str(transcript),
                    "--history",
                    "--fake",
                    "--min-reduction",
                    "0",
                    "--watch",
                    "0.05",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(cverdict),
                ]
            )
            try:
                ok = rc == 0 and json.loads(
                    cverdict.read_text(encoding="utf-8")
                ).get("verdict") == "ok"
            except (OSError, ValueError):
                ok = False
    if ok:
        # --fail-fast breaks the watch on the first fallback tick
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "50",
                "--watch",
                "0.05",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ok = rc == 1 and out.count("watch tick=") == 1
    if ok:
        # --stats prints a one-line summary; --stats-json the stats dict
        # (both on stderr, after the result JSON)
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--stats",
            ]
        )
        ok = rc == 0 and "stats:" in out
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--stats-json",
            ]
        )
        ok = rc == 0 and '"charsBefore"' in out
    if ok:
        # --explain logs one decision line per tool call on stderr
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                str(transcript),
                "--history",
                "--fake",
                "--min-reduction",
                "0",
                "--explain",
            ]
        )
        ok = rc == 0
    if ok:
        # real (non-fake) path: a localhost stub Jev keeps the call
        # but drops the result — the output shrinks and the spilled
        # body lands on disk (lossless). A fat tool_result fixture.
        real_t = tmp / "t-real.json"
        real_t.write_text(
            json.dumps(
                [
                    {"role": "user", "content": "read the file"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "r1",
                                "name": "read_file",
                                "input": {"path": "a.py"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "r1",
                                "content": "# body\n" + "line\n" * 800,
                            }
                        ],
                    },
                    {"role": "assistant", "content": "done"},
                ]
            ),
            encoding="utf-8",
        )
        spill_dir = tmp / "compact-spill"
        env2 = dict(os.environ)  # skillscan:allow
        env2["TYPESAFE_API_KEY"] = "smoke-stub-key"
        env2["USERPROFILE"] = str(tmp / "compact-home")
        env2["HOME"] = str(tmp / "compact-home")
        env2["JEV_CONSULT_SPILL"] = str(spill_dir)

        server = http.server.HTTPServer(

            ("127.0.0.1", 0), _stub_handler(drop_results=True)

        )

        try:

            _stub_policy(

                tmp, "compact-policy.json", server.server_address[1], env2,

            )
            threading.Thread(
                target=server.handle_request, daemon=True
            ).start()
            real_out = tmp / "t-compacted.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "compact.py"),
                    str(real_t),
                    "--history",
                    "--min-reduction",
                    "0",
                    "--preserve-recent",
                    "0",
                    "-o",
                    str(real_out),
                ],
                env=env2,
            )
            ok = rc == 0
            if ok:
                try:
                    compacted = json.loads(
                        real_out.read_text(encoding="utf-8")
                    )
                    stats = compacted.get("stats") or {}
                    ok = (
                        int(stats.get("charsAfter") or 0)
                        < int(stats.get("charsBefore") or 0)
                        and int(stats.get("resultsDropped") or 0) == 1
                        and any(spill_dir.iterdir())
                    )
                except (OSError, ValueError, TypeError):
                    ok = False
        finally:
            server.server_close()
    if ok:
        # option flags under --fake: keep_text pins a matching call,
        # keep_threshold above every noul keeps all calls, keep_first
        # pins the leading message, trace pins a file_path match, goal
        # is echoed into the stats payload
        opt_t = tmp / "t-opt.json"
        opt_t.write_text(
            json.dumps(
                [
                    {"role": "user", "content": "first smoke text"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "c1",
                                "name": "Read",
                                "input": {
                                    "file_path": "smoke_file.py",
                                    "extra": "needle-token",
                                },
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "c1",
                                "content": "R" * 400,
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "c2",
                                "name": "Bash",
                                "input": {"command": "smoke_cmd"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "c2",
                                "content": "Q" * 400,
                            }
                        ],
                    },
                ]
            ),
            encoding="utf-8",
        )
        base = [
            str(SCRIPTS / "compact.py"),
            str(opt_t),
            "--fake",
            "--history",
            "--preserve-recent",
            "0",
            "--min-reduction",
            "0",
        ]
        out_a = tmp / "opt-a.json"
        rc, out = _run(base + ["-o", str(out_a)])
        ok = rc == 0
        if ok:
            # baseline --fake run drops every call: needle-token gone
            try:
                compacted = json.loads(out_a.read_text(encoding="utf-8"))
                ok = "needle-token" not in out_a.read_text(
                    encoding="utf-8"
                ) and isinstance(compacted.get("stats"), dict)
            except (OSError, ValueError, AttributeError):
                ok = False
        if ok:
            # --keep-text pins the call whose input matches the pattern
            out_b = tmp / "opt-b.json"
            rc, out = _run(
                base
                + ["-o", str(out_b), "--keep-text", "needle-token"]
            )
            ok = rc == 0 and "needle-token" in out_b.read_text(
                encoding="utf-8"
            )
        if ok:
            # --keep-threshold below every fake noul keeps all calls
            out_c = tmp / "opt-c.json"
            rc, out = _run(
                base + ["-o", str(out_c), "--keep-threshold", "0.05"]
            )
            try:
                stats = json.loads(
                    out_c.read_text(encoding="utf-8")
                ).get("stats", {})
                ok = rc == 0 and int(
                    stats.get("charsAfter") or 0
                ) >= int(stats.get("charsBefore") or 0)
            except (OSError, ValueError, TypeError):
                ok = False
        if ok:
            # --trace pins calls whose file_path echoes the trace state
            trace = tmp / "opt-trace.json"
            trace.write_text(
                json.dumps({"current_step": "smoke_file.py"}),
                encoding="utf-8",
            )
            out_d = tmp / "opt-d.json"
            rc, out = _run(
                base + ["-o", str(out_d), "--trace", str(trace)]
            )
            ok = rc == 0 and "needle-token" in out_d.read_text(
                encoding="utf-8"
            )
        if ok:
            # --keep-first pins the leading messages: index 0 is always
            # pinned, so a tool call at index 1 only survives under
            # --keep-first 2
            head_t = tmp / "t-opt-head.json"
            head_t.write_text(
                json.dumps(
                    [
                        {"role": "user", "content": "lead text"},
                        {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "c0",
                                    "name": "Bash",
                                    "input": {"command": "smoke-head"},
                                }
                            ],
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "c0",
                                    "content": "H" * 300,
                                }
                            ],
                        },
                        {"role": "user", "content": "tail text"},
                    ]
                ),
                encoding="utf-8",
            )
            base_head = [
                str(SCRIPTS / "compact.py"),
                str(head_t),
                "--fake",
                "--history",
                "--preserve-recent",
                "0",
                "--min-reduction",
                "0",
            ]
            out_g = tmp / "opt-g.json"
            rc, out = _run(base_head + ["-o", str(out_g)])
            if ok := (
                rc == 0
                and "smoke-head" not in out_g.read_text(encoding="utf-8")
            ):
                out_h = tmp / "opt-h.json"
                rc, out = _run(
                    base_head
                    + ["-o", str(out_h), "--keep-first", "2"]
                )
                ok = rc == 0 and "smoke-head" in out_h.read_text(
                    encoding="utf-8"
                )
        if ok:
            # --preview emits the decision plan and applies nothing
            prev_out = tmp / "compact-preview.json"
            rc, out = _run(
                base + ["--preview", "-o", str(prev_out)],
                cwd=tmp,
                env=env2,
            )
            try:
                payload = json.loads(out)
                plan = payload.get("plan") or []
                ok = (
                    rc == 0
                    and payload.get("preview") is True
                    and len(plan) == 2
                    and plan[0].get("action") == "drop_call"
                    and plan[0].get("reason") == "call_dropped"
                    and plan[1].get("action") == "drop_call"
                    and "messages" not in payload
                    and not prev_out.exists()
                )
            except ValueError:
                ok = False
        if ok:
            # --goal parses and runs (it shapes the Jev state, not output)
            out_e = tmp / "opt-e.json"
            rc, out = _run(
                base + ["-o", str(out_e), "--goal", "smoke-goal"]
            )
            ok = rc == 0 and out_e.is_file()
        if ok:
            # --truncate-head-chars bounds the head kept in a dropped
            # result's emitted text (stub drops result_* nouls)
            head_server = http.server.HTTPServer(
                ("127.0.0.1", 0), _stub_handler(drop_results=True)
            )
            env3 = dict(env2)
            spill3 = tmp / "spill-head"
            env3["JEV_CONSULT_SPILL"] = str(spill3)
            _stub_policy(
                tmp,
                "head-policy.json",
                head_server.server_address[1],
                env3,
            )
            try:
                threading.Thread(
                    target=head_server.handle_request, daemon=True
                ).start()
                out_f = tmp / "opt-f.json"
                rc, out = _run(
                    [
                        str(SCRIPTS / "compact.py"),
                        str(opt_t),
                        "--history",
                        "--min-reduction",
                        "0",
                        "--preserve-recent",
                        "0",
                        "--truncate-head-chars",
                        "25",
                        "-o",
                        str(out_f),
                    ],
                    env=env3,
                )
                text_f = out_f.read_text(encoding="utf-8") if rc == 0 else ""
                ok = (
                    rc == 0
                    and "full output saved" in text_f
                    and "R" * 26 not in text_f
                )
            finally:
                head_server.server_close()
    return _step("compact_fake", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_decisions(tmp: Path) -> dict:
    log = tmp / "decisions.jsonl"
    log.write_text(
        '\n'.join(
            json.dumps({"ts": 1700000000, "jev_status": s, "harness": "smoke"})
            for s in ("winner", "none")
        )
        + "\n",
        encoding="utf-8",
    )
    rc, out = _run([str(SCRIPTS / "decisions.py"), "--file", str(log), "--json"])
    ok = False
    if rc == 0:
        try:
            ok = json.loads(out).get("total") == 2
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--statuses",
                "--json",
            ]
        )
        if rc == 0:
            try:
                ok = json.loads(out).get("counts", {}).get("winner") == 1
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        bad_log = tmp / "decisions-bad.jsonl"
        bad_log.write_text(
            "not json\n" + json.dumps({"harness": "smoke"}) + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--errors",
                "--json",
            ]
        )
        try:
            ok = rc == 1 and len(json.loads(out)) == 1
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--validate",
                "--json",
            ]
        )
        try:
            rows = json.loads(out)
            ok = rc == 1 and rows and rows[0]["missing"] == [
                "ts",
                "jev_status",
            ]
        except (ValueError, IndexError, KeyError):
            ok = False
    if ok:
        # --drop-bad --dry-run reports without rewriting; --drop-bad applies
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--drop-bad",
                "--dry-run",
            ]
        )
        ok = (
            rc == 0
            and "would drop 1" in out
            and len(bad_log.read_text(encoding="utf-8").splitlines()) == 2
        )
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(bad_log),
                "--drop-bad",
            ]
        )
        try:
            lines = bad_log.read_text(encoding="utf-8").splitlines()
            ok = (
                rc == 0
                and "dropped 1" in out
                and len(lines) == 1
                and json.loads(lines[0]).get("harness") == "smoke"
            )
        except (ValueError, IndexError):
            ok = False
    if ok:
        # --prune --dry-run reports counts without rewriting; --prune applies
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--status",
                "winner",
                "--prune",
                "--dry-run",
                "--json",
            ]
        )
        try:
            summary = json.loads(out).get("prune_dry_run", {})
            ok = (
                rc == 0
                and summary.get("kept") == 1
                and summary.get("would_prune") == 1
                and len(log.read_text(encoding="utf-8").splitlines()) == 2
            )
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --tail 1 lists only the last entry (a "none" row)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--tail",
                "1",
            ]
        )
        ok = rc == 0 and "none" in out.splitlines()[-1] and "winner" not in out.splitlines()[-1]
    if ok:
        # --first 1 lists only the earliest entry (a "winner" row)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--first",
                "1",
            ]
        )
        ok = rc == 0 and "winner" in out.splitlines()[-1] and "none" not in out.splitlines()[-1]
    if ok:
        # --reverse flips the listed rows (the "none" row leads)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--first",
                "2",
                "--reverse",
            ]
        )
        ok = rc == 0 and "none" in out.splitlines()[-2] and "winner" in out.splitlines()[-1]
    if ok:
        # --last prints the newest matching entry as JSON
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--last",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("jev_status") == "none"
        except ValueError:
            ok = False
    if ok:
        # --oldest prints the earliest matching entry as JSON
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--oldest",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("jev_status") == "winner"
        except ValueError:
            ok = False
    if ok:
        # --nth 1 prints the first matching entry as JSON
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--nth",
                "1",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("jev_status") == "winner"
        except ValueError:
            ok = False
    if ok:
        # --field KEY=VALUE filters on any entry field
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--field",
                "jev_status=none",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --grep substring-filters across string fields
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--grep",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --status filters on jev_status
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--status",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --harness filters on the harness field
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--harness",
                "smoke",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 2
        except ValueError:
            ok = False
    if ok:
        # --outcome filters on the fill outcome field
        outcome_log = tmp / "decisions-outcome.jsonl"
        outcome_log.write_text(
            '\n'.join(
                json.dumps({"ts": 1, "jev_status": "fill", "outcome": s})
                for s in ("human", "blocked")
            )
            + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(outcome_log),
                "--outcome",
                "human",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --min-need filters on the numeric need field
        need_log = tmp / "decisions-need.jsonl"
        need_log.write_text(
            '\n'.join(
                json.dumps({"ts": 1, "jev_status": "winner", "need": n})
                for n in (0.2, 0.9)
            )
            + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(need_log),
                "--min-need",
                "0.5",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --where KEY=VAL filters by field equality (case-insensitive)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--where",
                "jev_status=WINNER",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --winner/--winners/--explicit filter on winner name + explicit flag
        winner_log = tmp / "decisions-winners.jsonl"
        winner_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "winner": {"kind": "skill", "name": "alpha"}, "explicit": True, "question": "explicit"}),
                    json.dumps({"ts": 2, "jev_status": "winner", "winner": {"kind": "skill", "name": "beta"}}),
                    json.dumps({"ts": 3, "jev_status": "none"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--winner",
                "alpha",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--explicit",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--winners",
            ]
        )
        ok = rc == 0 and "skill:alpha" in out and "skill:beta" in out
    if ok:
        # --missing FIELD keeps only entries lacking the field
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--missing",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --jq KEY --jq-where-contains SUB keeps only matching values
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--jq",
                "winner.name",
                "--jq-where-contains",
                "alp",
            ]
        )
        ok = rc == 0 and out.strip() == "alpha"
    if ok:
        # --since/--until bound entries by ts window
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--since",
                "2",
                "--until",
                "2",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --daily buckets counts by UTC day
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--daily",
            ]
        )
        ok = rc == 0 and "2023-11-14" in out
    if ok:
        # --group-by FIELD counts entries per field value
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--group-by",
                "jev_status",
            ]
        )
        ok = rc == 0 and "winner" in out and "none" in out
    if ok:
        # --count prints only the matching entry count
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--count",
            ]
        )
        ok = rc == 0 and out.strip() == "2"
    if ok:
        # --question KIND filters by the routing question kind
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--question",
                "explicit",
                "--json",
            ]
        )
        # only the explicit-pick row carries question="explicit"
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --uniq dedupes --jq values
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "harness",
                "--uniq",
            ]
        )
        ok = rc == 0 and out.strip() == "smoke"
    if ok:
        # --fields lists field names with counts
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--fields",
            ]
        )
        ok = rc == 0 and "jev_status" in out and "harness" in out
    if ok:
        # count lists: --harnesses/--outcomes/--fills/--dedupes group a field
        kind_log = tmp / "decisions-kinds.jsonl"
        kind_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "harness": "hermes", "outcome": "human", "fill": "trace", "dedupe": True}),
                    json.dumps({"ts": 2, "jev_status": "none", "harness": "codex", "outcome": "auto", "fill": "peer", "dedupe": False}),
                    json.dumps({"ts": 3, "jev_status": "winner", "harness": "hermes", "outcome": "human", "fill": "trace", "dedupe": True}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        counts_ok = True
        for flag, want in (
            ("--harnesses", "hermes 2"),
            ("--outcomes", "human 2"),
            ("--fills", "trace 2"),
            ("--dedupes", "True 2"),
        ):
            rc, out = _run(
                [str(SCRIPTS / "decisions.py"), "--file", str(kind_log), flag]
            )
            if not (rc == 0 and want in out):
                counts_ok = False
        ok = counts_ok
    if ok:
        # --top N caps count-list rows
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(kind_log),
                "--harnesses",
                "--top",
                "1",
            ]
        )
        ok = rc == 0 and out.strip() == "hermes 2"
    if ok:
        # boolean-flag filters keep only entries with the flag true
        flag_log = tmp / "decisions-flags.jsonl"
        flag_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "dedupe": True, "stale_sidecar": True, "strong_pick": True, "over_budget": True, "prompt_head": "hello-jev", "prompt_sha": "abc123"}),
                    json.dumps({"ts": 2, "jev_status": "none"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        flag_ok = True
        for flag in ("--dedupe-only", "--stale", "--strong", "--over-budget"):
            rc, out = _run(
                [
                    str(SCRIPTS / "decisions.py"),
                    "--file",
                    str(flag_log),
                    flag,
                    "--json",
                ]
            )
            try:
                if not (rc == 0 and json.loads(out).get("total") == 1):
                    flag_ok = False
            except ValueError:
                flag_ok = False
        ok = flag_ok
    if ok:
        # --prompt substring-matches prompt_head/prompt_tail
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(flag_log),
                "--prompt",
                "HELLO",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --sha PREFIX matches prompt_sha prefix
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(flag_log),
                "--sha",
                "abc",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --week (--days 7) keeps only recent entries; old rows drop out
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--week",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 0
        except ValueError:
            ok = False
    if ok:
        # --days N bounds the window directly: far-past rows drop for small
        # N, a huge N keeps them all
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--days",
                "30",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 0
        except ValueError:
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "decisions.py"),
                    "--file",
                    str(log),
                    "--days",
                    "99999",
                    "--json",
                ]
            )
            try:
                ok = rc == 0 and json.loads(out).get("total") == 2
            except ValueError:
                ok = False
    if ok:
        # --since-last STATUS keeps entries after the newest row of that status
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(winner_log),
                "--since-last",
                "winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --where-not KEY=VAL drops matching rows
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--where-not",
                "jev_status=winner",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --max-need filters on the numeric need ceiling
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(need_log),
                "--max-need",
                "0.5",
                "--json",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("total") == 1
        except ValueError:
            ok = False
    if ok:
        # --jq-first/--jq-last pick the first/last extracted value
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "jev_status",
                "--jq-first",
            ]
        )
        ok = rc == 0 and out.strip() == "winner"
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "jev_status",
                "--jq-last",
            ]
        )
        ok = rc == 0 and out.strip() == "none"
    if ok:
        # --jq with an unknown dotted key is graceful: null per row, rc 0
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "nope.nope",
            ]
        )
        ok = (
            rc == 0
            and [ln.strip() for ln in out.splitlines()] == ["null", "null"]
        )
    if ok:
        # --jq digs a flat field present on every row
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--jq",
                "harness",
            ]
        )
        ok = (
            rc == 0
            and [ln.strip() for ln in out.splitlines()] == ["smoke", "smoke"]
        )
    if ok:
        # numeric-field threshold filters keep only the high row
        num_log = tmp / "decisions-num.jsonl"
        num_log.write_text(
            '\n'.join(
                [
                    json.dumps({"ts": 1, "jev_status": "winner", "fill": "peer", "prompt_len": 100, "shortlist_n": 5, "shortlist_score_avg": 0.9, "n_catalog": 4, "latency_ms": 500}),
                    json.dumps({"ts": 2, "jev_status": "winner", "prompt_len": 1, "shortlist_n": 1, "shortlist_score_avg": 0.1, "n_catalog": 0, "latency_ms": 9000}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        num_ok = True
        for flag, val in (
            ("--fill", "peer"),
            ("--min-prompt-len", "50"),
            ("--min-shortlist", "3"),
            ("--min-score", "0.5"),
            ("--min-catalog", "2"),
            ("--max-latency", "1000"),
            ("--min-latency", "1000"),
        ):
            rc, out = _run(
                [
                    str(SCRIPTS / "decisions.py"),
                    "--file",
                    str(num_log),
                    flag,
                    val,
                    "--json",
                ]
            )
            try:
                if not (rc == 0 and json.loads(out).get("total") == 1):
                    num_ok = False
            except ValueError:
                num_ok = False
        ok = num_ok
    if ok:
        # --sample N emits N matching entries as JSON lines
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--sample",
                "1",
            ]
        )
        ok = rc == 0 and len([ln for ln in out.splitlines() if ln.strip()]) >= 1
    if ok:
        # --csv emits a header plus one row per entry, no stats
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--csv",
            ]
        )
        lines = out.strip().splitlines()
        ok = (
            rc == 0
            and len(lines) == 3
            and lines[0].startswith("ts,")
            and lines[2].split(",")[2] == "none"
        )
    if ok:
        # --md emits a Markdown table (header, separator, rows)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--md",
            ]
        )
        lines = out.strip().splitlines()
        ok = (
            rc == 0
            and len(lines) == 4
            and lines[0].startswith("| ts")
            and "| none |" in lines[3]
        )
    if ok:
        # --skip 1 drops the first match; --jsonl emits the rest raw
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--skip",
                "1",
                "--jsonl",
            ]
        )
        lines = out.strip().splitlines()
        try:
            ok = (
                rc == 0
                and len(lines) == 1
                and json.loads(lines[0]).get("jev_status") == "none"
            )
        except ValueError:
            ok = False
    if ok:
        # --out writes the filtered entries as JSONL instead of printing
        filtered = tmp / "filtered.jsonl"
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--out",
                str(filtered),
            ]
        )
        try:
            rows = [
                json.loads(line)
                for line in filtered.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            ok = rc == 0 and len(rows) == 2
        except (OSError, ValueError):
            ok = False
    if ok:
        # --report writes a markdown stats report to PATH
        report = tmp / "report.md"
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--report",
                str(report),
            ]
        )
        ok = rc == 0 and report.is_file() and "winner" in report.read_text(encoding="utf-8")
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--status",
                "none",
                "--prune",
            ]
        )
        try:
            lines = log.read_text(encoding="utf-8").splitlines()
            ok = (
                rc == 0
                and len(lines) == 1
                and json.loads(lines[0]).get("jev_status") == "none"
            )
        except (ValueError, IndexError):
            ok = False
    if ok:
        # --watch emits {ts,count} ticks until --max-ticks stops it
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
            ]
        )
        ok = rc == 0 and '"count": 1' in out and "watch tick=2" in out
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"count"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --watch-max S bounds the loop by elapsed seconds
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.02",
                "--watch-max",
                "0.05",
            ]
        )
        ok = rc == 0 and "watch tick=" in out
    if ok:
        # --fail-fast breaks on the first tick with removals: shrink the
        # log mid-watch and the run exits 1 with verdict "removed"
        shrink_log = tmp / "decisions-shrink.jsonl"
        shrink_log.write_text(
            '\n'.join(
                json.dumps({"ts": t, "jev_status": "winner"})
                for t in (1, 2)
            )
            + "\n",
            encoding="utf-8",
        )
        wv = tmp / "decisions-watch-verdict.json"
        wout = ""
        proc = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(shrink_log),
                "--watch",
                "0.15",
                "--fail-fast",
                "--verdict",
                str(wv),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            time.sleep(0.4)  # let the first tick sample the full log
            shrink_log.write_text(
                json.dumps({"ts": 1, "jev_status": "winner"}) + "\n",
                encoding="utf-8",
            )
            wout, _ = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        ok = proc.returncode == 1 and '"removed": 1' in wout
        try:
            ok = ok and json.loads(wv.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "removed"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --verdict on an empty log writes {verdict: empty}
        empty_log = tmp / "decisions-empty.jsonl"
        empty_log.write_text("", encoding="utf-8")
        verdict = tmp / "decisions-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(empty_log),
                "--verdict",
                str(verdict),
            ]
        )
        try:
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "empty"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --jq inside --watch prints just the named tick field(s)
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--jq",
                "count",
            ]
        )
        lines = [
            ln.strip()
            for ln in out.splitlines()
            if ln.strip().isdigit()
        ]
        ok = rc == 0 and len(lines) == 2 and len(set(lines)) == 1
    if ok:
        # --watch-max bounds a slow watch by elapsed seconds
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.2",
                "--watch-max",
                "0.05",
                "--max-ticks",
                "20",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"count"' in ln]
        ok = rc == 0 and len(ticks) == 1
    if ok:
        # --out inside --watch appends each tick line to the file
        tick_log = tmp / "decisions-ticks.jsonl"
        rc, out = _run(
            [
                str(SCRIPTS / "decisions.py"),
                "--file",
                str(log),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--out",
                str(tick_log),
            ]
        )
        try:
            tick_lines = [
                ln
                for ln in tick_log.read_text(encoding="utf-8").splitlines()
                if '"count"' in ln
            ]
            ok = rc == 0 and len(tick_lines) == 2
        except OSError:
            ok = False
    return _step("decisions", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_trace(tmp: Path) -> dict:
    trace_file = tmp / ".jev-trace.json"
    rc, out = _run(
        [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "init", "--plan", "smoke"]
    )
    if rc != 0 or not trace_file.is_file():
        return _step("trace", False, out.strip()[:160] or "rc=%d" % rc)
    rc, out = _run(
        [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "state"]
    )
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out), dict)
        except ValueError:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "record",
                "--pick",
                "smoke-pick",
                "--note",
                "smoke note",
            ]
        )
        ok = rc == 0
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "notes",
                "--json",
            ]
        )
        if rc == 0:
            try:
                ok = any(
                    "smoke note" in str(n.get("text", ""))
                    for n in json.loads(out)
                )
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # notes --prune N rewrites the trace keeping only the last N notes
        _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "record",
                "--pick",
                "smoke-pick-2",
                "--note",
                "second note",
            ]
        )
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "notes",
                "--prune",
                "1",
            ]
        )
        if rc == 0:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--json",
                ]
            )
            try:
                notes = json.loads(out)
                ok = rc == 0 and len(notes) == 1 and "second" in str(notes[0].get("text", ""))
            except (ValueError, IndexError):
                ok = False
        else:
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "history",
                "--json",
            ]
        )
        if rc == 0:
            try:
                ok = any(
                    "smoke-pick" in str(p.get("pick", ""))
                    for p in json.loads(out)
                )
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # export dumps the whole trace bundle (state + history + notes)
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "export",
            ]
        )
        if rc == 0:
            try:
                bundle = json.loads(out)
                ok = (
                    "notes" in bundle
                    and "file" in bundle
                    and bundle.get("plan") == "smoke"
                )
            except ValueError:
                ok = False
        else:
            ok = False
    if ok:
        # bump/set/show/stats mutate and report the same trace
        rc, out = _run(
            [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "bump"]
        )
        ok = rc == 0
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "set",
                    "--step",
                    "smoke-step",
                    "--kv",
                    "mood=green",
                ]
            )
            ok = rc == 0
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "show"]
            )
            try:
                # show filters to known keys; --kv lands on the raw file
                trace = json.loads(out).get("trace", {})
                raw = json.loads(trace_file.read_text(encoding="utf-8"))
                ok = (
                    rc == 0
                    and trace.get("current_step") == "smoke-step"
                    and trace.get("attempt_count") == 1
                    and raw.get("mood") == "green"
                )
            except (ValueError, AttributeError, OSError):
                ok = False
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trace.py"), "--file", str(trace_file), "stats"]
            )
            try:
                stats = json.loads(out)
                ok = (
                    rc == 0
                    and stats.get("attempt_count") == 1
                    and stats.get("history") == 2
                    and stats.get("exists") is True
                )
            except (ValueError, AttributeError):
                ok = False
        if ok:
            # notes --limit 1 tails the note list
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--limit",
                    "1",
                ]
            )
            ok = rc == 0 and "second note" in out
        if ok:
            # notes filter flags: --grep matches text, --reverse flips order,
            # --field digs one field, --uniq dedupes, --since/--before bound ts
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--grep",
                    "second",
                ]
            )
            ok = (
                rc == 0
                and "second note" in out
                and "smoke note" not in out
            )
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--before",
                    "1",
                ]
            )
            ok = (
                rc == 0
                and "second note" not in out
                and "smoke note" not in out
            )
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--since",
                    "999999999999",
                ]
            )
            ok = (
                rc == 0
                and "second note" not in out
                and "smoke note" not in out
            )
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--reverse",
                    "--json",
                ]
            )
            try:
                texts = [n.get("text", "") for n in json.loads(out)]
                ok = rc == 0 and texts and texts[0] == texts[-1] if len(texts) == 1 else (
                    rc == 0 and texts[0] != texts[-1]
                )
            except (ValueError, AttributeError, IndexError):
                ok = False
        if ok:
            # --field prints one field per note; --uniq dedupes by sha/text
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--field",
                    "text",
                ]
            )
            ok = rc == 0 and "second note" in out
        if ok:
            dup_file = tmp / "trace-dup.json"
            _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(dup_file),
                    "init",
                    "--plan",
                    "p",
                ]
            )
            _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(dup_file),
                    "record",
                    "--pick",
                    "a",
                    "--note",
                    "same",
                ]
            )
            _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(dup_file),
                    "record",
                    "--pick",
                    "b",
                    "--note",
                    "same",
                ]
            )
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(dup_file),
                    "notes",
                    "--uniq",
                    "--json",
                ]
            )
            try:
                ok = rc == 0 and len(json.loads(out)) == 1
            except (ValueError, TypeError):
                ok = False
        if ok:
            # history --watch emits {picks} ticks; --verdict writes the probe
            verdict = tmp / "trace-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(verdict),
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"picks"' in ln]
            ok = rc == 0 and len(ticks) == 2
            try:
                ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "picks"
            except (OSError, ValueError):
                ok = False
        if ok:
            # history filters: --limit tails, --reverse flips, --grep
            # matches the pick name, --field digs one field
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--limit",
                    "1",
                ]
            )
            ok = rc == 0 and "smoke-pick-2" in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--grep",
                    "pick-2",
                ]
            )
            ok = (
                rc == 0
                and "smoke-pick-2" in out
                and "smoke-pick\n" not in out
            )
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--before",
                    "1",
                ]
            )
            ok = rc == 0 and "smoke-pick" not in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--field",
                    "pick",
                ]
            )
            ok = rc == 0 and "smoke-pick" in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--reverse",
                    "--json",
                ]
            )
            try:
                picks = [p.get("pick", "") for p in json.loads(out)]
                ok = (
                    rc == 0
                    and picks
                    and picks[0] == "smoke-pick-2"
                )
            except (ValueError, AttributeError, IndexError):
                ok = False
        if ok:
            # stats --watch emits {exists,attempt_count} ticks + verdict probe
            verdict = tmp / "trace-stats-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "stats",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(verdict),
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"exists"' in ln]
            ok = rc == 0 and len(ticks) == 2
            try:
                ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "exists"
            except (OSError, ValueError):
                ok = False
        if ok:
            # notes --watch emits {notes} ticks + verdict probe
            verdict = tmp / "trace-notes-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "notes",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(verdict),
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"notes"' in ln]
            ok = rc == 0 and len(ticks) == 2
            try:
                ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "notes"
            except (OSError, ValueError):
                ok = False
    if ok:
        # state --watch emits {ts,state,attempt_count,history,inspected}
        # ticks; --verdict writes {verdict: ok|empty} per tick
        stv = tmp / "trace-state-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "state",
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(stv),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"inspected"' in ln]
        ok = rc == 0 and len(ticks) == 2
        try:
            ok = ok and json.loads(stv.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "ok"
        except (OSError, ValueError):
            ok = False
        if ok:
            # an empty/missing trace reports verdict "empty"
            ev = tmp / "trace-state-verdict-empty.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(tmp / ".jev-trace-none.json"),
                    "state",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "1",
                    "--verdict",
                    str(ev),
                ]
            )
            try:
                ok = json.loads(ev.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "empty"
            except (OSError, ValueError):
                ok = False
    if ok:
        # init variants: --step seeds current_step, JEV_TRACE_PLAN fills
        # the plan, and neither given exits 2
        t2 = tmp / ".jev-trace-init2.json"
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(t2),
                "init",
                "--plan",
                "p2",
                "--step",
                "build",
            ]
        )
        try:
            ok = rc == 0 and (
                json.loads(t2.read_text(encoding="utf-8")).get(
                    "current_step"
                )
                == "build"
            )
        except (OSError, ValueError):
            ok = False
        if ok:
            t3 = tmp / ".jev-trace-init3.json"
            env2 = dict(os.environ)  # skillscan:allow
            env2["JEV_TRACE_PLAN"] = "env plan"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(t3),
                    "init",
                ],
                env=env2,
            )
            try:
                ok = rc == 0 and (
                    json.loads(t3.read_text(encoding="utf-8")).get("plan")
                    == "env plan"
                )
            except (OSError, ValueError):
                ok = False
        if ok:
            env2.pop("JEV_TRACE_PLAN", None)
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(tmp / ".jev-trace-noplan.json"),
                    "init",
                ],
                env=env2,
            )
            ok = rc == 2
    if ok:
        # set --kv lands on the raw file; export surfaces it while the
        # filtered `show` view does not
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "set",
                "--kv",
                "smoke_extra=1",
            ]
        )
        ok = rc == 0
        if ok:
            try:
                ok = (
                    json.loads(trace_file.read_text(encoding="utf-8")).get(
                        "smoke_extra"
                    )
                    == "1"
                )
            except (OSError, ValueError):
                ok = False
        if ok:
            # the loaded view filters unknown keys: export drops it,
            # show --key prints an empty line
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "export",
                ]
            )
            ok = rc == 0 and "smoke_extra" not in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "export",
                    "--jq",
                    "nope.key",
                ]
            )
            ok = rc == 2
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "show",
                    "--key",
                    "attempt_count",
                ]
            )
            ok = rc == 0 and out.strip().isdigit()
        if ok:
            export_file = tmp / "trace-export.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "export",
                    "--out",
                    str(export_file),
                ]
            )
            try:
                ok = rc == 0 and isinstance(
                    json.loads(export_file.read_text(encoding="utf-8")),
                    dict,
                )
            except (OSError, ValueError):
                ok = False
    if ok:
        # prune --dry-run reports without deleting; the real call removes
        os.utime(trace_file, (time.time() - 4000,) * 2)
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "prune",
                "--older-than",
                "60",
                "--dry-run",
            ]
        )
        try:
            ok = rc == 0 and trace_file.exists()
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # prune removes a trace file whose mtime is older than the TTL
        os.utime(trace_file, (time.time() - 4000,) * 2)
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "prune",
                "--older-than",
                "60",
            ]
        )
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and payload.get("removed") is True
                and not trace_file.exists()
            )
        except ValueError:
            ok = False
    if ok:
        # set --kv/--attempt/--error/--unknown write fields; show --key
        # prints one value, show --pretty prints text lines; record --kind
        # tags the history entry
        _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "init",
                "--plan",
                "smoke2",
            ]
        )
        rc, out = _run(
            [
                str(SCRIPTS / "trace.py"),
                "--file",
                str(trace_file),
                "set",
                "--kv",
                "smoke_k=smoke_v",
                "--attempt",
                "3",
                "--error",
                "smoke-err",
                "--unknown",
                "smoke-unknown",
            ]
        )
        # --kv fields ride the emitted trace blob but load() whitelists
        # the schema — assert they appear in the set response, not on disk
        try:
            ok = rc == 0 and json.loads(out).get("trace", {}).get(
                "smoke_k"
            ) == "smoke_v"
        except (ValueError, AttributeError):
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "show",
                    "--key",
                    "last_error",
                ]
            )
            ok = rc == 0 and "smoke-err" in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "show",
                    "--pretty",
                ]
            )
            ok = rc == 0 and "smoke-err" in out and "3" in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "record",
                    "--pick",
                    "smoke-pick2",
                    "--kind",
                    "smoke-kind",
                ]
            )
            ok = rc == 0
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--json",
                ]
            )
            try:
                ok = rc == 0 and any(
                    h.get("kind") == "smoke-kind"
                    and "smoke-pick2" in str(h.get("pick", ""))
                    for h in json.loads(out)
                )
            except (ValueError, AttributeError, TypeError):
                ok = False
        if ok:
            # suggest --dry-run emits the ask request without Jev/record
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "suggest",
                    "--dry-run",
                ]
            )
            try:
                req = json.loads(out)
                criteria = (
                    req.get("questions", {})
                    .get("next_move", {})
                    .get("criteria", {})
                )
                ok = rc == 0 and "return_to_plan" in criteria
            except (ValueError, AttributeError, TypeError):
                ok = False
        if ok:
            # --pick records the choice into history without calling Jev
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "suggest",
                    "--pick",
                    "ask_human",
                    "--kind",
                    "smoke-suggest",
                    "--jq",
                    "pick",
                ]
            )
            ok = rc == 0 and '"ask_human"' in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trace.py"),
                    "--file",
                    str(trace_file),
                    "history",
                    "--json",
                ]
            )
            try:
                ok = rc == 0 and any(
                    h.get("kind") == "smoke-suggest"
                    and h.get("pick") == "ask_human"
                    for h in json.loads(out)
                )
            except (ValueError, AttributeError, TypeError):
                ok = False
        if ok:
            # full loop: stub Jev picks the first non-'none' criterion
            sug_env = dict(os.environ)  # skillscan:allow
            sug_env["TYPESAFE_API_KEY"] = "smoke-stub-key"
            sug_env["JEV_CONSULT_LOG"] = "0"
            sug_server = http.server.HTTPServer(
                ("127.0.0.1", 0), _stub_handler()
            )
            _stub_policy(tmp, "trace-suggest", sug_server.server_port, sug_env)
            try:
                threading.Thread(
                    target=sug_server.handle_request, daemon=True
                ).start()
                rc, out = _run(
                    [
                        str(SCRIPTS / "trace.py"),
                        "--file",
                        str(trace_file),
                        "suggest",
                        "--ask-file",
                        str(tmp / "suggest-ask.json"),
                        "--jq",
                        "pick",
                    ],
                    env=sug_env,
                )
                ok = rc == 0 and '"return_to_plan"' in out
            finally:
                sug_server.server_close()
    return _step("trace", ok, "rc=%d" % rc if ok else out.strip()[:160])


def step_skill_lint(tmp: Path) -> dict:
    skill = SKILL_DIR / "SKILL.md"
    rc, out = _run([str(SCRIPTS / "skill_lint.py"), str(skill)])
    ok = rc == 0
    if ok:
        # --json emits a findings payload; the shipped SKILL.md is clean
        rc, out = _run([str(SCRIPTS / "skill_lint.py"), str(skill), "--json"])
        try:
            ok = rc == 0 and json.loads(out).get("findings") == []
        except ValueError:
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "skill_lint.py"), str(skill), "--explain", "S001"]
        )
        ok = rc == 0 and "S001" in out
    if ok:
        # --strict/--severity floors stay green on the shipped skill
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--strict",
                "--severity",
                "info",
            ]
        )
        ok = rc == 0
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe;
        # --out appends the payload file
        verdict = tmp / "slint-verdict.json"
        out_file = tmp / "slint-out.log"
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
                "--out",
                str(out_file),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and len(ticks) == 2 and out_file.is_file()
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "pass"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        bad_dir = tmp / "bad-skill"
        bad_dir.mkdir(exist_ok=True)
        (bad_dir / "SKILL.md").write_text(
            "no frontmatter here\n", encoding="utf-8"
        )
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(bad_dir / "SKILL.md"),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    if ok:
        # --fix rewrites a mismatched frontmatter name to the dir name
        fix_dir = tmp / "fix-skill"
        fix_dir.mkdir(exist_ok=True)
        fix_md = fix_dir / "SKILL.md"
        fix_md.write_text(
            "---\nname: Other Name\ndescription: a test skill\n---\nbody\n",
            encoding="utf-8",
        )
        rc, out = _run(
            [str(SCRIPTS / "skill_lint.py"), str(fix_md), "--fix"]
        )
        ok = rc == 0 and "name: fix-skill" in fix_md.read_text(
            encoding="utf-8"
        )
        if ok:
            # after the fix the file lints clean
            rc, out = _run(
                [str(SCRIPTS / "skill_lint.py"), str(fix_md), "--json"]
            )
            try:
                ok = rc == 0 and json.loads(out).get("findings") == []
            except ValueError:
                ok = False
    if ok:
        # --out writes the findings payload to a file; --watch-max stops
        # a slow watch by elapsed time before --max-ticks would
        out_file = tmp / "slint-oneshot-out.json"
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--json",
                "--out",
                str(out_file),
            ]
        )
        ok = rc == 0 and out_file.is_file()
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "skill_lint.py"),
                    str(skill),
                    "--watch",
                    "0.2",
                    "--watch-max",
                    "0.05",
                    "--max-ticks",
                    "20",
                ]
            )
            ticks = [
                ln for ln in out.splitlines() if '"findings"' in ln
            ]
            ok = rc == 0 and len(ticks) == 1
    if ok:
        # --jq FIELD inside --watch prints bare field values per tick;
        # --help prints the usage header
        rc, out = _run(
            [
                str(SCRIPTS / "skill_lint.py"),
                str(skill),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--jq",
                "errors",
            ]
        )
        lines = [ln for ln in out.splitlines() if ln.strip().isdigit()]
        ok = rc == 0 and len(lines) == 2 and '"findings"' not in out
        if ok:
            rc, out = _run([str(SCRIPTS / "skill_lint.py"), "--help"])
            ok = rc == 0 and "Usage:" in out
    return _step("skill_lint", ok, out.strip()[:160] or "rc=%d" % rc)


def step_question_lint(tmp: Path) -> dict:
    req = tmp / "req.json"
    req.write_text(
        json.dumps(
            {
                "questions": {
                    "q": {
                        "type": "noul",
                        "instructions": "Should the coder proceed with the plan?",
                        "criteria": {"true": "plan is sound", "false": "plan is risky"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rc, out = _run([str(SCRIPTS / "question_lint.py"), str(req)])
    ok = rc == 0 and "lint:" in out
    if ok:
        # --json emits a findings array; the good request is clean
        rc, out = _run(
            [str(SCRIPTS / "question_lint.py"), str(req), "--json"]
        )
        try:
            ok = rc == 0 and json.loads(out).get("findings") == []
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "question_lint.py"), "--explain", "J001"]
        )
        ok = rc == 0 and "J001" in out
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe
        verdict = tmp / "qlint-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(req),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and len(ticks) == 2
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "pass"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(req),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        bad_req = tmp / "req-bad.json"
        bad_req.write_text(
            json.dumps(
                {
                    "questions": {
                        "q": {
                            "type": "noul",
                            "instructions": "Go?",
                            "criteria": {"true": "x", "false": "x"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(bad_req),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    if ok:
        # --fix rewrites identical noul criteria in place (J014); after the
        # fix the request re-lints without errors
        rc, out = _run(
            [str(SCRIPTS / "question_lint.py"), str(bad_req), "--fix"]
        )
        try:
            fixed_req = json.loads(bad_req.read_text(encoding="utf-8"))
            ok = (
                rc == 0
                and "fixed J014" in out
                and fixed_req["questions"]["q"]["criteria"]["false"]
                != fixed_req["questions"]["q"]["criteria"]["true"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            ok = False
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "question_lint.py"), str(bad_req), "--json"]
            )
            try:
                ok = rc == 0 and all(
                    f.get("rule") != "J014"
                    for f in json.loads(out).get("findings", [])
                )
            except (ValueError, AttributeError):
                ok = False
    if ok:
        # --severity error filters the post-fix warns out; --strict exits 1
        # on them; --out writes the findings payload to a file
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(bad_req),
                "--json",
                "--severity",
                "error",
            ]
        )
        try:
            ok = rc == 0 and json.loads(out).get("findings") == []
        except ValueError:
            ok = False
        if ok:
            rc, _ = _run(
                [str(SCRIPTS / "question_lint.py"), str(bad_req), "--strict"]
            )
            ok = rc == 1
        if ok:
            qout = tmp / "qlint-out.json"
            rc, _ = _run(
                [
                    str(SCRIPTS / "question_lint.py"),
                    str(bad_req),
                    "--json",
                    "--out",
                    str(qout),
                ]
            )
            try:
                ok = (
                    rc == 0
                    and isinstance(
                        json.loads(qout.read_text(encoding="utf-8")), dict
                    )
                )
            except (OSError, ValueError):
                ok = False
    if ok:
        # --watch-max bounds the loop: a watch interval longer than the
        # budget still emits the first tick, then stops
        rc, out = _run(
            [
                str(SCRIPTS / "question_lint.py"),
                str(req),
                "--watch",
                "0.2",
                "--watch-max",
                "0.05",
                "--max-ticks",
                "20",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and len(ticks) == 1
    return _step("question_lint", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_apply_fill(tmp: Path) -> dict:
    env = dict(os.environ)  # skillscan:allow
    env["JEV_CONSULT_LOG"] = "0"
    env.pop("TYPESAFE_API_KEY", None)
    env["USERPROFILE"] = str(tmp / "home")
    env["HOME"] = str(tmp / "home")
    rc, out = _run(
        [
            str(SCRIPTS / "apply_fill.py"),
            "--task",
            "jwt",
            "--harness",
            "claude-code",
            "--cwd",
            str(tmp / "cwd"),
            "--dry-run",
        ],
        env=env,
    )
    ok = rc == 0 and "human" in out
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--status",
                "--cwd",
                str(tmp / "cwd"),
            ],
            env=env,
        )
        if rc == 0:
            try:
                report = json.loads(out)
                ok = "miss" in report and "ask" in report
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # --json emits {"outcome": ...} objects instead of bare tokens
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--task",
                "jwt",
                "--harness",
                "claude-code",
                "--cwd",
                str(tmp / "cwd"),
                "--dry-run",
                "--json",
            ],
            env=env,
        )
        if rc == 0:
            try:
                ok = json.loads(out.strip().splitlines()[0]).get("outcome") == "human"
            except (ValueError, IndexError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # --status --jq digs one dotted field; a bad key exits 2
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--status",
                "--cwd",
                str(tmp / "cwd"),
                "--jq",
                "miss",
            ],
            env=env,
        )
        ok = rc == 0 and out.strip() in ("true", "false")
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "apply_fill.py"),
                    "--status",
                    "--cwd",
                    str(tmp / "cwd"),
                    "--jq",
                    "nope.nope",
                ],
                env=env,
            )
            ok = rc == 2
    if ok:
        # --watch re-scans for miss/ask markers each tick; --verdict
        # writes the slim {verdict: clean|pending} probe
        verdict = tmp / "apply-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--cwd",
                str(tmp / "cwd"),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ],
            env=env,
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2
        try:
            ok = ok and "verdict" in json.loads(
                verdict.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps clean ticks off stdout (stderr still logs them)
        clean_cwd = tmp / "apply-clean-cwd"
        clean_cwd.mkdir(parents=True, exist_ok=True)
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--cwd",
                str(clean_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--quiet",
            ],
            env=env,
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first tick with a pending miss
        miss_cwd = tmp / "apply-miss-cwd"
        miss_cwd.mkdir(parents=True, exist_ok=True)
        (miss_cwd / ".jev-tools-miss.json").write_text(
            json.dumps({"task": "smoke", "written_at": time.time()}),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--cwd",
                str(miss_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "5",
                "--fail-fast",
            ],
            env=env,
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    if ok:
        # one-shot --verdict reports clean on a fresh cwd and pending on
        # a cwd carrying a miss marker
        clean_v = tmp / "apply-clean-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "apply_fill.py"),
                "--cwd",
                str(tmp / "apply-clean-cwd"),
                "--verdict",
                str(clean_v),
            ],
            env=env,
        )
        try:
            ok = (
                rc == 0
                and json.loads(clean_v.read_text(encoding="utf-8")).get(
                    "verdict"
                )
                == "clean"
            )
        except (OSError, ValueError):
            ok = False
        if ok:
            miss_v = tmp / "apply-miss-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "apply_fill.py"),
                    "--cwd",
                    str(tmp / "apply-miss-cwd"),
                    "--verdict",
                    str(miss_v),
                ],
                env=env,
            )
            try:
                ok = (
                    rc == 0
                    and json.loads(miss_v.read_text(encoding="utf-8")).get(
                        "verdict"
                    )
                    == "pending"
                )
            except (OSError, ValueError):
                ok = False
    if ok:
        # end-to-end fill path: a fake `hermes` on PATH serves
        # plugins search / mcp catalog / plugins install, and a
        # localhost stub answers the Jev pick ask.
        bin_dir = tmp / "apply-bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "hermes.bat").write_text(
            '@echo off\r\n'
            'if "%1"=="plugins" if "%2"=="search" echo '
            '[{"name":"smoke-thing","description":"smoke thing"}]\r\n'
            'exit /b 0\r\n',
            encoding="utf-8",
        )
        fill_cwd = tmp / "apply-fill-cwd"
        fill_cwd.mkdir(parents=True, exist_ok=True)
        env2 = dict(env)  # skillscan:allow
        env2["PATH"] = str(bin_dir) + os.pathsep + env2.get("PATH", "")
        env2["TYPESAFE_API_KEY"] = "smoke-stub-key"
        env2["JEV_CONSULT_LOG"] = str(tmp / "apply-decisions.jsonl")

        server = http.server.HTTPServer(

            ("127.0.0.1", 0), _stub_handler()

        )

        try:

            _stub_policy(

                tmp, "apply-policy.json", server.server_address[1], env2,

            )
            threading.Thread(
                target=server.handle_request, daemon=True
            ).start()
            rc, out = _run(
                [
                    str(SCRIPTS / "apply_fill.py"),
                    "--task",
                    "smoke",
                    "--harness",
                    "hermes",
                    "--cwd",
                    str(fill_cwd),
                    "--home",
                    str(tmp / "home"),
                    "--ask-file",
                    str(tmp / "apply-ask.json"),
                ],
                cwd=fill_cwd,
                env=env2,
            )
            ok = rc == 0 and "installed plugin smoke-thing" in out
            if ok:
                # the sidecar records the pick; --pick + --dry-run
                # short-circuits to a would_install line
                try:
                    ok = bool(
                        json.loads(
                            (fill_cwd / ".jev-tools.json").read_text(
                                encoding="utf-8"
                            )
                        )
                    )
                except (OSError, ValueError):
                    ok = False
                dry_cwd = tmp / "apply-dry-cwd"
                dry_cwd.mkdir(parents=True, exist_ok=True)
                rc, out = _run(
                    [
                        str(SCRIPTS / "apply_fill.py"),
                        "--task",
                        "smoke",
                        "--harness",
                        "hermes",
                        "--cwd",
                        str(dry_cwd),
                        "--pick",
                        "plugin:smoke-thing",
                        "--dry-run",
                    ],
                    cwd=dry_cwd,
                    env=env2,
                )
                ok = (
                    ok
                    and rc == 0
                    and "would_install plugin smoke-thing" in out
                )
            if ok:
                # --from-miss loop: the hook's miss marker carries
                # task+harness; the fill installs via the fake hermes
                miss_cwd = tmp / "apply-miss-cwd"
                miss_cwd.mkdir(parents=True, exist_ok=True)
                (miss_cwd / ".jev-tools-miss.json").write_text(
                    json.dumps(
                        {
                            "task": "smoke",
                            "harness": "hermes",
                            "written_at": int(time.time()),
                            "empty": True,
                        }
                    ),
                    encoding="utf-8",
                )
                threading.Thread(
                    target=server.handle_request, daemon=True
                ).start()
                rc, out = _run(
                    [
                        str(SCRIPTS / "apply_fill.py"),
                        "--from-miss",
                        "--cwd",
                        str(miss_cwd),
                        "--home",
                        str(tmp / "home"),
                        "--ask-file",
                        str(tmp / "apply-miss-ask.json"),
                    ],
                    cwd=miss_cwd,
                    env=env2,
                )
                ok = (
                    rc == 0
                    and "installed plugin smoke-thing" in out
                    and not (
                        miss_cwd / ".jev-tools-miss.json"
                    ).is_file()
                )
        finally:
            server.server_close()
    return _step("apply_fill", ok, out.strip()[:120] or "rc=%d" % rc)


def step_hook(tmp: Path) -> dict:
    env = dict(os.environ)  # skillscan:allow
    env["JEV_CONSULT_LOG"] = "0"
    env.pop("TYPESAFE_API_KEY", None)
    env["USERPROFILE"] = str(tmp / "home")
    env["HOME"] = str(tmp / "home")
    cwd = tmp / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "smoke test task",
            "cwd": str(cwd),
        }
    )
    winner_detail = None
    rc, out = _run(
        [str(SCRIPTS / "inventory_hook.py")], cwd=tmp, env=env, inp=payload
    )
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out.strip().splitlines()[0]), dict)
        except (ValueError, IndexError):
            ok = False
    if ok:
        # no Jev key + prompt tokens => the hook records a miss marker
        miss = cwd / ".jev-tools-miss.json"
        ok = miss.is_file()
        if ok:
            try:
                marker = json.loads(miss.read_text(encoding="utf-8"))
                ok = (
                    marker.get("task") == "smoke test task"
                    and isinstance(marker.get("written_at"), int)
                    and marker.get("empty") is True
                )
            except ValueError:
                ok = False
    if ok:
        # --dry-run resolves but must not write sidecar or miss files
        dry_cwd = tmp / "hook-dry-cwd"
        dry_cwd.mkdir(parents=True, exist_ok=True)
        dry_payload = json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "smoke test task",
                "cwd": str(dry_cwd),
            }
        )
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--dry-run"],
            cwd=dry_cwd,
            env=env,
            inp=dry_payload,
        )
        ok = (
            rc == 0
            and not (dry_cwd / ".jev-tools-miss.json").exists()
            and not (dry_cwd / ".jev-tools.json").exists()
        )
    if ok:
        # repeat of the same prompt over a fresh sidecar hits the dedupe path
        env["JEV_HOOK_DEBUG"] = "1"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py")], cwd=tmp, env=env, inp=payload
        )
        ok = rc == 0 and "dedupe=True" in out
    if ok:
        # --verbose explains a {} emit on stderr
        env.pop("JEV_HOOK_DEBUG", None)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps({"hook_event_name": "PreToolUse", "prompt": "x"}),
        )
        ok = rc == 0 and "verbose:" in out
    if ok:
        # same prompt over an expired sidecar hits the stale_match path;
        # --json echoes the whole LAST_DECISION record to stderr
        sidecar = json.loads((cwd / ".jev-tools.json").read_text(encoding="utf-8"))
        sidecar["written_at"] = 1  # ancient -> stale
        (cwd / ".jev-tools.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
        env.pop("JEV_HOOK_DEBUG", None)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=payload,
        )
        ok = rc == 0 and '"stale_sidecar": true' in out
    if ok:
        # emit shapes: claude-code -> hookSpecificOutput, grok -> {}
        for harness, want in (("claude-code", '"hookSpecificOutput"'), ("grok", None)):
            env["JEV_HOOK_HARNESS"] = harness
            rc, out = _run(
                [str(SCRIPTS / "inventory_hook.py")],
                cwd=tmp,
                env=env,
                inp=json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "prompt": "emit shape check %s" % harness,
                        "cwd": str(cwd),
                    }
                ),
            )
            if rc != 0:
                ok = False
                break
            first = out.strip().splitlines()[0] if out.strip() else ""
            if want is None:
                ok = first == "{}"
            else:
                ok = want in first
            if not ok:
                break
    if ok:
        # --events --json emits the allowed event names as an array
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--events", "--json"],
            cwd=tmp,
            env=env,
        )
        if rc == 0:
            try:
                ok = "UserPromptSubmit" in json.loads(out.strip().splitlines()[0])
            except (ValueError, TypeError, IndexError):
                ok = False
        else:
            ok = False
    if ok:
        # --env reports the resolved config (presence flags, never secrets)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--env"], cwd=tmp, env=env
        )
        if rc == 0:
            try:
                report = json.loads(out.strip())
                ok = "events" in report and "limit" in report
            except (ValueError, AttributeError):
                ok = False
        else:
            ok = False
    if ok:
        # JEV_HOOK_MAX_PROMPT truncates the prompt; --json reports it
        env.pop("JEV_HOOK_HARNESS", None)
        env["JEV_HOOK_MAX_PROMPT"] = "10"
        cwd2 = tmp / "cwd2"
        cwd2.mkdir(exist_ok=True)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "a prompt much longer than ten characters",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_MAX_PROMPT", None)
        ok = rc == 0 and '"prompt_truncated": true' in out
    if ok:
        # JEV_HOOK_OFF emits {} and --verbose says why
        env["JEV_HOOK_OFF"] = "1"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "kill switch smoke",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_OFF", None)
        ok = rc == 0 and "{}" in out and "disabled" in out
    if ok:
        # JEV_HOOK_SKIP_EVENTS removes the event from the allowed set
        env["JEV_HOOK_SKIP_EVENTS"] = "UserPromptSubmit"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "skipped event smoke",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_SKIP_EVENTS", None)
        ok = rc == 0 and "{}" in out
    if ok:
        # JEV_HOOK_EVENTS replaces the allowed set outright
        env["JEV_HOOK_EVENTS"] = "PreToolUse"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py")],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "restricted event smoke",
                    "cwd": str(cwd2),
                }
            ),
        )
        env.pop("JEV_HOOK_EVENTS", None)
        ok = rc == 0 and "{}" in out
    if ok:
        # a payload timestamp older than JEV_HOOK_MAX_AGE is dropped
        env["JEV_HOOK_MAX_AGE"] = "60"
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--verbose"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "stale prompt smoke",
                    "cwd": str(cwd2),
                    "timestamp": 1000000,
                }
            ),
        )
        env.pop("JEV_HOOK_MAX_AGE", None)
        ok = rc == 0 and "{}" in out
    if ok:
        # an empty prompt emits {} (nothing to consult on)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py")],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "",
                    "cwd": str(cwd2),
                }
            ),
        )
        ok = rc == 0 and out.strip().splitlines()[0] == "{}"
    if ok:
        # $name in the prompt is an explicit pick — no Jev call needed
        skill_dir = tmp / "hermes" / "skills" / "explicit-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: explicit-skill\n---\n", encoding="utf-8"
        )
        env["HERMES_HOME"] = str(tmp / "hermes")
        cwd3 = tmp / "cwd3"
        cwd3.mkdir(exist_ok=True)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "please apply $explicit-skill to this task",
                    "cwd": str(cwd3),
                }
            ),
        )
        env.pop("HERMES_HOME", None)
        ok = (
            rc == 0
            and '"explicit": true' in out
            and '"jev_status": "winner"' in out
        )
    if ok:
        # JEV_HOOK_WINNER forces the pick by name (question=env)
        env["HERMES_HOME"] = str(tmp / "hermes")
        env["JEV_HOOK_WINNER"] = "explicit-skill"
        cwd4 = tmp / "cwd4"
        cwd4.mkdir(exist_ok=True)
        rc, out = _run(
            [str(SCRIPTS / "inventory_hook.py"), "--json"],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "an unrelated prompt",
                    "cwd": str(cwd4),
                }
            ),
        )
        env.pop("JEV_HOOK_WINNER", None)
        env.pop("HERMES_HOME", None)
        ok = (
            rc == 0
            and '"jev_status": "winner"' in out
            and '"question": "env"' in out
        )
        if ok:
            winner_detail = "explicit-skill"
    if ok:
        # fail-open: with a key set but Jev unreachable the hook still emits {}
        bad_policy = tmp / "bad-policy.json"
        try:
            pdata = json.loads((SKILL_DIR / "policy.json").read_text(encoding="utf-8"))
            pdata["endpoint"] = "http://127.0.0.1:1/v1/systemone"
            bad_policy.write_text(json.dumps(pdata), encoding="utf-8")
        except (OSError, ValueError):
            ok = False
        if ok:
            env5 = dict(env)
            env5["JEV_POLICY"] = str(bad_policy)
            env5["TYPESAFE_API_KEY"] = "smoke-dummy"
            env5["JEV_HOOK_RETRIES"] = "0"
            rc, out = _run(
                [str(SCRIPTS / "inventory_hook.py")], cwd=tmp, env=env5, inp=payload
            )
            try:
                ok = rc == 0 and isinstance(
                    json.loads(out.strip().splitlines()[0]), dict
                )
            except (ValueError, IndexError):
                ok = False
    if ok:
        # --debug echoes LAST_DECISION to stderr; --out persists the payload;
        # --verdict writes the slim {verdict,...} probe
        out_path = tmp / "hook-emit.json"
        verdict_path = tmp / "hook-verdict.json"
        cwd5 = tmp / "cwd5"
        cwd5.mkdir(exist_ok=True)
        rc, out = _run(
            [
                str(SCRIPTS / "inventory_hook.py"),
                "--debug",
                "--out",
                str(out_path),
                "--verdict",
                str(verdict_path),
            ],
            cwd=tmp,
            env=env,
            inp=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "debug out verdict smoke",
                    "cwd": str(cwd5),
                }
            ),
        )
        ok = (
            rc == 0
            and "jev_status=" in out
            and out_path.is_file()
            and verdict_path.is_file()
        )
        try:
            emitted = json.loads(out_path.read_text(encoding="utf-8"))
            verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
            ok = ok and isinstance(emitted, dict) and "verdict" in verdict
        except (OSError, ValueError):
            ok = False
    if ok:
        # --watch S --max-ticks N emits N tick lines and stops
        event_file = tmp / "hook-event.json"
        event_file.write_text(payload, encoding="utf-8")
        rc, out = _run(
            [
                str(SCRIPTS / "inventory_hook.py"),
                "--file",
                str(event_file),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
            ],
            cwd=tmp,
            env=env,
        )
        # exits 1 when no tick produced a winner
        ticks = [ln for ln in out.splitlines() if '"winner_changed"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2 and 'watch tick=2' in out
    if ok:
        # --fail-fast stops the watch on the first winnerless tick
        rc, out = _run(
            [
                str(SCRIPTS / "inventory_hook.py"),
                "--file",
                str(event_file),
                "--watch",
                "0.05",
                "--max-ticks",
                "5",
                "--fail-fast",
            ],
            cwd=tmp,
            env=env,
        )
        ticks = [ln for ln in out.splitlines() if '"winner_changed"' in ln]
        ok = len(ticks) == 1 and 'watch tick=2' not in out
    if ok:
        # --verdict writes {verdict,ticks,winner,keys} — per tick in watch
        # mode, one-shot otherwise; the miss is a "fail" verdict
        verdict = tmp / "hook-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "inventory_hook.py"),
                "--file",
                str(event_file),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--watch-max",
                "30",
                "--verdict",
                str(verdict),
            ],
            cwd=tmp,
            env=env,
        )
        try:
            ok = (
                rc == 1
                and json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                )
                == "fail"
            )
        except (OSError, ValueError):
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "inventory_hook.py"),
                    "--file",
                    str(event_file),
                    "--verdict",
                    str(verdict),
                ],
                cwd=tmp,
                env=env,
            )
            try:
                ok = (
                    rc == 0
                    and json.loads(verdict.read_text(encoding="utf-8")).get(
                        "verdict"
                    )
                    == "fail"
                )
            except (OSError, ValueError):
                ok = False
    if ok:
        # --jq inside --watch prints just the named tick field(s)
        rc, out = _run(
            [
                str(SCRIPTS / "inventory_hook.py"),
                "--file",
                str(event_file),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--jq",
                "keys",
            ],
            cwd=tmp,
            env=env,
        )
        lines = [
            ln.strip()
            for ln in out.splitlines()
            if ln.strip().startswith("[")
        ]
        ok = len(lines) == 2 and '"winner_changed"' not in out
    if ok:
        # end-to-end pick path: an installed skill is shortlisted for
        # the prompt, a localhost stub answers the Jev ask, and the
        # hook emits the pick note + writes the sidecar.
        pick_home = tmp / "pick-home"
        pick_cwd = tmp / "pick-cwd"
        pick_cwd.mkdir(parents=True, exist_ok=True)
        sk = pick_home / ".codex" / "skills" / "smoke-thing"
        sk.mkdir(parents=True)
        (sk / "SKILL.md").write_text(
            "---\nname: smoke-thing\ndescription: picked skill\n---\n",
            encoding="utf-8",
        )
        env3 = dict(env)  # skillscan:allow
        env3["USERPROFILE"] = str(pick_home)
        env3["HOME"] = str(pick_home)
        env3["TYPESAFE_API_KEY"] = "smoke-stub-key"
        env3["JEV_HOOK_HARNESS"] = "codex"

        server = http.server.HTTPServer(

            ("127.0.0.1", 0), _stub_handler()

        )

        try:

            _stub_policy(

                tmp, "hook-policy.json", server.server_address[1], env3,

            )
            threading.Thread(
                target=server.handle_request, daemon=True
            ).start()
            pick_payload = json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": "smoke",
                    "cwd": str(pick_cwd),
                }
            )
            rc, out = _run(
                [str(SCRIPTS / "inventory_hook.py")],
                cwd=pick_cwd,
                env=env3,
                inp=pick_payload,
            )
            ok = False
            if rc == 0:
                try:
                    emitted = json.loads(out.strip().splitlines()[0])
                    context = (
                        emitted.get("hookSpecificOutput") or {}
                    ).get("additionalContext") or ""
                    ok = "smoke-thing" in context
                except (ValueError, IndexError):
                    ok = False
            if ok:
                ok = (pick_cwd / ".jev-tools.json").is_file()
        finally:
            server.server_close()
    detail = out.strip()[:120] or "rc=%d" % rc
    if winner_detail:
        detail = "winner=%s %s" % (winner_detail, detail)
    return _step("hook", ok, detail)


def step_install(tmp: Path) -> dict:
    repo_root = SCRIPTS.parent.parent.parent
    env = dict(os.environ)  # skillscan:allow
    home = tmp / "install-home"
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    rc, out = _run(
        [
            str(repo_root / "scripts" / "install.py"),
            "--dry-run",
            "--agents",
            "codex",
        ],
        env=env,
    )
    # dry-run plans into the redirected home and writes nothing there
    writes = [p for p in home.rglob("*") if p.is_file()]
    ok = rc == 0 and "skill ->" in out and not writes
    if ok:
        # --check-key reports set/missing and never echoes a value
        env.pop("TYPESAFE_API_KEY", None)
        rc, out = _run(
            [str(repo_root / "scripts" / "install.py"), "--check-key"],
            env=env,
        )
        ok = (
            rc == 0
            and out.strip().startswith("TYPESAFE_API_KEY:")
            and "apikey_" not in out
        )
    if ok:
        # --uninstall --dry-run plans removals into the redirected home and
        # writes nothing there either
        rc, out = _run(
            [
                str(repo_root / "scripts" / "install.py"),
                "--dry-run",
                "--uninstall",
                "--agents",
                "codex",
            ],
            env=env,
        )
        writes = [p for p in home.rglob("*") if p.is_file()]
        ok = (
            rc == 0
            and ("remove" in out or "missing" in out)
            and not writes
        )
    if ok:
        # a real install into the redirected home writes the skill tree,
        # instructions and the codex hooks file — never the API key
        rc, out = _run(
            [str(repo_root / "scripts" / "install.py"), "--agents", "codex"],
            env=env,
        )
        skill_md = home / ".codex" / "skills" / "jev-consult" / "SKILL.md"
        agents_md = home / ".codex" / "AGENTS.md"
        hooks = home / ".codex" / "hooks.json"
        try:
            ok = (
                rc == 0
                and skill_md.is_file()
                and agents_md.is_file()
                and hooks.is_file()
                and "jev" in hooks.read_text(encoding="utf-8")
                and "apikey_" not in out
            )
        except OSError:
            ok = False
        if ok:
            # --uninstall removes what it wrote
            rc, out = _run(
                [
                    str(repo_root / "scripts" / "install.py"),
                    "--uninstall",
                    "--agents",
                    "codex",
                ],
                env=env,
            )
            ok = rc == 0 and not skill_md.exists()
    return _step("install", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_compare(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "compare.py"), "--strict"])
    ok = rc == 0 and "after_jev" in out
    if ok:
        # --json emits the machine-readable payload with per-case rows
        rc, out = _run([str(SCRIPTS / "compare.py"), "--json"])
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and isinstance(payload, dict)
                and payload.get("rows")
                and all("after" in c and "before" in c for c in payload["rows"])
            )
        except (ValueError, AttributeError, TypeError):
            ok = False
    if ok:
        # --only runs a single case id
        case_id = payload["rows"][0].get("id", "")
        rc, out = _run(
            [str(SCRIPTS / "compare.py"), "--json", "--only", case_id]
        )
        try:
            ok = rc == 0 and len(json.loads(out).get("rows", [])) == 1
        except ValueError:
            ok = False
    if ok:
        # --md prints a markdown table, --verdict writes the slim probe
        verdict = tmp / "compare-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--md",
                "--verdict",
                str(verdict),
            ]
        )
        ok = rc == 0 and "|" in out
        try:
            ok = ok and "verdict" in json.loads(verdict.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ok = False
    if ok:
        # a bad --jq key exits 2
        rc, out = _run([str(SCRIPTS / "compare.py"), "--json", "--jq", "nope"])
        ok = rc == 2
    if ok:
        # --out writes the result JSON to PATH; --report writes markdown
        out_file = tmp / "compare-out.json"
        report = tmp / "compare-report.md"
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--json",
                "--out",
                str(out_file),
                "--report",
                str(report),
            ]
        )
        try:
            ok = (
                rc == 0
                and isinstance(
                    json.loads(out_file.read_text(encoding="utf-8")), dict
                )
                and len(report.read_text(encoding="utf-8").strip()) > 0
            )
        except (OSError, ValueError):
            ok = False
    if ok:
        # --watch emits {ts,cases,failures} ticks; --quiet keeps the
        # clean ones off stdout while stderr still logs them
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"failures"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast breaks the watch on the first tick with failures;
        # the run exits 1 since the last tick failed
        bad_cases = tmp / "compare-bad.json"
        bad_cases.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "bad1",
                            "defect": "x",
                            "prompt": "p",
                            "after": {"called_jev": False},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--cases",
                str(bad_cases),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ok = rc == 1 and "watch tick=2" not in out
    if ok:
        # --failing prints only strict-gate failures; --jq digs one field
        mix_cases = tmp / "compare-mix.json"
        mix_cases.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "ok1",
                            "defect": "x",
                            "prompt": "p",
                            "after": {
                                "called_jev": True,
                                "last_pick": "a",
                                "step": "s",
                            },
                        },
                        {
                            "id": "bad1",
                            "defect": "x",
                            "prompt": "p",
                            "after": {"called_jev": False},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--cases",
                str(mix_cases),
                "--failing",
            ]
        )
        ok = rc == 0 and "bad1" in out and "ok1" not in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "compare.py"),
                    "--cases",
                    str(mix_cases),
                    "--json",
                    "--jq",
                    "live",
                ]
            )
            ok = rc == 0 and out.strip() == "false"
    if ok:
        # --live scores each side through the stub Jev endpoint —
        # every noul answers 0.99, so both sides show live nouls
        env2 = dict(os.environ)  # skillscan:allow
        env2["TYPESAFE_API_KEY"] = "smoke-stub-key"

        server = http.server.HTTPServer(

            ("127.0.0.1", 0), _stub_handler()

        )

        try:

            _stub_policy(

                tmp, "cmp-policy.json", server.server_address[1], env2,

            )
            # one case scores two sides — serve both POSTs
            for _ in range(2):
                threading.Thread(
                    target=server.handle_request, daemon=True
                ).start()
            rc, out = _run(
                [
                    str(SCRIPTS / "compare.py"),
                    "--cases",
                    str(SKILL_DIR / "examples" / "compare-cases.json"),
                    "--only",
                    "off_track",
                    "--live",
                    "--json",
                    "--strict",
                ],
                env=env2,
            )
            ok = rc == 0 and '"noul": 0.99' in out
            if ok:
                # the stub reports proceed on every side
                try:
                    payload = json.loads(out)
                    row = (payload.get("rows") or [{}])[0]
                    ok = row.get("live") is not False and "noul" in (
                        row.get("after") or {}
                    )
                except (ValueError, IndexError):
                    ok = False
        finally:
            server.server_close()
    if ok:
        # --baseline writes the rows; --diff reports them unchanged
        base_file = tmp / "compare-baseline.json"
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--baseline",
                str(base_file),
                "--json",
            ]
        )
        ok = rc == 0 and base_file.is_file()
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--diff",
                str(base_file),
                "--json",
            ]
        )
        try:
            diff = json.loads(out).get("diff") or {}
            ok = (
                rc == 0
                and diff.get("unchanged") == 4
                and not diff.get("regressions")
                and not diff.get("added")
                and not diff.get("removed")
            )
        except ValueError:
            ok = False
    if ok:
        # a baseline where a case still called Jev makes the current
        # failing fixture read as a strict_failure regression
        cases_file = tmp / "smoke-cases.json"
        cases_file.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "id": "reg1",
                            "defect": "smoke",
                            "score": "on_track",
                            "prompt": "p",
                            "after": {"called_jev": False},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        base_file.write_text(
            json.dumps(
                {
                    "ts": 1,
                    "rows": [
                        {
                            "id": "reg1",
                            "after": {"called_jev": True},
                            "before": {"called_jev": True},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "compare.py"),
                "--cases",
                str(cases_file),
                "--diff",
                str(base_file),
                "--json",
                "--strict",
            ]
        )
        # merged stdout+stderr: --strict failures and regression lines
        # land on stderr, the JSON payload on stdout
        ok = (
            rc == 1
            and '"why": "strict_failure"' in out
            and '"id": "reg1"' in out
            and "strict: reg1: regressed vs baseline" in out
        )
    return _step("compare", ok, out.strip().splitlines()[-1][:120] if out.strip() else "rc=%d" % rc)


def step_doctor(tmp: Path) -> dict:
    rc, out = _run(
        [
            str(SCRIPTS / "doctor.py"),
            "--agents",
            "hermes,claude-code,codex,grok",
            "--home",
            str(tmp / "home"),
            "--hermes-home",
            str(tmp / "hermes"),
        ]
    )
    try:
        payload = json.loads(out)
        agents_seen = {
            c.get("agent") for c in payload.get("checks", []) if c.get("agent")
        }
        ok_json = isinstance(payload, dict) and {
            "hermes",
            "claude-code",
            "codex",
            "grok",
        } <= agents_seen
    except ValueError:
        ok_json = False
    ok = ok_json and rc in (0, 1)
    if ok:
        # --agents filters the checks to just the named harnesses
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
            ]
        )
        try:
            payload = json.loads(out)
            agents_seen = {
                c.get("agent")
                for c in payload.get("checks", [])
                if c.get("agent") and c.get("agent") != "*"
            }
            ok = rc in (0, 1) and agents_seen == {"codex"}
        except ValueError:
            ok = False
    if ok:
        # --jq digs one field; an empty-home run reports ok=false
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
                "--jq",
                "ok",
            ]
        )
        ok = rc == 0 and out.strip() == "false"
    if ok:
        # --watch emits {checks,failed,ok} ticks; --verdict writes the probe
        verdict = tmp / "doctor-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"failed"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "fail"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --fail-fast stops the watch on the first failing tick
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"failed"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    if ok:
        # --report writes a markdown probe alongside the JSON stdout
        report = tmp / "doctor-report.md"
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
                "--report",
                str(report),
            ]
        )
        try:
            ok = rc in (0, 1) and len(
                report.read_text(encoding="utf-8").strip()
            ) > 0
        except OSError:
            ok = False
    if ok:
        # --out writes the checks payload; --quiet still prints failing
        # watch ticks (it only suppresses clean ones)
        dout = tmp / "doctor-out.json"
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
                "--out",
                str(dout),
            ]
        )
        try:
            ok = rc in (0, 1) and isinstance(
                json.loads(dout.read_text(encoding="utf-8")), dict
            )
        except (OSError, ValueError):
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "doctor.py"),
                    "--agents",
                    "codex",
                    "--home",
                    str(tmp / "home"),
                    "--hermes-home",
                    str(tmp / "hermes"),
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--quiet",
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"failed"' in ln]
            ok = len(ticks) == 2
    if ok:
        # a bad --jq key exits 2; --watch-max bounds the loop to one tick
        rc, out = _run(
            [
                str(SCRIPTS / "doctor.py"),
                "--agents",
                "codex",
                "--home",
                str(tmp / "home"),
                "--hermes-home",
                str(tmp / "hermes"),
                "--jq",
                "nope.nope",
            ]
        )
        ok = rc == 2
        if ok:
            # --watch-max bounds elapsed: tick N+1 fires after the sleep,
            # sees the deadline passed, and stops — 2 ticks total here
            rc, out = _run(
                [
                    str(SCRIPTS / "doctor.py"),
                    "--agents",
                    "codex",
                    "--home",
                    str(tmp / "home"),
                    "--hermes-home",
                    str(tmp / "hermes"),
                    "--watch",
                    "0.2",
                    "--watch-max",
                    "0.05",
                    "--max-ticks",
                    "20",
                ]
            )
            ticks = [ln for ln in out.splitlines() if '"failed"' in ln]
            ok = len(ticks) == 2
        if ok:
            # --agents accepts a comma list; both harnesses get checks
            rc, out = _run(
                [
                    str(SCRIPTS / "doctor.py"),
                    "--agents",
                    "codex,grok",
                    "--home",
                    str(tmp / "home"),
                    "--hermes-home",
                    str(tmp / "hermes"),
                ]
            )
            try:
                checks = json.loads(out).get("checks", [])
                agents = {c.get("agent") for c in checks}
                ok = rc in (0, 1) and "codex" in agents and "grok" in agents
            except (ValueError, AttributeError, TypeError):
                ok = False
        if ok:
            # green path: a seeded .claude install (skill + settings.json
            # hooks wired to both scripts) passes every claude-code check
            home2 = tmp / "doctor-home"
            skill_dir = home2 / ".claude" / "skills" / "jev-consult"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: jev-consult\ndescription: x\n---\n",
                encoding="utf-8",
            )
            (home2 / ".claude" / "settings.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PostToolUse": [
                                {
                                    "hooks": [
                                        {"command": "python compact_hook.py"}
                                    ]
                                }
                            ],
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "command": "python inventory_hook.py"
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            rc, out = _run(
                [
                    str(SCRIPTS / "doctor.py"),
                    "--agents",
                    "claude-code",
                    "--home",
                    str(home2),
                    "--hermes-home",
                    str(tmp / "hermes"),
                ]
            )
            try:
                checks = [
                    c
                    for c in json.loads(out).get("checks", [])
                    if c.get("agent") == "claude-code"
                ]
                ok = (
                    rc in (0, 1)
                    and checks
                    and all(c.get("ok") for c in checks)
                )
            except (ValueError, AttributeError, TypeError):
                ok = False
    return _step("doctor_json", ok, "rc=%d" % rc)


def step_trigger_lint(tmp: Path) -> dict:
    cases = SCRIPTS.parent.parent.parent / "tests" / "fixtures" / "jev-consult.trigger-cases.json"
    if not cases.is_file():
        return _step("trigger_lint", False, "fixture missing: %s" % cases)
    rc, out = _run([str(SCRIPTS / "trigger_lint.py"), str(cases)])
    ok = rc == 0
    if ok:
        # --json emits a findings payload; the shipped fixture is clean
        rc, out = _run(
            [str(SCRIPTS / "trigger_lint.py"), str(cases), "--json"]
        )
        try:
            ok = rc == 0 and json.loads(out).get("findings") == []
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # --explain RULE prints one rule's description
        rc, out = _run(
            [str(SCRIPTS / "trigger_lint.py"), "--explain", "T001"]
        )
        ok = rc == 0 and "T001" in out
    if ok:
        # --watch emits {findings,errors} ticks; --verdict writes the probe
        verdict = tmp / "tlint-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(cases),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and len(ticks) == 2
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "pass"
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps passing ticks off stdout (stderr still logs them)
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(cases),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = rc == 0 and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first erroring tick
        bad_cases = tmp / "cases-bad.json"
        bad_cases.write_text(
            json.dumps({"skill": "x", "cases": [{"id": "c1"}]}),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(bad_cases),
                "--watch",
                "0.03",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"findings"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    if ok:
        # --fix --dry-run reports T005/T007 fixes without writing;
        # --fix renames the bad id and dedupes covers
        fix_cases = tmp / "cases-fix.json"
        fix_cases.write_text(
            json.dumps(
                {
                    "skill": "x",
                    "cases": [
                        {
                            "id": "Bad ID!",
                            "prompt": "prompt text here",
                            "should_trigger": True,
                        },
                        {
                            "id": "neg-ok",
                            "prompt": "prompt text here",
                            "should_trigger": False,
                            "covers": ["a", "a", 1],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(fix_cases),
                "--fix",
                "--dry-run",
            ]
        )
        try:
            ok = (
                rc in (0, 1)
                and "would fix T005" in out
                and json.loads(fix_cases.read_text(encoding="utf-8"))["cases"][0][
                    "id"
                ]
                == "Bad ID!"
            )
        except (OSError, ValueError, KeyError, IndexError):
            ok = False
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_lint.py"), str(fix_cases), "--fix"]
            )
            try:
                fixed = json.loads(fix_cases.read_text(encoding="utf-8"))
                ok = (
                    rc in (0, 1)
                    and "fixed T005" in out
                    and fixed["cases"][0]["id"] == "pos-bad-id"
                    and fixed["cases"][1]["covers"] == ["a"]
                )
            except (OSError, ValueError, KeyError, IndexError):
                ok = False
    if ok:
        # --severity error drops warn/info findings; --explain RULE prints
        # one rule's description; --out writes the payload file;
        # --watch-max bounds a slow watch before --max-ticks fires
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_lint.py"),
                str(fix_cases),
                "--json",
                "--severity",
                "error",
            ]
        )
        try:
            sev_payload = json.loads(out)
            ok = rc in (0, 1) and isinstance(
                sev_payload.get("findings", sev_payload), list
            )
        except (ValueError, AttributeError):
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_lint.py"),
                    str(fix_cases),
                    "--explain",
                    "T005",
                ]
            )
            ok = rc == 0 and "T005" in out
        if ok:
            tout = tmp / "tlint-out.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_lint.py"),
                    str(fix_cases),
                    "--json",
                    "--out",
                    str(tout),
                ]
            )
            ok = rc in (0, 1) and tout.is_file()
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_lint.py"),
                    str(cases),
                    "--watch",
                    "0.2",
                    "--watch-max",
                    "0.05",
                    "--max-ticks",
                    "20",
                ]
            )
            ticks = [
                ln for ln in out.splitlines() if '"findings"' in ln
            ]
            ok = len(ticks) == 1
    return _step("trigger_lint", ok, out.strip()[:120] or "rc=%d" % rc)


def step_trigger_eval(tmp: Path) -> dict:
    rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--quiet"])
    ok = rc == 0 and "PASS" in out
    if ok:
        # --json emits the scored-case payload
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--json"])
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and payload.get("ok") is True
                and isinstance(payload.get("cases"), list)
                and payload["cases"]
            )
        except ValueError:
            ok = False
    if ok:
        # --coverage reports the positive-prompt hit ratio
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--coverage"])
        ok = rc == 0 and "coverage:" in out
    if ok:
        # --score rates an ad-hoc prompt against the description
        rc, out = _run(
            [str(SCRIPTS / "trigger_eval.py"), "--score", "refactor this function"]
        )
        ok = rc == 0 and "score=" in out
    if ok:
        # a bad --jq key exits 2
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--json", "--jq", "nope"])
        ok = rc == 2
    if ok:
        # --watch emits {verdict,ok,coverage} ticks; --verdict writes the
        # slim eval probe
        verdict = tmp / "teval-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "trigger_eval.py"),
                "--watch",
                "0.03",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"verdict"' in ln]
        ok = rc == 0 and len(ticks) == 2
        try:
            ok = ok and json.loads(verdict.read_text(encoding="utf-8")).get(
                "verdict"
            ) == "PASS"
        except (OSError, ValueError):
            ok = False
    if ok:
        # row-shaping flags: --ids lists case ids, --id filters to one,
        # --csv emits comma rows, --summary collapses to the stats line
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--ids"])
        ids = [ln.strip() for ln in out.splitlines() if ln.strip()]
        ok = rc == 0 and len(ids) >= 2
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--id", ids[0], "--quiet"]
            )
            ok = rc == 0 and "PASS" in out
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--csv"])
            ok = rc == 0 and any(
                ln.split(",")[0] == ids[0] for ln in out.splitlines()
            )
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--summary"])
            ok = rc == 0 and "PASS" in out
        if ok:
            # --top N keeps only the N weakest rows
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--top", "2", "--ids"]
            )
            ok = rc == 0 and len(
                [ln for ln in out.splitlines() if ln.strip()]
            ) == 2
        if ok:
            # --min-coverage gates the hit rate: 0 passes, >1 fails
            rc, _ = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--min-coverage", "0"]
            )
            ok = rc == 0
            rc, _ = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--min-coverage", "1.1"]
            )
            ok = ok and rc == 1
        if ok:
            rc, _ = _run([str(SCRIPTS / "trigger_eval.py"), "--uncovered"])
            ok = rc == 0
    if ok:
        # coverage/reporting flags: --covers counts per tag, --covers-map
        # lists tag->ids, --dist prints the score histogram, --tokens shows
        # matched tokens per row, --prompts lists id: prompt lines
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--covers"])
        ok = rc == 0 and len(out.strip().splitlines()) >= 1
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--covers-map"]
            )
            ok = rc == 0 and len(out.strip().splitlines()) >= 1
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--dist"])
            ok = rc == 0 and "-" in out
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--tokens"])
            ok = rc == 0 and "tokens=" in out
        if ok:
            rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--prompts"])
            ok = rc == 0 and ":" in out
    if ok:
        # gate/sort flags: --min-covers 1 passes (every tag has a case),
        # --min-covers 3 fails; --sort orders weakest-first; --desc swaps
        # the description the prompts are scored against
        rc, _ = _run([str(SCRIPTS / "trigger_eval.py"), "--min-covers", "1"])
        ok = rc == 0
        rc, _ = _run([str(SCRIPTS / "trigger_eval.py"), "--min-covers", "3"])
        ok = ok and rc == 1
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--sort", "--ids"]
            )
            ok = rc == 0 and len(
                [ln for ln in out.splitlines() if ln.strip()]
            ) >= 2
        if ok:
            rc, out = _run(
                [str(SCRIPTS / "trigger_eval.py"), "--desc", "refactor code"]
            )
            ok = rc in (0, 1) and "margin:" in out
    if ok:
        # --fail prints only failing rows (none on the shipped fixture);
        # --min-score F keeps only rows scoring >= F
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--fail"])
        ok = rc == 0 and "PASS" in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_eval.py"),
                    "--min-score",
                    "1.0",
                    "--ids",
                ]
            )
            ok = rc == 0 and all(
                ln.strip().startswith("pos-")
                for ln in out.splitlines()
                if ln.strip()
            )
    if ok:
        # --env dumps the resolved config; --jq digs one field of it
        rc, out = _run([str(SCRIPTS / "trigger_eval.py"), "--env"])
        try:
            envp = json.loads(out)
            ok = rc == 0 and "margin" in envp and envp["cases_exists"]
        except (ValueError, KeyError):
            ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_eval.py"),
                    "--env",
                    "--jq",
                    "margin",
                ]
            )
            try:
                ok = rc == 0 and isinstance(json.loads(out), float)
            except ValueError:
                ok = False
    if ok:
        # --desc-tokens prints the scored token set; --unmatched adds
        # per-row missed tokens; --margin overrides the verdict factor;
        # --skill re-points at another skill dir
        rc, out = _run(
            [str(SCRIPTS / "trigger_eval.py"), "--desc-tokens"]
        )
        ok = rc == 0 and len(out.strip()) > 0
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_eval.py"),
                    "--json",
                    "--unmatched",
                ]
            )
            try:
                misses = json.loads(out)
                ok = rc == 0 and isinstance(misses, dict) and any(
                    isinstance(v, list) and v for v in misses.values()
                )
            except (ValueError, AttributeError, TypeError):
                ok = False
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "trigger_eval.py"),
                    "--margin",
                    "1.0",
                    "--json",
                ]
            )
            try:
                ok = rc in (0, 1) and json.loads(out).get("margin") == 1.0
            except (ValueError, AttributeError):
                ok = False
        if ok:
            rc, _ = _run(
                [
                    str(SCRIPTS / "trigger_eval.py"),
                    "--skill",
                    str(SCRIPTS.parent),
                    "--quiet",
                ]
            )
            ok = rc == 0
    return _step("trigger_eval", ok, out.strip()[:120] or "rc=%d" % rc)


def step_compact_hook(tmp: Path) -> dict:
    env = dict(os.environ)  # skillscan:allow
    env.pop("TYPESAFE_API_KEY", None)
    env["USERPROFILE"] = str(tmp / "home")
    env["HOME"] = str(tmp / "home")
    spill = tmp / "spill"
    env["JEV_CONSULT_SPILL"] = str(spill)
    payload = json.dumps(
        {"hook_event_name": "PostToolUse", "tool_result": "x" * 90000}
    )
    rc, out = _run([str(SCRIPTS / "compact_hook.py")], cwd=tmp, env=env, inp=payload)
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out.strip().splitlines()[0]), dict)
        except (ValueError, IndexError):
            ok = False
    if ok:
        # lossless spill: the full 90k result lands on disk while the
        # emitted tool output is abridged
        try:
            files = [
                f for f in spill.iterdir() if f.name != "index.jsonl"
            ]
            ok = files and max(f.stat().st_size for f in files) >= 90000
        except OSError:
            ok = False
    if ok:
        try:
            emitted = json.loads(out.strip().splitlines()[0])
            abridged = emitted.get("hookSpecificOutput", {}).get(
                "updatedToolOutput", ""
            )
            ok = 0 < len(abridged) < 90000
        except (ValueError, AttributeError):
            ok = False
    if ok:
        # compact --list-spill reports the spilled payloads (path/size/mtime)
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                "--list-spill",
                "--spill-dir",
                str(spill),
                "--json",
            ]
        )
        try:
            payload = json.loads(out)
            ok = (
                rc == 0
                and payload.get("count") == len(files)
                and all(f.get("size", 0) > 0 for f in payload.get("files", []))
            )
        except ValueError:
            ok = False
    if ok:
        # --verify-spill resolves the emitted "full output saved: P …]"
        # marker against the spill dir; --orphan-spill lists files no
        # emitted doc references
        doc = tmp / "emitted.txt"
        doc.write_text(
            "head\n[… 9000 chars omitted; full output saved: %s …]\ntail\n"
            % files[0],
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                "--verify-spill",
                str(doc),
                "--spill-dir",
                str(spill),
            ]
        )
        ok = rc == 0 and "0 missing" in out
        if ok:
            orphan = spill / "orphan.txt"
            orphan.write_text("unreferenced", encoding="utf-8")
            rc, out = _run(
                [
                    str(SCRIPTS / "compact.py"),
                    "--orphan-spill",
                    str(doc),
                    "--spill-dir",
                    str(spill),
                ]
            )
            ok = rc == 0 and "orphan.txt" in out
            try:
                orphan.unlink()
            except OSError:
                pass
    if ok:
        # --prune-spill unlinks spill files older than the TTL
        stale = spill / "stale.txt"
        stale.write_text("old payload", encoding="utf-8")
        os.utime(stale, (time.time() - 4000,) * 2)
        rc, out = _run(
            [
                str(SCRIPTS / "compact.py"),
                "--prune-spill",
                "60",
                "--spill-dir",
                str(spill),
            ]
        )
        # stale.txt is gone while the spill index ledger survives pruning
        ok = (
            rc == 0
            and not stale.is_file()
            and (spill / "index.jsonl").is_file()
        )
    if ok:
        # fail-open stdin variants: empty, invalid JSON, non-dict, a small
        # result under the live-fat threshold, and a truncated event all
        # emit {} rc 0
        variants = [
            "",
            "not json {",
            "[1, 2]",
            json.dumps({"hook_event_name": "PostToolUse", "tool_result": "tiny"}),
            json.dumps(
                {
                    "hook_event_name": "PostToolUse",
                    "toolResultTruncated": True,
                    "tool_result": "x" * 90000,
                }
            ),
        ]
        for variant in variants:
            rc, out = _run(
                [str(SCRIPTS / "compact_hook.py")], cwd=tmp, env=env, inp=variant
            )
            if rc != 0 or out.strip() != "{}":
                ok = False
                break
    return _step("compact_hook", ok, out.strip()[:120] or "rc=%d" % rc)


def step_jev_decide(tmp: Path) -> dict:
    answers = tmp / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "answers": {
                    "where": {
                        "type": "choice",
                        "choice": "refactor",
                        "confidence": 0.9,
                        "probabilities": {"refactor": 0.9, "rewrite": 0.1},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rc, out = _run(
        [
            str(SCRIPTS / "jev.py"),
            "decide",
            str(answers),
            "--jq",
            "decision.action",
        ]
    )
    ok = rc == 0 and out.strip() == '"proceed"'
    if ok:
        verdict = tmp / "decide-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(answers),
                "--verdict",
                str(verdict),
            ]
        )
        try:
            payload = json.loads(verdict.read_text(encoding="utf-8"))
            ok = rc == 0 and payload.get("verdict") == "proceed"
        except (OSError, ValueError):
            ok = False
    if ok:
        # tight top-two gap on an irreversible question escalates
        tight = tmp / "answers-tight.json"
        tight.write_text(
            json.dumps(
                {
                    "irreversible": True,
                    "answers": {
                        "where": {
                            "type": "choice",
                            "choice": "refactor",
                            "confidence": 0.9,
                            "probabilities": {
                                "refactor": 0.53,
                                "rewrite": 0.47,
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(tight),
                "--jq",
                "decision.action",
            ]
        )
        ok = rc == 2 and out.strip() == '"escalate"'
    if ok:
        # --irreversible flag escalates a tight gap even without the
        # payload field; --out persists the decision payload; a bad
        # --jq key exits 2
        tight_flag = tmp / "answers-tight-flag.json"
        tight_flag.write_text(
            json.dumps(
                {
                    "answers": {
                        "where": {
                            "type": "choice",
                            "choice": "refactor",
                            "confidence": 0.9,
                            "probabilities": {
                                "refactor": 0.53,
                                "rewrite": 0.47,
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(tight_flag),
                "--irreversible",
                "--jq",
                "decision.action",
            ]
        )
        ok = rc == 2 and out.strip() == '"escalate"'
    if ok:
        out_file = tmp / "decide-out.json"
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(answers),
                "--out",
                str(out_file),
            ]
        )
        try:
            payload = json.loads(out_file.read_text(encoding="utf-8"))
            ok = rc == 0 and "decision" in payload
        except (OSError, ValueError):
            ok = False
    if ok:
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                str(answers),
                "--jq",
                "nope.nope",
            ]
        )
        ok = rc == 2
    if ok:
        # file '-' reads answers from stdin; an escalating --verdict
        # writes the escalate probe even though the exit code is 2
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "decide",
                "-",
                "--jq",
                "decision.action",
            ],
            inp=answers.read_text(encoding="utf-8"),
        )
        ok = rc == 0 and out.strip() == '"proceed"'
        if ok:
            esc_v = tmp / "decide-escalate-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "jev.py"),
                    "decide",
                    str(tight),
                    "--verdict",
                    str(esc_v),
                ]
            )
            try:
                ok = (
                    rc == 2
                    and json.loads(esc_v.read_text(encoding="utf-8")).get(
                        "verdict"
                    )
                    == "escalate"
                )
            except (OSError, ValueError):
                ok = False
    return _step("jev_decide", ok, out.strip()[:120] or "rc=%d" % rc)


def step_ask_verdict(tmp: Path) -> dict:
    """jev.py ask against a one-shot localhost stub (never the real API)."""
    body = json.dumps(
        {
            "answers": {
                "q": {
                    "type": "choice",
                    "choice": "a",
                    "confidence": 0.9,
                    "probabilities": {"a": 0.9, "b": 0.1},
                }
            }
        }
    ).encode("utf-8")

    class Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            # answer the request's own question names so response
            # validation passes for both ask (choice q) and ping (noul ok)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, TypeError):
                request = {}
            questions = request.get("questions") or {}
            if "ok" in questions:
                reply = json.dumps(
                    {
                        "answers": {
                            "ok": {"type": "noul", "noul": 0.99}
                        },
                        "model": "smoke-stub",
                    }
                ).encode("utf-8")
            else:
                reply = body
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        policy = json.loads((SKILL_DIR / "policy.json").read_text(encoding="utf-8"))
        policy["endpoint"] = "http://127.0.0.1:%d/v1/systemone" % port
        policy_path = tmp / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        request = tmp / "ask.json"
        request.write_text(
            json.dumps(
                {
                    "state": "smoke test",
                    "questions": {
                        "q": {
                            "type": "choice",
                            "instructions": "pick one",
                            "criteria": {"a": "first", "b": "second"},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)  # skillscan:allow
        env["JEV_POLICY"] = str(policy_path)
        env["TYPESAFE_API_KEY"] = "smoke-stub-key"
        verdict = tmp / "verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "jev.py"),
                "ask",
                str(request),
                "--verdict",
                str(verdict),
            ],
            env=env,
        )
        ok = False
        if rc == 0 and verdict.is_file():
            try:
                ok = json.loads(verdict.read_text(encoding="utf-8")).get(
                    "verdict"
                ) == "proceed"
            except ValueError:
                ok = False
        if ok:
            # ping --json hits the same stub: {ok, model, noul, ms}
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            rc, out = _run(
                [str(SCRIPTS / "jev.py"), "ping", "--json"], env=env
            )
            if rc == 0:
                try:
                    ok = json.loads(out.strip()).get("ok") is True
                except ValueError:
                    ok = False
            else:
                ok = False
        if ok:
            # ask --jq digs one response field; --out persists the payload
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            out_file = tmp / "ask-out.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "jev.py"),
                    "ask",
                    str(request),
                    "--jq",
                    "answers.q.choice",
                    "--out",
                    str(out_file),
                ],
                env=env,
            )
            ok = rc == 0 and out.strip().splitlines()[0] == '"a"'
            try:
                ok = ok and "answers" in json.loads(
                    out_file.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                ok = False
        if ok:
            # a bad --jq key exits 2 (the stub still serves one more ask)
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            rc, out = _run(
                [
                    str(SCRIPTS / "jev.py"),
                    "ask",
                    str(request),
                    "--jq",
                    "nope.nope",
                ],
                env=env,
            )
            ok = rc == 2
        if ok:
            # ask reads the request from stdin when the file is '-';
            # ping --jq digs one slim-payload field and --verdict writes it
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            rc, out = _run(
                [str(SCRIPTS / "jev.py"), "ask", "-", "--jq", "decision.action"],
                env=env,
                inp=request.read_text(encoding="utf-8"),
            )
            ok = rc == 0 and out.strip() == '"proceed"'
        if ok:
            thread = threading.Thread(
                target=server.handle_request, daemon=True
            )
            thread.start()
            ping_v = tmp / "ping-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "jev.py"),
                    "ping",
                    "--jq",
                    "model",
                    "--verdict",
                    str(ping_v),
                ],
                env=env,
            )
            try:
                ok = (
                    rc == 0
                    and out.strip().startswith('"')
                    and json.loads(ping_v.read_text(encoding="utf-8")).get("ok")
                    is True
                )
            except (OSError, ValueError):
                ok = False
        if ok:
            # ask fails open when the endpoint is unreachable (port 1 is
            # never listening)
            bad_policy = tmp / "policy-down.json"
            policy["endpoint"] = "http://127.0.0.1:1/v1/systemone"
            bad_policy.write_text(json.dumps(policy), encoding="utf-8")
            env["JEV_POLICY"] = str(bad_policy)
            rc, out = _run(
                [str(SCRIPTS / "jev.py"), "ask", str(request)],
                env=env,
            )
            ok = rc != 0 and "network error" in out
        return _step("ask_verdict", ok, out.strip()[:120] or "rc=%d" % rc)
    finally:
        server.server_close()


def step_peer_fill_status(tmp: Path) -> dict:
    rc, out = _run(
        [
            str(SCRIPTS / "peer_fill.py"),
            "--status",
            "--cwd",
            str(tmp / "cwd"),
        ]
    )
    ok = False
    if rc == 0:
        try:
            ok = isinstance(json.loads(out), dict)
        except ValueError:
            ok = False
    if ok:
        # --status --jq digs one field; a bad key exits 2
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--status",
                "--cwd",
                str(tmp / "cwd"),
                "--jq",
                "miss",
            ]
        )
        ok = rc == 0 and out.strip() in ("true", "false")
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "peer_fill.py"),
                    "--status",
                    "--cwd",
                    str(tmp / "cwd"),
                    "--jq",
                    "nope.nope",
                ]
            )
            ok = rc == 2
    if ok:
        # --watch ticks the fill state; --verdict writes the slim probe
        verdict = tmp / "peer-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--cwd",
                str(tmp / "cwd"),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--verdict",
                str(verdict),
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and len(ticks) == 2
        try:
            ok = ok and "verdict" in json.loads(verdict.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ok = False
    if ok:
        # --quiet keeps clean ticks off stdout (stderr still logs them)
        clean_cwd = tmp / "peer-clean-cwd"
        clean_cwd.mkdir(parents=True, exist_ok=True)
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--cwd",
                str(clean_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "2",
                "--quiet",
            ]
        )
        stdout_ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = rc in (0, 1) and not stdout_ticks and "watch tick=2" in out
    if ok:
        # --fail-fast stops the watch on the first tick with a pending miss
        miss_cwd = tmp / "miss-cwd"
        miss_cwd.mkdir(parents=True, exist_ok=True)
        (miss_cwd / ".jev-tools-miss.json").write_text(
            json.dumps({"task": "smoke", "written_at": time.time()}),
            encoding="utf-8",
        )
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--cwd",
                str(miss_cwd),
                "--watch",
                "0.05",
                "--max-ticks",
                "5",
                "--fail-fast",
            ]
        )
        ticks = [ln for ln in out.splitlines() if '"miss"' in ln]
        ok = len(ticks) == 1 and "watch tick=2" not in out
    if ok:
        # one-shot --verdict (no --watch) reports pending|clean
        clean_v = tmp / "peer-clean-verdict.json"
        rc, out = _run(
            [
                str(SCRIPTS / "peer_fill.py"),
                "--cwd",
                str(clean_cwd),
                "--verdict",
                str(clean_v),
            ]
        )
        try:
            ok = (
                rc == 0
                and json.loads(clean_v.read_text(encoding="utf-8")).get(
                    "verdict"
                )
                == "clean"
            )
        except (OSError, ValueError):
            ok = False
        if ok:
            miss_v = tmp / "peer-miss-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "peer_fill.py"),
                    "--cwd",
                    str(miss_cwd),
                    "--verdict",
                    str(miss_v),
                ]
            )
            try:
                ok = (
                    rc == 0
                    and json.loads(miss_v.read_text(encoding="utf-8")).get(
                        "verdict"
                    )
                    == "pending"
                )
            except (OSError, ValueError):
                ok = False
    if ok:
        # end-to-end peer fill: a skill installed under one harness is
        # shortlisted for another, a localhost stub answers the pick
        # ask, and the skill is copied into the dest harness dirs.
        peer_home = tmp / "peer-home"
        peer_cwd = tmp / "peer-cwd"
        peer_cwd.mkdir(parents=True, exist_ok=True)
        src_dir = peer_home / ".claude" / "skills" / "smoke-thing"
        src_dir.mkdir(parents=True)
        (src_dir / "SKILL.md").write_text(
            "---\nname: smoke-thing\ndescription: peer skill\n---\n",
            encoding="utf-8",
        )
        env2 = dict(os.environ)  # skillscan:allow
        env2["TYPESAFE_API_KEY"] = "smoke-stub-key"
        env2["JEV_CONSULT_LOG"] = str(tmp / "peer-decisions.jsonl")

        server = http.server.HTTPServer(

            ("127.0.0.1", 0), _stub_handler()

        )

        try:

            _stub_policy(

                tmp, "peer-policy.json", server.server_address[1], env2,

            )
            threading.Thread(
                target=server.handle_request, daemon=True
            ).start()
            rc, out = _run(
                [
                    str(SCRIPTS / "peer_fill.py"),
                    "--task",
                    "smoke",
                    "--harness",
                    "codex",
                    "--home",
                    str(peer_home),
                    "--cwd",
                    str(peer_cwd),
                    "--ask-file",
                    str(tmp / "peer-ask.json"),
                ],
                cwd=peer_cwd,
                env=env2,
            )
            ok = (
                rc == 0
                and "copied" in out
                and (
                    peer_home
                    / ".codex"
                    / "skills"
                    / "smoke-thing"
                    / "SKILL.md"
                ).is_file()
            )
            if ok:
                # the sidecar records the pick for the dest harness
                try:
                    ok = bool(
                        json.loads(
                            (peer_cwd / ".jev-tools.json").read_text(
                                encoding="utf-8"
                            )
                        )
                    )
                except (OSError, ValueError):
                    ok = False
            if ok:
                # --from-miss loop: a miss marker written by the hook
                # carries task+harness; the fill picks it up and copies
                # the peer skill into the marker's harness (grok)
                miss_cwd2 = tmp / "peer-miss-cwd"
                miss_cwd2.mkdir(parents=True, exist_ok=True)
                (miss_cwd2 / ".jev-tools-miss.json").write_text(
                    json.dumps(
                        {
                            "task": "smoke",
                            "harness": "grok",
                            "written_at": int(time.time()),
                            "empty": True,
                        }
                    ),
                    encoding="utf-8",
                )
                threading.Thread(
                    target=server.handle_request, daemon=True
                ).start()
                rc, out = _run(
                    [
                        str(SCRIPTS / "peer_fill.py"),
                        "--from-miss",
                        "--home",
                        str(peer_home),
                        "--cwd",
                        str(miss_cwd2),
                        "--ask-file",
                        str(tmp / "peer-miss-ask.json"),
                    ],
                    cwd=miss_cwd2,
                    env=env2,
                )
                ok = (
                    rc == 0
                    and "copied" in out
                    and (
                        peer_home
                        / ".grok"
                        / "skills"
                        / "smoke-thing"
                        / "SKILL.md"
                    ).is_file()
                    and not (miss_cwd2 / ".jev-tools-miss.json").is_file()
                )
            if ok:
                # --list prints the peer rows; --show dumps one record;
                # --pick NAME skips Jev and --dry-run emits "dry copied"
                rc, out = _run(
                    [
                        str(SCRIPTS / "peer_fill.py"),
                        "--task",
                        "smoke",
                        "--harness",
                        "hermes",
                        "--home",
                        str(peer_home),
                        "--hermes-home",
                        str(tmp / "peer-hermes"),
                        "--cwd",
                        str(peer_cwd),
                        "--list",
                    ],
                    cwd=peer_cwd,
                    env=env2,
                )
                ok = rc == 0 and "smoke-thing" in out
            if ok:
                rc, out = _run(
                    [
                        str(SCRIPTS / "peer_fill.py"),
                        "--task",
                        "smoke",
                        "--harness",
                        "hermes",
                        "--home",
                        str(peer_home),
                        "--hermes-home",
                        str(tmp / "peer-hermes"),
                        "--cwd",
                        str(peer_cwd),
                        "--show",
                        "smoke-thing",
                    ],
                    cwd=peer_cwd,
                    env=env2,
                )
                ok = rc == 0 and "smoke-thing" in out
            if ok:
                rc, out = _run(
                    [
                        str(SCRIPTS / "peer_fill.py"),
                        "--task",
                        "smoke",
                        "--harness",
                        "hermes",
                        "--home",
                        str(peer_home),
                        "--hermes-home",
                        str(tmp / "peer-hermes"),
                        "--cwd",
                        str(peer_cwd),
                        "--pick",
                        "smoke-thing",
                        "--dry-run",
                    ],
                    cwd=peer_cwd,
                    env=env2,
                )
                ok = rc == 0 and "dry copied" in out
        finally:
            server.server_close()
    return _step("peer_fill_status", ok, out.strip()[:120] or "rc=%d" % rc)


def step_catalog_fill(tmp: Path) -> dict:
    """catalog_fill end-to-end: a fake `hermes` on PATH answers
    search/inspect/install, and a localhost stub answers the pick ask."""
    env = dict(os.environ)  # skillscan:allow
    home = tmp / "cat-home"
    hermes = tmp / "cat-hermes"
    cwd = tmp / "cat-cwd"
    for p in (home, hermes, cwd):
        p.mkdir(parents=True, exist_ok=True)
    # the "install" lands this skill in the hermes catalog (pre-seeded;
    # the fake installer exits 0 without doing anything)
    skill_src = hermes / "skills" / "smoke-thing"
    skill_src.mkdir(parents=True)
    (skill_src / "SKILL.md").write_text(
        "---\nname: smoke-thing\ndescription: smoke catalog hit\n---\n",
        encoding="utf-8",
    )
    bin_dir = tmp / "cat-bin"
    bin_dir.mkdir(parents=True)
    bat = bin_dir / "hermes.bat"
    bat.write_text(
        '@echo off\r\n'
        'if "%1"=="skills" if "%2"=="search" echo '
        '[{"id":"smoke-thing","identifier":"acme/smoke-thing","name":"smoke-thing","description":"smoke hit"}]\r\n'
        'exit /b 0\r\n',
        encoding="utf-8",
    )
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["JEV_CONSULT_LOG"] = str(tmp / "cat-decisions.jsonl")
    env["TYPESAFE_API_KEY"] = "smoke-stub-key"

    server = http.server.HTTPServer(

        ("127.0.0.1", 0), _stub_handler()

    )

    try:

        _stub_policy(

            tmp, "cat-policy.json", server.server_address[1], env,

        )
        thread = threading.Thread(
            target=server.handle_request, daemon=True
        )
        thread.start()
        rc, out = _run(
            [
                str(SCRIPTS / "catalog_fill.py"),
                "--task",
                "smoke",
                "--harness",
                "codex",
                "--home",
                str(home),
                "--hermes-home",
                str(hermes),
                "--cwd",
                str(cwd),
            ],
            cwd=cwd,
            env=env,
        )
        ok = rc == 0 and "installed" in out and "acme/smoke-thing" in out
        if ok:
            # the skill is copied into the harness dirs and the sidecar
            # records the pick
            copied = (home / ".codex" / "skills" / "smoke-thing" / "SKILL.md").is_file()
            sidecar = cwd / ".jev-tools.json"
            try:
                sidecar_ok = bool(
                    json.loads(sidecar.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                sidecar_ok = False
            ok = copied and sidecar_ok
        if ok:
            # --list prints cached hits; --show dumps one record
            rc, out = _run(
                [
                    str(SCRIPTS / "catalog_fill.py"),
                    "--task",
                    "smoke",
                    "--list",
                    "--home",
                    str(home),
                    "--cwd",
                    str(cwd),
                ],
                cwd=cwd,
                env=env,
            )
            ok = rc == 0 and "smoke-thing" in out
        if ok:
            rc, out = _run(
                [
                    str(SCRIPTS / "catalog_fill.py"),
                    "--task",
                    "smoke",
                    "--show",
                    "smoke-thing",
                    "--home",
                    str(home),
                    "--cwd",
                    str(cwd),
                ],
                cwd=cwd,
                env=env,
            )
            ok = rc == 0 and "smoke" in out
        if ok:
            # --watch emits {ts,hits,cached,cache_age_s} ticks; --jq digs
            # one field; --verdict writes the slim probe
            verdict = tmp / "cat-verdict.json"
            rc, out = _run(
                [
                    str(SCRIPTS / "catalog_fill.py"),
                    "--task",
                    "smoke",
                    "--watch",
                    "0.03",
                    "--max-ticks",
                    "2",
                    "--verdict",
                    str(verdict),
                    "--home",
                    str(home),
                    "--cwd",
                    str(cwd),
                ],
                cwd=cwd,
                env=env,
            )
            ticks = [ln for ln in out.splitlines() if '"hits"' in ln]
            ok = rc == 0 and len(ticks) == 2
            try:
                ok = ok and json.loads(
                    verdict.read_text(encoding="utf-8")
                ).get("verdict") in ("hits", "none")
            except (OSError, ValueError, AttributeError):
                ok = False
            if ok:
                rc, out = _run(
                    [
                        str(SCRIPTS / "catalog_fill.py"),
                        "--task",
                        "smoke",
                        "--watch",
                        "0.03",
                        "--max-ticks",
                        "1",
                        "--jq",
                        "hits",
                        "--home",
                        str(home),
                        "--cwd",
                        str(cwd),
                    ],
                    cwd=cwd,
                    env=env,
                )
                lines = out.strip().splitlines()
                ok = (
                    rc == 0
                    and lines
                    and lines[0].strip().lstrip("-").isdigit()
                )
        if ok:
            # --pick --dry-run reports the would-install without running
            # the fake installer
            rc, out = _run(
                [
                    str(SCRIPTS / "catalog_fill.py"),
                    "--task",
                    "smoke",
                    "--pick",
                    "acme/smoke-thing",
                    "--dry-run",
                    "--home",
                    str(home),
                    "--cwd",
                    str(cwd),
                ],
                cwd=cwd,
                env=env,
            )
            ok = rc == 0 and "would" in out.lower()
        if ok:
            # --clear drops the cached catalog hits for the task; a
            # second call reports no_cache
            rc, out = _run(
                [
                    str(SCRIPTS / "catalog_fill.py"),
                    "--task",
                    "smoke",
                    "--clear",
                    "--home",
                    str(home),
                    "--cwd",
                    str(cwd),
                ],
                cwd=cwd,
                env=env,
            )
            ok = rc == 0 and out.strip() in ("cleared", "no_cache")
            if ok and "cleared" in out:
                rc, out = _run(
                    [
                        str(SCRIPTS / "catalog_fill.py"),
                        "--task",
                        "smoke",
                        "--clear",
                        "--home",
                        str(home),
                        "--cwd",
                        str(cwd),
                    ],
                    cwd=cwd,
                    env=env,
                )
                ok = rc == 0 and "no_cache" in out
        return _step("catalog_fill", ok, out.strip()[:120] or "rc=%d" % rc)
    finally:
        server.server_close()


STEPS = (
    ("policy", "step_policy"),
    ("policy_lint", "step_policy_lint"),
    ("jev_scaffold_lint", "step_jev_scaffold_lint"),
    ("inventory", "step_inventory"),
    ("compact_fake", "step_compact_fake"),
    ("decisions", "step_decisions"),
    ("trace", "step_trace"),
    ("skill_lint", "step_skill_lint"),
    ("install", "step_install"),
    ("question_lint", "step_question_lint"),
    ("compare", "step_compare"),
    ("apply_fill", "step_apply_fill"),
    ("hook", "step_hook"),
    ("doctor_json", "step_doctor"),
    ("trigger_lint", "step_trigger_lint"),
    ("trigger_eval", "step_trigger_eval"),
    ("compact_hook", "step_compact_hook"),
    ("jev_decide", "step_jev_decide"),
    ("peer_fill_status", "step_peer_fill_status"),
    ("catalog_fill", "step_catalog_fill"),
    ("ask_verdict", "step_ask_verdict"),
)


def main(argv: list[str] | None = None) -> int:
    if _watch.maybe_version(sys.argv[1:] if argv is None else argv):
        return 0
    parser = argparse.ArgumentParser(description="Offline sanity for the jev-consult pack.")
    parser.add_argument(
        "--only",
        default=os.environ.get("JEV_SMOKE_ONLY", ""),
        help="Comma-separated step names to run (default: all; JEV_SMOKE_ONLY presets).",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first failing step (in --watch mode, stops on the first failing tick).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print step names (for --only) and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --list, emit the step names as a JSON array.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Per-step subprocess timeout (default 60; JEV_SMOKE_TIMEOUT overrides).",
    )
    parser.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="Also write the results JSON to PATH.",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        default="",
        help="Write a markdown step report (verdict + per-step table) to PATH.",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-run the steps every S seconds, printing a {ts,ok,failed} JSON tick.",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--verdict", metavar="PATH", default="", help="Write a slim {verdict, steps, failed} JSON to PATH when finished (in --watch mode, the final pass state).")
    parser.add_argument("--junit", metavar="PATH", default="", help="Write a JUnit XML <testsuite> for the step results to PATH (in --watch mode, the final pass).")
    parser.add_argument(
        "--repeat",
        metavar="N",
        type=int,
        default=0,
        help="Run each step N times; a step fails when any attempt does (default 1; JEV_SMOKE_REPEAT presets).",
    )
    parser.add_argument(
        "--jq",
        metavar="KEY",
        default="",
        help="Print just one dotted-path field of the results payload (e.g. ok); unknown key exits 2.",
    )
    args = parser.parse_args(argv)
    names = {name for name, _ in STEPS}
    if args.list:
        if getattr(args, "json", False):
            sys.stdout.write(json.dumps(sorted(names)) + "\n")
        else:
            for name in sorted(names):
                sys.stdout.write(name + "\n")
        return 0
    repeat = args.repeat
    if repeat <= 0:
        try:
            repeat = int(os.environ.get("JEV_SMOKE_REPEAT", "") or "1")
        except ValueError:
            repeat = 1
    if repeat < 1:
        repeat = 1
    wanted = {s.strip() for s in args.only.split(",") if s.strip()}
    unknown = wanted - names
    if unknown:
        sys.stderr.write(
            "unknown step(s): %s (valid: %s)\n" % (", ".join(sorted(unknown)), ", ".join(sorted(names)))
        )
        return 2
    global STEP_TIMEOUT
    if args.timeout > 0:
        STEP_TIMEOUT = float(args.timeout)
    else:
        try:
            env_timeout = float(os.environ.get("JEV_SMOKE_TIMEOUT", "") or "0")
        except ValueError:
            env_timeout = 0.0
        if env_timeout > 0:
            STEP_TIMEOUT = env_timeout
    def _run_steps() -> list[dict]:
        rows: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp_raw:
            tmp = Path(tmp_raw)
            for name, fn_name in STEPS:
                if wanted and name not in wanted:
                    continue
                fn = globals()[fn_name]
                for attempt in range(repeat):
                    try:
                        row = fn(tmp)
                    except Exception as exc:  # a crash is a failed step, not a crash
                        row = _step(getattr(fn, "__name__", "step"), False, "raised %r" % exc)
                    if not row["ok"] and repeat > 1:
                        row = dict(row)
                        row["detail"] = "attempt %d/%d: %s" % (
                            attempt + 1, repeat, row["detail"]
                        )
                    if not row["ok"]:
                        break
                rows.append(row)
                if args.fail_fast and not rows[-1]["ok"]:
                    break
        return rows

    def _write_verdict(steps_now: list[dict], elapsed_s=None) -> bool:
        if not args.verdict:
            return True
        payload = {
            "verdict": "PASS" if all(s["ok"] for s in steps_now) else "FAIL",
            "steps": len(steps_now),
            "failed": [s["name"] for s in steps_now if not s["ok"]],
        }
        if elapsed_s is not None:
            payload["elapsed_s"] = elapsed_s
        return _watch.write_verdict(args.verdict, payload)

    if args.watch and args.watch > 0:
        import time as _time

        max_ticks = _watch.cap("JEV_SMOKE_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_SMOKE_WATCH_SECS", getattr(args, "watch_max", 0.0))
        last_steps: list[dict] = []
        verdict_ok = True
        watch_t0 = _time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or _time.time() < dead):
            steps = _run_steps()
            tick = {
                "ts": int(_time.time()),
                "ok": all(s["ok"] for s in steps),
                "failed": [s["name"] for s in steps if not s["ok"]],
                "elapsed_s": round(_time.time() - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, args.jq, args.out, quiet=_watch.quiet("JEV_SMOKE_WATCH_QUIET", args.quiet), bad=bool(tick["failed"]))
            last_steps = steps
            ticks += 1
            sys.stderr.write(
                "watch tick=%d ok=%s failed=%s\n"
                % (ticks, tick["ok"], ",".join(tick["failed"]) or "-")
            )
            if verdict_ok and not _write_verdict(
                last_steps, elapsed_s=round(_time.time() - watch_t0, 2)
            ):
                verdict_ok = False  # warn once, stop retrying
            if args.fail_fast and tick["failed"]:
                break
            _time.sleep(args.watch)
        if args.verdict and verdict_ok and not _write_verdict(
            last_steps, elapsed_s=round(_time.time() - watch_t0, 2)
        ):
            return 1
        if args.junit:
            try:
                _atomic_write(Path(args.junit), junit_xml(last_steps))
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.junit, exc))
                return 1
        return 0 if tick["ok"] else 1
    steps = _run_steps()
    ok = all(s["ok"] for s in steps)
    text = json.dumps({"ok": ok, "steps": steps}, indent=2) + "\n"
    if args.jq:
        value, found = jq_lookup({"ok": ok, "steps": steps}, args.jq)
        if not found:
            sys.stderr.write(
                "bad --jq key %r (payload has: ok, steps)\n" % args.jq
            )
            return 2
        sys.stdout.write(json.dumps(value) + "\n")
    else:
        sys.stdout.write(text)
    if args.out:
        try:
            _atomic_write(Path(args.out), text)
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.out)
    if args.verdict and not _write_verdict(steps):
        return 1
    if args.report:
        lines = [
            "# smoke report",
            "",
            "verdict: **%s**" % ("PASS" if ok else "FAIL"),
            "",
            "- steps: %d" % len(steps),
            "- failed: %d" % sum(1 for s in steps if not s["ok"]),
            "",
            "| step | ok | detail |",
            "| --- | --- | --- |",
        ]
        for s in steps:
            detail = str(s.get("detail") or "").replace("|", "\\|").replace("\n", " ")
            lines.append(
                "| %s | %s | %s |" % (s["name"], "yes" if s["ok"] else "NO", detail)
            )
        try:
            _atomic_write(Path(args.report), "\n".join(lines) + "\n")
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.report, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.report)
    if args.junit:
        try:
            _atomic_write(Path(args.junit), junit_xml(steps))
        except OSError as exc:
            sys.stderr.write("cannot write %s: %s\n" % (args.junit, exc))
            return 1
        sys.stderr.write("wrote %s\n" % args.junit)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
