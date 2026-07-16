# PR Review Agent — How It Works

## What it is

A long-running Python daemon inside an OpenShell sandbox on OpenShift. It watches GitHub repos for open PRs, generates senior-engineer-level code reviews using Claude (routed through Vertex AI), and posts them back to GitHub as native GitHub Reviews with inline diff comments. No human in the loop.

---

## OpenShell's role

OpenShell is the security boundary. It wraps the agent in a hardened Linux sandbox with three enforcement layers:

**Landlock** — filesystem isolation. The agent can only read/write paths OpenShell allows. `/sandbox` is the runtime volume (state, repos). `/app` is read-only code baked into the image.

**seccomp** — syscall filtering. Prevents the agent (or any subprocess it spawns) from making unexpected kernel calls.

**L7 network policy** (`policy.yaml`) — the most important layer for this agent. OpenShell intercepts all outbound TCP and enforces per-binary, per-host, per-method/path rules:

| Binary | Host | What's allowed |
|---|---|---|
| `gh` | `api.github.com:443` | `GET /repos/**` (read PRs, diffs, reviews), `POST` to the reviews endpoint |
| `git` | `github.com:443` | clone and fetch only — no push |
| `opencode` | `opencode.ai:443` | telemetry (audit mode — logged but not blocked) |

The real GitHub token never enters the sandbox. The OpenShell proxy intercepts outgoing `gh` requests and transparently substitutes the real PAT. The sandbox holds only a proxy credential. Inference works the same way: OpenCode talks to `https://inference.local/v1`, which the OpenShell gateway intercepts and routes to Vertex AI / Claude. No raw API keys inside the sandbox.

---

## Image and payload layout

The Containerfile builds on UBI9 Python 3.11 and installs `git`, the `gh` CLI, and a pinned version of OpenCode. Two paths matter at runtime:

- `/app/payload/` — agent code (`entrypoint.py`, `review.py`, `lib/`, `prompts/`)
- `/app/pr-review-agent/config.yaml` — baked-in config (repos to watch, review settings)
- `/sandbox/` — volume-mounted by OpenShell at runtime; holds mutable state and cloned repos

---

## Payload files

### `entrypoint.py` — main loop

Runs as PID 1 in the sandbox. Startup sequence:

1. Verifies `gh auth status` — exits hard if the GitHub provider isn't attached.
2. Calls `gh api user --jq .login` to resolve `BOT_LOGIN` — the authenticated GitHub identity used in the heal check.
3. Loads `config.yaml`.
4. Does an initial `git clone --depth=1` of every watched repo into `/sandbox/pr-review-agent/repos/`.
5. Enters the main polling loop.

Each loop iteration:

- Writes a heartbeat timestamp to `/sandbox/pr-review-agent/state/heartbeat`.
- Reloads config (picks up changes without restart).
- Fans out repo polling across a `ThreadPoolExecutor` — one task per repo — so a slow `git fetch` on one repo doesn't stall the others.
- Calls `state.cleanup()` to prune state entries older than 30 days.
- Sleeps for `polling_interval_seconds`.

**`_poll_one_repo()`** runs the per-repo skip gauntlet for each open PR:

1. **Already reviewed** — `is_reviewed()` checks `reviewed.json` for `org/repo/pr_number/head_sha`.
2. **Retry cap** — `retry_cap_exceeded()` skips PRs that have failed `max_retries` consecutive times at the same SHA.
3. **Ignored labels** — `should_skip_pr()` fetches PR labels and checks against `ignore_labels`.
4. **Heal check** — first time this `(org, repo, pr_number, head_sha)` tuple is seen in the current process, fetches prior reviews from GitHub and checks whether any have `commit_id == head_sha` AND `user.login == BOT_LOGIN`. If found, local state is rebuilt and the PR is skipped. Handles sandbox restarts without double-posting. Human reviews on the same commit do not suppress the agent.
5. **In-flight** — `in_flight` set (protected by a lock) prevents submitting the same PR twice across overlapping poll cycles.

If all checks pass, the PR is submitted to the persistent `ThreadPoolExecutor` as `_review_and_record()`.

---

### `review.py` — per-PR review orchestration

Runs as a **subprocess** of `entrypoint.py` with `start_new_session=True`, placing it in its own process group. OpenCode itself spawns background children (language servers, file watchers). Writing OpenCode's stdout/stderr to files rather than pipes ensures `proc.wait()` returns when OpenCode exits — not when every background child finally closes its inherited pipe fd. After `wait()` returns, `killpg()` reaps any lingering children.

**`run_review()` steps:**

1. Fetch PR metadata (`title`, `body`, `additions`, `deletions`, `changedFiles`).
2. Check size limits — post an explanatory comment and skip if diff exceeds `max_diff_lines` or `max_files_changed`.
3. Fetch the raw unified diff via `gh pr diff`.
4. Fetch prior reviews for continuity context.
5. Fetch the PR head ref (`refs/pull/N/head`) with `git fetch --depth=1` to get `FETCH_HEAD` pointing at the exact post-PR tree.
6. Run cross-file symbol analysis (see below).
7. Annotate the diff and invoke OpenCode.
8. Parse output and post the review.

#### Cross-file symbol analysis

Before calling the model, the agent greps the live PR tree for usages of any public symbol that was renamed or removed in the diff:

- **`extract_changed_symbols()`** scans `-` (removed) lines for public symbol definitions — `def`/`class` in Python, exported `func`/`type` in Go, `export function/class/const` in TypeScript/JS, `pub fn/struct/enum/trait` in Rust. Private and unexported symbols are excluded.
- **`find_related_files()`** runs `git grep -wn {FETCH_HEAD} -- {symbol}` for each symbol, collecting `(file, line_number)` hits in files *not* in the diff. Test files are deprioritised.
- **`_extract_snippets()`** pulls file content from the git tree, extracts ±10 lines around each match, merges nearby ranges (gap ≤ 3 lines), and caps at `max_related_lines_per_file`. A global `max_related_lines` cap (~300 lines / ~2500 tokens) is enforced across all files.

The result is injected into the context file as a `## Related files` section, telling Claude exactly which external files call the changed symbols and on which lines.

#### Diff annotation

`_process_diff()` makes a single pass over the raw unified diff. Every context and addition line gets a `[N]` prefix with its new-file line number. Deleted lines get `[---]`. This gives the model explicit line number anchors it can read directly rather than counting lines itself. The function simultaneously builds `valid_right_lines`: the set of `(file, line_number)` pairs eligible for RIGHT-side inline comments.

#### OpenCode invocation

```
opencode run --model anthropic/claude-sonnet-4-6
```

The full prompt — instructions, context (PR description, CONTRIBUTING.md, prior reviews, cross-file snippets), and the annotated diff — is assembled in memory and written to OpenCode's stdin via a daemon thread. Using stdin rather than `-f` file attachments means all content is present in the model's initial context without requiring tool calls to read files. This also means `--auto` is not needed, keeping OpenShell's permission enforcement intact.

`ANTHROPIC_BASE_URL=https://inference.local/v1` routes inference through the OpenShell gateway to Vertex AI. A heartbeat thread logs progress every 30 seconds. If OpenCode doesn't exit within `review_timeout_seconds`, `killpg()` kills the entire process group.

#### Output parsing and comment placement

**`_parse_output()`** splits at the `<!-- FINDINGS` sentinel. Everything before it is the prose summary. The JSON array inside is the findings list (`file`, `line`, `severity`, `body`).

**`_split_findings()`** places each finding:
- **Exact match** in `valid_right_lines` → inline comment at that line.
- **Near miss** → snapped to the nearest valid line in the same file. Logged as a warning.
- **File not in diff** → appended as a prose bullet in the review body.

**`post_review()`** makes a single GitHub Reviews API call with the prose body and full inline comments array — atomic, either everything posts or nothing does.

---

### `lib/state.py` — durable state

Two JSON files, both written atomically via `os.replace()` on a `.tmp` file:

- `reviewed.json` — keyed by `org/repo/pr_number/head_sha`. Present = successfully reviewed. New SHA = new key = fresh review.
- `failures.json` — same key shape, stores `failure_count` and `last_failed_at`. Once `failure_count >= max_retries`, the PR is skipped until a new commit is pushed.

Both files handle migration from an older list format and silently reset on JSON corruption.

### `lib/github.py` — GitHub API wrapper

All GitHub calls go through `gh` CLI subprocesses. `check_rate_limit()` is called before every operation: it caches the remaining count with a 45-second TTL and is protected by a lock so concurrent threads don't each fire a subprocess — only the first caller through the lock makes the API call, the rest return immediately on the cached value.

### `lib/repos.py` — git mirrors

`clone_or_fetch()` maintains shallow mirrors under `/sandbox/pr-review-agent/repos/{org}/{repo}/`. Initial clone is `--depth=1`; subsequent fetches are `fetch --prune --depth=1 origin` followed by `checkout FETCH_HEAD`.

---

## Review prompt (`prompts/review-system.md`)

Instructs Claude to produce:

1. **Previous Review Follow-up** *(if prior reviews exist)* — explicitly states which earlier findings were addressed, partially addressed, or still open.
2. **Summary** — what the PR does and overall assessment. Max 150 words.
3. **Testing Gaps** *(optional)* — prose only, for defects without a specific diff line.
4. `<!-- FINDINGS [...] -->` — JSON array of inline findings with `file`, `line`, `severity`, `body`.

Key model instructions:
- Read `[N]` line numbers directly from the bracket annotation — do not count lines.
- Only reference lines that appear in the diff.
- Cross-file broken callers (from the Related files section) go in prose only — they have no diff line and cannot be inline comments.

---

## End-to-end flow

```
Developer pushes a commit to a PR branch
         │
         ▼  (up to polling_interval_seconds later)
entrypoint.py: list_open_prs() detects new head_sha
         │
         ├─ is_reviewed()? → no (not in reviewed.json)
         ├─ retry_cap_exceeded()? → no
         ├─ should_skip_pr() (ignored labels)? → no
         ├─ heal check (BOT_LOGIN + SHA match in prior reviews)? → no
         └─ in_flight? → no → add to in_flight, submit to ThreadPoolExecutor
                  │
                  ▼  (runs concurrently with other PR reviews)
         _review_and_record() → run_review_subprocess()
                  │
                  ▼
         review.py subprocess (own process group, start_new_session=True)
          1. gh pr view → metadata, size check
          2. gh pr diff → raw unified diff
          3. get_prior_reviews → prior review context
          4. git fetch refs/pull/N/head → FETCH_HEAD
          5. extract_changed_symbols(diff) → public symbols removed/renamed
          6. git grep FETCH_HEAD -- {symbols} → usages in files outside the diff
          7. _extract_snippets() from git tree → related_context injected into context.md
          8. _process_diff() → annotated diff with [N] line numbers + valid_right_lines set
          9. assemble full_prompt (instructions + context + annotated diff) in memory
         10. opencode run --model ... (stdin ← full_prompt; ANTHROPIC_BASE_URL=inference.local → OpenShell gateway → Vertex AI → Claude)
         11. _parse_output() → prose summary + findings[]
         12. _split_findings() → inline comments (exact/snapped) + prose fallbacks
         13. post_review() → single GitHub Reviews API call (prose + all inline comments)
                  │
                  ▼
         mark_reviewed() → reviewed.json written atomically
         in_flight key removed
         └─ on failure → record_failure(); retry next cycle up to max_retries
```
