from __future__ import annotations

import inspect
from typing import Any

from typesafe_cli import __version__
from typesafe_cli.detect import is_agent_mode


def command_schema(*, compact: bool = False) -> dict[str, Any]:
    from typesafe_cli.cli import app

    commands = _walk_typer(app, path=[])
    if compact:
        return {
            "version": __version__,
            "name": "typesafe",
            "commands": commands,
        }
    return {
        "version": __version__,
        "name": "typesafe",
        "description": "CLI for TypeSafe System One (Jev). Typed judgments, not chat.",
        "agent_mode": is_agent_mode(),
        "auth": {
            "check": "typesafe auth status",
            "flags": ["--key", "--creds"],
            "note": "Agents cannot access typesafeai-cli environment variables or key files. Use auth status (has_key only). Never print, echo, or cat secrets.",
        },
        "secrets": {
            "rule": "Agents cannot access typesafeai-cli environment variables or key files.",
            "forbidden": [
                "TYPESAFE_API_KEY",
                "TYPESAFE_BASE_URL",
                "TYPESAFE_DEFAULT_MODEL",
                "~/.config/typesafe/env",
                ".env.local",
                "--creds files",
                "--key values",
            ],
            "do_not": [
                "echo $TYPESAFE_API_KEY",
                "printenv or env | grep TYPESAFE",
                "cat ~/.config/typesafe/env or .env.local",
            ],
            "instead": "Run typesafe auth status. If has_key is false, ask the human. The CLI loads the key; you do not.",
        },
        "output": {
            "stdout": "{status, data, metadata}",
            "stderr": "{status, error: {code, message}}",
            "exit_codes": {"0": "answered", "1": "request failed", "2": "usage / invalid questions"},
        },
        "anti_patterns": [
            "Do not chat with Jev or ask it what to do next.",
            "Do not loop typesafe noul once per question; batch independent questions in typesafe ask, or use find/rank/extract/verify/screen/suggest-skill.",
            "Do not invent request or response fields; use typesafe agent schema.",
            "Do not treat a Noul near 0.5 as medium intensity; it is uncertainty.",
            "Do not read, print, or echo TYPESAFE_* environment variables or key files.",
        ],
        "workflows": [
            {
                "name": "evaluate",
                "skill": "typesafe-cli",
                "steps": [
                    "typesafe auth status — use has_key only; do not read env vars or key files.",
                    "Write questions first. Instructions must backtick the state paths they need. That list is the collection contract — do not catalog every agent job.",
                    "Fill only those paths in ${TMPDIR:-/tmp}/codex/<project>/state.json. If a path is code, include the hunk body. Redact secrets. If you cannot, do not call TypeSafe.",
                    "typesafe ask --state-file $WORKDIR/state.json --questions-file $WORKDIR/questions.json",
                    "Apply thresholds locally. Noul ~0.5 is unsure. Low confidence: abstain. You pick the next action.",
                    "Compose: one ask per document (do not loop noul). Local prefilter then Jev (regex, quote match). Use find/rank/extract/verify/screen/suggest-skill/decide when those flags fit. Second HTTP only to shrink options or fetch evidence.",
                ],
            }
        ],
        "commands": commands,
    }


def schema_for_help(ctx: Any) -> dict[str, Any]:
    full = command_schema(compact=False)
    path = [part for part in ctx.command_path.split() if part and part != "typesafe"]
    if not path:
        return full
    scoped = _find_command(full["commands"], path)
    if scoped is None:
        return full
    data = dict(full)
    data["commands"] = [scoped]
    return data


def _walk_typer(app: Any, *, path: list[str]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for info in getattr(app, "registered_commands", []):
        name = info.name or getattr(info.callback, "__name__", "")
        node = _command_node(info, path=[*path, name])
        if path:
            # drop global --agent flags from nested command param lists; they live on the root
            node["params"] = [p for p in node["params"] if "--agent" not in p["names"] and "--no-agent" not in p["names"]]
        nodes.append(node)
    for info in getattr(app, "registered_groups", []):
        name = info.name or ""
        sub_app = info.typer_instance
        nodes.append(
            {
                "name": " ".join([*path, name]).strip(),
                "help": info.help or "",
                "params": [],
                "commands": _walk_typer(sub_app, path=[*path, name]) if sub_app is not None else [],
            }
        )
    return nodes


def _command_node(info: Any, *, path: list[str]) -> dict[str, Any]:
    callback = info.callback
    params: list[dict[str, Any]] = []
    if callback is not None:
        for parameter in inspect.signature(callback).parameters.values():
            if parameter.name == "ctx":
                continue
            default = parameter.default
            decls = getattr(default, "param_decls", None)
            names = [str(d) for d in decls] if decls else [parameter.name]
            required = getattr(default, "default", inspect.Parameter.empty) is ...
            if default is inspect.Parameter.empty:
                required = True
                names = [parameter.name]
            params.append(
                {
                    "names": names,
                    "required": bool(required),
                    "help": getattr(default, "help", None),
                }
            )
    return {
        "name": " ".join(path),
        "help": info.help or (getattr(callback, "__doc__", None) or ""),
        "params": params,
    }


def _find_command(commands: list[dict[str, Any]], path: list[str]) -> dict[str, Any] | None:
    wanted = " ".join(path)
    for cmd in commands:
        if cmd.get("name") == wanted:
            return cmd
        nested = _find_command(cmd.get("commands") or [], path)
        if nested is not None:
            return nested
    return None
