# SAMP-Net 官方权重首轮实验

2026-10-17，实际运行共享 manifest 中全部 17 张动画画面。结果在 [results.json](results.json)，独立入口为 [run.py](run.py)。

本模型原任务是**构图质量评分**。这轮导出评分和中间特征，检验这些表示能否帮助检索相同构图；没有把八个池化模式的重要性解释成构图类别概率，也没有将质量分数接近视为构图相同。

## 来源与运行设置

- 代码：[CADB／SAMP-Net 官方仓库](https://github.com/bcmi/Image-Composition-Assessment-Dataset-CADB)，commit `b8f80be379d4a4caf9d045db3187cb60bcaca583`。
- 权重：官方 README 提供的 Dropbox `samp_net.pth`，183,416,305 bytes，SHA256 `d3d02f36967734fd6f0d2770380a04b777b5cd800a9ba68d4ccc9656f6fef603`。
- 本地缓存：项目根 `.cache/composition-models/samp/`。模型 45,832,256 参数，完整 `strict=True` 加载。
- 共享隔离环境：`.cache/composition-models/.venv/bin/python`；Python 3.12.13、torch 2.8.0、torchvision 0.23.0、NumPy 1.26.4、Pillow 13.0.0、einops 0.8.2、OpenCV 4.11.0。
- CPU、float32、6 个 torch 线程、batch 1、`eval()`、`inference_mode()`。每张图先进行一次预热，再测三次。
- 遵循官方输入流程：图像缩放到 224×224 后进行 ImageNet 归一化；显著图使用官方 spectral residual 算法从原始 RGB 图计算，不需要额外显著性模型。

旧官方代码存在两个运行兼容问题。入口在内存中忽略 `Config` 对训练数据集绝对路径的断言；使用 `resnet18(weights=None)` 构造等价骨干，代替旧版位置参数 `resnet18(pretrained)`。官方仓库文件保持原样，模型参数及前向计算流程不变。运行设置离线环境变量，骨干不会隐式下载 ImageNet 权重。

## 输出如何解读

| 字段 | 实际含义 | 用途 |
|---|---|---|
| `score_distribution_1_to_5` | 1～5 分构图质量的五维预测分布 | 展示原始模型输出 |
| `mean_composition_quality` | 五档分数按预测分布加权后的均分 | 质量评分，不能直接判断构图类别 |
| `feature` | 1024 维 `com_pred_layer` 输入，位于 AAFF 属性／构图特征融合后 | 本轮主检索表示，比较前做 L2 归一化 |
| `penultimate_feature` | 512 维 `com_pred_layer[4]` ReLU 输出，紧邻最终五分档线性层之前 | 辅助检索表示，便于后续比较取层位置 |
| `pattern_importance_weights` | 八种池化模式对质量评价的相对重要性 | 可解释辅助量，不是类别置信度 |
| `attribute_predictions` | 三分、平衡、景深、主体、对称、重复等辅助属性的回归输出 | 保留原始输出；未标定成类别概率 |

17 张均成功推理，分布均归一化，两个特征均为有限值。图像间质量均分为 1.6614～3.0703；单图 pipeline 三次测量的中位数为 11.7415～14.8931 ms，17 个中位数的中位数为 12.8101 ms。计时包含预加载 PIL 图的预处理、显著图、前向与特征复制，排除读图和加载模型；单次运行的样本量不支持泛化性能结论。

主特征余弦检索的一些原始观察：水族馆图最接近建筑透视、教室群像、斜向桥梁；大厅图最接近高视角房间、排练左侧构图、商店广角。需要对照原图和人工构图标注判断，不能仅凭余弦高分宣称“理解构图”。统一对比和评测由上层实验完成。

## 复现

在项目根目录运行：

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 \
.cache/composition-models/.venv/bin/python \
  engine/experiment/composition-models/20261017-first-pass/samp/run.py \
  --manifest engine/experiment/composition-models/20261017-first-pass/manifest.json \
  --repo .cache/composition-models/samp/repo \
  --weights .cache/composition-models/samp/samp_net.pth \
  --device cpu --repeat 3
```

## PCP 公开权重的补充核查

[2018 年摄影构图论文的作者主页](https://mcl.korea.ac.kr/research/Submitted/jtlee_JVCIR2018/index.html)提供 `code.zip`。HTTP 返回大小为 2,641,821,053 bytes；本轮没有下载整个包，仅用 HTTP Range 检查 ZIP 目录。目录内确认存在：

```text
composition classifier/photo_composition/Data/cnn/caffenet_finetune_iter_10000.caffemodel
composition classifier/photo_composition/Data/cnn/finetune_iter_10000.prototxt
```

初步核查后，本轮已进一步按字节区间取出所需权重，并通过 OpenCV DNN 完成全部 17 张推理，见 [PCP 实验记录](../pcp/README.md)。无需安装完整 Caffe，也无需下载整个代码包。本轮使用的是该摄影模型，未拿同名点云 PCPNet 替代。
