from __future__ import annotations

from typesafe_cli.format import dumps, error_envelope, success_envelope


def test_success_shape():
    envelope = success_envelope(data={"model": "jev-latest", "answers": {}}, command="ask")
    assert envelope["status"] == "success"
    assert envelope["data"]["model"] == "jev-latest"
    assert envelope["metadata"]["command"] == "ask"


def test_error_shape():
    envelope = error_envelope(code="usage", message="missing questions")
    assert envelope["status"] == "error"
    assert envelope["error"]["code"] == "usage"


def test_dumps_ends_with_newline():
    assert dumps({"a": 1}).endswith("\n")
