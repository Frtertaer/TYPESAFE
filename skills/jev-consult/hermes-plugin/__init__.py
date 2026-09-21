"""Hermes plugin: abridge fat tool results and inject an installed-tools shortlist."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "jev-consult" / "scripts"


def _load_mod(name: str):
    if _SCRIPTS.is_dir() and str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return __import__(name)


def _on_transform_tool_result(
    tool_name: str = "",
    result: Any = None,
    error_type: Any = None,
    error_message: Any = None,
    status: Any = None,
    **_: Any,
) -> str | None:
    del tool_name
    if error_type or error_message:
        return None
    if isinstance(status, str) and status.lower() in ("error", "failed"):
        return None
    try:
        compact_mod = _load_mod("compact")
    except Exception:
        return None
    if isinstance(result, str):
        text = result
    elif result is None:
        return None
    else:
        try:
            text = json.dumps(result, ensure_ascii=False)
        except TypeError:
            text = str(result)
    return compact_mod.abridge_live(text, is_error=False)


def _on_pre_llm_call(user_message: str = "", **_: Any) -> dict | str | None:
    prompt = (user_message or "").strip()
    if not prompt:
        return None
    try:
        hook = _load_mod("inventory_hook")
        out = hook.handle(
            {
                "hook_event_name": "pre_llm_call",
                "prompt": prompt,
                "cwd": str(Path.cwd()),
            },
            harness="hermes",
        )
        if out:
            return out
        return None
    except Exception:
        return None


def register(ctx: Any) -> None:
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
