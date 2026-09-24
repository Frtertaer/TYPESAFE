#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install one Hermes plugin (--no-enable) or one official MCP after Jev pick.

Sidecar after catalog_fill prints no_catalog. Does not run from a prompt hook.
Never --force. Never --enable. Never npx. Never claude plugin install.
Never clone kitze/skillbox. Other harness markets stay human. Fail open.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402
from catalog_fill import BLOCKED_INSPECT, blocked_text, slug_id  # noqa: E402
from inventory import (  # noqa: E402
    MISS_NAME,
    SIDECAR_NAME,
    append_decision,
    atomic_write_text,
    clear_miss,
    clear_scan_cache,
    detect_harness,
    FILL_SCHEMA_ROWS,
    hermes_home,
    emit_verify,
    verify_installed,
    shortlist,
    tokens,
    user_home,
    write_miss,
    write_sidecar,
)
from peer_fill import read_miss, run_jev, fill_timeout_seconds  # noqa: E402

ASK_NAME = ".jev-apply-fill.request.json"
SEARCH_LIMIT = 8


def env_report() -> dict:
    """Resolved apply_fill environment. Values only — never secrets."""
    policy = os.environ.get("JEV_POLICY", "").strip()
    try:
        watch_secs = float(os.environ.get("JEV_APPLY_WATCH_SECS", "") or 0)
    except ValueError:
        watch_secs = 0.0
    return {
        "fill_timeout_seconds": fill_timeout_seconds(),
        "watch_max": _watch.cap("JEV_APPLY_WATCH_MAX", None),
        "watch_secs": watch_secs,
        "watch_quiet": _watch.quiet("JEV_APPLY_WATCH_QUIET", False),
        "policy": policy if policy else "default",
    }
# Emitted outcome words (first word of every emit() line + the two stdout-only
# short-circuits); --schema lists them and the drift-guard test pins them.
OUTCOMES = (
    "blocked",
    "dry",
    "fail_open",
    "human",
    "inspect_fail",
    "install_fail",
    "installed",
    "jev_skip",
    "no_apply",
    "no_hermes",
    "no_task",
    "none",
)
MCP_LINE = re.compile(
    r"^\s+(\S+)\s+(available|configured|enabled|disabled|error|installed|connected)\s+(.*\S)\s*$",
    re.I,
)
KIND_RE = re.compile(r"^(plugin|mcp):(.+)$", re.I)


def hermes_bin() -> str | None:
    return shutil.which("hermes") or shutil.which("hermes.exe")


def run_hermes(argv: list[str], timeout: int = 120) -> tuple[int, str]:
    binary = hermes_bin()
    if not binary:
        return 127, ""
    try:
        proc = subprocess.run(
            [binary, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def bare_name(value: str) -> str:
    text = (value or "").strip()
    if not text or text.startswith("-") or "://" in text or "/" in text or "\\" in text:
        return ""
    return text


def parse_kind_pick(pick: str) -> tuple[str, str] | None:
    text = (pick or "").strip()
    if not text or text == "none":
        return None
    match = KIND_RE.match(text)
    if match:
        name = bare_name(match.group(2).strip())
        if not name:
            return None
        return match.group(1).lower(), name
    name = bare_name(text)
    if not name:
        return None
    return "plugin", name


def as_item(kind: str, name: str, description: str = "") -> dict:
    ident = "%s:%s" % (kind, name)
    return {
        "id": slug_id(ident),
        "kind": kind,
        "name": name,
        "identifier": ident,
        "description": description,
    }


def drop_blocked(hits: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        ident = str(hit.get("identifier") or "").strip()
        name = str(hit.get("name") or "").strip()
        kind = str(hit.get("kind") or "").strip()
        if not ident or ident in seen or kind not in ("plugin", "mcp"):
            continue
        if not bare_name(name):
            continue
        if blocked_text(name, ident, str(hit.get("description") or ""), kind):
            continue
        seen.add(ident)
        out.append(hit)
    return out


def parse_plugin_search(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows: list = []
    if isinstance(data, dict):
        rows = data.get("results") or data.get("plugins") or []
    elif isinstance(data, list):
        rows = data
    hits: list[dict] = []
    if not isinstance(rows, list):
        return hits
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = bare_name(str(row.get("name") or ""))
        if not name:
            continue
        hits.append(as_item("plugin", name, str(row.get("description") or "")))
    return drop_blocked(hits)


def parse_mcp_catalog(raw: str, query: set[str]) -> list[dict]:
    hits: list[dict] = []
    for line in (raw or "").splitlines():
        match = MCP_LINE.match(line)
        if not match:
            continue
        name = bare_name(match.group(1))
        if not name:
            continue
        desc = match.group(3).strip()
        blob = "%s %s" % (name, desc)
        words = tokens(blob)
        if query and not (query & words) and name.lower() not in query:
            continue
        hits.append(as_item("mcp", name, desc))
        if len(hits) >= SEARCH_LIMIT:
            break
    return drop_blocked(hits)


def search_hits(task: str) -> list[dict] | None:
    query_text = " ".join(sorted(tokens(task))[:8]) or task[:80]
    if not query_text.strip():
        return []
    query = tokens(query_text)
    plugin_code, plugin_out = run_hermes(["plugins", "search", query_text, "--json"])
    if plugin_code == 127:
        return None
    mcp_code, mcp_out = run_hermes(["mcp", "catalog"])
    if mcp_code == 127:
        return None
    plugins = parse_plugin_search(plugin_out) if plugin_code == 0 else []
    mcps = parse_mcp_catalog(mcp_out, query) if mcp_code == 0 else []
    return plugins[:SEARCH_LIMIT] + mcps[:SEARCH_LIMIT]


def inspect_ok(kind: str, name: str) -> bool:
    if kind == "plugin":
        code, out = run_hermes(["plugins", "search", name, "--json"])
        if code != 0:
            return False
        hits = parse_plugin_search(out)
        if not any(hit.get("name") == name for hit in hits):
            return False
        low = out.lower()
        return not any(marker in low for marker in BLOCKED_INSPECT)
    if kind == "mcp":
        return bool(bare_name(name))
    return False


def install_argv(kind: str, name: str) -> list[str]:
    if kind == "plugin":
        return ["plugins", "install", name, "--no-enable"]
    if kind == "mcp":
        return ["mcp", "install", name]
    return []


def install_one(kind: str, name: str, dry_run: bool) -> bool:
    argv = install_argv(kind, name)
    if not argv or "--force" in argv or "--enable" in argv:
        return False
    if dry_run:
        return True
    code, _out = run_hermes(argv, timeout=180)
    return code == 0


def write_apply_ask(path: Path, task: str, hits: list[dict]) -> None:
    criteria: dict[str, str] = {}
    for hit in hits:
        label = "%s (%s)" % (hit.get("name"), hit.get("kind"))
        desc = (hit.get("description") or "").strip()
        if desc:
            label = "%s: %s" % (label, desc[:160])
        criteria[str(hit["id"])] = label
    criteria["none"] = "none of these; skip the install"
    payload = {
        "state": {
            "task": task,
            "harness": "hermes",
            "note": (
                "catalog_fill had no skill. These are Hermes plugins and official MCP names. "
                "Pick one. Coder will inspect then install: plugin --no-enable, or mcp install. "
                "Never --force. Never npx. Never claude plugin install. Not skillbox."
            ),
        },
        "questions": {
            "load_tools": {
                "type": "choice",
                "instructions": (
                    "Which one Hermes plugin or official MCP should the coder inspect and install?"
                ),
                "criteria": criteria,
            }
        },
    }
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def item_for_pick(pick: str, hits: list[dict]) -> dict | None:
    parsed = parse_kind_pick(pick)
    for hit in hits:
        if hit.get("id") == pick or hit.get("identifier") == pick:
            return hit
        if parsed and hit.get("kind") == parsed[0] and hit.get("name") == parsed[1]:
            return hit
        if hit.get("name") == pick:
            return hit
    return None


def fill(
    task: str,
    dest: str,
    cwd: Path,
    pick: str | None,
    dry_run: bool,
    ask_path: Path,
    as_json: bool = False,
) -> int:
    def emit(msg: str) -> None:
        if as_json:
            sys.stdout.write(
                json.dumps({"outcome": msg.split()[0], "detail": msg}) + "\n"
            )
        else:
            sys.stdout.write(msg + "\n")
        append_decision(
            {
                "ts": int(time.time()),
                "harness": dest,
                "jev_status": "fill",
                "fill": "apply",
                "outcome": msg.split()[0],
                "prompt_head": task[:120],
            }
        )

    if dest != "hermes":
        emit("human")
        return 0
    if pick:
        if blocked_text(pick):
            emit("blocked")
            return 0
        parsed = parse_kind_pick(pick)
        if parsed is None:
            emit("none")
            return 0
        chosen = as_item(parsed[0], parsed[1])
    else:
        hits = search_hits(task)
        if hits is None:
            emit("no_hermes")
            return 0
        ranked = shortlist(hits, task, SEARCH_LIMIT, []) if hits else []
        if not ranked:
            emit("no_apply")
            return 0
        write_apply_ask(ask_path, task, ranked)
        data = run_jev(ask_path)
        if not data:
            emit("jev_skip")
            return 0
        decision = data.get("decision") or {}
        if decision.get("action") != "proceed":
            emit("jev_skip")
            return 0
        chosen = item_for_pick(str((decision.get("picks") or {}).get("load_tools") or ""), ranked)
    if chosen is None:
        emit("none")
        return 0
    kind = str(chosen.get("kind") or "")
    name = str(chosen.get("name") or "")
    if kind not in ("plugin", "mcp") or not name or blocked_text(kind, name):
        emit("blocked")
        return 0
    if not inspect_ok(kind, name):
        emit("inspect_fail")
        return 0
    if not install_one(kind, name, dry_run):
        emit("install_fail")
        return 0
    tag = "dry " if dry_run else ""
    if dry_run:
        emit("%swould_install %s %s" % (tag, kind, name))
        return 0
    write_sidecar(cwd / SIDECAR_NAME, dest, task, [chosen])
    clear_miss(cwd / MISS_NAME)
    clear_scan_cache()
    emit("installed %s %s" % (kind, name))
    return 0


def main() -> int:
    _watch.fix_stdio()
    if _watch.maybe_version(sys.argv[1:]):
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="", help="Task text; '-' reads it from stdin")
    parser.add_argument("--harness", default="auto")
    parser.add_argument("--home", default="")
    parser.add_argument("--hermes-home", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--pick", default="")
    parser.add_argument("--from-miss", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ask-file", default="")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print the ranked installable candidates for --task (or the miss task) and exit; no installs.",
    )
    parser.add_argument(
        "--show",
        default="",
        metavar="NAME",
        help="Print one candidate's full JSON record by name/id and exit.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the outcome as a JSON object instead of a text line.",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Print the decisions.jsonl fill-entry contract and exit (--json emits the object).",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-scan the cwd for miss/ask files every S seconds, printing {ts,miss,ask} ticks (read-only; JEV_APPLY_WATCH_MAX caps ticks).",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the cwd fill state (miss present/age, ask file) as JSON and exit; --jq KEY prints one dotted-path field.",
    )
    parser.add_argument("--env", action="store_true", help="Print the resolved env config JSON ({fill_timeout_seconds, watch_max, watch_secs, watch_quiet, policy}) and exit (--jq KEY prints one field, rc 2 on unknown)")
    parser.add_argument("--jq", metavar="KEY", default="", help="With --status/--env: print just one dotted-path field of the report (e.g. miss_age_s); unknown key exits 2. With --watch: print just the named tick field(s) per pass, comma list.")
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick that finds a pending miss or ask.")
    parser.add_argument("--unchanged-max", metavar="N", type=int, default=0, help="With --watch: stop after N consecutive identical ticks (volatile ts/elapsed_s/miss_age_s ignored)")
    parser.add_argument(
        "--out",
        default="",
        help="With --watch, append each tick line to PATH (fail-open).",
    )
    parser.add_argument(
        "--verdict",
        default="",
        metavar="PATH",
        help="Write a slim {verdict: pending|clean, ticks, miss, ask} JSON to PATH — refreshed every tick with --watch; without it, a one-shot {ticks: 1} payload. '-' prints it to stdout.",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Exercise the offline paths in a temp dir (hermes gate, blocked pick, pick match, miss round-trip+stale prune); exit 1 on failure (--json emits the checks).",
    )
    parser.add_argument(
        "--verify",
        metavar="NAME",
        default="",
        help="Re-scan installed Hermes items and exit 0 when NAME (plugin or MCP, name or kind:name) is installed, 1 otherwise; --json/--jq apply.",
    )
    args = parser.parse_args()
    args.task = _watch.text_arg(args.task)
    if args.schema:
        rows = {key: dict(row) for key, row in FILL_SCHEMA_ROWS.items()}
        rows["outcome"] = {
            "required": True,
            "type": "|".join(OUTCOMES) + " (first word of the emitted line; no_task/fail_open are stdout-only)",
        }
        if args.json:
            sys.stdout.write(json.dumps(rows, indent=2) + "\n")
        else:
            for key, row in rows.items():
                sys.stdout.write(
                    "%s: %s (%s)\n"
                    % (key, row["type"], "required" if row["required"] else "optional")
                )
        return 0
    if args.env:
        report = env_report()
        if args.jq:
            value, found = _watch.dig(report, args.jq)
            if found:
                sys.stdout.write(json.dumps(value) + "\n")
                return 0
            sys.stderr.write(
                "bad --jq key %r (env has: %s)\n"
                % (args.jq, ", ".join(sorted(report)))
            )
            return 2
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"
        sys.stdout.write(text)
        if getattr(args, "out", ""):
            try:
                atomic_write_text(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0
    if args.self_test:
        checks: dict = {}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                ask_path = tmp_path / "ask.json"
                old_log = os.environ.get("JEV_CONSULT_LOG")
                os.environ["JEV_CONSULT_LOG"] = str(tmp_path / "decisions.jsonl")
                try:
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        fill(
                            "self-test task",
                            "claude",
                            tmp_path,
                            None,
                            True,
                            ask_path,
                        )
                    checks["hermes_gate"] = "human" in buf.getvalue()
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        fill(
                            "self-test task",
                            "hermes",
                            tmp_path,
                            "exploit-kit",
                            True,
                            ask_path,
                        )
                    checks["blocked_pick"] = "blocked" in buf.getvalue()
                    hit = as_item("plugin", "selftest-item")
                    checks["pick_match"] = (
                        item_for_pick("plugin:selftest-item", [hit]) is hit
                    )
                    miss_path = tmp_path / MISS_NAME
                    write_miss(miss_path, "hermes", "self-test task")
                    checks["miss_roundtrip"] = (
                        str(read_miss(miss_path).get("task") or "")
                        == "self-test task"
                    )
                    miss_path.unlink()
                    write_miss(
                        miss_path,
                        "hermes",
                        "self-test task",
                        extra={"written_at": 1},
                    )
                    checks["stale_pruned"] = (
                        not read_miss(miss_path) and not miss_path.is_file()
                    )
                finally:
                    if old_log is None:
                        os.environ.pop("JEV_CONSULT_LOG", None)
                    else:
                        os.environ["JEV_CONSULT_LOG"] = old_log
        except Exception:
            checks = {"raised": False}
        ok = bool(checks) and all(checks.values())
        if args.json:
            sys.stdout.write(
                json.dumps(
                    {"self_test": "ok" if ok else "FAIL", "checks": checks},
                    indent=2,
                )
                + "\n"
            )
        else:
            sys.stdout.write(
                "self-test: %s %s\n"
                % (
                    "ok" if ok else "FAIL",
                    " ".join(
                        "%s=%s" % (k, "ok" if v else "FAIL")
                        for k, v in sorted(checks.items())
                    ),
                )
            )
        return 0 if ok else 1
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    if args.verify:
        report, _rc = verify_installed(
            args.verify,
            "hermes",
            home=Path(args.home).expanduser() if args.home else None,
            hermes=Path(args.hermes_home).expanduser() if args.hermes_home else None,
        )
        return emit_verify(report, jq=args.jq, as_json=args.json)
    if args.status:
        try:
            now = time.time()
            miss = read_miss(cwd / MISS_NAME)
            miss_ts = miss.get("written_at") if isinstance(miss.get("written_at"), (int, float)) else None
            ask_path = Path(args.ask_file) if args.ask_file else cwd / ASK_NAME
            report = {
                "cwd": str(cwd),
                "miss": bool(miss),
                "miss_age_s": round(now - miss_ts, 1) if miss_ts is not None else None,
                "miss_written_at": miss_ts,
                "ask": ask_path.is_file(),
                "task": str(miss.get("task") or "") if miss else "",
            }
            if getattr(args, "verdict", ""):
                if not _watch.write_verdict(
                    args.verdict,
                    {
                        "verdict": "pending"
                        if (report["miss"] or report["ask"])
                        else "clean",
                        "ticks": 1,
                        "miss": report["miss"],
                        "ask": report["ask"],
                    },
                ):
                    return 1
            if getattr(args, "out", ""):
                try:
                    atomic_write_text(
                        Path(args.out), json.dumps(report, indent=2, sort_keys=True) + "\n"
                    )
                except OSError as exc:
                    sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
            if args.jq:
                cur = report
                found = True
                for part in args.jq.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        found = False
                        break
                if not found:
                    sys.stderr.write(
                        "bad --jq key %r (payload has: %s)\n"
                        % (args.jq, ", ".join(sorted(report)))
                    )
                    return 2
                sys.stdout.write(json.dumps(cur) + "\n")
            else:
                sys.stdout.write(json.dumps(report, indent=2) + "\n")
        except Exception:
            sys.stdout.write(json.dumps({"error": "fail_open"}) + "\n")
        return 0
    if args.watch and args.watch > 0:
        max_ticks = _watch.cap("JEV_APPLY_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_APPLY_WATCH_SECS", getattr(args, "watch_max", 0.0))
        ask_path = Path(args.ask_file) if args.ask_file else cwd / ASK_NAME
        miss_path = cwd / MISS_NAME
        tick: dict = {}
        verdict_ok = True

        def _write_verdict() -> bool:
            pending = bool(tick.get("miss") or tick.get("ask"))
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "pending" if pending else "clean",
                    "ticks": ticks,
                    "miss": bool(tick.get("miss")),
                    "ask": bool(tick.get("ask")),
                    "elapsed_s": round(time.time() - watch_t0, 2),
                },
            )

        watch_t0 = time.time()
        prev_tick: dict | None = None
        unchanged = 0
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
            now = time.time()
            miss = read_miss(miss_path)
            miss_ts = miss.get("written_at") if isinstance(miss.get("written_at"), (int, float)) else None
            tick = {
                "ts": int(now),
                "miss": bool(miss),
                "miss_age_s": round(now - miss_ts, 1) if miss_ts is not None else None,
                "ask": ask_path.is_file(),
                "elapsed_s": round(now - watch_t0, 2),
            }
            _watch.emit_or_jq(tick, getattr(args, "jq", ""), args.out, quiet=_watch.quiet("JEV_APPLY_WATCH_QUIET", args.quiet), bad=bool(tick["miss"] or tick["ask"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d miss=%s ask=%s\n" % (ticks, tick["miss"], tick["ask"])
            )
            if args.verdict and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if args.fail_fast and (tick["miss"] or tick["ask"]):
                break
            if _watch.same_tick(prev_tick, tick, ignore=("ts", "elapsed_s", "miss_age_s")):
                unchanged += 1
            else:
                unchanged = 0
            prev_tick = dict(tick)
            if getattr(args, "unchanged_max", 0) and unchanged >= args.unchanged_max:
                sys.stderr.write("watch: %d consecutive identical ticks\n" % unchanged)
                break
            time.sleep(args.watch)
        if args.verdict and verdict_ok and not _write_verdict():
            return 1
        return 0
    task = args.task
    dest = args.harness
    if args.from_miss:
        miss = read_miss(cwd / MISS_NAME)
        task = task or str(miss.get("task") or "")
        if dest == "auto":
            dest = str(miss.get("harness") or "auto")
    if args.list or args.show:
        try:
            hits = search_hits(task)
            if args.list:
                items = shortlist(hits, task, SEARCH_LIMIT, []) if hits else []
                if args.json:
                    rows = [
                        {
                            "kind": item.get("kind") or "?",
                            "name": item.get("name") or "?",
                            "id": item.get("id") or "",
                        }
                        for item in items
                    ]
                    sys.stdout.write(json.dumps(rows, indent=2) + "\n")
                else:
                    for item in items:
                        sys.stdout.write(
                            "%s %s %s\n"
                            % (
                                item.get("kind") or "?",
                                item.get("name") or "?",
                                item.get("id") or "",
                            )
                        )
            else:
                match = next(
                    (
                        item
                        for item in (hits or [])
                        if args.show
                        in (
                            item.get("name"),
                            item.get("id"),
                            item.get("identifier"),
                        )
                    ),
                    None,
                )
                if match is None:
                    sys.stdout.write("not found: %s\n" % args.show)
                else:
                    sys.stdout.write(json.dumps(match, indent=2) + "\n")
        except Exception:
            pass
        return 0
    ask_path = Path(args.ask_file) if args.ask_file else cwd / ASK_NAME
    if args.verdict:
        miss_now = bool(read_miss(cwd / MISS_NAME))
        ask_now = ask_path.is_file()
        if not _watch.write_verdict(
            args.verdict,
            {
                "verdict": "pending" if (miss_now or ask_now) else "clean",
                "ticks": 1,
                "miss": miss_now,
                "ask": ask_now,
            },
        ):
            return 1
    if not task.strip():
        if args.json:
            sys.stdout.write(json.dumps({"outcome": "no_task"}) + "\n")
        else:
            sys.stdout.write("no_task\n")
        return 0
    if dest == "auto":
        dest = detect_harness(Path(__file__))
    try:
        return fill(
            task,
            dest,
            cwd,
            args.pick.strip() or None,
            args.dry_run,
            ask_path,
            args.json,
        )
    except Exception:
        if args.json:
            sys.stdout.write(json.dumps({"outcome": "fail_open"}) + "\n")
        else:
            sys.stdout.write("fail_open\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
