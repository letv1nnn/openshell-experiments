"""Tests for payload/lib/state.py."""
import json
import os

import pytest

import state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reviewed_path(state_dir):
    return os.path.join(state_dir, "reviewed.json")


def _failures_path(state_dir):
    return os.path.join(state_dir, "failures.json")


# ── is_reviewed / mark_reviewed ───────────────────────────────────────────────

def test_is_reviewed_unknown_key_returns_false(state_dir):
    assert not state.is_reviewed(state_dir, "org", "repo", 1, "abc123")


def test_is_reviewed_does_not_create_file_on_miss(state_dir):
    state.is_reviewed(state_dir, "org", "repo", 1, "abc123")
    assert not os.path.exists(_reviewed_path(state_dir))


def test_mark_reviewed_then_is_reviewed(state_dir):
    state.mark_reviewed(state_dir, "org", "repo", 1, "abc123")
    assert state.is_reviewed(state_dir, "org", "repo", 1, "abc123")


def test_different_sha_same_pr_is_independent(state_dir):
    state.mark_reviewed(state_dir, "org", "repo", 1, "sha1")
    assert not state.is_reviewed(state_dir, "org", "repo", 1, "sha2")


def test_different_pr_numbers_are_independent(state_dir):
    state.mark_reviewed(state_dir, "org", "repo", 1, "sha")
    assert not state.is_reviewed(state_dir, "org", "repo", 2, "sha")


def test_mark_reviewed_idempotent(state_dir):
    state.mark_reviewed(state_dir, "org", "repo", 1, "sha")
    state.mark_reviewed(state_dir, "org", "repo", 1, "sha")
    assert state.is_reviewed(state_dir, "org", "repo", 1, "sha")


# ── record_failure / get_failure_count / retry_cap_exceeded ──────────────────

def test_get_failure_count_unknown_returns_zero(state_dir):
    assert state.get_failure_count(state_dir, "org", "repo", 1, "sha") == 0


def test_record_failure_increments(state_dir):
    state.record_failure(state_dir, "org", "repo", 1, "sha")
    assert state.get_failure_count(state_dir, "org", "repo", 1, "sha") == 1
    state.record_failure(state_dir, "org", "repo", 1, "sha")
    assert state.get_failure_count(state_dir, "org", "repo", 1, "sha") == 2


def test_retry_cap_not_exceeded_below_threshold(state_dir):
    state.record_failure(state_dir, "org", "repo", 1, "sha")
    state.record_failure(state_dir, "org", "repo", 1, "sha")
    assert not state.retry_cap_exceeded(state_dir, "org", "repo", 1, "sha", max_retries=3)


def test_retry_cap_exceeded_at_threshold(state_dir):
    for _ in range(3):
        state.record_failure(state_dir, "org", "repo", 1, "sha")
    assert state.retry_cap_exceeded(state_dir, "org", "repo", 1, "sha", max_retries=3)


def test_retry_cap_exceeded_above_threshold(state_dir):
    for _ in range(5):
        state.record_failure(state_dir, "org", "repo", 1, "sha")
    assert state.retry_cap_exceeded(state_dir, "org", "repo", 1, "sha", max_retries=3)


def test_failure_count_isolated_per_sha(state_dir):
    state.record_failure(state_dir, "org", "repo", 1, "sha1")
    state.record_failure(state_dir, "org", "repo", 1, "sha1")
    assert state.get_failure_count(state_dir, "org", "repo", 1, "sha2") == 0


# ── _read_file edge cases ────────────────────────────────────────────────────

def test_read_file_missing_returns_empty(state_dir):
    # Indirectly: is_reviewed on fresh dir returns False without crash.
    assert not state.is_reviewed(state_dir, "o", "r", 99, "x")


def test_read_file_corrupted_json_returns_empty(state_dir):
    path = _reviewed_path(state_dir)
    with open(path, "w") as f:
        f.write("{not valid json")
    # Should not raise; corrupted file treated as empty.
    assert not state.is_reviewed(state_dir, "o", "r", 1, "sha")


def test_read_file_wrong_type_returns_empty(state_dir):
    path = _reviewed_path(state_dir)
    with open(path, "w") as f:
        json.dump("a string, not a dict or list", f)
    assert not state.is_reviewed(state_dir, "o", "r", 1, "sha")


def test_read_file_migrates_old_list_format(state_dir):
    path = _reviewed_path(state_dir)
    old_format = [
        {"key": "org/repo/1/sha1", "reviewed_at": "2026-01-01T00:00:00Z"},
        {"key": "org/repo/2/sha2", "reviewed_at": "2026-01-02T00:00:00Z"},
    ]
    with open(path, "w") as f:
        json.dump(old_format, f)
    assert state.is_reviewed(state_dir, "org", "repo", 1, "sha1")
    assert state.is_reviewed(state_dir, "org", "repo", 2, "sha2")
    assert not state.is_reviewed(state_dir, "org", "repo", 3, "sha3")


def test_read_file_migrates_list_entry_without_key(state_dir):
    path = _failures_path(state_dir)
    old_format = [
        {"key": "org/repo/1/sha", "failure_count": 2, "last_failed_at": "2026-01-01T00:00:00Z"},
        {"failure_count": 1},  # no "key" — should be dropped, not crash
    ]
    with open(path, "w") as f:
        json.dump(old_format, f)
    assert state.get_failure_count(state_dir, "org", "repo", 1, "sha") == 2


# ── atomic write ─────────────────────────────────────────────────────────────

def test_atomic_write_no_tmp_file_left(state_dir):
    state.mark_reviewed(state_dir, "org", "repo", 1, "sha")
    tmp = _reviewed_path(state_dir) + ".tmp"
    assert not os.path.exists(tmp)


# ── cleanup ───────────────────────────────────────────────────────────────────

def test_cleanup_removes_old_reviewed_entries(state_dir):
    path = _reviewed_path(state_dir)
    data = {
        "org/repo/1/old": {"reviewed_at": "2020-01-01T00:00:00Z"},
        "org/repo/2/recent": {"reviewed_at": "2099-01-01T00:00:00Z"},
    }
    with open(path, "w") as f:
        json.dump(data, f)

    state.cleanup(state_dir, days=30)

    result = json.loads(open(path).read())
    assert "org/repo/1/old" not in result
    assert "org/repo/2/recent" in result


def test_cleanup_removes_old_failure_entries(state_dir):
    path = _failures_path(state_dir)
    data = {
        "org/repo/1/old": {"failure_count": 2, "last_failed_at": "2020-01-01T00:00:00Z"},
        "org/repo/2/recent": {"failure_count": 1, "last_failed_at": "2099-01-01T00:00:00Z"},
    }
    with open(path, "w") as f:
        json.dump(data, f)

    state.cleanup(state_dir, days=30)

    result = json.loads(open(path).read())
    assert "org/repo/1/old" not in result
    assert "org/repo/2/recent" in result


def test_cleanup_on_missing_files_does_not_crash(state_dir):
    state.cleanup(state_dir, days=30)  # both files absent — should not raise
