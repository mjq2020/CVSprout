#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hf_cli="${project_dir}/.venv/bin/hf"

if [[ ! -x "${hf_cli}" ]]; then
  echo "未找到 ${hf_cli}，请先运行：.venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

"${hf_cli}" download IDEA-Research/grounding-dino-tiny \
  --revision a2bb814dd30d776dcf7e30523b00659f4f141c71 \
  --include '*.json' '*.txt' '*.safetensors' 'README.md' \
  --local-dir "${project_dir}/models/grounding-dino-tiny" \
  --max-workers 4

"${hf_cli}" download facebook/sam2.1-hiera-tiny \
  --revision de431c4043854a71d8101e17995dfe596bf101a5 \
  --include '*.json' '*.safetensors' 'README.md' \
  --local-dir "${project_dir}/models/sam2.1-hiera-tiny" \
  --max-workers 4

echo "本地标注模型已准备完成。"
