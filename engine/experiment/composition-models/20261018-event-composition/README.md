# 爱音俯视房间：新 Event 检索实测

2026-10-18。目标：使用当前 Event 检索工具查参考图，核对真实 Hit 1，并找“构图类似的其他画面”。

- [直接看图片对照](report.html)：静态离线页，图片全部内嵌，无需 fetch 或 JavaScript。
- [四图关键对照](key-comparison.jpg)。
- [正式文字工具结果](results.json)；[原图向量对照结果](image-results.json)。

## 实际结论

| 方式 | 原始 Event Hit 1 | 查看原画面后的判断 |
|---|---|---|
| 原图编码 → Event 视觉向量（底层实验） | 第 1 集 09:02.300–09:04.300 | 参考图同一来源镜头；cosine 0.9294221，不是构图概率或逐像素原图证明 |
| 正式工具：完整画面内容文字 | 第 1 集 09:02.300–09:04.300 | 同样找到来源镜头 |
| 正式工具：只写构图 | 第 13 集 00:37.500–00:40.800 | 大空间小人物，但上下分层与目标斜俯视地面构图有差别 |
| 正式工具：更宽泛的构图文字 | 第 5 集 22:07.600–22:10.400 | ED 字幕版式，属于误召回 |

构图文字的 **Rank 5** 为第 7 集 **04:08.600–04:09.100**：另一个斜俯视室内画面，人物靠右、中央茶几、家具边缘形成对角线，值得作为正候选进一步比较。它未进入原图向量 Top 40。

构图文字 **Rank 6**、原图向量 **Rank 5** 均为第 10 集 **15:30.300–15:35.600**：俯视客厅、环境占主导，但人物在上方中央，与参考图右下位置不同。

以上是助手对抽帧的目视分析，不是独立人工标注数据集，不代表总体检索准确率。原图向量后续高排名仍混入同房间低机位、同人物近景，说明它能够召回候选，但尚不能直接充当构图排序。

## 如何执行

正式工具当前只有文字 query，没有 image 参数。本轮并未声称将图片直接传入正式工具，也没有经过 Chat 自行选词或重排：助手先查看参考图，再明确写三条 query，通过 `build_search_video_events_tool(...).invoke(args)` 调用实际 Agent 工具，使用 `inspect_video_frames` 获取原视频预览。

所有文字请求：`top_k=10`、`channel_weights={lexical:0, semantic:0, visual:1}`。这代表文字向量检索 Event 图像索引，不是图片查询。完整 query 在 `results.json` 和报告中保留。

原图对照单独使用现有配置的 `embed_image(reference.jpg)`，仅调用一次；再对 Event 视觉索引执行 exact Top 40，保留 scope/source/contract 校验及正式回源校验，没有伪装 text modality。模型为现有 `doubao-embedding-vision-250615`，目的是测试现有检索候选来源，不是将它加入此前的专用构图模型排名。

范围：MyGO 13 个当前任务、312 个 Chunk 来源；此前补建验收为 7103 Events。使用当前覆盖合同：

`event-index:sha256:f9a6b7da7081de5958eabede0bdeb2fc75b1fd8979745fae41d1e7abc2a03e1f`

参考图 SHA-256：`f1af60a2cf0125052071c1d5d487324bab720aaa8dd9b5b824c9294b10f6a6f9`。

## 下一步实验

现在已经有足够的数据来源开始扩充构图实验，不必继续等待 Event 补建。

1. 合并原图与构图文字召回的候选，保留 Event ID、来源和时间。
2. 抽取真实帧，排除原镜头及重复画面；跨镜头 Event 取多帧，不能假定 Event 一定连续单镜头。
3. 对同一候选集合运行既有 PCP 类别表示和 person 框画布 → DINOv2，相似度加权仍可 DIY。需要人物时验证人物存在；俯视/仰视另行判断，不假定 PCP 类别覆盖视角。
4. 检查能否把第 7 集这类跨内容构图候选排前，把同房间低机位和 ED 版式排后。这一步尚未执行；应先取得人工判断，再比较权重。

如果扩大候选池，须同时检查每通道召回数量：当前文字通道默认 20 Events，`top_k` 是 Chunk 分组数，仅调大它不等于扩大 Event 候选池。原图实验采用 exact Top 40，不能把与文字实验的数量/分数差直接归因于输入模态。

## 限制与验证口径

- 文字检索三次均成功、`partial=false`、无 rejected；当前来源覆盖校验通过。
- 原图检索 40 条均通过回源和 Event 校验，原始顺序完整保存。
- 原图与文字的 Top 1 由实际原视频抽帧确认，不能仅凭文本描述确认构图。
- 每条展示 Event 只抽中点一帧；它不是该 Event 全过程的证明，也不是原始采样帧逐帧检索。
- 展示层跳过同集时间重叠或间隔 1.5 秒内的已展示候选，保留原始 rank。尚未执行视觉去重或精确镜头切分。
- 原图 exact 分数为 `cosineSimilarity + 1`，报告单独换算 cosine；文字保留 visual 原始分数。两者不直接横比，不当作构图概率。
- 本轮无生产代码、接口、数据库 Schema、索引写入或配置变更；不重启、不 commit、不 push。仅查询既有索引和抽帧产物。
- 报告生成成功；3 个 Python 脚本语法检查通过；29 条预览引用的 SHA 校验通过；HTML 中 34 个内嵌 JPEG 全部可以解码，所有本地文件链接存在，脚本数量为 0。尝试浏览器预览时，浏览器工具的 URL 策略拒绝了 `file://` 协议，因此没有宣称完成浏览器渲染验收；关键四图已直接打开目视核对。

## 产物与复现

```text
engine/experiment/composition-models/
├── [M] README.md                         实验索引
└── 20261018-event-composition/
    ├── [A] README.md                     本次说明
    ├── [A] run.py                        正式工具三组文字检索及抽帧
    ├── [A] image_search.py               原图向量独立对照
    ├── [A] build_report.py               由保存结果生成静态报告
    ├── [A] report.html                   图片内嵌报告
    ├── [A] reference.jpg                 查询参考图副本
    ├── [A] results.json                  文字结果及抽帧证据
    ├── [A] image-results.json            图像结果、原始排名及抽帧证据
    ├── [A] content-tool.json / content-trace.json
    ├── [A] layout-tool.json / layout-trace.json
    ├── [A] broad-tool.json / broad-trace.json
    ├── [A] key-comparison.jpg            关键结果四图
    ├── [A] content-contact.jpg / layout-contact.jpg / broad-contact.jpg
    └── frames/                          本轮真实原视频抽帧 JPEG
```

`experiment/` 受仓库忽略规则管理，本轮产物不会出现在普通 `git diff`。原有其他工作线修改保持不动。

实际范围为本实验目录 46 个新增文件（含 JSON 和图片）及上级实验索引 1 个更新，生产文件数为 0。

在项目根目录运行（查询脚本会调用已配置 embedding 服务；已有结果不会被覆盖）：

```sh
uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261018-event-composition/run.py
uv run --no-sync --env-file .env.local python engine/experiment/composition-models/20261018-event-composition/image_search.py
```

仅重新生成报告，无模型调用：

```sh
uv run --no-sync python engine/experiment/composition-models/20261018-event-composition/build_report.py
```
