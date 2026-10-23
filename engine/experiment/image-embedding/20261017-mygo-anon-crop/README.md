# 局部爱音与同一完整画面：图片 embedding 对照

2026-10-17。延续[小杯子实验](../20261017-mygo-cup-crop/README.md)，只新增一次局部爱音图片编码，复用同模型已保存的完整画面、杯子和六个固定参照向量。

**局部爱音 → 对应完整画面余弦为 0.705201739，在相同 7 候选中排第一。** 第二名夕阳下爱音为 0.675944289，差值 0.029257450。分数高于杯子对完整画面的 0.364078839，但前两名差距并未扩大。

## 输入与方法

- [局部爱音原图](images/anon_crop.png)：592×816、662328 字节，原始 PNG 未在本地缩放、裁剪、增亮或重编码。嘴形与完整输入图可见差异，未建立唯一同帧裁剪对应。
- 完整画面仍为[上轮用户提供的图片](../20261017-mygo-cup-crop/images/reference_full.png)。对应关系来自用户说明，未独立定位集数或时间。
- 实际使用 Ark endpoint `ep-20260911212434-j4xcb`，实际响应模型 doubao-embedding-vision-250615，1024 维；encoder revision=gateway-ark-embedding-v1。新查询与缓存的 provider/model/encoder/revision/dimensions 一致才执行。
- 缓存 8 个向量及上轮清单、结果均保存 SHA。本轮只调用 1 次 embed_image；主排名仍是完整画面加原六图，杯子图不加入这 7 个候选，只另算 pair，保持与上轮候选集一致。
- 不附加文字 query。余弦采用归一化点积，保存供应商原始向量及范数。

## 结果

| 排名 | 候选 | 局部爱音图的余弦 |
| --- | --- | ---: |
| 1 | 用户提供的对应完整画面 | 0.705202 |
| 2 | 原图 3：夕阳下爱音近景 | 0.675944 |
| 3 | 原图 1：化妆字幕爱音近景 | 0.648009 |
| 4 | 原图 5：祥子正面近景 | 0.642164 |
| 5 | 原图 2：教室同学围坐 | 0.554346 |
| 6 | 原图 6：爱音泡澡 | 0.541467 |
| 7 | 原图 4：暗处爽世回头 | 0.541374 |

| 同一完整画面的两种局部输入 | 余弦 | 固定 7 候选排名 | 与第二名差 |
| --- | ---: | ---: | ---: |
| 杯子局部图（上轮缓存） | 0.364079 | 1 | 0.032004 |
| 爱音局部图（本轮新增） | 0.705202 | 1 | 0.029257 |

爱音局部图与杯子局部图的额外 pair 余弦为 0.212218。两张局部图虽由用户确认来自同一场景，但这一信息没有输入 embedding 模型。

本次爱音图与对应画面的绝对分数更高，但另一张不对应的爱音近景也达到 0.675944。不能把 0.705202 当作 70.5% 匹配概率，或仅凭绝对分数断言局部出处定位更稳。当前数据也不能单独证明模型依赖了人物身份、姿势、画风中的哪一项。

## 耗时与验证

本轮单次编码及其本地初始化/记录计时约 409.068 ms，编码、全部比较和记录共 410.560 ms；缓存向量没有再次请求。该计时不代表服务并发或全库检索延迟。

原图 SHA/尺寸、缓存 SHA、模型身份、1024 维有限非零向量、范数及完整排名均核对；独立 NumPy 归一化点积与结果的最大误差为 1.11e-16。脚本 AST 与保存摘要一致，未写生产代码或配置、索引或数据库，未重启、commit 或 push。目录被现有 Git ignore 排除。

## 文件与复现

- [manifest.json](manifest.json)：新输入及被复用文件的 SHA、同一候选集与依据。
- [query-embedding.json](run1/query-embedding.json)：新局部图原始向量、范数和响应模型。
- [results.json](run1/results.json)：八个 pair 分数、主排名和前轮比较。
- [model-calls.json](run1/model-calls.json)：唯一新图片调用、状态与 usage。
- [validation.json](run1/validation.json)：独立复算记录。

```sh
uv run --no-sync --env-file .env.local python -u \
  engine/experiment/image-embedding/20261017-mygo-anon-crop/run.py \
  --output engine/experiment/image-embedding/20261017-mygo-anon-crop/run2
```

输出目录须不存在；依赖相邻杯子实验的未改原文件和同一模型配置。本轮仍是单个局部图对固定图片候选的比较，没有查询现有 Chunk 视频索引、建帧/区域索引、定位原视频时间或评估全库 Recall。

本轮新增 8 个实验文件并修改主题索引 1 份，生产文件和测试文件 0 个。实际树：

```text
AniVAG/
└── engine/
    └── experiment/
        └── image-embedding/
            ├── [M] README.md
            └── 20261017-mygo-anon-crop/
                ├── [A] README.md
                ├── [A] run.py
                ├── [A] manifest.json
                ├── images/
                │   └── [A] anon_crop.png
                └── run1/
                    ├── [A] results.json
                    ├── [A] query-embedding.json
                    ├── [A] model-calls.json
                    └── [A] validation.json
```
