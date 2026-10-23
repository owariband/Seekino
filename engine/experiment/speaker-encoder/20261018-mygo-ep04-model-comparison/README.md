# 第四集固定音频：声纹编码器对照

输入共47段：v11登记31段，旧balanced参考15段及1段目标。全部WAV的PCM与历史样本一致，参考池分开。模型、向量和来源详见JSON；未修改正式声纹库、speaker、Gateway或解析任务。

## 已确认目标的逐条cosine均值

每行分别使用该条件原有参考，排除目标自身、重复和重叠。v11仍保留原库其他错标，不能把两组不同边界／来源的爽世条件混成同一实验。

对单位向量，逐条均值为 `mean(dot(target, ref_i))`；质心分数为 `dot(target, normalize(mean(ref_i)))`，分开报告。没有套用 0.6 等新通过阈值。

| 模型 | 目标 | 真实人物 | 正确人物均值 | 均值最高候选 | 最高均值 | 第二候选 | 第一减第二 | 正确人物排名 |
|---|---|---|---:|---|---:|---|---:|---:|
| CAM++（冻结基线） | v11 293.500–296.000s | 千早爱音 | 0.661548 | 千早爱音 | 0.661548 | 高松灯 | 0.163359 | 1 |
| CAM++（冻结基线） | v11 47.500–48.800s | 长崎爽世 | 0.418053 | 丰川祥子 | 0.515009 | 要乐奈 | 0.078891 | 4 |
| CAM++（冻结基线） | balanced 47.700–48.700s | 长崎爽世 | 0.617295 | 长崎爽世 | 0.617295 | 高松灯 | 0.152796 | 1 |
| ERes2NetV2 common | v11 293.500–296.000s | 千早爱音 | 0.666774 | 千早爱音 | 0.666774 | 高松灯 | 0.225529 | 1 |
| ERes2NetV2 common | v11 47.500–48.800s | 长崎爽世 | 0.450785 | 丰川祥子 | 0.535569 | 千早爱音 | 0.031243 | 4 |
| ERes2NetV2 common | balanced 47.700–48.700s | 长崎爽世 | 0.588923 | 长崎爽世 | 0.588923 | 椎名立希 | 0.098886 | 1 |
| ReDimNet2-B6 VoxCeleb2 LM | v11 293.500–296.000s | 千早爱音 | 0.643885 | 千早爱音 | 0.643885 | 长崎爽世 | 0.318934 | 1 |
| ReDimNet2-B6 VoxCeleb2 LM | v11 47.500–48.800s | 长崎爽世 | 0.486330 | 长崎爽世 | 0.486330 | 要乐奈 | 0.026857 | 1 |
| ReDimNet2-B6 VoxCeleb2 LM | balanced 47.700–48.700s | 长崎爽世 | 0.631209 | 长崎爽世 | 0.631209 | 椎名立希 | 0.402377 | 1 |
| ReDimNet2-B6 mixed LM | v11 293.500–296.000s | 千早爱音 | 0.789628 | 千早爱音 | 0.789628 | 高松灯 | 0.192074 | 1 |
| ReDimNet2-B6 mixed LM | v11 47.500–48.800s | 长崎爽世 | 0.661716 | 长崎爽世 | 0.661716 | 要乐奈 | 0.007730 | 1 |
| ReDimNet2-B6 mixed LM | balanced 47.700–48.700s | 长崎爽世 | 0.740706 | 长崎爽世 | 0.740706 | 椎名立希 | 0.282165 | 1 |
| WavLM-Large SV | v11 293.500–296.000s | 千早爱音 | 0.723932 | 千早爱音 | 0.723932 | 要乐奈 | 0.308256 | 1 |
| WavLM-Large SV | v11 47.500–48.800s | 长崎爽世 | 0.242440 | 要乐奈 | 0.529823 | 千早爱音 | 0.157963 | 5 |
| WavLM-Large SV | balanced 47.700–48.700s | 长崎爽世 | 0.434954 | 长崎爽世 | 0.434954 | 高松灯 | 0.199849 | 1 |

v11 经典爽世条件中，两支 ReDimNet2 都把爽世排第一，但第二名乐奈仅 1 条参考，逐条均值差距分别只有 0.026857／0.007730。较高的绝对分数不表示更可靠；这个固定参考库尚不足以据此自动填写所有 speaker。

## 同目标的归一化质心分数

| 模型 | 目标 | 最高候选 | 质心cosine | 第一减第二 | 正确人物排名 |
|---|---|---|---:|---:|---:|
| CAM++（冻结基线） | v11 293.500–296.000s | 千早爱音 | 0.788730 | 0.151062 | 1 |
| CAM++（冻结基线） | v11 47.500–48.800s | 丰川祥子 | 0.696128 | 0.121401 | 2 |
| CAM++（冻结基线） | balanced 47.700–48.700s | 长崎爽世 | 0.744100 | 0.194285 | 1 |
| ERes2NetV2 common | v11 293.500–296.000s | 千早爱音 | 0.774315 | 0.224363 | 1 |
| ERes2NetV2 common | v11 47.500–48.800s | 丰川祥子 | 0.730792 | 0.132403 | 4 |
| ERes2NetV2 common | balanced 47.700–48.700s | 长崎爽世 | 0.706053 | 0.129143 | 1 |
| ReDimNet2-B6 VoxCeleb2 LM | v11 293.500–296.000s | 千早爱音 | 0.779003 | 0.334949 | 1 |
| ReDimNet2-B6 VoxCeleb2 LM | v11 47.500–48.800s | 长崎爽世 | 0.664584 | 0.155446 | 1 |
| ReDimNet2-B6 VoxCeleb2 LM | balanced 47.700–48.700s | 长崎爽世 | 0.779929 | 0.505593 | 1 |
| ReDimNet2-B6 mixed LM | v11 293.500–296.000s | 千早爱音 | 0.858459 | 0.127328 | 1 |
| ReDimNet2-B6 mixed LM | v11 47.500–48.800s | 长崎爽世 | 0.780476 | 0.071407 | 1 |
| ReDimNet2-B6 mixed LM | balanced 47.700–48.700s | 长崎爽世 | 0.833478 | 0.332909 | 1 |
| WavLM-Large SV | v11 293.500–296.000s | 千早爱音 | 0.830705 | 0.262414 | 1 |
| WavLM-Large SV | v11 47.500–48.800s | 要乐奈 | 0.529823 | 0.091418 | 4 |
| WavLM-Large SV | balanced 47.700–48.700s | 长崎爽世 | 0.551532 | 0.253910 | 1 |

## 资源记录

| 模型 | 输出维数 | 本轮提取数 | 提取中位ms | 合计ms |
|---|---:|---:|---:|---:|
| CAM++（冻结基线） | 192 | 0 | 冻结复用 | — |
| ERes2NetV2 common | 192 | 47 | 34.032 | 1969.744 |
| ReDimNet2-B6 VoxCeleb2 LM | 192 | 47 | 144.948 | 7829.894 |
| ReDimNet2-B6 mixed LM | 192 | 47 | 138.164 | 7527.518 |
| WavLM-Large SV | 256 | 47 | 124.285 | 7240.235 |

计时均为本机 CPU、FP32、4 线程、batch=1，累计为 47 段前向；不含下载、依赖导入或 checkpoint 加载。ERes2NetV2 与 WavLM-Large 各有一次预热；ReDimNet2 包含首条冷调用。各模型 provenance 另存加载时间、首条时间和进程峰值 RSS，不能直接推算生产并发吞吐。CAM++ 复用冻结向量，本轮未重新计时。

WavLM-Large 的专用 SV 权重为 1,301,926,579 字节，另外保留 1,261,965,425 字节的上游初始化 checkpoint 用于读取精确配置；最终编码器与 ECAPA 权重全部来自专用 SV 文件。模型加载 4.583 秒，首次预热 2.007 秒，进程峰值 RSS 约 2.843GB。权重和源码集中保存在 `outputs/local-models/`，本轮未安装新依赖。

WavLM 官方 Google Drive 下载限额、另一个 OneDrive 链接 403，因此使用 [UniAudio 作者镜像](https://huggingface.co/Dongchao/UniAudio/resolve/ec5555f9bd5038a606e09bf3399dc3bc6798d62d/wavlm_large_finetune.pth)。文件 SHA 与另外两处镜像一致，其中 [模型卡](https://huggingface.co/bezzam/wavlm_large_finetune_seed_tts_eval)明确指向微软同一专用 SV 文件；未能直接与不可下载的微软原件逐字节核对。复用官方 standalone WavLM 和 ECAPA，适配 s3prl 的 25 层 hooks；710 个模型状态键严格加载，唯一显式排除项为训练分类投影 `loss_calculator.projection.weight`。网络和后端没有随机缺失参数，兼容适配与失败日志均保留。已按源码核对适配语义，未另装旧 s3prl 全管线做数值等价复测。

## 原音频、向量与复现

| 条件 | 原音频 | 真值依据 |
|---|---|---|
| v11_02：293.500–296.000s | [WAV](audio/v11_02.wav) | 用户确认：千早爱音 |
| v11_27：47.500–48.800s | [WAV](audio/v11_27.wav) | 用户确认：长崎爽世 |
| balanced_null_target：47.700–48.700s | [WAV](audio/balanced_null_target.wav) | 用户确认：长崎爽世 |

[完整候选、逐条参考分数和来源](results.json) · [固定样本](manifest.json) · [PCM核验](preparation.json) · [独立复算](verification.json) · [本次文件清单](artifacts.md)

- CAM++（冻结基线）：[47 条原始向量](campp_baseline/embeddings.json)；来源记录包含在向量文件中。
- ERes2NetV2 common：[47 条原始向量](eres2netv2/embeddings.json)、[权重／源码／资源记录](eres2netv2/provenance.json)
- ReDimNet2-B6 VoxCeleb2 LM：[47 条原始向量](redimnet2_vox2/embeddings.json)、[权重／源码／资源记录](redimnet2_vox2/provenance.json)
- ReDimNet2-B6 mixed LM：[47 条原始向量](redimnet2_mixed/embeddings.json)、[权重／源码／资源记录](redimnet2_mixed/provenance.json)
- WavLM-Large SV：[47 条原始向量](wavlm_large/embeddings.json)、[权重／源码／资源记录](wavlm_large/provenance.json)

已有向量的比较与独立复算：

```sh
.venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/compare.py
.venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/verify.py
```

`matrices/`保存每模型各参考池的完整成对分数，空白对角线不作为证据。各模型目录保存原始向量、来源版本与运行记录。

只有两个不同台词具有用户确认真值，其中爽世有两个边界／参考条件；其余登记身份未全部人工审核。这里不估计整集准确率，不以绝对分数变大证明更好；未自动按top1写回。
