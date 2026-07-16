# PR Review Agent

A persistent, sandboxed agent that watches GitHub repositories for open pull requests, generates senior-engineer-level code reviews using Claude via Vertex AI, and posts them as GitHub PR reviews with inline diff comments.

The agent runs as an OpenShell sandbox on OpenShift (ROSA). It polls GitHub on a configurable interval, spawns a fresh OpenCode session per PR, and resets context between reviews to avoid drift. Reviews run in parallel across multiple PRs and repos.

## How it works

```
┌────────────────────────────────────────────────────────────────┐
│  OpenShell Sandbox (persistent)                                │
│                                                                │
│  entrypoint.py ──poll──► list_open_prs()                       │
│       │                       │                                │
│       │               [new SHA detected]                       │
│       │                       │                                │
│       │          ThreadPoolExecutor (parallel reviews)         │
│       │                       │                                │
│       │               review.py (subprocess)                   │
│       │                  │          │                          │
│       │             fetch diff   codebase analysis             │
│       │                  │       (symbol → usages)             │
│       │            _process_diff()                             │
│       │                  │                                     │
│       │            opencode run --model ...                    │
│       │            (full prompt piped via stdin)               │
│       │                       ↓                                │
│       │              _parse_output()                           │
│       │            ┌────────────────┐                          │
│       │            │ prose summary  │                          │
│       │            │ <!-- FINDINGS  │                          │
│       │            │  [{file,line,  │                          │
│       │            │    body}]  --> │                          │
│       │            └────────────────┘                          │
│       │              _split_findings()                         │
│       │            ┌──────────┬───────────┐                    │
│       │         inline      snap to     prose fallback         │
│       │        comments   nearest line   (no valid file)       │
│       │                        │                               │
│       └── state/ ◄──────── post_review()                      │
│           (SHA-keyed)      GitHub Reviews API                  │
└────────────────────────────────────────────────────────────────┘
          │  inference.local
          ▼
   OpenShell Gateway ──► Vertex AI ──► Claude
```

### Review lifecycle

1. **Poll** — `entrypoint.py` fetches open PRs for each watched repo every `polling_interval_seconds`. Each PR is keyed by `org/repo/number/head_sha`. Already-reviewed SHAs are skipped.

2. **State heal** — On sandbox restart, the agent checks GitHub's review history for each pending PR. If a review was already posted by this agent (matched by the authenticated GitHub login and the current SHA), it records it locally and skips — no duplicate reviews. Human reviews on the same commit do not suppress the agent.

3. **Parallel reviews** — New PRs are submitted to a `ThreadPoolExecutor` (configurable `max_concurrent_reviews`). An `in_flight` set prevents the same PR from being submitted twice across poll cycles.

4. **Subprocess isolation** — Each review runs as a separate `review.py` subprocess with `start_new_session=True`. This puts OpenCode and all its background children (TUI, language servers) in their own process group. A configurable timeout kills the entire group if OpenCode hangs; the failure is recorded for retry on the next cycle.

5. **Codebase-aware analysis** — Before invoking OpenCode, the agent fetches the PR branch head (`refs/pull/N/head`) and extracts removed or renamed public symbols from the diff's `-` lines. It then greps that PR-head tree for usages of those symbols in files not touched by the PR. Matching snippets are injected into the review context so Claude can detect broken callers outside the diff. Supported languages: Python, Go, TypeScript/JavaScript, Rust.

6. **Diff annotation** — The raw unified diff is annotated with `[N]` prefixes on every context and addition line, giving the model explicit new-file line-number anchors to reference in its findings.

7. **AI review** — OpenCode receives the full prompt via stdin: instructions (`review-system.md`), context (PR description, CONTRIBUTING.md, prior reviews, cross-file snippets), and the annotated diff — all assembled in memory, no temporary files. Inference routes through `inference.local` to Vertex AI / Claude.

8. **Structured output parsing** — The model output is split at the `<!-- FINDINGS [...] -->` sentinel. Everything before the sentinel is the prose summary; the JSON array inside is the findings list (each with `file`, `line`, `severity`, `body`).

9. **Inline comment placement** — Each finding is validated against the set of valid right-side diff lines. If the exact line is in the diff it goes inline. If the line is off by a small amount, the agent snaps it to the nearest valid line in the same file. Only if the file has no diff lines at all does it fall back to a prose bullet in the review body.

10. **Post** — A single GitHub Reviews API call posts the prose summary and all inline comments atomically.

For a detailed walkthrough of every component and the end-to-end flow, see [OVERVIEW.md](OVERVIEW.md).

## Prerequisites

- OpenShift cluster with `oc` CLI configured
- `openshell` CLI installed and authenticated
- `openssl` installed (for certificate generation)
- `gcloud` CLI authenticated (`gcloud auth application-default login`)
- A GCP project with Vertex AI API enabled and Claude models available
- A GitHub classic PAT with `repo` scope (or `public_repo` for public repos only)

## Setup

### 1. Deploy the OpenShell gateway

```bash
VERTEX_AI_PROJECT_ID=your-gcp-project bash scripts/deploy-all.sh
```

This will:
1. Install the Agent Sandbox CRDs on your cluster
2. Generate self-signed TLS certificates (auto-detecting your cluster's ingress domain for SANs)
3. Deploy the gateway via Kubernetes manifests with TLS enabled
4. Create an OpenShift Route with TLS passthrough (no port-forward needed)
5. Configure your local `openshell` CLI with the gateway's CA cert and client mTLS credentials

### 2. Configure repos to watch

Edit `config.yaml` before building the image — config is baked in at build time:

```yaml
polling_interval_seconds: 120

repos:
  - org: my-org
    repo: my-repo
    ignore_drafts: true
    ignore_labels:
      - do-not-review
      - wip

review_settings:
  model: "anthropic/claude-sonnet-4-6"
  max_diff_lines: 8000
  max_files_changed: 50
  review_timeout_seconds: 600
  max_retries: 3
  max_concurrent_reviews: 5
  max_prior_reviews: 3
  comment_prefix: "**AI PR Review**"
  codebase_aware: true
  max_related_files: 10
  max_related_lines_per_file: 30
  max_related_lines: 300
```

| Field | Default | Description |
|---|---|---|
| `polling_interval_seconds` | `120` | How often to check for new PRs |
| `model` | `anthropic/claude-sonnet-4-6` | OpenCode model string (must include provider prefix) |
| `max_diff_lines` | `8000` | PRs with larger diffs are skipped |
| `max_files_changed` | `50` | PRs touching more files are skipped |
| `review_timeout_seconds` | `600` | Per-PR timeout before the subprocess is killed |
| `max_retries` | `3` | Consecutive failures before a PR is paused |
| `max_concurrent_reviews` | `5` | Parallel review subprocesses |
| `max_prior_reviews` | `3` | Prior reviews included in context for continuity |
| `comment_prefix` | `**AI PR Review**` | Prefix added to every review body |
| `codebase_aware` | `true` | Enable cross-file broken-caller detection |
| `max_related_files` | `10` | Max unique files pulled in for codebase analysis |
| `max_related_lines_per_file` | `30` | Max lines per file after range merging |
| `max_related_lines` | `300` | Hard global line cap for codebase context (~2500 tokens) |

### 3. Build and push the image

```bash
podman build --platform linux/amd64 -t your-registry/pr-review-agent:latest .
podman push your-registry/pr-review-agent:latest
```

Override the default image at sandbox creation time by setting `SANDBOX_IMAGE`:

```bash
export SANDBOX_IMAGE=your-registry/pr-review-agent:latest
```

### 4. Launch the agent

```bash
export VERTEX_AI_PROJECT_ID=your-gcp-project

bash scripts/setup-providers.sh
```

This will:
1. Create the Vertex AI provider using your local gcloud ADC credentials
2. Enable providers v2 and configure inference routing to Claude
3. Create the GitHub provider using your existing `gh` auth
4. Apply RBAC manifests
5. Delete any existing `pr-reviewer` sandbox
6. Create a new sandbox from the image with both providers attached

### 5. Verify

```bash
# Check the sandbox is running
openshell sandbox list

# Watch live logs
openshell sandbox connect pr-reviewer

# Check gateway health
openshell status
```

## Updating config

Config is baked into the image at `/app/pr-review-agent/config.yaml`. To change the repos list, review settings, or any other option:

1. Edit `config.yaml`
2. Rebuild: `podman build --platform linux/amd64 -t your-registry/pr-review-agent:latest . && podman push your-registry/pr-review-agent:latest`
3. Redeploy: `bash scripts/setup-providers.sh` (recreates the sandbox from the new image)

## File structure

```
pr-review-agent/
├── Containerfile               # UBI9 Python 3.11 with git, gh CLI, opencode
├── config.yaml                 # Repos to watch and review settings (baked into image)
├── policy.yaml                 # Sandbox network allowlist (L7 enforcement)
├── scripts/
│   ├── deploy-all.sh           # One-command full deploy + CLI setup
│   ├── deploy.sh               # Deploy OpenShell gateway to OpenShift
│   ├── generate-certs.sh       # Generate self-signed TLS PKI for gateway
│   ├── setup-local-cli.sh      # Configure local openshell CLI with certs
│   ├── setup-providers.sh      # Create providers and launch sandbox
│   ├── setup-local.sh          # Local (non-OpenShift) provider setup
│   └── teardown.sh             # Remove sandbox, providers, and optionally gateway
├── manifests/                  # Kubernetes/OpenShift YAML manifests
│   ├── 00-namespace.yaml
│   ├── 01-agent-sandbox-prereq.yaml
│   ├── 02-rbac.yaml            # Service accounts and RBAC
│   ├── 02b-scc-binding.yaml    # OpenShift SCC binding
│   ├── 03-pki-secrets.yaml     # TLS cert secrets
│   ├── 04-configmap.yaml       # Gateway config
│   ├── 05-gateway-statefulset.yaml
│   ├── 06-service.yaml
│   └── 07-route.yaml           # OpenShift TLS passthrough route
└── payload/                    # Baked into the image at /app/payload/
    ├── entrypoint.py           # Main polling loop, parallel dispatch, state management
    ├── review.py               # Per-PR review orchestration and OpenCode invocation
    ├── requirements.txt        # Python dependencies
    ├── prompts/
    │   └── review-system.md    # Review instructions and structured output format
    └── lib/
        ├── config.py           # Config loading and validation
        ├── github.py           # gh CLI wrappers (PRs, diffs, reviews, rate limiting)
        ├── repos.py            # Git clone/fetch and codebase symbol analysis
        └── state.py            # Reviewed/failure state with atomic JSON writes
```

## Security

The sandbox `policy.yaml` enforces a network allowlist at L7 using OpenShell's enforcement engine. Only the declared binaries can make outbound connections, and only to the declared hosts with the declared HTTP method and path patterns:

| Endpoint | Binary | Allowed |
|---|---|---|
| `api.github.com:443` | `gh` | GET `/repos/**`, POST reviews and comments |
| `github.com:443` | `git` | git clone and fetch only (no push) |
| `opencode.ai:443` | `opencode` | Telemetry (audit mode — logged but not blocked) |

Real GitHub credentials never enter the sandbox. The OpenShell proxy intercepts outgoing `gh` requests and substitutes the real token transparently.

Inference traffic (to Claude via Vertex AI) routes through `inference.local` — the OpenShell gateway's internal proxy endpoint — and never leaves the cluster directly from the sandbox.

## Troubleshooting

**Agent not posting reviews**
```bash
openshell sandbox connect pr-reviewer  # stream live logs
```

**Check reviewed/failure state**
```bash
openshell sandbox exec pr-reviewer -- cat /sandbox/pr-review-agent/state/reviewed.json | python3 -m json.tool
openshell sandbox exec pr-reviewer -- cat /sandbox/pr-review-agent/state/failures.json | python3 -m json.tool
```

**PR hitting retry cap**

A PR stops being retried after `max_retries` consecutive failures at the same SHA. It automatically resumes when a new commit is pushed. Inspect which PRs are capped:
```bash
openshell sandbox exec pr-reviewer -- cat /sandbox/pr-review-agent/state/failures.json | python3 -m json.tool
```

**Vertex AI provider failing / credentials expired**
```bash
gcloud auth application-default login
bash scripts/setup-providers.sh  # recreates providers and sandbox
```

**OpenCode timing out mid-review**

The subprocess timeout is controlled by `review_timeout_seconds` (default 600s). If reviews are consistently timing out, either increase the timeout or reduce `max_diff_lines` to skip large PRs. The entire OpenCode process group is killed on timeout — no zombie processes.

**Gateway unreachable after cluster restart**
```bash
openshell gateway list           # verify gateway URL is registered
openshell status                 # check connection
oc -n openshell get pods         # check gateway pod health
```

**TLS certificate issues**
```bash
openssl s_client -connect $ROUTE_HOST:443 -showcerts
# Regenerate if expired or SANs don't match:
bash scripts/generate-certs.sh
bash scripts/deploy.sh
```

## Testing

Unit tests cover the four core pure-Python layers: diff processing, output parsing, comment placement, symbol extraction, GitHub rate limiting, and state management. No sandbox or GitHub credentials are required — all subprocess calls are mocked.

### Setup

```bash
cd pr-review-agent
python3 -m venv .venv
source .venv/bin/activate
pip install pytest pyyaml
```

### Run

```bash
pytest tests/
```

Or against a specific module:

```bash
pytest tests/test_diff.py
pytest tests/test_symbols.py
pytest tests/test_github.py
pytest tests/test_state.py
```

### What's tested

| Module | Coverage |
|---|---|
| `test_diff.py` | `_process_diff` — hunk offsets, multi-file, deletion exclusion, line numbering format; `_parse_output` — clean output, empty findings, all malformed-sentinel fallbacks; `_split_findings` — exact match, nearest-line snapping, file-not-in-diff fallback |
| `test_symbols.py` | `extract_changed_symbols` — Python/Go/TypeScript/JavaScript/Rust/TSX, private symbol exclusion, addition-line exclusion, multi-file language switching, deduplication; `_merge_ranges`; `_extract_snippets` — line cap, truncation marker, boundary clamping |
| `test_github.py` | `check_rate_limit` — TTL caching, below-threshold sleep, graceful failure, thread-safety (single subprocess under concurrent callers); `list_open_prs` — draft filtering, 100-PR pagination warning; `should_skip_pr` — label matching, no-API-call fast path |
| `test_state.py` | `mark_reviewed`/`is_reviewed`, `record_failure`/`retry_cap_exceeded`, old list-format migration, corrupted-file resilience, atomic write (no `.tmp` left behind), `cleanup` TTL pruning |

### What's not tested

End-to-end review flow (`run_review` in `review.py`) requires a live OpenCode binary, GitHub credentials, and a real git repo. Test those paths manually via a sandbox or by running `review.py` directly against a test repo:

```bash
# Inside the sandbox, or locally with ANTHROPIC_BASE_URL pointing at Vertex:
python3 payload/review.py <org> <repo> <pr_number> <head_sha>
```
