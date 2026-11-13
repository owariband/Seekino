# 1. 问题与现状

2026-11-14；分支 `fix/voiceprint-media-workspaces`，目标 `dev`，PR 创建后回填。上游实测与背景为 [B12](../../dev/problems.md#b12asr-匿名说话人分组不准refine-看帧后仍未纠正目标归属) / [PR #156](https://github.com/owariband/Seekino/pull/156)。

本机 EP04 整集 1fps/prefill 任务 `368fec7e-b1f3-42b3-9ed6-fd0766be471b` 已保存 24/24 Understanding，随后声纹准备三次均因 FileExistsError 失败，22 个 Refine 窗口未启动。整集媒体上下文持有 video/lease 工作目录；声纹切片以相同任务和租约调用同一目录创建器，`exist_ok=False` 在编码前拒绝。不能据此判断 CAM++ 的人物识别质量。

# 2. 影响因素、假设与评价指标

目录冲突由同一租约内不同媒体操作共享固定目录名引起，重试新租约仍会重现。修改应保留原媒体、租约身份、执行锁、取消清理和崩溃回收；普通 episode/chunk/interval 目录仍须拒绝重复创建。

| 指标 | 基线 | 验收 |
| --- | --- | --- |
| 活跃整集目录内切声纹音频 | FileExistsError | 同一租约的独立切片可执行且互不覆盖 |
| 生命周期 | 尚无共存保证 | 切片退出/失败只清理自己的目录，整集 manifest/音频保留 |
| 租约回收 | 固定目录及 marker/锁 | 新目录保留原 owner marker/锁；无 marker 的遗留目录也能识别和回收 |
| EP04 真实运行 | 24 Under，0 Refine，voiceprint.ready=false | 复用既有成果推进声纹和 Refine；目标身份质量单独验收 |

# 3. 改造方案与建模

只在 Server 的 `VideoParseMediaService` 中为 `prepare_speaker_audio` 申请操作独立的租约临时目录：原 lease 目录名后增加 UUID，marker 仍保存原任务/执行/租约身份。复用既有工作区锁和清理机制，并让 marker 缺失时的路径解析支持该后缀。其余媒体调用保持固定目录语义，不改声纹模型、阈值、Prompt、持久 Schema 或 Gateway。拒绝单纯放开 exist_ok，因为那会共享 speaker.wav 并在清理时删除父媒体。

# 4. 对照实验与结果分析

先用真实整集媒体上下文加替身媒体命令复现同租约切片冲突，再验证多个切片隔离、异常/取消清理、活动锁和遗留回收；先跑相关媒体测试，再覆盖声纹/解析恢复的直接回归。测试数据仅临时目录，不连接外部数据库或模型。已验证：原代码在新增整集共存用例稳定复现 FileExistsError；补丁后媒体与持久 ASR 恢复两组共 71 passed（4.76s），声纹/说话人 worker 定向 52 passed（2.89s，129 deselected）。新用例覆盖同租约多切片隔离、父媒体保留、video/interval 取消或编码失败清理、活动锁和 marker 缺失/损坏回收。git diff --check 通过。真实整集恢复另行验证。

# 5. 整集恢复与取舍

通过回归后仅重启本机隔离 API，继续同一任务的正式 retry-parse，优先复用已保存 ASR 和 24 个理解成果，保持 1fps/prefill/8线程。GPU 仅为既有 ASR/embedding 服务；不连接 CPU/prod。新补丁与原始 dev 失败分开记录。DS worker 继续依据最小声音结果文本/阶段计数判读实际评分和最终归属。

# 6. 验收与闭环

源代码修复、回归测试、真实声纹 ready、Refine 完成及目标是否为长崎爽世分别报告；本修复消除运行阻塞不等于算法准确率达标。同步 devlog、问题 B12 与工作台，关联 PR #156；本修复独立 PR 合入 dev，不混入文档验证 PR。PR #156 是实验文档背景，不是代码依赖；建议先集成 #156，再按本修复 2026-11-14 实测更新 B12，合并时保留两次验证事实。源码和 123 项直接回归已通过；2026-11-14 00:36:45（UTC+8）真实核实 voiceprint.ready=true，298 行/298 个样本、298 次 CAM++ 编码 completed，原 24 份理解仍保留，声纹准备 failures=0。目录冲突的运行阻塞已消除。随后 Refine 的初始输入因 ContextWindowOverflow 被本地预算校验拒绝，全链仍未跑通，最终身份尚未验收；该新问题属于声纹证据向模型呈现的体积控制，另行修复，不以提高窗口上限掩盖。复核证据见 [运行摘要](../../report/server/service/video_parse_media/voiceprint-workspaces-20261114.json)。
