# macOS 嵌入宿主验证

2026-11-23，Apple Silicon，macOS 26.5.1，CPython 3.12.16。本文记录 PR 1 的后端试验。模型返回固定的结构化结果和向量，不需要账户密钥。窗口、声音输出和持续播放由 PR 2 验收。

## 一部视频怎样走到原片

从空目录调用 `open_library`，建立系列，导入 2 秒、带 AAC 音轨的 MP4。相同请求键再次导入，得到原来的 video ID 和 task ID。实际 V2 Consumer 领任务，经过 Prepare、Understanding、Refine 和 INDEX。编码走 VideoToolbox，索引和结果写入本机 SQLite。

“红旗”的关键词查询没有调用向量模型，另一条描述查询调用了向量模型。两次都命中同一部片的 **[300, 1200) 毫秒**。用返回的 source ref 再查当前版本，读到的受管原片和导入文件的字节相同。300 毫秒所取画面与原片对得上，900 毫秒的单声道音频有 14,400 个 16 kHz 样本，声音标记和静音的位置符合样片。关闭重开后，关键词查询仍能回看原片；重解析完成后，旧引用失效，新引用能打开。[端到端用例](../../../../tests/server/test_desktop_pipeline.py)

第二次回放把窗口缩到 1 秒，在第一个 Understanding 请求执行时暂停任务，然后恢复。两段均完成解析和跨段 Refine，搜索回看分别落在 [300, 1000) 和 [1300, 2000) 毫秒。同一输入从继续操作提交到任务完成，60 秒心跳实测为 58.483 秒。桌面改用 2 秒心跳后实测为 2.337 秒。两组各运行一次，服务端保留原配置。桌面默认关闭可选的 Prefix 压缩。

## 实际运行了几个进程

验收程序复制并移动构建好的宿主，用一个测试入口替换命令行入口。可执行文件、Python、后端和媒体库都来自原产物。副本仅增加 pytest、固定模型响应和合成片生成代码。清空有效 PATH，传入无效的云端数据库地址，检查包内找不到 PyMySQL、Elasticsearch、elastic-transport 或 Redis 客户端，再跑上面的链路。模型边界拒绝向网络建立连接。[构建程序](../../../../deploy/desktop/build_host.py) · [验证程序](../../../../deploy/desktop/verify_host.py)

macOS 观察器挂起新宿主，在运行第一条指令前注册 `EVFILT_PROC`，收到退出事件后结束。阳性对照能看到 shell 创建的短命子进程。真实宿主运行期间没有 fork 或 exec 事件，Python 审计同样记录 0 次。dyld 报告 libpython 来自包内 Frameworks，libavformat、libavcodec、libavutil、libswscale、libswresample 和 sqlite-vec 来自包内 Resources。以上是这个宿主和这组输入的实测结果。[系统观察器](../../../../deploy/desktop/verify_process.c) · [包内动态库检查](../../../../deploy/desktop/verify_runtime.py)

自建 FFmpeg 9.0.2 和 PyAV 19.0.2 的 wheel 已经在同一原生宿主上重跑两个回放。macOS 系统观察器的阳性对照和宿主结果，以及 dyld 实际装入的库，见原始运行记录。构建配置禁用 FFmpeg 命令程序，使用系统 VideoToolbox 编码。 [媒体构建记录](media-build.md) · [宿主运行记录](host-run.json)

## 数据与媒体回归

- 两个连接争抢同一任务时只有一个获得租约；持锁等待期间任务暂停或租约到期，原工人的提交被拒绝。强退释放宿主文件锁，新进程从 SQLite 回收过期租约。并发重解析只产生一个新版本。[文件库竞争测试](../../../../tests/server/test_desktop_database.py)
- 桌面打开资料库后，恢复上次强退遗留的上传令牌。分别在探测原片、原片已落位而任务未提交、任务已提交但暂存未清理三个时刻强退。前两种可立即重试且只创建一个任务；后一种无需重新导入即可回收暂存。应用仅清理有已完成上传记录的受管目录。[上传强退恢复](../../../../tests/server/test_desktop_upload_recovery.py)
- 投影已经写入但事务尚未提交时注入失败，关键词索引、向量和发布回执一并回滚。重领 INDEX 能完成发布。删除派生投影后，搜索提示需要重建；按同一模型合同重建不追加 embedding 调用。短词、跨资料范围干扰、新连接加载 sqlite-vec 和不同合同重开均单独验证。[SQLite 索引测试](../../../../tests/server/test_sqlite_index.py)
- 桌面保留单独的视觉预处理合同，记录实际 MP4 编码器和 JPEG 预处理版本。导入、索引收据和重建使用同一合同；库内已有数据时换成不匹配的旧版本会被拒绝。[合同与重建测试](../../../../tests/server/test_desktop_visual_contract.py)
- 用 16 部合成视频、1,024 个 Chunk、4,096 个 Event 和 512 维向量测了 7 次暖查询。在全库范围，覆盖检查、全文候选和精确向量候选的中位数分别为 1.89、9.81、19.30 毫秒。该计时不含生成向量和回源核验。[条件与原始样本](index-bench.md)
- 同源视频分别走 CLI 与 PyAV，覆盖 H.264、HEVC、VFR、旋转、非零起点、音频长尾、无音轨和双音轨、取消和损坏文件。媒体相关的 57 个定向用例通过。[媒体对照](../../../../tests/server/test_embedded_media.py)
- HTTP 入口在新建的 MySQL 8.4.7 和 ES 8.19.22 上跑通上传、解析、搜索和原片片段下载。真实库相关 65 项通过；另有 539 项 HTTP、装配和存储回归通过。模型回复固定。[云端回归与命令](cloud-regression.md)

**最终定向回归共 447 项通过。** 运行时使用实际交付包里的 PyAV 与 sqlite-vec，开启 VideoToolbox 硬件用例。覆盖桌面入口、空库配置、上传强退恢复、SQLite 队列与索引、视觉合同、原片回看、Gateway 工厂、服务端装配、上传和两套索引协调器。各组与上文的专项统计有重叠。下列命令从仓库根目录运行，`app` 指向自建媒体 wheel 构出的宿主。

```sh
PYTHONDONTWRITEBYTECODE=1 SEEKINO_TEST_VIDEOTOOLBOX=1 \
PYTHONPATH="$app/Contents/Resources/site-packages:." \
.venv/bin/python -B -m pytest -p no:cacheprovider -q \
  tests/server/test_storage_profiles.py tests/server/test_desktop*.py \
  tests/server/test_sqlite_index.py tests/server/test_embedded_media.py \
  tests/server/test_video_parse_media.py tests/engine/test_frame_sampler.py \
  tests/server/test_bootstrap.py tests/gateway/test_model_factory.py tests/server/test_upload_service.py \
  tests/server/test_video_parse_index_coordinator.py tests/server/test_video_event_index_coordinator.py \
  tests/server/test_video_event_retrieval.py
```

依赖检查中的 `test_imports_do_not_load_v1_in_a_fresh_process` 仍尝试导入基线中已不存在的 `engine.common.model_factory`。较广的持久化测试中，`test_retrieval_scope_reads_only_task_and_chunk_projections_without_lazy_loading` 期望 2 次 SQL，实际 3 次。用本次实现前的 PR 提交单独运行，得到相同结果。正式素材上按台词、语义和视觉各 20 条做 Hit@5、Recall@10 对照，真实模型费用和质量、商业包安装与连续播放，均待验收。
