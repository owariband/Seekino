# PCP + 全部框 / 人物框：可调权重对照实验

日期：2026-10-17。**继续使用同一个 [交互实验页](report.html)**。刷新后，顶部标题为“PCP ＋ 检测框：全部框 vs 人物框”。原来的全框方案仍在 A 组，新人物框方案在 B 组。

## 怎么看

- 默认水族馆、PCP 80% / 框图 20%，A/B 并排。滑杆与数字输入同时作用于两组。
- 候选范围默认“检测到人物的图片”：13 张；查询自身也在其中时，实际有 12 个候选。两组使用相同候选池。
- 可切换“全部图片”，此时 A 通常有 16 个候选；B 在框权重大于 0 时，会排除没有人物框特征的候选。页面单独显示范围筛除和特征不可用数量。
- 查看方式可切到“仅 B：人物框”，以更大的图片查看结果。查询区域并排展示原图、全部框图、人物框图、PCP 九类分布。
- 建议先选“舞台人物靠左”，比较 80/20 与 0/100；再选水族馆、挑高客厅，观察人物位置、大小和排列是否符合预期。

## 两组实际比较什么

| 分支 | A：PCP + 全部框 | B：PCP + 人物框 |
|---|---|---|
| PCP | 同一原图的九类概率分布，9 维 | 相同 |
| 框图 | 保存的 Grounding DINO 全部阈值后检测框 | 从同一检测结果中只选 `label == person` |
| 编码器 | 冻结 DINOv2 Small，384 维 CLS | 相同权重、相同预处理，重新编码人物框图 |
| 排名 | PCP 与全框向量的原始余弦线性加权 | PCP 与人物框向量的原始余弦线性加权 |

```text
A = (1 − 框权重) × PCP余弦 + 框权重 × 全部框图余弦
B = (1 − 框权重) × PCP余弦 + 框权重 × 人物框图余弦
```

人物框保留在原画幅中的位置和大小，没有裁剪后分别居中。沿用 448×448 白底等比留白画布、黑色 3px 框线，再 LANCZOS 缩到 224×224，使用相同官方 rescale/normalize，无中心裁剪。所有框不附类别文字或置信度，不做 NMS；本轮只改变选取的框类别。

默认权重只是起点，没有训练或选择最优权重。向量分别 L2 归一化后计算余弦，没有对两路分数做 min-max 等校准。分数不是概率，权重系数也不等于对排名的实际影响比例。

## 本轮运行及边界

本轮实际在本地 CPU 运行已缓存的 DINOv2 Small，编码 **13 张人物框图**。17 张图都保存了框图，其中 4 张没有人物检测，保存白图但不生成向量：`pink_tilt`、`phone_tilt`、`bridge_diagonal`、`building_perspective`。

`pink_tilt` 是已确认的漏检：原图有人物特写，却没有 person 框。因此“未检出”不能解释成“无人”。没有人物特征的查询或候选，在 B 组且框权重大于 0 时标为不可用；框权重为 0 时两组都只用 PCP，仍遵守当前候选池，不自动重分配权重。

无 NMS，因此框数不等于人数：`classroom_group` 保存 7 个人物框，`stage_wide` 保存 6 个，其中包含重叠检测。本轮保留它们，以便和原全部框实验控制变量。

13 张单次编码中位耗时 **21.31 ms**（CPU float32、6 线程，不含模型载入、绘图和文件 I/O；不是硬件性能基准）。`grey_left`、`stage_wide`、`stage_left` 原来的全部框本来就都是人物框：本轮三张 PNG 与旧图逐像素和 SHA-256 一致，向量最大绝对差均为 0。

## 实际观察

“舞台人物靠左”查询，默认同一批检测到人物的候选，PCP 80% / 框 20%：

| 排名 | A：全部框 | B：人物框 |
|---:|---|---|
| 1 | 粉发正面近景 0.9817 | 窗边近景 0.9948 |
| 2 | 窗边近景 0.9765 | 粉发正面近景 0.9862 |
| 3 | 俯视房间 0.9626 | 俯视房间 0.9750 |

纯人物框时，B 的前三名是窗边近景 0.9942、灰发人物靠左 0.9755、粉发正面近景 0.9512。移除其他类别框后，单个人物大框更容易互相匹配，但窗边近景仍排在位置更接近的灰发左侧人物之前，说明冻结编码器对位置与尺度的取舍仍需验证。不能据此认为人物框 embedding 已经严格表达了人物布局。

水族馆在默认 80/20 下，两组第一名仍是商店全景；人物框主要交换了第二、第三名。纯人物框时其第一名成为俯视房间，后者只有一个人物框，也说明当前相似度不能直接保证人数和排列一致。

## 固定参考对照

两组的查询和候选都限定在同一批 13 张检测到人物的图片，排除查询自身。沿用旧助手目视的粗构图标签，10 个主三元组中仅 **6 组共同适用**：T02、T04、T07、T08、T09、T10。剩余主组和全部边界组缺少共同有效样本。

| 框权重 | A：主对照胜数 / 6 | B：主对照胜数 / 6 | A：Top-3共同标签 / 39 | B：Top-3共同标签 / 39 |
|---:|---:|---:|---:|---:|
| 0% | 1 | 1 | 6 | 6 |
| 20% | 1 | 1 | 5 | 5 |
| 50% | 1 | 1 | 6 | 6 |
| 80% | 2 | 2 | 9 | 8 |
| 100% | 1 | 3 | 7 | 10 |

这是小样本探索，不是准确率；旧标签也不是专门的人物布局标注。默认 20% 时并未看到粗构图参考指标的提升，100% 时的差异也不足以选出最佳权重。候选过滤本身带来的改善，不能归因于人物框向量。

下一批样本应按构图类别补充不同镜头，每类先准备约 10～20 个；同时包含“内容不同但人物布局相似”和“内容相近但布局不同”的对照，相邻帧/同镜头归组。位置、尺度、人数关系需要单独标注，避免仅凭一个粗构图标签判断所有布局需求。

## 文件与复现

- [人物框原始输出](person-boxes/results.json)：13 个向量、17 张框图路径、逐条人物检测、模型及数据哈希、编码耗时、相同输入对照。
- [页面数据](data.json)：PCP / 全部框 / 人物框三个相似度矩阵，空缺人物特征使用 `null`。
- [共同候选池对照](person_weight_checks.json)：五档权重下的完整 A/B 排名与共同参考统计。
- [旧全框对照](weight_checks.json) 与 [旧独立核查](review.md)：保留上一轮未过滤 17 张图库的结果；不能与当前 13 张池的统计混比。
- [验收记录](validation.json)：数值、源文件一致性及实际浏览器验证。

```bash
# 重新运行人物框编码；仅用已有本地模型
.cache/composition-models/.venv/bin/python engine/experiment/composition-models/20261017-pcp-box-fusion/person-boxes/run.py
# 重新生成页面和固定权重对照
.cache/composition-models/.venv/bin/python engine/experiment/composition-models/20261017-pcp-box-fusion/build.py
```

`viewer.html` 是页面模板。沿用原 PCP 和全框向量，本轮没有重新运行检测、训练、下载模型或调用外部接口。交互报告内嵌数据并引用本地图片，调整权重不触发模型调用。生产代码 0 行，未执行 commit、push、迁移或项目服务重启。

## 本轮变更范围

8 个现有文件更新，20 个文件新增（含 17 张人物框图）；生产代码 0 行。范围与开工时的实验边界一致。旧全部框的相似度矩阵和 `weight_checks.json` 保持不变。

```text
AniVAG/
└── engine/
    └── experiment/
        └── composition-models/
            ├── [M] README.md  实验索引
            └── 20261017-pcp-box-fusion/
                ├── [M] build.py  接入人物框向量及共同池对照
                ├── [M] viewer.html  A/B并排和候选池控件
                ├── [M] data.json  三路相似度与框图路径
                ├── [M] report.html  生成的交互报告
                ├── [M] README.md  运行方法、结果与局限
                ├── [M] validation.json  数据与浏览器验收
                ├── [M] report_browser_verified.jpg  实际浏览器截图
                ├── [A] person_weight_checks.json  五档权重的共同池对照
                └── person-boxes/
                    ├── [A] run.py  人物框绘制与本地编码
                    ├── [A] results.json  向量、检测及溯源记录
                    └── images/
                        ├── [A] aquarium__persons.png
                        ├── [A] bridge_diagonal__persons.png
                        ├── [A] building_perspective__persons.png
                        ├── [A] classroom_group__persons.png
                        ├── [A] door_pair__persons.png
                        ├── [A] grey_left__persons.png
                        ├── [A] interior__persons.png
                        ├── [A] phone_tilt__persons.png
                        ├── [A] pink_center__persons.png
                        ├── [A] pink_tilt__persons.png
                        ├── [A] rehearsal_left__persons.png
                        ├── [A] room_high__persons.png
                        ├── [A] school_layers__persons.png
                        ├── [A] stage_left__persons.png
                        ├── [A] stage_wide__persons.png
                        ├── [A] store_wide__persons.png
                        └── [A] window_close__persons.png
```

数据与交互的具体检查见验收记录。未修改 API、数据库或生产配置；未扩大到样本扩充与人物检测修复。
