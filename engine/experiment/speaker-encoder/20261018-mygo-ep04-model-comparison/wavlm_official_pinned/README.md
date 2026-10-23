# WavLM 官方指定版本复测

同一份 Large SV 权重、同一批 47 段 PCM，与之前的兼容封装逐项比较。

完成运行：`run1`。近似等价检查：**True**。

| 检查 | 实际最大差／最小值 | 预先固定的界限 |
|---|---:|---:|
| 归一化向量逐元素最大差 | 3.30430438505e-06 | ≤1e-5 |
| 同一音频两条路径的向量 cosine 最小值 | 0.99999999988 | ≥0.999999 |
| 585 个池内配对的 cosine 最大差 | 4.59211844922e-06 | ≤1e-5 |

## 已确认身份条件

逐条 cosine 等权均值，保留原登记标签、参考池和目标边界，排除自身／重复／重叠。

| 路径 | 目标 | 正确人物 | 正确人物均值 | 最高候选／均值 | 正确人物名次 |
|---|---|---|---:|---|---:|
| previous_wrapper | v11_02 | 千早爱音 | 0.723932399 | 千早爱音／0.723932399 | 1 |
| previous_wrapper | v11_27 | 长崎爽世 | 0.242440119 | 要乐奈／0.529823108 | 5 |
| previous_wrapper | balanced_null_target | 长崎爽世 | 0.434954155 | 长崎爽世／0.434954155 | 1 |
| official_pinned | v11_02 | 千早爱音 | 0.723933327 | 千早爱音／0.723933327 | 1 |
| official_pinned | v11_27 | 长崎爽世 | 0.242439711 | 要乐奈／0.529824682 | 5 |
| official_pinned | balanced_null_target | 长崎爽世 | 0.434954239 | 长崎爽世／0.434954239 | 1 |

## 按原登记标签的辅助分离度

| 路径 | v11（排除两条已知错标，29 refs）AUC | balanced（15 refs）AUC |
|---|---:|---:|
| previous_wrapper | 0.797699075 | 0.900444444 |
| official_pinned | 0.797699075 | 0.900444444 |

这些参考身份未全部人工审核，AUC 衡量与登记标签的一致性，不是人物准确率。

## 实际运行方式

直接执行官方 UniSpeech `verification.init_model`／ECAPA，特征来自官方指定 commit 的 s3prl `UpstreamExpert`、`UpstreamBase` 和 hooks。只将远程 hub 下载入口路由到同版本提供的 `wavlm_local`，读取已有本地初始化文件；模型网络、前处理和特征代码没有替换。原模型先按官方入口载入，再检查所有 embedding 状态键并仅排除训练分类投影后严格加载。

Python：`3.9.25 (main, Oct 31 2025, 23:27:22) `；平台：`macOS-26.4.1-x86_64-i386-64bit`。
依赖版本：`{"torch": "1.9.0", "torchaudio": "0.9.0", "numpy": "1.20.3", "scipy": "1.7.1", "s3prl": "0.3.1", "fairseq": "0.12.2", "soundfile": "0.13.1", "fire": "0.4.0", "sentencepiece": "0.1.96", "sklearn": "0.0", "scikit-learn": "1.0.2", "omegaconf": "2.0.6"}`。

s3prl 固定到官方 README 指定的 `7ab62aaf2606d83da6c71ee74e7d16e0979edbc3`。原 requirements 的六个版本保留；Torch 1.9.0／NumPy 1.20.3／Python 3.9.25 为兼容运行选择，fairseq 使用官方完整 v0.12.2 tag 原样构建。训练和其他 upstream 的依赖未全部安装，实际运行依赖及缺失项逐一记录，不宣称复原完整训练环境。

原生 ARM 安装遇到旧 NumPy／SciPy 与 sentencepiece 编译问题，实际推理使用本机已有 Rosetta 执行 x86_64 二进制包。官方 PyPI fairseq 0.12.2 sdist 缺少两份 C++ 源文件；切换完整同版本 Git tag 后构建成功。模型源码没有补丁。上游 requirements 的传递依赖未完整锁 hash，因此保留六个版本、记录实际 freeze，没有宣称完整 hash-lock 安装成功。

47 条前向累计 125.906 秒，单段中位 2191.463ms。CPU／FP32／4 线程／batch=1，另做一次预热；时间不含安装、导入和模型加载。不同环境或架构下的时间不作为生产吞吐比较。

本次仍使用上一轮同一份可追溯镜像 SV 权重；微软原文件不可直接下载，尚未与其逐字节核对。这项来源限制与执行链路是否数值等价分别记录。

[完整差异与逐条候选](comparison.json) · [本次向量](run1/embeddings.json) · [本次来源与资源](run1/provenance.json)

[独立复算](verification.json) · [隔离环境与失败记录](../../../../../outputs/local-models/wavlm-speaker-sv/official-pinned/environment.json) · [完整 freeze](../../../../../outputs/local-models/wavlm-speaker-sv/official-pinned/freeze.txt)

## 复现

从项目根目录执行。保留已有运行；重新提取时选择尚不存在的 `run2` 或其他名称。

```sh
outputs/local-models/wavlm-speaker-sv/official-pinned/env-x86/bin/python -u engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/runner.py --output engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/run2
.venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/compare_official.py --run engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/run2
.venv/bin/python engine/experiment/speaker-encoder/20261018-mygo-ep04-model-comparison/wavlm_official_pinned/verify.py
```

本轮生产逻辑 0 行；实验及隔离环境目录均被 Git 忽略，原项目环境、原实验音频与向量保留，未执行服务重启、迁移、解析任务、commit 或 push。
