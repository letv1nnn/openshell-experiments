# Plan V3 — Parallel Reviews

## Goal

Remove the sequential bottleneck. Currently the polling loop reviews one PR at a time — 5 open PRs means the last one waits behind 4 others, each up to 10 minutes. V3 fans out review subprocesses concurrently with a configurable cap.

## What changes

### 1. `config.yaml` — new fields

```yaml
review_settings:
  max_concurrent_reviews: 5
  max_prior_reviews: 3
```

`max_prior_reviews` caps how many prior reviews are injected into the context. Currently `build_context()` in `review.py` takes the last 5 unconditionally. On a long-lived PR with many review cycles this grows unbounded, burning context on old feedback the model doesn't need. The most recent N reviews are the relevant ones — older cycles are already addressed or carry forward as inline comments. Default of 3 is a reasonable starting point; set to 0 to disable prior review injection entirely.

### 2. `payload/entrypoint.py` — ThreadPoolExecutor

Replace the inner `for pr in prs:` loop with a `ThreadPoolExecutor`. Each PR gets submitted as a future; the executor caps concurrency at `max_concurrent_reviews`.

Key points:

- Track in-flight `(org, repo, pr_number, sha)` in a shared set so a slow cycle doesn't double-submit the same PR if the next poll fires before the previous review finishes.
- `run_review_subprocess` is already isolated (separate process, separate env) so no shared mutable state between workers beyond the in-flight set and the state JSON files.
- State writes in `state.py` use `os.replace()` (atomic) — safe for concurrent writers targeting different keys. Two reviews for the same PR/SHA at the same time is prevented by the in-flight set.
- The executor should be created once outside the loop and reused across cycles, not recreated each iteration.

Rough shape:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

executor = ThreadPoolExecutor(max_workers=max_concurrent_reviews)
in_flight: set[tuple] = set()

# inside poll cycle, per PR:
key = (org, repo, pr_number, head_sha)
if key in in_flight:
    continue
in_flight.add(key)

def review_and_record(key, ...):
    try:
        ok = run_review_subprocess(...)
        if ok:
            mark_reviewed(...)
        else:
            record_failure(...)
    finally:
        in_flight.discard(key)

executor.submit(review_and_record, key, ...)
```

### 3. Log readability — subprocess prefix

With multiple reviews running concurrently, stdout lines from different subprocesses will interleave. Pass `[org/repo#N]` as a prefix into `review.py` (via env var or argv) and have `review.py` include it in every log line.

Simplest approach: set `REVIEW_LOG_PREFIX=org/repo#N` in the subprocess env and have `review.py` include it in the logger name:

```python
prefix = os.environ.get("REVIEW_LOG_PREFIX", "review")
log = logging.getLogger(prefix)
```

This makes log lines like:
```
2026-07-07T10:48:00Z INFO [Bobbins228/openshell-experiments#4] Running OpenCode...
2026-07-07T10:48:00Z INFO [Bobbins228/openshell-experiments#7] Fetching diff...
```

## Files changed

| File | Change |
|---|---|
| `config.yaml` | Add `max_concurrent_reviews: 5`, `max_prior_reviews: 3` |
| `payload/entrypoint.py` | `ThreadPoolExecutor`; in-flight set; submit per-PR futures |
| `payload/review.py` | `REVIEW_LOG_PREFIX` env var → logger name; pass `max_prior_reviews` to `build_context()` |

## Known limitations after V3

- Reviews for different PRs on the same repo run concurrently. `clone_or_fetch` in the main thread keeps the local clone fresh, but if a review subprocess happens to read `CONTRIBUTING.md` mid-fetch there is a small TOCTOU window. In practice negligible — file reads are fast and fetches are infrequent.
- Rate limit budget is shared across all concurrent reviews. With 5 parallel `gh` callers the `RATE_LIMIT_THRESHOLD` of 100 may need raising to give more headroom before throttling kicks in.
