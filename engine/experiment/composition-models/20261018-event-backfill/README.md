# MyGO 13 集旧 Event 索引补建

执行日期：2026-10-18。用户明确要求补齐旧数据的 Event，复用当前正式 Parse / Refine / Timeline，通过现有 INDEX 补建入口完成。这里记录运行证据，不新增生产数据模型。

**结果：已完成并验收通过。** 16:17（北京时间）观测到 13 集全部 COMPLETE、队列清空；16:19 完成全量结构核验、真实检索及服务健康检查。共 312/312 Chunk、7,103/7,103 Event、16,535 条帧采样记录，本轮 INDEX retry_count=0、error_count=0。正式内容 ID、版本和 SHA-256 全部与补建前一致。摘要见 [verification.json](verification.json)，逐项结果见 [after.json](after.json)。

## 范围与执行过程

- 目标 Series：`836fa9f0-2bf4-50fb-9c64-75526fa1dea9`，tenant=`local-default`，MyGO 正片 13 集、312 个 Chunk。
- 依据正式 Timeline 投影计算：预期 7,103 个 Event 文档、16,535 个帧采样记录（含不同 Event 间重复采样，不等于独立画面数量）。
- 补建前：13 个最新任务均 COMPLETE、Event 发布覆盖均为 0/24；各 Chunk 的正式内容 ID、版本、SHA-256 见 [before.json](before.json)。
- 2026-10-18 15:31（北京时间）开始第 1 集试跑；完成后再提交剩余集数。
- 15:36 第 1 集完成并通过逐项 ES 核验：24/24 Chunk、566 Event、1,291 个帧记录；正式内容 ID／版本／哈希与基线一致。两条真实 Event 视觉检索通过，见 [pilot-verified.json](pilot-verified.json)、[search-ep01.json](search-ep01.json)。
- 15:37 剩余 12 集全部通过同一 CLI 排队；逐集回执见 `queued-ep02.json` 至 `queued-ep13.json`。运行观测见 [progress.jsonl](progress.jsonl)。
- API / Parse Consumer 正常；运行服务配置摘要与 `.env.local` 加载后的摘要一致。未重启服务。

## 实际合同

- 独立 Event 索引：`seekino_video_events_development_v1`。
- Contract：`event-index:sha256:f9a6b7da7081de5958eabede0bdeb2fc75b1fd8979745fae41d1e7abc2a03e1f`。
- 文本／视觉 encoder：现有 `doubao-embedding-vision-250615`，各 1024 维。本轮是正式 Event 索引补建，后续 PCP＋人物框图 DINOv2 实验仍使用原定各模型。
- Event 视觉采样：1 fps、最多 3 帧、均匀分箱中点、JPEG、最高 720p；单帧 L2 后均值再 L2。
- 不生成新 Caption；保留现有 Timeline。索引持久化抽帧时间与图片 SHA-256，抽取 JPEG 为临时文件，不视为永久关键帧图库。

## 执行与复现

从仓库根目录执行，凭据由 `.env.local` 加载，不复制到实验文件：

```bash
uv run --no-sync --env-file .env.local python -m server.cli request-es-index-rebuild \
  --environment development --video-parse-task-id <task-id> --with-events
```

上述命令只提交 INDEX 队列，返回不能作为完成证明。每集提交回执记录为 `queued-epNN.json`。已有 API 内的 Parse Consumer 负责实际处理。

只读检查（`--episode 1` 可限定一集；完成后加 `--verify-es`）：

```bash
uv run --no-sync --env-file .env.local python \
  engine/experiment/composition-models/20261018-event-backfill/inspect_backfill.py \
  --output engine/experiment/composition-models/20261018-event-backfill/after.json \
  --verify-es
```

## 验收口径

1. 13 集重新 COMPLETE，312 个 Chunk 的 Event 发布回执完整。
2. 从正式 Timeline 重算预期 Event ID；ES 文档身份／版本、文本投影、帧时间、向量合同逐项匹配，且完整集合已可搜索。
3. 限定这 13 个 task ID 的 ES 文档总数等于预期，不把整个共享索引总数当作本次产出。
4. 补建前后正式内容 ID／版本／SHA-256 相同。
5. 直接调用 Event retrieval service 做真实视觉通道检索并保存结果。现有 `server.cli search` 仍调用 Chunk 检索，不能用于证明 Event 链路。

以上全部通过。13 集分别覆盖 24/24 个 Chunk：

| 集数 | Event 文档 | 帧采样记录 | 状态 |
|---|---:|---:|---|
| 1 | 566 | 1,291 | COMPLETE |
| 2 | 575 | 1,306 | COMPLETE |
| 3 | 554 | 1,357 | COMPLETE |
| 4 | 573 | 1,320 | COMPLETE |
| 5 | 534 | 1,251 | COMPLETE |
| 6 | 544 | 1,235 | COMPLETE |
| 7 | 572 | 1,332 | COMPLETE |
| 8 | 469 | 1,113 | COMPLETE |
| 9 | 521 | 1,200 | COMPLETE |
| 10 | 509 | 1,209 | COMPLETE |
| 11 | 609 | 1,364 | COMPLETE |
| 12 | 533 | 1,249 | COMPLETE |
| 13 | 544 | 1,308 | COMPLETE |
| 合计 | **7,103** | **16,535** | **13/13 完成** |

全量真实检索见 [search-all.json](search-all.json)，两次均 vision-only、覆盖 312 个来源 Chunk、partial=false、rejected=0：

| 查询 | 返回 Event | 返回 Chunk 组 | 本次端到端耗时 |
|---|---:|---:|---:|
| 俯视房间，人物很小，坐在沙发上 | 19 | 5 | 5.75s |
| 舞台上乐队演奏 | 8 | 5 | 5.30s |

`top_k=5` 代表不同 Chunk 组，组内可以返回多个 Event，不能把返回 19 个 Event 当成超出 top_k。俯视查询包含第 7 集 `248600–249100ms` 的 Event，落在先前 Chat 找到的 `246559–249100ms` 候选范围内；同时也返回其他室内画面，结果仍需按构图目标看图评议。这两次耗时是本次运行观测，非性能基准。

Event 检索可用后，构图实验可根据 Event 来源和时间取帧扩大共用图库；本轮索引验收不等于构图相似度效果评测。

## 运行性能观察

15:44:43 只读采样：全局视频准入为 3 集，每集冻结 INDEX 并发上限为 10（瞬时实际运行数量不等于上限）。此前约 5 分钟内完成 image embedding 1,749 次、text embedding 773 次，失败 0；图片调用耗时中位数约 320ms、P90 400ms，文本约 125ms、P90 196ms。

当前实现同一 Chunk 内逐个 Event、逐帧串行执行，每帧独立 ffmpeg 抽取并保存可恢复缓存。上述统计不能把总耗时精确归因于模型、抽帧或数据库；本轮沿用现有配置，未调整并发、超时或重启服务。可按 [progress.jsonl](progress.jsonl) 观察实际发布吞吐。

## 本轮维护范围

```text
AniVAG/
├── docs/
│   └── dev/
│       └── [M] problems.md                 ← 更新 C16 的 Event 依赖与补建结果
└── engine/
    └── experiment/
        └── composition-models/
            ├── [M] README.md              ← 增加本轮数据准备入口
            └── 20261018-event-backfill/
                ├── [A] README.md          ← 执行、合同、验收和后续实验边界
                ├── [A] inspect_backfill.py ← 只读基线／全量来源与 ES 验收
                ├── [A] progress.py        ← 只读进度采样
                └── [A] smoke_event_search.py ← 真实 Event 视觉检索验收
```

另保存 21 个运行产物：13 个逐集提交回执、`before.json`、`after.json`、`pilot-progress.json`、`pilot-verified.json`、`progress.jsonl`、`search-ep01.json`、`search-all.json`、`verification.json`。本目录共 25 个文件，沿用实验目录的本地 ignore 规则。

生产代码修改 0；执行现有补建入口写入 INDEX 状态、可恢复 embedding 缓存、发布回执和独立 Event ES 投影。正式 Timeline 与任务版本经基线对比保持一致。没有数据库 Schema 迁移、重启、commit 或 push。脚本语法检查通过；验收使用真实 DB／ES 和 4 次真实检索（单集 2 次、全量 2 次），未运行与本轮无关的单元测试。

共享工作区同时存在其他工作线的 Chat 代码与 C17／C18 文档改动，不属于本轮补建；`problems.md` 本轮只维护 C16 及其顶部进度说明，未覆盖其他工作。
