# Grounding DINO Tiny：MyGO 第一集本地检测与性能实验

- 日期：2026-10-16。
- 状态：完成。MPS 与 CPU 均真实执行，六张图片各重复三次；每个设备另有两次预热。
- 结论：本机可以运行。MPS 检测流程中位耗时 **784.81 ms/张**，CPU 为 **1999.66 ms/张**。六张样本目视观察到 19 个真实人物，18 个 `person` 框分别覆盖其中 18 人；物件类别和复杂空间关系仍需验证。
- 范围：这是六张特意覆盖不同难点的探索性样本，不是随机测试集，也不是全片准确率评测。没有 GT 框或 IoU 标注，不报告标准 recall、precision 或 AP。这里的目视核对由助手完成，并非人工专家标注；清单中的 `manual_*` 是助手在查看检测结果前记录的可见目标预期。

## 1. 产物

- [六张标注图总览](./annotated_contact_sheet.jpg)：红框为人物，蓝框为其他类别，数字为模型匹配分数。
- [原图及目视预期清单](./manifest.json)：含视频路径、时间、图片 SHA-256、可见人数和样本难点。
- [MPS 完整结果](./mps/results.json)、[CPU 完整结果](./cpu/results.json)：检测框、分数、每次计时和内存数据。
- [逐图视觉审核](./visual_review.json)。助手目视可见目标预期在查看模型输出之前建立，之后逐框核对原图和检测结果。
- [实验脚本](./run.py)、[依赖版本](./requirements.txt)、[硬件记录](./hardware.json)。

![六张标注结果](./annotated_contact_sheet.jpg)

## 2. 模型、输入与运行口径

| 项目 | 本轮设置 |
| --- | --- |
| 硬件 | MacBook Pro，Apple M5 Pro，48 GB 统一内存 |
| 系统 | macOS 26.4.1，ARM64 |
| 模型 | `IDEA-Research/grounding-dino-tiny`，未微调 |
| 模型 revision | `a2bb814dd30d776dcf7e30523b00659f4f141c71` |
| 实际参数量 | 172,249,090 |
| 权重文件 | FP32 `model.safetensors`，689,359,096 字节 |
| 软件 | Python 3.12.13，PyTorch 2.14.1，Transformers 4.57.6 |
| 推理精度、批量 | FP32，batch=1 |
| 线程数 | PyTorch intra-op CPU threads=6；两个设备均使用此设置 |
| 原始输入 | 第一集六张 1920×1080 JPG；没有裁剪、去字幕或增亮 |
| 模型输入 | 默认 shortest_edge=800 / longest_edge=1333；实际张量均为 `[1,3,750,1333]` |
| 文字提示 | `person . window . door . chair . table . guitar . telescope .` |
| 阈值 | box threshold=0.35，text threshold=0.25；所有样本固定一致 |
| 后处理 | 官方 grounded object detection 后处理；没有额外 NMS 或人工过滤 |
| 算子路径 | `disable_custom_kernels=True`，使用 PyTorch 路径 |
| MPS 回退 | `PYTORCH_ENABLE_MPS_FALLBACK=0`，没有启用不支持算子的自动 CPU 回退 |

原视频为 `assets/example_video_mygo/[Nekomoe kissaten][BanG Dream! It’s MyGO!!!!!][01][1080p][JPSC].mp4`，时长约 1430.12 秒。先检查 12 个候选时刻，再追加 6 个候选以补足俯视和室外小人物，最终选 6 张。样本均为叙事画面，未选 OP/ED。

模型与依赖放在 `/tmp/anivag-grounding-mygo-20261016/` 的隔离目录。推理设置 `HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`，没有把画面发送给云端模型。项目主虚拟环境、依赖声明和业务代码未修改。

## 3. 性能结果

CPU 与 MPS 顺序执行，没有同时运行两个检测任务。每个设备先对第一张图预热两次，再按六张图的顺序执行三轮，共 18 次正式测量。

**检测流程计时**：已经加载的 PIL 图片 → 预处理 → 设备传输 → 模型前向 → GPU 同步（MPS）→ 官方后处理 → 检测框和分数转到 CPU。排除模型加载、Python 导入、JPEG 读取、视频解码和绘图保存。

| 指标 | Apple GPU / MPS | CPU / 6 threads |
| --- | ---: | ---: |
| 模型前向中位耗时 | 736.84 ms | 1973.84 ms |
| 检测流程中位耗时 | **784.81 ms** | **1999.66 ms** |
| 检测流程范围 | 445.00–925.25 ms | 1782.78–2557.64 ms |
| 累计推理时间换算吞吐 | 1.30 张/秒 | 0.50 张/秒 |
| 模型加载与设备迁移 | 1.74 秒 | 0.35 秒 |
| 第一次预热 | 5.90 秒 | 1.99 秒 |
| 第二次预热 | 0.50 秒 | 1.80 秒 |
| 进程峰值 RSS | 0.63 GiB | 2.86 GiB |
| MPS 驱动已分配内存观测最大值 | 3.25 GiB | 不适用 |

内存口径：进程峰值 RSS 来自 macOS `getrusage`；另每 20 ms 采样一次进程 RSS。MPS 数值是在每次前向结束并同步后读取 `driver_allocated_memory()` 的最大观测值，包含驱动分配和缓存，并非完整连续监控得到的精确峰值。**MPS 驱动分配和进程 RSS 不能直接相加作为总内存，也不能把两个设备的 RSS 单独视为同口径的全部推理内存。**

以上为当前桌面环境、FP32、默认分辨率和未专门优化的结果，不是机器性能上限。MPS 中位耗时约为 CPU 的 1/2.55。不能由这些图片结果推出整段视频的解码、逐帧处理、SAM 2 或 VLM 耗时。

官方并非没有性能数据：论文 Table 20 报告 Tiny 为 172M 参数、464 GFLOPs、8.37 FPS，但 Appendix C.4 和表注没有给出该速度对应的完整硬件、分辨率、批量与精度口径，因此本轮以本机实测为依据。[官方论文](https://arxiv.org/html/2303.05499v5#A3.SS4)

## 4. 六张图的实际输出与目视核对

以下人数是按可见真实人物逐框核对，排除倒影；不是只比较框数量。两个设备第一轮保留的标签序列和框数量全部一致，对应框坐标最大差异约 0.07 像素。

| 时间／标注图 | 画面 | 目视可见人数 | 检出人物框 | MPS 中位耗时 | 观察 |
| --- | --- | ---: | ---: | ---: | --- |
| [01:00](./mps/mygo_ep01_0060s.jpg) | 暗室五人 | 5 | 5 | 782.02 ms | 五人分别被定位；墙上设备被误标为 window |
| [06:30](./mps/mygo_ep01_0390s.jpg) | 教室前景四人＋背景三人 | 7 | 6 | 754.02 ms | 漏掉中央偏上、被前景人物挡住的棕发背景人物 |
| [09:00](./mps/mygo_ep01_0540s.jpg) | 俯视房间、小人物 | 1 | 1 | 783.84 ms | 人物检出；怀中吉他／手臂区域误标为 telescope，guitar 未检出 |
| [11:00](./mps/mygo_ep01_0660s.jpg) | 单人近景、背景窗户 | 1 | 1 | 802.43 ms | 人物检出；人物框遗漏部分长发，window 框几乎覆盖全图 |
| [12:30](./mps/mygo_ep01_0750s.jpg) | 门口双人、望远镜 | 2 | 2 | 798.16 ms | 遮挡人物和望远镜均检出；door 框选中右侧柜门，没有定位中间打开的门 |
| [18:00](./mps/mygo_ep01_1080s.jpg) | 室外店面远景 | 3 | 3 | 785.78 ms | 三个人物分别检出，没有把倒影多算一人；大面积店面玻璃和上层窗户未覆盖 |

19 个可见人物中，18 个有独立且对应正确的人物框；这六张中未观察到 person 类的重复框或明显误检。物件类有明确误标，不能把“人物框可用”推广为所有类别均可靠。

具体错误包括：

- 01:00 的 `window` 分数约 0.559，实际是墙上音箱／设备；分数较高也不能证明类别正确。
- 09:00 的 `telescope` 分数约 0.371，实际没有望远镜。保留这条错误，没有调阈值或删除后再展示。
- 11:00 的 window 框面积约占画幅 99.32%，其中包含前景人物，不能当作“可见窗户面积”。
- 12:30 检出柜门不能证明“人物处于门框内”；框之间的二维关系还需语义和区域核验。

## 5. 输出怎样用于构图检索

09:00 的人物输出（MPS 第一轮）如下。`label`、`score`、`box_xyxy` 来自模型及官方后处理；归一化中心和框面积是实验脚本额外计算的字段。

```json
{
  "label": "person",
  "score": 0.407468,
  "box_xyxy": [1115.22, 558.60, 1403.75, 737.96],
  "center_xy_normalized": [0.655982, 0.600260],
  "box_area_ratio": 0.024957
}
```

坐标以 1920×1080 原图左上角为原点，x 向右、y 向下。该框说明人物中心在画面偏右、偏下，外接矩形约占画幅 **2.50%**。11:00 近景的人物框则约占 **51.23%**。这提供了“主体大小和位置”的明确比较依据。

这几个面积均是**矩形框面积**，包含背景且受遮挡、画幅裁截和检测误差影响；不是人物可见像素面积。模型没有输出俯视角度、留白、前后景深或“隔着窗户拍摄”等构图标签，需要 VLM 定向判断，必要时再结合区域分割。

`score` 是模型匹配分数，不是经过校准的正确概率。本轮没有运行 SAM 2、VLM、结构检索或跨镜头跟踪。

## 6. 复现

本轮环境：`/tmp/anivag-grounding-mygo-20261016/venv`；模型目录：`/tmp/anivag-grounding-mygo-20261016/model`。临时目录可能被系统清理；长期复现应按固定 revision 重新下载模型，并使用记录的依赖版本。

在项目根目录执行，以 MPS 为例：

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTORCH_ENABLE_MPS_FALLBACK=0 TOKENIZERS_PARALLELISM=false \
/tmp/anivag-grounding-mygo-20261016/venv/bin/python \
  engine/experiment/grounding-dino/20261016-mygo-local/run.py \
  --model-dir /tmp/anivag-grounding-mygo-20261016/model \
  --manifest engine/experiment/grounding-dino/20261016-mygo-local/manifest.json \
  --output /tmp/mygo-grounding-mps-retest \
  --device mps
```

CPU 对照将 `--device` 改为 `cpu`，并使用单独输出目录。该脚本要求 MPS 实际可用，不会静默改用 CPU；在限制 GPU 访问的沙箱内，需要让该离线推理进程获得本机 GPU 访问权限。

只新增本实验报告、脚本、环境记录和结果文件。没有改业务代码、项目依赖声明、数据库、API 或配置；没有重启服务、迁移、commit 或 push。

## 7. 文件范围与验证

新增 28 个文件，约 7.2 MB；其中实验脚本为 151 行，生产代码和测试代码均为 0 行改动。以下全部是本轮新增文件，已有工作区改动没有触碰。

```text
AniVAG/
└── engine/
    └── experiment/
        ├── [A] 20261016-grounding-dino-mygo.md  ← 方法、结果和复现说明
        └── 20261016-grounding-dino-mygo/
            ├── [A] run.py                     ← 离线检测、计时、内存记录和标注
            ├── [A] requirements.txt           ← 隔离环境版本
            ├── [A] hardware.json              ← 硬件信息，不含设备序列号
            ├── [A] manifest.json              ← 样本、预期及哈希
            ├── [A] visual_review.json         ← 逐框复核记录
            ├── [A] annotated_contact_sheet.jpg
            ├── images/
            │   ├── [A] frame_0060.jpg
            │   ├── [A] frame_0390.jpg
            │   ├── [A] frame_0540.jpg
            │   ├── [A] frame_0660.jpg
            │   ├── [A] frame_0750.jpg
            │   ├── [A] frame_1080.jpg
            │   └── [A] selected_contact_sheet.jpg
            ├── mps/
            │   ├── [A] results.json
            │   ├── [A] mygo_ep01_0060s.jpg
            │   ├── [A] mygo_ep01_0390s.jpg
            │   ├── [A] mygo_ep01_0540s.jpg
            │   ├── [A] mygo_ep01_0660s.jpg
            │   ├── [A] mygo_ep01_0750s.jpg
            │   └── [A] mygo_ep01_1080s.jpg
            └── cpu/
                ├── [A] results.json
                ├── [A] mygo_ep01_0060s.jpg
                ├── [A] mygo_ep01_0390s.jpg
                ├── [A] mygo_ep01_0540s.jpg
                ├── [A] mygo_ep01_0660s.jpg
                ├── [A] mygo_ep01_0750s.jpg
                └── [A] mygo_ep01_1080s.jpg
```

验证通过：6 张原图 SHA-256 与清单一致；12 张设备标注图可解码且保持 1920×1080；两份结果均有 6 个样本、18 次正式测量与一致的清单哈希；中位耗时能从逐次结果重算；文档内相对链接全部存在；脚本 AST 语法检查通过。实际模型执行共 36 次正式测量和 4 次预热，没有失败或自动 CPU 回退。

这些文件当前均为 untracked，限定本实验路径的 `git diff --stat` 和 staged diff 为空；未执行 git add、commit 或 push。

## 8. 目录整理

按用户要求，报告和产物统一迁入 `engine/experiment/grounding-dino/20261016-mygo-local/`。报告现在为本目录的 `README.md`；第 7 节文件树保留实验初次生成时的历史路径。结果 JSON 中 `arguments` 也保留实际执行时的旧路径，其他内容未变；上面的复现命令已更新。原始清单中的 `Human-visible` 和 `manual_*` 表示助手目视检查，不是人工专家标注。
