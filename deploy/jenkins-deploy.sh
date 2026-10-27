#!/usr/bin/env bash
set -Eeuo pipefail
set +x

DEPLOY_ROOT=/opt/personal-system-inspector
SERVICE_NAME=personal-system-inspector.service
SERVICE_USER=inspector
INTERNAL_PORT=31880
HISTORY_RELEASES_TO_KEEP=2
SECRET_FILE=.jenkins-secrets

[[ -f "$SECRET_FILE" ]] || { echo "Missing Jenkins secret payload" >&2; exit 2; }
# shellcheck disable=SC1090
source "$SECRET_FILE"
rm -f -- "$SECRET_FILE"

RELEASE_DIR="$DEPLOY_ROOT/releases/$RELEASE_ID"
SHARED_DIR="$DEPLOY_ROOT/shared"
PREVIOUS_RELEASE="$(readlink -f "$DEPLOY_ROOT/current" 2>/dev/null || true)"
DEPLOYED=0

cleanup_and_rollback() {
  exit_code=$?
  rm -f -- "$SECRET_FILE"
  if [[ $exit_code -ne 0 && $DEPLOYED -eq 1 && -n "$PREVIOUS_RELEASE" && -d "$PREVIOUS_RELEASE" ]]; then
    echo "Deployment failed; rolling back to $PREVIOUS_RELEASE"
    sudo systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    ln -sfn "$PREVIOUS_RELEASE" "$DEPLOY_ROOT/current"
    sudo systemctl restart "$SERVICE_NAME"
    curl --fail --silent --retry 12 --retry-delay 5 "http://127.0.0.1:$INTERNAL_PORT/health/ready" >/dev/null
  fi
  exit "$exit_code"
}
trap cleanup_and_rollback EXIT

mkdir -p "$RELEASE_DIR" "$SHARED_DIR/data/reports" "$SHARED_DIR/logs" "$SHARED_DIR/config"
sudo python3.12 deploy/backup.py "$SHARED_DIR" "$RELEASE_ID"
cp -a pyproject.toml README.md src "$RELEASE_DIR/"
[[ -f "$SHARED_DIR/config/app.json" ]] || cp config/app.json "$SHARED_DIR/config/app.json"
cp config/checks.yaml "$SHARED_DIR/config/checks.yaml"

# Build the complete production environment before touching the running service.
python3.12 -m venv "$RELEASE_DIR/.venv"
PIP_DISABLE_PIP_VERSION_CHECK=1 "$RELEASE_DIR/.venv/bin/pip" install \
  --retries 5 --timeout 60 "$RELEASE_DIR"
"$RELEASE_DIR/.venv/bin/python" -c 'import app, uvicorn; print("Production virtualenv ready")'

touch "$SHARED_DIR/.env"
grep -Ev '^(APP_SECRET_KEY|ADMIN_INITIAL_PASSWORD|RUN_API_TOKEN|APP_VERSION|INSPECTOR_IMAGE|INSPECTOR_PORT|UBUNTU_SSH_USERNAME|UBUNTU_SSH_PASSWORD|CENTOS_SSH_USERNAME|CENTOS_SSH_PASSWORD|POSTGRES_HOST|POSTGRES_PORT|POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB|MYSQL_HOST|MYSQL_PORT|MYSQL_USER|MYSQL_PASSWORD|MYSQL_DB|JENKINS_URL|JENKINS_USERNAME|JENKINS_API_TOKEN|SMTP_HOST|SMTP_PORT|SMTP_USERNAME|SMTP_PASSWORD|SMTP_FROM|SMTP_TO|SMTP_USE_SSL)=' "$SHARED_DIR/.env" > "$SHARED_DIR/.env.next" || true
{
  printf 'APP_SECRET_KEY=%s\n' "$C_APP_SECRET"; printf 'ADMIN_INITIAL_PASSWORD=%s\n' "$C_ADMIN_PASSWORD"; printf 'RUN_API_TOKEN=%s\n' "$C_RUN_TOKEN"
  printf 'UBUNTU_SSH_USERNAME=%s\nUBUNTU_SSH_PASSWORD=%s\n' "$C_UBUNTU_USER" "$C_UBUNTU_PASSWORD"
  printf 'CENTOS_SSH_USERNAME=%s\nCENTOS_SSH_PASSWORD=%s\n' "$C_CENTOS_USER" "$C_CENTOS_PASSWORD"
  printf 'POSTGRES_HOST=192.168.0.100\nPOSTGRES_PORT=15432\nPOSTGRES_USER=%s\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=vnpy\n' "$C_POSTGRES_USER" "$C_POSTGRES_PASSWORD"
  printf 'MYSQL_HOST=192.168.0.110\nMYSQL_PORT=3306\nMYSQL_USER=%s\nMYSQL_PASSWORD=%s\nMYSQL_DB=mysql\n' "$C_MYSQL_USER" "$C_MYSQL_PASSWORD"
  printf 'JENKINS_URL=http://192.168.0.111:8080\nJENKINS_USERNAME=%s\nJENKINS_API_TOKEN=%s\n' "$C_JENKINS_USER" "$C_JENKINS_TOKEN"
  printf '%s' "$C_SMTP_ENV_B64" | base64 --decode
} >> "$SHARED_DIR/.env.next"
mv "$SHARED_DIR/.env.next" "$SHARED_DIR/.env"
chmod 600 "$SHARED_DIR/.env"

if ! getent passwd "$SERVICE_USER" >/dev/null; then
  sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin "$SERVICE_USER"
fi

if [[ ! -f "$SHARED_DIR/data/password.txt" ]]; then
  umask 077
  printf '# username:password:role\nadmin:%s:admin\n' "$C_ADMIN_PASSWORD" > "$SHARED_DIR/data/password.txt"
fi

sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$SHARED_DIR/data" "$SHARED_DIR/logs"
ln -s "$SHARED_DIR/data" "$RELEASE_DIR/data"
ln -s "$SHARED_DIR/logs" "$RELEASE_DIR/logs"
ln -s "$SHARED_DIR/config" "$RELEASE_DIR/config"
ln -s "$SHARED_DIR/.env" "$RELEASE_DIR/.env"
printf '%s' "$GIT_COMMIT" > "$RELEASE_DIR/COMMIT"
printf 'APP_VERSION=%s\n' "$BUILD_NUMBER" > "$RELEASE_DIR/version.env"

sudo install -o root -g root -m 0644 deploy/personal-system-inspector.service \
  "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null

# Cut over only after the release and virtualenv are complete.
sudo python3.12 deploy/backup.py "$SHARED_DIR" wait-idle
DEPLOYED=1
ln -sfn "$RELEASE_DIR" "$DEPLOY_ROOT/current"
sudo systemctl restart "$SERVICE_NAME"

attempts=0
until curl --fail --silent --max-time 5 "http://127.0.0.1:$INTERNAL_PORT/health/live" >/dev/null \
  && curl --fail --silent --max-time 5 "http://127.0.0.1:$INTERNAL_PORT/health/ready" >/dev/null; do
  attempts=$((attempts + 1))
  if [[ $attempts -ge 18 ]]; then
    sudo journalctl -u "$SERVICE_NAME" --no-pager -n 120 || true
    exit 1
  fi
  sleep 5
done

# Verify TLS and OAuth discovery from the configured LAN address, using the internal CA.
"$RELEASE_DIR/.venv/bin/python" - "$SHARED_DIR" <<'PY'
import json, pathlib, ssl, sys, urllib.request
root = pathlib.Path(sys.argv[1])
values = dict(line.split('=', 1) for line in (root / '.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
base = values.get('AI_PUBLIC_BASE_URL')
if base:
    ca = root / 'tls/ca.crt'
    context = ssl.create_default_context(cafile=str(ca)) if ca.exists() else ssl.create_default_context()
    for path in ['/health/ready', '/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-authorization-server']:
        with urllib.request.urlopen(base + path, context=context, timeout=15) as response:
            data = json.load(response)
        if path == '/health/ready':
            assert data['database'] is True
        elif 'protected-resource' in path:
            assert data['resource'] == base + '/mcp'
        else:
            assert 'S256' in data['code_challenge_methods_supported']
    print('TLS, OAuth discovery and resource binding verified')
PY

# Keep the active release plus exactly two rollback releases.
current_release="$(readlink -f "$DEPLOY_ROOT/current")"
historical_kept=0
while IFS= read -r old_release; do
  [[ -n "$old_release" ]] || continue
  [[ "$(readlink -f "$old_release")" == "$current_release" ]] && continue
  historical_kept=$((historical_kept + 1))
  if [[ $historical_kept -gt $HISTORY_RELEASES_TO_KEEP ]]; then
    case "$old_release" in
      "$DEPLOY_ROOT"/releases/*) rm -rf -- "$old_release" ;;
      *) echo "Refusing to remove unexpected path: $old_release" >&2; exit 1 ;;
    esac
  fi
done < <(find "$DEPLOY_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)

DEPLOYED=0
trap - EXIT
echo "Deployment healthy: release=$RELEASE_ID runtime=systemd-python port=$INTERNAL_PORT history=$HISTORY_RELEASES_TO_KEEP"
