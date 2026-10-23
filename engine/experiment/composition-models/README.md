# 构图模型对比实验

目标：比较现成模型在不同构图画面上的原生输出，以及跨人物、物件和场景检索相同构图的能力。以类别相似为主，同时通过可调权重实验检查全部框与人物框的位置、尺度信息能否补充检索。

## 实验索引

- [2026-10-18 爱音俯视房间：新 Event 工具与原图向量实测](20261018-event-composition/README.md) · [真实 Hit 1 与其他构图候选](20261018-event-composition/report.html)。
- [2026-10-18 MyGO 13 集旧 Event 索引补建与验收](20261018-event-backfill/README.md)：扩大后续构图实验的候选来源。
- [2026-10-17 俯视房间：现有 Chat 与构图向量检索对照](20261017-chat-composition/README.md) · [参考图与真实返回画面](20261017-chat-composition/report.html)。
- [2026-10-17 PCP＋检测框图：全部框 / 人物框对照，DIY 调整融合权重](20261017-pcp-box-fusion/README.md) · [交互实验页](20261017-pcp-box-fusion/report.html)。
- [2026-10-17 结构图实验：框／线画布 → DINOv2 Small](20261017-layout-raster/README.md) · [离线结果页](20261017-layout-raster/report.html)。
- [2026-10-17 首轮：17 张动画图 × 5 种候选模型](20261017-first-pass/README.md)
- [交互结果页](20261017-first-pass/report.html)：原始解析、类别分布、评分、框／线可视化与 Top-5。
- [机器可读统一结果](20261017-first-pass/comparison.json)。

首轮模型为 PCP 摄影构图分类模型、SAMP-Net、CLIP ViT-B/32、DINOv2 Small、MUSIQ AVA；Grounding DINO 作定位参照，LSD 作传统线段基线。后续结构图实验复用这些框／线，编码整张结构图比较构图。这些构图模型对比未将项目现有豆包 embedding 作为候选模型；2026-10-18 Event 补建复用正式检索的既有 embedding 配置，为后续构图实验补齐候选来源。

模型缓存及隔离环境保存在项目 `.cache/composition-models/`，与日期实验分开，避免再次依赖会被系统清理的 `/tmp`。缓存内有独立忽略文件，不将模型权重放进版本管理。
