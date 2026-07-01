# OpenShell Experiments

Sandboxed AI agent experiments using [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) on OpenShift. Each project deploys an autonomous agent inside an OpenShell sandbox with L7 network policy enforcement, Landlock filesystem isolation, and seccomp filtering — so agents can only reach the APIs they're explicitly allowed to.

## Projects

### [Leadership Report Agent](leadership-report-openshell/)

A weekly CronJob that fetches Google Meet/Gemini meeting notes from Drive, sends them through a vLLM-hosted LLM for analysis, and publishes a structured leadership report to a Google Doc. Runs inside an OpenShell sandbox on OpenShift with L7 network rules scoped to specific Google API endpoints and `inference.local`.

**Stack:** Python, vLLM (Llama 3.1 8B), Google Drive/Docs APIs, OpenShift CronJob

**Status:** Deployed

### [PR Review Agent](pr-review-agent/) *(planned)*

A persistent sandbox agent that polls GitHub for new/updated PRs across multiple repos and orgs, generates senior-engineer-level reviews using Claude Code (`--bare -p`), and posts them via `gh pr review`. Uses Vertex AI for Claude inference, with config delivered via a Kubernetes ConfigMap for live repo watchlist updates without restarting the sandbox.

**Stack:** Bash, Claude Code, GitHub CLI, Vertex AI (Claude), OpenShell persistent sandbox

**Status:** Design phase — see [PLAN.md](pr-review-agent/PLAN.md)

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
