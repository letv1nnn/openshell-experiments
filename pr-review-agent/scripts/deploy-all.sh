#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAMESPACE="${OPENSHELL_NAMESPACE:-openshell}"
GATEWAY_NAME="${OPENSHELL_GATEWAY_NAME:-openshift}"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }
log_step() { echo -e "${BLUE}==>${NC} $*"; }

log_step "Deploying OpenShell gateway to OpenShift..."
bash "${SCRIPT_DIR}/deploy.sh"

ROUTE_HOST=$(oc -n "${NAMESPACE}" get route openshell \
  -o jsonpath='{.spec.host}' 2>/dev/null || true)

if [[ -n "${ROUTE_HOST}" ]]; then
  log_step "Registering gateway with CLI..."
  openshell gateway remove "${GATEWAY_NAME}" 2>/dev/null || true
  # --local imports certs from the Homebrew package dir; we override them
  # immediately after by re-running setup-local-cli.sh with our custom CA.
  openshell gateway add "https://${ROUTE_HOST}" \
    --name "${GATEWAY_NAME}" \
    --local

  log_step "Installing custom TLS certificates (overriding Homebrew package certs)..."
  bash "${SCRIPT_DIR}/setup-local-cli.sh" "${GATEWAY_NAME}"

  echo ""
  log_info "Deployment complete!"
  log_info "Gateway: https://${ROUTE_HOST}"
  echo ""
  log_info "Next step — configure providers and launch sandbox:"
  echo "  VERTEX_AI_PROJECT_ID=<your-project> bash scripts/setup-providers.sh"
else
  echo ""
  log_info "Cluster deployment complete."
  log_info "Could not detect route hostname. Register the gateway manually:"
  echo "  OPENSHELL_TLS_CA=certs/ca.crt openshell gateway add https://<route-hostname> --name ${GATEWAY_NAME} --local"
fi
