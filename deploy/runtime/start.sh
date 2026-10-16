#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mode=cpu
[[ "${1:-}" != --gpu ]] || mode=gpu
if [[ ! -f .env ]]; then
  umask 077
  docker run --rm python:3.12-slim python -c 'import secrets; print("VISIONFORGE_API_KEY=" + secrets.token_urlsafe(32))' > .env.tmp
  mv .env.tmp .env
fi
compose=(docker compose -f compose.yaml)
[[ "$mode" != gpu ]] || compose+=(-f compose.gpu.yaml)
"${compose[@]}" up --build -d --wait --wait-timeout 180
address=$("${compose[@]}" port model 8080)
echo "模型已部署：http://${address/0.0.0.0/127.0.0.1} 。API 密钥见 .env 文件。"
