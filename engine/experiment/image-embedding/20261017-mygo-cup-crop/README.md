# 小杯子局部图与完整画面：图片 embedding 对照

2026-10-17。直接使用用户提供的杯子局部图与对应完整画面，分别调用当前 Gateway 的图片 embedding，计算余弦相似度，不经过 Chat Agent。

**杯子局部图 → 对应完整画面的余弦为 0.364078839。** 以原六张 MyGO 截图作为固定的非对应画面参照，在给定 7 个候选中，对应完整画面排名第一，第二名泡澡图为 0.332075052，差值 0.032003787。本例不能证明全库检索稳定，也不能把余弦当作匹配概率。

## 输入与方法

- [杯子局部图](./images/cup_crop.png)：696×422；[完整画面](./images/reference_full.png)：2412×1246。用户确认二者在同一地方截取；本轮未独立标注原视频集数、时间、精确帧或裁剪坐标。
- 八张 PNG 原字节保存，未在本地缩放、裁剪、增亮、去字幕或重编码。Gateway 仅把原文件转为 data URL，供应商内部预处理未观察。
- 六个参照在评分前固定为之前六图实验的原始输入，不按本次得分挑选。它们是方便取得的不同画面参照，缺少相似咖啡店、相同杯具等困难负例。
- 只给 embedding API 图片，不附加人物、场景描述或问题文本。SDK usage 仍报告每次 13 个 text_input_tokens；请求代码只有 image_url，不能据此解释为人为添加了文字检索词。
- 使用 `GatewayClient.create_embedding_model("embedding")` 的同一解析绑定。实际请求 endpoint 为 `ep-20260911212434-j4xcb`，配置 encoder 标识及八次实际响应模型均为 `doubao-embedding-vision-250615`，Ark，1024 维；encoder revision 为 gateway-ark-embedding-v1。
- 保存原始向量；范数约 0.9978–1.0024，并非精确单位向量。比较使用 `dot(a,b)/(L2(a)*L2(b))`，没有把原始点积当余弦。查询图自身不进入候选排名。

输入角色、来源文件、尺寸、字节数和 SHA 见 [manifest.json](./manifest.json)；最终解析绑定见 [results.json](./run1/results.json)，原向量和范数见 [embeddings.json](./run1/embeddings.json)。

## 固定候选排序

查询始终为小杯子局部图；表内 7 个候选均单独按整张图片编码。

| 排名 | 候选 | 余弦相似度 |
| --- | --- | ---: |
| 1 | 用户确认对应的完整画面 | **0.364079** |
| 2 | 原图 6：爱音泡澡 | 0.332075 |
| 3 | 原图 1：带化妆字幕的爱音近景 | 0.293606 |
| 4 | 原图 5：祥子正面近景 | 0.274795 |
| 5 | 原图 4：暗处爽世回头 | 0.242053 |
| 6 | 原图 2：教室同学围坐 | 0.223067 |
| 7 | 原图 3：夕阳下爱音近景 | 0.163869 |

这一次对应画面排第一，说明不能直接断言现有图片向量完全无法关联局部图与整图。但与第二名仅差 0.032004，没有困难负例或阈值校准，无法判断该差距是否足以支持完整视频库检索。

反向以完整画面作为查询，在这另外 7 张图中，杯子局部图反而排最后：完整画面与六个参照的余弦为 0.552087–0.702064，均高于与杯子的 0.364079。余弦本身对称，变化的是查询相对于其他候选的排名，不是这对图的分数。这是同一组向量的另一种排名视角，表明本例的局部与整图关系没有在两个查询方向都优先于其他画面。它不等同于系统级准确率或模型机制的独立证明。

## 调用耗时与验证

8 次图片 embedding 全部成功，平均 609.992 ms/图。杯子图 343.864 ms，完整画面 721.205 ms；这两次合计 **1.065 秒**。全部 8 次编码及本地输出共 **4.990 秒**，不是单对编码耗时，也不是线上并发延迟评测。

SDK 共报告 9662 input tokens，其中 image_input_tokens=9558、text_input_tokens=104；本轮未计算费用。模型调用状态、实际模型名和 usage 见 [model-calls.json](./run1/model-calls.json)，没有保存认证信息或 Base64。

独立复核已确认：8 张原图 SHA、尺寸、字节数及输入摘要一致；8 个响应均为 image、1024 维、有限且非零；独立使用高精度求和重算全部余弦，误差不超过 3e-16，排名和差值一致。自动复核记录见 [validation.json](./run1/validation.json)。

## 边界与复现

本轮是 **image → image 的单样本、小候选集比较**。没有查询现有 Chunk 视频向量索引，没有构建帧索引、局部特征或区域匹配，没有从视频中定位这个杯子，也未用坐标或几何关系证明同一精确源帧。对应关系来自用户提供；候选是否困难也没有经过系统采样。

```sh
uv run --no-sync --env-file .env.local python -u \
  engine/experiment/image-embedding/20261017-mygo-cup-crop/run.py \
  --output engine/experiment/image-embedding/20261017-mygo-cup-crop/run2
```

输出目录必须不存在。需要当前配置的 Ark 凭据，会重新调用图片 embedding 服务；以每次 results.json 保存的实际模型绑定为准。

本轮只新增 1 个约 120 行的实验脚本、2 份 Markdown、固定清单、8 张原字节输入及 4 份运行 JSON，共 16 个文件。没有生产代码、配置、数据库、前端或 API 变更，没有重启服务、commit 或 push。实验目录沿用现有 Git ignore 规则，普通 git diff 不显示新增产物。

```text
AniVAG/
└── engine/
    └── experiment/
        └── image-embedding/
            ├── [A] README.md
            └── 20261017-mygo-cup-crop/
                ├── [A] README.md
                ├── [A] run.py
                ├── [A] manifest.json
                ├── images/
                │   ├── [A] cup_crop.png
                │   ├── [A] reference_full.png
                │   ├── [A] control_01.png
                │   ├── [A] control_02.png
                │   ├── [A] control_03.png
                │   ├── [A] control_04.png
                │   ├── [A] control_05.png
                │   └── [A] control_06.png
                └── run1/
                    ├── [A] results.json
                    ├── [A] embeddings.json
                    ├── [A] model-calls.json
                    └── [A] validation.json
```
