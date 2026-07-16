# OpenShell Experiments

Sandboxed AI agent experiments using [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) on OpenShift. Each project deploys an autonomous agent inside an OpenShell sandbox with L7 network policy enforcement, Landlock filesystem isolation, and seccomp filtering — so agents can only reach the APIs they're explicitly allowed to.

## Projects

### [Leadership Report Agent](leadership-report-openshell/)

A weekly CronJob that fetches Google Meet/Gemini meeting notes from Drive, sends them through a vLLM-hosted LLM for analysis, and publishes a structured leadership report to a Google Doc. Runs inside an OpenShell sandbox on OpenShift with L7 network rules scoped to specific Google API endpoints and `inference.local`.

**Stack:** Python, vLLM (Llama 3.1 8B), Google Drive/Docs APIs, OpenShift CronJob

**Status:** Deployed

### [PR Review Agent](pr-review-agent/)

A persistent sandbox agent that polls GitHub for open PRs across multiple repos, generates senior-engineer-level code reviews using Claude (via Vertex AI), and posts them as GitHub Reviews with inline diff comments.

Each review runs as an isolated subprocess that invokes [OpenCode](https://opencode.ai) with three context files: the review instructions, PR metadata (description, prior reviews, CONTRIBUTING.md), and the annotated diff. The diff is pre-processed to stamp new-file line numbers on every context and addition line, giving the model precise anchors for inline comment placement. Before invoking OpenCode, the agent fetches the PR branch head and greps the live tree for usages of any renamed or removed public symbols in files outside the diff — broken callers are surfaced in the review even when they aren't part of the change.

Reviews run in parallel across repos. State is persisted with atomic writes so sandbox restarts don't double-post. A heal check on startup reconciles local state against GitHub's review history.

**Stack:** Python, OpenCode, GitHub CLI, Vertex AI (Claude), OpenShell persistent sandbox

**Status:** Deployed

## Common Patterns

Both agents share an architecture where OpenShell provides the security boundary:

- **Sandboxed execution** — agents run under Landlock + seccomp + network namespace isolation
- **L7 network policy** — per-endpoint, per-method/path rules (e.g., allow `GET /drive/v3/files` but block everything else)
- **Inference routing** — `inference.local` is intercepted by the OpenShell proxy and routed to the configured LLM provider (vLLM, Vertex AI)
- **OpenShift deployment** — Helm chart with privileged SCC for sandbox capabilities, RBAC-scoped service accounts, and NetworkPolicy for gateway ingress

## Prerequisites

| Tool | Version |
|------|---------|
| OpenShift | 4.19+ |
| Helm | 3.x |
| `oc` CLI | 4.18+ |
| [OpenShell CLI](https://github.com/NVIDIA/OpenShell) | Latest |

## Getting Started

Each project has its own README with deployment instructions. Start with the [Leadership Report Agent](leadership-report-openshell/README.md) for a working end-to-end example.
