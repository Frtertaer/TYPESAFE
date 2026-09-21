#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Copy one already-local skill into the current harness.

Does not install marketplace items. Does not call Jev from a prompt hook.
Fail open.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from inventory import (  # noqa: E402
    HARNESSES,
    KIND_SKILL,
    MISS_NAME,
    SIDECAR_NAME,
    append_decision,
    clear_miss,
    clear_scan_cache,
    detect_harness,
    hermes_home,
    iter_skills,
    read_sidecar,
    roots_for,
    shortlist,
    sidecar_fresh,
    tokens,
    user_home,
    write_sidecar,
)

SKIP_NAMES = {"jev-consult"}
COPY_SKIP = {".git", "node_modules", "__pycache__", ".venv", ".mypy_cache"}
PEER_LIMIT = 12
ASK_NAME = ".jev-peer-fill.request.json"


def skill_dirs(harness: str, home: Path, hermes: Path) -> list[Path]:
    return list(roots_for(harness, home, hermes)["skills"])


def skill_items(harness: str, home: Path, hermes: Path) -> list[dict]:
    return iter_skills(skill_dirs(harness, home, hermes))


def names_in(items: list[dict]) -> set[str]:
    return {str(item.get("name") or "").lower() for item in items}


def allowed_roots(home: Path, hermes: Path) -> list[Path]:
    roots: list[Path] = []
    for harness in HARNESSES:
        roots.extend(skill_dirs(harness, home, hermes))
    return roots


def under_any(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


def peer_skills(dest: str, home: Path, hermes: Path) -> list[dict]:
    skip = names_in(skill_items(dest, home, hermes)) | SKIP_NAMES
    out: list[dict] = []
    for harness in HARNESSES:
        if harness == dest:
            continue
        for item in skill_items(harness, home, hermes):
            name = str(item.get("name") or "").lower()
            if item.get("kind") != KIND_SKILL or name in skip:
                continue
            copy = dict(item)
            copy["source_harness"] = harness
            out.append(copy)
            skip.add(name)
    return out


def write_peer_ask(path: Path, task: str, dest: str, picked: list[dict]) -> None:
    criteria: dict[str, str] = {}
    for item in picked:
        label = "%s from %s" % (item.get("name"), item.get("source_harness") or "?")
        desc = (item.get("description") or "").strip()
        if desc:
            label = "%s: %s" % (label, desc[:160])
        ident = str(item.get("id") or item.get("name"))
        criteria[ident] = label
    criteria["none"] = "none of these; do not copy anything"
    payload = {
        "state": {
            "task": task,
            "harness": dest,
            "note": (
                "Current harness has no installed match. These skills exist on another "
                "local harness. Pick one to copy here. Do not install marketplace items."
            ),
        },
        "questions": {
            "load_tools": {
                "type": "choice",
                "instructions": "Which local skill should the coder copy into this harness?",
                "criteria": criteria,
            }
        },
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_jev(ask_path: Path) -> dict | None:
    script = _SCRIPTS / "jev.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "ask", str(ask_path)],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def item_for_pick(pick: str, candidates: list[dict]) -> dict | None:
    if not pick or pick == "none":
        return None
    for item in candidates:
        if item.get("id") == pick or item.get("name") == pick:
            return item
    return None


def ignore(_directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in COPY_SKIP or n.endswith(".pyc")}


def copy_one(src: Path, dest_parent: Path, dry_run: bool) -> Path | None:
    dest = dest_parent / src.name
    if dest.exists():
        return None
    if dry_run:
        return dest
    dest_parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=ignore, symlinks=False)
    return dest


def read_miss(path: Path, ttl_seconds: float | None = None, now: float | None = None) -> dict:
    data = read_sidecar(path)
    if not data:
        return {}
    if not sidecar_fresh(data, ttl_seconds, now):
        try:
            path.unlink()
        except OSError:
            pass
        return {}
    return data


def fill(
    task: str,
    dest: str,
    home: Path,
    hermes: Path,
    cwd: Path,
    pick: str | None,
    dry_run: bool,
    ask_path: Path,
) -> int:
    def emit(msg: str) -> None:
        sys.stdout.write(msg + "\n")
        append_decision(
            {
                "ts": int(time.time()),
                "harness": dest,
                "jev_status": "fill",
                "fill": "peer",
                "outcome": msg.split()[0],
                "prompt_head": task[:120],
            }
        )

    local = skill_items(dest, home, hermes)
    if tokens(task) and shortlist(local, task, PEER_LIMIT, []):
        write_sidecar(cwd / SIDECAR_NAME, dest, task, shortlist(local, task, 6, []))
        emit("already_enough")
        return 0
    peers = peer_skills(dest, home, hermes)
    chosen = None
    if pick:
        chosen = item_for_pick(pick, peers)
    else:
        candidates = shortlist(peers, task, PEER_LIMIT, [])
        if not candidates:
            emit("no_peer")
            return 0
        write_peer_ask(ask_path, task, dest, candidates)
        data = run_jev(ask_path)
        if not data:
            emit("jev_skip")
            return 0
        decision = data.get("decision") or {}
        if decision.get("action") != "proceed":
            emit("jev_skip")
            return 0
        chosen = item_for_pick(
            str((decision.get("picks") or {}).get("load_tools") or ""),
            candidates,
        )
    if chosen is None:
        emit("none")
        return 0
    src = Path(str(chosen.get("path") or ""))
    if (
        not src.is_dir()
        or not (src / "SKILL.md").is_file()
        or src.name.lower() in SKIP_NAMES
        or not under_any(src, allowed_roots(home, hermes))
    ):
        emit("bad_source")
        return 0
    copied: list[str] = []
    for parent in skill_dirs(dest, home, hermes):
        dest_path = copy_one(src, parent, dry_run)
        if dest_path is not None:
            copied.append(str(dest_path))
    if not copied:
        emit("exists")
        return 0
    if not dry_run:
        write_sidecar(cwd / SIDECAR_NAME, dest, task, [chosen])
        clear_miss(cwd / MISS_NAME)
        clear_scan_cache()
    tag = "dry " if dry_run else ""
    emit("%scopied %s -> %s" % (tag, src, ";".join(copied)))
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
        "--list",
        action="store_true",
        help="Print shortlisted peer items (kind, name, path) and exit; no writes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --list: emit a JSON array of items instead of text lines.",
    )
    args = parser.parse_args()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    task = args.task
    dest = args.harness
    if args.from_miss:
        miss = read_miss(cwd / MISS_NAME)
        task = task or str(miss.get("task") or "")
        if dest == "auto":
            dest = str(miss.get("harness") or "auto")
    home = Path(args.home) if args.home else user_home()
    hermes = Path(args.hermes_home) if args.hermes_home else hermes_home()
    if dest == "auto":
        dest = detect_harness(Path(__file__))
    if args.list:
        try:
            peers = peer_skills(dest, home, hermes)
            items = shortlist(peers, task, PEER_LIMIT, []) if tokens(task) else peers
            if args.json:
                rows = [
                    {
                        "kind": item.get("kind") or "skill",
                        "name": item.get("name") or "?",
                        "path": item.get("path") or "",
                    }
                    for item in items
                ]
                sys.stdout.write(json.dumps(rows, indent=2) + "\n")
            else:
                for item in items:
                    sys.stdout.write(
                        "%s %s %s\n"
                        % (
                            item.get("kind") or "skill",
                            item.get("name") or "?",
                            item.get("path") or "",
                        )
                    )
        except Exception:
            pass
        return 0
    if not task.strip():
        sys.stdout.write("no_task\n")
        return 0
    ask_path = Path(args.ask_file) if args.ask_file else cwd / ASK_NAME
    try:
        return fill(
            task,
            dest,
            home,
            hermes,
            cwd,
            args.pick.strip() or None,
            args.dry_run,
            ask_path,
        )
    except Exception:
        sys.stdout.write("fail_open\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
