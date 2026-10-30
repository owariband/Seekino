# 1. 问题与现状

2026-10-30：用户只希望执行 Building GPT，其余解析需要停止但保留视频。当前没有暂停 API；删除上传会删除媒体。Building GPT 已占容量，正在整集 ASR，Micrograd 同时运行 3 个 Chunk，共享串行 WhisperX；“准备中”无法解释实际阶段。

# 2. 影响因素、假设与评价指标

暂停必须持久化、阻止所有新派发并撤销旧租约，避免旧结果提交；继续沿用版本和已有成果，不重置重试预算。通过队列、提交 fencing、租户与版本冲突测试验证。Building GPT 已有 ASR 执行，不把所有慢都归因于视频名额。

# 3. 改造方案与建模

使用任务 detail 中的 execution_control.paused 作为独立控制状态，不改变业务 phase 或 accepted 合同。Service 持有事务，在父任务锁下设置控制状态，撤销父/Chunk/Interval/Prefix/Summary 租约；未完成子任务回到可领取态。队列、实时准备、Prefix、Summary 的入口统一检查 paused。继续恢复派发，不创建新版本。为兼容尚未重启的旧 Consumer，暂停同时将父/子任务与 Prefix/Summary 的调度时间设置到 2037-01-01（MySQL TIMESTAMP 范围内）；新入口以持久 paused 标记为准，不依赖日期，继续时恢复为当前时间。API 暴露 isPaused、canPause、canResume；任务页显示暂停/继续和整集语音转写阶段，保留原视频。原生 ASR 不可强制中断，停止为尽力取消且迟到结果被 fencing 拒绝。历史过滤复用独立 PR11。

# 4. 对照实验与结果分析

基线没有暂停。先测暂停后不能领取、旧 lease 不得提交、恢复版本/成果不变、越权和历史任务拒绝，再验证前端按钮与请求。180 项队列/总结/实时解析测试通过；新增门禁与状态投影测试、任务页暂停继续交互通过。13:43 开发库实操暂停 Micrograd，父租约 6→7、容量 true→false，活动 Chunk 租约 3→0；Building GPT 原 lease=1 / PID 40888 保持不变。

# 5. 试点验证与方案取舍

正式只暂停非目标任务，不删除媒体；Building GPT 保持执行。部署重启会中断内存 ASR，应避免重复重启。本次保留 9621/PID 40888 的原 Consumer，以 9622 无 Consumer 的 HTTP 实例服务 `/api/jobs/`，其余接口和健康检查仍走 9621；Web 5173 临时代理脚本位于 `outputs/local-services/web-parse-control.mjs`。旧实时请求入口以子任务远期调度时间防绕过；正常重启 dev 后撤下临时实例和路由。记录实际状态与成本，不用单元测试替代整集验收。

# 6. 结果验收与闭环

实现和隔离测试完成；开发任务已暂停。13:56 本机 5173 已部署，浏览器验证进行中=1、Building GPT 显示整集语音转写、Micrograd 显示暂停与继续按钮，当前/历史和暂停筛选可用。控制 API 幂等暂停返回成功，旧租约请求返回 409。目标是其他任务释放执行资源，Building GPT 独立推进，并有用户可操作的暂停/继续入口。整集解析完成另行验收。

代码：暂停 [PR16](https://github.com/owariband/Seekino/pull/16)、历史筛选 [PR11](https://github.com/owariband/Seekino/pull/11)。运行证据：`outputs/verification/parse-pause-20261030/api-controls.json`；部署交接：同目录 `rollout.md`。
