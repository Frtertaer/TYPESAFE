from __future__ import annotations

import json
from typing import Any


def success_envelope(*, data: dict[str, Any], command: str) -> dict[str, Any]:
    return {"status": "success", "data": data, "metadata": {"command": command}}


def error_envelope(*, code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code, "message": message}}


def dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=2, default=_json_default) + "\n"


def _json_default(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): v for k, v in value.items()}
    return str(value)
