# 2026-11-01 Prompt / Refine 对照证据

本目录保留冻结 Refine 输入、四组 Prompt/最终结果/usage、指标及核对记录；没有凭据、图片、完整模型思考文本或业务表修改。

experiment.py、analyze.py、verify_results.py 是实际执行脚本的副本，用于审查方法；它们依赖原本机实验目录的 control.py、冻结请求、原始 evidence 和环境，不能直接在本目录执行或宣称自包含复跑。原位置：

/Users/hypnos/workspace/agent/Seekino/outputs/verification/video-asr-api-recovery-20261031/prompt-refine-ab-20261101/

离线核验命令（从主工作区，使用项目 Python 3.12）：

.venv/bin/python outputs/verification/video-asr-api-recovery-20261031/prompt-refine-ab-20261101/verify_results.py

该核验不调用模型、不读写数据库。重新运行实模是新的有成本实验；原脚本拒绝覆盖已运行组，不应为了补缺失指标直接删除 started.json。

旧 Understanding 的 SSE/usage 未成功采集，summary.json 中空 usage 表示缺失，不能当作零 token。后续三组已修复采集；四组墙钟及结果完整。analysis.json 中 request_chars 是诊断请求序列化长度，不是实际网络上传字节。reasoning 已包含在 output tokens 中，不应重复加总。

refine-input.json 是两组共享的 compact evidence，cue.words 在进入两组之前统一移除，完整原始来源仍保存在本机 refine-source.json。该设计只比较 cue 级精修，没有新声学对齐或逐词人工验收。
