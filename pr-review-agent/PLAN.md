# PR Review Agent — Implementation Plan

## Context

We want a persistent, sandboxed PR review agent that watches for new/updated PRs across multiple repos (multiple GitHub orgs), generates full senior-engineer-level reviews using an AI coding agent, and posts them directly as PR comments. The agent runs inside an OpenShell sandbox on **OpenShift**, with inference routed through **Google Vertex AI** to access Claude.

The core architectural insight: **persistent sandbox, ephemeral AI sessions**. The sandbox stays alive with repos cloned and state tracked. A bash driver script polls GitHub for PRs. For each PR needing review, it gathers context (diff, changed files, architecture docs), launches a fresh OpenCode session via `opencode run`, captures the output, and posts it via `gh pr review`. Context resets naturally between PRs — no accumulation, no drift.

We use **OpenCode** for the AI sessions. OpenCode's `run` subcommand provides non-interactive invocation: `opencode run "prompt" --auto --model anthropic/claude-sonnet-4-6 --file context.md`. Key flags: `--auto` (auto-approve permissions), `--model`/`-m` (model selection), `--file`/`-f` (attach files). The prompt is passed as a positional argument. Note: `-p` is `--password` (for `opencode serve` auth), not `--prompt`. OpenCode has partial default policy coverage in the base sandbox — the policy must include `opencode.ai:443` and OpenCode binary paths. `ANTHROPIC_BASE_URL` requires the `/v1` suffix for OpenCode.

## File Structure

```
~/experiments/pr-review-agent/
├── setup.sh                    # Host-side: deploy gateway on OpenShift, configure providers, launch sandbox
├── setup-local.sh              # Host-side: same but for local Docker (dev/test)
├── teardown.sh                 # Host-side: delete sandbox, providers, and optionally the OpenShift deployment
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
- `review_settings` — `max_diff_lines` (8000), `max_files_changed` (50), `comment_prefix`

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

**`state.sh`**: State stored at `/sandbox/pr-review-agent/state/reviewed.json`. Functions:
- `state_is_reviewed org repo pr_number head_sha` — returns 0 if already reviewed at this SHA
- `state_mark_reviewed org repo pr_number head_sha` — records the review
- `state_cleanup days` — removes entries older than N days

Uses `jq` for JSON manipulation (available in base image).

**`github.sh`**: Wrappers around `gh` CLI:
- `list_open_prs org repo ignore_drafts` — returns TSV of `number\thead_sha\ttitle`
- `should_skip_pr org repo pr_number ignore_labels` — checks labels against skip list
- `get_pr_diff org repo pr_number` — returns the diff
- `post_review org repo pr_number body` — posts via `gh pr review --comment`

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
6. Invoke OpenCode: `ANTHROPIC_BASE_URL="https://inference.local/v1" ANTHROPIC_API_KEY=unused opencode run "$(cat prompt_file)" --auto --model anthropic/claude-sonnet-4-6 --file diff.patch` — capture stdout to file. Uses `--auto` to auto-approve permissions for unattended operation. The `--file` flag attaches the diff directly.
7. Post the review via `gh pr review --comment --body "$(cat output)"`
8. Cleanup temp files

Each review runs with `timeout 600` (10 minutes) to prevent hangs. Errors cause the review to fail (not crash the loop); the PR will be retried next cycle.

### Step 7: `payload/entrypoint.sh` — main polling loop

The sandbox entrypoint. Steps:
1. Initialize directories, source libs, validate `gh auth status`
2. Initial clone of all configured repos
3. Infinite loop:
   - For each repo in config: fetch latest, list open PRs, filter by state/labels
   - For each unreviewed PR: run `review.sh` in a subshell (isolated failures)
   - On success: mark reviewed in state. On failure: log error, move on.
   - Cleanup old state entries and log files
   - Sleep for `polling_interval_seconds`

### Step 8: `setup.sh` — host-side bootstrap (OpenShift)

The primary setup script targets OpenShift. It has two phases: **deploy the gateway** (if not already running) and **configure providers + launch sandbox**.

**Phase 1: OpenShift gateway deployment** (idempotent, skipped if already running):
1. Validate `oc` CLI configured and cluster reachable
2. Create namespace: `oc create ns openshell` (if not exists)
3. Grant privileged SCC: `oc adm policy add-scc-to-user privileged -z openshell-sandbox -n openshell`
4. Install Helm chart with OpenShift overrides:
   ```
   helm install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
     --namespace openshell \
     --set pkiInitJob.enabled=false \
     --set server.disableTls=true \
     --set podSecurityContext.fsGroup=null \
     --set securityContext.runAsUser=null
   ```
5. Wait for rollout: `oc -n openshell rollout status statefulset/openshell`
6. Start port-forward in background: `oc -n openshell port-forward svc/openshell 8080:8080 &`
7. Register gateway: `openshell gateway add http://127.0.0.1:8080 --local --name openshift`

**Phase 2: Provider + sandbox setup** (same for OpenShift and local):
1. Validate `openshell status` reaches the gateway
2. Create GitHub provider: `openshell provider create --name github-pr-reviewer --type github --from-existing`
3. Create Vertex AI provider: `openshell provider create --name vertex-pr-reviewer --type google-vertex-ai --from-gcloud-adc --config VERTEX_AI_PROJECT_ID=... --config VERTEX_AI_REGION=...`
4. Enable providers v2: `openshell settings set --global --key providers_v2_enabled --value true --yes`
5. Configure inference: `openshell inference set --provider vertex-pr-reviewer --model claude-sonnet-4-6 --no-verify`
6. Create ConfigMap for dynamic config (OpenShift only): `oc -n openshell create configmap pr-review-agent-config --from-file=config.yaml=config.yaml`
7. Delete existing sandbox if present
8. Create sandbox: `openshell sandbox create --name pr-reviewer --from base --provider vertex-pr-reviewer --provider github-pr-reviewer --policy policy.yaml --upload ./payload:/sandbox/payload --upload ./config.yaml:/sandbox/pr-review-agent/config.yaml --no-tty -- bash /sandbox/payload/entrypoint.sh`

The config is uploaded both as a file (initial seed) and as a ConfigMap (for live updates). The entrypoint's `sync_config()` overwrites the local file from the ConfigMap each cycle.

Environment variables required: `GITHUB_TOKEN`, `VERTEX_AI_PROJECT_ID`, and optionally `VERTEX_AI_REGION`.

The script detects whether the gateway is already deployed by checking `openshell status` first and skips Phase 1 if it's reachable. A `--skip-deploy` flag also bypasses Phase 1 explicitly.

### Step 8b: `setup-local.sh` — local dev/test variant

Identical to Phase 2 of `setup.sh` but assumes a local gateway is already running (via `mise run gateway` or Docker). Used for development iteration before deploying to OpenShift.

### Step 9: `teardown.sh` — cleanup

Accepts a `--full` flag:
- Default: Deletes the sandbox and providers only (leaves the OpenShift gateway running for reuse).
- `--full`: Also uninstalls the Helm chart and removes the `openshell` namespace from OpenShift.

### OpenShift-specific considerations

- **TLS is disabled** in the experimental OpenShift path. The gateway runs plaintext HTTP. This is acceptable for evaluation on a private network but not for production. The port-forward provides transport security from your workstation to the cluster.
- **The port-forward must stay open** for the CLI to reach the gateway. For persistent access without a port-forward, expose via an OpenShift Route or use the Gateway API ingress path (see `docs/kubernetes/ingress.mdx`). The setup script starts it in the background and logs the PID for teardown.
- **Sandbox pods need the `privileged` SCC** because the supervisor sets up network namespaces and policy enforcement. This is granted to the `openshell-sandbox` service account, not cluster-wide.
- **Gateway needs outbound access** to `*.aiplatform.googleapis.com:443` and `oauth2.googleapis.com:443` for Vertex AI token refresh. Verify your cluster's egress NetworkPolicies or EgressFirewall allow this.
- **For production use**: Replace `--from-gcloud-adc` with a service account key flow so token refresh doesn't depend on your local gcloud credentials. The gateway refreshes tokens server-side, so it works even when your workstation is disconnected — but only if bootstrapped with a service account key, not ADC.

## Key Design Decisions

- **OpenShift as primary target**: The agent runs on OpenShift with the gateway deployed via Helm. A local dev variant (`setup-local.sh`) exists for iteration without a cluster.
- **Vertex AI + Claude**: Uses Google Vertex AI as the inference provider to access Claude via your existing subscription. The gateway manages GCP token refresh server-side — sandboxes never see raw GCP credentials.
- **OpenCode `run` for non-interactive reviews**: OpenCode's `run` subcommand accepts a prompt as positional args with `--auto` for unattended permission approval and `--file` to attach context files. Requires `/v1` suffix on `ANTHROPIC_BASE_URL` and explicit `opencode.ai` + binary paths in the sandbox policy (partial default coverage).
- **`gh pr diff` over local git diff**: Avoids needing deep clone history. Shallow clones save disk; the local clone exists only for reading context files (tests, CONTRIBUTING.md).
- **State in JSON file**: Simple, no external dependencies. Persists in the sandbox filesystem at `/sandbox/`. Cleaned up after 30 days.
- **No webhooks**: Polling via `gh pr list` is simpler and fits the sandbox model (outbound only, no ingress needed). Works naturally on OpenShift without needing to expose an ingress for webhook delivery.
- **Per-review subshell isolation**: One bad review can't crash the loop.
- **L7-scoped policy**: The agent can read repos and post reviews, but cannot merge, close, push, or delete anything.

## Verification

1. **Local dry run**: First, test with `setup-local.sh` against a local gateway and a test repo with an open PR. Verify the sandbox starts, the polling loop runs, and a review is posted.
2. **OpenShift deployment**: Run `setup.sh` targeting your OpenShift cluster. Verify the Helm chart installs, the gateway comes up healthy, and the port-forward works.
3. **Vertex AI inference**: Inside the sandbox, verify `inference.local` is reachable by running `ANTHROPIC_BASE_URL="https://inference.local/v1" ANTHROPIC_API_KEY=unused opencode run "say hello" --auto`. Confirm the Vertex AI token refresh is working via `openshell provider list`.
4. **Reconnect**: `openshell sandbox connect pr-reviewer` to see the agent's live output.
5. **Re-review test**: Force-push a commit to the test PR; verify the agent detects the new SHA and re-reviews.
6. **Error test**: Point at a non-existent repo; verify the loop logs the error and continues with other repos.
7. **Size limit test**: Open a PR with a massive diff; verify the agent posts a "skipping" comment instead of reviewing.
8. **Policy test**: From inside the sandbox, attempt `gh pr merge` or `git push` — should be denied by policy.
9. **Resilience test**: Kill the port-forward, verify the sandbox keeps running, reconnect the port-forward, verify the CLI can reach the gateway again.
