#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

server_bin="${ANIVAG_LLAMA_CPP_SERVER_BIN:-llama-server}"
model_path="${ANIVAG_LLAMA_CPP_MODEL_PATH:-${script_dir}/models/qwen3-vl-8b-q4_k_m.gguf}"
mmproj_path="${ANIVAG_LLAMA_CPP_MMPROJ_PATH:-${script_dir}/models/mmproj-qwen3-vl-8b-q8_0.gguf}"
host="${ANIVAG_LLAMA_CPP_HOST:-127.0.0.1}"
port="${ANIVAG_LLAMA_CPP_PORT:-8080}"
context_size="${ANIVAG_LLAMA_CPP_CONTEXT_SIZE:-65536}"
video_fps="${ANIVAG_LLAMA_CPP_VIDEO_FPS:-0.5}"
video_timestamp_interval="${ANIVAG_LLAMA_CPP_VIDEO_TIMESTAMP_INTERVAL:-2000}"
ffmpeg_dir="${ANIVAG_LLAMA_CPP_FFMPEG_DIR:-/opt/homebrew/bin}"
model_alias="${ANIVAG_LLAMA_CPP_MODEL_ALIAS:-anivag-qwen3-vl-direct}"

if ! command -v "${server_bin}" >/dev/null 2>&1; then
  echo "llama-server not found: ${server_bin}" >&2
  exit 1
fi
if [[ ! -f "${model_path}" ]]; then
  echo "Qwen model not found: ${model_path}" >&2
  exit 1
fi
if [[ ! -f "${mmproj_path}" ]]; then
  echo "Qwen multimodal projector not found: ${mmproj_path}" >&2
  exit 1
fi
if [[ ! -x "${ffmpeg_dir}/ffmpeg" || ! -x "${ffmpeg_dir}/ffprobe" ]]; then
  echo "ffmpeg and ffprobe must both exist in: ${ffmpeg_dir}" >&2
  exit 1
fi

exec "${server_bin}" \
  -m "${model_path}" \
  --mmproj "${mmproj_path}" \
  -c "${context_size}" \
  -ngl 99 \
  --parallel 1 \
  --video-fps "${video_fps}" \
  --video-timestamp-interval "${video_timestamp_interval}" \
  --video-ffmpeg-dir "${ffmpeg_dir}" \
  --alias "${model_alias}" \
  --cors-origins localhost \
  --host "${host}" \
  --port "${port}"
