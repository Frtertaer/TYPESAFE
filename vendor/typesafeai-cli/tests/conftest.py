from __future__ import annotations

import pytest

from typesafe_cli.detect import ALL_ENV_VARS, set_force_agent


@pytest.fixture(autouse=True)
def isolate_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ALL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    set_force_agent(None)
    yield
    set_force_agent(None)
