#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mode=auto
action=start
build=false
for argument in "$@"; do
  case "$argument" in
    --cpu) mode=cpu ;;
    --gpu) mode=gpu ;;
    --check) action=check ;;
    --status) action=status ;;
    --stop) action=stop ;;
    --build) build=true ;;
    --help) echo 'Usage: ./start.sh [--cpu|--gpu] [--check|--status|--stop] [--build]'; exit 0 ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done
if ! command -v docker >/dev/null || ! docker info >/dev/null 2>&1; then
  echo '请安装并启动 Docker。Windows 请启用 Docker Desktop 的 WSL2 后端。' >&2; exit 1
fi
docker compose version >/dev/null
[[ -f .env ]] || cp .env.example .env
if [[ "$mode" == auto && -f .visionforge-mode ]]; then
  read -r mode < .visionforge-mode
fi
if [[ "$mode" == auto ]]; then
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then mode=gpu; else mode=cpu; fi
fi
case "$mode" in cpu|gpu) ;; *) echo 'Invalid .visionforge-mode' >&2; exit 2;; esac
compose=(docker compose -f compose.yaml)
[[ "$mode" != gpu ]] || compose+=(-f compose.gpu.yaml)
"${compose[@]}" config --quiet
if [[ "$action" == stop ]]; then "${compose[@]}" stop; exit 0; fi
if [[ "$action" == status ]]; then
  "${compose[@]}" ps
  "${compose[@]}" exec -T web python -m app.setup
  exit 0
fi
images=$("${compose[@]}" config --images)
if [[ "$build" == true || "$images" == *:local* ]]; then
  echo '源码模式：首次启动需要构建镜像；正式发行包会直接拉取镜像。'
  "${compose[@]}" build
else
  "${compose[@]}" pull
fi
diagnostic=(python -m app.diagnostics)
[[ "$mode" != gpu ]] || diagnostic+=(--gpu)
"${compose[@]}" run --rm --no-deps worker "${diagnostic[@]}"
[[ "$action" != check ]] || exit 0
"${compose[@]}" up -d --no-build --wait --wait-timeout 180
printf '%s\n' "$mode" > .visionforge-mode
"${compose[@]}" exec -T web python -m app.setup
address=$("${compose[@]}" port web 8000)
echo "CVSprout 已启动（$mode）：http://${address/0.0.0.0/127.0.0.1}"
