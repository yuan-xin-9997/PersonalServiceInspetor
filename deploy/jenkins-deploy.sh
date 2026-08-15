#!/usr/bin/env bash
set -Eeuo pipefail
set +x

DEPLOY_ROOT=/opt/personal-system-inspector
INTERNAL_PORT=31880
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
    ln -sfn "$PREVIOUS_RELEASE" "$DEPLOY_ROOT/current"
    cd "$PREVIOUS_RELEASE"
    docker compose -p personal-system-inspector up -d --remove-orphans
    curl --fail --silent --retry 12 --retry-delay 5 "http://127.0.0.1:$INTERNAL_PORT/health/ready" >/dev/null
  fi
  exit "$exit_code"
}
trap cleanup_and_rollback EXIT

mkdir -p "$RELEASE_DIR" "$SHARED_DIR/data/reports" "$SHARED_DIR/logs" "$SHARED_DIR/config"
cp docker-compose.yml "$RELEASE_DIR/"
[[ -f "$SHARED_DIR/config/app.json" ]] || cp config/app.json "$SHARED_DIR/config/app.json"
[[ -f "$SHARED_DIR/config/checks.yaml" ]] || cp config/checks.yaml "$SHARED_DIR/config/checks.yaml"

touch "$SHARED_DIR/.env"
grep -Ev '^(APP_SECRET_KEY|ADMIN_INITIAL_PASSWORD|RUN_API_TOKEN|APP_VERSION|INSPECTOR_IMAGE|INSPECTOR_PORT|UBUNTU_SSH_USERNAME|UBUNTU_SSH_PASSWORD|CENTOS_SSH_USERNAME|CENTOS_SSH_PASSWORD|POSTGRES_HOST|POSTGRES_PORT|POSTGRES_USER|POSTGRES_PASSWORD|POSTGRES_DB|MYSQL_HOST|MYSQL_PORT|MYSQL_USER|MYSQL_PASSWORD|MYSQL_DB|JENKINS_URL|JENKINS_USERNAME|JENKINS_API_TOKEN|SMTP_HOST|SMTP_PORT|SMTP_USERNAME|SMTP_PASSWORD|SMTP_FROM|SMTP_TO|SMTP_USE_SSL)=' "$SHARED_DIR/.env" > "$SHARED_DIR/.env.next" || true
{
  printf 'APP_SECRET_KEY=%s\n' "$C_APP_SECRET"; printf 'ADMIN_INITIAL_PASSWORD=%s\n' "$C_ADMIN_PASSWORD"; printf 'RUN_API_TOKEN=%s\n' "$C_RUN_TOKEN"
  printf 'APP_VERSION=%s\nINSPECTOR_IMAGE=%s\nINSPECTOR_PORT=%s\n' "$GIT_SHORT_SHA" "$IMAGE_TAG" "$INTERNAL_PORT"
  printf 'UBUNTU_SSH_USERNAME=%s\nUBUNTU_SSH_PASSWORD=%s\n' "$C_UBUNTU_USER" "$C_UBUNTU_PASSWORD"
  printf 'CENTOS_SSH_USERNAME=%s\nCENTOS_SSH_PASSWORD=%s\n' "$C_CENTOS_USER" "$C_CENTOS_PASSWORD"
  printf 'POSTGRES_HOST=192.168.0.100\nPOSTGRES_PORT=15432\nPOSTGRES_USER=%s\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=vnpy\n' "$C_POSTGRES_USER" "$C_POSTGRES_PASSWORD"
  printf 'MYSQL_HOST=192.168.0.110\nMYSQL_PORT=3306\nMYSQL_USER=%s\nMYSQL_PASSWORD=%s\nMYSQL_DB=mysql\n' "$C_MYSQL_USER" "$C_MYSQL_PASSWORD"
  printf 'JENKINS_URL=http://192.168.0.111:8080\nJENKINS_USERNAME=%s\nJENKINS_API_TOKEN=%s\n' "$C_JENKINS_USER" "$C_JENKINS_TOKEN"
  printf '%s' "$C_SMTP_ENV_B64" | base64 --decode
} >> "$SHARED_DIR/.env.next"
mv "$SHARED_DIR/.env.next" "$SHARED_DIR/.env"
chmod 600 "$SHARED_DIR/.env"

ln -s "$SHARED_DIR/data" "$RELEASE_DIR/data"
ln -s "$SHARED_DIR/logs" "$RELEASE_DIR/logs"
ln -s "$SHARED_DIR/config" "$RELEASE_DIR/config"
ln -s "$SHARED_DIR/.env" "$RELEASE_DIR/.env"
printf '%s' "$GIT_COMMIT" > "$RELEASE_DIR/COMMIT"
ln -sfn "$RELEASE_DIR" "$DEPLOY_ROOT/current"
DEPLOYED=1
cd "$RELEASE_DIR"
docker compose -p personal-system-inspector up -d --remove-orphans

attempts=0
until curl --fail --silent --max-time 5 "http://127.0.0.1:$INTERNAL_PORT/health/live" >/dev/null && curl --fail --silent --max-time 5 "http://127.0.0.1:$INTERNAL_PORT/health/ready" >/dev/null; do
  attempts=$((attempts + 1))
  if [[ $attempts -ge 18 ]]; then docker compose -p personal-system-inspector logs --tail=120; exit 1; fi
  sleep 5
done

find "$DEPLOY_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | awk 'NR>5 {$1=""; sub(/^ /,""); print}' | while IFS= read -r old_release; do
  [[ -n "$old_release" ]] || continue
  [[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$(readlink -f "$old_release")" ]] || rm -rf -- "$old_release"
done
docker image prune -f --filter 'until=168h' >/dev/null

DEPLOYED=0
trap - EXIT
echo "Deployment healthy: release=$RELEASE_ID port=$INTERNAL_PORT"
