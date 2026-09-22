#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install one Hermes plugin (--no-enable) or one official MCP after Jev pick.

Sidecar after catalog_fill prints no_catalog. Does not run from a prompt hook.
Never --force. Never --enable. Never npx. Never claude plugin install.
Never clone kitze/skillbox. Other harness markets stay human. Fail open.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
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
    clear_miss,
    clear_scan_cache,
    detect_harness,
    hermes_home,
    shortlist,
    tokens,
    user_home,
    write_sidecar,
)
from peer_fill import read_miss, run_jev  # noqa: E402

ASK_NAME = ".jev-apply-fill.request.json"
SEARCH_LIMIT = 8
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
    if not text or "://" in text or "/" in text or "\\" in text:
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
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


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
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="")
    parser.add_argument("--harness", default="auto")
    parser.add_argument("--home", default="")
    parser.add_argument("--hermes-home", default="")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--pick", default="")
    parser.add_argument("--from-miss", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ask-file", default="")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the outcome as a JSON object instead of a text line.",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-scan the cwd for miss/ask files every S seconds, printing {ts,miss,ask} ticks (read-only; JEV_APPLY_WATCH_MAX caps ticks).",
    )
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument(
        "--out",
        default="",
        help="With --watch, append each tick line to PATH (fail-open).",
    )
    args = parser.parse_args()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    if args.watch and args.watch > 0:
        max_ticks = _watch.cap("JEV_APPLY_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline(getattr(args, "watch_max", 0.0))
        ask_path = Path(args.ask_file) if args.ask_file else cwd / ASK_NAME
        miss_path = cwd / MISS_NAME
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
            miss = read_miss(miss_path)
            tick = {
                "ts": int(time.time()),
                "miss": bool(miss),
                "ask": ask_path.is_file(),
            }
            _watch.emit(tick, args.out, quiet=args.quiet, bad=bool(tick["miss"] or tick["ask"]))
            ticks += 1
            time.sleep(args.watch)
        return 0
    task = args.task
    dest = args.harness
    if args.from_miss:
        miss = read_miss(cwd / MISS_NAME)
        task = task or str(miss.get("task") or "")
        if dest == "auto":
            dest = str(miss.get("harness") or "auto")
    if not task.strip():
        if args.json:
            sys.stdout.write(json.dumps({"outcome": "no_task"}) + "\n")
        else:
            sys.stdout.write("no_task\n")
        return 0
    if dest == "auto":
        dest = detect_harness(Path(__file__))
    ask_path = Path(args.ask_file) if args.ask_file else cwd / ASK_NAME
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
