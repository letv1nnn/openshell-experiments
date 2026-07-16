# PR Review Agent — V2 Plan

## Context

V1 shipped a working agent: sandbox running on OpenShift, polling GitHub, invoking OpenCode for reviews, posting results. The infrastructure (manifests, gateway, TLS, Vertex AI routing) is solid and unchanged. The payload — the bash scripts inside the sandbox — has enough structural problems that incremental patching is the wrong call.

Identified faults driving V2:

1. **Prior review injection is architecturally broken**: the "system prompt" is passed as a positional argument to `opencode run`, not as a true system prompt. Everything lands in one undifferentiated user-turn blob. The model received prior review context on PR #2's second review and produced no "Previous Review Follow-up" section. User-message instruction compliance is unreliable for buried content.
2. **Silent error suppression pervasive**: `2>/dev/null || echo "[]"` appears throughout the bash libs. API failures silently become empty data. No log entry. No indication anything went wrong.
3. **Log file grows unboundedly**: `tee -a "${LOG_FILE}"` appends forever with no rotation. Will eventually fill the PVC.
4. **Label skip matching is a substring match**: `grep -qF "wip"` matches any label containing "wip" as a substring (e.g. "wippy"). Should be exact set membership.
5. **Fragile YAML parser**: the regex-based stdlib parser drops block-sequence `ignore_labels`, multi-line values, and anchors. Failure mode is silent — agent watches zero repos with only a non-alarming warning log.
6. **`state_should_retry` name is inverted**: returns exit code 0 (bash-true) when the retry cap is *exceeded* and the PR should be *skipped*. The name implies the opposite.
7. **Prompt passed as shell argument**: `opencode run "$(cat prompt.md)"` runs PR titles, review bodies, and code snippets through shell word-splitting. Fails unpredictably on backticks, `$(...)`, and special characters in real-world input.
8. **Bash is the wrong tool for this complexity**: every significant operation already embeds a Python heredoc (JSON, config parsing, metadata extraction). The bash layer adds quoting fragility, opaque error codes, and makes unit testing impossible.

## What V2 Changes

- The payload (`payload/`) is rewritten in Python.
- The sandbox base image is replaced with a custom UBI Python image (`Containerfile`) that includes all required tools.
- The sandbox launch command changes to `python3 /sandbox/payload/entrypoint.py`.

Everything else — manifests, gateway, TLS PKI, deploy scripts, `policy.yaml`, `config.yaml` schema — is **unchanged**.

## What Stays the Same

- All of `manifests/`
- All of `scripts/` (except the sandbox launch command in `setup-providers.sh`)
- `certs/` generation
- `config.yaml` schema (block-sequence `ignore_labels` now correctly supported)
- `policy.yaml` (binary paths updated to match UBI image locations)
- `payload/prompts/review-system.md` (content unchanged, loading mechanism changes)

## Custom Sandbox Image

Rather than vendoring dependencies or adding PyPI network access to the sandbox policy, V2 uses a purpose-built container image that includes everything the agent needs. Build once, reference from the sandbox create command.

### `Containerfile`

```dockerfile
FROM registry.access.redhat.com/ubi9/python-311:latest

USER root

# OpenShell policy.yaml requires run_as_user: sandbox / run_as_group: sandbox
RUN groupadd -r sandbox && useradd -r -g sandbox -d /sandbox sandbox

# git
RUN dnf install -y git && dnf clean all

# gh CLI — GitHub's RPM repo
RUN curl -fsSL https://cli.github.com/packages/rpm/gh-cli.repo \
      -o /etc/yum.repos.d/github-cli.repo \
    && dnf install -y gh \
    && dnf clean all

# opencode — binary release from GitHub
# Pin version rather than fetching latest at build time to keep builds reproducible.
ARG OPENCODE_VERSION=1.17.13
RUN curl -fsSL \
      "https://github.com/sst/opencode/releases/download/v${OPENCODE_VERSION}/opencode_linux_amd64" \
      -o /usr/local/bin/opencode \
    && chmod +x /usr/local/bin/opencode

# Python dependencies
RUN pip3 install --no-cache-dir pyyaml

USER sandbox
WORKDIR /sandbox
```

**Why UBI Python over the OpenShell base + pip install pyyaml**: the OpenShell base image is an opaque prebuilt; any `pip3` call inside it may fail if pip is absent or outdated. UBI Python provides a supported, Red Hat maintained Python runtime with pip. The cost is having to install `gh`, `git`, and `opencode` explicitly — but these are pinned installations and the Containerfile is fully auditable.

**Trade-off to be aware of**: the OpenShell base image may set up things beyond the `sandbox` user (seccomp profiles, Landlock helpers, supervisor init). If the UBI image is missing a required setup step, sandbox pod startup will fail. Validate by running `openshell sandbox connect pr-reviewer` and checking that the supervisor process is healthy after the first deployment.

**OPENCODE_VERSION**: pin this to a specific release tag. Fetching `latest` at build time makes builds non-reproducible — two builds a week apart may produce different binaries. Update the pin deliberately.

### Building and pushing

```bash
mk build --push --tag pr-reviewer-sandbox
# produces: quay.io/mcampbel/pr-reviewer-sandbox:latest
```

### Binary paths for `policy.yaml`

UBI Python installs binaries at different paths than the OpenShell base. Update `policy.yaml` accordingly:

```yaml
  github_api:
    binaries:
      - { path: /usr/bin/gh }

  github_git:
    binaries:
      - { path: /usr/bin/git }
      - { path: /usr/libexec/git-core/git-remote-https }  # UBI path differs from Debian

  kubernetes_config:
    binaries:
      - { path: /usr/bin/curl }
      - { path: /usr/bin/python3.11 }   # sync_config uses urllib, not curl

  opencode_telemetry:
    binaries:
      - { path: /usr/local/bin/opencode }
```

Verify paths after the first image build: `docker run --rm quay.io/mcampbel/pr-reviewer-sandbox:latest which gh git python3 opencode`.

### Sandbox create command

Update `scripts/setup-providers.sh` to reference the custom image and use `python3` as the entrypoint:

```bash
openshell sandbox create \
  --name pr-reviewer \
  --from quay.io/mcampbel/pr-reviewer-sandbox:latest \
  --provider vertex-pr-reviewer \
  --provider github-pr-reviewer \
  --policy policy.yaml \
  --upload ./payload:/sandbox \
  --upload ./config.yaml:/sandbox/pr-review-agent/ \
  --no-tty \
  -- python3 /sandbox/payload/entrypoint.py
```

`--from` accepts an image reference in addition to the registered sandbox type names. Confirm this is supported by the installed OpenShell version — if not, the image must be registered first via `openshell sandbox image add`.

## File Structure (payload/)

```
payload/
├── requirements.txt            # pyyaml (for local dev/testing outside sandbox)
├── entrypoint.py               # main polling loop
├── review.py                   # per-PR review (invoked as subprocess by entrypoint)
├── prompts/
│   └── review-system.md        # unchanged
└── lib/
    ├── __init__.py
    ├── config.py
    ├── github.py
    ├── state.py
    └── repos.py
```

`requirements.txt` contains `pyyaml>=6.0`. It is not used inside the sandbox (pyyaml is baked into the image) but enables `pip install -r requirements.txt` for local development and testing outside the sandbox.

## Logging

Configure in `entrypoint.py` before any imports:

```python
import logging, sys

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
```

Each module gets its own named logger: `logging.getLogger("config")`, `logging.getLogger("github")`, `logging.getLogger("state")`, etc. This makes `grep '\[github\]'` useful in practice.

Log levels:
- `DEBUG`: per-PR skip decisions ("already reviewed at abc123")
- `INFO`: poll cycle events, review posted, config reloaded, clone/fetch
- `WARNING`: rate limit approached, retry recorded, config has unexpected shape
- `ERROR`: subprocess failures (with stderr captured and logged), empty review output, API errors

**No log file**. Log to stdout only. OpenShift kubelet rotates container logs. The `tee -a` pattern is removed entirely — it created an unbounded file with no rotation.

**No `2>/dev/null`**. All subprocess calls use `capture_output=True`. On failure, `stderr` is logged at ERROR level before raising.

## Implementation

### `lib/config.py`

```python
import yaml, os, ssl, urllib.request, json as _json, logging

log = logging.getLogger("config")

def sync_config(config_file: str, namespace: str = "openshell") -> bool:
    """Pull config from K8s ConfigMap. Returns True if file was updated."""
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    if not os.path.exists(token_path):
        return False
    try:
        token = open(token_path).read().strip()
        url = (f"https://kubernetes.default.svc/api/v1/namespaces"
               f"/{namespace}/configmaps/pr-review-agent-config")
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            content = _json.loads(resp.read()).get("data", {}).get("config.yaml", "")
        if content:
            with open(config_file, "w") as f:
                f.write(content)
            log.info("Config synced from ConfigMap.")
            return True
    except Exception as e:
        log.warning("K8s config sync failed: %s — using local file.", e)
    return False

def load_config(path: str) -> dict:
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"config.yaml is not a YAML mapping: {path}")
    repos = raw.get("repos")
    if not repos:
        raise ValueError("config.yaml has no repos[] entries.")
    for i, r in enumerate(repos):
        if not r.get("org") or not r.get("repo"):
            raise ValueError(f"repos[{i}] is missing 'org' or 'repo'.")
    return raw
```

`yaml.safe_load` replaces the regex-based parser entirely. Supports block sequences, inline sequences, multi-line values, and anchors. Validation raises `ValueError` with a clear message rather than silently watching zero repos.

### `lib/github.py`

```python
import subprocess, json, time, logging

log = logging.getLogger("github")
RATE_LIMIT_THRESHOLD = 100

def _gh(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["gh", *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("gh %s failed (exit %d):\n%s", " ".join(args), result.returncode, result.stderr)
        result.check_returncode()  # raises CalledProcessError
    return result

def check_rate_limit() -> None:
    try:
        data = json.loads(_gh("api", "rate_limit").stdout)
        remaining = data["rate"]["remaining"]
        if remaining < RATE_LIMIT_THRESHOLD:
            reset = data["rate"]["reset"]
            sleep_secs = max(0, reset - int(time.time()) + 5)
            log.warning("Rate limit low (%d remaining). Sleeping %ds.", remaining, sleep_secs)
            time.sleep(sleep_secs)
    except Exception as e:
        log.warning("Rate limit check failed: %s", e)

def list_open_prs(org: str, repo: str, ignore_drafts: bool = True) -> list[dict]:
    check_rate_limit()
    prs = json.loads(_gh("api", f"repos/{org}/{repo}/pulls?state=open&per_page=100").stdout)
    if ignore_drafts:
        prs = [p for p in prs if not p.get("draft", False)]
    return [{"number": p["number"], "head_sha": p["head"]["sha"], "title": p["title"]}
            for p in prs]

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

def post_review(org: str, repo: str, pr_number: int, body: str) -> None:
    check_rate_limit()
    _gh("api", f"repos/{org}/{repo}/pulls/{pr_number}/reviews",
        "-X", "POST", "-f", f"body={body}", "-f", "event=COMMENT")
```

**Label fix**: `pr_label_names & set(ignore_labels)` is set intersection. "wip" only matches a label whose name is exactly "wip". No substring matching.

**Error visibility**: `_gh()` logs the full stderr and exit code before raising. Callers catch `subprocess.CalledProcessError` and decide whether to log ERROR and skip, or let it propagate.

### `lib/state.py`

Direct port of V1 state logic. Key changes:

- **Rename**: `state_should_retry` → `retry_cap_exceeded`. Returns `True` when the failure count meets or exceeds `max_retries`, meaning the PR should be skipped. The V1 name was inverted.
- **Corrupt file recovery**: if `reviewed.json` or `failures.json` cannot be parsed, log ERROR and reset to `[]` rather than crashing the loop.

```python
def retry_cap_exceeded(state_dir, org, repo, pr_number, head_sha, max_retries=3) -> bool:
    """Returns True when failure count >= max_retries (PR should be skipped)."""
    return get_failure_count(state_dir, org, repo, pr_number, head_sha) >= max_retries
```

Everything else (key format, file paths, cleanup logic) is identical to V1.

### `lib/repos.py`

```python
import subprocess, os, logging

log = logging.getLogger("repos")

def clone_or_fetch(org: str, repo: str, repos_base: str) -> None:
    dest = os.path.join(repos_base, org, repo)
    if not os.path.isdir(os.path.join(dest, ".git")):
        log.info("Cloning %s/%s...", org, repo)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth=1",
             f"https://github.com/{org}/{repo}.git", dest],
            check=True, capture_output=True,
        )
    else:
        log.info("Fetching %s/%s...", org, repo)
        subprocess.run(
            ["git", "-C", dest, "fetch", "--prune", "--depth=1", "origin"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", dest, "checkout", "FETCH_HEAD"],
            check=True, capture_output=True,
        )
```

### `review.py` — Prior Review Injection Fix

The architectural change that fixes the prior review problem: the system instructions, context, and diff are written to separate files and each passed to OpenCode via `-f`. The main prompt argument is short and unambiguous.

**Why this works**: OpenCode attaches `-f` files to the conversation alongside the user message. Separating the review instructions from the context gives the model clear delineation — the instructions aren't buried in a 3000-character blob. The "Previous Review Follow-up" instruction is in `instructions.md` where it will be read, not somewhere in the middle of a concatenated string.

```python
import os, sys, subprocess, tempfile, textwrap, logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

from config import load_config
from github import get_pr_diff, get_prior_reviews, post_review
from state import STATE_DIR

log = logging.getLogger("review")

def build_context(pr_meta: dict, repo_dir: str, prior_reviews: list[dict]) -> str:
    parts = ["## PR Description\n", pr_meta.get("body") or "(no description)", "\n"]

    contributing = os.path.join(repo_dir, "CONTRIBUTING.md")
    if os.path.exists(contributing):
        parts += ["\n## CONTRIBUTING.md\n"]
        with open(contributing) as f:
            parts.append("".join(f.readlines()[:100]))

    if prior_reviews:
        parts += ["\n## Prior Reviews\n",
                  "The following reviews were posted on earlier commits of this PR "
                  "(oldest first, capped at 5). Cross-reference with the diff to "
                  "determine what has been addressed.\n"]
        for r in prior_reviews[-5:]:
            author = r.get("user", {}).get("login", "unknown")
            submitted = r.get("submitted_at", "")
            commit = r.get("commit_id", "")[:8]
            parts.append(f"\n### Review by @{author} — {submitted} (commit {commit})\n\n")
            parts.append(r.get("body", "").strip())
            parts.append("\n")

    return "".join(parts)

def render_instructions(template_path: str, org: str, repo: str,
                         pr_number: int, pr_title: str) -> str:
    with open(template_path) as f:
        t = f.read()
    return (t.replace("{{ORG}}", org)
             .replace("{{REPO}}", repo)
             .replace("{{PR_NUMBER}}", str(pr_number))
             .replace("{{PR_TITLE}}", pr_title))

def run_review(org, repo, pr_number, head_sha, payload_dir, config):
    review_cfg = config.get("review_settings", {})
    max_diff_lines = review_cfg.get("max_diff_lines", 8000)
    max_files = review_cfg.get("max_files_changed", 50)
    timeout = review_cfg.get("review_timeout_seconds", 600)
    comment_prefix = review_cfg.get("comment_prefix", "**AI PR Review**")

    import json as _json
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", f"{org}/{repo}",
         "--json", "title,body,additions,deletions,changedFiles"],
        capture_output=True, text=True, check=True,
    )
    meta = _json.loads(result.stdout)
    total_lines = meta["additions"] + meta["deletions"]

    if total_lines > max_diff_lines:
        log.warning("SKIP: diff too large (%d lines > %d)", total_lines, max_diff_lines)
        post_review(org, repo, pr_number,
                    f"{comment_prefix}\n\nThis PR is too large to review automatically "
                    f"({total_lines} changed lines, limit {max_diff_lines}). "
                    "Break it into smaller PRs.")
        return True  # mark as reviewed so we don't keep posting this

    if meta["changedFiles"] > max_files:
        log.warning("SKIP: too many files (%d > %d)", meta["changedFiles"], max_files)
        post_review(org, repo, pr_number,
                    f"{comment_prefix}\n\nThis PR touches too many files to review "
                    f"automatically ({meta['changedFiles']} files, limit {max_files}).")
        return True

    diff = get_pr_diff(org, repo, pr_number)
    if not diff.strip():
        log.info("SKIP: empty diff for %s/%s#%s", org, repo, pr_number)
        return True

    prior_reviews = get_prior_reviews(org, repo, pr_number)
    log.info("Found %d prior review(s).", len(prior_reviews))

    repos_base = config.get("repos_base", "/sandbox/pr-review-agent/repos")
    repo_dir = os.path.join(repos_base, org, repo)

    with tempfile.TemporaryDirectory() as tmp:
        instr_file = os.path.join(tmp, "instructions.md")
        ctx_file = os.path.join(tmp, "context.md")
        diff_file = os.path.join(tmp, "pr.patch")
        out_file = os.path.join(tmp, "review-output.md")

        template = os.path.join(payload_dir, "prompts", "review-system.md")
        with open(instr_file, "w") as f:
            f.write(render_instructions(template, org, repo, pr_number, meta["title"]))

        with open(ctx_file, "w") as f:
            f.write(build_context(meta, repo_dir, prior_reviews))

        with open(diff_file, "w") as f:
            f.write(diff)

        log.info("Running OpenCode (timeout %ds)...", timeout)
        proc = subprocess.run(
            [
                "opencode", "run",
                ("Review the pull request following the instructions in instructions.md. "
                 "Context (PR description, prior reviews) is in context.md. "
                 "The diff is in pr.patch."),
                "--model", "anthropic/claude-sonnet-4-6",
                "-f", instr_file,
                "-f", ctx_file,
                "-f", diff_file,
            ],
            env={**os.environ,
                 "ANTHROPIC_BASE_URL": "https://inference.local/v1",
                 "ANTHROPIC_API_KEY": "unused"},
            capture_output=True, text=True,
            timeout=timeout,
        )

        if proc.returncode != 0:
            log.error("OpenCode exited %d:\n%s", proc.returncode, proc.stderr)
            return False

        if not proc.stdout.strip():
            log.error("OpenCode produced empty output. stderr:\n%s", proc.stderr)
            return False

        review_body = f"{comment_prefix}\n\n{proc.stdout.strip()}"
        post_review(org, repo, pr_number, review_body)
        log.info("Review posted for %s/%s#%s.", org, repo, pr_number)
        return True

if __name__ == "__main__":
    import json
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%SZ")
    org, repo, pr_number, head_sha = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
    payload_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = os.environ.get("CONFIG_FILE", "/sandbox/pr-review-agent/config.yaml")
    config = load_config(config_file)
    success = run_review(org, repo, pr_number, head_sha, payload_dir, config)
    sys.exit(0 if success else 1)
```

### `entrypoint.py` — Main Loop

```python
#!/usr/bin/env python3
import logging, os, signal, subprocess, sys, time

logging.basicConfig(
    stream=sys.stdout, level=logging.INFO,
    format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("entrypoint")

PAYLOAD_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = "/sandbox/pr-review-agent/state"
CONFIG_FILE = "/sandbox/pr-review-agent/config.yaml"
REPOS_BASE = "/sandbox/pr-review-agent/repos"
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat")

sys.path.insert(0, os.path.join(PAYLOAD_DIR, "lib"))
from config import sync_config, load_config
from github import check_rate_limit, list_open_prs, should_skip_pr
from state import is_reviewed, mark_reviewed, record_failure, retry_cap_exceeded, cleanup
from repos import clone_or_fetch

def handle_signal(sig, frame):
    log.info("Received signal %d, shutting down.", sig)
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)

def run_review_subprocess(org, repo, pr_number, head_sha, review_timeout) -> bool:
    try:
        result = subprocess.run(
            ["python3", os.path.join(PAYLOAD_DIR, "review.py"),
             org, repo, str(pr_number), head_sha],
            timeout=review_timeout + 30,  # outer timeout: review timeout + startup
            capture_output=True, text=True,
            env={**os.environ, "CONFIG_FILE": CONFIG_FILE},
        )
        if result.stdout:
            for line in result.stdout.splitlines():
                log.info("[review subprocess] %s", line)
        if result.returncode != 0:
            if result.stderr:
                log.error("[review subprocess stderr]\n%s", result.stderr)
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("Review timed out for %s/%s#%s (limit %ds).", org, repo, pr_number, review_timeout)
        return False

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(REPOS_BASE, exist_ok=True)

log.info("PR Review Agent starting.")

result = subprocess.run(["gh", "auth", "status"], capture_output=True)
if result.returncode != 0:
    log.error("gh auth failed. Ensure the GitHub provider is attached.\n%s", result.stderr.decode())
    sys.exit(1)
log.info("GitHub auth: OK")

sync_config(CONFIG_FILE)
try:
    config = load_config(CONFIG_FILE)
except (ValueError, FileNotFoundError) as e:
    log.error("Config error: %s", e)
    sys.exit(1)

repos = config.get("repos", [])
review_cfg = config.get("review_settings", {})
polling_interval = config.get("polling_interval_seconds", 120)

log.info("Performing initial clone of %d repo(s)...", len(repos))
for r in repos:
    try:
        clone_or_fetch(r["org"], r["repo"], REPOS_BASE)
    except Exception as e:
        log.warning("Initial clone failed for %s/%s: %s", r["org"], r["repo"], e)

log.info("Starting polling loop (interval: %ds).", polling_interval)

while True:
    with open(HEARTBEAT_FILE, "w") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    sync_config(CONFIG_FILE)
    try:
        config = load_config(CONFIG_FILE)
    except Exception as e:
        log.error("Config reload failed: %s — continuing with previous config.", e)

    repos = config.get("repos", [])
    review_cfg = config.get("review_settings", {})
    polling_interval = config.get("polling_interval_seconds", 120)
    review_timeout = review_cfg.get("review_timeout_seconds", 600)
    max_retries = review_cfg.get("max_retries", 3)

    for r in repos:
        org, repo = r["org"], r["repo"]
        ignore_drafts = r.get("ignore_drafts", True)
        ignore_labels = r.get("ignore_labels", [])

        log.info("Polling %s/%s...", org, repo)

        try:
            clone_or_fetch(org, repo, REPOS_BASE)
        except Exception as e:
            log.error("Fetch failed for %s/%s: %s", org, repo, e)
            continue

        try:
            prs = list_open_prs(org, repo, ignore_drafts)
        except Exception as e:
            log.error("list_open_prs failed for %s/%s: %s", org, repo, e)
            continue

        if not prs:
            log.info("No open PRs in %s/%s.", org, repo)
            continue

        for pr in prs:
            pr_number = pr["number"]
            head_sha = pr["head_sha"]
            pr_title = pr["title"]

            if is_reviewed(STATE_DIR, org, repo, pr_number, head_sha):
                log.debug("SKIP %s/%s#%s: already reviewed at %s", org, repo, pr_number, head_sha[:8])
                continue

            if retry_cap_exceeded(STATE_DIR, org, repo, pr_number, head_sha, max_retries):
                log.debug("SKIP %s/%s#%s: retry cap exceeded for %s", org, repo, pr_number, head_sha[:8])
                continue

            try:
                if should_skip_pr(org, repo, pr_number, ignore_labels):
                    log.info("SKIP %s/%s#%s: ignored label.", org, repo, pr_number)
                    continue
            except Exception as e:
                log.warning("Label check failed for %s/%s#%s: %s", org, repo, pr_number, e)

            log.info("Reviewing %s/%s#%s: %s", org, repo, pr_number, pr_title)

            if run_review_subprocess(org, repo, pr_number, head_sha, review_timeout):
                mark_reviewed(STATE_DIR, org, repo, pr_number, head_sha)
                log.info("OK %s/%s#%s reviewed.", org, repo, pr_number)
            else:
                record_failure(STATE_DIR, org, repo, pr_number, head_sha)
                from state import get_failure_count
                count = get_failure_count(STATE_DIR, org, repo, pr_number, head_sha)
                log.error("ERROR %s/%s#%s: review failed. Attempt %d/%d.",
                          org, repo, pr_number, count, max_retries)

    cleanup(STATE_DIR, days=30)
    log.info("Cycle complete. Sleeping %ds...", polling_interval)
    time.sleep(polling_interval)
```

## Fixes Summary

| Fault | Fix |
|---|---|
| Prior review injection unreliable | Separate `-f` files for instructions, context, diff; model receives structured input, not one blob |
| Silent error suppression (`2>/dev/null`) | All subprocess calls use `capture_output=True`; errors log at ERROR level before raising |
| Log file grows unboundedly | stdout only; kubelet handles rotation; `tee -a` removed |
| Label match is substring | Set intersection: `pr_label_names & set(ignore_labels)` — exact match only |
| Fragile YAML parser | `yaml.safe_load()` via pyyaml baked into image; handles all valid YAML |
| `state_should_retry` inverted name | Renamed `retry_cap_exceeded()`, returns `True` when PR should be skipped |
| Prompt as shell argument | Files written to tmpdir, each passed via `-f`; no shell interpolation of content |
| Bash unsuitable for this complexity | Full Python rewrite of payload |
| No Python runtime with pyyaml | Custom UBI Python sandbox image with all tools baked in |

## Migration from V1

1. `scripts/teardown.sh` — default mode (leaves gateway running)
2. Build and push the custom image: `mk build --push --tag pr-reviewer-sandbox`
3. Update `policy.yaml` binary paths for UBI filesystem layout
4. Update `scripts/setup-providers.sh` launch command
5. `scripts/setup-providers.sh`

State is lost on sandbox recreation (by design — `reviewed.json` lives in the sandbox filesystem). Open PRs will be re-reviewed once on the new SHA. Identical diff produces near-identical review; this is acceptable.

## What V2 Does Not Change

- Review prompt content in `review-system.md` — the prior review follow-up instruction is already correct; V2 delivers it more reliably, not differently.
- State file schema — `reviewed.json` and `failures.json` format is identical.
- Polling model — pull-based, no webhooks, configurable interval.
- Sandbox network policy scope — L7-pinned; no new external hosts permitted.
- Size limit behaviour — too-large PRs still get a comment and are marked reviewed at that SHA.
- `config.yaml` schema — compatible; block sequences now correctly supported.
