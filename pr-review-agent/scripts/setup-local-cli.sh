#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS_DIR="${SCRIPT_DIR}/../certs"
GATEWAY_NAME="${1:-openshift}"
CLI_CONFIG_DIR="${HOME}/.config/openshell/gateways/${GATEWAY_NAME}"
MTLS_DIR="${CLI_CONFIG_DIR}/mtls"

GREEN='\033[0;32m'
NC='\033[0m'
log_info() { echo -e "${GREEN}[INFO]${NC} $*"; }

log_info "Setting up CLI for gateway: ${GATEWAY_NAME}"

mkdir -p "${CLI_CONFIG_DIR}" "${MTLS_DIR}"

cp "${CERTS_DIR}/ca.crt" "${CLI_CONFIG_DIR}/ca.crt"
cp "${CERTS_DIR}/ca.crt" "${MTLS_DIR}/ca.crt"
log_info "Copied server CA certificate"

log_info "Generating client mTLS certificate..."
openssl genrsa -out "${MTLS_DIR}/tls.key" 2048

cat > "${MTLS_DIR}/client.cnf" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = openshell-cli

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF

openssl req -new -key "${MTLS_DIR}/tls.key" \
  -out "${MTLS_DIR}/tls.csr" \
  -config "${MTLS_DIR}/client.cnf"

openssl x509 -req -in "${MTLS_DIR}/tls.csr" \
  -CA "${CERTS_DIR}/client-ca.crt" \
  -CAkey "${CERTS_DIR}/client-ca.key" \
  -CAcreateserial \
  -out "${MTLS_DIR}/tls.crt" \
  -days 825 -sha256 \
  -extensions v3_req \
  -extfile "${MTLS_DIR}/client.cnf"

rm -f "${MTLS_DIR}/client.cnf" "${MTLS_DIR}/tls.csr"
chmod 600 "${MTLS_DIR}/tls.key"

log_info "CLI setup complete. Add your gateway with:"
echo "  openshell gateway add https://<route-hostname> --name ${GATEWAY_NAME} --local"
