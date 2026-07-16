#!/usr/bin/env bash
# Local dev variant of setup-providers.sh.
# Assumes a local OpenShell gateway is already running (e.g. via `mise run gateway`).
# Does NOT create the OpenShift ConfigMap — config is uploaded directly to the sandbox.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

GREEN='\033[0;32m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_step()  { echo -e "${BLUE}==>${NC} $*"; }

: "${VERTEX_AI_PROJECT_ID:?VERTEX_AI_PROJECT_ID must be set}"
VERTEX_AI_REGION="${VERTEX_AI_REGION:-global}"

echo "=========================================="
echo "  PR Review Agent — Local Setup"
echo "=========================================="
echo ""

log_step "Validating gateway connection..."
if ! openshell status &>/dev/null; then
  log_error "Cannot reach local OpenShell gateway. Start it with: mise run gateway"
  exit 1
fi

log_step "Creating Vertex AI provider..."
openshell provider delete vertex-pr-reviewer 2>/dev/null || true
openshell provider create \
  --name vertex-pr-reviewer \
  --type google-vertex-ai \
  --from-gcloud-adc \
  --config "VERTEX_AI_PROJECT_ID=${VERTEX_AI_PROJECT_ID}" \
  --config "VERTEX_AI_REGION=${VERTEX_AI_REGION}"

log_step "Enabling provider endpoint injection..."
openshell settings set --global --key providers_v2_enabled --value true --yes

log_step "Configuring inference routing..."
openshell inference set --provider vertex-pr-reviewer --model claude-sonnet-4-6 --no-verify

log_step "Creating GitHub provider..."
openshell provider delete github-pr-reviewer 2>/dev/null || true
openshell provider create \
  --name github-pr-reviewer \
  --type github \
  --from-existing

log_step "Removing any existing pr-reviewer sandbox..."
openshell sandbox delete pr-reviewer 2>/dev/null || true

log_step "Creating pr-reviewer sandbox..."
openshell sandbox create \
  --name pr-reviewer \
  --from base \
  --provider vertex-pr-reviewer \
  --provider github-pr-reviewer \
  --policy "${PROJECT_DIR}/policy.yaml" \
  --upload "${PROJECT_DIR}/payload:/sandbox/payload" \
  --upload "${PROJECT_DIR}/config.yaml:/sandbox/pr-review-agent/config.yaml" \
  --no-tty \
  -- python3 /sandbox/payload/entrypoint.py

echo ""
log_info "Sandbox pr-reviewer is running."
log_info "Connect with: openshell sandbox connect pr-reviewer"
