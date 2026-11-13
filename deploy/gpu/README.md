# GPU WhisperX 转写与匿名声源分组

`whisperx_server.py` 提供 loopback `18844` 上的 `/v1/audio/transcriptions`，默认执行 CUDA / FP16 WhisperX 转写、强制对齐、community-1 分组和字词声源分配，ASR 和分组的 CPU threads 均为 8。通过当前项目的 `WhisperXRuntime`、`PyannoteDiarizationModel` 复用模型和音频合同；不识别人名，不承载 Video / Task 业务状态。

使用已安装 WhisperX extra 的 Python 环境，另需 FastAPI、uvicorn、python-multipart。`SEEKINO_GPU_MODELS` 指向模型根目录，包含 `faster-whisper-large-v3/`、`align-en/`、`align-zh/`、`align-ja/` 和 `community-1/`。后者使用完整的本地 community-1 snapshot，包含 `config.yaml`、`segmentation/`、`embedding/`、`plda/`，配置中的相对权重引用由 pyannote 按模型目录解析。模型来源、授权与 revision 由部署者保留，不在请求中下载。

```sh
export SEEKINO_GPU_MODELS=/root/seekino-gpu/models
export NLTK_DATA=/root/seekino-gpu/nltk_data
export HF_HUB_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 PYANNOTE_METRICS_ENABLED=0
export PYTHONPATH=/root/seekino-gpu/seekino
# CUDA 动态库目录沿用此环境已验证的 LD_LIBRARY_PATH。
/root/seekino-gpu/seekino/.venv/bin/python deploy/gpu/whisperx_server.py
```

请求音频必须为单声道、16 kHz、PCM16 WAV。`model`、`response_format` 保留旧 wrapper 的表单兼容；接口仍返回 verbose JSON。`diarize=false` 仅用于显式基线对照。默认分组失败时整个请求失败，不静默退回无标签结果。同进程请求串行执行，模型调用日志不记录原始音频或台词。

返回 `segments[].speaker_id` 供现有 CPU 标准化读取，同时保留 WhisperX 原生 `speaker`、字词和独立 `diarization` turns；按字词声源切换拆分句段，拼接后原文保持一致。缺少安全的字词时间或文字对应时，保留原段并令 `speaker_id=null`，不采用整段的多数声源替代短句。分组仍可能识别错误；非空 ID 不等于人工核实。

每次请求的 ID 只属于该输入音频。CPU 整片解析建议使用 `SEEKINO_ASR_SCOPE=video`，新任务会冻结 `models.asr.scope=video`，同一次完整请求的标签可在该任务的所有 Chunk 中复用；旧任务不随配置变化。CPU 对原生 `speaker` 的回退不覆盖显式 `speaker_id=null`。标签与人物名的对应仍由既有人物背景和证据链判断。

2026-11-13 实测：接口已部署到用户 GPU 并产生真实声源标签；EP04 原始目标句仍被误合并，未证明人物归属改善，因此 CPU 候选变更未部署、旧 EP04 未重解析。当前只是进程部署，未验收 GPU 重启自动恢复或 CPU 反向转发。完整证据和失败参数见[报告](../../docs/report/gateway/local/gpu-asr-speakers-20261113/README.md)。
