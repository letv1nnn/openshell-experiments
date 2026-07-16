import datetime
import json
import logging
import os

log = logging.getLogger("state")


def _state_key(org: str, repo: str, pr_number: int, head_sha: str) -> str:
    return f"{org}/{repo}/{pr_number}/{head_sha}"


def _read_file(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            # Migrate from old list format: [{key, ...fields}, ...]
            return {e["key"]: {k: v for k, v in e.items() if k != "key"}
                    for e in data if "key" in e}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        log.error("State file corrupted, resetting: %s (%s)", path, e)
        return {}


def _write_file(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def is_reviewed(state_dir: str, org: str, repo: str, pr_number: int, head_sha: str) -> bool:
    path = os.path.join(state_dir, "reviewed.json")
    return _state_key(org, repo, pr_number, head_sha) in _read_file(path)


def mark_reviewed(state_dir: str, org: str, repo: str, pr_number: int, head_sha: str) -> None:
    path = os.path.join(state_dir, "reviewed.json")
    key = _state_key(org, repo, pr_number, head_sha)
    data = _read_file(path)
    data[key] = {"reviewed_at": _now()}
    _write_file(path, data)


def record_failure(state_dir: str, org: str, repo: str, pr_number: int, head_sha: str) -> None:
    path = os.path.join(state_dir, "failures.json")
    key = _state_key(org, repo, pr_number, head_sha)
    data = _read_file(path)
    ts = _now()
    if key in data:
        data[key]["failure_count"] = data[key].get("failure_count", 0) + 1
        data[key]["last_failed_at"] = ts
    else:
        data[key] = {"failure_count": 1, "last_failed_at": ts}
    _write_file(path, data)


def retry_cap_exceeded(
    state_dir: str, org: str, repo: str, pr_number: int, head_sha: str, max_retries: int = 3
) -> bool:
    """Returns True when failure count >= max_retries (PR should be skipped)."""
    return get_failure_count(state_dir, org, repo, pr_number, head_sha) >= max_retries


def get_failure_count(
    state_dir: str, org: str, repo: str, pr_number: int, head_sha: str
) -> int:
    path = os.path.join(state_dir, "failures.json")
    key = _state_key(org, repo, pr_number, head_sha)
    entry = _read_file(path).get(key)
    return entry.get("failure_count", 0) if entry else 0


def cleanup(state_dir: str, days: int = 30) -> None:
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    for filename, date_field in [
        ("reviewed.json", "reviewed_at"),
        ("failures.json", "last_failed_at"),
    ]:
        path = os.path.join(state_dir, filename)
        if not os.path.exists(path):
            continue
        data = _read_file(path)
        cleaned = {k: v for k, v in data.items() if v.get(date_field, "") >= cutoff}
        _write_file(path, cleaned)
