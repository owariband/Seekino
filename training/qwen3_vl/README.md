# Qwen3-VL post-training

This directory contains the project-specific part of anivag's Qwen3-VL SFT
pipeline. It exports committed parse checkpoints, validates the resulting
dataset, and starts a QLoRA run through ms-swift. CUDA, PyTorch, and ms-swift
remain outside the application's root dependency set.

## Data contract

Each sample contains one AnalysisWindow video and the same Lite ASR, bounded
history, SeriesBible, and window constraints used by the parse pipeline. The
assistant target is the raw `VideoTimeline` reconstructed from
`window_summary` and every `observed_events[].relative_event`.

The exporter uses only the text between `ANIVAG_RUNTIME_CONTEXT_START` and
`ANIVAG_RUNTIME_CONTEXT_END` when those markers are present. Full background
documents are too large to repeat in every video sample and can cause ms-swift
to discard rows that exceed `max_length`. A marker-free context longer than
6000 characters is rejected instead of being silently truncated.

Only successful Doubao direct-video checkpoints are exported. Ordered-frame
fallback output is deliberately excluded because it is not a reliable teacher
for the direct-video student path. Qwen3-VL receives the Lite ASR in text and
is told that it cannot consume the video's audio track.

The generated bundle is portable:

```text
dataset-v001/
├── train.jsonl
├── validation.jsonl
├── manifest.jsonl
├── summary.json
└── videos/
```

Video paths in the JSONL files are relative to this directory. The launcher
changes into the dataset directory before invoking ms-swift.

## Export locally

Load the normal anivag persistence variables, then assign complete episodes to
different splits:

```bash
python -m training.qwen3_vl.export_dataset \
  --train-job <episode-1-job-id> \
  --validation-job <episode-2-job-id> \
  --output-dir outputs/training/qwen3_vl/datasets/v001

python -m training.qwen3_vl.validate_dataset \
  outputs/training/qwen3_vl/datasets/v001 \
  --probe-videos
```

Multiple `--train-job` and `--validation-job` flags are supported. Never put
windows from the same episode into both splits. The exporter hard-links media
when possible and falls back to copying across filesystems.

## Run on the GPU machine

Use a CUDA image that already contains a compatible PyTorch build. Create a
separate environment and install the training dependencies. Install
TorchCodec according to the PyTorch/CUDA versions in that image; Qwen recommends
it over Decord for video decoding.

```bash
python -m venv .venv-training
source .venv-training/bin/activate
pip install -r training/qwen3_vl/requirements-gpu.txt
# Install a TorchCodec build compatible with the image's PyTorch version.
```

Start with five optimizer steps. This verifies video decoding, tokenization,
backpropagation, checkpoint saving, validation, and peak memory before paying
for a full run:

```bash
training/qwen3_vl/scripts/run_sft.sh \
  smoke outputs/training/qwen3_vl/datasets/v001
```

Only after the smoke run succeeds should the full configuration be used:

```bash
training/qwen3_vl/scripts/run_sft.sh \
  train outputs/training/qwen3_vl/datasets/v001
```

The checked-in values are experiment starting points. `FPS`,
`FPS_MAX_FRAMES`, `VIDEO_MAX_TOKEN_NUM`, and `max_length` must remain identical
between training and the later Base/Adapter evaluation. On a 24 GB card, reduce
frames or per-frame video tokens first when the smoke run runs out of memory.

Official references:

- https://github.com/modelscope/ms-swift/blob/main/docs/source_en/BestPractices/Qwen3-VL-Best-Practice.md
- https://github.com/modelscope/ms-swift/blob/main/docs/source_en/Customization/Custom-dataset.md
- https://github.com/QwenLM/Qwen3-VL
