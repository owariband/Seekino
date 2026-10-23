# CAM++ 全片段聚类与 Agent 辅助归名调研

2026-10-18。目标是评估“算法先形成匿名声音簇，再由 Agent 辅助确认人物”的路线。这里只读核对项目、官方代码和论文，没有安装依赖、执行新模型、改生产代码、写数据库、重启服务或重解析。

## 判断

这是经典的 **clustering-based speaker diarization**：把语音按声音身份分组，解决“谁在何时说话”；再把匿名簇映射到角色姓名，是额外的身份归名步骤。3D-Speaker 已提供 VAD、切段、embedding、聚类的完整流水线，因此不需要自研聚类算法。[官方流水线说明](https://github.com/modelscope/3D-Speaker/blob/main/egs/3dspeaker/speaker-diarization/README.md)

对 AniVAG 的建议是先做 **AHC 和谱聚类两条离线对照**，沿用现有 CAM++ 编码器，把 Agent 的工作改为检查簇代表音频、确认姓名和处理异常。是否接生产取决于第四集的人工核验结果；本报告不把会议/电话数据集成绩视为日语动画成绩。

## 现有代码能复用什么

本地官方源码在 `outputs/local-models/campplus/3D-Speaker/`。`speakerlab/process/cluster.py` 已实现三类后端：AHC、spectral、UMAP+HDBSCAN；`CommonClustering` 接收 `[N,C]` embedding 矩阵，因此 `[N,192]` 是可用输入形状。AHC 用 cosine 与 average-link；spectral 用 cosine 图、行裁剪、Laplacian、eigengap 与 k-means。[官方聚类代码](https://github.com/modelscope/3D-Speaker/blob/main/speakerlab/process/cluster.py)

官方 `conf/diar.yaml` 使用 CAM++、192 维、spectral，支持给定或估计人数。这证明“CAM++ 后接经典聚类”是上游已有组合。[官方配置](https://github.com/modelscope/3D-Speaker/blob/main/egs/3dspeaker/speaker-diarization/conf/diar.yaml)

不能直接照搬整个 `CommonClustering` 默认行为：少于 40 个向量时它会切换 AHC；`filter_minor_cluster` 会将小簇成员分给大簇，全部都是小簇时直接返回单簇；之后还可能按质心相似度合并。对只说一句的 NPC、小角色或有意保留未知的策略，这些默认值会产生业务影响，应逐项对照而非默认启用。[同一官方代码的 CommonClustering](https://github.com/modelscope/3D-Speaker/blob/main/speakerlab/process/cluster.py#L149)

官方快速入口默认下载的是 `iic/speech_campplus_sv_zh_en_16k-common_advanced`，不是本项目当前的 `iic/speech_campplus_sv_zh-cn_16k-common`。其切段默认是 1.5 秒窗口、0.75 秒步长，设备默认选 CUDA 或 CPU。不能把它的结果和本项目 checkpoint 混为一谈，也不能据此宣称已验证 MPS。[官方 infer_diarization.py](https://github.com/modelscope/3D-Speaker/blob/main/speakerlab/bin/infer_diarization.py)

## 算法选择

| 路线 | 算法如何工作 | 对当前项目的价值 | 成本与边界 |
|---|---|---|---|
| cosine + average-link AHC | 每段先单独成簇，反复合并最相似的两簇，达到停止距离或目标簇数时停止 | 第一条实验基线；能查看“哪两簇何时被合并”，便于人工排错 | 需要检验停止阈值；一旦早期误合并，常规 AHC 不会自行拆回 |
| 谱聚类 + eigengap | 用片段间的相似关系形成图，求图的谱表示，再分组；可从特征值间隙估计簇数 | 利用全局结构，不把“单条与某个已命名库的均值”当作全部依据 | 图构造、人数范围和短片段仍影响结果；不等于没有参数或自动正确 |
| NME-SC | 同时估计人数和相似图行裁剪参数的谱聚类变体 | 可作为第二阶段谱聚类对照，减少人工固定人数/裁剪值 | 论文的自动调参主张不意味着动画场景无需检验 |
| VBx / HMM 重分段 | 从初始簇出发，结合时间序列和说话人分布，迭代细化分配 | 若主要错误来自短暂跳簇和边界，值得后续研究 | 现有官方配方还需要匹配的 transform/LDA/PLDA，接入成本高于直接聚类 |
| UMAP + HDBSCAN / 社区发现 | 按局部密度或声音关系图形成簇，可探索少数异常点 | 可用于难例和离群点分析 | 小角色样本少、密度不均和降维会改变结果；不作为首轮唯一判定器 |

AHC 的 average-link 是两个簇之间所有点对距离的平均，并非把每簇向量平均后重新归一化的 cosine。SciPy 已有标准实现及距离矩阵接口，不需要自行实现树合并。[SciPy linkage 文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.cluster.hierarchy.linkage.html)

谱聚类用于声音分组已有明确论文先例：Wang 等在 ICASSP 2018 将声音 embedding 与非参数聚类结合。不能将该论文使用的 d-vector 编码器成绩移植为 CAM++ 成绩。[Speaker Diarization with LSTM](https://arxiv.org/abs/1710.10468)

NME-SC 论文使用归一化最大特征值间隙估计簇数和行阈值；作者仓库链接到后续 NeMo 实现，可作算法来源，而不必为一个聚类实验引入整个 NeMo。[NME-SC 论文](https://arxiv.org/abs/2003.02405)、[作者仓库](https://github.com/tango4j/Auto-Tuning-Spectral-Clustering)

`wq2012/SpectralCluster` 可直接接受 numpy 的 `[n_samples,n_features]` 并返回标签，包含谱聚类与约束变体；作者明确说明它不是 Google 产品，也不是论文使用的原始 C++ 代码。其运行依赖只有 numpy、scipy、scikit-learn，适合列为较小的现成后端候选。[作者维护的实现](https://github.com/wq2012/SpectralCluster)、[依赖](https://github.com/wq2012/SpectralCluster/blob/master/requirements.txt)

VBx 论文关注 x-vector 序列的 Bayesian HMM 聚类；官方脚本先 AHC 再 VB，读取 `--xvec-transform` 和 `--plda-file`，执行均值/LDA/归一化和 PLDA 变换。**192 维相同不能证明后端匹配**，不能把为别的 embedding 训练的后端直接套在 CAM++ 上。若没有匹配资料，首轮保留为后续选项。[VBx 论文](https://arxiv.org/abs/2012.14952)、[官方 vbhmm.py](https://github.com/BUTSpeechFIT/VBx/blob/master/VBx/vbhmm.py)

社区发现是另一种有论文依据的路线，该工作研究 Leiden、UMAP 与干净 embedding 的提取。上游的 `UmapHdbscan` 类并不等同于整篇论文的完整系统，不能把其最高报告收益直接归给一个可调用类。[Reformulating Speaker Diarization as Community Detection](https://arxiv.org/abs/2204.12112)

## 输入与 Agent 职责

推荐先在**同一 video 内全部可用语音**上分组，包括当前 `speaker=null` 的片段，暂时不把 Understanding 的人物姓名当作聚类真值。仅聚类已登记的 31 条参考无法评估整集覆盖，也保留了 Agent 先选错来源的瓶颈。这是基于本项目错标案例作出的方案判断，不是论文结论。

输入质量比替换聚类名称更先决：现有 ASR cue 可能混入两个人，跨 chunk 的 `speaker1` 也不是同一全局人物。项目 prompt 已明确这些限制；所以不能直接“每个 ASR cue 一个不可分向量”，也不能把匿名编号硬约束为同人。应保留来源区间，对疑似多人长段细分、检测转折，或者让此类段保持未分配。相关项目证据：`engine/common/transcription.py` 与 `agent/video_parse_v2/prompts.py`。

Turn-to-Diarize 提供了“先检出说话人轮次，再提 embedding 并施加聚类约束”的研究例子；其轮次检测依赖专用模型，不能把当前通用 ASR 输出直接当作该论文的可靠约束。[Turn-to-Diarize](https://arxiv.org/abs/2109.11641)

算法输出匿名 `cluster_id`、成员时间段、代表片段与异常候选。Agent 检查每簇多个分散来源，辅助确认人物名；簇内出现身份冲突时建议拆分或回查切段。**允许一个角色对应多个簇**，声音状态变化可能导致过分裂；也允许未命名簇。自动把每个簇强制映射为不同人物，会引入额外错误。这些是建议的项目职责划分，尚未实现。

角色名不宜只按当前 speaker 多数投票，因为当前错标会成为锚点；可以把已有姓名作为待验证线索，与音频、画面中的明确发声证据及来源一起展示。簇本身只证明算法的声音分组，不能凭空证明簇名。

## 本机依赖与计算边界

当前项目 `pyproject.toml` 的 speaker 可选依赖已有 numpy、scipy、torch、torchaudio。本机能提 CAM++ 向量不意味着已验证所有聚类依赖；本调研未做 import、安装或运行验证。

上游 `cluster.py` 顶层还导入 fastcluster、umap 和 hdbscan，即使只调用 spectral 也会遇到其导入要求。其整套 diarization requirements 还包含旧的 numpy/numba 固定版本及 onnxruntime-gpu 等项目，因此不能照抄为 Python 3.12 + Apple CPU 的最小环境。[官方聚类导入](https://github.com/modelscope/3D-Speaker/blob/main/speakerlab/process/cluster.py)、[整套上游 requirements](https://github.com/modelscope/3D-Speaker/blob/main/egs/3dspeaker/speaker-diarization/requirements.txt)

聚类后端通常在 CPU 上处理已提取矩阵，不要求再运行一个大语言模型。矩阵规模由片段数决定：例如 N=2000 时，192 维 float32 embedding 约 1.54 MB，一个 N×N float32 相似矩阵约 16 MB；求解过程还会分配额外空间。这是形状推算，不是 M5 Pro 性能实测。提向量耗时、线性代数耗时和 Agent 命名耗时应分别记录。

本项目当前 ASR 观测随 Understanding 提交后才有可恢复持久化来源，声纹准备在 75% Understanding 提交后开始（`scheduler.py`、`repository/video_parse.py`）。因此“固定整集聚类”需要等待全量来源，75% 时只能做部分聚类或设计后续增量；这是当前宿主取数时序，不是聚类算法必须等 VLM。离线实验可以先用已完成第四集，避免现在扩大调度改造。

## 建议的下一步实验

1. 固定同一版本第四集来源，提取全部候选片段的 embedding，保留输入区间、有效语音时长、PCM hash、模型/revision；切段和剔除规则在评分前固定。
2. 同一矩阵分别跑 average-link AHC 和谱聚类，记录人数、簇成员、参数与随机种子；不按经典爽世片段的分数反复选参数。
3. 给每簇生成代表片段及疑点试听清单，再由人/Agent 对匿名簇命名；单独核验两条已确认错标、短应答、相邻换人、不同场景和 NPC。
4. 区分“匿名分组正确率”和“角色姓名正确率”。有足够人工区间标注时再计算 DER；少量标注时报告可核验的同人/异人对和未决率，不能把与旧标签一致率称作准确率。

首轮不需要训练 CAM++、向 ES 建向量索引、引入新服务，或同时接 VBx/多编码器。本报告只提出实验路线，未给生产修改树，也未执行上述实验。

## 聚类以外的对照

| 方案 | 官方依据与用途 | 当前边界 |
|---|---|---|
| pyannote Community-1 完整流水线 | 官方说明默认 CPU、可离线使用，提供普通与 exclusive diarization，后者便于对齐 ASR 时间。[官方模型卡](https://huggingface.co/pyannote/speaker-diarization-community-1) | 是独立的整套系统对照，不只是替换 CAM++ 后的聚类函数；首次获取模型需接受条件。第四集准确率、M5 耗时未实测。 |
| MSDD 多尺度神经解码 | 通过多尺度表示和动态尺度权重处理时间分辨率与声音表示质量的矛盾。[Multi-scale Speaker Diarization with Dynamic Scale Weighting](https://arxiv.org/abs/2203.15974) | 是需模型支持的后处理，不是把一个人物的参考分数再求平均；不能只套公式替代。 |
| Nemotron-3-Diarization / NeMo-Speech.cpp | 2026-09-23 发布的 100M 参数模型，支持至多 8 名匿名说话人；官方卡提供原生 C++ 入口。[官方模型卡](https://huggingface.co/nvidia/Nemotron-3-Diarization)；其原生运行时列有 macOS、CPU 和 Metal 构建路径。[官方 C++ 仓库](https://github.com/NVIDIA/NeMo-Speech.cpp) | Python NeMo 卡主要面向 NVIDIA GPU/Linux，与原生 C++ 支持范围分开理解。M5 实际编译、速度、日语动画表现均未实测；8 人限制需要核对路人/唱歌等真实输入。 |

## 研究范围与落盘

本次仅新增本文件。项目现有实验目录被 Git 忽略；本地记录与是否提交分别处理，没有强制加入 Git。引用上游 `main/master` 为 2026-10-18 阅读状态，后续复现实验应固定依赖和提交版本。
