"""Tests for payload/lib/github.py — subprocess calls are mocked throughout."""
import json
import threading
import time
from subprocess import CompletedProcess
from unittest.mock import MagicMock, call, patch

import pytest

import github as gh_mod


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cp(stdout: str = "", returncode: int = 0) -> CompletedProcess:
    cp = MagicMock(spec=CompletedProcess)
    cp.stdout = stdout
    cp.stderr = ""
    cp.returncode = returncode
    return cp


def _rate_limit_response(remaining: int = 5000, reset_in: int = 3600) -> str:
    return json.dumps({
        "rate": {
            "limit": 5000,
            "remaining": remaining,
            "reset": int(time.time()) + reset_in,
        }
    })


def _reset_rate_cache():
    """Reset module-level rate limit cache between tests."""
    gh_mod._rate_limit_remaining = None
    gh_mod._rate_limit_expires = 0.0


# ── check_rate_limit: caching ─────────────────────────────────────────────────

def test_rate_limit_first_call_hits_subprocess():
    _reset_rate_cache()
    with patch("github.subprocess.run", return_value=_make_cp(_rate_limit_response())) as mock_run:
        gh_mod.check_rate_limit()
    assert mock_run.called


def test_rate_limit_second_call_within_ttl_no_subprocess():
    _reset_rate_cache()
    response = _make_cp(_rate_limit_response(remaining=5000))
    with patch("github.subprocess.run", return_value=response) as mock_run:
        gh_mod.check_rate_limit()
        gh_mod.check_rate_limit()
    assert mock_run.call_count == 1


def test_rate_limit_cache_expires_after_ttl():
    _reset_rate_cache()
    response = _make_cp(_rate_limit_response(remaining=5000))
    with patch("github.subprocess.run", return_value=response) as mock_run:
        gh_mod.check_rate_limit()
        # Manually expire the cache.
        gh_mod._rate_limit_expires = time.time() - 1
        gh_mod.check_rate_limit()
    assert mock_run.call_count == 2


def test_rate_limit_always_rechecks_when_below_threshold():
    _reset_rate_cache()
    response = _make_cp(_rate_limit_response(remaining=50, reset_in=1))
    with patch("github.subprocess.run", return_value=response), \
         patch("github.time.sleep") as mock_sleep:
        gh_mod.check_rate_limit()
    mock_sleep.assert_called_once()
    sleep_arg = mock_sleep.call_args[0][0]
    assert sleep_arg > 0


def test_rate_limit_no_sleep_when_above_threshold():
    _reset_rate_cache()
    response = _make_cp(_rate_limit_response(remaining=5000))
    with patch("github.subprocess.run", return_value=response), \
         patch("github.time.sleep") as mock_sleep:
        gh_mod.check_rate_limit()
    mock_sleep.assert_not_called()


def test_rate_limit_check_fails_gracefully():
    _reset_rate_cache()
    bad = _make_cp(stdout="{invalid", returncode=0)
    with patch("github.subprocess.run", return_value=bad):
        gh_mod.check_rate_limit()  # Should not raise.


# ── check_rate_limit: thread safety ──────────────────────────────────────────

def test_rate_limit_concurrent_calls_single_subprocess():
    _reset_rate_cache()
    call_count = 0

    def slow_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        time.sleep(0.05)
        return _make_cp(_rate_limit_response(remaining=5000))

    with patch("github.subprocess.run", side_effect=slow_run):
        threads = [threading.Thread(target=gh_mod.check_rate_limit) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # All threads raced to the lock; only the first should have called subprocess.
    assert call_count == 1


# ── list_open_prs ─────────────────────────────────────────────────────────────

def _make_pr(number: int, draft: bool = False) -> dict:
    return {
        "number": number,
        "draft": draft,
        "head": {"sha": f"sha{number}"},
        "title": f"PR #{number}",
    }


def test_list_open_prs_returns_prs():
    _reset_rate_cache()
    prs = [_make_pr(1), _make_pr(2)]
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(prs))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]):
        result = gh_mod.list_open_prs("org", "repo")

    assert len(result) == 2
    assert result[0]["number"] == 1
    assert result[0]["head_sha"] == "sha1"


def test_list_open_prs_filters_drafts_when_ignore_drafts_true():
    _reset_rate_cache()
    prs = [_make_pr(1, draft=False), _make_pr(2, draft=True)]
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(prs))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]):
        result = gh_mod.list_open_prs("org", "repo", ignore_drafts=True)

    assert len(result) == 1
    assert result[0]["number"] == 1


def test_list_open_prs_includes_drafts_when_ignore_drafts_false():
    _reset_rate_cache()
    prs = [_make_pr(1, draft=True)]
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(prs))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]):
        result = gh_mod.list_open_prs("org", "repo", ignore_drafts=False)

    assert len(result) == 1


def test_list_open_prs_warns_at_100(caplog):
    _reset_rate_cache()
    import logging
    prs = [_make_pr(i) for i in range(100)]
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(prs))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]), \
         caplog.at_level(logging.WARNING, logger="github"):
        gh_mod.list_open_prs("org", "repo")

    assert any("100" in msg for msg in caplog.messages)


def test_list_open_prs_no_warning_below_100(caplog):
    _reset_rate_cache()
    import logging
    prs = [_make_pr(i) for i in range(5)]
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(prs))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]), \
         caplog.at_level(logging.WARNING, logger="github"):
        gh_mod.list_open_prs("org", "repo")

    pagination_warns = [m for m in caplog.messages if "pagination" in m.lower()]
    assert not pagination_warns


# ── should_skip_pr ────────────────────────────────────────────────────────────

def test_should_skip_pr_empty_ignore_labels_no_api_call():
    _reset_rate_cache()
    with patch("github.subprocess.run") as mock_run:
        result = gh_mod.should_skip_pr("org", "repo", 1, ignore_labels=[])
    mock_run.assert_not_called()
    assert result is False


def test_should_skip_pr_matching_label_returns_true():
    _reset_rate_cache()
    pr = {"labels": [{"name": "do-not-review"}, {"name": "wip"}]}
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(pr))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]):
        result = gh_mod.should_skip_pr("org", "repo", 1, ignore_labels=["do-not-review"])

    assert result is True


def test_should_skip_pr_no_matching_label_returns_false():
    _reset_rate_cache()
    pr = {"labels": [{"name": "enhancement"}]}
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(pr))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]):
        result = gh_mod.should_skip_pr("org", "repo", 1, ignore_labels=["do-not-review"])

    assert result is False


def test_should_skip_pr_no_labels_field_returns_false():
    _reset_rate_cache()
    pr = {}  # no "labels" key
    rate_resp = _make_cp(_rate_limit_response())
    pr_resp = _make_cp(json.dumps(pr))

    with patch("github.subprocess.run", side_effect=[rate_resp, pr_resp]):
        result = gh_mod.should_skip_pr("org", "repo", 1, ignore_labels=["wip"])

    assert result is False
