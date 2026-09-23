#!/usr/bin/env bash
# 单机版：bash scripts/deploy.sh，默认自动检查环境
set -euo pipefail
SYNC_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${SYNC_PYTHON:-}" ]]; then
    SYNC_INTERPRETER="$SYNC_PYTHON"

else
    SYNC_INTERPRETER=python3
fi
exec "$SYNC_INTERPRETER" "$SYNC_ROOT/scripts/deploy.py" "$@"
