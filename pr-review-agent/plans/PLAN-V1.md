# PR Review Agent — Implementation Plan

## Context

We want a persistent, sandboxed PR review agent that watches for new/updated PRs across multiple repos (multiple GitHub orgs), generates full senior-engineer-level reviews using an AI coding agent, and posts them directly as PR comments. The agent runs inside an OpenShell sandbox on **OpenShift**, with inference routed through **Google Vertex AI** to access Claude.

The core architectural insight: **persistent sandbox, ephemeral AI sessions**. The sandbox stays alive with repos cloned and state tracked. A bash driver script polls GitHub for PRs. For each PR needing review, it gathers context (diff, changed files, architecture docs), launches a fresh OpenCode session via `opencode run`, captures the output, and posts it via `gh pr review`. Context resets naturally between PRs — no accumulation, no drift.

We use **OpenCode** for the AI sessions. OpenCode's `run` subcommand provides non-interactive invocation: `opencode run "prompt" --model anthropic/claude-sonnet-4-6 -f context.md`. Key flags: `--model`/`-m` (model selection), `--file`/`-f` (attach files). The prompt is passed as a positional argument. Note: there is **no `--auto` flag** in OpenCode (`--auto` is a Claude Code concept); `-p` is `--password` (for `opencode serve` auth), not `--prompt`. OpenCode has partial default policy coverage in the base sandbox — the policy must include `opencode.ai:443` and OpenCode binary paths. `ANTHROPIC_BASE_URL` **must include the `/v1` suffix**: `https://inference.local/v1` (OpenCode does NOT add `/v1` internally — omitting it causes all inference calls to silently fail with a non-JSON response).

## File Structure

```
~/experiments/pr-review-agent/
├── scripts/
│   ├── deploy.sh               # Host-side: deploy gateway to OpenShift with TLS
│   ├── deploy-all.sh           # Host-side: full deploy + CLI setup + gateway registration
│   ├── generate-certs.sh       # Generate self-signed TLS PKI (CA, server, client CA, JWT key)
│   ├── setup-local-cli.sh      # Copy CA cert + generate client mTLS cert for CLI auth
│   ├── setup-providers.sh      # Configure Vertex AI + GitHub providers, launch sandbox
│   ├── setup-local.sh          # Local dev/test variant (assumes local gateway)
│   └── teardown.sh             # Delete sandbox, providers, and optionally the deployment
├── manifests/
│   ├── 00-namespace.yaml       # openshell namespace
│   ├── 01-agent-sandbox-prereq.yaml  # Reference for Agent Sandbox CRD controller
│   ├── 02-rbac.yaml            # ServiceAccounts, Roles, ClusterRoles, Bindings
│   ├── 03-pki-secrets.yaml     # Placeholder (secrets created by generate-certs.sh)
│   ├── 04-configmap.yaml       # Gateway config (gateway.toml with TLS paths)
│   ├── 05-gateway-statefulset.yaml   # Gateway StatefulSet with TLS volume mounts
│   ├── 06-service.yaml         # ClusterIP service on port 8443
│   └── 07-route.yaml           # OpenShift Route with TLS passthrough
├── certs/                      # Generated certificates (git-ignored)
├── config.yaml                 # Which repos to watch, polling interval, preferences
├── policy.yaml                 # Sandbox network policy (GitHub API + git transport)
└── payload/                    # Uploaded into sandbox at /sandbox/payload/
    ├── entrypoint.sh           # Main polling loop
    ├── review.sh               # Single-PR review orchestration
    ├── prompts/
    │   └── review-system.md    # Review prompt template
    └── lib/
        ├── config.sh           # Parse config.yaml → shell vars (uses python3 + jq)
        ├── github.sh           # gh CLI wrappers (list PRs, post reviews)
        ├── state.sh            # Track reviewed (repo, PR#, SHA) tuples in JSON
        └── repos.sh            # Clone/fetch repo management
```

## Implementation Steps

### Step 1: Create project skeleton

Create `~/experiments/pr-review-agent/` with the directory structure above. All files listed, all empty initially.

### Step 2: `config.yaml` — repo watchlist and preferences

YAML config parsed inside the sandbox. Defines:
- `polling_interval_seconds` (default 120)
- `repos[]` — each with `org`, `repo`, `base_branch`, `ignore_drafts`, `ignore_labels[]`, optional `file_patterns[]`
- `review_settings` — `max_diff_lines` (8000), `max_files_changed` (50), `review_timeout_seconds` (600), `max_retries` (3), `comment_prefix`

**Dynamic config updates (OpenShift)**: The config is stored in a ConfigMap (`pr-review-agent-config`) in the `openshell` namespace. A lightweight config-sync loop in the entrypoint fetches the ConfigMap via the Kubernetes API every polling cycle using the pod's service account token (mounted at `/var/run/secrets/kubernetes.io/serviceaccount/`). When the config changes, new repos are cloned and picked up on the next poll; removed repos stop being watched. No sandbox restart needed.

On the host side, updating the watched repos is just:
```shell
oc -n openshell create configmap pr-review-agent-config \
  --from-file=config.yaml=config.yaml --dry-run=client -o yaml | oc apply -f -
```

For local dev, config is uploaded via `--upload` and re-read from the filesystem each cycle.

### Step 3: `policy.yaml` — sandbox network policy

Scoped L7 rules:
- **GitHub REST API** (`api.github.com:443`): Allow GET on `/repos/**` (read PRs, files, metadata). Allow POST on `/repos/*/pulls/*/reviews` and `/repos/*/pulls/*/comments` (post reviews). No PUT/PATCH/DELETE — agent cannot merge, close, or push.
- **GitHub git transport** (`github.com:443`): Read-only clone/fetch via `git-upload-pack`. No `git-receive-pack` — agent cannot push.
- **Kubernetes API** (`kubernetes.default.svc:443`): Allow GET on `/api/v1/namespaces/openshell/configmaps/pr-review-agent-config` only. Used by the config sync loop to read updated repo watchlists. Binary: `/usr/bin/curl`.
- **OpenCode telemetry** (`opencode.ai:443`): Allow outbound for OpenCode's update checks and telemetry. Binary: `/usr/local/bin/opencode`.
- Binaries: `/usr/bin/gh`, `/usr/bin/git`, `/usr/bin/curl`, `/usr/local/bin/opencode`, `/usr/lib/git-core/git-remote-http*`
- `inference.local` is handled by the gateway, not the network policy.

### Step 4: `payload/lib/` — shell library functions

**`config.sh`**: `load_config()` parses `config.yaml` using a Python one-liner that emits shell-compatible variables. Populates `REPOS` array, `POLLING_INTERVAL`, and per-repo settings. Re-reads the config each polling cycle so changes take effect without restart. On OpenShift, `sync_config()` fetches the ConfigMap from the K8s API before parsing:
```bash
curl -sSk \
  -H "Authorization: Bearer $(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
  "https://kubernetes.default.svc/api/v1/namespaces/${NAMESPACE}/configmaps/pr-review-agent-config" \
  | jq -r '.data["config.yaml"]' > /sandbox/pr-review-agent/config.yaml
```
Falls back to the local file if the K8s API is unreachable (e.g., running locally).

**`state.sh`**: State stored at `/sandbox/pr-review-agent/state/reviewed.json` (successful reviews) and `/sandbox/pr-review-agent/state/failures.json` (failed attempts). Functions:
- `state_is_reviewed org repo pr_number head_sha` — returns 0 if already reviewed at this SHA
- `state_mark_reviewed org repo pr_number head_sha` — records the review
- `state_record_failure org repo pr_number head_sha` — increments the failure count for this PR+SHA
- `state_should_retry org repo pr_number head_sha` — returns 1 if failure count >= `max_retries` (default 3). After hitting the cap, the PR is skipped until a new SHA is pushed.
- `state_cleanup days` — removes entries older than N days from both files

Uses Python `json` module for all JSON manipulation. **`jq` is NOT present in the base sandbox image** (`ghcr.io/nvidia/openshell-community/sandboxes/base:latest`) and cannot be assumed. `gh --jq` (built-in jq engine in the gh binary) is available and works. Standalone `jq` calls must use Python json instead. `pyyaml` is also absent — YAML parsing must use stdlib-only Python (regex or line-by-line).

**`github.sh`**: Wrappers around `gh` CLI:
- `list_open_prs org repo ignore_drafts` — returns TSV of `number\thead_sha\ttitle`; draft detection uses `.draft` (REST field). **Do not use `.isDraft`** — that is the GraphQL field name, always `null` via REST, silently dropping every PR from `select(.isDraft == false)`.
- `should_skip_pr org repo pr_number ignore_labels` — checks labels against skip list
- `get_pr_diff org repo pr_number` — returns the diff
- `post_review org repo pr_number body` — posts via `gh api repos/{org}/{repo}/pulls/{pr}/reviews -X POST -f body=... -f event=COMMENT`. **Do NOT use `gh pr review --comment`** — that command routes through GraphQL (`POST /graphql`), which is not controllable per-endpoint in the sandbox policy. The REST equivalent hits `/repos/.../pulls/.../reviews` which can be individually allow-listed.
- `check_rate_limit` — reads `X-RateLimit-Remaining` and `X-RateLimit-Reset` from `gh api rate_limit`. Returns 1 if remaining < 100, logging the reset time. All other `github.sh` functions call this before making API requests and sleep until reset if the budget is exhausted.

**`repos.sh`**: Clone/fetch management:
- `clone_or_fetch org repo` — shallow clone (`--depth=1`) if missing, `git fetch --prune` + `git checkout FETCH_HEAD` if exists
- Repos stored at `/sandbox/pr-review-agent/repos/{org}/{repo}/`
- Diffs come from `gh pr diff` (GitHub API), not local git diff — shallow clones are fine
- **Sync frequency**: `git fetch` runs once per polling cycle per repo (every `polling_interval_seconds`). This keeps context files (CONTRIBUTING.md, test files, architecture docs) current for the review prompt. Fetch is cheap on shallow clones. The local checkout tracks the default branch head so the agent reads the latest version of context files, not stale ones from the initial clone.

### Step 5: `payload/prompts/review-system.md` — review prompt template

Template with `{{ORG}}`, `{{REPO}}`, `{{PR_NUMBER}}`, `{{PR_TITLE}}` placeholders. Instructs OpenCode to:
1. Review for correctness, architecture, security, testing gaps, docs, and style
2. Output structured markdown with sections: Summary, Critical Issues, Warnings, Suggestions, Testing Gaps, What Looks Good
3. Reference specific `file:line` from the diff
4. Show concrete fix suggestions
5. Skip empty sections; be brief on trivially correct PRs
6. Prefix output with the configured comment prefix

The full diff and PR context are appended after the template.

### Step 6: `payload/review.sh` — per-PR review driver

Called by the polling loop for each PR needing review. Steps:
1. Fetch PR metadata via `gh pr view --json`
2. Check size limits (diff lines, file count) — skip with a comment if too large
3. Get the diff via `gh pr diff`
4. Gather context: PR description, CONTRIBUTING.md (if exists), related test file paths
5. Render the prompt template with `sed` substitution, append context + diff
6. Invoke OpenCode:
   ```
   ANTHROPIC_BASE_URL="https://inference.local/v1" \
   ANTHROPIC_API_KEY=unused \
   opencode run "$(cat prompt_file)" --model anthropic/claude-sonnet-4-6 -f diff.patch
   ```
   Capture stdout to file. `ANTHROPIC_BASE_URL` must end with `/v1` — OpenCode does NOT append it. `ANTHROPIC_API_KEY=unused` is a placeholder stripped by the proxy. There is no `--auto` flag in OpenCode. The `-f` flag attaches the diff directly. Do NOT set `CLAUDE_CODE_USE_VERTEX=1` — that bypasses `inference.local` and attempts direct Vertex AI connections which fail inside the sandbox.
7. Post the review via `gh api repos/{org}/{repo}/pulls/{pr}/reviews -X POST -f body="..." -f event=COMMENT`. Do not use `gh pr review --comment` (uses GraphQL, not policy-controllable).
8. Cleanup temp files

Each review runs with `timeout $REVIEW_TIMEOUT` (configurable, default 600s / 10 minutes) to prevent hangs. Errors cause the review to fail (not crash the loop); the failure is recorded via `state_record_failure` and the PR will be retried next cycle up to `max_retries` (default 3) times per SHA. After exhausting retries, the PR is skipped until a new commit is pushed.

### Step 7: `payload/entrypoint.sh` — main polling loop

The sandbox entrypoint. Steps:
1. Initialize directories, source libs, validate `gh auth status`
2. Initial clone of all configured repos
3. Infinite loop:
   - For each repo in config: fetch latest, list open PRs, filter by state/labels
   - For each unreviewed PR: check `state_should_retry` — skip if retry cap exceeded. Otherwise run `review.sh` in a subshell (isolated failures).
   - On success: mark reviewed in state. On failure: record failure via `state_record_failure`, log error and remaining retries, move on.
   - Cleanup old state entries and log files
   - Sleep for `polling_interval_seconds`

### Step 8: Gateway deployment scripts

The deployment follows the approach from [2000krysztof/Openshell-Openshift-Deploy](https://github.com/2000krysztof/Openshell-Openshift-Deploy) — raw YAML manifests with TLS enabled, applied in order. No Helm chart.

#### `scripts/generate-certs.sh` — TLS PKI generation

Generates self-signed certificates for the gateway:
1. **CA certificate** (`ca.crt`, `ca.key`) — root CA for signing the server cert
2. **Server certificate** (`server.crt`, `server.key`) — gateway TLS cert with SANs:
   - `openshell`, `openshell.openshell.svc`, `openshell.openshell.svc.cluster.local`, `localhost`, `127.0.0.1`
   - Auto-detected OpenShift route hostname (queries `oc get ingresses.config.openshift.io cluster` for the ingress domain, adds `openshell-openshell.<domain>`)
3. **Client CA certificate** (`client-ca.crt`, `client-ca.key`) — separate CA for mTLS from sandbox pods
4. **JWT signing key** — three separate files required: `jwt-signing.pem` (Ed25519 PKCS#8 private key, via `openssl genpkey -algorithm Ed25519`), `jwt-public.pem` (SPKI PEM public key), `jwt-kid` (32-char hex = first 16 bytes of SHA-256 of DER public key). A raw base64 string does **not** work. All three must be mounted into the gateway and configured in `[openshell.gateway.gateway_jwt]`.

Certificates are stored in `certs/` (git-ignored).

#### `scripts/deploy.sh` — deploy gateway to OpenShift

Applies manifests in order:
1. Check prerequisites (`oc` CLI, cluster connectivity)
2. Install Agent Sandbox CRDs and controller (`kubectl apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/latest/download/manifest.yaml`) if not already present
3. Generate TLS certificates via `generate-certs.sh` (skips if `certs/` already populated)
4. Create namespace (`00-namespace.yaml`)
5. Create PKI secrets from generated certs. **Three secrets, two mTLS roles:**
   - `openshell-server-tls` — server cert + key (used by gateway for TLS). `ca.crt` field = the CA that signed the server cert, used by CLI to verify the gateway.
   - `openshell-server-client-ca` — **only `ca.crt`** = the client CA cert. Gateway uses this to verify incoming sandbox client certs. Must NOT include the client CA private key.
   - `openshell-sandbox-client-tls` — `ca.crt` = server CA (sandbox verifies gateway), `tls.crt` + `tls.key` = sandbox client cert (signed by client CA). Mounted into sandbox pods by the supervisor.
   - `openshell-sandbox-jwt-signing-secret` — three keys: `signing.pem`, `public.pem`, `kid` (all three required; see JWT key generation above).
6. Apply RBAC (`02-rbac.yaml`) — gateway ServiceAccount, sandbox ServiceAccount, ClusterRole for node inspection + TokenReview, Role for sandbox CRD management
7. Apply ConfigMap (`04-configmap.yaml`) — `gateway.toml` with TLS enabled, bind on `0.0.0.0:8443`, cert paths, gRPC endpoint
8. Apply StatefulSet (`05-gateway-statefulset.yaml`) — gateway container with TLS volume mounts, `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `drop: ALL` capabilities, `RuntimeDefault` seccomp, 10Gi PVC for SQLite
9. Apply Service (`06-service.yaml`) — ClusterIP on port 8443
10. Apply Route (`07-route.yaml`) — OpenShift Route with `tls.termination: passthrough` (gateway handles its own TLS)
11. Wait for gateway pod readiness

#### `scripts/setup-local-cli.sh` — configure CLI for TLS

Copies the CA cert to `~/.config/openshell/gateways/k8s/ca.crt` and generates a client mTLS certificate signed by the client CA. Places client cert/key in `~/.config/openshell/gateways/k8s/mtls/`.

#### `scripts/deploy-all.sh` — one-command full setup

Runs in sequence:
1. `deploy.sh` (cluster deployment)
2. `setup-local-cli.sh` (local CLI certs)
3. Detects the OpenShift Route hostname
4. Registers the gateway: `openshell gateway add https://$ROUTE_HOST --name openshift --local`

No port-forward needed — the Route provides persistent HTTPS access.

#### `scripts/setup-providers.sh` — provider + sandbox setup

Separated from gateway deployment so it can be run independently. Steps:
1. Validate `openshell status` reaches the gateway
2. Create Vertex AI provider:
   ```
   openshell provider create \
     --name vertex-pr-reviewer \
     --type google-vertex-ai \
     --from-gcloud-adc \
     --config VERTEX_AI_PROJECT_ID=$VERTEX_AI_PROJECT_ID \
     --config VERTEX_AI_REGION=global
   ```
3. Enable provider endpoint injection: `openshell settings set --global --key providers_v2_enabled --value true --yes`
4. Configure inference routing: `openshell inference set --provider vertex-pr-reviewer --model claude-sonnet-4-6 --no-verify`
5. Create GitHub provider: `openshell provider create --name github-pr-reviewer --type github --from-existing`
6. Create ConfigMap for dynamic config: `oc -n openshell create configmap pr-review-agent-config --from-file=config.yaml=config.yaml`
7. Delete existing sandbox if present
8. Create sandbox: `openshell sandbox create --name pr-reviewer --from base --provider vertex-pr-reviewer --provider github-pr-reviewer --policy policy.yaml --upload ./payload:/sandbox --upload ./config.yaml:/sandbox/pr-review-agent/ --no-tty -- bash /sandbox/payload/entrypoint.sh`

   **Upload path gotcha**: `--upload src:dest` puts the source directory/file *inside* the destination path (like `rsync` without a trailing slash). `--upload payload:/sandbox` lands files at `/sandbox/payload/entrypoint.sh`. `--upload payload:/sandbox/payload` would nest to `/sandbox/payload/payload/entrypoint.sh`. Same for files: `--upload config.yaml:/sandbox/pr-review-agent/` puts the file at `/sandbox/pr-review-agent/config.yaml`; `--upload config.yaml:/sandbox/pr-review-agent/config.yaml` nests to `.../config.yaml/config.yaml`.

The config is uploaded both as a file (initial seed) and as a ConfigMap (for live updates). The entrypoint's `sync_config()` overwrites the local file from the ConfigMap each cycle.

Environment variables required: `VERTEX_AI_PROJECT_ID`. `GITHUB_TOKEN` must be available for the GitHub provider. `VERTEX_AI_REGION` defaults to `global`.

#### `scripts/setup-local.sh` — local dev/test variant

Identical to `setup-providers.sh` but assumes a local gateway is already running (via `mise run gateway` or Docker). Used for development iteration before deploying to OpenShift.

#### `scripts/teardown.sh` — cleanup

Accepts a `--full` flag:
- Default: Deletes the sandbox and providers only (leaves the gateway running for reuse).
- `--full`: Also deletes the `openshell` namespace (cascades to all resources), cluster-scoped RBAC, and optionally the Agent Sandbox controller and generated certificates.

### OpenShift-specific considerations

- **TLS is enabled** using self-signed certificates generated by `generate-certs.sh`. The gateway runs on port 8443 with mTLS for sandbox communication. For production, replace self-signed certs with cert-manager + Let's Encrypt or your corporate PKI.
- **OpenShift Route with TLS passthrough** eliminates the port-forward dependency. The Route passes encrypted traffic directly to the gateway pod — no TLS termination at the router. The gateway handles its own TLS using the server cert in `openshell-server-tls`.
- **Privileged SCC for sandbox pods only**. The gateway runs with `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, and drops all capabilities. Sandbox pods require `system:openshift:scc:privileged` (granted via `02b-scc-binding.yaml`) for Landlock filesystem isolation, seccomp filters, and netns setup. This is scoped to the `openshell-sandbox` ServiceAccount only.
- **Kubernetes version constraint**: ROSA (K8s 1.32) does not support `ImageVolume` sideloading (requires K8s 1.33+). Must set `supervisor_sideload_method = "init-container"` in `[openshell.drivers.kubernetes]`. Without this the sandbox pod fails to start.
- **No mTLS user auth on Kubernetes gateways**: Per OpenShell docs, mTLS user authentication is unsupported for Kubernetes deployments. Must use `[openshell.gateway.auth] allow_unauthenticated_users = true`. Without this, all `openshell provider create`, `sandbox create` calls fail with "missing authorization header". This is acceptable for single-user personal clusters; for multi-user, OIDC is the alternative.
- **`gateway add --local` ordering**: Run `openshell gateway add --local` *before* `setup-local-cli.sh`. The `--local` flag imports certs from the Homebrew install directory and overwrites the mtls dir. Running it after the custom cert setup would undo it.
- **Gateway needs outbound access** to `*.aiplatform.googleapis.com:443` and `oauth2.googleapis.com:443` for Vertex AI token refresh. Verify your cluster's egress NetworkPolicies or EgressFirewall allow this.
- **For production use**: Replace `--from-gcloud-adc` with a GCP service account key so token refresh doesn't depend on your local gcloud credentials. The gateway refreshes tokens server-side, so it works even when your workstation is disconnected — but only if bootstrapped with a service account key, not ADC.
- **Do NOT set `CLAUDE_CODE_USE_VERTEX=1`** inside the sandbox. That flag causes direct Vertex AI connection attempts which fail because the sandbox does not have GCP credentials. Inference routing through `inference.local` is the correct approach.

## Policy Wildcard Behaviour

In OpenShell sandbox policy path patterns, `*` does **not** match across `/`. A path like `/repos/*/pulls/*/reviews` will NOT match `/repos/Bobbins228/openshell-experiments/pulls/2/reviews` because `org/repo` spans two segments. Use `/*/*` for two-segment paths: `/repos/*/*/pulls/*/reviews`. `**` (double star) does match recursively and can be used for read-only GET rules like `GET /repos/**`.

## Prior Review Context (added post-V1)

`review.sh` fetches existing reviews for the PR via `GET /repos/{org}/{repo}/pulls/{pr}/reviews` (covered by the existing `GET /repos/**` policy rule) and injects them into the prompt context under `## Prior Reviews` (last 5, empty-body filtered). The system prompt instructs the model to open with a **Previous Review Follow-up** section: tick each prior finding as Addressed / Partially addressed / Still open, then continue with the normal review. This prevents re-raising already-fixed issues on subsequent pushes.

## Key Design Decisions

- **OpenShift as primary target**: The agent runs on OpenShift with the gateway deployed via raw YAML manifests with TLS enabled, following the approach from [2000krysztof/Openshell-Openshift-Deploy](https://github.com/2000krysztof/Openshell-Openshift-Deploy). An OpenShift Route with TLS passthrough provides persistent access without a port-forward. A local dev variant (`setup-local.sh`) exists for iteration without a cluster.
- **Vertex AI + Claude**: Uses Google Vertex AI as the inference provider to access Claude via your existing subscription. The gateway manages GCP token refresh server-side — sandboxes never see raw GCP credentials.
- **OpenCode `run` for non-interactive reviews**: OpenCode's `run` subcommand accepts a prompt as positional args with `--auto` for unattended permission approval and `--file` to attach context files. `ANTHROPIC_BASE_URL` is set to `https://inference.local` (no `/v1` suffix). `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` is required to prevent sending Vertex AI-unsupported parameters. Explicit `opencode.ai` + binary paths in the sandbox policy (partial default coverage).
- **`gh pr diff` over local git diff**: Avoids needing deep clone history. Shallow clones save disk; the local clone exists only for reading context files (tests, CONTRIBUTING.md).
- **State in JSON file**: Simple, no external dependencies. Persists in the sandbox filesystem at `/sandbox/`. Cleaned up after 30 days. **Tradeoff**: state is lost when the sandbox is recreated (teardown + setup), causing all open PRs to be re-reviewed once. This is acceptable — re-reviews on the same SHA are harmless (identical diff → near-identical review), and sandbox recreation is infrequent. For v2, persisting state to a PVC or lightweight database (e.g., SQLite on a PVC) would survive sandbox recreation and also enable review history queries. Not justified for v1 given the low impact.
- **No webhooks**: Polling via `gh pr list` is simpler and fits the sandbox model (outbound only, no ingress needed). Works naturally on OpenShift without needing to expose an ingress for webhook delivery.
- **Per-review subshell isolation**: One bad review can't crash the loop.
- **L7-scoped policy**: The agent can read repos and post reviews, but cannot merge, close, push, or delete anything.

## Logging and Monitoring

**Logging**: All output goes to stdout/stderr, visible via `openshell sandbox connect pr-reviewer` or `oc logs`. The entrypoint prefixes log lines with `[entrypoint]`, `[review]`, or `[lib/<name>]` and ISO-8601 timestamps. Key events logged: poll cycle start/end, PR skipped (reason), review started, review posted, review failed (error + retry count), rate limit paused, config reloaded. No log rotation needed — sandbox stdout is ephemeral and OpenShift handles container log rotation via the kubelet.

**Monitoring**: For v1, liveness is checked by connecting to the sandbox (`openshell sandbox connect`) and verifying the polling loop is running. The entrypoint writes a heartbeat timestamp to `/sandbox/pr-review-agent/state/heartbeat` at the start of each poll cycle. A simple external check (e.g., a CronJob or script) can read this via `openshell sandbox exec pr-reviewer -- cat /sandbox/pr-review-agent/state/heartbeat` and alert if it's stale beyond `2 * polling_interval_seconds`.

**`gh` auth inside the sandbox**: The GitHub provider (`github-pr-reviewer`) injects a `GITHUB_TOKEN` environment variable into the sandbox. The `gh` CLI automatically picks this up — no explicit `gh auth login` is needed. The entrypoint validates this with `gh auth status` on startup and exits with a clear error if the token is missing or expired.

## Verification

1. **Local dry run**: First, test with `setup-local.sh` against a local gateway and a test repo with an open PR. Verify the sandbox starts, the polling loop runs, and a review is posted.
2. **OpenShift deployment**: Run `scripts/deploy-all.sh` targeting your OpenShift cluster. Verify the manifests apply, the gateway pod comes up healthy, the Route is created, and the CLI can reach the gateway over HTTPS.
3. **Vertex AI inference**: Inside the sandbox, verify `inference.local` is reachable by running `ANTHROPIC_BASE_URL="https://inference.local" ANTHROPIC_API_KEY=unused CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 opencode run "say hello" --auto`. Confirm the Vertex AI token refresh is working via `openshell provider list`.
4. **Reconnect**: `openshell sandbox connect pr-reviewer` to see the agent's live output.
5. **Re-review test**: Force-push a commit to the test PR; verify the agent detects the new SHA and re-reviews.
6. **Error test**: Point at a non-existent repo; verify the loop logs the error and continues with other repos.
7. **Size limit test**: Open a PR with a massive diff; verify the agent posts a "skipping" comment instead of reviewing.
8. **Policy test**: From inside the sandbox, attempt `gh pr merge` or `git push` — should be denied by policy.
9. **TLS test**: Verify TLS is working by running `openssl s_client -connect $ROUTE_HOST:443 -showcerts` and confirming the server cert SANs include the route hostname.
10. **Resilience test**: Delete and recreate the gateway pod; verify the sandbox keeps running and the CLI can reach the gateway again after the pod restarts (Route + Service handle reconnection).
