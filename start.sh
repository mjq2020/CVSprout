#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
mode=auto
action=start
build=false
strict_port=false
requested_port=
port_supplied=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cpu) mode=cpu ;;
    --gpu) mode=gpu ;;
    --check) action=check ;;
    --status) action=status ;;
    --stop) action=stop ;;
    --build) build=true ;;
    --port)
      [[ $# -ge 2 ]] || { echo '--port requires a number (0–65535).' >&2; exit 2; }
      requested_port=$2; port_supplied=true; shift ;;
    --port=*) requested_port=${1#*=}; port_supplied=true ;;
    --strict-port) strict_port=true ;;
    --help) echo 'Usage: ./start.sh [--cpu|--gpu] [--port 0–65535] [--strict-port] [--check|--status|--stop] [--build]'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ "$port_supplied" == true ]]; then
  if [[ ! "$requested_port" =~ ^[0-9]{1,5}$ ]] || (( 10#$requested_port > 65535 )); then
    echo '--port must be an integer between 0 and 65535 (0 selects an available port).' >&2; exit 2
  fi
  export MDP_PORT=$((10#$requested_port))
fi

save_port() {
  local temporary
  temporary=$(mktemp .env.port.XXXXXX) || return 1
  if awk -v port="$1" '
    /^[[:space:]]*(export[[:space:]]+)?MDP_PORT[[:space:]]*=/ { print "MDP_PORT=" port; found=1; next }
    { print }
    END { if (!found) print "MDP_PORT=" port }
  ' .env > "$temporary" && mv -f "$temporary" .env; then
    return 0
  fi
  rm -f "$temporary"
  return 1
}

show_address() {
  address=$("${compose[@]}" port web 8000)
  echo "CVSprout 已启动（$mode）：http://${address/0.0.0.0/127.0.0.1}"
}
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
  show_address
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
startup_log=$(mktemp)
trap 'rm -f "$startup_log"' EXIT
if ! "${compose[@]}" up -d --no-build --wait --wait-timeout 180 2>&1 | tee "$startup_log"; then
  if [[ "$strict_port" == true ]] || ! grep -Eqi 'address already in use|port is already allocated|only one usage of each socket address' "$startup_log"; then
    echo '启动失败，请检查上方错误信息。' >&2; exit 1
  fi
  echo '所选端口已被占用，正在由 Docker 分配可用端口…'
  # Docker allocates the port while binding it, avoiding a check-then-bind race.
  export MDP_PORT=0
  "${compose[@]}" up -d --no-build --wait --wait-timeout 180
fi
printf '%s\n' "$mode" > .visionforge-mode
"${compose[@]}" exec -T web python -m app.setup
show_address
actual_port=${address##*:}
if [[ "$actual_port" =~ ^[0-9]{1,5}$ ]] && (( 10#$actual_port > 0 && 10#$actual_port <= 65535 )); then
  if save_port "$actual_port"; then
    echo "已保存 MDP_PORT=$actual_port 到 .env，下次启动将沿用此端口。"
  else
    echo "服务已启动，但无法保存端口；下次可使用 --port $actual_port。" >&2
  fi
else
  echo '无法读取实际映射端口，请运行 docker compose ps 查看。' >&2; exit 1
fi
