import json
import logging
import subprocess
import threading
import time

log = logging.getLogger("github")
RATE_LIMIT_THRESHOLD = 100
_RATE_LIMIT_TTL = 45  # seconds between API refreshes in the normal (above-threshold) case

_rate_limit_lock = threading.Lock()
_rate_limit_remaining: int | None = None
_rate_limit_expires: float = 0.0


def _gh(*args: str, input: str | None = None, **run_kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True,
        input=input,
        **run_kwargs,
    )
    if result.returncode != 0:
        log.error("gh %s failed (exit %d):\n%s", " ".join(args), result.returncode, result.stderr)
        result.check_returncode()
    return result


def check_rate_limit() -> None:
    global _rate_limit_remaining, _rate_limit_expires

    sleep_secs = 0

    with _rate_limit_lock:
        now = time.time()
        # Cache hit: skip the API call when remaining is above threshold and cache is fresh.
        if (now < _rate_limit_expires
                and _rate_limit_remaining is not None
                and _rate_limit_remaining >= RATE_LIMIT_THRESHOLD):
            return
        try:
            data = json.loads(_gh("api", "rate_limit").stdout)
            _rate_limit_remaining = data["rate"]["remaining"]
            _rate_limit_expires = now + _RATE_LIMIT_TTL
        except Exception as e:
            log.warning("Rate limit check failed: %s", e)
            return
        if _rate_limit_remaining < RATE_LIMIT_THRESHOLD:
            sleep_secs = max(0, data["rate"]["reset"] - int(time.time()) + 5)
            log.warning("Rate limit low (%d remaining). Sleeping %ds.", _rate_limit_remaining, sleep_secs)

    if sleep_secs:
        time.sleep(sleep_secs)


def list_open_prs(org: str, repo: str, ignore_drafts: bool = True) -> list[dict]:
    check_rate_limit()
    prs = json.loads(_gh("api", f"repos/{org}/{repo}/pulls?state=open&per_page=100").stdout)
    if ignore_drafts:
        prs = [p for p in prs if not p.get("draft", False)]
    if len(prs) == 100:
        log.warning(
            "list_open_prs: got exactly 100 PRs for %s/%s — pagination not implemented, some PRs may be missed.",
            org, repo,
        )
    return [
        {"number": p["number"], "head_sha": p["head"]["sha"], "title": p["title"]}
        for p in prs
    ]


def should_skip_pr(org: str, repo: str, pr_number: int, ignore_labels: list[str]) -> bool:
    if not ignore_labels:
        return False
    check_rate_limit()
    pr = json.loads(_gh("api", f"repos/{org}/{repo}/pulls/{pr_number}").stdout)
    pr_label_names = {lbl["name"] for lbl in pr.get("labels", [])}
    matched = pr_label_names & set(ignore_labels)
    if matched:
        log.debug("PR #%d has skip label(s): %s", pr_number, matched)
    return bool(matched)


def get_pr_diff(org: str, repo: str, pr_number: int) -> str:
    check_rate_limit()
    return _gh("pr", "diff", str(pr_number), "--repo", f"{org}/{repo}").stdout


def get_prior_reviews(org: str, repo: str, pr_number: int) -> list[dict]:
    check_rate_limit()
    reviews = json.loads(_gh("api", f"repos/{org}/{repo}/pulls/{pr_number}/reviews").stdout)
    return [r for r in reviews if r.get("body", "").strip()]


def post_review(
    org: str, repo: str, pr_number: int, body: str,
    comments: list[dict] | None = None,
) -> None:
    check_rate_limit()
    payload = {"body": body, "event": "COMMENT"}
    if comments:
        payload["comments"] = comments
    _gh(
        "api", f"repos/{org}/{repo}/pulls/{pr_number}/reviews",
        "-X", "POST", "--input", "-",
        input=json.dumps(payload),
    )
