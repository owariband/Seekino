# 检测框／主线图转视觉 embedding：论文依据与待测方案

日期：2026-10-17。状态：仅核查论文并记录方案；本页没有新增模型下载、推理或检索实验。

## 结论

- 把所有检测框按原位置画进同一张空白图，再编码整张图，是有论文先例的表示方法。线框能呈现多个元素的位置、比例与重叠；不能逐框裁剪后丢掉相对位置。直接依据是 LayoutGAN 的线框渲染判别器。[LayoutGAN §3.3.2](https://arxiv.org/html/1901.06767v1#S3.SS3.SSS2)
- 布局图送视觉网络做检索也有先例：LayoutGMN 的 U-Net baseline 输入多通道语义布局图，使用三元组训练。但它不支持“任意冻结视觉模型已经能可靠比较构图”的结论。[LayoutGMN §5.1](https://arxiv.org/html/2012.06547v2#S5.SS1)
- 冻结预训练模型对线稿计算特征也有依据：CLIPasso 用冻结 CLIP 约束线稿，且专门利用中间层保留几何信息。它验证的是图片到草图的优化，不是多框构图检索。[CLIPasso 官方说明](https://clipasso.github.io/clipasso/)
- 因此可以先用已有本地 DINOv2 Small 做免训练基线；是否需要训练由实测决定。这里的 DINOv2 与负责检测的 Grounding DINO 是两个不同模型；本地 384 维输出见[首轮实验](README.md)。本页没有证明该向量在框图／线图上符合构图相似度。

## 一手论文具体做了什么

| 论文 | 表示与比较／学习方式 | 是否训练 | 与当前任务的距离 |
|---|---|---|---|
| [LayoutGAN，ICLR 2019](https://arxiv.org/html/1901.06767v1) | N 个元素的几何参数 → W×H×M 多类别通道线框图 → 3 层卷积和全连接判别真假；各通道用 max 聚合元素响应。 | 训练生成器和判别器。 | 直接支持“多框画成图让 CNN 看布局”；没有给出可直接部署的构图检索 embedding 或两图距离。 |
| [LayoutGMN，CVPR 2021](https://arxiv.org/html/2012.06547v2) | U-Net baseline 对多通道语义布局图做三元组学习和检索。主方法将框构成图，跨图交互后输出两份 1024 维表示，以 L2 距离比较，使用 margin triplet loss。 | 两条路线均做任务训练；训练三元组由布局 IoU 生成弱标签。 | 主方法在 UI／户型图上验证；其向量依赖配对对象，不能直接当作一图一次编码的 ANN 向量。U-Net baseline 说明栅格检索可做，但在该文检索实验中弱于主方法。 |
| [CLIPasso，SIGGRAPH 2022](https://clipasso.github.io/clipasso/) | Bézier 笔画 → 可微栅格图；草图和原图进入 CLIP，用特征损失优化笔画。几何约束依赖 CLIP 中间层。 | CLIP 冻结；优化的是每张图的笔画控制点，无需草图数据集训练。 | 支持冻结视觉特征参与线稿几何比较；不能推出最终全局 CLIP／DINOv2 向量已适合本项目的构图排序。 |

### 可引用原文定位

- LayoutGAN：§3.3.2，公式 (3) 和该节最后一段给出多通道栅格化与 CNN 结构。§1 说明填充图会遮住重叠的小元素，线框保留这些元素的可见边界。短摘录：“The layer output is a multi-channel image”。[全文](https://arxiv.org/html/1901.06767v1#S3.SS3.SSS2)
- LayoutGMN：§5.1 的 U-Net 段明确输入和训练；§3.3／Fig. 5 给出成对图表示、L2 距离和 margin loss；§5.2／Table 1 给出检索结果。短摘录：“The input to the network is a multi-channel image with semantic segmentations.” [全文](https://arxiv.org/html/2012.06547v2#S5.SS1)
- CLIPasso：官网 “How does it work?” 最后三段，说明中间层约束几何、控制点更新以及冻结部分。短摘录：“use the intermediate layers of a pretrained CLIP model”。[官方页](https://clipasso.github.io/clipasso/)

## 本地下一步方案：尚未执行

1. 分别制作两种全画面结构图：全部保留框的线框图、主线段图。统一背景、线宽、画布比例和缩放方法；框图去掉原图纹理、标签文字、置信度文字。先分别编码，便于判断是谁贡献了相似度。
2. 使用已有 DINOv2 Small，冻结参数，保留全画面缩放／补边后提取末层 CLS，得到 384 维向量；做 L2 归一化，再计算点积，即余弦相似度。查询与库图使用同一套提取与渲染流程。
3. 先检查同图轻微框抖动／线宽变化是否稳定，再看同类构图跨场景能否靠近、同场景不同构图能否分开。它是新的输入域，现有原图 embedding 结果不能充当该路线的实验结果。
4. 如果冻结特征主要响应线条密度、矩形数量或绘制风格，再评估中间层／空间特征，或用构图正负对训练小投影头。是否训练、使用哪种层、怎样融合 PCP，均未由上述论文替我们确定。

注意：Grounding DINO 的轴对齐框本身不包含斜梁边缘方向；线图补充的是框图尚未表达的方向结构。主线提取质量仍会影响结果，栅格化和 embedding 不会自动修复漏检或杂线。
