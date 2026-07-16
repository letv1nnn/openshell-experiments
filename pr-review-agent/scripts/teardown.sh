#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${OPENSHELL_NAMESPACE:-openshell}"
FULL=false

for arg in "$@"; do
  [[ "${arg}" == "--full" ]] && FULL=true
done

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_step() { echo -e "${BLUE}==>${NC} $*"; }

echo "=========================================="
echo "  PR Review Agent — Teardown"
echo "=========================================="
if [[ "${FULL}" == "true" ]]; then
  log_warn "Full teardown: will delete namespace and all cluster resources"
else
  log_warn "Default teardown: deletes sandbox and providers only"
fi
echo ""
read -r -p "Continue? (y/N) " reply
[[ "${reply}" =~ ^[Yy]$ ]] || { log_info "Teardown cancelled"; exit 0; }

log_step "Deleting sandbox..."
openshell sandbox delete pr-reviewer 2>/dev/null && log_info "Sandbox deleted" || log_info "No sandbox found"

log_step "Deleting providers..."
openshell provider delete vertex-pr-reviewer 2>/dev/null && log_info "Vertex AI provider deleted" || true
openshell provider delete github-pr-reviewer 2>/dev/null && log_info "GitHub provider deleted" || true

if [[ "${FULL}" == "true" ]]; then
  log_step "Deleting openshell namespace (cascades to all resources)..."
  oc delete namespace "${NAMESPACE}" --ignore-not-found=true

  log_step "Deleting cluster-scoped RBAC..."
  oc delete clusterrole openshell-gateway --ignore-not-found=true
  oc delete clusterrolebinding openshell-gateway --ignore-not-found=true
  oc delete clusterrolebinding openshell-sandbox-privileged --ignore-not-found=true

  read -r -p "Remove Agent Sandbox controller? (y/N) " reply
  if [[ "${reply}" =~ ^[Yy]$ ]]; then
    AGENT_SANDBOX_VERSION="v0.4.6"
    oc delete -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/manifest.yaml" \
      --ignore-not-found=true
    log_info "Agent Sandbox controller removed"
  fi

  read -r -p "Remove generated certificates from ${PROJECT_DIR}/certs? (y/N) " reply
  if [[ "${reply}" =~ ^[Yy]$ ]]; then
    rm -rf "${PROJECT_DIR}/certs"
    log_info "Certificates removed"
  fi
fi

log_info "Teardown complete."
