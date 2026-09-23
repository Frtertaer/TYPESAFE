#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Install one catalog skill after Jev pick + inspect.

Sidecar after miss. Does not run from a prompt hook. Never --force.
Never plugins, MCP, or npx. Fail open.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import _watch  # noqa: E402
from inventory import (  # noqa: E402
    MISS_NAME,
    SIDECAR_NAME,
    UNTRUSTED_RULE,
    append_decision,
    atomic_write_text,
    clear_miss,
    clear_scan_cache,
    detect_harness,
    FILL_SCHEMA_ROWS,
    hermes_home,
    shortlist,
    tokens,
    user_home,
    write_sidecar,
    _policy_float_key,
)
from peer_fill import (  # noqa: E402
    copy_one,
    fill_timeout_seconds,
    read_miss,
    run_jev,
    skill_dirs,
)

ASK_NAME = ".jev-catalog-fill.request.json"
SEARCH_LIMIT = 8
DEFAULT_CATALOG_CACHE_SECONDS = 900.0
CACHE_MAX_QUERIES = 50
BLOCK_RE = re.compile(
    r"(exploit|attack|hack|malware|phishing|privesc|ransom|payload|\bcve\b|weapon)",
    re.I,
)
BLOCKED_INSPECT = ("blocked scan", "verdict: blocked", "scan blocked", "install blocked")
# Emitted outcome words (first word of every emit() line + the two stdout-only
# short-circuits); --schema lists them and the drift-guard test pins them.
OUTCOMES = (
    "blocked",
    "dry",
    "fail_open",
    "inspect_fail",
    "install_fail",
    "installed",
    "jev_skip",
    "no_catalog",
    "no_hermes",
    "no_task",
    "none",
    "scan_fail",
)


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


def slug_id(identifier: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", identifier or "").strip("_")
    return (text or "item")[:80]


def blocked_text(*parts: str) -> bool:
    blob = " ".join(parts)
    return bool(BLOCK_RE.search(blob))


def drop_blocked(hits: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        ident = str(hit.get("identifier") or "").strip()
        name = str(hit.get("name") or "").strip()
        if not ident or ident in seen:
            continue
        if blocked_text(
            name,
            ident,
            str(hit.get("description") or ""),
            str(hit.get("source") or ""),
        ):
            continue
        seen.add(ident)
        copy = dict(hit)
        copy["id"] = slug_id(ident)
        copy["kind"] = "skill"
        out.append(copy)
    return out


def catalog_cache_seconds() -> float:
    """TTL for cached catalog search hits. Threshold lives in policy.json."""
    return _policy_float_key("catalog_cache_seconds", DEFAULT_CATALOG_CACHE_SECONDS)


def env_report() -> dict:
    """Resolved catalog_fill environment. Values only -- never secrets."""
    policy = os.environ.get("JEV_POLICY", "").strip()
    try:
        watch_secs = float(os.environ.get("JEV_CATALOG_WATCH_SECS", "") or 0)
    except ValueError:
        watch_secs = 0.0
    return {
        "fill_timeout_seconds": fill_timeout_seconds(),
        "catalog_cache_seconds": catalog_cache_seconds(),
        "watch_max": _watch.cap("JEV_CATALOG_WATCH_MAX", None),
        "watch_secs": watch_secs,
        "watch_quiet": _watch.quiet("JEV_CATALOG_WATCH_QUIET", False),
        "policy": policy if policy else "default",
        "miss": MISS_NAME,
        "ask": ASK_NAME,
    }


def catalog_cache_path() -> Path:
    return user_home() / ".cache" / "jev-consult" / "catalog-cache.json"


def read_catalog_cache(
    query: str,
    ttl_seconds: float | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> list[dict] | None:
    """Fresh cached hits for query, or None. Stale/invalid entries ignored."""
    target = path or catalog_cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    entry = raw.get(query)
    if not isinstance(entry, dict):
        return None
    written = entry.get("written_at")
    if not isinstance(written, (int, float)) or isinstance(written, bool):
        return None
    ttl = catalog_cache_seconds() if ttl_seconds is None else float(ttl_seconds)
    age = (time.time() if now is None else float(now)) - float(written)
    if age > ttl:
        return None
    hits = entry.get("hits")
    if not isinstance(hits, list):
        return None
    return [item for item in hits if isinstance(item, dict)]


def catalog_cache_age(query: str, path: Path | None = None) -> float | None:
    """Seconds since the cached hits entry for query was written; None if absent."""
    target = path or catalog_cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = raw.get(query) if isinstance(raw, dict) else None
    written = entry.get("written_at") if isinstance(entry, dict) else None
    if not isinstance(written, (int, float)) or isinstance(written, bool):
        return None
    return time.time() - float(written)


def write_catalog_cache(query: str, hits: list[dict], path: Path | None = None) -> None:
    target = path or catalog_cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    raw[query] = {"written_at": int(time.time()), "hits": hits}
    while len(raw) > CACHE_MAX_QUERIES:
        oldest = min(
            raw,
            key=lambda k: (
                raw[k].get("written_at", 0) if isinstance(raw[k], dict) else 0
            ),
        )
        del raw[oldest]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(raw, indent=2) + "\n")
    except OSError:
        pass


def clear_catalog_cache(query: str, path: Path | None = None) -> bool:
    """Drop the cached hits entry for query. True when an entry was removed."""
    target = path or catalog_cache_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict) or query not in raw:
        return False
    del raw[query]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, json.dumps(raw, indent=2) + "\n")
    except OSError:
        return False
    return True


def parse_search(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def cache_query(task: str) -> str:
    """Cache key search_hits writes under for a task string."""
    return " ".join(sorted(tokens(task))[:8]) or task[:80]


def search_hits(task: str, cache: bool = True) -> list[dict] | None:
    query = cache_query(task)
    if not query.strip():
        return []
    if cache:
        cached = read_catalog_cache(query)
        if cached is not None:
            return drop_blocked(cached)
    code, out = run_hermes(
        ["skills", "search", query, "--json", "--limit", str(SEARCH_LIMIT)]
    )
    if code == 127:
        return None
    if code != 0:
        return []
    hits = drop_blocked(parse_search(out))
    write_catalog_cache(query, hits)
    return hits


def inspect_ok(identifier: str) -> bool:
    code, out = run_hermes(["skills", "inspect", identifier])
    if code != 0:
        return False
    low = out.lower()
    return not any(marker in low for marker in BLOCKED_INSPECT)


def install_argv(identifier: str) -> list[str]:
    return ["skills", "install", identifier, "--yes"]


def install_one(identifier: str, dry_run: bool) -> bool:
    if dry_run:
        return True
    argv = install_argv(identifier)
    if "--force" in argv:
        return False
    code, _out = run_hermes(argv, timeout=180)
    return code == 0


def installed_dir(hermes: Path, hit: dict) -> Path | None:
    ident = str(hit.get("identifier") or "")
    name = str(hit.get("name") or "")
    leaf = ident.rstrip("/").split("/")[-1]
    skills = hermes / "skills"
    for candidate in (name, name.lower(), leaf, leaf.lower()):
        if not candidate:
            continue
        path = skills / candidate
        if (path / "SKILL.md").is_file():
            return path
    if not skills.is_dir():
        return None
    try:
        children = list(skills.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir() or not (child / "SKILL.md").is_file():
            continue
        if child.name.lower() in {name.lower(), leaf.lower()}:
            return child
    return None


def scan_critical(src: Path) -> int | None:
    try:
        import skill_scanner

        findings = skill_scanner.scan_skill(str(src))
    except Exception:
        return None
    return sum(1 for f in findings if f.severity == "CRITICAL")


def write_catalog_ask(path: Path, task: str, dest: str, hits: list[dict]) -> None:
    criteria: dict[str, str] = {}
    for hit in hits:
        label = "%s (%s)" % (hit.get("name"), hit.get("identifier"))
        desc = (hit.get("description") or "").strip()
        if desc:
            label = "%s: %s" % (label, desc[:160])
        criteria[str(hit["id"])] = label
    criteria["none"] = "none of these; skip the install"
    payload = {
        "state": {
            "task": task,
            "harness": dest,
            "note": (
                "Local harness is empty. These are catalog skills from hermes skills search. "
                "Pick one. Coder will inspect then hermes skills install --yes. Never --force. "
                "Never plugins, MCP, or npx."
            ),
        },
        "questions": {
            "load_tools": {
                "type": "choice",
                "instructions": UNTRUSTED_RULE
                + " Which one catalog skill should the coder inspect and install?",
                "criteria": criteria,
            }
        },
    }
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def item_for_pick(pick: str, hits: list[dict]) -> dict | None:
    if not pick or pick == "none":
        return None
    for hit in hits:
        if hit.get("id") == pick or hit.get("identifier") == pick or hit.get("name") == pick:
            return hit
    return None


def fill(
    task: str,
    dest: str,
    home: Path,
    hermes: Path,
    cwd: Path,
    pick: str | None,
    dry_run: bool,
    ask_path: Path,
    as_json: bool = False,
) -> int:
    def emit(msg: str) -> None:
        if as_json:
            sys.stdout.write(
                json.dumps({"outcome": msg.split()[0], "message": msg}) + "\n"
            )
        else:
            sys.stdout.write(msg + "\n")
        append_decision(
            {
                "ts": int(time.time()),
                "harness": dest,
                "jev_status": "fill",
                "fill": "catalog",
                "outcome": msg.split()[0],
                "prompt_head": task[:120],
            }
        )

    if pick:
        if blocked_text(pick):
            emit("blocked")
            return 0
        chosen = {
            "id": slug_id(pick),
            "identifier": pick,
            "name": pick.split("/")[-1],
            "kind": "skill",
        }
    else:
        hits = search_hits(task)
        if hits is None:
            emit("no_hermes")
            return 0
        ranked = shortlist(hits, task, SEARCH_LIMIT, []) if hits else []
        if not ranked:
            emit("no_catalog")
            return 0
        write_catalog_ask(ask_path, task, dest, ranked)
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
    ident = str(chosen.get("identifier") or "").strip()
    if not ident or ident.startswith("-") or blocked_text(ident, str(chosen.get("name") or "")):
        emit("blocked")
        return 0
    if not inspect_ok(ident):
        emit("inspect_fail")
        return 0
    if not install_one(ident, dry_run):
        emit("install_fail")
        return 0
    tag = "dry " if dry_run else ""
    if dry_run:
        emit("%swould_install %s" % (tag, ident))
        return 0
    src = installed_dir(hermes, chosen)
    if src is not None:
        critical = scan_critical(src)
        if critical is not None and critical > 0:
            shutil.rmtree(src, ignore_errors=True)
            emit("scan_fail %s" % ident)
            return 0
    copied: list[str] = []
    if dest != "hermes" and src is not None:
        for parent in skill_dirs(dest, home, hermes):
            dest_path = copy_one(src, parent, False)
            if dest_path is not None:
                copied.append(str(dest_path))
    write_sidecar(cwd / SIDECAR_NAME, dest, task, [chosen])
    clear_miss(cwd / MISS_NAME)
    clear_scan_cache()
    if copied:
        emit("installed %s -> %s" % (ident, ";".join(copied)))
    else:
        emit("installed %s" % ident)
    return 0


def _self_test() -> int:
    """Run the catalog cache/block/parse machinery against a temp-dir
    fixture (no Jev, no Hermes); print ok|FAIL per check."""
    checks = {}
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "catalog-cache.json"
        query = cache_query("selftest task")
        hits = [
            {"name": "selftest-hit", "description": "synthetic", "identifier": "x/y"},
            {"name": "exploit-kit", "description": "drops shells", "identifier": "e/k"},
        ]
        write_catalog_cache(query, hits, path=cache)
        back = read_catalog_cache(query, path=cache)
        checks["cache_roundtrip"] = isinstance(back, list) and len(back) == 2
        stale = read_catalog_cache(
            query, path=cache, ttl_seconds=1, now=time.time() + 3600
        )
        checks["stale_pruned"] = stale is None
        checks["age_reported"] = isinstance(
            catalog_cache_age(query, path=cache), (int, float)
        )
        checks["clear_drops"] = (
            clear_catalog_cache(query, path=cache)
            and read_catalog_cache(query, path=cache) is None
        )
        kept = drop_blocked(hits)
        checks["blocked_drop"] = [h["name"] for h in kept] == ["selftest-hit"]
        parsed = parse_search(json.dumps(hits))
        checks["parse_search"] = [h["name"] for h in parsed] == [
            "selftest-hit",
            "exploit-kit",
        ]
    ok = all(checks.values())
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


def main() -> int:
    if _watch.maybe_version(sys.argv[1:]):
        return 0
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
        help="Print catalog hits for --task (name, identifier) and exit; no installs.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --list: emit a JSON array of hits; in fill mode emit one JSON object per outcome.",
    )
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Print the decisions.jsonl fill-entry contract and exit (--json emits the object).",
    )
    parser.add_argument(
        "--show",
        default="",
        metavar="NAME",
        help="Print one catalog hit's full JSON record by name and exit.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Drop the cached catalog hits for --task and exit.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print the cwd fill state (miss present/age, ask file) as JSON and exit; --jq KEY prints one dotted-path field.",
    )
    parser.add_argument(
        "--watch",
        metavar="S",
        type=float,
        default=0.0,
        help="Re-run the catalog search for --task every S seconds, printing {ts,hits,cached,cache_age_s} ticks (read-only; JEV_CATALOG_WATCH_MAX caps ticks).",
    )
    parser.add_argument("--jq", metavar="KEY", default="", help="With --status: print just one dotted-path field of the report (e.g. miss_age_s); unknown key exits 2. With --watch: print just the named tick field(s) per pass, comma list (e.g. hits); null on a miss.")
    parser.add_argument("--max-ticks", metavar="N", type=int, default=0, help="With --watch: stop after N ticks (overrides the JEV_*_WATCH_MAX env)")
    parser.add_argument("--watch-max", metavar="S", type=float, default=0.0, help="With --watch: stop after S elapsed seconds")
    parser.add_argument("--quiet", action="store_true", help="With --watch: print only failing ticks to stdout (--out still logs all)")
    parser.add_argument("--fail-fast", action="store_true", help="With --watch: stop after the first tick that finds catalog hits")
    parser.add_argument("--verdict", default="", metavar="PATH", help="Write a slim {verdict: hits|none, ticks, hits, cached} JSON — refreshed every tick with --watch; in --list/--show mode a one-shot {ticks: 1} payload.")
    parser.add_argument(
        "--out",
        default="",
        metavar="PATH",
        help="With --watch, append each tick line to PATH (fail-open).",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Exercise the cache/search machinery on a temp-dir catalog (no Jev, no Hermes); exits 1 on failure.",
    )
    parser.add_argument(
        "--env",
        action="store_true",
        help="Print the resolved env config JSON ({fill_timeout_seconds, catalog_cache_seconds, watch_max, watch_secs, watch_quiet, policy, miss, ask}) and exit (--jq KEY prints one field, rc 2 on unknown; --out PATH also writes it).",
    )
    args = parser.parse_args()
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
        if args.out:
            try:
                atomic_write_text(Path(args.out), text)
            except OSError as exc:
                sys.stderr.write("cannot write %s: %s\n" % (args.out, exc))
        return 0
    if args.self_test:
        return _self_test()
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    task = args.task
    dest = args.harness
    if args.from_miss:
        miss = read_miss(cwd / MISS_NAME)
        task = task or str(miss.get("task") or "")
        if dest == "auto":
            dest = str(miss.get("harness") or "auto")
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
    if not task.strip():
        sys.stdout.write(
            json.dumps({"outcome": "no_task"}) + "\n" if args.json else "no_task\n"
        )
        return 0
    home = Path(args.home) if args.home else user_home()
    hermes = Path(args.hermes_home) if args.hermes_home else hermes_home()
    if args.clear:
        removed = clear_catalog_cache(cache_query(task))
        if args.json:
            sys.stdout.write(
                json.dumps({"cleared": bool(removed), "task": task}) + "\n"
            )
        else:
            sys.stdout.write("cleared\n" if removed else "no_cache\n")
        return 0
    if args.watch and args.watch > 0:
        max_ticks = _watch.cap("JEV_CATALOG_WATCH_MAX", args.max_ticks)
        ticks = 0
        dead = _watch.deadline("JEV_CATALOG_WATCH_SECS", getattr(args, "watch_max", 0.0))
        tick: dict = {}
        verdict_ok = True

        def _write_verdict() -> bool:
            return _watch.write_verdict(
                args.verdict,
                {
                    "verdict": "hits" if tick.get("hits") else "none",
                    "ticks": ticks,
                    "hits": tick.get("hits", 0),
                    "cached": bool(tick.get("cached")),
                    "elapsed_s": round(time.time() - watch_t0, 2),
                },
            )

        watch_t0 = time.time()
        while (max_ticks <= 0 or ticks < max_ticks) and (not dead or time.time() < dead):
            tick = {"ts": int(time.time())}
            cquery = cache_query(task)
            try:
                hits = search_hits(task) or []
                tick["hits"] = len(hits)
                tick["cached"] = read_catalog_cache(cquery) is not None
                age = catalog_cache_age(cquery)
                tick["cache_age_s"] = round(age, 1) if age is not None else None
            except Exception:
                tick["hits"] = 0
                tick["cached"] = False
                tick["cache_age_s"] = None
            tick["elapsed_s"] = round(time.time() - watch_t0, 2)
            _watch.emit_or_jq(tick, getattr(args, "jq", ""), args.out, quiet=_watch.quiet("JEV_CATALOG_WATCH_QUIET", args.quiet), bad=bool(tick["hits"]))
            ticks += 1
            sys.stderr.write(
                "watch tick=%d hits=%d cached=%s\n"
                % (ticks, tick["hits"], tick["cached"])
            )
            if args.verdict and verdict_ok and not _write_verdict():
                verdict_ok = False  # warn once, stop retrying
            if args.fail_fast and tick["hits"]:
                break
            time.sleep(args.watch)
        if args.verdict and verdict_ok and not _write_verdict():
            return 1
        return 0
    if dest == "auto":
        dest = detect_harness(Path(__file__))
    def _oneshot_verdict(hits_now) -> bool:
        hits_list = hits_now or []
        return _watch.write_verdict(
            args.verdict,
            {
                "verdict": "hits" if hits_list else "none",
                "ticks": 1,
                "hits": len(hits_list),
                "cached": read_catalog_cache(task) is not None,
            },
        )

    if args.list:
        try:
            hits = search_hits(task)
            if args.verdict and not _oneshot_verdict(hits):
                return 1
            if args.json:
                rows = [
                    {
                        "name": item.get("name") or "?",
                        "identifier": item.get("identifier") or "",
                    }
                    for item in (hits or [])
                ]
                sys.stdout.write(json.dumps(rows, indent=2) + "\n")
            else:
                for item in (hits or []):
                    sys.stdout.write(
                        "%s %s\n"
                        % (item.get("name") or "?", item.get("identifier") or "")
                    )
        except Exception:
            pass
        return 0
    if args.show:
        try:
            hits = search_hits(task)
            if args.verdict and not _oneshot_verdict(hits):
                return 1
            match = next(
                (item for item in (hits or []) if item.get("name") == args.show),
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
            as_json=args.json,
        )
    except Exception:
        sys.stdout.write(
            json.dumps({"outcome": "fail_open"}) + "\n" if args.json else "fail_open\n"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
