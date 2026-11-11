#!/usr/bin/env bash
# 启动 Hermes Skill 控制台（只读），仅绑定 127.0.0.1:8000。
# venv 位于仓库根目录，由 install.sh 创建。
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
exec "${VENV:-../.venv}/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" --log-level info
