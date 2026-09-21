from __future__ import annotations

import json
from pathlib import Path

from typesafe_cli.config import load_config


def test_env_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "apikey_test")
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    monkeypatch.delenv("TYPESAFE_DEFAULT_MODEL", raising=False)
    cfg = load_config()
    assert cfg.api_key == "apikey_test"
    assert cfg.model == "jev-latest"
    assert cfg.base_url == "https://api.typesafe.ai"


def test_flag_key_wins(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env")
    cfg = load_config(key="flag")
    assert cfg.api_key == "flag"


def test_creds_file(tmp_path: Path):
    path = tmp_path / "creds.json"
    path.write_text(json.dumps({"api_key": "fromfile"}))
    cfg = load_config(creds=path)
    assert cfg.api_key == "fromfile"


def test_model_and_base_url_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_DEFAULT_MODEL", "jev-preview")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://example.test")
    cfg = load_config()
    assert cfg.model == "jev-preview"
    assert cfg.base_url == "https://example.test"
