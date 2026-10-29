# 本地 Embedding 运行时与跨平台分发调研

日期：2026-10-29。目标：保留 Qwen3-Embedding-0.6B、1024 维文本检索，评估比要求用户安装完整模型平台更轻的部署方式。本文是源码与官方资料调查，不是替代运行时的实机性能验收；未安装新依赖、切换服务或重建索引。

## 判断与术语

建议保留当前可运行的 Ollama 试点，同时将 **由 Seekino 管理的 llama.cpp 子进程**与 **进程内 FastEmbed／ONNX Runtime**列为产品化候选：前者适合量化 GGUF 与多种桌面 GPU，后者适合无需端口的 CPU 基线。选择前需比较同批课程文本的资源、延迟和召回，不能仅凭“少一个进程”判断更轻。

本文中的用词是架构职责，不是通用标准规定：

| 概念 | 在 Seekino 中回答的问题 | 例子 |
| --- | --- | --- |
| Gateway | 应用怎样统一选模型、调用、限流、记用量与校验结果 | 当前 `gateway/` |
| Provider／Source | 模型服务由谁提供、连接哪个实例 | Ark、localhost Ollama；Source 还包含 URL 等配置 |
| Adapter | 用哪种请求格式与接口翻译 | Ollama `/api/embed`、TEI `/embed`、兼容 `/v1/embeddings` |
| Runtime／Backend | 谁真正加载权重、执行推理 | llama.cpp、ONNX Runtime、Torch、TEI 的 Candle 后端 |
| 模型 | 实际使用哪套参数与编码合同 | Qwen3-Embedding-0.6B + 权重版本 + pooling／指令／归一化 |

`OpenAI-compatible` 是协议兼容，不代表调用 OpenAI 云服务。ONNX 的 *Execution Provider* 又是 CUDA/CoreML 等硬件后端术语，和业务层的模型供应商不是同一层。[Seekino Gateway](../../gateway/README.md)、[ONNX Runtime EP](https://onnxruntime.ai/docs/execution-providers/)。

## RAGFlow 本地源码核对

实际目录为 `/Users/hypnos/workspace/rag/ragflow`，相对于 Seekino 是 `../../rag/ragflow`。
本次只读检查版本 `313ca90f6`（2026-09-24），工作树干净；结论针对这个本地 checkout，
不是声称已检查最新远程部署。

Python 调用链是 `LLMBundle / LLM4Tenant → EmbeddingModel[llm_factory] → 具体 Adapter`。
`rag/llm/__init__.py` 按 `_FACTORY_NAME` 注册适配类，
`tenant_llm_service.py` 根据租户模型配置选择类、API key 与 URL。这是它自己的模型
接入抽象；虽然仓库包含 LiteLLM 的 Chat 路线，embedding 此处使用专门适配器，
不能概括为所有向量调用都经过 LiteLLM 网关。
[工厂选择](/Users/hypnos/workspace/rag/ragflow/api/db/services/tenant_llm_service.py:34)、
[适配器注册](/Users/hypnos/workspace/rag/ragflow/rag/llm/__init__.py:165)。

| 本地路线 | 核对到的执行方式 | 源码 |
| --- | --- | --- |
| Builtin / HuggingFace | Builtin 在 `tei-*` compose profile 下构造 HuggingFaceEmbed，HTTP POST `/embed` 调 TEI | [Builtin](/Users/hypnos/workspace/rag/ragflow/rag/llm/embedding_model.py:224)、[HTTP Adapter](/Users/hypnos/workspace/rag/ragflow/rag/llm/embedding_model.py:1235) |
| Ollama | `ollama.Client.embed()` 调原生 `/api/embed`，可配置 keep_alive，批量编码 | [OllamaEmbed](/Users/hypnos/workspace/rag/ragflow/rag/llm/embedding_model.py:499) |
| Xinference | OpenAI 兼容客户端的 `embeddings.create()` | [XinferenceEmbed](/Users/hypnos/workspace/rag/ragflow/rag/llm/embedding_model.py:542) |
| Go Builtin | `BuiltinModel.Embed` 同样 HTTP POST `/embed`，默认本地 6380 端口 | [Go Builtin](/Users/hypnos/workspace/rag/ragflow/internal/entity/models/builtin.go:48) |

Compose 提供可选 `tei-cpu` / `tei-gpu` 服务；`.env` 的这两个启用示例均被注释，
不能据此说本机已经启动 TEI。配置默认 `TEI_MODEL=Qwen/Qwen3-Embedding-0.6B`，镜像
为 `infiniflow/text-embeddings-inference:cpu-1.8` / `:1.8`。因此 RAGFlow 的内置
本地方案也是独立模型服务，不是把向量模型直接嵌进 Python 主进程。
[Compose](/Users/hypnos/workspace/rag/ragflow/docker/docker-compose-base.yml:397)、
[TEI 默认配置](/Users/hypnos/workspace/rag/ragflow/docker/.env:281)。其注释中的模型 RAM
数字未提供本次可核对测量条件，不作为模型最低内存需求或与 Ollama 的性能对照。

## 当前机器的 Ollama 成本

本机为 Apple M5 / 16 GiB；本次只查询已运行的 Ollama 0.40.13，没有重新加载模型或
改变 keep_alive。`ps -axo pid,ppid,rss,comm` 与 `GET /api/ps` 的现场结果为：

```text
PID 93972，RSS 14064 KiB，进程为 outputs/tools/ollama/ollama
GET http://127.0.0.1:11434/api/ps → {"models":[]}
```

这表示观测时模型已经空闲卸载，daemon 的 RSS 快照约 13.73 MiB；它不是长时间平均、
峰值或已加载状态，也不是 macOS 完整 physical footprint。

文件大小按普通文件 `stat().st_size` 汇总，排除符号链接，避免把 du 的分配块数与逻辑
大小混用：

| 项目 | 实际观测 | 口径 |
| --- | --- | --- |
| 解压后的运行文件 | 608,727,919 bytes，580.53 MiB | 不含 `.tgz` 下载归档；含多种 runner 与库 |
| 下载归档，仍保留 | 186,689,993 bytes，178.04 MiB | 额外磁盘副本，不是常驻内存 |
| 运行文件内的 MLX v3 / v4 目录 | 199.65 / 239.40 MiB | 当前 Qwen 实验使用 llama.cpp runner；未做删除后的可运行性验收 |
| Qwen Q8_0 权重 | 639,150,592 bytes，609.54 MiB | 已有模型下载记录 |
| 模型加载后的 GPU 驻留 | 约 1.155 GiB | 上一轮 `/api/ps` 的 `size_vram`，不是 daemon 额外开销 |

上一轮模型与延迟原始证据见
[model-probe.json](../../outputs/verification/local-text-embedding-20261029/model-probe.json)
与 [实验 devlog](../dev/devlog.md#devlog-local-text-embedding-20261029)。当前项目虚拟环境
未安装 FastEmbed、ONNX Runtime、Torch、Transformers 或 Sentence Transformers；
因此“直接用现成 Torch”在该环境不是零新增依赖。本轮未检查其他服务各自的虚拟环境。

资源应分开统计：安装包／解压运行时的磁盘占用、模型文件磁盘占用、无模型时的 daemon RSS、加载模型后的进程/设备内存，以及推理峰值。减少模型管理服务不会消除模型权重和计算张量。RSS、GPU 统计、统一内存不可不加区分相加。

Ollama 官方 API 有独立 `/api/embed`，支持 `truncate=false` 和 `keep_alive`。FAQ 提供模型卸载与 `OLLAMA_KEEP_ALIVE`、并行数限制；因此可以按需加载/空闲卸载，不必让模型永久驻留。本次不修改现有服务参数。[Embedding API](https://docs.ollama.com/api/embed)、[Ollama FAQ](https://docs.ollama.com/faq)。

## 候选比较

| 方案 | 依赖与产品接入 | 平台证据与限制 | 对本次需求的判断 |
| --- | --- | --- | --- |
| Headless Ollama | 单独本地服务，模型下载/加载/卸载由它处理，应用走 HTTP | 官方有 macOS、Windows、Linux 安装入口；硬件和版本仍须验收 | 当前试点已跑通，保留作为用户自带后端；不必因为名称就判定运行内存很重 |
| llama.cpp `llama-server` | GGUF + 原生程序/后端库，可由 Seekino 启停，走本地 HTTP；无需 Torch | 官方提供三系统安装路线与 CPU、Metal、CUDA、Vulkan 等构建；按 OS/架构/后端分发，不能一份二进制覆盖所有机器 | 值得优先验证的桌面候选；减少用户独立管理平台的操作，但我们要承担下载、校验、进程与升级管理 |
| Sentence Transformers + Torch | Python 进程内加载模型，省独立服务；引入 Torch/Transformers 依赖 | 默认后端可选 CUDA/MPS/CPU，具体 wheel、算子和硬件要匹配 | 方便算法验证；省 daemon 不代表总安装或内存更小，尤其要避免服务多 worker 各加载一份 |
| FastEmbed + ONNX Runtime | Python 进程内，无 Torch 运行依赖；Qdrant 已提供 Qwen ONNX 导出 | ORT 有多平台 CPU 与硬件 EP，但安装包/算子是否支持某 EP要另验收，不能保证 Mac 自动走 GPU | 值得验证的无端口 CPU 候选；当前 int8 模型文件仍比 Q8_0 GGUF 大 |
| Hugging Face TEI | 专用文本向量服务，动态批处理与监控；HTTP/gRPC | 当前官方支持 Qwen3、原生 Apple Silicon Metal、CPU/CUDA；Docker 的 ARM64 CPU 路线不提供 Metal/MPS | 和 RAGFlow 服务化方向一致；不能说它只支持 Linux/CUDA，也不能据此推定原生 Windows 安装最简单 |

资料：[Ollama 安装入口](https://github.com/ollama/ollama/blob/main/README.md)、[llama.cpp 安装](https://github.com/ggml-org/llama.cpp/blob/master/docs/install.md)、[llama.cpp 构建后端](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md)、[Sentence Transformers 后端](https://sbert.net/docs/sentence_transformer/usage/efficiency.html)、[FastEmbed 依赖](https://github.com/qdrant/fastembed/blob/main/pyproject.toml)、[TEI README](https://github.com/huggingface/text-embeddings-inference)。这些是上游能力证据，不是 Seekino 已支持所有平台的声明。

## Qwen 与 llama.cpp 的明确适配合同

Qwen 官方有 `Qwen3-Embedding-0.6B-GGUF`，发布 Q8_0、F16；模型卡给出原生 `llama-embedding` 和 `llama-server` 用法，并明确使用 `--pooling last`。因此此路线不需要我们先自行训练或导出模型。[官方 GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF)。

仅作说明的上游启动形式，未在本轮执行：

```bash
llama-server -m model.gguf --embedding --pooling last --host 127.0.0.1
```

上下文、batch、GPU 后端和并行配置应按固定版本实测后设置。`llama-server` 提供兼容的 embeddings 路由，不能套用普通聊天响应或把 token-level 向量直接当句向量。[Server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)。

Qwen 原始 Transformer 示例使用最后一个非 padding token，随后 L2 归一化；查询添加任务 instruction，文档不加。Sentence Transformers 示例同样区分 query prompt 与 document。更换运行时必须保留这些规则，而不是只检查向量长度。[原模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)。

## FastEmbed 已有 Qwen 支持，仍需确认可部署版本

检查到的 FastEmbed `main` 注册了 `LastTokenNormalizedEmbedding`，包含：

| FastEmbed 模型名 | Qdrant 提供的模型文件 | 官方标注大小/限制 |
| --- | --- | --- |
| `Qwen/Qwen3-Embedding-0.6B` | `onnx/model.onnx` + `model.onnx_data` | fp32，2.38 GB |
| `Qwen/Qwen3-Embedding-0.6B-Q` | `onnx/model_quantized.onnx` | int8 weights / fp32 activations，1.12 GB；要求 ORT ≥ 1.23 |

两者为 1024 维，源码在图外执行 last-token pooling 与 normalization。模型由 **Qdrant 转换发布**，不应称为 Qwen 团队的官方 ONNX 包。上游 `main` 的 pyproject 标注版本 0.9.1，但本轮未确认对应 PyPI 已发布版本/安装后注册表；具体版本锁定仍待执行。[FastEmbed 注册实现](https://github.com/qdrant/fastembed/blob/main/fastembed/text/last_token_normalized_embedding.py)、[Qdrant 模型卡](https://huggingface.co/Qdrant/Qwen3-Embedding-0.6B-onnx)。

**`query_embed()` 不会替我们加 Qwen instruction。** Qdrant 示例要求调用方先拼接任务指令；直接调用函数即使输出 1024 维也不代表检索编码正确。模型卡还说明较小维数需自己截短并重新归一化。本项目当前保持 1024 维，不因可裁维自动改动合同。[Qdrant 使用说明](https://huggingface.co/Qdrant/Qwen3-Embedding-0.6B-onnx#usage)。

FastEmbed 运行依赖使用 `onnxruntime`、tokenizers 等，没有把 Torch 列为运行依赖；这是依赖简化的证据，不是内存/性能胜出的实测。ONNX Runtime 有 CoreML、CUDA、DirectML 等 EP；某 EP 存在不表示 Qwen 量化模型全部算子会在该设备执行。CPU 可作为兼容基线，GPU 逐平台另测。[依赖清单](https://github.com/qdrant/fastembed/blob/main/pyproject.toml)、[ORT EP](https://onnxruntime.ai/docs/execution-providers/)。

## 推荐的产品形态与验证边界

候选产品体验可以是“本地文本检索：自动准备所需运行时和固定模型”，让用户看到下载体积与状态；后台选择一个经过验收的实现。Provider/Adapter 保留扩展能力，普通用户不必先了解 Ollama、TEI 或 Torch。这个建议尚未实施。

优先比较两条路线：

1. **llama.cpp 管理子进程**：Mac 用 Metal，其他桌面平台提供 CPU 基线并按支持条件启用 GPU。保留用户已有 Ollama 为可选来源。
2. **FastEmbed 进程内 CPU**：若用户明确更看重无端口与统一 Python 部署，比较其权重下载、应用增量依赖和批处理速度；若后续考虑 GPU，单独增加 EP 验收。

TEI 保留为共享向量服务或服务端批量索引候选；原生 Apple Silicon 已有文档支持，因此也可加入 Mac 实测。当前没有同机器数据支持在三者间宣布绝对 winner。

最小验收应记录：固定运行时/权重 digest、查询模板、pooling、归一化、截断策略；Mac ARM、Windows x64、Linux x64 各一次安装/编码/重启；空闲与加载内存、峰值、磁盘/下载量、首次与热请求、批处理；以及独立中文问题召回英文课程文本的质量。

运行时变更不能只切查询端。即使名字和维数相同，量化、tokenizer、模板或数值实现也可能改变结果；应在独立索引比较兼容性，未证明兼容前重新生成整套文档与查询向量。[现有向量配置合同](../model-config.md#本地文本向量与视觉向量开关)。

本轮结论是轻量化候选与证据，不是安装、迁移或全平台验收已完成。没有对 llama.cpp、TEI、FastEmbed 做实机 benchmark，也没有证明更换运行时会直接提高课程召回质量。
