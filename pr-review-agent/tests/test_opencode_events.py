"""Tests for _parse_opencode_events and _stderr_tail in review.py."""
import json

from review import _parse_opencode_events, _stderr_tail


def _write_events(tmp_path, events):
    p = tmp_path / "opencode.out"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return str(p)


# ── _parse_opencode_events ─────────────────────────────────────────────────────

def test_assembles_text_from_ordered_parts(tmp_path):
    path = _write_events(tmp_path, [
        {"type": "message.part.updated", "part": {"type": "text", "id": "a", "text": "Hello"}},
        {"type": "message.part.updated", "part": {"type": "text", "id": "b", "text": "wo"}},
        {"type": "message.part.updated", "part": {"type": "text", "id": "b", "text": "world"}},
    ])
    text, usage, error = _parse_opencode_events(path)
    assert text == "Helloworld"  # latest cumulative text per part id, in first-seen order
    assert usage == {}
    assert error == ""


def test_current_schema_text_and_step_finish(tmp_path):
    # The event names OpenCode 1.18 actually emits in `run --format json`.
    path = _write_events(tmp_path, [
        {"type": "step_start", "part": {"type": "step-start", "id": "s"}},
        {"type": "text", "part": {"type": "text", "id": "t", "text": "1\n2\n3"}},
        {"type": "step_finish", "part": {"type": "step-finish", "cost": 0.0037,
            "tokens": {"input": 2, "output": 13, "reasoning": 0,
                       "cache": {"read": 7647, "write": 339}}}},
    ])
    text, usage, error = _parse_opencode_events(path)
    assert text == "1\n2\n3"
    assert usage == {"input": 2, "output": 13, "reasoning": 0,
                     "cache_read": 7647}
    assert error == ""


def test_reasoning_parts_excluded_from_text(tmp_path):
    path = _write_events(tmp_path, [
        {"type": "message.part.updated", "part": {"type": "reasoning", "id": "r", "text": "thinking"}},
        {"type": "message.part.updated", "part": {"type": "text", "id": "a", "text": "answer"}},
    ])
    text, _, _ = _parse_opencode_events(path)
    assert text == "answer"


def test_extracts_usage_from_assistant_message(tmp_path):
    path = _write_events(tmp_path, [
        {"type": "message.updated", "info": {
            "role": "assistant", "cost": 0.0321,
            "tokens": {"input": 2100, "output": 480, "reasoning": 64,
                       "cache": {"read": 1900, "write": 0}},
        }},
    ])
    _, usage, _ = _parse_opencode_events(path)
    assert usage == {"input": 2100, "output": 480, "reasoning": 64,
                     "cache_read": 1900}


def test_error_event_is_captured(tmp_path):
    path = _write_events(tmp_path, [
        {"type": "error", "error": {
            "name": "APIError",
            "data": {"message": "Not Found", "metadata": {"url": "https://x/models/foo"}},
        }},
    ])
    text, _, error = _parse_opencode_events(path)
    assert error == "APIError: Not Found (https://x/models/foo)"


def test_error_without_metadata_url(tmp_path):
    path = _write_events(tmp_path, [
        {"type": "session.error", "error": {"name": "Boom", "data": {"message": "kaboom"}}},
    ])
    _, _, error = _parse_opencode_events(path)
    assert error == "Boom: kaboom"


def test_non_json_lines_are_skipped(tmp_path):
    p = tmp_path / "opencode.out"
    p.write_text(
        "not json\n"
        + json.dumps({"type": "message.part.updated",
                      "part": {"type": "text", "id": "a", "text": "ok"}}) + "\n"
        + "\n"  # blank line
    )
    text, _, error = _parse_opencode_events(str(p))
    assert text == "ok"
    assert error == ""


# ── _stderr_tail ───────────────────────────────────────────────────────────────

def test_stderr_tail_strips_ansi_and_blank_lines():
    raw = "\x1b[31mred error\x1b[0m\n\n   \nplain line\n"
    assert _stderr_tail(raw) == "red error\nplain line"


def test_stderr_tail_caps_length():
    raw = "\n".join(f"line{i}" for i in range(1000))
    out = _stderr_tail(raw, limit=20)
    assert len(out) <= 20
    assert out.endswith("line999")
    # capped on whole-line boundaries — never a leading partial line
    assert all(ln.startswith("line") for ln in out.split("\n"))


def test_stderr_tail_single_oversized_line_stays_bounded():
    out = _stderr_tail("x" * 5000, limit=20)
    assert len(out) == 20
