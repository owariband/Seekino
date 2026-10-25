为了统一管理项目，我们需要整理对seekino的整体架构规划和拆分、介绍

# 整体逻辑
原因在于镜头识别是服务于剧本的，目前我们做的event识别，本质上是一种剧本识别（我不知道为什么自己一直没有想到这一点）

然后分镜、构图都是服务于剧本的
因此在剧本识别的同时做分镜识别，这个才是我们的目的
因此我们相对于普通的召回片段，多了一个根据召回的片段进行AGI生成的复杂度


因为拆解这个还能为大家提供一种视频产品制作思路，同时能做“最高光十大名场面推荐”，在问答助手那一块儿


# 架构

seekino的项目架构可以分为以下几层：



# 重点问题
> 10.24 update：更新TODO的重构方案，需要大抽血；对项目的业务能力定位、具体工程技术实现都需要精进
> 10.25 update：思考视频生成需要收敛方向，研究论文从视频生成角度结构解析后的script schema

当前项目选型和视野存在巨大问题
1. chat bot从业务视角已经过时，目前agent均一个sandbox，如果ban掉LLM自身的代码能力、展示能力、剪视频能力和建模能力，我们的项目支撑范围太窄；TODO：增加agent sandbox能力，支持agent自己写代码并可运行、渲染到前端展示成果；Goal：agent能够根据召回片段剪出视频（动漫等娱乐作品）、做出html PPT（MIT课程）
2. 解析层面存在问题，大的工程骨架可以复用；然而对于策略层面，本该复现[OmniScript](https://arxiv.org/html/2604.11102v1) 的算法逻辑（而且是今年4月份的新论文）就能轻松解决；TODO：研究论文，解读论文复现论文算法为工程体系；Goal：去掉user自导入library的prereq，并且refine解析的准确度、耗时和成本
> 回顾问题，我当时已经尝试让AI去寻找相关论文，但是很显然我自己没有把这个项目建模清晰。如果清晰地收敛为video2script和video2shot，可以把scope缩小的非常清晰精准定位到这两篇文章
> 我进一步反思的话，很可能原因在于：自己同时冷启动产品、策略和后端，对一个项目的思考边界非常混乱；产品的核心价值定位，我没有思考清楚和清晰就先做了；如何从0开始搭建起一个系统的后端，也是我所要摸索着做的东西；策略层面RAG流程、解析的定义等等层面从0开始摸也是不清晰混乱
> 现在想来这就是基础差喜欢速成的弊端。动得快，但是栽跟头疼
> 同时意识到，自己在和AI交互层面的习惯、沟通方式因为疏忽，已经影响到自己在现实和别人沟通的清晰度、明确度问题
> 写了再多trd，也是体系混乱，一个bullet会出现不相干的语句的
3. 视频制作层面：出于agent参考refer视频进行剪辑（我们上传视频为系列的好处即可以复用为素材库）的需求；我们需要对解析结果的script严格保证其具有参考价值，agent可以通过script进行refer并剪辑，提供方向性的同时不是抄袭某一个特定的视频；TODO：研究论文，解读论文生产video的体系；Goal：剧本解析调整为对视频生成有参考价值的格式，引入视频生成简单workflow



# referrence

核查与更新：2026-10-24 首次整理解析与镜头研究；2026-10-25 增补视频生成与自动剪辑 Agent 研究。以下记录论文的方法、输入条件、公开资产与对 Seekino 的适用判断；论文结果不代表已在我们的动画素材上验证。

## 1. 解析目标与当前 schema 的覆盖情况

目标是从成片音画重建可核对的剧本，同时关联分镜：谁在何时说了什么、发生了什么、人物状态怎样变化，以及这些内容通过哪些镜头呈现。
成片中没有出现的原作者设定、未拍摄情节和创作意图，不能仅凭视频可靠还原。

需要区分三种边界：剪辑产生的 **Shot 镜头边界**、多个镜头组成的 **Scene 剧情场景**、对白或动作自身的 **Event 事件区间**。一句对白可以跨镜头，一组正反打也可以始终属于同一场景。

对照 [当前 Parse schema](../../engine/parsev2/schemas.py)，下表综合本节收录的镜头、视听剧本、人物、情绪、关系与叙事研究，并纳入剧本组织和名场面推荐目标。
“直接研究”表示已有对应任务的方法；“部分覆盖”表示只解决其中的子任务。数据集标注、模型方法和输出示例分别注明，具体输入条件及公开资产见后文。

| 我们需要的能力 | 已查论文的覆盖与方法依据 | 仍需补充或验证 |
|---|---|---|
| 镜头起止、硬切、渐变转场 | 直接研究：[TransNetV2](https://arxiv.org/abs/2008.04838)、[AutoShot](https://arxiv.org/abs/2304.06116) 检测镜头边界 | 动画素材验证、阈值和时间边界校准 |
| 景别、角度、构图、镜头运动 | 直接研究：[SGNet](https://arxiv.org/abs/2008.03548)、[ShotBench](https://arxiv.org/abs/2506.21356)、[CameraBench](https://arxiv.org/abs/2504.15376) 覆盖不同镜头属性 | 统一标签子集，处理不确定结果，验证动画精度 |
| 多个镜头组成一个剧情场景 | 直接研究：[ShotCoL](https://arxiv.org/abs/2104.13537) 检测语义场景边界；[ScreenWriter](https://arxiv.org/html/2410.19809v1) 分场景组织剧本；[OmniScript](https://arxiv.org/html/2604.11102v1) 输出场景—事件层级 | 统一各论文的场景定义，结合对白、人物与地点；保留跨镜头事件 |
| 跨镜头认出同一个角色、确定名字 | 已有方法：[ScreenWriter](https://arxiv.org/html/2410.19809v1) 匹配声音 ID 与角色名；[OmniScript](https://arxiv.org/html/2604.11102v1) 研究角色一致性；[MovieNet](https://arxiv.org/abs/2007.10937) 提供身份标注 | ScreenWriter 依赖演员脸图和角色名库；动画、变装、未知人物与命名证据仍需验证 |
| 台词转写与准确时间戳 | 直接研究：[WhisperX](https://arxiv.org/abs/2303.00747) 转写与时间对齐；[OmniScript](https://arxiv.org/html/2604.11102v1) 生成带时间定位的对白事件 | 区分词级对齐与事件级时间戳，验证多语言、噪声、漏词和字幕冲突 |
| 对白翻译与多语言版本对应 | 本轮尚未核实完整方案；已有转写和剧本研究不能直接证明原文—译文逐句对应可靠 | 翻译、语言识别、原文与译文关联，以及 `text_variants` 一致性 |
| 谁说了哪句话、画外音、多人重叠 | 已覆盖多个子任务：[TalkNet](https://arxiv.org/abs/2107.06592) 检测画面中的活跃说话人；[ScreenWriter](https://arxiv.org/html/2410.19809v1) 做角色命名归属；[OmniScript](https://arxiv.org/html/2604.11102v1) 生成含角色的对白与旁白 | 仍需验证画外音、重叠说话和动画口型下的联合归属精度，结合说话人分段、声纹和 Refine 校验 |
| 动作、人物互动与视觉叙述 | 直接研究：[OmniScript](https://arxiv.org/html/2604.11102v1) 生成动作事件；[LIReC](https://arxiv.org/abs/2003.13158) 预测交互；[AutoAD II](https://arxiv.org/abs/2310.06838) 描述视觉行动；[MovieGraphs](https://arxiv.org/abs/1712.06761) 提供结构化标注 | 关联人物与时间，检查遗漏和幻觉；AD 的对白间隙描述不覆盖全部动作 |
| 人物情绪、表情与状态 | 直接研究：[EmoTx](https://arxiv.org/abs/2304.05634) 预测人物及场景情绪；[OmniScript](https://arxiv.org/html/2604.11102v1) 输出表情信息；[MovieGraphs](https://arxiv.org/abs/1712.06761) 提供情绪和属性标注 | 区分可见表情与推断情绪，验证动画特征、上下文和变化起止 |
| 人物关系与关系变化 | 部分覆盖：[LIReC](https://arxiv.org/abs/2003.13158) 预测关系；[MovieGraphs](https://arxiv.org/abs/1712.06761) 标注关系与交互；[OmniScript](https://arxiv.org/html/2604.11102v1) 包含角色关系推理 | 关系状态已有依据；`RELATION_CHANGE` 仍需前后状态、触发事件、变化时间与证据 |
| 剧本组织、长片剧情与叙事结构 | 直接研究：[OmniScript](https://arxiv.org/html/2604.11102v1) 做 V2S；[ScreenWriter](https://arxiv.org/html/2410.19809v1)、[Integrating Video and Text](https://aclanthology.org/2025.ijcnlp-long.129/) 组织剧本并生成摘要；[TRIPOD](https://aclanthology.org/D19-1180/) 识别叙事转折点 | 区分成片输入和已有文本输入；后两项分别依赖带名字的转写、梗概及剧本，另需验证跨段一致性 |
| 高光与名场面推荐 | 部分覆盖：[TRIPOD](https://aclanthology.org/D19-1180/) 提供剧情转折点方法；[OmniScript 官方示例](https://github.com/TencentARC/OmniScript) 包含 `high_points` 输出格式 | 叙事高潮和输出格式不等于偏好排序；需定义推荐标准、候选覆盖、去重和证据验证 |
| 字幕、标题卡、其他画面文字 | 本轮研究尚未给出完整 OCR、文字分类与时序去重方案；使用字幕辅助理解不等于覆盖画面文字解析 | 保留 OCR 流程，区分对白字幕与其他文字，并验证跨帧去重和时间定位 |
| 纠错、版本一致性、断点恢复、索引 | 属于我们的工程层 | 继续使用现有系统机制并统一新输出 |

## 2. 镜头与分镜研究

| 论文与原始来源 | 实际解决什么 | 我们可以借用什么 |
|---|---|---|
| [TransNetV2，2020](https://arxiv.org/abs/2008.04838)；[官方代码与模型](https://github.com/soCzech/TransNetV2) | 硬切和渐变转场检测 | 镜头起止候选；不直接确定剧情场景或对白起止 |
| [AutoShot，CVPR Workshops 2023](https://arxiv.org/abs/2304.06116) | 面向短视频的镜头边界检测，提供 SHOT 数据集 | 与 TransNetV2 对照，验证复杂切镜；基准成绩不能直接外推到动画 |
| [SGNet / MovieShots，ECCV 2020](https://arxiv.org/abs/2008.03548)；[作者项目页](https://movienet.github.io/projects/eccv20shot.html) | 将主体和背景分别建模，分类景别及运动类型 | 分镜字段与标注定义；需映射到我们选择的标签子集 |
| [ShotBench / ShotQA / ShotVL，2025](https://arxiv.org/abs/2506.21356)；[作者项目页](https://vchitect.github.io/ShotBench-project/) | 八个维度的电影镜头语言理解、QA 数据与专用模型 | 景别、取景、角度、构图、运动等标签体系；不是完整剧本模型 |
| [CameraBench / Towards Understanding Camera Motions in Any Video，2025](https://arxiv.org/abs/2504.15376)；[作者项目页](https://linzhiqiu.github.io/papers/camerabench/) | 定义和识别镜头运动，区分相机运动与主体运动 | 镜头内部运动区间；区分变焦、相机位移和跟随主体 |
| [ShotCoL，CVPR 2021](https://arxiv.org/abs/2104.13537) | 学习镜头表示，用于语义场景边界检测 | 将多个镜头组织成剧情场景；仍需人物、对白和剧情上下文 |

需要校正精度预期：ShotVL-3B 在论文表 3 的八项任务中平均准确率为 65.1%，镜头运动一项为 51.7%。这是该基准上的分类表现，不能解释为我们的完整解析正确率，也不能直接推算动画效果。[ShotBench 实验原文](https://arxiv.org/html/2506.21356v2)

音频与说话人还可参考 [WhisperX，2023](https://arxiv.org/abs/2303.00747) 的转写时间对齐，以及 [TalkNet，2021](https://arxiv.org/abs/2107.06592) 的视听活跃说话人检测。前者不自动解决角色命名，后者主要判断画面中的人是否正在说话，不能独立覆盖画外音和全部对白归属。

## 3. Schema 可以借鉴什么，仍需定义什么

这些论文提供不同任务的标注体系，没有共同规定一个覆盖全部解析功能的统一 schema。

| 可以直接借鉴文献的部分 | 我们仍需定义的部分 |
|---|---|
| 近景、中景、俯拍、平移、变焦等术语与标注规则 | 标签子集，以及不同论文之间的标签映射 |
| 镜头边界、转场区间、语义场景 | 镜头怎样关联多个对白和动作，事件怎样跨镜头 |
| 人物、交互、情绪和关系的表示方法 | 同一角色跨镜头的稳定身份、名字、别名与未知值 |
| 训练样本、方法和评测指标 | 哪个字段来自测量、哪个来自模型推断，如何引用证据 |
| 单项任务的输出约定 | 时间单位、版本、冲突、纠错、恢复与索引映射 |

当前 `SCENE` 是稀疏时间线中的事件类别，尚不是“包含多个 Shot 的剧情场景实体”。`participants` 与 `visible_persons` 分别表示语义参与者和画面可见者，应继续区分；不能把镜头中出现的人直接当作说话人。
现有 `start_seconds` / `end_seconds` 虽沿用旧名称，实际采用绝对整数毫秒和左闭右开区间；借用论文结构时必须保持时间契约清晰。[当前实现](../../engine/parsev2/schemas.py)

## 4. 直接的成片转剧本研究

### 4.1 OmniScript：Video-to-Script 的整体任务定义

[OmniScript: Towards Audio-Visual Script Generation for Long-Form Cinematic Video，2026](https://arxiv.org/abs/2604.11102) 直接研究从成片音画生成按场景、事件组织的剧本，包含人物、对白、动作、表情与声音线索。

- **输入条件：** 任务定义不要求用户提供已有剧本或演员表。
- **方法参考：** 分层输出、角色一致性、事件语义与时间边界的分别评价；角色记忆与逐步命名还用于其训练数据标注流水线。
- **长视频条件：** 基础设置面向约 5 分钟以内片段，更长视频采用上下文扩展或剧情分段后生成并合并。[方法与评测全文](https://arxiv.org/html/2604.11102v1)
- **公开状态：** [官方仓库](https://github.com/TencentARC/OmniScript) 明确说明代码与模型权重仍在内部开源审核；虽然 README 列出推理命令，当前尚无完整对应脚本。已有 Demo/API 入口，不能据此认定可下载权重自托管。
- **对我们的用途：** 最贴近整体剧本任务，可先借鉴输出与验收设计。README 的 `high_points` 格式也与名场面推荐有关，但输出格式本身不证明排名可靠。

### 4.2 ScreenWriter：模块化重建剧本

[ScreenWriter: Automatic Screenplay Generation and Movie Summarisation，2024](https://arxiv.org/abs/2410.19809) 从成片生成对白、说话人名字、场景分隔和视觉描述，再形成全片摘要。

- **输入条件：** 不要求已有完整剧本，但角色命名额外依赖演员脸图与角色名数据库，论文以 IMDb 为来源。
- **处理思路：** 转写与说话人分段 → 关键帧特征分场景 → 全片角色命名匹配 → 视觉描述与剧本组织 → 分层摘要。
- **关键假设：** 说话角色至少在同一场景某处出现。持续画外音和动画角色会使其人脸命名方案需要适配。[第 3 节方法](https://arxiv.org/html/2410.19809v1)
- **公开状态：** 第 7 节称补充材料包含代码；本次未确认可直接复现的官方仓库及完整依赖组合。
- **对我们的用途：** 参考转写、人物身份、场景组织与摘要的职责划分；摘要质量仍需与逐句归属、逐事件时间定位分别验收。

### 4.3 Integrating Video and Text：带额外输入条件的剧本中间表示

[Integrating Video and Text: A Balanced Approach to Multimodal Summary Generation and Evaluation，IJCNLP-AACL 2025](https://aclanthology.org/2025.ijcnlp-long.129/) 将视觉描述与对白整合为类似剧本的中间表示，再生成长视频摘要。
其 transcript 输入已包含说话人名字，视觉片段还根据对白停顿筛选；适合借鉴摘要组织和降低处理量的方法，不能直接替代未知说话人识别或完整 Parse。[方法原文](https://aclanthology.org/2025.ijcnlp-long.129.pdf)；[作者代码](https://github.com/galannp/integrating_video_and_text)（未运行验证）。

## 5. 人物、情绪、关系与叙事的补充研究

| 论文与来源 | 研究内容和输入条件 | 对我们有用的部分与限制 |
|---|---|---|
| [MovieGraphs，CVPR 2018](https://arxiv.org/abs/1712.06761)；[官方标注与加载代码](https://github.com/katha-ai/MovieGraphs-Dataset-CVPR2018) | 为电影片段标注人物、属性、情绪、关系、交互、话题、原因及时间；研究图检索、交互排序与原因预测 | 借鉴 `participants`、`ACTION`、`EMOTION` 和关系证据的关联。它主要是标注数据集与部分任务研究，不是完整视频转图谱模型 |
| [EmoTx / How You Feelin'，CVPR 2023](https://arxiv.org/abs/2304.05634)；[官方代码与权重](https://github.com/katha-ai/EmoTx-CVPR2023) | 融合视频、人物面部特征和对白，预测场景级、人物级多标签情绪 | 参考 `EMOTION` 的上下文建模。人物轨迹命名使用已有 MovieGraphs 标注，不能当作从零发现角色名字的方法 |
| [LIReC / Learning Interactions and Relationships between Movie Characters，CVPR 2020](https://arxiv.org/abs/2003.13158)；[官方实现](https://github.com/Annusha/LIReC) | 使用裁好的交互片段、视觉、对白和人物轨迹，联合预测交互、关系及涉及人物 | 参考动作与关系联合建模；关系分类不直接定位“何时由朋友变成敌人”。发布入口已核对，未下载运行 |
| [AutoAD II，ICCV 2023](https://arxiv.org/abs/2310.06838)；[作者项目页](https://www.robots.ox.ac.uk/~vgg/research/autoad/v2.html) | 输入视觉特征、演员/角色资料及语音区间，在对白间隙生成面向视障观众的视觉描述 | 参考角色条件化的动作与场景描述；不覆盖完整对白剧本，演员脸图路线也不能原样用于动画 |
| [MovieNet，ECCV 2020](https://arxiv.org/abs/2007.10937)；[官方数据说明](https://github.com/movienet/movienet-tools/blob/master/docs/wiki/data.md) | 提供人物身份、场景边界、动作/地点、电影风格等标注，以及收集的剧本、字幕和元数据 | 帮助划分联合解析任务与设计测试集；收集到剧本文本不代表已经从裸视频自动恢复剧本 |
| [Movie Plot Analysis via Turning Point Identification / TRIPOD，EMNLP-IJCNLP 2019](https://aclanthology.org/D19-1180/) | 输入已有剧情梗概和剧本文本，识别转折点并投射到剧本场景 | 可接在我们生成的剧本之后，分析剧情推进与高潮；不是视频到完整剧情的解析器，也不能把高潮直接等同于用户偏好的名场面 |

**关系状态与关系变化必须分开。** MovieGraphs、LIReC 的关系标签能提供状态参考；我们的 `RELATION_CHANGE` 还需要前后状态、变化事件和对应证据。EmoTx 的情绪预测也不能直接当成精确情绪变化时间线。

**AutoAD 发布状态需区分版本。** [AutoAD II 子目录](https://github.com/TengdaHan/AutoAD/tree/main/autoad_ii) 仍标注代码和模型待开源；[AutoAD III 子目录](https://github.com/TengdaHan/AutoAD/tree/main/autoad_iii) 有权重和输出示例，但训练/测试脚本仍标为待发布。主仓库存在不等于完整流程可直接部署。

## 6. 视频生成与自动剪辑 Agent：从叙事记忆到可执行剧本

2026-10-25 增补。这里需要区分两类“生成”：**素材重剪**，即选择、重排和修剪已有镜头；**新内容生成**，即额外生成图像、视频、旁白、音乐或音效。当前优先验证前者，后者作为素材不足时的补镜头能力。

### 6.1 直接研究素材重剪与自动成片的工作

| 工作 | 方法与输入条件 | 公开资产与对 Seekino 的适用判断 |
|---|---|---|
| [CineAgents，2026](https://arxiv.org/abs/2604.10456) | 输入用户指令和原视频。通过 Script Reverse-Engineering 建立 Shot、Event、Story、Character 分层叙事记忆，再以 Director／Orchestrator 迭代叙事规划，形成包含具体 Shot ID 的 Compiled Script，最后调用配乐、文字、封面和转场工具。每次生成当前 Shot 摘要时，以前序 10 个 Shot 为历史上下文，减少孤立召回造成的剧情断裂 | **与当前目标最直接对应。** 可借鉴“原片叙事记忆→创意蓝图→可执行剪辑剧本”的完整链路。CineBench 论文称将发布，包含 500+ 指令—成片对及不可能指令；本次未核实到可直接部署的官方代码。其角色锚定使用 Wikipedia 等外部资料，不能原样替代我们的无预导入 Library 冷启动 |
| [CutClaw，2026](https://arxiv.org/abs/2603.29664)；[代码入口](https://github.com/GVCLab/CutClaw) | 输入数小时原始素材、目标音乐和用户指令。先做多模态层级拆解，再由 Playwriter 以音乐结构为时间锚规划叙事，Editor／Reviewer 进行细粒度选段与审美、语义复核 | 适合补充 `music_anchor`、节奏、视觉审美和审核约束。说明剪辑剧本不能只记录内容顺序，还应记录音乐与情绪节奏。代码入口已确认，尚未运行 |
| [EditDuet，SIGGRAPH 2025](https://arxiv.org/html/2509.10761v1)；[项目页](https://mudtriangle.com/editduet) | 输入视频集合、固定 A-roll 和自然语言目标。Editor 使用搜索、添加、删除、交换、移动等 NLE 工具修改时间线，Critic 根据用户目标反馈，直到调用渲染器 | 适合设计可迭代的 `timeline` 和 `critic_feedback`：剪辑剧本不是一次性输出，而是多轮修改对象。项目页已确认，完整开源实现未在本次核实 |
| [LAVE，IUI 2024](https://arxiv.org/html/2402.10294v1) | 为素材自动生成标题和视觉叙述，支持素材概览、创意脑暴、语义检索、Storyboard 和 Clip Trimming，并允许用户继续手动调整 | 适合参考“解析描述→检索→故事板→修剪”的轻量链路，以及 Agent 与人工共编的关系。其重点是交互式编辑，不提供我们所需的完整层级 Schema |

### 6.2 生成式分镜、角色一致性与多模态合成

| 工作 | 可以借用的结构 | 使用限制与适用判断 |
|---|---|---|
| [MovieAgent，2025](https://arxiv.org/html/2503.07314v1)；[官方代码](https://github.com/showlab/MovieAgent) | Director 将梗概拆成子剧本，Scene Plan 补充人物、情节、情绪和视觉风格，Shot Plan 再生成景别、运镜、灯光、字幕等镜头参数，之后调用图像、视频和音频模型 | 适合补全 `generation_ref`。其输入需要角色照片与音频组成的 Character Bank，属于我们想去掉的前置依赖，不能直接照搬；代码与模型准备说明已公开，未运行 |
| [VideoDirectorGPT，2023](https://arxiv.org/html/2309.15091v2) | Video Plan 包含多 Scene 描述、实体及二维框、背景，以及实体跨 Scene 的一致性分组，再由 Layout2Vid 执行布局控制 | 适合表达生成镜头的 `layout`、参考实体和 `consistency_groups`；它解决的是新视频生成，不负责已有素材检索与剪辑 |
| [VideoGen-of-Thought，2024／2025](https://arxiv.org/html/2412.02259v3) | 将一句话先变成 Shot 草稿，再扩展为角色动态、背景连续性、关系变化、运镜和 HDR 灯光五类规格，并进行自我校验、身份保持和转场处理 | 适合细化生成型 Shot Spec 和连续性问题。不能把生成规格误当成原片解析时必然可观察的事实 |
| [MM-StoryAgent，2025](https://arxiv.org/abs/2503.05242) | 通过多阶段写作、提示词修订和多 Agent 协作，组合故事文本、角色一致图像、旁白、音乐与音效；不同模态可以替换生成模块 | 与 HTML PPT、讲解视频和故事板合成较接近，说明最终剧本应同时管理视觉、旁白、音乐与音效，而不只是一个视频生成 Prompt |

### 6.3 我们应维护两份不同的结构

现有解析结果与 Agent 的创作计划不能合并成一份不可区分的 `video.json`。

| 结构 | 回答的问题 | 建议包含的内容 |
|---|---|---|
| `NarrativeMemory`：原片叙事记忆 | 原片实际有什么？ | `source_meta`、稳定 `character_id`、`shots`、`events`、`stories`、`relationships`、`high_points`，以及时间、证据、置信度和模型版本。它是可复核的证据层，不随创作意图随意改写 |
| `CompiledScript`：新片可执行剪辑剧本 | Agent 准备怎样组成新视频？ | `brief`、`blueprint`、`timeline`，以及可选 `generation_ref`、`style_reference` 和 `review`。它是可迭代的创作层，允许多个版本 |

`CompiledScript.timeline[]` 至少需要同时保存**源时间和输出时间**：

```text
index
narrative_role              // hook／setup／development／turn／climax／payoff
timeline_start_ms           // 在新片中的开始时间
duration_ms                 // 在新片中持续的时长
source_ref {
  video_id,
  shot_id,
  in_ms,                    // 在原片中的入点
  out_ms                    // 在原片中的出点
}
selection_reason
evidence_event_ids
transition
audio_policy
subtitle／overlay
alternatives[]
```

`brief` 记录目标、受众、风格、总时长、画幅、语言和素材限制；`blueprint` 记录每个叙事阶段的目的、摘要、目标时长和必要证据。只有素材不足时，才追加 `generation_ref`，其中包括 Prompt、负面 Prompt、角色与资产引用、景别、角度、运镜、灯光和运动要求，避免把“原片事实”与“生成指令”混写。

`review` 至少保存 `version／review_status／critic_feedback／approved`。`NarrativeMemory` 的解析置信度与 `CompiledScript` 的选段、叙事和审美评分也必须分开，不能把检测器分数直接当成“剪得好不好”的置信度。

### 6.4 参考剧本对创作的价值与当前判断

原片剧本除了支持召回，还可以提供**剪辑风格先验**：平均 Shot 时长、切镜密度、景别比例、对白与反应镜头的交替、高潮前后的节奏变化、高光间隔，以及音乐与画面转折的对齐方式。Agent 可以复用叙事结构和节奏，替换具体 Shot 引用，而不是简单复制某个视频。

2026 年的 CineAgents 与 CutClaw 表明，“视频理解→结构化叙事记忆→自动剪辑与再创作”正在形成一条新兴研究链路。我们的机会也不只是再做一个解析器，而是打通：

```text
Source Video
  → NarrativeMemory
  → Creative Blueprint
  → CompiledScript
  → Editor／Renderer
```

建议阅读顺序：

1. **CineAgents §4.2～4.4**：先看叙事记忆、迭代规划和 Compiled Script 如何落到具体 Shot ID。
2. **CutClaw §3**：补充音乐锚点、Editor／Reviewer 和细粒度选段。
3. **EditDuet §3.1～3.3**：看时间线动作和 Critic 多轮修改。
4. **MovieAgent §3.2＋VideoDirectorGPT §3.1**：只在需要生成新镜头时补景别、运镜、布局和角色一致性。

工程上应先验证 `CompiledScript` 是否足以驱动剪辑与 HTML PPT，再决定是否引入图像／视频生成模型。论文年份新不等于实现已经成熟，代码、数据、角色库依赖和动画效果仍需分别核查。

## 7. 对 Seekino 的采用建议与阅读顺序

建议把结果组织为三个相互关联的层次；这是结构设计建议，本次尚未修改生产 schema。

| 层次 | 主要内容 | 服务于什么 |
|---|---|---|
| 剧本与事件 | 对白、动作、情绪、关系变化、剧情场景 | 理解发生了什么，以及人物怎样参与 |
| 分镜 | 镜头边界、转场、景别、角度、构图、运动 | 理解剧情怎样通过画面表达，并提供视觉证据 |
| 叙事与推荐 | 场景摘要、角色发展、转折点、高光候选 | 问答、名场面推荐、剪辑与视频制作分析 |

现有 ASR、OCR、声纹、Refine、校验和恢复机制仍有独立职责。优先补充镜头与剧情之间的关联，保留推断来源、未知值和冲突，而不是用切镜结果替代全部事件分段。

推荐阅读顺序：

1. **OmniScript 第 3、4、5 节**：先学 V2S 输出定义、评测和长视频分段。
2. **ScreenWriter 第 3 节**：看模块分工、角色命名假设与场景级组织。
3. **MovieGraphs 标注定义**：整理人物、交互、情绪、关系和原因的最小表示。
4. **TransNetV2 + ShotBench**：补上“在哪里切”和“镜头怎样表达”；细化运动时再读 CameraBench。
5. 按失败案例补读 **EmoTx / LIReC / AutoAD**；叙事和高光阶段再深入 TRIPOD。

尚需实测的内容：动画与多语言适配、画外音和重叠对白、角色跨段一致性、事件遗漏与幻觉、关系变化证据、时间边界误差以及推荐相关性。
当前方向允许远端 GPU 服务；部署选型应根据每小时视频处理时间、显存峰值、调用成本和返工率实测决定。本次只整理资料，没有下载、训练或部署模型，不能从论文规模直接承诺硬件配置、时延和成本。
