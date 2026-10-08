# DOVideo-AI 源码审计证据（2026-10-07）

本笔记为 AniVAG 与 DoVideoAI 可视化对比报告提供外部项目的一手证据。比较对象由用户确认是 [Xiaoc7r/DOVideo-AI](https://github.com/Xiaoc7r/DOVideo-AI)，不将同名项目混入结论。

- 固定源码版本：`df4fa83a78a51dfa1718606bf47cf49942e1112e`，`main`。
- 本地只读审计副本：`/private/tmp/anivag-dovideoai-audit`，使用 `git clone --depth 1` 获取。
- HEAD 提交时间：2026-09-30 18:56:20 +08:00；标题 `Merge pull request #12 from Xiaoc7r/codex/full-review-20260930`。依据本地 `git show -s`，可核对 [固定提交](https://github.com/Xiaoc7r/DOVideo-AI/commit/df4fa83a78a51dfa1718606bf47cf49942e1112e)。提交标题不能证明某段代码的作者能力或 AI 生成比例。
- 许可证：MIT；[LICENSE:1-13](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/LICENSE#L1-L13)。
- GitHub 仓库页在本次抓取时展示 98 commits、41 forks；根任务通过 GitHub API 抓取 368 stars、2026-01-13 创建、2026-09-30 最近 push。动态元数据仅反映公开传播和仓库历史，不进入技术复杂度或含金量分数。[仓库页](https://github.com/Xiaoc7r/DOVideo-AI)，[官方仓库元数据 API](https://api.github.com/repos/Xiaoc7r/DOVideo-AI)。此副本是浅克隆，不能拿本地 `rev-list --count` 当总提交数。

## 审计边界与证据等级

**实现事实**：追踪当前固定版本的 controller → dispatch → MQ consumer → ASR/OCR → retrieval → AgentLoop → checkpoint → 状态/结果读取，检查生产源码、迁移、CI 与测试。**推断**：根据实现给出的架构、维护性或风险判断。**未验证**：真实媒体解析准确率、完整外部服务联调、生产负载、真实账单与安全部署。本次没有启动业务服务，没有调用外部 AI/ASR/Embedding，没有连接数据库或消息队列。

源码中的实现和仓库里的演示数据分开处理；截图、README 叙事、展示分数都不能替代运行测量。负面结论限于已检查目录和文件，不能外推为作者从未验证、所有部署都有漏洞或系统一定不能工作。

## 可复算规模

采用物理非空行统计，包含注释与 docstring，**不称为 SLOC**。固定 HEAD 的已跟踪源码，后端纳入 `server/src/main/java`，前端纳入 `client/src` 和 `client/index.html`，浏览器插件单列 `extension`。只计 `.java/.js/.ts/.vue/.css/.html`；排除测试、依赖、生成和构建物。独立黄金任务实验 runner `OfflineAgentEvaluationRunner.java` 排除；`AgentEvaluationService.java` 被生产 `/analysis/agent-evaluation` 端点调用，保留为运行时评分服务。

| 范围 | 生产文件 | 物理非空行 |
| --- | ---: | ---: |
| Java 后端（保留运行时评分，排独立离线 runner） | 85 | 7,880 |
| Vue/Web（src + index.html） | 12 | 3,246 |
| 浏览器插件（排 tests） | 10 | 1,381 |
| 合计 | **107** | **12,507** |
| 不含插件的 Web + server 主路径 | 97 | 11,126 |

边界敏感性：若进一步排除运行时 `AgentEvaluationService.java`，合计降为 106 文件 / 12,426 非空行；若纳入 `client/vite.config.js`，增加 1 文件 / 18 非空行。不要混用不同口径。上述数字不计 Maven/Python/前端配置清单、SQL、YAML、Markdown 等非源码格式，因此不能当整个仓库所有内容规模。

| 最大生产文件 | 非空行 | 全部物理行 |
| --- | ---: | ---: |
| `client/src/App.vue` | 1,664 | 1,785 |
| `client/src/useAnalysisWorkspace.js` | 788 | 836 |
| `server/src/main/java/com/example/server/utils/DeepSeekUtils.java` | 527 | 570 |
| `server/src/main/java/com/example/server/service/AiService.java` | 510 | 555 |
| `server/src/main/java/com/example/server/service/AgentLoopService.java` | 484 | 526 |
| `server/src/main/java/com/example/server/service/VideoContextService.java` | 369 | 401 |
| `client/src/chunkUpload.js` | 369 | 411 |

静态测试声明为 24 文件 / 106 条，测试源码 1,607 非空行：Java 14 文件 / 27 个 `@Test` / 765 非空行；client 5 文件 / 25 个 `test()` / 382 非空行；extension 5 文件 / 54 个 `test()` / 460 非空行。声明数不等于参数化执行案例数、断言数、测试通过率或覆盖率；不同语言不能按声明数线性比较可靠性。

以下脚本可在固定副本中复算生产量，并打印完整纳入清单，不依赖第三方包：

```python
from pathlib import Path
import subprocess

repo = Path('/private/tmp/anivag-dovideoai-audit')
paths = subprocess.check_output(
    ['git', '-C', str(repo), 'ls-tree', '-r', '--name-only', 'HEAD'],
    text=True).splitlines()
suffixes = {'.java', '.js', '.ts', '.vue', '.css', '.html'}
total_files = total_nonblank = 0
for name in paths:
    p = Path(name)
    in_scope = (name.startswith('server/src/main/java/') or
                name.startswith('client/src/') or name == 'client/index.html' or
                name.startswith('extension/'))
    is_test = ('tests' in p.parts or 'test' in p.parts or
               '.test.' in p.name or '.spec.' in p.name)
    if not in_scope or is_test or p.suffix not in suffixes:
        continue
    if p.name == 'OfflineAgentEvaluationRunner.java':
        continue
    content = subprocess.check_output(
        ['git', '-C', str(repo), 'show', 'HEAD:' + name], text=True)
    nonblank = sum(bool(line.strip()) for line in content.splitlines())
    print(name, nonblank)
    total_files += 1
    total_nonblank += nonblank
print(total_files, total_nonblank)  # 107 12507
```

## 16 个核心源码事实

### 1. 架构是完整的应用编排，基础设施依赖有实际调用

**实现事实**：Java 21 / Spring Boot 3.5.9，REST API，MyBatis/MySQL，Redis/Redisson，RocketMQ，MinIO，LangChain4j；Vue Web 与 Manifest V3 侧边栏插件。Compose 配置 MySQL、Redis、Qdrant、MinIO、MQ nameserver 和 broker 六个容器。后台主要是一套 Spring 应用服务，而非六个独立业务微服务。真实请求路径为 `/analysis/ai` 校验归属 → `AnalysisDispatchService.submit` 投递 → `VideoAnalysisConsumer.onMessage` → `AiService.asyncAnalyze` → `AgentLoopService.run`。

证据：[pom.xml:5-17,54-109](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/pom.xml#L5-L109)，[AnalysisController.java:96-110](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/controller/AnalysisController.java#L96-L110)，[AiService.java:97-132](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AiService.java#L97-L132)，[docker-compose.yml:1-82](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/docker-compose.yml#L1-L82)。

**推断**：依赖部署和故障处理复杂度较高，但组件个数本身不是原创算法数量。

### 2. 分片、断点续传与合并重试真正实现

**实现事实**：分片上限 5 MiB，最多 410 片。Redis 元数据保存文件名、总片数与 userId，进度以 set 记录，MinIO 保存分片；上传/查询校验所有者。合并用 Redisson 锁，串流写临时文件并计算 MD5，成功媒体记录后写入 25 小时完成回执，迟到重试先返回已有媒体记录；分片清理失败独立记录。

证据：[ChunkUploadService.java:37-40,57-113,115-187](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/ChunkUploadService.java#L37-L187)，[ChunkUploadServiceTest.java:78-108](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/test/java/com/example/server/service/ChunkUploadServiceTest.java#L78-L108)。

**边界**：对“响应丢失”的重试有明确代码和 mock 单测；跨 MySQL/MinIO/Redis 每个写入中断点的完全一致性未联调，不能等同事务性 exactly-once。

### 3. MQ 防重复、内容复用与模式隔离有真实实现

**实现事实**：提交以 `mediaId + goalDigest(goal, mode)` 的 active key 做 `SET NX`，有效期 6 小时。消费者用媒体任务锁与内容任务锁，完成结果按内容指纹+目标模式索引复用，同内容的另一条媒体记录等待共享结果后写自己的 checkpoint 和终态。`AiService` 另有内容级上下文锁，避免不同目标重复 ASR/OCR。

证据：[AnalysisDispatchService.java:63-101](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AnalysisDispatchService.java#L63-L101)，[VideoAnalysisConsumer.java:89-175](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/consumer/VideoAnalysisConsumer.java#L89-L175)，[AiService.java:161-206](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AiService.java#L161-L206)。

**边界**：这是状态与锁配合的幂等设计，不能仅凭源码称为全局 exactly-once 或宣称具体并发容量。

### 4. 有有界重投、毒消息处理与人工重试入口

**实现事实**：MQ 注解最多 2 次重投，应用计数最多 3 次投递。永久参数/权限/资源错误直接收敛；其他失败保留 active 标记并抛出触发 MQ 重试。耗尽重试时写失败任务表和自定义失败主题；毒消息至少成功留下台账或失败主题之一才确认，双失败时拒绝确认。预算耗尽则终止，不重跑全链路。

证据：[VideoAnalysisConsumer.java:30-49,180-242,247-317](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/consumer/VideoAnalysisConsumer.java#L30-L317)，[AdminController.java](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/controller/AdminController.java)。

**边界**：自定义失败主题不应称为自动原生 DLQ。消费者注释也明确未配置其并发参数；未做 broker 真实故障注入。

### 5. Checkpoint 保存阶段中间产物，MySQL 是恢复真源

**实现事实**：context/chunks 为媒体级，plan/execution draft/critic/result 为目标与模式级。持久化采用 MySQL upsert，JSON payload 和阶段在事务中写入，提交后再写 Redis；缓存读取失败或损坏会回查数据库，Redis TTL 7 天。Agent 的草稿已保存且尚未 Critic 时可从 Critic 继续，完成 checkpoint 可以跳过重算。

证据：[AgentCheckpointService.java:51-170](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentCheckpointService.java#L51-L170)，[AgentCheckpointRepository.java:70-101,133-165,191-201](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/repository/AgentCheckpointRepository.java#L70-L201)，[AgentLoopService.java:97-143](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentLoopService.java#L97-L143)。

**推断**：恢复设计比只在内存里保留聊天上下文更完整；真实进程 kill、DB rollback、缓存竞态恢复的正确性仍需系统实验。

### 6. 双分支媒体预处理存在，时间轴粒度较粗

**实现事实**：独立有界 ASR 与 OCR 线程池并行跑两条分支。FFmpeg 音频按 60 秒切片；关键帧使用 `scene > 0.35`，另每 30 秒保底采样；相邻帧以 9×8 差分哈希 Hamming distance ≤5 去重。Tesseract 执行中文+英文 OCR。任一分支失败可保留另一条，双失败终止。合并按 60 秒窗口生成 `VideoSegment`。

证据：[VideoContextService.java:41-49,70-104,139-163,205-259,267-306,391-398](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/VideoContextService.java#L41-L398)，[SegmentedTranscriptionService.java:31-65,82-96](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/SegmentedTranscriptionService.java#L31-L96)，[OcrUtils.java:25-43](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/utils/OcrUtils.java#L25-L43)，[ThreadPoolConfig.java:14-43](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/config/ThreadPoolConfig.java#L14-L43)。

**边界**：当前所检查主链的“多模态”是语音文字+画面文字的时序融合；传给 Agent 的 JSON 包括文本和帧地址，未见把图像内容送给视觉模型进行动作/物体/情绪理解。证据定位主要为分钟窗口，不是细粒度语音对齐。

### 7. 混合检索的融合公式可直接复算

**实现事实**：每 5 分钟构建 summary + keywords + embedding，并保留原始 segments。先由 LLM 生成 semanticQuery/keywords/visualKeywords；chunk score 为 `0.60 × semantic + 0.25 × keyword + 0.15 × OCR`，选 Top 3 chunks。segment score 为 `0.55 × chunk + 0.25 × transcript + 0.20 × visual`；用户检索最多 8 条。Qdrant 的查询限定当前 mediaId；失败时降级本地余弦/子串词匹配，Embedding 失败则空向量。

证据：[VideoChunkingService.java:18,32-86](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/VideoChunkingService.java#L18-L86)，[VideoEvidenceRetrievalService.java:20-22,56-79,92-127,175-215](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/VideoEvidenceRetrievalService.java#L20-L215)，[QdrantVectorStore.java:83-102](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/QdrantVectorStore.java#L83-L102)。

**边界**：是手设权重的加权分数融合，不是 RRF、不代表学得的融合权重；未看到这些权重/TopK 的消融或 Recall@K 结果。

### 8. 检索后限制上下文，失败 Critic 可重新检索

**实现事实**：上下文目标 24,000 字符；按 relevance 排序选入，最后恢复时间顺序。Critic 的时间戳会直接在全量 context 中找附近片段；缺失要求/无支持结论/反馈合成新 query，再把新证据与旧证据合并。摘要失败时降级原文前 500 字与空 keywords。

证据：[LongVideoContextService.java:18-19,40-49,58-104,107-125](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/LongVideoContextService.java#L18-L125)，[VideoChunkingService.java:66-77](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/VideoChunkingService.java#L66-L77)。

**边界**：24k 是文本字符策略，不是精确 Token cap；第一个 segment 允许超字符预算以保证至少一段。这些选择与摘要丢失都可能影响多意图长视频召回，尚未由实验量化。

### 9. Planner → Executor → Critic 有真实闭环，但执行角色不是通用 DAG

**实现事实**：Planner 返回 1–5 个有序文本任务；Executor 用一次模型调用生成结构化整体结果；Critic 用模型审查再叠加程序规则。默认最多 2 轮，遗漏要求可 replan，证据缺口可定向刷新，普通表达问题只重写。四模式在 `ModeRegistry` 中附加不同 prompt 与 required section keys，共用同一编排器。

证据：[DeepSeekUtils.java:106-120](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/utils/DeepSeekUtils.java#L106-L120)，[AgentLoopService.java:146-158,162-218,375-421](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentLoopService.java#L146-L421)，[ModeRegistry.java:24-64](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/mode/ModeRegistry.java#L24-L64)。

**边界**：Planner 的 tasks 是提示约束下的字符串列表，不是逐任务工具执行图。第 2 轮未通过 Critic、但结构合法时会保留警告并保存结果，不能把“completed”读作“全部证据核验通过”。

### 10. 确定性校验提供了真实引用约束，但没有证明语义蕴含

**实现事实**：要求证据 timestamp 落在 segment 的 `[start,end)`，source 指明 ASR/OCR，证据 content 经去标点/符号/空白与 lowercase 后必须为相应原文子串；claim 必须与结论经同样归一后相等。程序规则可推翻模型 Critic 的 passed，要求重新绑定证据。

证据：[EvidenceVerificationService.java:12-60](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/EvidenceVerificationService.java#L12-L60)，[AgentLoopService.java:290-345](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentLoopService.java#L290-L345)。

**推断**：能阻止不在原文里的“引用”，也可能拒绝合理释义；正确抄到原文且复制了 claim，不等于原文逻辑上支持该结论。语义准确性仍依赖模型 Critic/原始 ASR/OCR 质量。

### 11. 时间/轮次/估算 Token/限流护栏存在，成本账单证明不足

**实现事实**：默认 Agent 2 轮、120 秒、50,000 估算 Tokens。`ThreadLocal` deadline 传入模型包装调用，Future 等待取模型 timeout 与剩余预算的较小值；模型失败最多 3 次尝试；JSON parse 失败另有结构化重试。用户 5/min、全局 30/min 限制提交、追问与证据检索。估算 Tokens 使用非 ASCII 字符数 + ASCII/4，成功模型响应后累加；input/output 单价及成本上限默认 0，成本 guard 默认关闭。

证据：[application.properties:27-44](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/resources/application.properties#L27-L44)，[AgentLoopService.java:453-467](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentLoopService.java#L453-L467)，[DeepSeekUtils.java:390-465](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/utils/DeepSeekUtils.java#L390-L465)，[AgentTelemetry.java:113-142,227-231](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentTelemetry.java#L113-L231)，[AnalysisDispatchService.java:29-31,114-138](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AnalysisDispatchService.java#L29-L138)。

**边界**：是按阶段检查估算消耗，未使用 provider token usage；失败尝试的收费与 ASR/Embedding 费用没有完整反映。120 秒是 Agent 阶段，媒体 context 构建有独立最长 60 分钟等待，不能写成全链路 120 秒 SLA。

### 12. 身份、归属校验与数据隔离有具体实现

**实现事实**：PBKDF2-HMAC-SHA256 210,000 iterations、16-byte salt、256-bit 派生密码；会话为 32-byte 随机 token，Redis key 使用 SHA-256 token 摘要，有效 24 小时；同账户 8 次失败/10 分钟。拦截器保护 media/analysis/admin；媒体读取、分析、追问、搜索、SSE、评分、trace 校验 DB userId 归属；admin 按角色校验。MinIO 访问生成 1 小时预签名地址，代码忽略 public-read 开关，不主动创建公开桶策略。

证据：[AuthService.java:28-38,110-165,227-241](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AuthService.java#L28-L241)，[WebConfig.java:38-41](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/config/WebConfig.java#L38-L41)，[MediaService.java:226-231](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/MediaService.java#L226-L231)，[AnalysisController.java:96-235](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/controller/AnalysisController.java#L96-L235)，[MinioUtils.java:144-152](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/utils/MinioUtils.java#L144-L152)，[MinioConfig.java:33-39](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/config/MinioConfig.java#L33-L39)。

**边界**：这些是应用层安全事实，不代表已审查生产 TLS、实际桶 ACL、会话泄露与全部授权攻击面。`yt-dlp` 首轮 DNS/网段检查明确仍有重解析/redirect 残余风险，需要网络出口管控：[YtDlpUtils.java:128-174](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/utils/YtDlpUtils.java#L128-L174)。

### 13. SSE 与 trace 提供状态可观测性，尚非性能证明

**实现事实**：SSE subscription 先登记再读取初始状态，减少完成事件落空；Redis Pub/Sub 跨实例分发，本地 fallback；终态发送后关闭流。trace 保存阶段耗时、调用次数、降级、checkpoint hits、估算 Token 与成本。状态有同步 GET 端点，客户端 stream 有重试策略。

证据：[TaskEventService.java:62-86,105-149](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/TaskEventService.java#L62-L149)，[AgentTelemetry.java:79-142](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentTelemetry.java#L79-L142)，[client/src/taskEvents.js](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/client/src/taskEvents.js)。

**边界**：Pub/Sub 没有持久化事件 replay 的保证；有采集入口不等于已经测量 p95、吞吐、可用性或上线 SLO。

### 14. 插件与追问工具调用接入真实 API，演示模式须单列

**实现事实**：Chrome Manifest V3 包含 tabs/sidePanel/storage，权限指向本机 9090；侧边栏实际调用 upload-url/route/ai/analysis-events。追问允许唯一 searchEvidence 工具，最多 3 轮，结果再进入 AgentLoop；模型请求不在白名单的工具会抛出。Web 的 `?demo` 模式使用内置 DEMO_* 数据，真实模式调用 API。

证据：[extension/manifest.json:1-16](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/extension/manifest.json#L1-L16)，[extension/sidepanel.js:374-398](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/extension/sidepanel.js#L374-L398)，[AiService.java:466-503](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AiService.java#L466-L503)，[DeepSeekUtils.java:471-503](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/utils/DeepSeekUtils.java#L471-L503)，[App.vue:518](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/client/src/App.vue#L518)。

**边界**：真实连接能力存在，浏览器商店分发/线上用户采纳未验证；演示评分不能引用为 benchmark。

### 15. 评估入口真实，但实验规模和真实性边界清楚

**实现事实**：离线 runner 使用 4 个手写任务，每个仅一段已经结构化的 VideoContext：ASR/OCR互补、纯语音、纯画面、音画冲突。runner 调真实 AgentLoop，并以结构合法 + claimEvidenceSupportRate ≥0.8 + 输出关键词覆盖 ≥0.8 判断成功。这不是原始真实视频端到端 benchmark；短单段 task 也没有真正触发长视频 chunk retrieval 路径。CI 不运行该 runner。运行时评分复用了同一子串证据校验。

证据：[golden-video-tasks.json:1-70](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/resources/evaluation/golden-video-tasks.json#L1-L70)，[OfflineAgentEvaluationRunner.java:41-82](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/OfflineAgentEvaluationRunner.java#L41-L82)，[AgentEvaluationService.java:47-87](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/main/java/com/example/server/service/AgentEvaluationService.java#L47-L87)。

**证据不足**：检查 README/docs/CI/evaluation 文件未找到可复现 Recall@K/准确率/延迟对照结果或检索融合消融。项目自己的说明也明确尚无正式实验结果，并废弃无法复现的 60 秒→50ms 叙事：[interview-qa-six-pillars.md:209,309,361](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/docs/interview-qa-six-pillars.md#L209)，[interview-qa-v2.md:476](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/docs/interview-qa-v2.md#L476)。没有测量结果时不填 0% 或虚构比较数字。

### 16. CI 与边界测试值得肯定，算法联调和维护性仍有缺口

**实现事实**：CI 的三个 jobs 分别跑 Maven tests、client npm test + build、extension node --test，另检查 shell/Compose 配置。Java 已检查测试使用 Mockito、纯领域校验或 standalone MockMvc；`rg` 在 `server/src/test` 未发现 SpringBootTest/Testcontainers。已有单测覆盖丢失合并响应、归属、旧登录 401、SSE 停止/关闭、Redis 清理失败仍释放锁、模式键、旧阶段回查等具体回归。

证据：[ci.yml:7-53](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/.github/workflows/ci.yml#L7-L53)，[VideoAnalysisConsumerTest.java:21-116](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/server/src/test/java/com/example/server/consumer/VideoAnalysisConsumerTest.java#L21-L116)，[client/src/chunkUpload.test.js](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/client/src/chunkUpload.test.js)，[client/src/taskEvents.test.js](https://github.com/Xiaoc7r/DOVideo-AI/blob/df4fa83a78a51dfa1718606bf47cf49942e1112e/client/src/taskEvents.test.js)。

**边界与推断**：已检查 14 个 Java test files 未见 AgentLoop 或 VideoEvidenceRetrieval 的直接测试文件，也未见真实 ASR/OCR/LLM/MQ/DB 集成验证；不能用 106 声明推出覆盖率。最大 Vue 文件 1,785 物理行和工作台 composable 836 行显示 UI 集中，模型包装和业务服务也各约 500 非空行；是维护复杂度信号，不是 cyclomatic complexity 实测。单次静态查找没有 TODO/stub 命中，只能说明所检查主路径没有显式占位标记。

## 本次实际执行的验证

1. `extension`: `node --test --test-reporter=dot`，退出码 0，54 个现有纯 Node tests 通过。
2. `client`: `node --test --test-reporter=spec src/api.test.js src/chunkUpload.test.js src/taskEvents.test.js`，15 tests / 15 pass / 0 fail / 0 skipped，退出码 0。
3. 其余 2 个 client test files 需要 Vue/Marked 依赖，本次没有安装；Java Maven 测试没有运行；没有声称完整 CI 通过。
4. 未运行离线 Agent evaluation；其名称“offline”不代表不调用在线模型。未消耗外部 AI、ASR、Embedding 和数据库服务。

以上是纯单元边界验证，不能替代真实视频处理、完整前端构建或服务集成验收。

## 给对比报告的公平判断

- **工程复杂度**：DoVideoAI 并非只有 UI + API prompt 包装。它有确实相连的长任务链路、分布式幂等/锁、内容复用、多个中间产物 checkpoint、多分支容错、SSE 与成本/限流护栏。这部分应该给足工程价值。
- **研究/算法含量**：源码主要实现已有 ASR、OCR、Embedding、LLM 与数据库的组合、时序检索和闭环约束。固定融合权重、分钟级 context、字符串证据校验有清楚局限；未看到受控实验支持算法效果或原创模型训练。不能把用了 5 类基础设施、Planner/Executor/Critic 角色名直接算成研究创新。
- **产品交付**：注册/登录、归属隔离、视频工作台、四种结构产物、继续追问、插件入口和可查看证据都有实际源码路径；这是完整使用闭环。开源采用量与真实生产用户收益未验证。
- **验证成熟度**：有 CI、有有针对性的回归单测，4 个黄金任务 runner 是评估起点；还缺原始长视频、多样本人工标注、检索基线/消融、真实失败恢复/负载以及 provider usage/cost 对账。
- **最终相对结论**：本笔记没有独立审计 AniVAG，因此不单方面判谁全面获胜。根报告应将 AniVAG 的检索/图结构/工具执行能力与 DoVideoAI 的异步 SaaS 编排/隔离/恢复能力分别比较，并把证据强度、范围与使用目标放到权重中。规模、星数、语言栈与作者能力不能互相替代。
