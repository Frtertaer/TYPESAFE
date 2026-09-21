from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"


@dataclass(frozen=True)
class Config:
    api_key: str | None
    base_url: str
    model: str


def load_config(
    *,
    key: str | None = None,
    creds: Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
) -> Config:
    api_key = key
    if api_key is None and creds is not None:
        payload = json.loads(creds.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "api_key" not in payload:
            raise ValueError("creds file must be JSON with an api_key field")
        api_key = payload["api_key"]
    if api_key is None:
        api_key = os.environ.get("TYPESAFE_API_KEY") or None

    resolved_base = base_url or os.environ.get("TYPESAFE_BASE_URL") or DEFAULT_BASE_URL
    resolved_model = model or os.environ.get("TYPESAFE_DEFAULT_MODEL") or DEFAULT_MODEL
    return Config(api_key=api_key, base_url=resolved_base, model=resolved_model)
