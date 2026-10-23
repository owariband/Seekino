# 构图模型首轮实测：17 张动画画面

日期：2026-10-17。模型已真实下载并完成本地推理；本页记录结果，不是待执行计划。

直接打开 [交互结果页](report.html)，可切换查询图和特征表示，查看原生输出、PCP 类别条形图、SAMP／MUSIQ 评分、Grounding DINO 框、LSD 线段及各模型 Top-5。也可以先看 [样本联系表](contact_sheet.jpg)、[水族馆检索对比](retrieval_aquarium.jpg)、[大厅检索对比](retrieval_interior.jpg)、[单侧人物检索对比](retrieval_stage_left.jpg)。

## 当前结论

**本批最值得继续检查的是 PCP 的分类输出与 fc7 特征，以及保留全画面的 CLIP；尚不能选定一个能稳定代表构图思想的模型。**

- PCP 提供明确可读的构图类别。水族馆斜梁和桥栏都判为 `diagonal`（各约 0.893），学校横向楼层判为 `horizontal`（0.624）。但大厅判为 `vertical`（0.565），倾斜手机、倾斜人像和左侧人物近景常判为 `center`。这些类别可以共存；原有九类单选标签与我们的关注点并不完全一致，高 softmax 值也不保证命中产品要求。
- 在 10 组探索性三元组中，PCP fc7 特征有 8 组把构图正例排在负例之前，CLIP 保留全画面为 7 组。**8/10 指这十组成对排序，不是构图分类准确率或全图库召回率。** PCP fc7 对水族馆的全图库 Top-1 仍是学校，说明不能凭该数字宣布检索已解决。
- 水族馆全图库 Top-1，PCP 类别分布、DINOv2 全画面和 MUSIQ 特征均返回斜桥栏，而 CLIP 全画面返回店面。不同任务、取层和查询会改变结论，没有覆盖本批所有情形的赢家。
- CLIP 的中心裁剪与保留全画面，主三元组分别为 4/10 和 7/10。这说明输入预处理本身值得控制；它不是“全画面预处理在所有图片上都更好”的证据。
- SAMP 的构图质量分和 MUSIQ 的美学分能够输出，但本轮中间特征没有稳定排除内容干扰。质量分数接近不能证明构图类别接近。
- Grounding DINO 提供对象框，LSD 提供局部直线。二者均没有直接输出本轮摄影构图类别；LSD 会受重复栏杆和背景边缘影响，舞台全景甚至没有满足固定长度阈值的线段。

## 样本和参考的范围

[manifest.json](manifest.json) 固定 17 张来自同一动画作品不同场景的图片，保存来源路径／时间、尺寸和 SHA256。已有截图按最长边不超过 960 缩放；新抽帧宽 960，保存完整画面。没有去字幕、手工修主体或做内容裁剪。字幕、色调、场景和画风仍是混杂因素。

[reference.json](reference.json) 包含助手目视参考、多标签说明、10 组主对比及 3 组边界对比。参考类别包括斜向、水平分层、中心主体、主体左／右侧空域、框中框、群体平衡、透视等，是本地产品粗类，不是 PICD 原始 24 类。

候选视觉观察先于学习模型的检索排名；参考文件和三元组在早期 SAMP／LSD 特征抽取后落盘，其余模型继续推理。因此本轮是探索实验，**不声称盲测、专家人工真值或预注册评测**。同作品、相关三元组和不完全匹配的语义负例，限制了结果的推广范围。手机／人像倾斜与全图对角布局的边界单独列出，不并入主十组。

主比较始终使用单模型原始表示的 L2 归一化余弦，没有训练分类器／度量，没有调几何融合权重，没有根据这十组选择阈值。

## 实际模型、原生输出与成本

| 模型 | 下载权重体积 | 本轮原生输出 | 用于比较的表示 | 执行方式与单图中位耗时 |
|---|---:|---|---|---|
| PCP／FT_CNN 摄影模型 | 217.1 MiB | 9 类 softmax | 9 维类别分布；4096 维 fc7 | OpenCV DNN CPU：4.17 ms |
| SAMP-Net | 174.9 MiB | 1～5 分构图质量分布、属性和模式重要性 | 1024 维融合特征；512 维评分前特征 | PyTorch CPU：12.81 ms |
| CLIP ViT-B/32 | 337.6 MiB | 图像向量 | 512 维图像投影；两种输入处理 | MPS：默认 7.65／全图 6.67 ms |
| DINOv2 Small | 84.2 MiB | 图像向量 | 384 维末层 CLS；两种输入处理 | MPS：默认 7.91／全图 6.97 ms |
| MUSIQ AVA | 103.6 MiB | 1～10 分美学分布 | 384 维评分头前 CLS | MPS：38.04 ms |
| Grounding DINO Tiny（参照） | 约 658 MiB，含配置 | 对象标签、框、置信度 | 不参与类别向量胜负统计 | MPS：487.35 ms |
| OpenCV LSD（参照） | 无权重 | 线段端点／角度／长度 | 最长 12 条合格线的 12 维方向直方图 | CPU 单线程：15.54 ms |

这些是不同后端、不同输入流程的本机记录，不能直接横向宣称速度优劣。PCP／SAMP 每图一次预热后三次取中位；CLIP／DINOv2／MUSIQ／Grounding DINO 每变体一次预热后每图单次，计时包含预处理、前向、同步和输出复制，不含文件读取、模型加载和绘图。LSD 包含灰度转换及检测，不含绘图。

运行环境：macOS 26.4.1 arm64、Python 3.12.13、torch 2.8.0、torchvision 0.23.0、transformers 4.57.6、pyiqa 0.1.15.post2、OpenCV 4.11.0、NumPy 1.26.4。PyTorch 线程数 6，float32、batch 1、eval。GPU 推理显式使用 MPS，禁隐式 CPU fallback。完整安装版本见 [requirements.lock.txt](requirements.lock.txt)。进程 RSS 和 MPS driver 占用仅记录运行后采样，不是每个模型的独立峰值，也不可相加。

## 排序比较结果

对每个三元组比较 `sim(query, 构图正例) > sim(query, 内容较近但构图不同的负例)`；自匹配排除。该正例与负例仍基于助手的构图解释，应查看每项原图和 margin。

| 表示 | 主三元组胜／平／负／不可用 | 3 组边界例胜出 | 全图库 Top-3 与参考多标签重合均值 |
|---|---:|---:|---:|
| PCP fc7 | 8／0／2／0 | 1 | 25.5% |
| CLIP 全画面 | 7／0／3／0 | 1 | 23.5% |
| SAMP 融合特征 | 5／0／5／0 | 2 | 19.6% |
| SAMP 评分前特征 | 5／0／5／0 | 1 | 11.8% |
| PCP 类别分布 | 4／0／6／0 | 2 | 19.6% |
| CLIP 默认中心裁剪 | 4／0／6／0 | 0 | 17.6% |
| DINOv2 默认中心裁剪 | 4／0／6／0 | 0 | 17.6% |
| DINOv2 全画面 | 4／0／6／0 | 1 | 29.4% |
| MUSIQ AVA 特征 | 4／0／6／0 | 1 | 17.6% |
| LSD 方向直方图 | 4／3／2／1 | 1 | 22.9%，16 个有效查询 |

Top-3 重合只是粗参考诊断，其他模型有效查询数 17；多标签之间并非互斥、部分标签只有一个样本，不能当成标准分类准确率。DINOv2 全画面在该指标上最高，PCP fc7 在十组对比上最高，二者不同也说明“小样本单一分数”不足以回答谁最懂构图。

LSD 的 `stage_wide` 没有合格线，明确保留不可用状态，禁止伪造 Top-K。涉及零向量的三元组不计有效胜负；其他方向直方图并列明确标平。余弦差在 `1e-8` 内为平局，图库中并列按 manifest 顺序显示，显示顺序不代表优先级。

## 如何看输出

以水族馆为例，PCP 原生读数是 `diagonal=0.8927`；SAMP 构图质量均分约 `2.815/5`；MUSIQ 美学均分约 `4.975/10`；Grounding DINO 得到 8 个对象框，包括两个人和一个梁框。它们回答的问题不同，不能把这些数值统一解释成“构图相似度”。

CLIP、DINOv2 和评分模型的中间向量不是可直接阅读的线段或类别。结果页用同一查询的 Top-5 展示这些向量究竟保留了什么信息。原始向量完整保存在各模型 JSON，不只保存了降维后的图。

- [PCP 原始输出](pcp/results.json)及[模型／预处理依据](pcp/README.md)。
- [SAMP 原始输出](samp/results.json)及[模型／取层依据](samp/README.md)。
- [CLIP 全画面](backbones/clip__fullframe.json)、[CLIP 默认](backbones/clip__native.json)。
- [DINOv2 全画面](backbones/dinov2__fullframe.json)、[DINOv2 默认](backbones/dinov2__native.json)。
- [MUSIQ AVA 原始输出](backbones/musiq_ava.json)。
- [Grounding DINO 原始输出](backbones/grounding_dino.json)、[LSD 原始输出](lines/results.json)。
- [统一比较与每组三元组分差](comparison.json)。

## 权重来源和复现

固定权重和 SHA256 记录在 [models.json](models.json)、[SAMP source.json](samp/source.json)、[PCP source.json](pcp/source.json)。缓存为项目根 `.cache/composition-models/`，不使用临时目录。只有准备步骤联网下载，所有本地推理入口禁止隐式下载。

- CLIP 使用 [OpenAI 官方 ViT-B/32 发布权重](https://github.com/openai/CLIP)，文件 SHA256 与官方 URL 中的校验值一致。
- DINOv2 使用 [facebook/dinov2-small](https://huggingface.co/facebook/dinov2-small)，revision `ed25f3a31f01632728cabb09d1542f84ab7b0056`。
- MUSIQ 使用 [IQA-PyTorch](https://github.com/chaofengc/IQA-PyTorch) 的 AVA 权重。它是 PyTorch 复现及权重转换路径，不冒充 Google 原始 TensorFlow checkpoint；也不是 VBench 常用的 SPAQ 权重。
- SAMP 使用 [CADB 官方仓库](https://github.com/bcmi/Image-Composition-Assessment-Dataset-CADB)及 README 中的预训练权重。
- PCP 使用[原作者发布页](https://mcl.korea.ac.kr/research/Submitted/jtlee_JVCIR2018/index.html)里的摄影分类器。通过 HTTP Range 只取约 211 MB 压缩权重和小文件，未下载完整 2.64 GB 包；不使用同名点云 PCPNet。
- Grounding DINO 使用 [IDEA-Research/grounding-dino-tiny](https://huggingface.co/IDEA-Research/grounding-dino-tiny)，revision `a2bb814dd30d776dcf7e30523b00659f4f141c71`。

从项目根执行本地推理／分析：

```bash
.cache/composition-models/.venv/bin/python engine/experiment/composition-models/20261017-first-pass/run_backbones.py --device mps
.cache/composition-models/.venv/bin/python engine/experiment/composition-models/20261017-first-pass/analyze.py
.cache/composition-models/.venv/bin/python engine/experiment/composition-models/20261017-first-pass/render.py
```

PCP、SAMP 独立运行参数见各自 README；LSD 入口为 [lines/run.py](lines/run.py)。MPS 需在能访问本机 Metal 的执行环境运行，受限沙箱会把可用 GPU 报为不可用。离线查看报告不需要服务、API 或外部资源。

本轮范围仅实验脚本、样本、缓存与结果文件；没有修改生产检索流程，没有训练／微调，没有接入豆包 embedding 或其他远程模型。后续应先让用户复核标签和失败案例，再扩大独立作品／场景的测试集；本轮不能替代 PICD／CADB 标准评测。

## 文件范围与校验

全部实验文件均为本轮新建，原有其他实验与工作区改动保持不变。完整新增文件树见 [artifacts-tree.txt](artifacts-tree.txt)，输入／输出／向量／脚本校验见 [validation.json](validation.json)，页面资源及交互检查见 [viewer_validation.json](viewer_validation.json)。新目录尚未跟踪，普通 `git diff --stat` 不包含它；本轮没有 commit、push、迁移或重启服务。
