# 用户大厅截图：Grounding DINO 构图结构检测

日期：2026-10-17。状态：完成。3 组提示 × 3 次正式推理，另有 2 次预热；全部成功。

问题：直接运行 Grounding DINO，查看用户提供的大厅截图能否检出支撑“上下分层”构图的结构，而非仅由助手目视解释。

输入为用户提供的 2606×1318 PNG，原始字节复制为 [input.png](./input.png)，不裁剪、不增强、不重新编码。三组提示词在推理前固定，使用同一个模型及相同阈值；没有追加 NMS 或人工删除检测框。

1. `baseline_objects`：沿用上一轮 `person . window . door . chair . table . guitar . telescope .`
2. `architectural_elements`：`person . staircase . railing . mezzanine . horizontal wooden beam . window . dining table . chair . painting .`
3. `layout_regions`：直接尝试 `upper part of the room . lower part of the room . horizontal divider .`

前两组比较物件及建筑结构定位，第三组尝试用文字直接请求构图区域。第三组包含预期布局词，不是无提示的自主布局识别；即使输出上／下标签，也必须核对框实际覆盖的区域。

复用已下载的 `IDEA-Research/grounding-dino-tiny`，FP32、MPS、batch=1，box threshold=0.35、text threshold=0.25。每组正式运行三次，先执行两次基线预热。输入、参数和哈希见 [manifest.json](./manifest.json)。

## 结果

**建筑结构提示确实让模型定位到了支撑上下分层的横向木质带。** 普通物件提示未请求这一结构；直接请求上／下区域的提示，在本轮固定阈值下没有返回框。

| 提示组 | 原始框数量 | 主要结果 | 检测流程中位耗时 |
| --- | ---: | --- | ---: |
| baseline_objects | 9 | person×1、chair×3、table×1、window×2、door×2；左侧画作误标为 window | 553.39 ms |
| architectural_elements | 17 | person×1、staircase×1、painting×1、horizontal wooden beam×1、dining table×1、window×2、chair×10 | 735.15 ms |
| layout_regions | 0 | 没有保留框；没有得到所要求的上下区域 | 709.03 ms |

这些是原始框数量，不等于真实物体数量。没有做逐个物体真值标注；重复区域和类别错误均保留。第三组的零框只说明本模型、这条提示、0.35 阈值下未成功，不能推出所有其他提示或阈值也一定失败。

### 建筑结构提示的完整标注

![建筑结构提示：全部17个框](./architectural_elements.jpg)

关键输出如下，坐标相对于 2606×1318 原图，图上编号从 1 开始：

| 编号／标签 | score | 原图框 `[x0,y0,x1,y1]` | 目视核对 |
| --- | ---: | --- | --- |
| #1 person | 0.711156 | `[884.00,1049.51,947.45,1275.56]` | 下方偏左的小人物 |
| #2 staircase | 0.723250 | `[1927.20,716.69,2603.44,1313.87]` | 右侧楼梯主体；框本身不提供楼梯斜线角度 |
| #9 painting | 0.554256 | `[1.44,277.77,434.91,1314.88]` | 左侧大幅画作 |
| #17 horizontal wooden beam | 0.460176 | `[576.58,719.97,2298.30,846.95]` | 中间横向木质连廊／楼板带；右端包含部分楼梯顶部踏步 |

没有单独保留 `railing` 或 `mezzanine` 标签。建筑提示里同时加入了多个词，所以这一轮只能比较两套提示的结果，不能把某个变化归因于其中某一个词的独立作用。`horizontal wooden beam` 是输入提示中的名称，不是模型自行给出的建筑构件鉴定。

### 横向框能提供哪些构图证据

把 #17 的框除以原图宽高，得到：

```json
{
  "label": "horizontal wooden beam",
  "score": 0.460176,
  "x_range_normalized": [0.2213, 0.8819],
  "y_range_normalized": [0.5463, 0.6426],
  "width_fraction": 0.6607,
  "height_fraction": 0.0963,
  "box_aspect_ratio": 13.56,
  "center_y_normalized": 0.5944
}
```

即：它是一个宽约占画面 66.1%、高约占 9.6% 的横向长框，位于高度 54.6%–64.3%，中心约 59.4%。结合原图目视确认，该框可以作为“中部偏下有一条强水平分隔带”的定位证据。

若后处理选择该框中心作为候选上下分界，则候选 `split_y ≈ 0.5944`。**这一步是代码对目标框的几何解释，不是 DINO 直接输出了两个语义区域。** 该框也没有贯穿全画宽，左右区域和楼梯仍会跨越分区。不能把横梁框当作精确的楼板轮廓或全画幅硬切分。

以上派生数值记录在 [derived_geometry.json](./derived_geometry.json)，计算为 `x/W`、`y/H`、`(x1-x0)/W`、`(y1-y0)/H`、`(x1-x0)/(y1-y0)` 和 `(y0+y1)/(2H)`。

### 另外两组的原始标注

- [普通物件提示标注图](./baseline_objects.jpg)：左侧画作被标为 window，说明开放词汇定位也受候选文字影响。
- [直接上下区域提示标注图](./layout_regions.jpg)：零框，画面原样显示；不是缺少保存结果。

## 本轮回答的问题

这次真实执行支持更具体的分工：**DINO 能在给定建筑结构提示后定位构图的重要组成部分；在这张图上，横向木带和右侧楼梯都被检出。要得到“整体上下分层”的表达，还要对这些框做布局解释。**

本轮没有运行 VLM、SAM、线段检测、其他模型权重或阈值搜索。没有将此前助手对图片的目视判断冒充模型输出；逐项核对记录在 [visual_review.json](./visual_review.json)。只有这一张图片，无法判断该提示在其他建筑、自然景观或抽象构图上的通用性。

## 运行设置与复现

- 硬件：沿用上一轮 Apple M5 Pro、48 GB 统一内存；[硬件记录](../20261016-mygo-local/hardware.json)。
- 模型 revision：`a2bb814dd30d776dcf7e30523b00659f4f141c71`；172,249,090 参数。
- PyTorch 2.14.1，Transformers 4.57.6；[隔离环境依赖](../20261016-mygo-local/requirements.txt)。
- 默认处理器 shortest_edge=800 / longest_edge=1333，9 次正式推理输入全部为 `[1,3,674,1333]`。
- CPU threads=6，禁用自定义 CUDA 算子，`PYTORCH_ENABLE_MPS_FALLBACK=0`。图片未上传，推理离线运行。
- 计时：预加载 PIL 图片 → 预处理 → MPS 传输 → 前向 → 同步 → 官方后处理 → CPU 输出；排除 Python 导入、加载模型、读取图片和绘图。只测三次，不用于比较提示长度的性能上限。
- 每组第一次正式推理的完整检测结果保存为 JSON；三次均计时，日志保留每次的标签列表。

```sh
/tmp/anivag-grounding-mygo-20261016/venv/bin/python -u \
  engine/experiment/grounding-dino/20261017-interior-layout/run.py \
  --model-dir /tmp/anivag-grounding-mygo-20261016/model \
  > engine/experiment/grounding-dino/20261017-interior-layout/run.log 2>&1
```

临时模型和环境可能被系统清理，长期复现需按固定 revision 和依赖记录重建。受限沙箱若禁止 GPU 访问，需要让本地实验进程访问 MPS。[脚本](./run.py)、[原始 JSON](./results.json)、[执行日志](./run.log)均保留。

## 改动范围与验证

只新增本轮实验目录，并更新上一级实验索引。未改业务代码、主环境依赖、数据库、API 或配置，未执行 commit、push、迁移和重启。原有实验和其他工作区修改保持原样。实际文件清单和验证结果见下方。

验证全部通过：[validation.json](./validation.json)。原图字节／哈希、原图尺寸、清单哈希一致；3 组共 9 次正式推理和 2 次预热；三组每轮标签列表一致；3 张标注图尺寸保持 2606×1318；中位耗时重算、脚本 AST、文档相对链接校验通过。

本轮新增 12 个文件，修改模型实验索引 1 个文件；新增实验脚本 106 行，生产和测试代码 0 行改动，未超出内部预计范围。该模型实验目录仍为 untracked，限定目录的 `git diff --stat` / `--numstat` / staged diff 均为空，未执行 git add。

```text
AniVAG/
└── engine/
    └── experiment/
        └── grounding-dino/
            ├── [M] README.md  ← 增加本轮实验索引
            └── 20261017-interior-layout/
                ├── [A] README.md  ← 实验方法、结果和复现
                ├── [A] architectural_elements.jpg  ← 完整原始检测框标注图
                ├── [A] baseline_objects.jpg  ← 完整原始检测框标注图
                ├── [A] derived_geometry.json  ← 横向检测框的派生几何，非模型直接分区
                ├── [A] input.png  ← 用户原始图片，字节不变
                ├── [A] layout_regions.jpg  ← 完整原始检测框标注图
                ├── [A] manifest.json  ← 输入哈希和预先固定参数
                ├── [A] results.json  ← 原始检测框、分数、逐次耗时
                ├── [A] run.log  ← 实际执行日志
                ├── [A] run.py  ← 本地三组提示推理及标注
                ├── [A] validation.json  ← 产物一致性校验
                └── [A] visual_review.json  ← 助手独立目视复核记录
```
