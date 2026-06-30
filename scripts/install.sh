#!/usr/bin/env bash
set -euo pipefail

# End-to-end OpenShell + Leadership Report Agent installation on OpenShift.
# Run from the repo root: ./scripts/install.sh
#
# Prerequisites:
#   - oc CLI authenticated to your OpenShift cluster
#   - helm 3.x installed
#   - Secrets created before running (see below)
#
# Required secrets (create before running):
#   oc create secret generic google-credentials -n openshell \
#     --from-literal=client-id=YOUR_CLIENT_ID \
#     --from-literal=client-secret=YOUR_CLIENT_SECRET \
#     --from-literal=refresh-token=YOUR_REFRESH_TOKEN
#
#   oc create secret generic leadership-report-config -n openshell \
#     --from-literal=doc-id=YOUR_GOOGLE_DOC_ID \
#     --from-literal=model-id=meta-llama/Llama-3.1-8B-Instruct \
#     --from-literal=meeting-name="Your Meeting Name" \
#     --from-literal=gcp-project=your-gcp-project
#
# Prerequisites completed manually (see README Steps 4B and 5):
#   - Port-forward: oc port-forward openshell-0 8080:8080 -n openshell
#   - Register gateway: openshell gateway add http://localhost:8080 --name openshift
#   - Configure vLLM: openshell provider create / openshell inference set
#
# Environment variables (optional overrides):
#   OPENSHELL_CHART_VERSION  Helm chart version (default: 0.0.70)
#   AGENT_IMAGE              Agent container image ref

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFESTS="${REPO_DIR}/manifests"

CHART_VERSION="${OPENSHELL_CHART_VERSION:-0.0.70}"
NAMESPACE="openshell"

echo "=== OpenShell on OpenShift Installer ==="
echo "Chart version: ${CHART_VERSION}"
echo ""

# Step 1: Agent Sandbox CRDs
echo "[1/7] Installing Agent Sandbox CRDs..."
oc apply -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v0.4.6/manifest.yaml
echo ""

# Step 2: Namespace + SCC + RBAC
echo "[2/7] Creating namespace, SCC binding, and RBAC..."
oc apply -f "${MANIFESTS}/01-namespace.yaml"
oc apply -f "${MANIFESTS}/02-scc-binding.yaml"
oc apply -f "${MANIFESTS}/05-rbac.yaml"
echo ""

# Step 3: Helm install
echo "[3/7] Installing OpenShell via Helm..."
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart \
  --version "${CHART_VERSION}" \
  -n "${NAMESPACE}" \
  -f "${MANIFESTS}/03-helm-values.yaml"
echo ""

# Step 4: Wait for readiness + NetworkPolicy
echo "[4/7] Waiting for gateway to be ready..."
oc wait --for=condition=Ready pod/openshell-0 -n "${NAMESPACE}" --timeout=120s
oc apply -f "${MANIFESTS}/05-network-policy.yaml"
echo ""

# Step 5: Verify secrets exist
echo "[5/7] Verifying secrets..."
if oc get secret google-credentials -n "${NAMESPACE}" &>/dev/null; then
  echo "  google-credentials: OK"
else
  echo "  google-credentials: MISSING"
  echo "  Create it with:"
  echo "    oc create secret generic google-credentials -n ${NAMESPACE} \\"
  echo "      --from-literal=client-id=YOUR_CLIENT_ID \\"
  echo "      --from-literal=client-secret=YOUR_CLIENT_SECRET \\"
  echo "      --from-literal=refresh-token=YOUR_REFRESH_TOKEN"
  echo ""
fi

if oc get secret leadership-report-config -n "${NAMESPACE}" &>/dev/null; then
  echo "  leadership-report-config: OK"
else
  echo "  leadership-report-config: MISSING"
  echo "  Create it with:"
  echo "    oc create secret generic leadership-report-config -n ${NAMESPACE} \\"
  echo "      --from-literal=doc-id=YOUR_DOC_ID \\"
  echo "      --from-literal=model-id=meta-llama/Llama-3.1-8B-Instruct \\"
  echo "      --from-literal=meeting-name=\"Your Meeting Name\" \\"
  echo "      --from-literal=gcp-project=your-gcp-project"
  echo ""
fi

echo ""

# Step 6: Deploy the CronJob
echo "[6/7] Deploying weekly CronJob..."
oc apply -f "${MANIFESTS}/06-cronjob.yaml"
echo ""

# Step 7: Verify NetworkPolicy
echo "[7/7] Verifying NetworkPolicy..."
oc get networkpolicy -n "${NAMESPACE}"
echo ""

echo "=== Installation complete ==="
echo ""
echo "Gateway pod:"
oc get pods -n "${NAMESPACE}"
echo ""
echo "CronJob:"
oc get cronjob -n "${NAMESPACE}"
echo ""
echo "Schedule: Every Wednesday at 18:00 UTC"
echo ""
echo "Next steps (see README Steps 4B and 5):"
echo "  1. Port-forward: oc port-forward openshell-0 8080:8080 -n ${NAMESPACE}"
echo "  2. Register gateway: openshell gateway add http://localhost:8080 --name openshift"
echo "  3. Configure vLLM provider: openshell provider create / openshell inference set"
echo ""
echo "Test with a manual run:"
echo "  oc create job --from=cronjob/leadership-report-agent test-run -n ${NAMESPACE}"
echo "  oc logs -f job/test-run -n ${NAMESPACE}"
