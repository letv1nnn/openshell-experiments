# OpenShell on OpenShift

Deploy [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) on an OpenShift cluster with vLLM-hosted inference and a weekly scheduled skill execution via CronJob.

> **Warning:** This deployment disables several OpenShell security features to work on OpenShift. See [Security Considerations](#security-considerations) before running in production.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     OpenShift Cluster                        │
│                                                             │
│  ┌─────────────┐    ┌───────────────────┐    ┌───────────┐  │
│  │  OpenShell   │───▶│  Sandboxed Agent  │───▶│   vLLM    │  │
│  │   Gateway    │    │ ┌───────────────┐ │    │ (via      │  │
│  │ (openshell-0)│    │ │  Supervisor   │ │    │ inference │  │
│  └──────┬───────┘    │ │  - Landlock   │ │    │  .local)  │  │
│         │            │ │  - seccomp    │ │    └───────────┘  │
│         │            │ │  - net proxy  │ │                   │
│  ┌──────┴───────┐    │ └───────┬───────┘ │    ┌───────────┐  │
│  │   CronJob    │    │ ┌──────┴────────┐ │───▶│  Google   │  │
│  │  (weekly)    │    │ │  agent.py     │ │    │  APIs     │  │
│  │ openshell    │    │ │  fetch→llm→   │ │    │ (policy-  │  │
│  │ sandbox      │    │ │  push         │ │    │  gated)   │  │
│  │ create       │    │ └───────────────┘ │    └───────────┘  │
│  └──────────────┘    └───────────────────┘                   │
└─────────────────────────────────────────────────────────────┘
```

## Cluster Requirements

| Component | Version |
|-----------|---------|
| OpenShift | 4.19+ |
| Kubernetes | v1.32+ |
| Helm | 3.x |
| `oc` CLI | 4.18+ |

This was tested on:
- OpenShift 4.19.18 (Kubernetes v1.32.9)
- ROSA, 4 worker nodes
- CRI-O 1.32.9, RHEL CoreOS 9.6

---

## Deployment Steps

### Step 1: Install Agent Sandbox CRDs

OpenShell's Kubernetes driver depends on the [agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox) CRDs.

```shell
oc apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.4.6/manifest.yaml
```

> **Note:** Pinned to v0.4.6 because it uses `v1alpha1` as the CRD storage version. The OpenShell gateway v0.0.70 only recognizes `v1alpha1` in sandbox ownerReferences — agent-sandbox v0.5.0+ uses `v1beta1` as storage, which causes the sandbox bootstrap to fail with `PERMISSION_DENIED`.

Verify:

```shell
oc get crd sandboxes.agents.x-k8s.io
```

### Step 2: Create Namespace, SCC Binding, and RBAC

OpenShell sandboxes require privileged access for Landlock, seccomp, and network namespace isolation. The RBAC manifest restricts the CronJob service account to only the specific secrets and ConfigMaps it needs.

```shell
oc apply -f manifests/01-namespace.yaml
oc apply -f manifests/02-scc-binding.yaml
oc apply -f manifests/05-rbac.yaml
```

### Step 3: Deploy the Gateway via Helm

Install the chart from the OCI registry with OpenShift-specific overrides:

```shell
helm install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  -n openshell \
  -f manifests/03-helm-values.yaml
```

For subsequent upgrades, skip the certgen hook — the JWT signing keys from the initial install are reused, and the hook's service account lacks RBAC to list secrets on OpenShift:

```shell
helm upgrade openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  -n openshell \
  -f manifests/03-helm-values.yaml \
  --no-hooks
```

Wait for the gateway to be ready:

```shell
oc wait --for=condition=Ready pod/openshell-0 -n openshell --timeout=120s
```

Verify:

```shell
oc get pods -n openshell
oc logs openshell-0 -n openshell | head -20
```

You should see `Server listening address=0.0.0.0:8080` and `Health server listening` in the logs.

Apply the NetworkPolicy to restrict gateway ingress to only pods in the `openshell` namespace and the agent-sandbox controller:

```shell
oc apply -f manifests/05-network-policy.yaml
```

### Step 4: Expose the Gateway

Create a Route so the CLI (and CronJob) can reach the gateway from outside the cluster, or use a ClusterIP service if accessing only from within.

**Option A — Internal only (ClusterIP, already created by Helm):**

The service `openshell.openshell.svc.cluster.local:8080` is reachable from any pod in the cluster.

**Option B — Local CLI access via port-forward (RECOMMENDED):**

The gateway's browser auth flow is designed for Cloudflare Access and does not work behind an OpenShift Route. Use `oc port-forward` to register a plaintext gateway from your local machine:

```shell
# Terminal 1: forward the gateway port
oc port-forward openshell-0 8080:8080 -n openshell

# Terminal 2: register as a plaintext gateway (http:// skips browser auth)
openshell gateway add http://localhost:8080 --name openshift
```

**Option C — External access via Route (cluster-internal consumers only):**

The Route exposes the gateway for HTTP/gRPC access but cannot be used with `openshell gateway add` (browser auth requires Cloudflare Access). It is useful if other services outside the cluster need to reach the gateway API directly.

```shell
oc apply -f manifests/04-route.yaml
```

### Step 5: Configure vLLM as Inference Provider

One-time gateway setup. This registers vLLM as the inference backend so sandboxes can reach it via `inference.local`. Run these commands through the port-forwarded gateway from Step 4B. The configuration persists on the gateway across pod restarts.

With the port-forward running (`oc port-forward openshell-0 8080:8080 -n openshell`):

```shell
openshell provider create \
  --name vllm \
  --type openai \
  --config "OPENAI_BASE_URL=http://vllm.default.svc.cluster.local:8000/v1" \
  --credential OPENAI_API_KEY=none

openshell inference set \
  --provider vllm \
  --model meta-llama/Llama-3.1-8B-Instruct
```

> **Important:** `OPENAI_BASE_URL` must be passed via `--config`, not `--credential`. The gateway looks for base URL overrides in the provider's config map only. If passed as a credential, the URL is silently ignored and requests route to `api.openai.com` instead.

Verify:

```shell
openshell inference get
```

To change the provider later, re-run the commands above with updated values.

### Step 6: Build Images

Two images are needed:

**Launcher image** — a minimal image with the `openshell` CLI and a shell. The gateway image is distroless and cannot run shell scripts, so the CronJob uses this image to render the policy template and call `openshell sandbox create`.

```shell
podman build -f launcher/Containerfile \
  -t quay.io/<your-org>/openshell-launcher:latest \
  launcher/
podman push quay.io/<your-org>/openshell-launcher:latest
```

**Agent image** — the self-contained Python agent that fetches meeting notes, analyzes them via vLLM, and publishes a formatted report to Google Docs. See its [README](leadership-report-agent/README.md) for details.

```shell
podman build -f leadership-report-agent/Containerfile \
  -t quay.io/<your-org>/leadership-report-agent:latest \
  leadership-report-agent/
podman push quay.io/<your-org>/leadership-report-agent:latest
```

### Step 7: Sandbox Policy

The sandbox policy controls what the agent can access at runtime. It is defined as a ConfigMap template in `manifests/06-cronjob.yaml`.

The policy enforces:

- **L7 network rules** — only specific Google API methods/paths and `inference.local` are reachable; all other egress is blocked
  - **Drive**: `GET /drive/v3/files` and `GET /drive/v3/files/**` only (read-only)
  - **Docs**: `GET` and `POST :batchUpdate` scoped to a single doc ID (no access to other documents)
  - **OAuth2**: `POST /token` only (token refresh, no other auth flows)
  - **Inference**: `inference.local` is intercepted by the OpenShell proxy and routed to vLLM
- **Filesystem isolation** — Landlock restricts reads to `/usr`, `/lib`, `/etc`, `/app` and writes to `/sandbox`, `/tmp` only
- **Process isolation** — agent runs as unprivileged `sandbox` user/group under seccomp

The doc ID is templated as `__DOC_ID__` in the policy. At runtime, the CronJob launcher renders it from the `leadership-report-config` secret via `sed` before passing the policy to `openshell sandbox create`.

To customize the policy for a different agent, edit the `sandbox-policy.yaml.tmpl` section in `manifests/06-cronjob.yaml`.

### Step 8: Deploy the Weekly CronJob

The CronJob uses `openshell sandbox create` to launch the agent inside a sandboxed pod every Wednesday at 18:00 UTC with the policy from Step 7.

1. Create the secrets:

```shell
# Google OAuth2 credentials for Drive/Docs API access
oc create secret generic google-credentials \
  -n openshell \
  --from-literal=client-id=YOUR_CLIENT_ID \
  --from-literal=client-secret=YOUR_CLIENT_SECRET \
  --from-literal=refresh-token=YOUR_REFRESH_TOKEN

# Agent configuration
oc create secret generic leadership-report-config \
  -n openshell \
  --from-literal=doc-id=YOUR_GOOGLE_DOC_ID \
  --from-literal=model-id=meta-llama/Llama-3.1-8B-Instruct \
  --from-literal=meeting-name="Your Meeting Name" \
  --from-literal=gcp-project=your-gcp-project
```

2. Edit `manifests/06-cronjob.yaml` — set your agent image.

3. Apply:

```shell
oc apply -f manifests/06-cronjob.yaml
```

4. Test with a manual trigger:

```shell
oc create job --from=cronjob/leadership-report-agent test-run -n openshell
oc logs -f job/test-run -n openshell
```

---

## File Index

| File | Purpose |
|------|---------|
| `manifests/01-namespace.yaml` | Namespace definition |
| `manifests/02-scc-binding.yaml` | Privileged SCC binding for sandbox SA |
| `manifests/03-helm-values.yaml` | Helm values with OpenShift overrides |
| `manifests/04-route.yaml` | OpenShift Route for external gateway access |
| `manifests/05-network-policy.yaml` | NetworkPolicy restricting gateway ingress |
| `manifests/05-rbac.yaml` | Least-privilege RBAC for CronJob SA |
| `manifests/06-cronjob.yaml` | Weekly CronJob using `openshell sandbox create` |
| `launcher/Containerfile` | Launcher image: UBI-minimal + openshell CLI |
| `scripts/install.sh` | End-to-end install script (steps 1-4, 6-8) |
| `leadership-report-agent/` | Self-contained agent: fetch notes → LLM analysis → publish report |
| `leadership-report-agent/agent.py` | Main entry point / pipeline orchestrator |
| `leadership-report-agent/Containerfile` | OCI image build (UBI9 + Python 3.12) |
| `leadership-report-agent/lib/` | Auth, fetch, push, and LLM modules |

---

## Teardown

```shell
oc delete cronjob leadership-report-agent -n openshell
oc delete configmap leadership-report-policy -n openshell
oc delete secret leadership-report-config -n openshell
oc delete secret google-credentials -n openshell
helm uninstall openshell -n openshell
oc delete -f manifests/02-scc-binding.yaml
oc delete -f manifests/01-namespace.yaml
oc delete crd sandboxes.agents.x-k8s.io
```

---

## Security Considerations

This deployment disables several OpenShell security features to work on OpenShift. These are documented here so they can be tracked and re-enabled as upstream support improves.

| Feature | Default | This Deployment | Helm Value | Why |
|---------|---------|-----------------|------------|-----|
| **TLS (gateway)** | Enabled | Disabled | `server.disableTls: true` | OpenShift Routes handle TLS edge termination. The gateway runs plaintext behind the Route. All intra-cluster traffic is unencrypted. |
| **mTLS (sandbox ↔ gateway)** | Enabled | Disabled | `server.tls.clientTlsSecretName: ""` | With gateway TLS disabled, the supervisor cannot perform mTLS. Sandbox-to-gateway communication is plaintext within the cluster network. |
| **User authentication** | Required | Disabled | `server.auth.allowUnauthenticatedUsers: true` | The gateway's browser auth flow requires Cloudflare Access, which is not present on OpenShift. All CLI and gRPC requests are accepted as an unauthenticated local-dev principal with admin privileges. |
| **Privileged SCC** | N/A (not OpenShift-specific) | Required | `manifests/02-scc-binding.yaml` | OpenShell sandboxes require `SYS_ADMIN`, `NET_ADMIN`, and `SYS_PTRACE` capabilities for Landlock, seccomp, and network namespace isolation. The `privileged` SCC is the simplest way to grant these on OpenShift. A custom SCC scoped to only the required capabilities would be more restrictive. |
| **PKI init job** | Enabled | Disabled | `pkiInitJob.enabled: false` | The certgen job crashes when `clientTlsSecretName` is empty (it tries to create a secret with no name). Disabling it skips TLS cert generation entirely. The JWT signing keys must be pre-created manually before install (see Step 3). |

### What is still enforced

Even with the above features disabled, the following sandbox-level protections remain active:

- **L7 network policy** — the OpenShell proxy enforces per-endpoint, per-method/path network rules inside the sandbox
- **Landlock filesystem isolation** — the sandbox kernel restricts file access to declared paths
- **seccomp filtering** — system calls are restricted to the sandbox profile
- **Process isolation** — the agent runs as an unprivileged `sandbox` user/group
- **Inference routing** — `inference.local` is intercepted and routed through the OpenShell proxy to the configured provider
- **OCSF audit logging** — all network and process events inside the sandbox are logged in structured OCSF format

### Mitigations applied in this deployment

In addition to the sandbox-level protections above, the following cluster-level mitigations are in place:

| Mitigation | Manifest | What it does |
|------------|----------|--------------|
| **NetworkPolicy** | `05-network-policy.yaml` | Restricts gateway ingress to pods in the `openshell` namespace and the `agent-sandbox-system` namespace on port 8080. Other namespaces cannot reach the unauthenticated gateway. |
| **Least-privilege RBAC** | `05-rbac.yaml` | Scopes the `openshell-sandbox` SA to `get` on only `google-credentials`, `leadership-report-config` (secrets) and `leadership-report-policy` (configmap). Limits blast radius if the SA token is compromised. |
| **Non-root launcher** | `launcher/Containerfile` | The launcher container runs as a non-root `launcher` user, not `root`. |
| **Drive query injection fix** | `lib/fetch_gdoc.py` | The meeting name is escaped before interpolation into the Google Drive query string, preventing query injection via crafted meeting names. |

### Credential exposure — not recommended for production use

> **Warning:** This deployment is a proof-of-concept. Do not use it to process sensitive data or with production credentials until the issues below are resolved.

Google OAuth2 credentials (including the refresh token, which grants indefinite access to Drive and Docs) are still exposed at these points outside the sandbox:

| Stage | Exposure | Visible to | Mitigated? |
|-------|----------|------------|------------|
| Pod spec env vars | `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` in plaintext | Any principal with `get pods` RBAC in the `openshell` namespace | Partially — RBAC limits SA access to named secrets only |
| `openshell sandbox create --env=...` | All credentials in the process command line | Any user on the worker node (`/proc/<pid>/cmdline`) | No — requires upstream secret mount support |
| Launcher → gateway gRPC | Env values and bootstrap tokens on the wire | Pods that can reach the gateway | Partially — NetworkPolicy restricts gateway access to `openshell` and `agent-sandbox-system` namespaces |
| Gateway API | Full admin access, no authentication | Pods that can reach the gateway | Partially — NetworkPolicy blocks access from other namespaces |

The sandbox isolation itself (Landlock, seccomp, L7 network policy) is strong — the agent can only reach the declared Google API endpoints and `inference.local`. But the credentials are visible before they enter the sandbox.

**Blocked on upstream:** The OpenShell Kubernetes driver does not currently support volume or secret mounts in `--driver-config-json` (it uses `deny_unknown_fields`). Once the driver supports mounting Kubernetes secrets directly into sandbox pods, credentials can bypass the pod spec, command line, and gRPC channel entirely. Until then, env var injection is the only option.

### Recommendations for production

- ~~**Add a NetworkPolicy**~~ — Done (`05-network-policy.yaml`). Gateway ingress restricted to `openshell` and `agent-sandbox-system` namespaces.
- ~~**Tighten RBAC**~~ — Done (`05-rbac.yaml`). SA scoped to named secrets and configmaps only.
- ~~**Run launcher as non-root**~~ — Done (`launcher/Containerfile`).
- **Re-enable TLS**: Configure the gateway with a cert-manager-issued certificate and set `server.disableTls: false`. Use passthrough Route termination so gRPC/HTTP2 works end-to-end.
- **Re-enable mTLS**: Set `server.tls.clientTlsSecretName` back to `openshell-client-tls` once TLS is active.
- **Use OIDC auth**: Replace `allowUnauthenticatedUsers` with `--oidc-issuer` pointing to a Keycloak or RHSSO instance on the cluster.
- **Scope the SCC**: Create a custom SCC that grants only `SYS_ADMIN`, `NET_ADMIN`, `SYS_PTRACE`, and `SYSLOG` instead of full `privileged`.
- **Fix certgen RBAC**: Grant the `openshell-certgen` service account `get`/`list`/`create` on secrets in the `openshell` namespace, or use cert-manager (`certManager.enabled: true`) to manage TLS externally.
