#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFESTS="${PROJECT_DIR}/manifests"
CERTS_DIR="${PROJECT_DIR}/certs"

NAMESPACE="${OPENSHELL_NAMESPACE:-openshell}"
AGENT_SANDBOX_VERSION="v0.5.0"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}==>${NC} $*"; }

echo "=========================================="
echo "  PR Review Agent — OpenShell Deployment"
echo "=========================================="
echo ""

# 1. Prerequisites
log_step "Checking prerequisites..."
if ! command -v oc &>/dev/null; then
  log_error "oc CLI not found. Install and configure it for your OpenShift cluster."
  exit 1
fi
if ! oc cluster-info &>/dev/null; then
  log_error "Cannot connect to cluster. Check your oc login."
  exit 1
fi
log_info "Cluster: $(oc cluster-info | head -1)"

# 2. Agent Sandbox CRDs
log_step "Checking Agent Sandbox CRDs..."
if ! oc get crd sandboxes.agents.x-k8s.io &>/dev/null; then
  log_info "Installing Agent Sandbox controller (${AGENT_SANDBOX_VERSION})..."
  oc apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/manifest.yaml"
  log_info "Waiting for controller to be ready..."
  oc -n agent-sandbox-system wait --for=condition=available deployment \
    -l app=agent-sandbox-controller --timeout=120s || \
    log_warn "Controller still starting — continuing anyway"
else
  log_info "Agent Sandbox CRDs already installed"
fi

# 3. TLS certificates
log_step "Checking TLS certificates..."
if [[ -f "${CERTS_DIR}/server.crt" && -f "${CERTS_DIR}/server.key" ]]; then
  log_info "Certificates already exist in ${CERTS_DIR}"
else
  log_info "Generating TLS certificates..."
  bash "${SCRIPT_DIR}/generate-certs.sh"
fi

# 4. Namespace
log_step "Creating namespace..."
oc apply -f "${MANIFESTS}/00-namespace.yaml"

# 5. PKI secrets
log_step "Creating PKI secrets..."
oc -n "${NAMESPACE}" create secret generic openshell-server-tls \
  --from-file=tls.crt="${CERTS_DIR}/server.crt" \
  --from-file=tls.key="${CERTS_DIR}/server.key" \
  --from-file=ca.crt="${CERTS_DIR}/ca.crt" \
  --dry-run=client -o yaml | oc apply -f -

oc -n "${NAMESPACE}" create secret generic openshell-server-client-ca \
  --from-file=ca.crt="${CERTS_DIR}/client-ca.crt" \
  --dry-run=client -o yaml | oc apply -f -

oc -n "${NAMESPACE}" create secret generic openshell-sandbox-client-tls \
  --from-file=ca.crt="${CERTS_DIR}/ca.crt" \
  --from-file=tls.crt="${CERTS_DIR}/sandbox-client.crt" \
  --from-file=tls.key="${CERTS_DIR}/sandbox-client.key" \
  --dry-run=client -o yaml | oc apply -f -

oc -n "${NAMESPACE}" create secret generic openshell-sandbox-jwt-signing-secret \
  --from-file=signing.pem="${CERTS_DIR}/jwt-signing.pem" \
  --from-file=public.pem="${CERTS_DIR}/jwt-public.pem" \
  --from-file=kid="${CERTS_DIR}/jwt-kid" \
  --dry-run=client -o yaml | oc apply -f -

log_info "PKI secrets created"

# 6. RBAC + SCC
log_step "Applying RBAC and SCC binding..."
oc apply -f "${MANIFESTS}/02-rbac.yaml"
oc apply -f "${MANIFESTS}/02b-scc-binding.yaml"

# 7. ConfigMap
log_step "Applying gateway ConfigMap..."
oc apply -f "${MANIFESTS}/04-configmap.yaml"

# 8. StatefulSet + Service + Route
log_step "Applying gateway StatefulSet, Service, and Route..."
oc apply -f "${MANIFESTS}/05-gateway-statefulset.yaml"
oc apply -f "${MANIFESTS}/06-service.yaml"
oc apply -f "${MANIFESTS}/07-route.yaml"

# 9. Wait for readiness
log_step "Waiting for gateway pod to be ready (timeout: 5m)..."
oc -n "${NAMESPACE}" wait --for=condition=ready pod \
  -l app.kubernetes.io/name=openshell --timeout=300s

log_info "Gateway is ready!"
echo ""
oc -n "${NAMESPACE}" get pods -l app.kubernetes.io/name=openshell
echo ""
ROUTE_HOST=$(oc -n "${NAMESPACE}" get route openshell \
  -o jsonpath='{.spec.host}' 2>/dev/null || true)
if [[ -n "${ROUTE_HOST}" ]]; then
  log_info "Route: https://${ROUTE_HOST}"
fi
