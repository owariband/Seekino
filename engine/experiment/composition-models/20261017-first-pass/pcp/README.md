# PCP 摄影构图分类模型首轮实验

2026-10-17，使用 Lee 等 2018 年 *Photographic composition classification and dominant geometric element detection for outdoor scenes* 作者公开的模型，对共享 manifest 中 17 张动画画面完成实际推理。

PICD 将该方法称为 PCPNet；这里对应原工作摄影分类部分的 FT_CNN／CaffeNet，不是点云领域同名 PCPNet。结果见 [results.json](results.json)，入口见 [run.py](run.py)。

## 官方来源与精准下载

[作者项目主页](https://mcl.korea.ac.kr/research/Submitted/jtlee_JVCIR2018/index.html)提供 [code.zip](https://mcl.korea.ac.kr/research/Submitted/jtlee_JVCIR2018/code.zip)。整个包为 2,641,821,053 bytes，本轮只读取 ZIP 目录和推理必需文件的 HTTP Range，未下载整个包或其中图片。

模型条目为 `composition classifier/photo_composition/Data/cnn/caffenet_finetune_iter_10000.caffemodel`，压缩数据区间 `36916410–248162123`，实际传输该区间 211,245,714 bytes，解压后 227,622,242 bytes。CRC32 `663777677` 与 ZIP 目录一致，SHA256 为 `1c56d9d0526aaf0b3ef6f62b20283bd27cc0c48a1c369ddb16d356608d980700`。代码包未标版本号，记录各文件 hash 与原始字节区间，见 [source.json](source.json)。

官方 C++ 程序依赖旧 Caffe。为在本机执行，使用共享环境的 OpenCV DNN 4.11.0 CPU 后端；只从原 prototxt 剥离训练输入、MemoryData、Accuracy、SoftmaxWithLoss 层并添加显式 1×3×227×227 输入。保留卷积、线性、ReLU、LRN、池化、Dropout 测试态及九类 softmax 网络。没有重新训练或转换权重数值。

## 预处理与输出依据

- `Main.cpp`：`cv::imread(..., cv::IMREAD_COLOR)`，得到 BGR。
- `ConvNet.cpp::ExtractFeature`：`cv::resize` 至 MemoryData 宽高，默认双线性；`AddMatVector` 后前向。
- `finetune_iter_10000.prototxt`：TEST MemoryData 宽高均为 227，没有 mean、scale、mirror 或 crop 配置。因此本轮 BGR、0～255 float32、直接缩放 227×227、scale 1、不减均值、不交换通道。
- `CompositionClassifier.cpp`：九类顺序为三分、垂直、水平、对角、曲线、三角、居中、对称、重复图案。
- `class_probabilities`：该公开 checkpoint 原生九分类 softmax。不是动画域校准后的置信度，不扩展解释为多标签预测。
- `feature`：4096 维 fc7 经 in-place ReLU 后、最终九类线性层之前的特征；测试态 Dropout 恒等。检索时做 L2 归一化后计算余弦。

本轮只运行摄影构图分类网络；未运行该论文各类别对应的传统几何元素检测算法。

## 本批结果

| 图片 | Top-1 类别 | 原生概率 | 观察 |
|---|---|---:|---|
| `aquarium` | 对角 | 0.8927 | 水族馆图的大斜梁与对角标签一致 |
| `bridge_diagonal` | 对角 | 0.8927 | 桥梁图也得到同类标签 |
| `building_perspective` | 对角 | 0.4114 | 同类倾向较弱，需要查看其他类别分布 |
| `school_layers` | 水平 | 0.6238 | 符合明显水平层次 |
| `interior` | 垂直 | 0.5652 | 与本轮关心的上下分区有差异，是需要复核的例子 |
| `phone_tilt` | 居中 | 0.9957 | 即使置信度很高，也不能保证对应我们关注的斜向结构 |

这些例子表明专用分类模型能直接给出可读的构图类别；也会受主体与训练域影响。完整表现需要结合统一标签、原图和检索排序判断，17 张样本不能建立模型优劣的泛化结论。

全部 17 张均成功，九维分布归一化，4096 维特征有限。CPU 六线程、batch 1、float32；每图先一次预热再三次计时。单图 pipeline 中位数为 3.3933～4.9154 ms，排除加载模型和读图。不同模型预处理与后端不同，该数字仅记录本机运行成本。

## 复现

已取得权重时，在项目根目录执行：

```bash
.cache/composition-models/.venv/bin/python \
  engine/experiment/composition-models/20261017-first-pass/pcp/run.py \
  --manifest engine/experiment/composition-models/20261017-first-pass/manifest.json \
  --cache .cache/composition-models/pcp --repeat 3
```

精准下载脚本为 [download_selected.py](download_selected.py)，依赖缓存中的 `zip_entries.json`。脚本通过 HTTP 206 取每个 ZIP entry 的压缩数据，用 raw DEFLATE 解压并校验字节数与 CRC32。缓存根为项目 `.cache/composition-models/pcp/`。
