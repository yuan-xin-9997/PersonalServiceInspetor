#!/bin/sh
set -eu

mkdir -p /app/data /app/logs /app/data/reports
if [ ! -f /app/data/password.txt ]; then
  if [ -z "${ADMIN_INITIAL_PASSWORD:-}" ]; then
    echo "ADMIN_INITIAL_PASSWORD is required on first startup" >&2
    exit 1
  fi
  umask 077
  printf '# username:password:role\nadmin:%s:admin\n' "$ADMIN_INITIAL_PASSWORD" > /app/data/password.txt
fi

if [ "$(id -u)" = "0" ]; then
  chown -R inspector:inspector /app/data /app/logs
  exec setpriv --reuid=inspector --regid=inspector --init-groups \
    uvicorn app.main:app --host 0.0.0.0 --port 31880 --proxy-headers
fi

exec uvicorn app.main:app --host 0.0.0.0 --port 31880 --proxy-headers
