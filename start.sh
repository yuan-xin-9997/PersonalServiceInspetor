#!/usr/bin/env bash
set -euo pipefail
sudo systemctl start personal-system-inspector.service
systemctl --no-pager --full status personal-system-inspector.service
