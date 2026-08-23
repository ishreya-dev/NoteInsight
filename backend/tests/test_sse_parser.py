"""Focused tests for the shared _parse_sse helper."""

from tests.conftest import _parse_sse


def test_parse_single_event_with_json_data():
    text = "event: status\ndata: {\"stage\": \"preparing\"}\n"
    events = _parse_sse(text)
    assert len(events) == 1
    assert events[0]["event"] == "status"
    assert events[0]["data"] == {"stage": "preparing"}


def test_parse_event_without_data():
    text = "event: ping\n"
    events = _parse_sse(text)
    assert len(events) == 1
    assert events[0]["event"] == "ping"
    assert events[0]["data"] is None


def test_parse_ignores_blank_lines_and_trailing_newlines():
    text = "\n\nevent: status\ndata: {\"ok\": true}\n\n\n"
    events = _parse_sse(text)
    assert len(events) == 1
    assert events[0]["event"] == "status"
    assert events[0]["data"] == {"ok": True}


def test_parse_non_json_data_is_preserved_as_string():
    text = "event: error\ndata: something went wrong\n"
    events = _parse_sse(text)
    assert len(events) == 1
    assert events[0]["event"] == "error"
    assert events[0]["data"] == "something went wrong"


def test_parse_multiple_events():
    text = (
        "event: status\ndata: {\"stage\": \"preparing\"}\n\n"
        "event: token\ndata: {\"text\": \"hello\"}\n\n"
        "event: complete\ndata: {\"done\": true}\n"
    )
    events = _parse_sse(text)
    assert [e["event"] for e in events] == ["status", "token", "complete"]
    assert events[1]["data"] == {"text": "hello"}


def test_parse_empty_string_returns_empty_list():
    assert _parse_sse("") == []
    assert _parse_sse("\n\n") == []
