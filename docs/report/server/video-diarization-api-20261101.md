# GPT 整集分组正式接口与恢复试点（2026-11-01）

关联：[ASR 主 TRD](../../design/03-product-reassessment/local-whisperx-asr-trd.md)、[离线 CPU/MPS 实测](../gateway/local/pyannote-mps-20261101.md)、[PR #18](https://github.com/owariband/Seekino/pull/18)。代码仍在 `fix/video-asr-recovery` 工作区，未 commit/push；真实模型与 API 使用隔离 MySQL/ES，没有部署到旧开发库。

## 问题与实施边界

正式 whisper.cpp Turbo 的原始 ASR 已持久保存，但 speaker_id 全为空。离线分组文件不能直接证明准备链、失败恢复或 Understanding 已实际消费。此次新增可选冻结的 models.diarization，通过 Gateway 的本地 community-1 Pipeline、Engine 的毫秒/分组合同、Server 的准备及当前租约提交接入原链路。原始 ASR 与 diarization 分列保存；新增分组配置不改旧任务，不覆盖原文、时间或旧 Chunk。

没有重新运行 Whisper。旧版 ASR 先按同一 Video、媒体、冻结来源/模型、Schema 和正文摘要核对，在新任务合法租约下复用，保留 reused_from_task_id。普通详情/任务轮询不加载两个全文。旧版完整成果未持久化的情况仍不能凭实验产物回填。

## 环境、执行与恢复

- 原片：《Let's build GPT》，6,979,873 ms，117 Chunk；16 kHz/单声道 PCM16 音轨约 6,979.942 秒。
- 硬件：Apple M5、10 核 GPU、16 GiB 统一内存。
- 旧任务：`613f40e2-7bcb-4e38-9ae4-9ad1021afb51`，原 30 个 Understanding、3 个 Refine、1 个 Indexed 的历史成果保留。
- 新版本：`6cf69603-6708-4d24-bee3-2dff0e858ad3`，通过正式 `/api/uploads/reparse` 创建；parse_concurrency=1，其他已冻结输入保留，追加分组绑定。
- 模型：community-1 revision `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`；pyannote.audio 4.0.7 / torch 2.8.0；CPU threads=6，segmentation/embedding batch=32；不强制人数。
- API：本 worktree 的 API/Consumer，端口 19621；数据库 `seekino_video_asr_recovery_20261031_test`，端口 13361；ES 19231。先核对旧任务暂停且租约排空、备份库、停止写入者，再用正式迁移脚本添加 nullable diarization JSON 列。只修改隔离模型配置。

MPS 不可用或两个神经模型实际设备不符即报错，禁用不支持算子的静默 CPU 回退；聚类等 CPU 运算仍存在。传入已解码 waveform，避免本机 TorchCodec/FFmpeg 动态库问题。推理期间临时 caffeinate 防空闲休眠，模型完成后释放引用/缓存。单 Gateway 同设备串行，不宣称这是测得的并发上限。

完整分组提交后，对核对过的 API PID 执行 SIGKILL，重新启动同一工作树。观察脚本最初错误地从任务表读取素材序号，触发 Unknown column 后自动暂停；修正为 join video_chunk 后继续同一个任务，没有重新跑音频。恢复日志同时显示 ASR 和 diarization cache_hit。该测试脚本错误保留在原始 error.json 中，不归为模型或产品失败。

## 实测耗时与资源

| 阶段 | 本轮正式 API 实测 |
| --- | ---: |
| Adapter 总日历耗时 | 425.828 s（约 7 分 6 秒） |
| Adapter 单调时钟耗时 | 425.831 s |
| segmentation | 19.615 s |
| embeddings | 398.565 s |
| 含音频准备的 ASR/分组阶段记账 | 429.425 s |
| ASR 新请求 | 0 |

主要计算仍是声纹提取。两种时钟几乎一致，本轮没有前一次离线实验的长时间休眠。此阶段账目包含复用后的分组及音频准备，不是新鲜 Whisper+分组全流程耗时。转写历史约 3 分钟，不能把分开的实验相加冒充本轮整体实测。

回调观察到 MPS driver 分配最大 4,066,492,416 bytes（约 3.787 GiB），活跃 tensor 32,480,512 bytes；回调发生在批次结束，后者漏掉中间张量，不当真实峰值。独立整集实验的每秒采样为 tensor 0.981 GiB、driver 3.699 GiB、OS RSS 1.679 GiB；统一内存口径有重叠，不能相加。本轮没有测 GPU 利用率、功耗、整机内存压力或硬件并发上限。

用户询问的独立 CPU VAD 已对完整原音轨完成：6 threads/batch32，模型 1,472,749 参数，推理 268.170 s、含提取/初始化 274.551 s、峰值 RSS 1.297 GiB。17 个 CPU 样本中位数 250.5%（约 2.5 个逻辑核的占用口径）、最大 268.1%，不是整机利用率。此次没有为重复数字再跑 VAD，也没有将它额外串在已完成转写前；pyannote 分组内部自带 segmentation。

## 分组内容与实际输入

原 ASR 2,476 条 cues / 24,088 个 token/word，原始摘要保持 `1910697d9ea39ce5c671a3be13327ebda4518dd310c61152f1f9877adcf597c4`。新持久分组摘要为 `8875920a4ccd7134fa7597d070346529f59fe5b041e53a4bb527500eaa99cd8c`。

regular 输出包含 1455 段、两个匿名簇；SPEAKER_01 只有 9 个重叠短段、累计 1266 ms，未人工核实为第二个人。exclusive 输出 1446 段，主声源均 SPEAKER_00。按 exclusive 直接时间重叠合并，2,474 条 cues 获得主声源标签，`matter`、`Colab.` 两条没有覆盖而保持 null；原文、cue 和 token 时间逐字段不变。跨多个主声源的 cue 保留未知，不按最近声音强制补全。

真实 Kimi 请求 `fdc4e02f-ae06-4b29-88ca-4e9d6ebbcf25` 的首块输入已核对：

```json
{
  "speaker_scope": "video",
  "speaker_scope_id": "6cf69603-6708-4d24-bee3-2dff0e858ad3",
  "speaker_ids": ["SPEAKER_00"],
  "transcript": [{
    "start_seconds": 0,
    "end_seconds": 3600,
    "text": "Hi everyone. So by now you have probably heard of ChatGPT.",
    "speaker_id": "SPEAKER_00"
  }]
}
```

此处 *_seconds 是历史字段名，值为整数毫秒。Kimi stream=true，189 张 timestamp 配对图片。Prompt 明确相同 scope/ID 可跨 Chunk 复用，但不能把匿名编号注册成已知姓名；没有直接人物映射时保留 speaker=null，避免伪造。声源候选的一致性与真实人物身份、人工质量验收分别判断。

首个 Understanding 已在重启后成功提交；持久 Chunk evidence 中 22 条输入 cues 均含 SPEAKER_00 和同一全片 scope。输出 18 个事件：2 SCENE、4 ACTION、5 ON_SCREEN_TEXT、6 DIALOGUE、1 EMOTION；persons 为“右下角男性讲解者”，6 个 DIALOGUE.speaker 均为 null。模型没有从匿名声源直接确认人物；可见推理仍讨论这种映射及事件边界，不能认定归属犹豫已消失。

| Kimi 请求阶段 | 耗时 / 用量 |
| --- | ---: |
| 请求开始到首 SSE | 50.781 s |
| 首 reasoning 到末 reasoning | 180.503 s |
| 首正文到末正文 | 29.606 s |
| 请求开始到 completed | 260.990 s（约 4 分 21 秒） |
| prompt tokens（Provider 上报） | 233,207 |
| completion / reasoning tokens | 6,949 / 5,358（reasoning 占 77.1%） |

历史同一首块的成功调用 `8f5c9522-cdb4-4ced-837c-7340dd6c0a50` 为 234.166 s、189 图、7,025 completion / 5,475 reasoning tokens；当时 stream=false、日期/网络/并发和缓存条件不同，不是受控声源标签 A/B。本次未证明 Understanding 提速，也不能把差值归因于标签使模型变慢。当前仍需解决“匿名声源如何与人物对应”和原有多模态推理负担，Refine 600 秒问题没有在本轮解决。

完整试点观测约 848.708 s，包含音频、受控崩溃重启、观察 SQL 修正、媒体准备和一个真实 Understanding，不作为全课程吞吐。首块提交后经正式 control API 暂停；最终租约 5、没有活跃 Chunk 租约，ASR 新请求为 0、分组只加载 1 次、重启命中分组 1 次。新版本仅 1/117 Understanding；旧版本 30/117 成果保留，未伪称新输入下已完成。

## 服务交付状态

试点开始时原 whisper.cpp 进程已停止，本次依靠已验证 ASR 缓存完成组合与恢复。收尾再次核对端口和控制记录：API 19621 仍可用，新任务暂停；恢复 whisper.cpp 服务到 18842，launcher/server PID 为 12654/12655。先验证原 binary/模型 SHA-256 未改变，再沿用原解码参数并要求 Metal；实际后端与 `/health` 已核对，未额外发转写请求。控制记录更新于 `outputs/local-services/whisper/runtime.json`，使用隔离试点模型配置。见[服务就绪记录](evidence/video-diarization-api-20261101/whisper-ready.json)。完整新鲜转写与分组同时驻留的整机峰值未单独测量，不能把两次独立资源样本相加当峰值。

## 验证与剩余边界

226 项定向及 726 项相关后端回归通过，覆盖分组失败恢复、独立正文损坏、旧租约拒绝、旧快照关闭分组、跨版本媒体/生产身份匹配复用、按需 Chunk 兼容、迁移幂等与准备门禁。最后移除无硬件依据的 pyannote batch 上限，配置/恢复 69 项复验通过（与前述用例重叠）。新前端配置合同通过独立严格类型检查；完整 web typecheck 因本 worktree 未安装 web/node_modules 而未完成，不标通过。

实现/真实接入验证不能代替效果验收。声学强制对齐、人工 DER/人物映射准确率、整集 117 Chunk、Refine、索引检索回看问答，以及主开发部署仍未完成；B20 保持 TODO。下游性能调查见[性能 TRD](../../design/03-product-reassessment/video-parse-performance-trd.md)。

证据：[音频摘要](evidence/video-diarization-api-20261101/audio-summary.json)、[阶段日志](evidence/video-diarization-api-20261101/diarization-progress.jsonl)、[恢复命中](evidence/video-diarization-api-20261101/recovery-events.log)、[验证范围](evidence/video-diarization-api-20261101/validation.json)、[真实 Understanding 时序和内容统计](evidence/video-diarization-api-20261101/understanding-summary.json)、[试点摘要](evidence/video-diarization-api-20261101/pilot-summary.json)、[最终暂停状态](evidence/video-diarization-api-20261101/final-state.json)。完整原始输出、观察脚本、SQL 备份及受控配置备份位于本机 `outputs/verification/video-asr-api-recovery-20261031/diarization-api-20261101/` 及其父目录；备份不进入 Git，不包含在便携报告中。
