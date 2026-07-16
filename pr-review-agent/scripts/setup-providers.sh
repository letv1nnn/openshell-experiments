#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
NAMESPACE="${OPENSHELL_NAMESPACE:-openshell}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}==>${NC} $*"; }

: "${VERTEX_AI_PROJECT_ID:?VERTEX_AI_PROJECT_ID must be set}"
VERTEX_AI_REGION="${VERTEX_AI_REGION:-global}"

echo "=========================================="
echo "  PR Review Agent — Provider + Sandbox"
echo "=========================================="
echo ""

# 1. Validate gateway
log_step "Validating gateway connection..."
if ! openshell status &>/dev/null; then
  log_error "Cannot reach OpenShell gateway. Run scripts/deploy-all.sh first."
  exit 1
fi
log_info "Gateway reachable"

# 2. Vertex AI provider
log_step "Creating Vertex AI provider..."
openshell provider delete vertex-pr-reviewer 2>/dev/null || true
openshell provider create \
  --name vertex-pr-reviewer \
  --type google-vertex-ai \
  --from-gcloud-adc \
  --config "VERTEX_AI_PROJECT_ID=${VERTEX_AI_PROJECT_ID}" \
  --config "VERTEX_AI_REGION=${VERTEX_AI_REGION}"
log_info "Vertex AI provider created"

# 3. Enable providers v2
log_step "Enabling provider endpoint injection..."
openshell settings set --global --key providers_v2_enabled --value true --yes

# 4. Inference routing
log_step "Configuring inference routing..."
openshell inference set --provider vertex-pr-reviewer --model claude-sonnet-4-6 --no-verify
log_info "Inference routed via Vertex AI (claude-sonnet-4-6)"

# 5. GitHub provider
log_step "Creating GitHub provider..."
openshell provider delete github-pr-reviewer 2>/dev/null || true
openshell provider create \
  --name github-pr-reviewer \
  --type github \
  --from-existing
log_info "GitHub provider created"

# 6. Apply RBAC
log_step "Applying RBAC manifests..."
oc apply -f "${PROJECT_DIR}/manifests/02-rbac.yaml"
log_info "RBAC applied"

# 7. Delete existing sandbox
log_step "Removing any existing pr-reviewer sandbox..."
openshell sandbox delete pr-reviewer 2>/dev/null || true

# 8. Create sandbox
log_step "Creating pr-reviewer sandbox..."
if [[ -z "${SANDBOX_IMAGE:-}" ]]; then
  echo "Error: SANDBOX_IMAGE is not set. Build and push the image first, then:"
  echo "  export SANDBOX_IMAGE=your-registry/pr-review-agent:latest"
  exit 1
fi
openshell sandbox create \
  --name pr-reviewer \
  --from "${SANDBOX_IMAGE}" \
  --provider vertex-pr-reviewer \
  --provider github-pr-reviewer \
  --policy "${PROJECT_DIR}/policy.yaml" \
  --memory 6Gi \
  --no-tty \
  -- python3 /app/payload/entrypoint.py

echo ""
log_info "Sandbox pr-reviewer is running."
log_info "Connect to it with: openshell sandbox connect pr-reviewer"
