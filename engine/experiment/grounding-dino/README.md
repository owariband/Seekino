# Grounding DINO 实验

本目录集中维护 Grounding DINO 的本地实验。每次实验独立建目录，在自己的 `README.md` 记录问题、输入、模型与参数、结果、局限和复现方式；脚本、原始输出和可视化与报告放在一起。

| 实验 | 问题 | 状态 |
| --- | --- | --- |
| [20261016-mygo-local](./20261016-mygo-local/README.md) | MyGO 第一集六帧的检测效果、MPS / CPU 耗时与内存 | 完成 |
| [20261016-mygo-resolution](./20261016-mygo-resolution/README.md) | 同六帧在 360p / 480p / 720p / 1080p 下的检测稳定性 | 完成 |
| [20261017-interior-layout](./20261017-interior-layout/README.md) | 用户大厅截图：普通物件、建筑结构和直接上下分区提示的真实检测结果 | 完成 |
| [20261017-aquarium-vlm-vs-dino](./20261017-aquarium-vlm-vs-dino/README.md) | 新图先冻结助手线段与人物框，再运行 DINO 比较 | 完成 |

`20261016-mygo-local/` 由原来的 `engine/experiment/20261016-grounding-dino-mygo/` 迁入，原来的同名 `.md` 报告迁为该目录的 `README.md`。原始结果和输入字节保持不变；历史结果 JSON 中的执行参数仍保留当时的路径。
