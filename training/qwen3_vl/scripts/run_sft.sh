#!/usr/bin/env bash
set -euo pipefail

mode="${1:-smoke}"
dataset_input="${2:-}"
if [[ -z "${dataset_input}" ]]; then
  echo "usage: $0 <smoke|train> <dataset-directory> [run-directory]" >&2
  exit 2
fi
if [[ "${mode}" != "smoke" && "${mode}" != "train" ]]; then
  echo "mode must be smoke or train" >&2
  exit 2
fi

script_directory="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
training_directory="$(cd "${script_directory}/.." && pwd)"
repository_root="$(cd "${training_directory}/../.." && pwd)"
dataset_directory="$(cd "${dataset_input}" && pwd)"
config_path="${training_directory}/configs/${mode}.yaml"
run_directory="${3:-${dataset_directory}/runs/${mode}}"

cd "${repository_root}"
python -m training.qwen3_vl.validate_dataset "${dataset_directory}" --probe-videos

mkdir -p "${run_directory}"
run_directory="$(cd "${run_directory}" && pwd)"
cd "${dataset_directory}"

export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"
swift sft "${config_path}" \
  --dataset "${dataset_directory}/train.jsonl" \
  --val_dataset "${dataset_directory}/validation.jsonl" \
  --output_dir "${run_directory}"
