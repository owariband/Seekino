# 第四集：聚类选源 → VoiceprintAgent 实验

最终运行状态：`coverage_incomplete`；本地 ready=`False`；15 条登记、5 人；耗时 149.47 秒。
本轮未达到11人名单严格过半的6人门槛；15条仅为未发布的诊断草稿，不能作为正式声库使用。即使本地ready通过，也只代表覆盖和提交检查，不代表身份已由人工核验。未写入生产。

## 固定选源

使用此前第四集 V9 的完整24个chunk ASR快照及WavLM-Large SV兼容实现已算好的256维向量。226条 fit，K8/seed1000；cosine距离半径0.40、至少2个独立邻居，得到151条常规候选、75条待核。每组先取cue均衡medoid，再逐轮max-min，优先不同chunk；每组展示至多3条、最小新增距离0.05，合计18条。这是展示预算，不是人物登记上限。75条待核本轮未交给Agent；C02/C06为空，不降低门槛补数。

两句目标及其关联完整cue、重复PCM和重叠来源均未参与选源或登记。人物背景从当前生产Series只读导出，包含视觉姓名映射，不含两句目标答案；它不是历史V9背景快照。

## 接线与运行边界

真实调用现有VoiceprintAgent.InvokeVoiceprint、Canvas、标准登记/撤销/完成工具和Gateway。MCP关闭；新增实验工具只允许查看当前候选的3张原视频帧。模型不能听音频，WavLM负责声学计算。登记仅接收宿主签发的source_ref及精确缓存区间，不接收模型自行提供的向量或路径。WavLM256维登记保存在本地registry.json；正式192维schema、Server、数据库、队列均未修改。

完成要求11人名单中至少6人有参考，并为少于目标6条的人提供原因；没有启用未经校准的WavLM相似度门禁。

## 两条目标结果

主指标逐条均值 = mean(cos(query, ref))；次指标质心分 = cos(query, mean(unit_refs))。两种口径分开报告，不根据结果切换。混合对照保留Agent新增人物，同时只为缺失的原六人物沿用旧参考。

| 目标 | 声库 | 正确人物均值/名次 | 均值最高人物 | 正确人物质心/名次 | 质心最高人物 |
|---|---|---:|---|---:|---|
| v11_02：千早爱音 | old_v11_excluding_known_errors | 0.723932 / 1 | 千早爱音 | 0.830705 / 1 | 千早爱音 |
| v11_02：千早爱音 | previous_exploratory_selection | 0.647584 / 1 | 千早爱音 | 0.706987 / 1 | 千早爱音 |
| v11_02：千早爱音 | agent_only | 0.605230 / 1 | 千早爱音 | 0.769891 / 1 | 千早爱音 |
| v11_02：千早爱音 | agent_plus_missing_old | 0.605230 / 1 | 千早爱音 | 0.769891 / 1 | 千早爱音 |
| v11_27：长崎爽世 | old_v11_excluding_known_errors | 0.242440 / 5 | 要乐奈 | 0.366890 / 4 | 要乐奈 |
| v11_27：长崎爽世 | previous_exploratory_selection | 0.529157 / 2 | 要乐奈 | 0.593479 / 1 | 长崎爽世 |
| v11_27：长崎爽世 | agent_only | 0.406597 / 1 | 长崎爽世 | 0.539832 / 1 | 长崎爽世 |
| v11_27：长崎爽世 | agent_plus_missing_old | 0.406597 / 2 | 要乐奈 | 0.539832 / 1 | 长崎爽世 |

旧参考补入人物：丰川祥子、要乐奈

全人物逐条分数、名次与正确人物相对最高竞争者的差值见 [evaluation.json](evaluation.json)。

## 本轮效果判断

爱音目标在旧WavLM参考库本来就第一，本轮保持第一，不能称新增修复。爽世目标在仅5人的草稿库排第一；加入缺失的祥子、乐奈旧参考并保留海铃这一新竞争者后，均值为0.406597，乐奈0.529823，爽世仍第二；质心爽世0.539832、乐奈0.529823，仅相差0.010009。这个差距没有校准为可靠接受条件。

上一轮按近邻支持排序的探索选源，爽世均值0.529157、质心0.593479，均高于本轮。上一轮有3条爽世参考、本轮有6条，姓名核验方式也不同，因此不是等预算算法消融；至少当前结果不支持‘多样性采样加更多参考必然更好’。不能只与较差旧库比较后宣布新方法更优。

身份核验还发现疑点：985100–987100毫秒的原null候选（K8:C03典型片）被登记为千早爱音，依据却写‘爱音正面张口说出让爱音站中间的台词’。实际台词是在称呼爱音，多人画面也不能只凭一个张嘴者确定声音。这是需要试听/连续画面人工核验的疑点，尚未确认正确发声人；本轮未修改其标签或按目标分数删除它。具体依据写得更长不等于身份可靠。

本轮结论：候选生成与真实Agent调用已接通；人物覆盖和声库身份质量验收均未通过。后续优先独立核验典型片身份，再测试每人物多个可信原型；保留候选间不同身份和未知，不把整簇当作人物。75条待核来源可用于补覆盖，但需要独立试验，不能本轮为了ready临时降低筛选标准。

## 候选与登记来源

[打开可试听报告](report.html)。弱标签及Agent登记都不能当作人工真值。

| 片段 | 分组/角色 | 毫秒区间 | 原弱标签 | 最终登记 | 台词 |
|---|---|---:|---|---|---|
| [asr_c00_i007_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c00_i007_p00.wav) | K8:C01/medoid | 35400–37900 | 千早爱音 | 千早爱音 | 別に灯ちゃんのせいじゃないと思うけど |
| [asr_c20_i000_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c20_i000_p00.wav) | K8:C01/diversity | 1200000–1201600 | 长崎爽世 | 长崎爽世 | これかなりむずいんだよね |
| [asr_c11_i015_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c11_i015_p00.wav) | K8:C01/diversity | 694600–696700 | 千早爱音 | 千早爱音 | ちょ、ちょちょちょ、一生だよ、一生。 |
| [asr_c16_i012_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c16_i012_p00.wav) | K8:C03/medoid | 985100–987100 | null | 千早爱音 | あのちゃん、真ん中立って。 |
| [asr_c00_i005_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c00_i005_p00.wav) | K8:C03/diversity | 26100–27700 | 高松灯 | 高松灯 | 私のせいだから |
| [asr_c09_i000_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c09_i000_p00.wav) | K8:C03/diversity | 540000–542950 | 高松灯 | 高松灯 | 私が ライブうまく歌えなかったから。だから、さきちゃんが来なくなった。 |
| [asr_c10_i003_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c10_i003_p00.wav) | K8:C04/medoid | 608700–611100 | 长崎爽世 | 长崎爽世 | 立希ちゃん、燈ちゃんのことが大好きなんだから |
| [asr_c08_i012_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c08_i012_p00.wav) | K8:C04/diversity | 529500–531300 | 长崎爽世 | 未登记 | 何か理由があったんだと思う |
| [asr_c03_i002_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c03_i002_p00.wav) | K8:C04/diversity | 191300–194300 | 长崎爽世 | 长崎爽世 | ねえ、どうしてだろう |
| [asr_c20_i005_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c20_i005_p00.wav) | K8:C05/medoid | 1211900–1214400 | 长崎爽世 | 长崎爽世 | とりあえず できそうな曲探してみよか |
| [asr_c10_i010_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c10_i010_p00.wav) | K8:C05/diversity | 647600–650600 | 长崎爽世 | 长崎爽世 | そうだよ、今度はみんなでちゃんと話し合っていくの |
| [asr_c16_i016_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c16_i016_p00.wav) | K8:C05/diversity | 996500–998150 | 长崎爽世 | 长崎爽世 | いったんライブに向けてほかのことも相談しない。 |
| [asr_c14_i017_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c14_i017_p00.wav) | K8:C07/medoid | 886300–889100 | 八幡海铃 | 八幡海铃 | では 元サヤですか。よかったですね。 |
| [asr_c22_i009_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c22_i009_p00.wav) | K8:C07/diversity | 1375100–1377700 | まな | 未登记 | あ、猫のドーナツあったよ。ういちゃんは？ |
| [asr_c14_i003_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c14_i003_p00.wav) | K8:C07/diversity | 851700–853200 | 八幡海铃 | 八幡海铃 | 中身ぶちまけたいんですか。 |
| [asr_c09_i018_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c09_i018_p00.wav) | K8:C08/medoid | 597000–600000 | 椎名立希 | 椎名立希 | 私はケンカより ともりが練習に来ないことのほうが堪えたから。 |
| [asr_c19_i003_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c19_i003_p00.wav) | K8:C08/diversity | 1149200–1150900 | 椎名立希 | 椎名立希 | 高校羽丘なんだ |
| [asr_c18_i007_p00](../20261018-mygo-ep04-wavlm-kmeans/audio/asr_c18_i007_p00.wav) | K8:C08/diversity | 1136600–1138300 | 千早爱音 | 未登记 | ともりも、バンドも戻りたかったんだ。 |

## 审计与成本

工具调用：`{'get_speaker_list': 1, 'get_context_canvas': 7, 'inspect_candidate_frames': 22, 'register_voiceprint': 15, 'finish_voiceprint_preparation': 1}`；22次看帧请求、18个不同来源。
最终轮由gpt-5.6-sol完成，没有触发备用模型；声纹计算复用此前CPU WavLM向量，本轮没有重新运行编码器。
用量采用Gateway记录的SDK线程累计口径，包含多轮输入和缓存重复计数，不是单个上下文长度。见 [usage.json](usage.json)。

| 执行 | 秒 | 累计输入 tokens | 其中缓存 | 输出 tokens | 状态 |
|---|---:|---:|---:|---:|---|
| superseded-run-01 | 78.34 | 557427 | 449664 | 2708 | complete |
| superseded-run-02 | 54.66 | 451819 | 359552 | 2867 | coverage_incomplete |
| 20261018-mygo-ep04-voiceprint-agent | 149.47 | 1315582 | 1174272 | 4795 | coverage_incomplete |

前两次宿主结果保留：`superseded-run-01/`把弱标签重复放入members，Agent错误地称为独立佐证；`superseded-run-02/`移除重复后，因为宿主遗漏生产人物背景而零登记。最终补齐已有背景，仍不复制members人物身份。它们是接线纠正记录，不是算法对照组。18条候选及几何参数始终不变，目标分数在最终运行结束后才计算。

启动失败记录在startup-failure-01/02：未加载项目环境、UV缓存权限、Codex状态库沙箱限制。最终按项目环境启动，使用正常权限访问Codex自有状态库。模型名额从20改65，是匹配Gateway每次工具回调预留一个名额的计数方式；64工具/900秒时限不变。

## 复算与范围

```sh
# 从仓库根目录执行；已存在的正式运行产物不会被自动覆盖
.venv/bin/python engine/experiment/speaker-clustering/20261018-mygo-ep04-voiceprint-agent/report.py
```

prepare.py可重复复算固定候选；run_agent.py --check用真实Agent/工具、替代模型进行本地检查并实际抽帧。真实模型命令与代码SHA见run-manifest.json，完整工具参数/返回见trace.jsonl。

本次新增三个实验脚本及本目录产物，并同步原voiceprint-trd.md的实现状态和结果。无生产代码/API/数据库schema/配置/前端改动，无服务重启、迁移、整集重解析、commit或push。只读导出一份现有Series背景。engine/experiment与design目录均被Git忽略；这些是本地维护成果，不在提交中。

```text
AniVAG/
├── agent/video_parse_v2/design/
│   └── [M] voiceprint-trd.md  # 更新实验状态、门禁及效果边界
└── engine/experiment/speaker-clustering/20261018-mygo-ep04-voiceprint-agent/
    ├── [A] prepare.py       # 179行：冻结参数、选源、来源校验
    ├── [A] run_agent.py     # 384行：现有Agent与本地宿主、真实工具和自检
    ├── [A] report.py        # 离线评分与可试听报告生成
    ├── [A] protocol.json / candidates.json / selection.json / background.json
    ├── [A] registry.json / trace.jsonl / status.json / result.json / usage.json
    ├── [A] run-manifest.json / run.log / evaluation.json / verification.json
    ├── [A] report.md / report.html
    └── startup-failure-*/、superseded-run-*/  # 保留诊断与旧执行快照
```

没有新增测试文件；本地自检与独立复算的范围：真实Agent/工具驱动、真实帧提取、未读/未看/非精确区间拒绝、登记幂等、完成结果落盘回读、人物字段不重复、三轮18条候选一致、逐条分数与排名复核、HTML本地资源链接有效。没有运行生产租约/恢复/全量解析测试，也未证明全部参考身份正确。其他并行工作区改动未纳入本次范围。
