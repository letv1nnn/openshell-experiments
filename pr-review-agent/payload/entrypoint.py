#!/usr/bin/env python3
"""Main polling loop for the PR review agent."""
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    stream=sys.stdout,
    level=_log_level,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("entrypoint")

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(PAYLOAD_DIR, "lib"))

from config import load_config
from github import list_open_prs, should_skip_pr, get_prior_reviews
from repos import clone_or_fetch
from state import (
    cleanup,
    get_failure_count,
    is_reviewed,
    mark_reviewed,
    record_failure,
    retry_cap_exceeded,
)

STATE_DIR = "/sandbox/pr-review-agent/state"
REPOS_BASE = "/sandbox/pr-review-agent/repos"
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat")

# Uploaded config lands in the writable /sandbox mount; the image bakes a copy
# under read-only /app. Prefer the uploaded one, fall back to the baked default.
CONFIG_PRIMARY = "/sandbox/pr-review-agent/config.yaml"
CONFIG_FALLBACK = "/app/pr-review-agent/config.yaml"
CONFIG_PATH = CONFIG_PRIMARY if os.path.exists(CONFIG_PRIMARY) else CONFIG_FALLBACK

in_flight: set[tuple] = set()
in_flight_lock = threading.Lock()

# Tracks keys that have already had the sandbox-restart heal check performed
# so we only call get_prior_reviews once per (org, repo, pr, sha) key.
_heal_checked: set[tuple] = set()


def handle_signal(sig, frame):
    log.info("Received signal %d, shutting down.", sig)
    sys.exit(0)


signal.signal(signal.SIGTERM, handle_signal)


def run_review_subprocess(
    org: str, repo: str, pr_number: int, head_sha: str, review_timeout: int,
    log_prefix: str = "review",
) -> bool:
    try:
        proc = subprocess.Popen(
            ["python3", os.path.join(PAYLOAD_DIR, "review.py"),
             org, repo, str(pr_number), head_sha],
            stdout=None,  # inherit — review.py logs flow straight to pod stdout in real time
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={**os.environ, "REPOS_BASE": REPOS_BASE,
                 "REVIEW_LOG_PREFIX": log_prefix,
                 "LOG_LEVEL": os.environ.get("LOG_LEVEL", "INFO")},
        )
        try:
            _, stderr = proc.communicate(timeout=review_timeout + 30)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            log.error("Review subprocess timed out for %s/%s#%s.", org, repo, pr_number)
            return False
        if proc.returncode != 0:
            if stderr:
                log.error("[review stderr]\n%s", stderr)
            return False
        return True
    except Exception as e:
        log.error("Review subprocess error for %s/%s#%s: %s", org, repo, pr_number, e)
        return False


def _review_and_record(
    key: tuple, org: str, repo: str, pr_number: int, head_sha: str,
    review_timeout: int, max_retries: int,
) -> None:
    log_prefix = f"{org}/{repo}#{pr_number}"
    try:
        ok = run_review_subprocess(org, repo, pr_number, head_sha, review_timeout, log_prefix)
        if ok:
            mark_reviewed(STATE_DIR, org, repo, pr_number, head_sha)
            log.info("OK %s/%s#%s reviewed.", org, repo, pr_number)
        else:
            record_failure(STATE_DIR, org, repo, pr_number, head_sha)
            count = get_failure_count(STATE_DIR, org, repo, pr_number, head_sha)
            log.error(
                "ERROR %s/%s#%s: review failed. Attempt %d/%d.",
                org, repo, pr_number, count, max_retries,
            )
    finally:
        with in_flight_lock:
            in_flight.discard(key)


def _poll_one_repo(r: dict, review_timeout: int, max_retries: int) -> None:
    org = r["org"]
    repo = r["repo"]
    ignore_drafts = r.get("ignore_drafts", True)
    ignore_labels = r.get("ignore_labels", [])

    log.info("Polling %s/%s...", org, repo)

    try:
        clone_or_fetch(org, repo, REPOS_BASE)
    except Exception as e:
        log.error("Fetch failed for %s/%s: %s", org, repo, e)
        return

    try:
        prs = list_open_prs(org, repo, ignore_drafts)
    except Exception as e:
        log.error("list_open_prs failed for %s/%s: %s", org, repo, e)
        return

    if not prs:
        log.info("No open PRs in %s/%s.", org, repo)
        return

    for pr in prs:
        pr_number = pr["number"]
        head_sha = pr["head_sha"]
        pr_title = pr["title"]

        if is_reviewed(STATE_DIR, org, repo, pr_number, head_sha):
            log.debug("SKIP %s/%s#%s: already reviewed at %s",
                      org, repo, pr_number, head_sha[:8])
            continue

        if retry_cap_exceeded(STATE_DIR, org, repo, pr_number, head_sha, max_retries):
            log.debug("SKIP %s/%s#%s: retry cap exceeded for %s",
                      org, repo, pr_number, head_sha[:8])
            continue

        try:
            if should_skip_pr(org, repo, pr_number, ignore_labels):
                log.info("SKIP %s/%s#%s: ignored label.", org, repo, pr_number)
                continue
        except Exception as e:
            log.warning("Label check failed for %s/%s#%s: %s", org, repo, pr_number, e)

        # Heal state after sandbox restart: if the agent already posted a review
        # on this exact SHA (matched by the authenticated GitHub login, not just
        # any reviewer), mark it locally and skip without re-posting.
        # Only checked once per key to avoid burning API rate limit every cycle.
        key = (org, repo, pr_number, head_sha)
        if key not in _heal_checked:
            _heal_checked.add(key)
            try:
                prior = get_prior_reviews(org, repo, pr_number)
                if BOT_LOGIN and any(
                    rev.get("commit_id") == head_sha
                    and rev.get("user", {}).get("login") == BOT_LOGIN
                    for rev in prior
                ):
                    log.info(
                        "SKIP %s/%s#%s: agent review already posted at %s — healing state.",
                        org, repo, pr_number, head_sha[:8],
                    )
                    mark_reviewed(STATE_DIR, org, repo, pr_number, head_sha)
                    continue
            except Exception as e:
                log.warning("Prior review check failed for %s/%s#%s: %s — proceeding.",
                            org, repo, pr_number, e)

        with in_flight_lock:
            if key in in_flight:
                log.debug("SKIP %s/%s#%s: review already in flight.", org, repo, pr_number)
                continue
            in_flight.add(key)

        log.info("Submitting review %s/%s#%s: %s", org, repo, pr_number, pr_title)

        executor.submit(
            _review_and_record,
            key, org, repo, pr_number, head_sha,
            review_timeout, max_retries,
        )


# ── Init ──────────────────────────────────────────────────────────────────────

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(REPOS_BASE, exist_ok=True)

log.info("PR Review Agent starting.")

auth_check = subprocess.run(["gh", "auth", "status"], capture_output=True)
if auth_check.returncode != 0:
    log.error(
        "gh auth failed. Ensure the GitHub provider is attached to this sandbox.\n%s",
        auth_check.stderr.decode(errors="replace"),
    )
    sys.exit(1)
log.info("GitHub auth: OK")

try:
    _whoami = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True, check=True,
    )
    BOT_LOGIN: str | None = _whoami.stdout.strip()
    log.info("GitHub identity: @%s", BOT_LOGIN)
except Exception as _e:
    log.warning("Could not resolve GitHub identity: %s — heal check will skip all reviews.", _e)
    BOT_LOGIN = None

try:
    config = load_config(CONFIG_PATH)
except (ValueError, FileNotFoundError) as e:
    log.error("Config error: %s", e)
    sys.exit(1)

repos = config.get("repos", [])
polling_interval = config.get("polling_interval_seconds", 120)
review_cfg = config.get("review_settings", {})
max_concurrent = review_cfg.get("max_concurrent_reviews", 5)

executor = ThreadPoolExecutor(max_workers=max_concurrent)

log.info("Performing initial clone of %d repo(s)...", len(repos))
for r in repos:
    try:
        clone_or_fetch(r["org"], r["repo"], REPOS_BASE)
    except Exception as e:
        log.warning("Initial clone failed for %s/%s: %s", r["org"], r["repo"], e)

# ── Main polling loop ─────────────────────────────────────────────────────────

log.info("Starting polling loop (interval: %ds).", polling_interval)

while True:
    try:
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except OSError as e:
        log.warning("Failed to write heartbeat: %s", e)

    try:
        config = load_config(CONFIG_PATH)
    except Exception as e:
        log.error("Config reload failed: %s — continuing with previous config.", e)

    repos = config.get("repos", [])
    review_cfg = config.get("review_settings", {})
    polling_interval = config.get("polling_interval_seconds", 120)
    review_timeout = review_cfg.get("review_timeout_seconds", 600)
    max_retries = review_cfg.get("max_retries", 3)

    # Fan out polling across repos in parallel so a slow git fetch on one repo
    # doesn't delay the others.  Each repo is independent; shared state
    # (in_flight, _heal_checked, executor) is already protected or benign-racy.
    with ThreadPoolExecutor(max_workers=len(repos) or 1) as poll_pool:
        poll_futures = {
            poll_pool.submit(_poll_one_repo, r, review_timeout, max_retries): r
            for r in repos
        }
        for fut in as_completed(poll_futures):
            r = poll_futures[fut]
            try:
                fut.result()
            except Exception as e:
                log.error("Unexpected error polling %s/%s: %s", r["org"], r["repo"], e)

    cleanup(STATE_DIR, days=30)
    log.info("Cycle complete. Sleeping %ds...", polling_interval)
    time.sleep(polling_interval)
