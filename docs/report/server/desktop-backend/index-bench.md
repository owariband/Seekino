# SQLite 本地索引查询耗时

2026-11-23，在一台 Apple Silicon 开发机上用临时文件库写入 16 个合成视频，每部 64 个 Chunk，每个 Chunk 4 个 Event，共 4096 个 Event。每个 Event 有一个 512 维文本向量和一个包含“杯子”的中文描述。运行环境为 macOS 26.5.1、CPython 3.12.16、SQLite 3.53.1、sqlite-vec 0.1.6。

查询一次限定一部视频，另一次涵盖全库。FTS5 搜“杯子”，向量通道在同一范围内求精确余弦距离，两者各取前 20 个 Event。每组先预热一次，再连续测七次。下表给出每组的中位数。[七次测量的原始记录](index-samples.json)

| 查询范围 | 核对投影覆盖 | FTS5 关键词 | 精确向量 |
| --- | ---: | ---: | ---: |
| 64 个 Chunk / 256 个 Event | 0.14 ms | 1.52 ms | 0.74 ms |
| 1024 个 Chunk / 4096 个 Event | 1.89 ms | 9.81 ms | 19.30 ms |

计时含 SQL、读取和构造候选。生成查询向量、从业务表确定授权范围、回源核验和 UI 展示的时间需要另测。合成素材对“杯子”全部命中，这轮只测限定范围后的查询耗时。真实视频的首个正确命中名次、top-k 覆盖率及与 ES 的差异待验。

在仓库根目录用安装了 `desktop` 依赖组的环境运行下面的命令即可复测。脚本自建临时库，退出时清理。[测量脚本](index_bench.py)

```sh
PYTHONPATH=. uv run --group desktop python docs/report/server/desktop-backend/index_bench.py
```
