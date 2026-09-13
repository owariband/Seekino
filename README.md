# AniVAG

AniVAG 是一个面向事件级视频检索与时间证据定位的多模态视频理解产品。M1 以动画名场面为首个落地场景：用户导入若干集动画视频后，可以用自然语言查询梗、动作、人物关系或情绪变化，并获得可跳转的集数、时间戳与关键帧证据。

当前阶段采用 **AniVAG 产品主仓 + 可替换检索引擎** 的结构。上游 VideoRAG 通过 Git submodule 接入，为多模态索引与检索提供实现参考；AniVAG 的 Gateway 只封装模型调用，视频解析算法与整集编排由 Engine 承担。

## M1 边界

- Anime-first：课程视频只作为未来迁移验证，不改变产品定位。
- Video-only：用户不需要上传外挂字幕文件。
- 语音、镜头与关键帧进入查询前的异步建库链路；主路径调用在线模型，内容审核失败时默认降级到本地有序帧模型，也可选择尚待 A/B 的 llama.cpp direct-video 路径。烧录字幕 OCR 暂不阻塞第一版。
- 在线查询融合语音文本、视觉与角色事件候选，返回时间证据。
- V1 使用持久化 Job Store + Worker；RocketMQ 不是单用户 MVP 的前置依赖。

## 快速开始

仓库根目录是运行配置的唯一入口：`config.yaml` 保存可提交的模型默认值，`.env.local` 保存 API Key 和个人 Endpoint，不进入 Git。

```bash
cp .env.example .env.local
# 编辑 .env.local，填写 ARK_API_KEY、DEEPSEEK_API_KEY 和个人 ep-* Endpoint。

uv sync --project packages/gateway
uv run --project packages/gateway --env-file .env.local \
  anivag-model --help
```

视频解析命令见 [`packages/engine/README.md`](packages/engine/README.md)，模型调用与跨模态向量冒烟见 [`packages/gateway/README.md`](packages/gateway/README.md)。

## 仓库结构

```text
AniVAG/
├── config.yaml                       # 可提交的模型默认配置
├── .env.example                      # 本地密钥与覆盖项模板
├── docs/
│   ├── anivag-system-design.html     # 自包含工程蓝图
│   └── diagrams/                     # 可再生成的图源与单图产物
├── engines/
│   └── VideoRAG/                     # 上游算法底座（Git submodule）
└── packages/
    ├── gateway/                      # Ark/Ollama/llama.cpp 模型调用与 Embedding
    └── engine/                       # Segment 解析与整集领域编排
```

## 工程蓝图

直接用浏览器打开 [`docs/anivag-system-design.html`](docs/anivag-system-design.html)。该文件已内嵌系统架构、动画建库、失败续跑和名场面检索四张图，离线可用。

## 上游归属

`engines/VideoRAG` 来源于 [Slimzeo/VideoRAG](https://github.com/Slimzeo/VideoRAG)。AniVAG 会保留上游许可证与提交历史，并明确区分 Adapter 集成、独立复现和动画领域改造三类贡献。
