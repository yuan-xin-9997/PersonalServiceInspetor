#!/usr/bin/env bash
# Run once by an operator, before the Jenkins deployment. Never replaces existing keys.
set -Eeuo pipefail
set +x
: "${DEPLOY_ROOT:?}" "${AI_TLS_HOST:?}" "${AI_TLS_PORT:?}"
TLS_DIR="$DEPLOY_ROOT/shared/tls"
umask 077
mkdir -p "$TLS_DIR"
if [[ ! -f "$TLS_DIR/server.key" ]]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -sha256 \
    -subj '/CN=Inspector Internal CA' -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" 2>/dev/null
  openssl req -newkey rsa:3072 -nodes -subj "/CN=$AI_TLS_HOST" \
    -keyout "$TLS_DIR/server.key" -out "$TLS_DIR/server.csr" 2>/dev/null
  printf 'subjectAltName=IP:%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "$AI_TLS_HOST" > "$TLS_DIR/extensions"
  openssl x509 -req -in "$TLS_DIR/server.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" \
    -CAcreateserial -days 365 -sha256 -extfile "$TLS_DIR/extensions" -out "$TLS_DIR/server.crt" 2>/dev/null
fi
# The CA signing key is readable only by the deployment owner; the service only reads its leaf key.
sudo chgrp inspector "$TLS_DIR" "$TLS_DIR/server.key" "$TLS_DIR/server.crt" "$TLS_DIR/ca.crt"
chmod 750 "$TLS_DIR"
chmod 640 "$TLS_DIR/server.key" "$TLS_DIR/server.crt" "$TLS_DIR/ca.crt"
chmod 600 "$TLS_DIR/ca.key"
ENV_FILE="$DEPLOY_ROOT/shared/.env"
grep -Ev '^AI_(PUBLIC_BASE_URL|TLS_HOST|TLS_PORT|TLS_CERTFILE|TLS_KEYFILE|REQUIRE_HTTPS)=' "$ENV_FILE" > "$ENV_FILE.ai-next" || true
{
  printf 'AI_PUBLIC_BASE_URL=https://%s:%s\nAI_TLS_HOST=%s\nAI_TLS_PORT=%s\n' "$AI_TLS_HOST" "$AI_TLS_PORT" "$AI_TLS_HOST" "$AI_TLS_PORT"
  printf 'AI_REQUIRE_HTTPS=true\n'
  printf 'AI_TLS_CERTFILE=%s/server.crt\nAI_TLS_KEYFILE=%s/server.key\n' "$TLS_DIR" "$TLS_DIR"
} >> "$ENV_FILE.ai-next"
chmod 600 "$ENV_FILE.ai-next"
mv "$ENV_FILE.ai-next" "$ENV_FILE"
openssl x509 -in "$TLS_DIR/ca.crt" -noout -fingerprint -sha256
echo 'TLS files prepared. Listener activates with the next release, bound only to AI_TLS_HOST.'
