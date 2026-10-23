# 本次实验文件与执行范围

生产代码 0 文件、0 行；新增实验目录 87 个文件，其中 6 个离线脚本共 1091 行，音频 47 个、矩阵 10 个。另修改交接文档 1 个文件（相对此轮写前快照 +33/-9 行）。下载的官方实现与模型缓存单独列出，不算自写生产改动。

新增脚本只承担固定音频准备、官方模型 CPU 提取、比较和独立复算；没有改动 API、数据库、配置、前端、Gateway 或解析流程。主 Python 环境没有安装新依赖。

```text
AniVAG/
├── engine/
│   ├── design/
│   │   └── [M] 多模态理解解析研究.md  ← 更新候选状态与实测结论
│   └── experiment/
│       └── speaker-encoder/
│           └── 20261018-mygo-ep04-model-comparison/
│               ├── [A] README.md
│               ├── [A] artifacts.md
│               ├── audio/
│               │   ├── [A] balanced_null_target.wav
│               │   ├── [A] balanced_soyo_baseline.wav
│               │   ├── [A] balanced_soyo_confused_01.wav
│               │   ├── [A] balanced_soyo_downcast_01.wav
│               │   ├── [A] balanced_soyo_gentle_01.wav
│               │   ├── [A] balanced_soyo_ordinary_02.wav
│               │   ├── [A] balanced_taki_baseline.wav
│               │   ├── [A] balanced_taki_promise.wav
│               │   ├── [A] balanced_taki_suggest.wav
│               │   ├── [A] balanced_taki_wistful.wav
│               │   ├── [A] balanced_taki_work.wav
│               │   ├── [A] balanced_tomori_baseline.wav
│               │   ├── [A] balanced_tomori_plain.wav
│               │   ├── [A] balanced_tomori_reflect.wav
│               │   ├── [A] balanced_tomori_resolve.wav
│               │   ├── [A] balanced_tomori_suggest.wav
│               │   ├── [A] v11_00.wav
│               │   ├── [A] v11_01.wav
│               │   ├── [A] v11_02.wav
│               │   ├── [A] v11_03.wav
│               │   ├── [A] v11_04.wav
│               │   ├── [A] v11_05.wav
│               │   ├── [A] v11_06.wav
│               │   ├── [A] v11_07.wav
│               │   ├── [A] v11_08.wav
│               │   ├── [A] v11_09.wav
│               │   ├── [A] v11_10.wav
│               │   ├── [A] v11_11.wav
│               │   ├── [A] v11_12.wav
│               │   ├── [A] v11_13.wav
│               │   ├── [A] v11_14.wav
│               │   ├── [A] v11_15.wav
│               │   ├── [A] v11_16.wav
│               │   ├── [A] v11_17.wav
│               │   ├── [A] v11_18.wav
│               │   ├── [A] v11_19.wav
│               │   ├── [A] v11_20.wav
│               │   ├── [A] v11_21.wav
│               │   ├── [A] v11_22.wav
│               │   ├── [A] v11_23.wav
│               │   ├── [A] v11_24.wav
│               │   ├── [A] v11_25.wav
│               │   ├── [A] v11_26.wav
│               │   ├── [A] v11_27.wav
│               │   ├── [A] v11_28.wav
│               │   ├── [A] v11_29.wav
│               │   └── [A] v11_30.wav
│               ├── campp_baseline/
│               │   └── [A] embeddings.json
│               ├── [A] compare.py
│               ├── eres2netv2/
│               │   ├── [A] embeddings.json
│               │   ├── [A] provenance.json
│               │   ├── [A] run.log
│               │   ├── [A] runner.py
│               │   └── [A] status.json
│               ├── [A] manifest.json
│               ├── matrices/
│               │   ├── [A] campp_baseline-balanced.csv
│               │   ├── [A] campp_baseline-v11.csv
│               │   ├── [A] eres2netv2-balanced.csv
│               │   ├── [A] eres2netv2-v11.csv
│               │   ├── [A] redimnet2_mixed-balanced.csv
│               │   ├── [A] redimnet2_mixed-v11.csv
│               │   ├── [A] redimnet2_vox2-balanced.csv
│               │   ├── [A] redimnet2_vox2-v11.csv
│               │   ├── [A] wavlm_large-balanced.csv
│               │   └── [A] wavlm_large-v11.csv
│               ├── [A] preparation.json
│               ├── [A] prepare_samples.py
│               ├── redimnet2_mixed/
│               │   ├── [A] embeddings.json
│               │   ├── [A] provenance.json
│               │   ├── [A] run.log
│               │   └── [A] status.json
│               ├── redimnet2_vox2/
│               │   ├── [A] embeddings.json
│               │   ├── [A] provenance.json
│               │   ├── [A] run.log
│               │   ├── [A] runner.py
│               │   └── [A] status.json
│               ├── [A] results.json
│               ├── [A] verification.json
│               ├── [A] verify.py
│               └── wavlm_large/
│                   ├── [A] embeddings.json
│                   ├── [A] provenance.json
│                   ├── [A] run1.log
│                   ├── [A] run2.log
│                   ├── [A] runner.py
│                   └── [A] status.json
└── outputs/
    └── local-models/  ← 下载缓存，不纳入代码提交
        ├── redimnet2/
        │   ├── [A] b6-vox2-lm.pt  ← 官方权重
        │   ├── [A] b6-vb2+vox2+cnc2_v0-lm.pt  ← 官方权重
        │   └── 官方固定版本源码／下载证明等附属文件
        ├── eres2netv2/
        │   ├── [A] pretrained_eres2netv2.ckpt  ← 官方权重
        │   └── 下载证明等附属文件
        └── wavlm-speaker-sv/
            ├── checkpoints/
            │   ├── [A] wavlm_large_finetune.pth  ← 专用 SV 镜像权重
            │   ├── [A] wavlm_large_backbone.pt  ← 仅取初始化配置
            │   └── 下载错误响应等附属证据
            └── UniSpeech／s3prl 官方固定版本源码等附属文件
```

ERes2NetV2 复用了此前已存在的 `outputs/local-models/campplus/3D-Speaker` 官方源码；未修改该检出。上述下载目录内的原始源码、Git 元数据、网络响应不是手写实现，完整来源和哈希见各模型 provenance。

| 缓存目录 | 当前逻辑文件总字节数（含源码、权重及下载证明） |
|---|---:|
| `outputs/local-models/redimnet2` | 104,198,872 |
| `outputs/local-models/eres2netv2` | 71,769,302 |
| `outputs/local-models/wavlm-speaker-sv` | 2,570,338,707 |

本轮校验包括：47 份音频 SHA／PCM SHA、5 组各 47 份向量、原始标签保留、两套参考池隔离、目标自身／重复／重叠排除、逐对矩阵和两种分数的独立复算。详见 [verification.json](verification.json)。

该目录、`engine/design/` 和模型缓存均被 Git 忽略。当前混合工作区的其他 staged／unstaged／untracked 内容不是本轮成果；没有强制 add、commit、push、服务重启、数据库写入或重新解析。

相对此轮写前快照的文档差异可用：

```sh
git diff --no-index /tmp/anivag-multimodal-research-before-model-comparison.md engine/design/多模态理解解析研究.md
```

脚本语法与 Markdown 本地链接已检查；只复算静态实验结果，没有为本次离线实验运行生产全仓测试。
