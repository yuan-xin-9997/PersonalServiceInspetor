#!/usr/bin/env bash
set -euo pipefail
docker compose ps
curl --fail --silent http://127.0.0.1:${INSPECTOR_PORT:-31880}/health/ready
