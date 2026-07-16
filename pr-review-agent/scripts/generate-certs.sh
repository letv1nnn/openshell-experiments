#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERTS_DIR="${SCRIPT_DIR}/../certs"
NAMESPACE="${OPENSHELL_NAMESPACE:-openshell}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

if ! command -v openssl &>/dev/null; then
  log_error "openssl is not installed"
  exit 1
fi

mkdir -p "${CERTS_DIR}"

# Auto-detect OpenShift route hostname
ROUTE_HOSTNAME=""
if command -v oc &>/dev/null; then
  INGRESS_DOMAIN=$(oc get ingresses.config.openshift.io cluster \
    -o jsonpath='{.spec.domain}' 2>/dev/null || true)
  if [[ -n "${INGRESS_DOMAIN}" ]]; then
    ROUTE_HOSTNAME="openshell-${NAMESPACE}.${INGRESS_DOMAIN}"
    log_info "Detected OpenShift route hostname: ${ROUTE_HOSTNAME}"
  fi
fi

log_info "Generating CA certificate..."
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout "${CERTS_DIR}/ca.key" \
  -out "${CERTS_DIR}/ca.crt" \
  -subj "/CN=OpenShell CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

log_info "Generating server private key..."
openssl genrsa -out "${CERTS_DIR}/server.key" 4096

log_info "Creating server CSR with SANs..."
cat > "${CERTS_DIR}/server-san.cnf" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = openshell.${NAMESPACE}.svc.cluster.local

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = openshell
DNS.2 = openshell.${NAMESPACE}
DNS.3 = openshell.${NAMESPACE}.svc
DNS.4 = openshell.${NAMESPACE}.svc.cluster.local
DNS.5 = localhost
IP.1 = 127.0.0.1
EOF

if [[ -n "${ROUTE_HOSTNAME}" ]]; then
  echo "DNS.6 = ${ROUTE_HOSTNAME}" >> "${CERTS_DIR}/server-san.cnf"
fi

openssl req -new -key "${CERTS_DIR}/server.key" \
  -out "${CERTS_DIR}/server.csr" \
  -config "${CERTS_DIR}/server-san.cnf"

log_info "Signing server certificate..."
openssl x509 -req -in "${CERTS_DIR}/server.csr" \
  -CA "${CERTS_DIR}/ca.crt" \
  -CAkey "${CERTS_DIR}/ca.key" \
  -CAcreateserial \
  -out "${CERTS_DIR}/server.crt" \
  -days 825 -sha256 \
  -extensions v3_req \
  -extfile "${CERTS_DIR}/server-san.cnf"

log_info "Generating client CA certificate..."
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
  -keyout "${CERTS_DIR}/client-ca.key" \
  -out "${CERTS_DIR}/client-ca.crt" \
  -subj "/CN=OpenShell Client CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign"

log_info "Generating sandbox client certificate..."
openssl genrsa -out "${CERTS_DIR}/sandbox-client.key" 2048
cat > /tmp/sandbox-client.cnf <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
CN = openshell-sandbox

[v3_req]
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
EOF
openssl req -new -key "${CERTS_DIR}/sandbox-client.key" \
  -out /tmp/sandbox-client.csr \
  -config /tmp/sandbox-client.cnf
openssl x509 -req -in /tmp/sandbox-client.csr \
  -CA "${CERTS_DIR}/client-ca.crt" \
  -CAkey "${CERTS_DIR}/client-ca.key" \
  -CAcreateserial \
  -out "${CERTS_DIR}/sandbox-client.crt" \
  -days 825 -sha256 \
  -extensions v3_req \
  -extfile /tmp/sandbox-client.cnf
rm -f /tmp/sandbox-client.cnf /tmp/sandbox-client.csr

log_info "Generating JWT signing key (Ed25519)..."
openssl genpkey -algorithm Ed25519 -out "${CERTS_DIR}/jwt-signing.pem"
openssl pkey -in "${CERTS_DIR}/jwt-signing.pem" -pubout -out "${CERTS_DIR}/jwt-public.pem"
# kid = first 16 bytes of SHA-256(SubjectPublicKeyInfo DER), hex-encoded (matches openshell-bootstrap logic)
openssl pkey -in "${CERTS_DIR}/jwt-signing.pem" -pubout -outform DER 2>/dev/null \
  | openssl dgst -sha256 -hex \
  | awk '{print $NF}' | cut -c1-32 > "${CERTS_DIR}/jwt-kid"

rm -f "${CERTS_DIR}/server.csr" "${CERTS_DIR}/server-san.cnf" "${CERTS_DIR}/ca.srl"

log_info "Certificate generation complete."
log_info "Verify SANs: openssl x509 -in ${CERTS_DIR}/server.crt -text -noout | grep -A2 'Subject Alternative Name'"
